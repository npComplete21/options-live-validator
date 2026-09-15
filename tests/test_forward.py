"""The forward, measured from the chain. See docs/IMPLEMENTATION_PLAN.md section 3.

The point of these tests is that the forward is *observed*, not assumed. At
0DTE, using spot in its place is wrong by roughly a tick, which is a large
fraction of a wing's entire premium.
"""

from __future__ import annotations

import math

import pytest

from olv.analytics.forward import (
    ForwardEstimate,
    NoParityPairError,
    implied_forward,
    parity_forward_per_strike,
)

SPOT = 500.0


def chain(forward: float, strikes, sigma: float = 0.20, tau: float = 0.0038):
    """Arbitrage-free call/put mids from one forward, via the shared pricer.

    Built from ``obl.pricing`` rather than by hand so the fixture cannot drift
    from the model the code under test inverts.
    """
    from obl.pricing.black_scholes import price

    calls, puts = {}, {}
    for k in strikes:
        calls[k] = float(price(forward, k, tau, sigma, 0.0, 0.0, "C"))
        puts[k] = float(price(forward, k, tau, sigma, 0.0, 0.0, "P"))
    return calls, puts


class TestParityRecoversTheForward:
    def test_exact_on_an_arbitrage_free_chain(self):
        """If parity did not recover the forward that generated the chain, the
        estimator would be measuring its own error."""
        strikes = [495.0, 497.5, 500.0, 502.5, 505.0]
        calls, puts = chain(500.75, strikes)
        est = implied_forward(calls, puts, SPOT)
        assert est.forward == pytest.approx(500.75, abs=1e-9)
        assert est.method == "put_call_parity"

    def test_basis_is_the_measured_rate_dividend_and_borrow(self):
        calls, puts = chain(500.75, [499.0, 500.0, 501.0])
        est = implied_forward(calls, puts, SPOT)
        assert est.basis == pytest.approx(0.75, abs=1e-9)

    def test_every_strike_agrees_on_an_arbitrage_free_chain(self):
        calls, puts = chain(500.5, [490.0, 495.0, 500.0, 505.0, 510.0])
        per_strike = parity_forward_per_strike(calls, puts)
        assert all(v == pytest.approx(500.5, abs=1e-9) for v in per_strike.values())

    def test_dispersion_is_zero_when_strikes_agree(self):
        calls, puts = chain(500.5, [498.0, 500.0, 502.0])
        assert implied_forward(calls, puts, SPOT).dispersion == pytest.approx(0.0, abs=1e-9)


class TestRobustness:
    def test_a_single_bad_print_does_not_move_the_forward(self):
        """The estimator is a median for this reason. At 0DTE a stale or crossed
        print is routine, and the forward feeds every implied vol in the
        snapshot — the wrong place to average in an outlier."""
        strikes = [496.0, 498.0, 500.0, 502.0, 504.0]
        calls, puts = chain(500.0, strikes)
        calls[500.0] += 5.0  # one contract prints absurdly rich

        est = implied_forward(calls, puts, SPOT)
        assert est.forward == pytest.approx(500.0, abs=1e-9)
        assert est.dispersion > 0.0, "the outlier should still be visible as dispersion"

    def test_dispersion_flags_a_chain_not_to_be_trusted(self):
        """A chain whose strikes disagree about the forward is one whose implied
        vols should not be believed that instant, so the disagreement is
        reported rather than smoothed away."""
        calls, puts = chain(500.0, [498.0, 500.0, 502.0])
        calls[498.0] += 1.0
        puts[502.0] += 1.0
        assert implied_forward(calls, puts, SPOT).dispersion > 0.1

    def test_only_the_nearest_strikes_are_used(self):
        """Far from the money one side is nearly worthless, so C - P is dominated
        by the spread rather than the forward."""
        strikes = [400.0, 450.0, 499.0, 500.0, 501.0, 550.0, 600.0]
        calls, puts = chain(500.0, strikes)
        est = implied_forward(calls, puts, SPOT, max_strikes=3)
        assert est.strikes_used == 3


class TestUnusableChains:
    def test_no_paired_strike_falls_back_to_spot(self):
        est = implied_forward({500.0: 2.0}, {505.0: 3.0}, SPOT)
        assert est == ForwardEstimate(SPOT, SPOT, 0, 0.0, "spot_fallback")

    def test_fallback_can_be_refused(self):
        """Calibration work should refuse it: a forward that quietly became spot
        is a silent bias of about a tick, and it would not announce itself."""
        with pytest.raises(NoParityPairError, match="cannot be measured"):
            implied_forward({}, {}, SPOT, allow_spot_fallback=False)

    def test_a_single_pair_is_usable_and_reports_no_dispersion(self):
        calls, puts = chain(500.25, [500.0])
        est = implied_forward(calls, puts, SPOT)
        assert est.forward == pytest.approx(500.25, abs=1e-9)
        assert est.strikes_used == 1
        assert est.dispersion == 0.0


class TestInputValidation:
    def test_non_positive_discount_factor_is_refused(self):
        with pytest.raises(ValueError, match="discount factor must be positive"):
            parity_forward_per_strike({500.0: 1.0}, {500.0: 1.0}, discount_factor=0.0)

    def test_max_strikes_must_be_at_least_one(self):
        with pytest.raises(ValueError, match="at least 1"):
            implied_forward({500.0: 1.0}, {500.0: 1.0}, SPOT, max_strikes=0)


def test_discount_factor_is_immaterial_at_0dte():
    """The module leaves df at 1.0, and this is why: df scales ``C - P``, which
    near the money is cents, so a 0DTE-sized df error moves the forward by far
    less than a tick. The error that mattered was in the S -> F step, which
    parity removes entirely."""
    calls, puts = chain(500.0, [499.0, 500.0, 501.0])
    unit = implied_forward(calls, puts, SPOT)
    discounted = implied_forward(calls, puts, SPOT, discount_factor=math.exp(-0.04 * 0.0038))
    assert abs(unit.forward - discounted.forward) < 0.001
