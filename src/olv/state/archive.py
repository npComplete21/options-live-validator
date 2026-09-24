"""Kafka -> Parquet. See docs/IMPLEMENTATION_PLAN.md sections 11 and 12.

Kafka is a buffer with seven days of retention; this is the permanent record,
and per section 0 it is the primary artifact of the whole program — the only
dataset from which a 0DTE backtest can later be run by replay.

**Commit ordering is the point.** Offsets are committed only *after* a batch is
durably written, never before and never automatically. The consequence is
at-least-once: a crash between the write and the commit replays that batch and
duplicates rows. That is the correct trade — a duplicate row is recoverable by
deduplicating on ``(symbol, quote_ts)``, whereas a lost row is gone for good
and the session cannot be re-observed.

At Phase 4 the strategy consumers need something stronger, because a duplicate
*decision* opens a position twice and no amount of later deduplication undoes
a filled order. That is what the transactional offset-with-state design in
section 12 is for. The archiver does not need it, and pretending otherwise
would obscure why the strategy path does.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from olv.common.kafka import DEFAULT_BOOTSTRAP, chain_topic, consumer_config, quotes_topic

logger = logging.getLogger(__name__)

ARCHIVE_GROUP = "archiver.quotes"
REJECTION_GROUP = "archiver.rejections"

#: Hive dataset names under the archive root. Rejections live beside accepted
#: quotes rather than inside them: a reader who forgets a status filter would
#: otherwise enrich crossed markets and stale prints as though they were
#: tradeable, which is the same class of silent contamination that stamping
#: ``feed_source`` on every row exists to prevent.
QUOTE_DATASET = "quotes"
REJECTION_DATASET = "rejections"

#: Deduplication key for readers, given at-least-once delivery.
DEDUPE_KEYS = ("symbol", "quote_ts")

#: A rejected quote is refused at most once per snapshot, so the instant plus
#: the contract identifies it. ``quote_ts`` is deliberately absent: a quote
#: rejected as MISSING may have no timestamp at all.
REJECTION_DEDUPE_KEYS = ("symbol", "observed_at")

#: A message without these cannot be archived. Anything else on the topic is
#: quarantined and counted rather than crashing the run: a recorder that dies
#: on one malformed message loses the rest of a session that cannot be
#: re-observed, which is a far worse outcome than dropping the message.
REQUIRED_FIELDS = ("symbol", "observed_at", "quote_ts")


@dataclass(frozen=True, slots=True)
class ArchiveStats:
    messages: int
    files_written: int
    batches: int
    malformed: int = 0

    def __str__(self) -> str:
        suffix = f", {self.malformed} malformed" if self.malformed else ""
        return (
            f"{self.messages} messages -> {self.files_written} file(s) "
            f"in {self.batches} batch(es){suffix}"
        )


def is_archivable(payload: object) -> bool:
    """Whether a decoded message carries the fields the archive schema needs."""
    return isinstance(payload, dict) and all(
        payload.get(field) is not None for field in REQUIRED_FIELDS
    )


def partition_dir(
    root: Path, ticker: str, session_date: dt.date, dataset: str = QUOTE_DATASET
) -> Path:
    """Hive-style partitioning, so DuckDB and Polars can prune on both keys."""
    return Path(root) / dataset / f"ticker={ticker.upper()}" / f"date={session_date.isoformat()}"


def write_batch(
    rows: list[dict[str, Any]], root: Path, ticker: str, dataset: str = QUOTE_DATASET
) -> list[Path]:
    """Write rows to Parquet, one file per session date in the batch."""
    if not rows:
        return []

    frame = pl.DataFrame(rows, infer_schema_length=None)
    written: list[Path] = []
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")

    for session_date, group in _by_session_date(frame):
        target = partition_dir(root, ticker, session_date, dataset)
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"part-{stamp}-{len(written)}.parquet"
        group.write_parquet(path, compression="zstd")
        written.append(path)
    return written


def _by_session_date(frame: pl.DataFrame):
    dates = frame.get_column("observed_at").str.slice(0, 10)
    frame = frame.with_columns(dates.alias("_session_date"))
    for (value,), group in frame.group_by(["_session_date"], maintain_order=True):
        yield dt.date.fromisoformat(value), group.drop("_session_date")


class QuoteArchiver:
    """Consumes ``quotes.<ticker>`` and writes Parquet."""

    def __init__(
        self,
        ticker: str,
        root: Path,
        *,
        bootstrap: str = DEFAULT_BOOTSTRAP,
        group_id: str = ARCHIVE_GROUP,
        batch_size: int = 5_000,
    ) -> None:
        from confluent_kafka import Consumer

        self.ticker = ticker.upper()
        self.root = Path(root)
        self.topic = quotes_topic(ticker).name
        self.batch_size = batch_size
        self._consumer = Consumer(consumer_config(bootstrap, group_id=group_id))

    def run(self, *, max_messages: int | None = None, idle_timeout: float = 5.0) -> ArchiveStats:
        """Consume until idle or ``max_messages`` reached, writing as it goes."""
        self._consumer.subscribe([self.topic])
        buffer: list[dict[str, Any]] = []
        messages = files = batches = malformed = 0

        try:
            while max_messages is None or messages < max_messages:
                msg = self._consumer.poll(timeout=idle_timeout)
                if msg is None:
                    break
                if msg.error():
                    logger.error("consume error: %s", msg.error())
                    continue

                try:
                    payload = json.loads(msg.value())
                except (ValueError, TypeError):
                    payload = None
                if not is_archivable(payload):
                    malformed += 1
                    logger.warning(
                        "skipping unarchivable message at %s[%d]@%d",
                        msg.topic(),
                        msg.partition(),
                        msg.offset(),
                    )
                    continue

                buffer.append(payload)
                messages += 1

                if len(buffer) >= self.batch_size:
                    files += self._flush(buffer)
                    batches += 1
                    buffer.clear()

            if buffer:
                files += self._flush(buffer)
                batches += 1
        finally:
            self._consumer.close()

        return ArchiveStats(
            messages=messages, files_written=files, batches=batches, malformed=malformed
        )

    def _flush(self, buffer: list[dict[str, Any]]) -> int:
        """Write, *then* commit. Never the other way round."""
        written = write_batch(buffer, self.root, self.ticker)
        self._consumer.commit(asynchronous=False)
        logger.info("archived %d rows to %d file(s)", len(buffer), len(written))
        return len(written)


def read_archive(root: Path, ticker: str, session_date: dt.date | None = None) -> pl.DataFrame:
    """Read back a recording, deduplicated.

    Deduplication is applied here rather than at write time because it is the
    reader that knows what a duplicate means, and because at-least-once
    delivery makes duplicates expected rather than exceptional.
    """
    pattern = (
        partition_dir(root, ticker, session_date)
        if session_date
        else (Path(root) / "quotes" / f"ticker={ticker.upper()}")
    )
    files = sorted(Path(pattern).rglob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed").unique(
        subset=list(DEDUPE_KEYS), keep="first", maintain_order=True
    )


def flatten_rejections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One ``chain.<ticker>`` snapshot -> one row per rejected quote.

    Each row carries the snapshot's own accepted and rejected counts. That is
    deliberate denormalisation: a rejection *rate* needs a denominator, and
    carrying it on the row means the rate is readable without joining back to
    the quotes dataset — which also makes the two independently checkable
    against each other.

    Snapshots that rejected nothing produce no rows, so a session-level rate
    computed from this dataset alone would be missing their denominators. Use
    the quotes archive for the accepted total; these counts are for
    cross-checking and for per-snapshot rates.
    """
    rejected = payload.get("rejected") or []
    if not rejected:
        return []

    context = {
        "underlying": payload.get("underlying"),
        "expiry": payload.get("expiry"),
        "underlying_price": payload.get("underlying_price"),
        "snapshot_accepted": len(payload.get("quotes") or []),
        "snapshot_rejected": len(rejected),
    }
    rows = []
    for item in rejected:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row.setdefault("observed_at", payload.get("observed_at"))
        row.setdefault("feed_source", payload.get("feed_source"))
        rows.append(row | context)
    return rows


