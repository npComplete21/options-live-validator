"""The tau clock, which lives in ``obl.timebase``. See plan section 3.

**This repo no longer owns a clock.** It briefly did, and that was the bug:
options-backtest-lab had one too, and section 3 requires the two be identical
or no live-vs-backtest comparison means anything. The implementations were
merged into the shared module, which this repo pins by tag.

What stays here is the half that cannot move: ``SessionCalendar``, this repo's
implementation of the shared clock's ``SessionSource`` protocol, and a
conformance test asserting that the shared clock *driven by our real exchange
calendar* still reproduces the section 3 numbers. The shared package pins the
same ratio against synthetic sessions; this pins it against
``pandas_market_calendars``, so a calendar change that silently moved tau would
fail here rather than in a residual nobody can explain.
"""

from __future__ import annotations

import datetime as dt

import pytest
from obl.timebase import (
    TRADING_SECONDS_PER_YEAR,
    CalendarClock,
    SessionSource,
    TradingHoursClock,
    clock_ratio,
)

from olv.common.sessions import MARKET_TZ, SessionCalendar

#: An ordinary Tuesday: no holiday, no early close.
NORMAL_SESSION_DAY = dt.date(2025, 6, 10)


@pytest.fixture(scope="module")
def calendar() -> SessionCalendar:
    return SessionCalendar()


@pytest.fixture(scope="module")
def clocks(calendar):
    return CalendarClock(), TradingHoursClock(calendar)


def at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=MARKET_TZ)


class TestSessionCalendar:
    def test_normal_session_is_six_and_a_half_hours(self, calendar):
        session = calendar.session_on(NORMAL_SESSION_DAY)
        assert session is not None
        assert session.seconds == 6.5 * 3600

    def test_weekend_is_not_a_trading_day(self, calendar):
        assert not calendar.is_trading_day(dt.date(2025, 6, 7))  # Saturday

    def test_expiry_on_a_closed_day_raises(self, calendar):
        """A contract cannot expire on a day the market never opened.

        Silently defaulting to 16:00 would corrupt every tau derived from it.
        """
        with pytest.raises(ValueError, match="not a trading day"):
            calendar.expiry_instant(dt.date(2025, 6, 7))

    def test_early_closes_are_honoured(self, calendar):
        """Half-days shorten tau; assuming 16:00 would overstate it ~2x."""
        sessions = calendar.sessions_between(dt.date(2025, 1, 1), dt.date(2025, 12, 31))
        early = [s for s in sessions if s.seconds < 6.5 * 3600]
        assert early, "2025 should contain at least one early close"
        for session in early:
            assert calendar.expiry_instant(session.date) == session.close


class TestZeroDteClockDivergence:
    """The section 3 finding, as an executable assertion."""

    def test_calendar_and_trading_tau_at_0945(self, clocks):
        calendar_clock, trading_clock = clocks
        now = at(NORMAL_SESSION_DAY, 9, 45)
        expiry = at(NORMAL_SESSION_DAY, 16, 0)

        assert calendar_clock.year_fraction(now, expiry) == pytest.approx(0.000713, rel=1e-3)
        assert trading_clock.year_fraction(now, expiry) == pytest.approx(0.003815, rel=1e-3)

    def test_sigma_root_tau_differs_by_2_31x(self, clocks):
        calendar_clock, trading_clock = clocks
        now = at(NORMAL_SESSION_DAY, 9, 45)
        expiry = at(NORMAL_SESSION_DAY, 16, 0)

        ratio = clock_ratio(now, expiry, trading_clock, calendar_clock)
        assert ratio == pytest.approx(2.31, abs=0.01)


