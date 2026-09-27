"""The five tournament strategies. See docs/IMPLEMENTATION_PLAN.md section 5.

These assert properties of the *set*, not of any one member. The tournament's
value comes from spanning Natenberg's gamma/vega quadrants deliberately — four
short-premium strategies compared only against each other could all be losing
while the "winner" still looked like one.

Every rule and selector used here is defined in ``obl.strategy``, pinned by
tag. That all five express in the shared vocabulary with no engine changes is
section 5's acceptance test that the abstraction is real.
"""

from __future__ import annotations

import datetime as dt

import pytest
from obl.strategy.rules import (
    SAFE_ACTIONS,
    AbsPortfolioDelta,
    TimeOfDayExit,
    TimeOfDayTrigger,
    UnderlyingTouch,
)

from olv.strategy.library import UnknownStrategyError, all_names, get, tournament

EXPECTED = {
    "short_strangle_0dte",
    "iron_condor_0dte",
    "iron_butterfly_0dte",
    "short_strangle_0dte_hedged",
    "long_straddle_0dte",
}


@pytest.fixture(scope="module")
def bound():
    return {name: spec.bind() for name, spec in tournament().items()}


class TestTheFieldIsComplete:
    def test_all_five_are_present(self):
        assert set(all_names()) == EXPECTED

    def test_every_one_binds_through_the_shared_dsl(self, bound):
        """Section 5's acceptance test: five strategies, zero engine changes."""
        assert len(bound) == 5

    def test_an_unknown_name_is_fatal(self):
        """A tournament quietly running four of its five members is not the
        experiment that was claimed."""
        with pytest.raises(UnknownStrategyError, match="unknown strategy"):
            get("wishful_thinking")


class TestEveryMemberIsZeroDte:
    def test_all_legs_expire_today(self, bound):
        for name, strategy in bound.items():
            for leg in strategy.legs:
                target = getattr(leg.expiry, "target", 0)
                assert target == 0, f"{name}:{leg.id} is not 0DTE"

    def test_all_enter_at_the_same_instant(self, bound):
        """Comparability: strategies entered at different times of day would
        differ by the intraday vol curve as much as by their own merits."""
        for name, strategy in bound.items():
            trigger = strategy.entry.trigger
            assert isinstance(trigger, TimeOfDayTrigger), name
            assert trigger.at == dt.time(9, 45), name
            assert str(trigger.tz) == "America/New_York", name

    def test_all_flatten_before_the_close(self, bound):
        """Section 8: positions never survive the session, unconditionally. At
        15:57 ATM gamma is eleven times its morning value."""
        for name, strategy in bound.items():
            flats = [
                r
                for r in strategy.management
                if r.action == "force_flat" and isinstance(r.condition, TimeOfDayExit)
            ]
            assert flats, f"{name} has no pre-close flatten"
            assert flats[0].condition.at == dt.time(15, 45), name


class TestTheQuadrantsAreSpanned:
    """Section 5 maps the field to Natenberg's quadrants deliberately."""

    def test_four_are_short_premium_and_one_is_long(self, bound):
        net = {n: sum(leg.qty for leg in s.legs) for n, s in bound.items()}
        assert net["long_straddle_0dte"] > 0
        assert all(net[n] <= 0 for n in EXPECTED - {"long_straddle_0dte"})

    def test_the_null_hypothesis_owns_gamma_rather_than_selling_it(self, bound):
        """Every sign flipped: pay theta, own gamma, want the violent move. If
        the short-premium book cannot beat this, the premium is not real."""
        straddle = bound["long_straddle_0dte"]
        assert all(leg.qty > 0 for leg in straddle.legs)

    def test_the_defined_risk_members_carry_wings(self, bound):
        """Wings are what make max loss an arithmetic fact rather than a hope —
        the asymmetry section 6 refuses to render in one column."""
        for name in ("iron_condor_0dte", "iron_butterfly_0dte"):
            longs = [leg for leg in bound[name].legs if leg.qty > 0]
            assert len(longs) == 2, name

    def test_the_undefined_risk_members_do_not(self, bound):
        for name in ("short_strangle_0dte", "short_strangle_0dte_hedged"):
            assert all(leg.qty < 0 for leg in bound[name].legs), name

    def test_the_condor_is_the_strangle_plus_two_wings(self, bound):
        """Section 5: '#2 and #3 differ from #1 only by two offset wing legs —
        which is the acceptance test that the abstraction is real.'"""
        strangle = bound["short_strangle_0dte"].legs
        condor = bound["iron_condor_0dte"].legs
        assert len(condor) == len(strangle) + 2
        shorts = {(leg.right, leg.qty) for leg in condor if leg.qty < 0}
        assert shorts == {(leg.right, leg.qty) for leg in strangle}


class TestRiskControlsArePresentNotAssumed:
    def test_no_strategy_can_add_to_a_loser(self, bound):
        """Natenberg's first rule, enforced at config load by the shared DSL."""
        for name, strategy in bound.items():
            for rule in strategy.management:
                assert rule.action in SAFE_ACTIONS, f"{name}: {rule.action}"

    def test_the_unhedged_strangle_stops_on_price_not_delta(self, bound):
        """Delta becomes a step function near expiry, so a per-leg delta stop
        fires erratically in the last hour — exactly when it matters."""
        touches = [
            r
            for r in bound["short_strangle_0dte"].management
            if isinstance(r.condition, UnderlyingTouch)
        ]
        assert {t.condition.leg for t in touches} == {"short_put", "short_call"}

    def test_the_hedged_variant_hedges_in_the_underlying(self, bound):
        """Shares, never options: shares move delta and nothing else, so the
        adjustment does not quietly change gamma and vega too."""
        hedges = [
            r
            for r in bound["short_strangle_0dte_hedged"].management
            if r.action == "hedge_underlying"
        ]
        assert len(hedges) == 1
        assert isinstance(hedges[0].condition, AbsPortfolioDelta)

    def test_the_hedged_and_unhedged_strangles_differ_only_in_management(self, bound):
        """Otherwise the comparison measures two things at once."""
        a, b = bound["short_strangle_0dte"], bound["short_strangle_0dte_hedged"]
        assert [(leg.right, leg.qty) for leg in a.legs] == [(leg.right, leg.qty) for leg in b.legs]

    def test_the_null_hypothesis_carries_no_exit_rule(self, bound):
        """Any exit rule would smuggle a second hypothesis into the null."""
        rules = bound["long_straddle_0dte"].management
        assert len(rules) == 1 and rules[0].action == "force_flat"


def test_each_strategy_has_a_distinct_identity(bound):
    """Every run is tagged with its parameter hash. Two members colliding would
    make their results indistinguishable in the scorecard."""
    hashes = {s.param_hash() for s in bound.values()}
    assert len(hashes) == len(bound)
