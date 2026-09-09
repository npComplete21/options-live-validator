"""Which contracts to watch. See docs/IMPLEMENTATION_PLAN.md section 9.

Two mistakes are available here and the design avoids both. Subscribing to the
whole chain is wasteful — a full QQQ chain is thousands of contracts.
Subscribing only to open positions is worse: the residual work needs quotes on
the strikes the backtest *priced*, including strikes that are never traded,
and those are exactly the wing strikes where the skew lives.

So: a moneyness band around spot, rebuilt each session.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from olv.common.models import Right, occ_symbol
from olv.common.sessions import SessionCalendar

#: 0DTE needs a far narrower band than a 45-DTE strategy: beyond ~5% there is
#: no meaningful premium left on the day of expiry.
DEFAULT_BAND = 0.05


@dataclass(frozen=True, slots=True)
class WatchSet:
    """The contracts to subscribe to for one session."""

    underlying: str
    expiry: dt.date
    spot: float
    band: float
    strikes: tuple[float, ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(
            occ_symbol(self.underlying, self.expiry, right, strike)
            for strike in self.strikes
            for right in (Right.CALL, Right.PUT)
        )

    def __len__(self) -> int:
        return len(self.strikes) * 2


def log_moneyness(strike: float, spot: float) -> float:
    """``ln(K/S)``.

    Uses spot rather than the forward deliberately: at 0DTE the carry
    adjustment over a few hours is negligible against the strike grid's $1
    spacing, and using spot keeps the watch set independent of a rate and
    dividend assumption. Residual work at Phase 2 uses the forward, where it
    matters.
    """
    if spot <= 0 or strike <= 0:
        raise ValueError(f"spot and strike must be positive, got {spot=} {strike=}")
    return math.log(strike / spot)


def zero_dte_expiry(session_date: dt.date, calendar: SessionCalendar | None = None) -> dt.date:
    """Today's expiry, or raise if the market is closed.

    QQQ lists expirations every weekday, so on any trading day the 0DTE expiry
    is that day. Raising on a closed day is deliberate: silently rolling to the
    next session would have a "0DTE" strategy quietly trading a 1DTE contract.
    """
    calendar = calendar or SessionCalendar()
    if not calendar.is_trading_day(session_date):
        raise ValueError(f"{session_date} is not a trading day; there is no 0DTE expiry")
    return session_date


def select_strikes(
    spot: float,
    available: list[float] | tuple[float, ...],
    band: float = DEFAULT_BAND,
) -> tuple[float, ...]:
    """Strikes within ``|ln(K/S)| <= band``, sorted ascending.

    Takes the venue's actual strike list rather than generating a grid, because
    listed increments vary ($1 near the money on QQQ, wider in the wings) and a
    generated grid would silently request contracts that do not exist.
    """
    if band <= 0:
        raise ValueError(f"band must be positive, got {band}")
    return tuple(sorted(k for k in available if k > 0 and abs(log_moneyness(k, spot)) <= band))


def build_watch_set(
    underlying: str,
    expiry: dt.date,
    spot: float,
    available_strikes: list[float] | tuple[float, ...],
    band: float = DEFAULT_BAND,
) -> WatchSet:
    strikes = select_strikes(spot, available_strikes, band)
    if not strikes:
        raise ValueError(f"no listed strikes within {band} of spot {spot}")
    return WatchSet(
        underlying=underlying.upper(), expiry=expiry, spot=spot, band=band, strikes=strikes
    )