class TestTradingHoursClock:
    def test_full_session_is_one_252nd_of_a_year(self, clocks):
        _, trading_clock = clocks
        tau = trading_clock.year_fraction(at(NORMAL_SESSION_DAY, 9, 30), at(NORMAL_SESSION_DAY, 16))
        assert tau == pytest.approx(1 / 252, rel=1e-9)

    def test_overnight_contributes_nothing(self, clocks):
        """16:00 Tuesday to 09:30 Wednesday is zero trading time."""
        _, trading_clock = clocks
        start = at(NORMAL_SESSION_DAY, 16, 0)
        end = at(NORMAL_SESSION_DAY + dt.timedelta(days=1), 9, 30)
        assert trading_clock.trading_seconds(start, end) == 0.0

    def test_weekend_is_skipped(self, clocks):
        """Friday close to Monday open spans three calendar days and no trading time."""
        _, trading_clock = clocks
        friday = dt.date(2025, 6, 6)
        monday = dt.date(2025, 6, 9)
        assert trading_clock.trading_seconds(at(friday, 16, 0), at(monday, 9, 30)) == 0.0

    def test_calendar_clock_would_have_charged_for_that_weekend(self, clocks):
        """The contrast that makes the trading clock worth having."""
        calendar_clock, _ = clocks
        friday, monday = dt.date(2025, 6, 6), dt.date(2025, 6, 9)
        assert calendar_clock.year_fraction(at(friday, 16, 0), at(monday, 9, 30)) > 0

    def test_multi_day_tau_accumulates_whole_sessions(self, clocks):
        _, trading_clock = clocks
        start = at(dt.date(2025, 6, 9), 9, 30)
        end = at(dt.date(2025, 6, 11), 16, 0)
        expected = 3 * 6.5 * 3600 / TRADING_SECONDS_PER_YEAR
        assert trading_clock.year_fraction(start, end) == pytest.approx(expected, rel=1e-9)


class TestClockInputValidation:
    def test_naive_datetimes_are_rejected(self, clocks):
        calendar_clock, _ = clocks
        naive = dt.datetime(2025, 6, 10, 9, 45)
        with pytest.raises(ValueError, match="timezone-aware"):
            calendar_clock.year_fraction(naive, naive + dt.timedelta(hours=1))

    def test_expired_option_is_zero_tau_not_an_error(self, clocks):
        """A deliberate loosening when the clocks merged.

        This repo's clock used to raise on an expiry in the past. The shared one
        returns zero, because a tau clock is a pure function of two instants and
        backtest-lab needs zero for contracts that have rolled off a chain.

        The guard did not disappear, it moved: an expired contract reaching a
        live decision is a selection bug, caught where contracts are chosen, and
        anything that would divide by the resulting zero is still refused by
        ``clock_ratio``.
        """
        calendar_clock, _ = clocks
        now = at(NORMAL_SESSION_DAY, 15, 0)
        assert calendar_clock.year_fraction(now, at(NORMAL_SESSION_DAY, 14, 0)) == 0.0

    def test_ratio_refuses_a_degenerate_denominator(self, clocks):
        """After the close, trading tau is zero; dividing by it must not pass silently."""
        calendar_clock, trading_clock = clocks
        start = at(NORMAL_SESSION_DAY, 16, 0)
        end = at(NORMAL_SESSION_DAY + dt.timedelta(days=1), 9, 0)
        with pytest.raises(ValueError, match="degenerate"):
            clock_ratio(start, end, calendar_clock, trading_clock)


class TestSessionSourceContract:
    """The seam between the two repos: the shared clock is pure-stdlib and takes
    sessions injected, so this repo's calendar has to satisfy its protocol."""

    def test_our_calendar_is_a_session_source(self, calendar):
        assert isinstance(calendar, SessionSource)

    def test_sessions_carry_real_close_instants(self, calendar):
        """The shared Session type is the one the clock consumes; if our
        calendar returned something merely similar, tau would be computed from
        the wrong close on every early-close day."""
        session = calendar.session_on(NORMAL_SESSION_DAY)
        assert session is not None
        assert session.close == calendar.expiry_instant(NORMAL_SESSION_DAY)
        assert session.overlap_seconds(session.open, session.close) == session.seconds
