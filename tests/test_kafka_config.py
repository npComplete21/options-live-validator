"""Topology and client configuration. See docs/IMPLEMENTATION_PLAN.md section 11."""

from __future__ import annotations

import pytest

from olv.common.kafka import (
    RETENTION_MS,
    chain_topic,
    consumer_config,
    producer_config,
    quotes_topic,
    strategy_group_id,
    topics_for,
)


class TestTopology:
    def test_quotes_is_partitioned_for_parallelism(self):
        spec = quotes_topic("qqq")
        assert spec.name == "quotes.QQQ"
        assert spec.partitions == 6
        assert "contract" in spec.key.lower()

    def test_chain_is_single_partition_for_global_ordering(self):
        """A trading decision needs a coherent whole-chain snapshot.

        Kafka orders only within a partition, so the snapshot stream gets
        exactly one.
        """
        spec = chain_topic("qqq")
        assert spec.name == "chain.QQQ"
        assert spec.partitions == 1

    def test_both_topics_are_provisioned_together(self):
        assert {s.name for s in topics_for("QQQ")} == {"quotes.QQQ", "chain.QQQ"}

    def test_every_topic_carries_its_rationale(self):
        """The partition count is a decision; the reason travels with it."""
        for spec in topics_for("QQQ"):
            assert spec.rationale.strip()

    def test_retention_is_a_week(self):
        assert RETENTION_MS == 7 * 24 * 3600 * 1000


class TestProducerConfig:
    def test_durability_defaults(self):
        config = producer_config()
        assert config["acks"] == "all"
        assert config["enable.idempotence"] is True

    def test_bootstrap_and_overrides_apply(self):
        config = producer_config("broker:9092", **{"linger.ms": 50})
        assert config["bootstrap.servers"] == "broker:9092"
        assert config["linger.ms"] == 50


class TestConsumerConfig:
    def test_auto_commit_is_off_by_default(self):
        assert consumer_config(group_id="g")["enable.auto.commit"] is False

    def test_enabling_auto_commit_raises(self):
        """Auto-commit would silently defeat transactional offset storage.

        Consume a snapshot, open a position, crash before the commit, get the
        snapshot redelivered, open the position twice.
        """
        with pytest.raises(ValueError, match="auto.commit"):
            consumer_config(group_id="g", **{"enable.auto.commit": True})

    def test_falsey_override_is_tolerated_and_still_off(self):
        assert (
            consumer_config(group_id="g", **{"enable.auto.commit": False})["enable.auto.commit"]
            is False
        )

    def test_each_strategy_gets_its_own_group(self):
        """Separate groups are what let all five strategies see every snapshot."""
        groups = {strategy_group_id(n) for n in ("short_strangle", "iron_condor")}
        assert len(groups) == 2
