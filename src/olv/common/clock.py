"""Time to expiry — the largest single modelling lever in this repo.

See docs/IMPLEMENTATION_PLAN.md section 3. At 09:45 on expiration day there are
6.25 hours to the close, and the two obvious conventions disagree wildly:

    calendar       6.25 / (24 * 365)     = 0.000713 years
    trading hours  6.25 / (6.5 * 252)    = 0.003815 years

That is 5.35x in tau and therefore **2.31x in sigma*sqrt(tau)**. Every strike
selector is expressed in deltas or standard deviations, so this choice moves
which strikes a strategy sells by better than a factor of two. It is not a
rounding detail.

Two rules follow, and ``tests/test_clock.py`` pins both:

*   The clock must be identical here and in options-backtest-lab. When the
    shared package lands (Phase 2) this module moves into it; until then it is
    the reference implementation and the ratio above is the conformance test.
*   Never take greeks or implied vol from a market-data vendor. They embed the
    vendor's clock, rate and dividend conventions, which would silently
    contaminate both strike selection and every residual.

``VolWeightedClock`` is deliberately absent. Intraday volatility is U-shaped,
so trading-hours time still misstates tau through the session, but the
weighting curve is only knowable from recorded data (Phase 2). Guessing it now
would be exactly the invented-assumption problem this program exists to avoid.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

from olv.common.sessions import MARKET_TZ, SessionCalendar

#: Standard market convention: 365 calendar days.
CALENDAR_DAYS_PER_YEAR = 365.0
CALENDAR_SECONDS_PER_YEAR = CALENDAR_DAYS_PER_YEAR * 24 * 3600

#: 252 sessions x 6.5 hours. The denominator for trading-time tau.
TRADING_SESSIONS_PER_YEAR = 252.0
REGULAR_SESSION_HOURS = 6.5
TRADING_SECONDS_PER_YEAR = TRADING_SESSIONS_PER_YEAR * REGULAR_SESSION_HOURS * 3600

#: Below this, Black-Scholes is numerically degenerate: delta becomes a step
#: function and vega collapses. Positions must be flat before this is reached
#: (the 15:45 force-flat in section 8), so hitting it means a bug, not a trade.
MIN_TAU = 1e-8


@runtime_checkable
class TauClock(Protocol):
    """Maps a pair of instants to a year fraction."""

    name: str

    def year_fraction(self, now: dt.datetime, expiry: dt.datetime) -> float: ...


def _validate(now: dt.datetime, expiry: dt.datetime) -> None:
    if now.tzinfo is None or expiry.tzinfo is None:
        raise ValueError("clock requires timezone-aware datetimes; naive input is ambiguous")
    if expiry < now:
        raise ValueError(f"expiry {expiry.isoformat()} precedes now {now.isoformat()}")


class CalendarClock:
    """Wall-clock time over 365 days.

    Correct for multi-week tenors and wrong for 0DTE, where it prices six hours
    of market risk as if it were spread across six hours of a sleeping world.
    Kept because backtest-lab's longer-dated work uses it and because it is the
    baseline the 2.31x comparison is made against.
    """

    name = "calendar/365"

    def year_fraction(self, now: dt.datetime, expiry: dt.datetime) -> float:
        _validate(now, expiry)
        return max((expiry - now).total_seconds() / CALENDAR_SECONDS_PER_YEAR, 0.0)


class TradingHoursClock:
    """Time measured only while the market is open. The v1 default.

    Counts real session seconds between the two instants, so overnights,
    weekends and holidays contribute nothing and early closes shorten the day
    correctly.
    """

    name = "trading-hours/252x6.5"

    def __init__(self, calendar: SessionCalendar | None = None) -> None:
        self.calendar = calendar or SessionCalendar()

    def trading_seconds(self, now: dt.datetime, expiry: dt.datetime) -> float:
        _validate(now, expiry)
        start = now.astimezone(MARKET_TZ)
        end = expiry.astimezone(MARKET_TZ)
        sessions = self.calendar.sessions_between(start.date(), end.date())
        return sum(s.overlap_seconds(start, end) for s in sessions)

    def year_fraction(self, now: dt.datetime, expiry: dt.datetime) -> float:
        return self.trading_seconds(now, expiry) / TRADING_SECONDS_PER_YEAR


def clock_ratio(
    now: dt.datetime,
    expiry: dt.datetime,
    a: TauClock,
    b: TauClock,
) -> float:
    """``sqrt(tau_a / tau_b)`` — how much the clock choice moves sigma*sqrt(tau).

    This is the quantity that matters, because strike selectors work in
    standard deviations. Reported in the Phase 2 surface report so the
    convention's effect stays visible rather than buried in a constant.
    """
    tau_b = b.year_fraction(now, expiry)
    if tau_b <= MIN_TAU:
        raise ValueError(f"denominator clock {b.name} returned a degenerate tau ({tau_b})")
    return (a.year_fraction(now, expiry) / tau_b) ** 0.5
