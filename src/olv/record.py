"""Session recorder — Phase 1. See docs/IMPLEMENTATION_PLAN.md section 15.

    python -m olv.record --feed synthetic --snapshots 20 --interval 0
    python -m olv.record --feed synthetic --duration 300 --interval 5

Recording is independent of, and outlives, every strategy decision: it starts
at the open and stops at the close regardless of whether anything traded. Per
section 0 the recording *is* the deliverable, because backtest-lab cannot
synthesise a 0DTE chain and every unrecorded session is permanently absent
from the dataset.

No orders are placed here and no broker credentials are read. The mode guard
still runs at startup, so a recording is stamped with the mode it was taken
under and a misconfigured environment fails immediately rather than at Phase 5.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from dataclasses import dataclass

from olv.common.kafka import DEFAULT_BOOTSTRAP
from olv.common.mode import ExecutionMode, mode_from_env
from olv.common.models import ChainSnapshot
from olv.feed import gate
from olv.feed.client import QuoteFeed
from olv.feed.producer import SnapshotPublisher
from olv.feed.universe import DEFAULT_BAND, build_watch_set, zero_dte_expiry

logger = logging.getLogger("olv.record")


@dataclass(frozen=True, slots=True)
class RecordingStats:
    snapshots: int
    quotes_published: int
    rejected: int
    rejection_counts: dict[str, int]

    @property
    def rejection_rate(self) -> float:
        total = self.quotes_published + self.rejected
        return self.rejected / total if total else 0.0

    def __str__(self) -> str:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(self.rejection_counts.items()))
        return (
            f"{self.snapshots} snapshots, {self.quotes_published} quotes, "
            f"{self.rejected} rejected ({self.rejection_rate:.1%})"
            + (f" [{detail}]" if detail else "")
        )


def take_snapshot(
    feed: QuoteFeed,
    underlying: str,
    expiry: dt.date,
    *,
    band: float = DEFAULT_BAND,
    config: gate.GateConfig | None = None,
    now: dt.datetime | None = None,
) -> ChainSnapshot:
    """One observation: build the watch set from live spot, poll, validate.

    The watch set is rebuilt every snapshot rather than once per session,
    because spot moves and a band pinned at the open drifts off centre by the
    afternoon — which would quietly stop recording the strikes that matter.
    """
    spot, underlying_ts = feed.underlying_quote(underlying)
    strikes = feed.listed_strikes(underlying, expiry)
    watch_set = build_watch_set(underlying, expiry, spot, strikes, band=band)

    observed_at = now or dt.datetime.now(dt.UTC)
    raws = feed.option_quotes(watch_set)
    accepted, rejected = gate.apply(
        raws,
        underlying=underlying.upper(),
        expiry=expiry,
        observed_at=observed_at,
        underlying_ts=underlying_ts,
        feed_source=feed.source,
        config=config,
    )
    return ChainSnapshot(
        underlying=underlying.upper(),
        expiry=expiry,
        underlying_price=spot,
        underlying_ts=underlying_ts,
        observed_at=observed_at,
        quotes=accepted,
        rejected=rejected,
        feed_source=feed.source,
    )


def record(
    feed: QuoteFeed,
    underlying: str,
    expiry: dt.date,
    *,
    snapshots: int,
    interval: float,
    publisher: SnapshotPublisher | None = None,
    band: float = DEFAULT_BAND,
) -> RecordingStats:
    total_quotes = total_rejected = 0
    counts: dict[str, int] = {}

    for i in range(snapshots):
        snapshot = take_snapshot(feed, underlying, expiry, band=band)
        if publisher is not None:
            publisher.publish(snapshot)

        total_quotes += len(snapshot.quotes)
        total_rejected += len(snapshot.rejected)
        for reason, count in snapshot.rejection_counts().items():
            counts[reason] = counts.get(reason, 0) + count

        if hasattr(feed, "advance"):
            feed.advance(seconds=interval or 1.0)
        if interval and i < snapshots - 1:
            time.sleep(interval)

    return RecordingStats(
        snapshots=snapshots,
        quotes_published=total_quotes,
        rejected=total_rejected,
        rejection_counts=counts,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a 0DTE option chain.")
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--feed", choices=["synthetic"], default="synthetic")
    parser.add_argument("--snapshots", type=int, default=10)
    parser.add_argument("--duration", type=float, help="seconds; overrides --snapshots")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between snapshots")
    parser.add_argument("--band", type=float, default=DEFAULT_BAND)
    parser.add_argument("--defect-rate", type=float, default=0.05, help="synthetic feed only")
    parser.add_argument("--bootstrap", default=DEFAULT_BOOTSTRAP)
    parser.add_argument("--no-publish", action="store_true", help="skip Kafka; just report")
    parser.add_argument("--date", help="session date (YYYY-MM-DD); defaults to today")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    mode = mode_from_env()
    if mode is not ExecutionMode.PAPER:
        logger.warning("recording under mode=%s; the recorder places no orders either way", mode)

    session_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    try:
        expiry = zero_dte_expiry(session_date)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    from olv.feed.synthetic import SyntheticFeed

    feed = SyntheticFeed(defect_rate=args.defect_rate)
    snapshots = (
        int(args.duration / args.interval) if args.duration and args.interval else args.snapshots
    )

    publisher = None if args.no_publish else SnapshotPublisher(args.ticker, args.bootstrap)
    try:
        stats = record(
            feed,
            args.ticker,
            expiry,
            snapshots=snapshots,
            interval=args.interval,
            publisher=publisher,
            band=args.band,
        )
    finally:
        if publisher is not None:
            publisher.__exit__()

    logger.info("%s", stats)
    if publisher is not None and publisher.delivery_failures:
        logger.error("%d delivery failures", publisher.delivery_failures)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
