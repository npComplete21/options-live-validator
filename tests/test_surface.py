"""IV and greeks from recorded quotes. Phase 2; plan sections 3, 9 and 10.

Most of these assert **model-independent** truths rather than numbers this
repo produced. Put-call parity, ``delta_C - delta_P = 1``, and companion calls
and puts sharing gamma and vega hold for any arbitrage-free chain, so a test
that pins them catches a broken forward, a wrong clock or a mis-keyed ``right``
without also hard-coding whatever our pricer happened to return today.

The section 15 gate for this phase reads "reproduces vendor greeks to a stated
tolerance", and **that half cannot be discharged yet**: there is no broker
account, and `olv.feed.client.QuoteFeed` deliberately refuses to carry vendor
greeks at all. What stands in for it is the round-trip in
``TestRoundTripAgainstThePricer`` — our IV, fed back through the pricer,
reproduces the price it came from. The pricer itself is cross-validated against
QuantLib in options-backtest-lab, so that is an independent reference rather
than a tautology, but it is not the same claim and is not recorded as one.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest
from obl.pricing.black_scholes import price

from olv.analytics.surface import IVStatus, default_clock, enrich, stats_for, tau_for
from olv.common.sessions import MARKET_TZ, SessionCalendar

SESSION_DAY = dt.date(2026, 9, 9)  # a Wednesday, regular 09:30-16:00 session
STRIKES = (495.0, 497.0, 499.0, 500.0, 501.0, 503.0, 505.0)
FORWARD = 500.0
SIGMA = 0.25


def at(hour: int, minute: int = 0, day: dt.date = SESSION_DAY) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=MARKET_TZ)


#: 09:45 is the plan's entry trigger (section 8), so it is the default instant.
DEFAULT_INSTANTS = (at(9, 45),)


def archive_frame(
    *,
    instants=DEFAULT_INSTANTS,
    strikes=STRIKES,
    sigma: float = SIGMA,
    half_spread: float = 0.02,
    spot: float = FORWARD,
) -> pl.DataFrame:
    """An archive-shaped frame whose prices are arbitrage-free by construction.

    Mirrors the columns ``SnapshotPublisher`` puts on the quotes topic, which is
    what the archiver writes and therefore what this layer really consumes.
    """
    calendar = SessionCalendar()
    rows = []
    for ts in instants:
        tau = default_clock(calendar).year_fraction(ts, calendar.expiry_instant(SESSION_DAY))
        for strike in strikes:
            for right in ("C", "P"):
                mid = float(price(FORWARD, strike, tau, sigma, 0.0, 0.0, right))
                rows.append(
                    {
                        "symbol": f"QQQ{SESSION_DAY:%y%m%d}{right}{int(strike * 1000):08d}",
                        "underlying": "QQQ",
                        "expiry": SESSION_DAY,
                        "strike": strike,
                        "right": right,
                        "bid": max(mid - half_spread, 0.0),
                        "ask": mid + half_spread,
                        "bid_size": 10,
                        "ask_size": 10,
                        "mid": mid,
                        "spread": 2 * half_spread,
                        "quote_ts": ts,
                        "observed_at": ts,
                        "underlying_price": spot,
                        "underlying_ts": ts,
                        "feed_source": "test",
                    }
                )
    return pl.DataFrame(rows)


@pytest.fixture(scope="module")
def calendar() -> SessionCalendar:
    return SessionCalendar()


@pytest.fixture
def enriched() -> pl.DataFrame:
    return enrich(archive_frame())


class TestTauComesFromTheSharedClock:
    def test_0945_matches_the_section_3_figure(self, calendar):
        """0.003815 is the number the plan quotes and sizes strikes with. If it
        moves, every delta-selected strike moves with it."""
        tau = tau_for(at(9, 45), SESSION_DAY, clock=default_clock(calendar), calendar=calendar)
        assert tau == pytest.approx(0.003815, abs=1e-6)

    def test_tau_shrinks_through_the_session(self, calendar):
        clock = default_clock(calendar)
        taus = [tau_for(at(h), SESSION_DAY, clock=clock, calendar=calendar) for h in (10, 12, 14)]
        assert taus == sorted(taus, reverse=True)

    def test_expiry_instant_is_the_real_close_not_a_guess(self, calendar):
        """An early close shortens tau; assuming 16:00 would overstate it ~2x on
        exactly the days liquidity is worst."""
        assert calendar.expiry_instant(SESSION_DAY) == at(16, 0)


class TestModelIndependentInvariants:
    """These hold for any arbitrage-free chain, whatever model produced it."""

    def test_companion_call_and_put_share_one_implied_vol(self, enriched):
        """Put-call parity. A disagreement here means the forward is wrong — the
        single most likely way this layer breaks silently."""
        by_strike = enriched.filter(pl.col("iv_status") == IVStatus.OK).pivot(
            values="sigma_mid", index="strike", on="right", aggregate_function="first"
        )
        for _, call_iv, put_iv in by_strike.iter_rows():
            assert call_iv == pytest.approx(put_iv, rel=1e-6)

    def test_call_delta_minus_put_delta_is_one(self, enriched):
        by_strike = enriched.pivot(
            values="delta", index="strike", on="right", aggregate_function="first"
        )
        for _, call_delta, put_delta in by_strike.iter_rows():
            assert call_delta - put_delta == pytest.approx(1.0, abs=1e-9)

    @pytest.mark.parametrize("greek", ["gamma", "vega"])
    def test_companion_options_share_gamma_and_vega(self, enriched, greek):
        """Natenberg's rule, and the reason the residual layer keys volatility
        exposure by (strike, expiry) rather than by call-vs-put — see the
        constraint table in plan section 2."""
        by_strike = enriched.pivot(
            values=greek, index="strike", on="right", aggregate_function="first"
        )
        for _, call_value, put_value in by_strike.iter_rows():
            assert call_value == pytest.approx(put_value, rel=1e-9)


class TestRoundTripAgainstThePricer:
    def test_implied_vol_recovers_the_vol_that_made_the_price(self, enriched):
        priced = enriched.filter(pl.col("iv_status") == IVStatus.OK)
        assert priced.height > 0
        assert priced["sigma_mid"].to_numpy() == pytest.approx(SIGMA, rel=1e-6)

    def test_reprinting_from_our_iv_reproduces_the_quote(self, enriched):
        """price -> IV -> price. The pricer is QuantLib-cross-validated upstream,
        so this is an independent reference, but it is *not* the vendor-greeks
        comparison the phase gate asks for."""
        priced = enriched.filter(pl.col("iv_status") == IVStatus.OK)
        reprinted = price(
            priced["forward"].to_numpy(),
            priced["strike"].to_numpy(),
            priced["tau"].to_numpy(),
            priced["sigma_mid"].to_numpy(),
            0.0,
            0.0,
            priced["right"].to_numpy().astype(str),
        )
        assert reprinted == pytest.approx(priced["mid"].to_numpy(), rel=1e-6)


class TestSpreadInVolPoints:
    def test_half_spread_is_the_bid_ask_iv_gap(self, enriched):
        """Section 10's hypothesis is that the spread, not the IV/RV edge,
        decides the outcome. A mid-only sigma cannot show that, so the spread is
        carried in the unit the strategy actually trades in."""
        usable = enriched.filter(pl.col("sigma_bid").is_finite() & pl.col("sigma_ask").is_finite())
        assert usable.height > 0
        expected = (usable["sigma_ask"] - usable["sigma_bid"]) / 2
        assert usable["vol_half_spread"].to_numpy() == pytest.approx(expected.to_numpy())

    def test_a_wider_market_costs_more_vol_points(self):
        tight = enrich(archive_frame(half_spread=0.01))
        wide = enrich(archive_frame(half_spread=0.10))
        assert wide["vol_half_spread"].mean() > tight["vol_half_spread"].mean()


class TestUnpriceableRowsAreRecordedNotDropped:
    def test_after_the_close_tau_is_zero_and_rows_are_marked_expired(self):
        """Not an error: positions are flat by 15:45 and the recorder outlives
        them, so post-close rows are expected in the archive."""
        out = enrich(archive_frame(instants=(at(16, 30),)))
        assert set(out["iv_status"]) == {IVStatus.EXPIRED}
        assert stats_for(out).priced == 0

    def test_a_zero_bid_row_has_no_usable_price(self):
        frame = archive_frame().with_columns(pl.lit(0.0).alias("mid"), pl.lit(0.0).alias("bid"))
        out = enrich(frame)
        assert set(out["iv_status"]) == {IVStatus.NO_PRICE}

    def test_non_identifiable_vol_is_nan_not_a_bracket_edge(self):
        """Deep ITM contracts are pure intrinsic to machine precision, so vol is
        genuinely not recoverable. Reporting the edge of the root-finder's
        bracket would silently poison anything calibrated from it.

        The row is added *unpaired* so it cannot move the parity forward — the
        forward is measured only from strikes quoting both sides, and a contract
        that is untradeable for vol should not be allowed to reprice the chain
        it sits in.
        """
        frame = archive_frame()
        intrinsic = FORWARD - 400.0
        deep_itm = frame.head(1).with_columns(
            pl.lit(400.0).alias("strike"),
            pl.lit("C").alias("right"),
            pl.lit(intrinsic).alias("mid"),
            pl.lit(intrinsic).alias("bid"),
            pl.lit(intrinsic).alias("ask"),
        )
        out = enrich(pl.concat([frame, deep_itm]))

        call = out.filter(pl.col("strike") == 400.0)
        assert call["iv_status"][0] == IVStatus.NOT_IDENTIFIABLE
        assert np.isnan(call["sigma_mid"][0])
        # The rest of the chain is unaffected: one unpriceable contract is not
        # an unpriceable snapshot.
        assert stats_for(out).priced == frame.height

    def test_stats_account_for_every_row(self, enriched):
        s = stats_for(enriched)
        assert s.priced + s.not_identifiable + s.expired + s.no_price == s.rows


class TestSnapshotsAreEnrichedIndependently:
    def test_each_instant_gets_its_own_forward(self):
        """One snapshot is one instant and therefore one forward. Sharing a
        forward across instants would smear the basis through time and surface
        later as a skew artefact."""
        out = enrich(archive_frame(instants=(at(9, 45), at(11, 0), at(14, 30))))
        assert out["observed_at"].n_unique() == 3
        per_snapshot = out.group_by("observed_at").agg(pl.col("tau").first())
        assert per_snapshot["tau"].n_unique() == 3

    def test_no_vendor_greeks_are_consumed(self):
        """Section 9: greeks and IV are computed here, never taken from a vendor
        whose clock, rate and dividend conventions would ride along unseen."""
        source_columns = set(archive_frame().columns)
        assert not source_columns & {"delta", "gamma", "vega", "theta", "iv", "sigma"}
