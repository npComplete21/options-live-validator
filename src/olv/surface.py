"""Offline driver: recorded archive -> implied vol, greeks, and report C.

    python -m olv.surface --archive ./archive --ticker QQQ
    python -m olv.surface --archive ./archive --date 2026-09-09 --out report.md

Phase 2 runs this over Parquet. Phase 4 wires the *same* ``olv.analytics``
functions into a Kafka consumer, per plan section 11's requirement that replay
from a recording and replay from an offset drive one engine. Nothing in this
file does analysis; it reads, calls, and prints.

No mode guard here and no credentials read: this touches recorded data only and
places no orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from olv.analytics.surface import enrich, stats_for
from olv.reporting import surface as report_c
from olv.state.archive import read_archive, read_rejections

logger = logging.getLogger("olv.surface")


def run(root: Path, ticker: str, session_date: dt.date | None = None) -> report_c.SurfaceReport:
    raw = read_archive(root, ticker, session_date)
    if raw.is_empty():
        raise SystemExit(f"no recorded quotes under {root} for {ticker}")
    enriched = enrich(raw)
    logger.info("%s", stats_for(enriched))

    # Absent rather than empty when the dataset does not exist, so the report
    # can distinguish "rejected nothing" from "nobody archived the rejections".
    rejections = read_rejections(root, ticker, session_date)
    if rejections.is_empty():
        logger.warning("no rejections archived under %s - rejection rates unavailable", root)
    else:
        logger.info("%d archived rejections", rejections.height)
    return report_c.build(enriched, raw, rejections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="archive root")
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--date", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--out", type=Path, default=None, help="write the report here")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    rendered = run(args.archive, args.ticker, args.date).render()

    if args.out:
        args.out.write_text(rendered)
        logger.info("wrote %s", args.out)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
