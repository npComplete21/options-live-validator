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

from olv.common.kafka import DEFAULT_BOOTSTRAP, consumer_config, quotes_topic

logger = logging.getLogger(__name__)

ARCHIVE_GROUP = "archiver.quotes"

#: Deduplication key for readers, given at-least-once delivery.
DEDUPE_KEYS = ("symbol", "quote_ts")

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


def partition_dir(root: Path, ticker: str, session_date: dt.date) -> Path:
    """Hive-style partitioning, so DuckDB and Polars can prune on both keys."""
    return root / "quotes" / f"ticker={ticker.upper()}" / f"date={session_date.isoformat()}"


def write_batch(rows: list[dict[str, Any]], root: Path, ticker: str) -> list[Path]:
    """Write rows to Parquet, one file per session date in the batch."""
    if not rows:
        return []

    frame = pl.DataFrame(rows, infer_schema_length=None)
    written: list[Path] = []
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")

    for session_date, group in _by_session_date(frame):
        target = partition_dir(root, ticker, session_date)
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
