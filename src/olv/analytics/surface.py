"""Recorded quotes -> implied vol and greeks. Phase 2, plan sections 3 and 9.

Everything here is a pure function of a recorded frame. That is deliberate:
plan section 11 requires that replay from Parquet and replay from a Kafka
offset drive the **same** engine, and the cheapest way to guarantee that is to
have only one engine, with the drivers outside it. Phase 2 drives this over the
archive; Phase 4 wires the identical call into a consumer.

Three rules this module exists to enforce
-----------------------------------------
*   **Greeks and IV are computed here, never taken from a vendor.** A vendor's
    IV embeds a vendor's clock, rate and dividend conventions. The clock alone
    moves ``sigma*sqrt(tau)`` by 2.31x at 0DTE (section 3), so importing theirs
    would contaminate strike selection and every residual with a convention we
    did not choose and cannot see.
*   **Tau comes from the shared clock**, ``obl.timebase``, the same module
    options-backtest-lab prices with. Computing a year fraction inline here
    would silently re-fork the convention the two repos just merged.
*   **The forward is measured, not assumed** — see :mod:`olv.analytics.forward`.

Why sigma is computed at bid, mid *and* ask
-------------------------------------------
Section 10's working hypothesis is that transaction costs, not the IV/RV edge,
decide whether this program reaches its return target: on a 10-delta QQQ 0DTE
option quoted $0.05/$0.09, the spread is plausibly larger than the entire
volatility premium. A single mid-derived sigma cannot show that. The bid/ask
pair converts the spread into **vol points**, which is the unit the strategy
actually trades in and the one section 13's surface report asks for.

Non-identifiable vol is recorded, not dropped
---------------------------------------------
``implied_vol`` returns NaN both for targets outside the no-arbitrage bounds
and for contracts that are pure intrinsic to machine precision, where vol is
genuinely not recoverable from the price. Those are not failures to hide: at
0DTE a large share of the wing is exactly that, and *how much of the chain
carries no usable vol* is a first-class result in the same way rejection rates
are (section 9). Every row keeps a status so the report can count them.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import numpy as np
import polars as pl
from obl.pricing.black_scholes import implied_vol, price_and_greeks
from obl.timebase import MIN_TAU, TauClock, TradingHoursClock

from olv.analytics.forward import DEFAULT_MAX_STRIKES, ForwardEstimate, implied_forward
from olv.common.sessions import SessionCalendar

logger = logging.getLogger(__name__)

#: Rate and dividend are zero *because the forward already carries them*. The
#: parity forward is the market's own basis, so re-applying a rate here would
#: double-count it. Passing ``S=forward`` with ``r=q=0`` makes the pricer's
#: internal ``forward()`` the identity, which is exactly what we want.
NO_RATE = 0.0
NO_DIVIDEND = 0.0


class IVStatus:
    """Why a row does or does not carry a usable implied vol."""

    OK = "ok"
    NOT_IDENTIFIABLE = "not_identifiable"
    EXPIRED = "expired"
    NO_PRICE = "no_price"


@dataclass(frozen=True, slots=True)
class EnrichmentStats:
    rows: int
    priced: int
    not_identifiable: int
    expired: int
    no_price: int

    @property
    def identifiable_rate(self) -> float:
        return self.priced / self.rows if self.rows else 0.0

    def __str__(self) -> str:
        return (
            f"{self.rows} rows, {self.priced} priced ({self.identifiable_rate:.1%}), "
            f"{self.not_identifiable} vol not identifiable, {self.expired} expired, "
            f"{self.no_price} no usable price"
        )


def default_clock(calendar: SessionCalendar | None = None) -> TradingHoursClock:
    """The v1 clock for this repo: flat session seconds (plan section 3).

    Section 3 recommends trading-hours for v1, with the vol-weighted curve
    substituted once it has been *measured*. ``obl.timebase.VolWeightedClock``
    raises until that data exists, which is the intended state: this repo has
    not recorded a real session yet.
    """
    return TradingHoursClock(calendar or SessionCalendar())


def tau_for(
    quote_ts: dt.datetime,
    expiry: dt.date,
    *,
    clock: TauClock,
    calendar: SessionCalendar,
) -> float:
    """Year fraction from a quote's timestamp to the close it expires at.

    The expiry instant is the session's **real** close, so an early close
    shortens tau correctly. Assuming 16:00 would overstate tau by roughly 2x on
    half-days — exactly the sessions where liquidity is worst.
    """
    return clock.year_fraction(quote_ts, calendar.expiry_instant(expiry))


def _sigma(target: np.ndarray, forward: np.ndarray, strike, tau, right) -> np.ndarray:
    """Implied vol with the forward supplied as spot and no rate applied."""
    with np.errstate(all="ignore"):
        return implied_vol(target, forward, strike, tau, NO_RATE, NO_DIVIDEND, right)


def enrich_snapshot(
    frame: pl.DataFrame,
    *,
    forward: float,
    clock: TauClock,
    calendar: SessionCalendar,
) -> pl.DataFrame:
    """Add tau, moneyness, sigma at bid/mid/ask, and greeks to one snapshot.

    One snapshot means one instant and therefore one forward — which is the
    reason ``chain.QQQ`` is a single partition (section 11). Enriching rows from
    different instants against a shared forward would smear the basis across
    time and show up later as a skew artefact.
    """
    if frame.is_empty():
        # Full schema, not a partial one: an empty recording is a normal
        # outcome (a holiday, a halted session), and a consumer that has to
        # branch on "did any row survive" will eventually forget to.
        floats = (
            "tau",
            "forward",
            "log_moneyness",
            "sigma_bid",
            "sigma_mid",
            "sigma_ask",
            "vol_half_spread",
            "delta",
            "gamma",
            "vega",
            "theta",
        )
        return frame.with_columns(
            *[pl.lit(None, dtype=pl.Float64).alias(c) for c in floats],
            pl.lit(None, dtype=pl.String).alias("iv_status"),
        )

    taus = np.array(
        [
            tau_for(ts, exp, clock=clock, calendar=calendar)
            for ts, exp in zip(frame["quote_ts"], frame["expiry"], strict=True)
        ]
    )
    strike = frame["strike"].to_numpy().astype(float)
    right = frame["right"].to_numpy().astype(str)
    fwd = np.full_like(strike, float(forward))

    mid = frame["mid"].to_numpy().astype(float)
    bid = frame["bid"].to_numpy().astype(float)
    ask = frame["ask"].to_numpy().astype(float)

    expired = taus <= MIN_TAU
    # A zero or absent bid cannot be inverted for vol, and at 0DTE it is the
    # normal state of the wing rather than an anomaly.
    no_price = ~expired & ~(np.isfinite(mid) & (mid > 0))

    safe_tau = np.where(expired, np.nan, taus)
    sigma_mid = _sigma(mid, fwd, strike, safe_tau, right)
    sigma_bid = _sigma(np.where(bid > 0, bid, np.nan), fwd, strike, safe_tau, right)
    sigma_ask = _sigma(np.where(ask > 0, ask, np.nan), fwd, strike, safe_tau, right)

    status = np.where(
        expired,
        IVStatus.EXPIRED,
        np.where(
            no_price,
            IVStatus.NO_PRICE,
            np.where(np.isnan(sigma_mid), IVStatus.NOT_IDENTIFIABLE, IVStatus.OK),
        ),
    )

    greeks = price_and_greeks(fwd, strike, safe_tau, sigma_mid, NO_RATE, NO_DIVIDEND, right)

    return frame.with_columns(
        pl.Series("tau", taus),
        pl.Series("forward", fwd),
        pl.Series("log_moneyness", np.log(strike / fwd)),
        pl.Series("sigma_bid", sigma_bid),
        pl.Series("sigma_mid", sigma_mid),
        pl.Series("sigma_ask", sigma_ask),
        # The spread expressed in the unit the strategy trades in. Section 10
        # expects this to rival the entire volatility premium at 0DTE.
        pl.Series("vol_half_spread", (sigma_ask - sigma_bid) / 2.0),
        pl.Series("delta", greeks["delta"]),
        pl.Series("gamma", greeks["gamma"]),
        pl.Series("vega", greeks["vega"]),
        pl.Series("theta", greeks["theta"]),
        pl.Series("iv_status", status),
    )


def stats_for(frame: pl.DataFrame) -> EnrichmentStats:
    counts = dict(
        frame.group_by("iv_status").len().iter_rows()  # type: ignore[arg-type]
    )
    return EnrichmentStats(
        rows=frame.height,
        priced=counts.get(IVStatus.OK, 0),
        not_identifiable=counts.get(IVStatus.NOT_IDENTIFIABLE, 0),
        expired=counts.get(IVStatus.EXPIRED, 0),
        no_price=counts.get(IVStatus.NO_PRICE, 0),
    )


#: Columns the recorder writes as ISO strings. They arrive that way from both
#: drivers - Kafka carries JSON, and the archiver writes what it consumed - so
#: parsing here is what lets one engine serve replay from either.
TIMESTAMP_COLUMNS = ("quote_ts", "observed_at", "underlying_ts")
DATE_COLUMNS = ("expiry",)


def normalise_timestamps(frame: pl.DataFrame) -> pl.DataFrame:
    """Parse ISO timestamp columns, leaving already-typed frames untouched.

    The archive stores timestamps as strings because that is how they crossed
    Kafka, and a tau clock cannot be handed a string. Parsing is idempotent so
    callers holding a typed frame - tests, or a future in-process consumer - do
    not have to care which shape they have.
    """
    casts = [
        # UTC explicitly: the recorded strings carry an offset, and polars
        # refuses to guess. The clock is timezone-agnostic (it pads its
        # session lookup by a day for exactly this reason), so normalising
        # to UTC here changes no tau.
        pl.col(name).str.to_datetime(time_zone="UTC").alias(name)
        for name in TIMESTAMP_COLUMNS
        if name in frame.columns and frame.schema[name] == pl.String
    ] + [
        pl.col(name).str.to_date().alias(name)
        for name in DATE_COLUMNS
        if name in frame.columns and frame.schema[name] == pl.String
    ]
    return frame.with_columns(casts) if casts else frame


def snapshot_forward(
    frame: pl.DataFrame,
    *,
    max_strikes: int = DEFAULT_MAX_STRIKES,
    allow_spot_fallback: bool = True,
) -> ForwardEstimate:
    """Measure one snapshot's forward from the strikes quoting both sides."""
    usable = frame.filter(pl.col("mid").is_finite() & (pl.col("mid") > 0))
    sides = {
        right: dict(
            usable.filter(pl.col("right") == right).select("strike", "mid").iter_rows()  # type: ignore[arg-type]
        )
        for right in ("C", "P")
    }
    return implied_forward(
        sides["C"],
        sides["P"],
        float(frame["underlying_price"][0]),
        max_strikes=max_strikes,
        allow_spot_fallback=allow_spot_fallback,
    )