class RejectionArchiver:
    """Consumes ``chain.<ticker>`` and archives the quotes the gate refused.

    Rejections are published on the chain topic rather than the quotes topic —
    by construction, since a rejected quote never becomes a quote — so this
    consumer exists to move them into permanent storage. Without it they expire
    with Kafka's seven-day retention, and section 9 calls how often a strategy
    *could not have traded* a first-class result. A first-class result with a
    seven-day memory is not a record.

    Its own consumer group, so it can be run, stopped and replayed entirely
    independently of the quote archiver. Same write-then-commit discipline: a
    duplicated rejection deduplicates on ``(symbol, observed_at)``, whereas a
    lost one cannot be re-observed.
    """

    def __init__(
        self,
        ticker: str,
        root: Path,
        *,
        bootstrap: str = DEFAULT_BOOTSTRAP,
        group_id: str = REJECTION_GROUP,
        batch_size: int = 5_000,
    ) -> None:
        from confluent_kafka import Consumer

        self.ticker = ticker.upper()
        self.root = Path(root)
        self.topic = chain_topic(ticker).name
        self.batch_size = batch_size
        self._consumer = Consumer(consumer_config(bootstrap, group_id=group_id))

    def run(self, *, max_messages: int | None = None, idle_timeout: float = 5.0) -> ArchiveStats:
        self._consumer.subscribe([self.topic])
        buffer: list[dict[str, Any]] = []
        messages = files = batches = malformed = 0

        try:
            while max_messages is None or messages < max_messages:
                msg = self._consumer.poll(timeout=idle_timeout)
                if msg is None:
                    break
                if msg.error():
                    logger.error("consume error: %s", msg.error())
                    continue

                try:
                    payload = json.loads(msg.value())
                except (ValueError, TypeError):
                    payload = None
                if not isinstance(payload, dict) or payload.get("observed_at") is None:
                    malformed += 1
                    logger.warning(
                        "skipping unarchivable snapshot at %s[%d]@%d",
                        msg.topic(),
                        msg.partition(),
                        msg.offset(),
                    )
                    continue

                # A snapshot that rejected nothing is still consumed and still
                # counted: it advances the offset, it just contributes no rows.
                buffer.extend(flatten_rejections(payload))
                messages += 1

                if len(buffer) >= self.batch_size:
                    files += self._flush(buffer)
                    batches += 1
                    buffer.clear()

            if buffer:
                files += self._flush(buffer)
                batches += 1
        finally:
            self._consumer.close()

        return ArchiveStats(
            messages=messages, files_written=files, batches=batches, malformed=malformed
        )

    def _flush(self, buffer: list[dict[str, Any]]) -> int:
        """Write, *then* commit. Never the other way round."""
        written = write_batch(buffer, self.root, self.ticker, REJECTION_DATASET)
        self._consumer.commit(asynchronous=False)
        logger.info("archived %d rejections to %d file(s)", len(buffer), len(written))
        return len(written)


def read_rejections(root: Path, ticker: str, session_date: dt.date | None = None) -> pl.DataFrame:
    """Read back archived rejections, deduplicated."""
    pattern = (
        partition_dir(root, ticker, session_date, REJECTION_DATASET)
        if session_date
        else (Path(root) / REJECTION_DATASET / f"ticker={ticker.upper()}")
    )
    files = sorted(Path(pattern).rglob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed").unique(
        subset=list(REJECTION_DEDUPE_KEYS), keep="first", maintain_order=True
    )
