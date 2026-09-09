from __future__ import annotations

import datetime as dt

import pytest

from olv.common.models import Right, parse_occ_symbol
from olv.feed.universe import build_watch_set, log_moneyness, select_strikes, zero_dte_expiry

TRADING_DAY = dt.date(2025, 6, 10)
WEEKEND = dt.date(2025, 6, 7)


def grid(lo: float, hi: float, step: float = 1.0) -> tuple[float, ...]:
    n = int(round((hi - lo) / step)) + 1
    return tuple(round(lo + i * step, 2) for i in range(n))


class TestZeroDteExpiry:
    def test_trading_day_expires_same_day(self):
        assert zero_dte_expiry(TRADING_DAY) == TRADING_DAY

    def test_closed_day_raises_rather_than_rolling(self):
        """Rolling forward would quietly make a '0DTE' strategy trade 1DTE."""
        with pytest.raises(ValueError, match="not a trading day"):
            zero_dte_expiry(WEEKEND)


class TestStrikeSelection:
    def test_band_is_symmetric_in_log_moneyness(self):
        strikes = select_strikes(500.0, grid(400, 600), band=0.05)
        assert min(strikes) >= 500 * 0.95
        assert max(strikes) <= 500 * 1.06

    def test_wider_band_selects_more(self):
        available = grid(400, 600)
        assert len(select_strikes(500.0, available, band=0.10)) > len(
            select_strikes(500.0, available, band=0.05)
        )

    def test_only_listed_strikes_are_returned(self):
        """A generated grid would request contracts that do not exist."""
        listed = (495.0, 497.5, 500.0, 502.5, 505.0)
        assert set(select_strikes(500.0, listed, band=0.05)) <= set(listed)

    def test_result_is_sorted(self):
        strikes = select_strikes(500.0, (505.0, 495.0, 500.0), band=0.05)
        assert list(strikes) == sorted(strikes)

    def test_non_positive_band_rejected(self):
        with pytest.raises(ValueError, match="band must be positive"):
            select_strikes(500.0, grid(490, 510), band=0.0)

    def test_log_moneyness_rejects_non_positive(self):
        with pytest.raises(ValueError, match="positive"):
            log_moneyness(100.0, 0.0)


class TestWatchSet:
    def test_covers_both_rights_for_every_strike(self):
        ws = build_watch_set("QQQ", TRADING_DAY, 500.0, grid(400, 600), band=0.02)
        assert len(ws.symbols) == len(ws.strikes) * 2
        rights = {parse_occ_symbol(s)[2] for s in ws.symbols}
        assert rights == {Right.CALL, Right.PUT}

    def test_symbols_carry_the_requested_expiry(self):
        ws = build_watch_set("QQQ", TRADING_DAY, 500.0, grid(490, 510), band=0.05)
        assert all(parse_occ_symbol(s)[1] == TRADING_DAY for s in ws.symbols)

    def test_ticker_is_normalised(self):
        assert build_watch_set("qqq", TRADING_DAY, 500.0, grid(490, 510)).underlying == "QQQ"

    def test_empty_band_raises_rather_than_recording_nothing(self):
        with pytest.raises(ValueError, match="no listed strikes"):
            build_watch_set("QQQ", TRADING_DAY, 500.0, (100.0, 110.0), band=0.01)

    def test_band_size_is_plausible_for_0dte(self):
        """~100-200 contracts, per section 9's sizing."""
        ws = build_watch_set("QQQ", TRADING_DAY, 500.0, grid(400, 600), band=0.05)
        assert 60 <= len(ws) <= 300
