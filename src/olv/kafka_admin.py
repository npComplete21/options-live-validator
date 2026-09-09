"""Topic provisioning CLI.

Auto-creation is disabled on the broker (docker-compose.yml) on purpose:
partition counts and key choices are a design decision (section 11), not
something that should fall out of a default the first time someone produces to
a misspelled topic name.

    python -m olv.kafka_admin describe
    python -m olv.kafka_admin provision --ticker QQQ
"""

from __future__ import annotations

import argparse
import logging
import sys

from olv.common.kafka import DEFAULT_BOOTSTRAP, RETENTION_DAYS, TopicSpec, topics_for

logger = logging.getLogger("olv.kafka_admin")


def describe(ticker: str) -> str:
    lines = [f"Kafka topology for {ticker.upper()} (retention {RETENTION_DAYS}d):", ""]
    for spec in topics_for(ticker):
        lines += [
            f"  {spec.name}",
            f"    partitions : {spec.partitions}",
            f"    key        : {spec.key}",
            f"    why        : {spec.rationale}",
            "",
        ]
    return "\n".join(lines)


def provision(ticker: str, bootstrap: str) -> int:
    from confluent_kafka.admin import AdminClient

    admin = AdminClient({"bootstrap.servers": bootstrap})
    specs: tuple[TopicSpec, ...] = topics_for(ticker)

    existing = set(admin.list_topics(timeout=10).topics)
    wanted = [s for s in specs if s.name not in existing]

    for spec in specs:
        if spec.name in existing:
            logger.info("%s already exists, leaving alone", spec.name)

    if not wanted:
        return 0

    failures = 0
    for name, future in admin.create_topics([s.to_new_topic() for s in wanted]).items():
        try:
            future.result()
            logger.info("created %s", name)
        except Exception as exc:  # noqa: BLE001 - report every failure, not just the first
            logger.error("failed to create %s: %s", name, exc)
            failures += 1
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["describe", "provision"])
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--bootstrap", default=DEFAULT_BOOTSTRAP)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "describe":
        print(describe(args.ticker))
        return 0
    return provision(args.ticker, args.bootstrap)


if __name__ == "__main__":
    sys.exit(main())
