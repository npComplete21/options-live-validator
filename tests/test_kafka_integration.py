"""End-to-end checks against a real broker.

    docker compose up -d
    pytest -m integration

Skipped automatically when no broker is reachable, so the default suite stays
runnable without docker.

These exist because the topology in section 11 rests on a claim about Kafka's
behaviour -- that keying by contract symbol keeps a contract's quotes in one
partition, and that a single-partition topic therefore gives total ordering.
That claim is worth verifying against the real thing rather than trusting.
"""

from __future__ import annotations

import json
import uuid

import pytest

from olv.common.kafka import (
    DEFAULT_BOOTSTRAP,
    chain_topic,
    consumer_config,
    producer_config,
    quotes_topic,
)
from olv.kafka_admin import provision

pytestmark = pytest.mark.integration


def _broker_available(bootstrap: str = DEFAULT_BOOTSTRAP) -> bool:
    try:
        from confluent_kafka.admin import AdminClient

        AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=3)
    except Exception:  # noqa: BLE001 - any failure means "no broker"
        return False
    return True


@pytest.fixture(scope="module", autouse=True)
def require_broker():
    if not _broker_available():
        pytest.skip("no Kafka broker on localhost:9092 (docker compose up -d)")


@pytest.fixture(scope="module")
def provisioned():
    assert provision("QQQ", DEFAULT_BOOTSTRAP) == 0
    return quotes_topic("QQQ").name, chain_topic("QQQ").name


def test_provisioning_is_idempotent(provisioned):
    """Re-running against existing topics must be a no-op, not an error."""
    assert provision("QQQ", DEFAULT_BOOTSTRAP) == 0


def test_same_contract_always_lands_in_one_partition(provisioned):
    """The guarantee that makes per-contract ordering real."""
    from confluent_kafka import Producer

    quotes, _ = provisioned
    producer = Producer(producer_config())
    partitions = []

    def record(err, msg):
        assert err is None, err
        partitions.append(msg.partition())

    key = b"QQQ260908C00500000"
    for i in range(12):
        producer.produce(quotes, key=key, value=json.dumps({"seq": i}).encode(), on_delivery=record)
    producer.flush(15)

    assert len(partitions) == 12
    assert len(set(partitions)) == 1, "one contract must not be split across partitions"


def test_distinct_contracts_spread_across_partitions(provisioned):
    """Otherwise the six partitions would buy nothing."""
    from confluent_kafka import Producer

    quotes, _ = provisioned
    producer = Producer(producer_config())
    partitions = set()

    def record(err, msg):
        assert err is None, err
        partitions.add(msg.partition())

    for strike in range(400, 560, 5):
        key = f"QQQ260908C{strike:05d}000".encode()
        producer.produce(quotes, key=key, value=b"{}", on_delivery=record)
    producer.flush(15)

    assert len(partitions) > 1, "distinct contracts should not all hash to one partition"


def test_chain_snapshots_are_totally_ordered(provisioned):
    """A single partition means consumers see snapshots in production order."""
    from confluent_kafka import Consumer, Producer

    _, chain = provisioned
    marker = uuid.uuid4().hex
    producer = Producer(producer_config())
    for seq in range(25):
        producer.produce(chain, key=b"QQQ", value=json.dumps({"m": marker, "seq": seq}).encode())
    producer.flush(15)

    consumer = Consumer(consumer_config(group_id=f"test.{marker}", auto_offset_reset="earliest"))
    consumer.subscribe([chain])
    seen: list[int] = []
    try:
        while len(seen) < 25:
            msg = consumer.poll(timeout=10.0)
            if msg is None:
                break
            assert not msg.error(), msg.error()
            payload = json.loads(msg.value())
            if payload.get("m") == marker:
                seen.append(payload["seq"])
                assert msg.partition() == 0
    finally:
        consumer.close()

    assert seen == sorted(seen) == list(range(25))


def test_offsets_are_not_committed_behind_our_back(provisioned):
    """The correctness property the entire offset design rests on.

    Consume a batch, close without committing, then rejoin with the *same*
    group id. If Kafka had auto-committed, the second consumer would resume
    past the batch and see nothing. It must instead re-read every message,
    because the only thing allowed to advance an offset is the transactional
    write in section 12.

    This is the failure that would otherwise appear as a position silently
    opening twice after a crash.
    """
    from confluent_kafka import Consumer, Producer

    _, chain = provisioned
    marker = uuid.uuid4().hex
    group = f"test.nocommit.{marker}"
    expected = 10

    producer = Producer(producer_config())
    for seq in range(expected):
        producer.produce(chain, key=b"QQQ", value=json.dumps({"m": marker, "seq": seq}).encode())
    producer.flush(15)

    def drain() -> list[int]:
        consumer = Consumer(consumer_config(group_id=group, auto_offset_reset="earliest"))
        consumer.subscribe([chain])
        seen: list[int] = []
        try:
            while len(seen) < expected:
                msg = consumer.poll(timeout=10.0)
                if msg is None:
                    break
                assert not msg.error(), msg.error()
                payload = json.loads(msg.value())
                if payload.get("m") == marker:
                    seen.append(payload["seq"])
        finally:
            consumer.close()
        return seen

    first = drain()
    assert first == list(range(expected))

    second = drain()
    assert second == first, "offsets advanced without an explicit commit"
