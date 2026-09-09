"""Quote hygiene. The gate's job is to reject what is unusable while admitting
the wings, which look pathological but are real. Both halves are tested.
"""

from __future__ import annotations

import datetime as dt

import pytest

from olv.common.models import RejectReason, Right, occ_symbol
from olv.feed import gate
from olv.feed.gate import GateConfig, RawQuote

EXPIRY = dt.date(2026, 9, 9)
NOW = dt.datetime(2026, 9, 9, 15, 0, tzinfo=dt.UTC)
SYMBOL = occ_symbol("QQQ", EXPIRY, Right.CALL, 500.0)


def raw(**kwargs) -> RawQuote:
    base = dict(symbol=SYMBOL, bid=1.00, ask=1.10, bid_size=10, ask_size=10, quote_ts=NOW)
    base.update(kwargs)
    return RawQuote(**base)


def check(quote: RawQuote, config: GateConfig | None = None, underlying_ts=NOW):
    return gate.check(
        quote, observed_at=NOW, underlying_ts=underlying_ts, config=config or GateConfig()
    )


class TestAccepts:
    def test_a_normal_quote(self):
        assert check(raw()) is None

    def test_a_penny_wing_with_a_huge_relative_spread(self):
        """$0.05 / $0.09 is a 57% relative spread and a perfectly real quote.

        Rejecting these would destroy exactly the data the skew work needs.
        """
        assert check(raw(bid=0.05, ask=0.09)) is None

    def test_a_one_tick_market(self):
        assert check(raw(bid=0.01, ask=0.02)) is None


class TestRejects:
    def test_crossed(self):
        assert check(raw(bid=1.20, ask=1.10)) is RejectReason.CROSSED

    def test_locked(self):
        assert check(raw(bid=1.10, ask=1.10)) is RejectReason.LOCKED

    def test_zero_bid(self):
        assert check(raw(bid=0.0, ask=0.05)) is RejectReason.ZERO_BID

    def test_missing_side(self):
        assert check(raw(ask=None)) is RejectReason.MISSING

    def test_missing_timestamp(self):
        assert check(raw(quote_ts=None)) is RejectReason.MISSING

    def test_non_finite(self):
        assert check(raw(ask=float("nan"))) is RejectReason.NON_FINITE

    def test_negative_price(self):
        assert check(raw(bid=-0.01)) is RejectReason.NEGATIVE

    def test_stale(self):
        assert check(raw(quote_ts=NOW - dt.timedelta(minutes=2))) is RejectReason.STALE

    def test_underlying_skew(self):
        """Implied vol from a stale spot and a fresh option is garbage."""
        assert (
            check(raw(), underlying_ts=NOW - dt.timedelta(minutes=1))
            is RejectReason.UNDERLYING_SKEW
        )

    def test_absurd_width(self):
        assert check(raw(bid=1.00, ask=20.00)) is RejectReason.ABSURD_WIDTH


class TestReasonPrecedence:
    def test_crossed_is_reported_over_stale(self):
        """The market condition is more informative than our connection state."""
        quote = raw(bid=1.20, ask=1.10, quote_ts=NOW - dt.timedelta(minutes=5))
        assert check(quote) is RejectReason.CROSSED


class TestConfigurability:
    def test_zero_bid_can_be_admitted(self):
        assert check(raw(bid=0.0, ask=0.05), GateConfig(reject_zero_bid=False)) is None

    def test_locked_can_be_admitted(self):
        assert check(raw(bid=1.1, ask=1.1), GateConfig(reject_locked=False)) is None


class TestApply:
    def test_splits_without_losing_anything(self):
        quotes = [raw(), raw(bid=1.20, ask=1.10), raw(bid=0.0, ask=0.05)]
        accepted, rejected = gate.apply(
            quotes,
            underlying="QQQ",
            expiry=EXPIRY,
            observed_at=NOW,
            underlying_ts=NOW,
            feed_source="test",
        )
        assert len(accepted) + len(rejected) == len(quotes)
        assert {r.reason for r in rejected} == {RejectReason.CROSSED, RejectReason.ZERO_BID}

    def test_stamps_the_feed_source_on_both_sides(self):
        """So fabricated data can never be mistaken for observed data."""
        accepted, rejected = gate.apply(
            [raw(), raw(bid=1.20, ask=1.10)],
            underlying="QQQ",
            expiry=EXPIRY,
            observed_at=NOW,
            underlying_ts=NOW,
            feed_source="synthetic",
        )
        assert all(q.feed_source == "synthetic" for q in accepted)
        assert all(r.feed_source == "synthetic" for r in rejected)

    def test_contract_outside_the_watch_set_is_rejected_not_recorded(self):
        other = occ_symbol("QQQ", dt.date(2026, 9, 10), Right.CALL, 500.0)
        accepted, rejected = gate.apply(
            [raw(symbol=other)],
            underlying="QQQ",
            expiry=EXPIRY,
            observed_at=NOW,
            underlying_ts=NOW,
            feed_source="test",
        )
        assert not accepted
        assert rejected[0].reason is RejectReason.WRONG_EXPIRY

    def test_parsed_fields_come_from_the_symbol(self):
        accepted, _ = gate.apply(
            [raw(symbol=occ_symbol("QQQ", EXPIRY, Right.PUT, 487.5))],
            underlying="QQQ",
            expiry=EXPIRY,
            observed_at=NOW,
            underlying_ts=NOW,
            feed_source="test",
        )
        assert accepted[0].strike == 487.5
        assert accepted[0].right is Right.PUT

    def test_mid_and_spread(self):
        accepted, _ = gate.apply(
            [raw(bid=1.00, ask=1.10)],
            underlying="QQQ",
            expiry=EXPIRY,
            observed_at=NOW,
            underlying_ts=NOW,
            feed_source="test",
        )
        assert accepted[0].mid == pytest.approx(1.05)
        assert accepted[0].spread == pytest.approx(0.10)
