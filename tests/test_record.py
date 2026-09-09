"""Snapshot assembly, with no broker involved."""

from __future__ import annotations

import datetime as dt

from olv.common.models import SYNTHETIC_SOURCE, parse_occ_symbol
from olv.feed.gate import GateConfig
from olv.feed.synthetic import SyntheticFeed
from olv.feed.universe import build_watch_set
from olv.record import record, take_snapshot

EXPIRY = dt.date(2025, 6, 10)


class TestSyntheticFeed:
    def test_is_deterministic_given_a_seed(self):
        """Replays and tests have to be reproducible."""
        a = SyntheticFeed(seed=42, defect_rate=0.2)
        b = SyntheticFeed(seed=42, defect_rate=0.2)
        ws = build_watch_set("QQQ", EXPIRY, a.spot, a.listed_strikes("QQQ", EXPIRY))
        assert [q.bid for q in a.option_quotes(ws)] == [q.bid for q in b.option_quotes(ws)]

    def test_different_seeds_diverge(self):
        a, b = SyntheticFeed(seed=1, defect_rate=0.5), SyntheticFeed(seed=2, defect_rate=0.5)
        ws = build_watch_set("QQQ", EXPIRY, a.spot, a.listed_strikes("QQQ", EXPIRY))
        assert [q.bid for q in a.option_quotes(ws)] != [q.bid for q in b.option_quotes(ws)]

    def test_advance_moves_spot(self):
        feed = SyntheticFeed(seed=3)
        before = feed.spot
        feed.advance()
        assert feed.spot != before


class TestTakeSnapshot:
    def test_snapshot_is_internally_coherent(self):
        snapshot = take_snapshot(SyntheticFeed(seed=0), "QQQ", EXPIRY)
        assert snapshot.underlying == "QQQ"
        assert snapshot.expiry == EXPIRY
        assert snapshot.underlying_price > 0
        assert all(q.expiry == EXPIRY for q in snapshot.quotes)
        assert all(parse_occ_symbol(q.symbol)[0] == "QQQ" for q in snapshot.quotes)

    def test_everything_is_stamped_synthetic(self):
        snapshot = take_snapshot(SyntheticFeed(seed=0, defect_rate=0.3), "QQQ", EXPIRY)
        assert snapshot.feed_source == SYNTHETIC_SOURCE
        assert all(q.feed_source == SYNTHETIC_SOURCE for q in snapshot.quotes)
        assert all(r.feed_source == SYNTHETIC_SOURCE for r in snapshot.rejected)

    def test_clean_feed_produces_no_rejections(self):
        """The gate must not be rejecting legitimate wings."""
        snapshot = take_snapshot(SyntheticFeed(seed=0, defect_rate=0.0), "QQQ", EXPIRY)
        assert snapshot.rejected == ()
        assert len(snapshot.quotes) > 50, "a 5% band on a $1 grid should hold ~100 contracts"

    def test_defects_are_caught_and_counted(self):
        snapshot = take_snapshot(SyntheticFeed(seed=5, defect_rate=0.5), "QQQ", EXPIRY)
        assert snapshot.rejected
        assert sum(snapshot.rejection_counts().values()) == len(snapshot.rejected)

    def test_band_controls_the_contract_count(self):
        narrow = take_snapshot(SyntheticFeed(seed=0), "QQQ", EXPIRY, band=0.01)
        wide = take_snapshot(SyntheticFeed(seed=0), "QQQ", EXPIRY, band=0.05)
        assert len(wide.quotes) > len(narrow.quotes)

    def test_zero_bid_admitted_when_configured(self):
        config = GateConfig(reject_zero_bid=False, reject_locked=False)
        snapshot = take_snapshot(SyntheticFeed(seed=0), "QQQ", EXPIRY, config=config)
        assert snapshot.quotes


class TestRecord:
    def test_accounting_adds_up(self):
        feed = SyntheticFeed(seed=11, defect_rate=0.2)
        stats = record(feed, "QQQ", EXPIRY, snapshots=4, interval=0)
        assert stats.snapshots == 4
        assert stats.quotes_published + stats.rejected > 0
        assert sum(stats.rejection_counts.values()) == stats.rejected
        assert 0.0 < stats.rejection_rate < 1.0

    def test_watch_set_follows_spot(self):
        """A band pinned at the open drifts off centre by the afternoon."""
        feed = SyntheticFeed(seed=0, now=dt.datetime(2025, 6, 10, 14, tzinfo=dt.UTC))
        first = take_snapshot(feed, "QQQ", EXPIRY)
        for _ in range(50):
            feed.advance(vol=0.01)
        later = take_snapshot(feed, "QQQ", EXPIRY, now=dt.datetime.now(dt.UTC))
        assert first.underlying_price != later.underlying_price
