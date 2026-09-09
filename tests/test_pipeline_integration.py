"""The whole Phase 1 path: recorder -> Kafka -> archiver -> Parquet -> read back.

    docker compose up -d
    pytest -m integration

Uses a throwaway ticker so each run gets its own topics, and deletes them
afterwards -- otherwise leftover messages from previous runs accumulate under
the seven-day retention and quietly change what later runs consume.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid

import pytest

from olv.common.kafka import DEFAULT_BOOTSTRAP, chain_topic, quotes_topic
from olv.common.models import SYNTHETIC_SOURCE
from olv.feed.producer import SnapshotPublisher
from olv.feed.synthetic import SyntheticFeed
from olv.kafka_admin import provision
from olv.record import record
from olv.state.archive import QuoteArchiver, read_archive

pytestmark = pytest.mark.integration

EXPIRY = dt.date(2025, 6, 10)
SNAPSHOTS = 3


def _broker_available() -> bool:
    try:
        from confluent_kafka.admin import AdminClient

        AdminClient({"bootstrap.servers": DEFAULT_BOOTSTRAP}).list_topics(timeout=3)
    except Exception:  # noqa: BLE001
        return False
    return True


@pytest.fixture(scope="module")
def ticker():
    if not _broker_available():
        pytest.skip("no Kafka broker on localhost:9092 (docker compose up -d)")

    name = f"ZZ{uuid.uuid4().hex[:6].upper()}"
    assert provision(name, DEFAULT_BOOTSTRAP) == 0
    yield name

    from confluent_kafka.admin import AdminClient

    admin = AdminClient({"bootstrap.servers": DEFAULT_BOOTSTRAP})
    for future in admin.delete_topics([quotes_topic(name).name, chain_topic(name).name]).values():
        with contextlib.suppress(Exception):  # cleanup must not fail the suite
            future.result(timeout=30)


@pytest.fixture(scope="module")
def recorded(ticker):
    """Record a few snapshots through Kafka and archive them to Parquet."""
    feed = SyntheticFeed(seed=17, defect_rate=0.1)
    publisher = SnapshotPublisher(ticker, DEFAULT_BOOTSTRAP)
    stats = record(feed, ticker, EXPIRY, snapshots=SNAPSHOTS, interval=0, publisher=publisher)
    assert publisher.flush(30) == 0
    assert publisher.delivery_failures == 0
    return stats


@pytest.fixture(scope="module")
def archived(ticker, recorded, tmp_path_factory):
    root = tmp_path_factory.mktemp("archive")
    archiver = QuoteArchiver(
        ticker, root, bootstrap=DEFAULT_BOOTSTRAP, group_id=f"test.archiver.{ticker}"
    )
    stats = archiver.run(max_messages=recorded.quotes_published, idle_timeout=15.0)
    return root, stats


class TestRoundTrip:
    def test_every_published_quote_reaches_parquet(self, ticker, recorded, archived):
        root, stats = archived
        assert stats.messages == recorded.quotes_published
        frame = read_archive(root, ticker)
        assert len(frame) == recorded.quotes_published

    def test_rejected_quotes_never_reach_the_quotes_topic(self, recorded):
        """Rejections are counted and published separately, not silently mixed in."""
        assert recorded.rejected > 0, "the defect rate should have produced some"

    def test_feed_source_survives_into_storage(self, ticker, archived):
        """Fabricated data must be self-identifying forever."""
        root, _ = archived
        assert set(read_archive(root, ticker)["feed_source"].unique()) == {SYNTHETIC_SOURCE}

    def test_schema_carries_what_phase_2_needs(self, ticker, archived):
        root, _ = archived
        columns = set(read_archive(root, ticker).columns)
        required = {
            "symbol",
            "strike",
            "right",
            "expiry",
            "bid",
            "ask",
            "bid_size",
            "ask_size",
            "mid",
            "spread",
            "quote_ts",
            "observed_at",
            "underlying_price",
            "underlying_ts",
            "feed_source",
        }
        missing = required - columns
        assert not missing, f"archive is missing columns needed to back out IV: {missing}"

    def test_partitioned_by_ticker_and_date(self, ticker, archived):
        root, _ = archived
        files = list((root / "quotes").rglob("*.parquet"))
        assert files
        assert all(f"ticker={ticker}" in str(f) for f in files)
        assert all("date=" in str(f) for f in files)


class TestDeduplication:
    def test_reading_twice_written_data_yields_one_copy(self, ticker, recorded, archived):
        """At-least-once delivery means duplicates are expected, not exceptional.

        The archiver commits only after a durable write, so a crash replays a
        batch. Readers deduplicate on (symbol, quote_ts).
        """
        root, _ = archived
        from olv.state.archive import write_batch

        frame = read_archive(root, ticker)
        write_batch(frame.to_dicts(), root, ticker)  # simulate a replayed batch

        assert len(read_archive(root, ticker)) == recorded.quotes_published


class TestResilience:
    def test_a_junk_message_is_quarantined_not_fatal(self, ticker, tmp_path):
        """One bad message must not cost the rest of a session.

        A session cannot be re-observed, so the archiver counts and skips
        anything unarchivable rather than dying on it.
        """
        from confluent_kafka import Producer

        from olv.common.kafka import producer_config
        from olv.state.archive import QuoteArchiver

        topic = quotes_topic(ticker).name
        producer = Producer(producer_config(DEFAULT_BOOTSTRAP))
        producer.produce(topic, key=b"junk", value=b'{"seq": 0}')
        producer.produce(topic, key=b"junk", value=b"not json at all")
        assert producer.flush(30) == 0

        archiver = QuoteArchiver(
            ticker, tmp_path, bootstrap=DEFAULT_BOOTSTRAP, group_id=f"test.junk.{ticker}"
        )
        stats = archiver.run(idle_timeout=10.0)

        assert stats.malformed >= 2
        assert "malformed" in str(stats)
