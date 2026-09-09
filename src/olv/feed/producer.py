"""Publishing recorded quotes. See docs/IMPLEMENTATION_PLAN.md section 11.

Each snapshot is published twice, keyed differently, because the two consumers
need different guarantees:

* ``quotes.<ticker>`` — one message per contract, keyed by OCC symbol, so a
  contract's history stays ordered within its partition and consumption can
  parallelise across six partitions.
* ``chain.<ticker>`` — one message per snapshot, keyed by ticker, on a single
  partition, so strategy consumers see whole chains in production order rather
  than reassembling them from interleaved updates.

JSON on the wire, deliberately: it is debuggable with ``kafka-console-consumer``
while the schema is still moving. Avro or Protobuf with a registry is the right
answer once it settles, and is worth doing as its own exercise.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from olv.common.kafka import DEFAULT_BOOTSTRAP, chain_topic, producer_config, quotes_topic
from olv.common.models import ChainSnapshot

logger = logging.getLogger(__name__)


def _encode(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    raise TypeError(f"cannot serialise {type(value).__name__}")


def dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, default=_encode, separators=(",", ":")).encode()


def snapshot_payload(snapshot: ChainSnapshot) -> dict[str, Any]:
    return {
        "underlying": snapshot.underlying,
        "expiry": snapshot.expiry,
        "underlying_price": snapshot.underlying_price,
        "underlying_ts": snapshot.underlying_ts,
        "observed_at": snapshot.observed_at,
        "feed_source": snapshot.feed_source,
        "quotes": [q.to_row() for q in snapshot.quotes],
        "rejected": [r.to_row() for r in snapshot.rejected],
    }


class SnapshotPublisher:
    """Publishes snapshots to both topics.

    Not thread-safe, and not meant to be: one recorder process owns one
    publisher.
    """

    def __init__(self, underlying: str, bootstrap: str = DEFAULT_BOOTSTRAP) -> None:
        from confluent_kafka import Producer

        self.underlying = underlying.upper()
        self.quotes_topic = quotes_topic(underlying).name
        self.chain_topic = chain_topic(underlying).name
        self._producer = Producer(producer_config(bootstrap))
        self._delivery_failures = 0

    def _on_delivery(self, err: Any, msg: Any) -> None:
        if err is not None:
            self._delivery_failures += 1
            logger.error("delivery failed for %s: %s", msg.topic() if msg else "?", err)

    @property
    def delivery_failures(self) -> int:
        return self._delivery_failures

    def publish(self, snapshot: ChainSnapshot) -> int:
        """Publish one snapshot. Returns the number of messages produced."""
        produced = 0
        for quote in snapshot.quotes:
            row = quote.to_row()
            row["underlying_price"] = snapshot.underlying_price
            row["underlying_ts"] = snapshot.underlying_ts
            row["observed_at"] = snapshot.observed_at
            self._producer.produce(
                self.quotes_topic,
                key=quote.symbol.encode(),
                value=dumps(row),
                on_delivery=self._on_delivery,
            )
            produced += 1

        self._producer.produce(
            self.chain_topic,
            key=self.underlying.encode(),
            value=dumps(snapshot_payload(snapshot)),
            on_delivery=self._on_delivery,
        )
        produced += 1
        self._producer.poll(0)
        return produced

    def flush(self, timeout: float = 30.0) -> int:
        """Block until every message is acknowledged. Returns messages still queued."""
        return self._producer.flush(timeout)

    def __enter__(self) -> SnapshotPublisher:
        return self

    def __exit__(self, *exc: object) -> None:
        remaining = self.flush()
        if remaining:
            logger.error("%d messages unflushed at shutdown", remaining)
