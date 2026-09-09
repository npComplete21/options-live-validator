"""Trading sessions for the underlying's exchange.

Session boundaries have to be identical here and in options-backtest-lab, or a
live-vs-backtest comparison is measuring a calendar disagreement rather than a
strategy. Both repos use ``pandas_market_calendars`` for that reason; see
docs/IMPLEMENTATION_PLAN.md section 16.
"""

from __future__ import annotations

import datetime as dt
import functools
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

MARKET_TZ = ZoneInfo("America/New_York")

#: US equity options cease trading at the equity close. A 0DTE contract's
#: expiry instant, for pricing purposes, is that close.
REGULAR_CLOSE = dt.time(16, 0)


@dataclass(frozen=True)
class Session:
    """One trading day, as an inclusive-open/exclusive-close instant pair."""

    date: dt.date
    open: dt.datetime
    close: dt.datetime

    @property
    def seconds(self) -> float:
        return (self.close - self.open).total_seconds()

    def overlap_seconds(self, start: dt.datetime, end: dt.datetime) -> float:
        """Seconds of this session lying inside ``[start, end)``."""
        lo = max(self.open, start)
        hi = min(self.close, end)
        return max(0.0, (hi - lo).total_seconds())


class SessionCalendar:
    """Exchange sessions, with early closes handled rather than assumed away.

    Half-days matter more than they sound: a 0DTE strategy on a 13:00 close has
    barely half its usual time to expiry, and a clock that assumes 16:00 would
    overstate tau by roughly 2x on exactly the days liquidity is worst.
    """

    def __init__(self, exchange: str = "NASDAQ") -> None:
        self.exchange = exchange
        self._calendar = mcal.get_calendar(exchange)

    @functools.lru_cache(maxsize=32)  # noqa: B019 - bounded, instance-scoped by key
    def _schedule(self, start: dt.date, end: dt.date) -> tuple[Session, ...]:
        sched = self._calendar.schedule(start_date=start, end_date=end)
        return tuple(
            Session(
                date=idx.date(),
                open=row.market_open.to_pydatetime().astimezone(MARKET_TZ),
                close=row.market_close.to_pydatetime().astimezone(MARKET_TZ),
            )
            for idx, row in sched.iterrows()
        )

    def sessions_between(self, start: dt.date, end: dt.date) -> tuple[Session, ...]:
        """All sessions in ``[start, end]``, inclusive of both endpoints."""
        if end < start:
            return ()
        return self._schedule(start, end)

    def session_on(self, day: dt.date) -> Session | None:
        """The session for ``day``, or ``None`` if the market is closed."""
        sessions = self._schedule(day, day)
        return sessions[0] if sessions else None

    def is_trading_day(self, day: dt.date) -> bool:
        return self.session_on(day) is not None

    def expiry_instant(self, day: dt.date) -> dt.datetime:
        """The moment a contract expiring on ``day`` stops trading.

        Honours early closes. Raises if ``day`` is not a trading day, because a
        contract cannot expire on a day the market never opened and silently
        picking 16:00 would corrupt every tau computed from it.
        """
        session = self.session_on(day)
        if session is None:
            raise ValueError(f"{day} is not a trading day on {self.exchange}")
        return session.close
