"""Kafka topology and client factories.

See docs/IMPLEMENTATION_PLAN.md section 11 for why the topology is shaped this
way. The short version, because it is the decision that matters:

Kafka orders messages **only within a partition**. Keying ``quotes.<ticker>``
by OCC contract symbol gives per-contract ordering and lets consumption
parallelise, which is right for the archiver and the surface jobs. But a
strategy decision needs a *coherent whole-chain snapshot at one instant*, and
reassembling that from interleaved per-contract updates means event-time
windowing with watermarks and a late-arrival policy.

So there are two topics, each keyed for what it must guarantee: fine-grained
events, and a materialised snapshot stream on a single partition where global
ordering is free at this volume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BOOTSTRAP = "localhost:9092"

#: Kafka is a buffer, not a database. Seven days is enough to replay a week of
#: sessions straight from the log; permanent history is the archiver's Parquet.
RETENTION_DAYS = 7
RETENTION_MS = RETENTION_DAYS * 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class TopicSpec:
    """A topic and the reason it is partitioned the way it is."""

    name: str
    partitions: int
    key: str
    rationale: str
    replication: int = 1
    config: dict[str, str] = field(default_factory=dict)

    def to_new_topic(self) -> Any:
        from confluent_kafka.admin import NewTopic

        return NewTopic(
            self.name,
            num_partitions=self.partitions,
            replication_factor=self.replication,
            config={"retention.ms": str(RETENTION_MS), **self.config},
        )


def quotes_topic(ticker: str) -> TopicSpec:
    return TopicSpec(
        name=f"quotes.{ticker.upper()}",
        partitions=6,
        key="OCC contract symbol",
        rationale=(
            "Per-contract ordering, parallel consumption. Six partitions is more "
            "than this volume needs; it exists so rebalancing and partition "
            "assignment are observable rather than theoretical."
        ),
    )


def chain_topic(ticker: str) -> TopicSpec:
    return TopicSpec(
        name=f"chain.{ticker.upper()}",
        partitions=1,
        key="ticker (single key)",
        rationale=(
            "One message is one whole-chain snapshot. A single partition buys "
            "strict global ordering, which is the guarantee a trading decision "
            "needs, and costs nothing at ~200 contracts per snapshot."
        ),
    )


def topics_for(ticker: str) -> tuple[TopicSpec, ...]:
    return (quotes_topic(ticker), chain_topic(ticker))


def producer_config(bootstrap: str = DEFAULT_BOOTSTRAP, **overrides: Any) -> dict[str, Any]:
    """Durable-by-default producer settings.

    ``acks=all`` with ``enable.idempotence`` costs nothing at this throughput
    and removes silent message loss on broker failover, which would show up
    much later as an unexplained gap in the recorded session.
    """
    config: dict[str, Any] = {
        "bootstrap.servers": bootstrap,
        "acks": "all",
        "enable.idempotence": True,
        "compression.type": "lz4",
        "linger.ms": 5,
    }
    config.update(overrides)
    return config


def consumer_config(
    bootstrap: str = DEFAULT_BOOTSTRAP,
    *,
    group_id: str,
    auto_offset_reset: str = "earliest",
    **overrides: Any,
) -> dict[str, Any]:
    """Consumer settings with automatic offset commits refused, not merely off.

    Auto-commit is the most common correctness bug in Kafka applications, and
    here it breaks the system silently: consume a snapshot, open a position,
    crash before the commit, get the snapshot redelivered, open the position
    **twice**.

    Offsets are instead written inside the same DynamoDB transaction as the
    state mutation they caused, and the consumer seeks to the stored offset on
    startup (section 12). Turning auto-commit back on would defeat that
    silently, so it raises here rather than being quietly overridden.
    """
    if overrides.get("enable.auto.commit"):
        raise ValueError(
            "enable.auto.commit must stay False: offsets are committed "
            "transactionally with position state (see section 12)"
        )
    config: dict[str, Any] = {
        "bootstrap.servers": bootstrap,
        "group.id": group_id,
        "enable.auto.commit": False,
        "auto.offset.reset": auto_offset_reset,
    }
    config.update(overrides)
    config["enable.auto.commit"] = False
    return config


def strategy_group_id(strategy_name: str) -> str:
    """Each strategy is its own consumer group — that *is* the tournament.

    All five see every snapshot independently, keep their own offsets, and one
    crashing or falling behind does not affect the others.
    """
    return f"strategy.{strategy_name}"