def enrich(
    frame: pl.DataFrame,
    *,
    clock: TauClock | None = None,
    calendar: SessionCalendar | None = None,
    allow_spot_fallback: bool = True,
) -> pl.DataFrame:
    """Enrich a recorded archive, one measured forward per snapshot.

    Snapshots are keyed on ``observed_at``, the instant the recorder polled,
    rather than on ``quote_ts``, which varies contract by contract within a
    single poll. Grouping on the latter would produce a different "forward" for
    every contract and defeat the point.
    """
    calendar = calendar or SessionCalendar()
    clock = clock or default_clock(calendar)
    frame = normalise_timestamps(frame)

    enriched = [
        enrich_snapshot(
            group,
            forward=snapshot_forward(group, allow_spot_fallback=allow_spot_fallback).forward,
            clock=clock,
            calendar=calendar,
        )
        for (_, group) in sorted(frame.group_by("observed_at").__iter__(), key=lambda kv: kv[0])
    ]
    if not enriched:
        return enrich_snapshot(frame, forward=float("nan"), clock=clock, calendar=calendar)
    return pl.concat(enriched)


def forward_series(frame: pl.DataFrame) -> pl.DataFrame:
    """Per-snapshot forward, basis and dispersion — an output of its own.

    The basis is the market's rate, dividend and borrow observed rather than
    assumed, and the dispersion says whether the chain's strikes agreed. Both
    belong in the surface report: a snapshot whose strikes disagree about the
    forward is one whose implied vols should not be trusted.
    """
    rows = []
    for observed_at, group in normalise_timestamps(frame).group_by("observed_at"):
        est = snapshot_forward(group)
        rows.append(
            {
                "observed_at": observed_at[0] if isinstance(observed_at, tuple) else observed_at,
                "spot": est.spot,
                "forward": est.forward,
                "basis": est.basis,
                "strikes_used": est.strikes_used,
                "dispersion": est.dispersion,
                "method": est.method,
            }
        )
    return pl.DataFrame(rows).sort("observed_at") if rows else pl.DataFrame()
