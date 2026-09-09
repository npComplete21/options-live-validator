from __future__ import annotations

import datetime as dt

import pytest

from olv.common.models import Right, occ_symbol, parse_occ_symbol

EXPIRY = dt.date(2026, 9, 9)


class TestOccSymbol:
    def test_shape(self):
        assert occ_symbol("QQQ", EXPIRY, Right.CALL, 500.0) == "QQQ260909C00500000"

    def test_fractional_strike(self):
        assert occ_symbol("QQQ", EXPIRY, Right.PUT, 487.5) == "QQQ260909P00487500"

    def test_round_trips(self):
        for strike in (1.0, 99.5, 487.5, 500.0, 1234.25):
            for right in (Right.CALL, Right.PUT):
                symbol = occ_symbol("QQQ", EXPIRY, right, strike)
                assert parse_occ_symbol(symbol) == ("QQQ", EXPIRY, right, strike)

    def test_non_positive_strike_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            occ_symbol("QQQ", EXPIRY, Right.CALL, 0.0)

    @pytest.mark.parametrize("bad", ["", "QQQ", "QQQ260909X00500000", "260909C00500000"])
    def test_malformed_symbols_rejected(self, bad):
        with pytest.raises(ValueError):
            parse_occ_symbol(bad)
