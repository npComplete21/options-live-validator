"""The feed boundary.

Everything upstream of this protocol is vendor-specific; everything downstream
is not. The recorder, the gate, the producer and the archiver are all written
against ``QuoteFeed``, so adding a broker means adding one class and changing
no pipeline code.

Greeks and implied vol are deliberately **not** part of this interface. Vendors
supply them, and taking them would import that vendor's clock, rate and
dividend conventions into our numbers — the tau clock alone moves
sigma*sqrt(tau) by 2.31x at 0DTE (section 3). They are computed at Phase 2 from
the recorded mid using backtest-lab's pricer.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

from olv.feed.gate import RawQuote
from olv.feed.universe import WatchSet


@runtime_checkable
class QuoteFeed(Protocol):
    """A source of option and underlying quotes."""

    #: Recorded on every row so fabricated data can never be silently mixed
    #: with observed data in a later analysis.
    source: str

    def underlying_quote(self, underlying: str) -> tuple[float, dt.datetime]:
        """Last price and its timestamp."""
        ...

    def listed_strikes(self, underlying: str, expiry: dt.date) -> tuple[float, ...]:
        """Strikes actually listed for that expiry."""
        ...

    def option_quotes(self, watch_set: WatchSet) -> tuple[RawQuote, ...]:
        """Top-of-book for every contract in the watch set, unvalidated."""
        ...
