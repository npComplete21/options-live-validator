"""Quote hygiene. See docs/IMPLEMENTATION_PLAN.md section 9.

Live data is dirtier than a backtest ever is: crossed and locked markets,
absent bids, stale prints, halts. Every quote passes this gate before it
reaches a topic, and **rejections are published rather than dropped** — at
0DTE the wings go untradeable for stretches of the session, and how often a
strategy could not have traded is a first-class result, not noise.

One deliberate calibration. The plan specifies an absurd-width test expressed
in *vol points* via vega, which needs a pricer this repo does not import until
Phase 2. The width check here is therefore a crude dollar/ratio backstop with
generous defaults, because the relative spread on a legitimate 0DTE wing is
enormous — $0.05 bid against $0.09 ask is a 57% relative spread and a
perfectly real quote. Over-rejecting the wings would destroy exactly the data
the skew work depends on, so this errs heavily toward admitting.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from olv.common.models import OptionQuote, RejectedQuote, RejectReason


@dataclass(frozen=True, slots=True)
class GateConfig:
    """Thresholds. All generous; tighten only with recorded evidence."""

    #: A quote older than this is not describing the current market.
    max_quote_age: dt.timedelta = dt.timedelta(seconds=15)
    #: Spot and option must be observed close enough together that implied vol
    #: computed from the pair is meaningful.
    max_underlying_skew: dt.timedelta = dt.timedelta(seconds=5)
    #: Absolute spread cap. A 0DTE QQQ contract trading wider than this is not
    #: a market anyone could transact in.
    max_spread_abs: float = 5.00
    #: Spread as a multiple of mid. Set high on purpose: penny-priced wings
    #: legitimately quote wider than their own mid.
    max_spread_ratio: float = 3.0
    #: Reject a zero bid. True by default because a contract with no bid cannot
    #: be sold, and every strategy here sells something.
    reject_zero_bid: bool = True
    #: Reject bid == ask. Anomalous in options; kept configurable.
    reject_locked: bool = True


@dataclass(frozen=True, slots=True)
class RawQuote:
    """A vendor quote before validation. Fields are deliberately permissive."""

    symbol: str
    bid: float | None
    ask: float | None
    bid_size: int | None
    ask_size: int | None
    quote_ts: dt.datetime | None
    open_interest: int | None = None
    volume: int | None = None


def _finite(*values: float | None) -> bool:
    return all(v is not None and math.isfinite(v) for v in values)


def check(
    raw: RawQuote,
    *,
    observed_at: dt.datetime,
    underlying_ts: dt.datetime,
    config: GateConfig,
) -> RejectReason | None:
    """Return the reason this quote is unusable, or ``None`` if it passes.

    Ordered cheapest and most fundamental first, so the reported reason is the
    most informative one: a quote that is both stale and crossed is reported as
    crossed, because that says something about the market rather than about our
    connection.
    """
    if raw.bid is None or raw.ask is None or raw.quote_ts is None:
        return RejectReason.MISSING
    if not _finite(raw.bid, raw.ask):
        return RejectReason.NON_FINITE
    if raw.bid < 0 or raw.ask < 0 or (raw.bid_size or 0) < 0 or (raw.ask_size or 0) < 0:
        return RejectReason.NEGATIVE
    if raw.bid > raw.ask:
        return RejectReason.CROSSED
    if config.reject_locked and raw.bid == raw.ask:
        return RejectReason.LOCKED
    if config.reject_zero_bid and raw.bid == 0:
        return RejectReason.ZERO_BID

    if observed_at - raw.quote_ts > config.max_quote_age:
        return RejectReason.STALE
    if (
        abs((raw.quote_ts - underlying_ts).total_seconds())
        > config.max_underlying_skew.total_seconds()
    ):
        return RejectReason.UNDERLYING_SKEW

    spread = raw.ask - raw.bid
    mid = (raw.bid + raw.ask) / 2
    if spread > config.max_spread_abs or (mid > 0 and spread > config.max_spread_ratio * mid):
        return RejectReason.ABSURD_WIDTH
    return None


def apply(
    raws: list[RawQuote] | tuple[RawQuote, ...],
    *,
    underlying: str,
    expiry: dt.date,
    observed_at: dt.datetime,
    underlying_ts: dt.datetime,
    feed_source: str,
    config: GateConfig | None = None,
) -> tuple[tuple[OptionQuote, ...], tuple[RejectedQuote, ...]]:
    """Split raw quotes into accepted and rejected, losing nothing."""
    from olv.common.models import parse_occ_symbol

    config = config or GateConfig()
    accepted: list[OptionQuote] = []
    rejected: list[RejectedQuote] = []

    for raw in raws:
        reason = check(raw, observed_at=observed_at, underlying_ts=underlying_ts, config=config)
        if reason is not None:
            rejected.append(
                RejectedQuote(
                    symbol=raw.symbol,
                    reason=reason,
                    bid=raw.bid,
                    ask=raw.ask,
                    quote_ts=raw.quote_ts,
                    observed_at=observed_at,
                    feed_source=feed_source,
                )
            )
            continue

        _, parsed_expiry, right, strike = parse_occ_symbol(raw.symbol)
        if parsed_expiry != expiry:
            # The vendor answered with a contract we did not ask for. That is a
            # subscription bug rather than a market condition, but recording
            # must survive it, so it is rejected and counted like anything else.
            rejected.append(
                RejectedQuote(
                    symbol=raw.symbol,
                    reason=RejectReason.WRONG_EXPIRY,
                    bid=raw.bid,
                    ask=raw.ask,
                    quote_ts=raw.quote_ts,
                    observed_at=observed_at,
                    feed_source=feed_source,
                )
            )
            continue

        accepted.append(
            OptionQuote(
                symbol=raw.symbol,
                underlying=underlying,
                expiry=parsed_expiry,
                strike=strike,
                right=right,
                bid=raw.bid,  # type: ignore[arg-type]
                ask=raw.ask,  # type: ignore[arg-type]
                bid_size=raw.bid_size or 0,
                ask_size=raw.ask_size or 0,
                quote_ts=raw.quote_ts,  # type: ignore[arg-type]
                feed_source=feed_source,
                open_interest=raw.open_interest,
                volume=raw.volume,
            )
        )
    return tuple(accepted), tuple(rejected)
