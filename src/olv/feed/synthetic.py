"""A fake feed, for exercising the pipeline without a vendor account.

**This is a fixture generator, not a pricing model.** The extrinsic-value
shape below is a crude bump chosen to look plausible on a chart; it is not
arbitrage-free, it is not calibrated to anything, and no number it produces
may be used in any analysis. This repo's rule that no pricing math lives here
(section 14) is intact: nothing in this file claims to value an option.

The protection against misuse is structural rather than a warning. Every row
this feed produces is stamped ``feed_source="synthetic"`` all the way into
Parquet, so a recording made from it is self-identifying forever and cannot be
mistaken for an observed session.

What it is genuinely good for: driving the watch set, the hygiene gate, the
producer, Kafka and the archiver end to end, including the messy cases — stale
prints, crossed markets, absent bids in the wings — that a real feed produces
only occasionally and never on demand.
"""

from __future__ import annotations

import datetime as dt
import math
import random

from olv.common.models import SYNTHETIC_SOURCE, Right, occ_symbol
from olv.feed.gate import RawQuote
from olv.feed.universe import WatchSet


class SyntheticFeed:
    """Deterministic given a seed, so tests and replays are reproducible."""

    source = SYNTHETIC_SOURCE

    def __init__(
        self,
        spot: float = 500.0,
        *,
        seed: int = 0,
        strike_increment: float = 1.0,
        strike_range: float = 0.10,
        defect_rate: float = 0.0,
        now: dt.datetime | None = None,
    ) -> None:
        self.spot = spot
        self.strike_increment = strike_increment
        self.strike_range = strike_range
        #: Fraction of quotes deliberately malformed, so the gate has something
        #: to catch. Zero by default; raise it in tests.
        self.defect_rate = defect_rate
        self._rng = random.Random(seed)
        self._now = now
        self._drift_steps = 0

    # -- clock ---------------------------------------------------------------

    def _timestamp(self) -> dt.datetime:
        return self._now or dt.datetime.now(dt.UTC)

    def advance(self, seconds: float = 1.0, vol: float = 0.0002) -> None:
        """Move spot one step and, if pinned, the clock with it."""
        self._drift_steps += 1
        self.spot *= math.exp(self._rng.gauss(0.0, vol))
        if self._now is not None:
            self._now += dt.timedelta(seconds=seconds)

    # -- QuoteFeed -----------------------------------------------------------

    def underlying_quote(self, underlying: str) -> tuple[float, dt.datetime]:
        return round(self.spot, 2), self._timestamp()

    def listed_strikes(self, underlying: str, expiry: dt.date) -> tuple[float, ...]:
        lo = self.spot * (1 - self.strike_range)
        hi = self.spot * (1 + self.strike_range)
        step = self.strike_increment
        first = math.floor(lo / step) * step
        count = int((hi - first) / step) + 1
        return tuple(round(first + i * step, 2) for i in range(count))

    def option_quotes(self, watch_set: WatchSet) -> tuple[RawQuote, ...]:
        now = self._timestamp()
        quotes: list[RawQuote] = []
        for strike in watch_set.strikes:
            for right in (Right.CALL, Right.PUT):
                symbol = occ_symbol(watch_set.underlying, watch_set.expiry, right, strike)
                quotes.append(self._quote(symbol, strike, right, now))
        return tuple(quotes)

    # -- shape ---------------------------------------------------------------

    def _fake_value(self, strike: float, right: Right) -> float:
        """A plausibly-shaped number. Not a price. See the module docstring."""
        intrinsic = (
            max(self.spot - strike, 0.0) if right is Right.CALL else max(strike - self.spot, 0.0)
        )
        moneyness = (strike - self.spot) / self.spot
        # Scale chosen so a 5% wing lands near $0.05 rather than at zero -- the
        # penny-priced regime section 10 cares about, where the spread is
        # plausibly larger than the whole volatility premium. A tighter decay
        # makes the entire band worthless and gives the gate nothing to see.
        extrinsic = 2.5 * math.exp(-((moneyness / 0.025) ** 2))
        return round(intrinsic + extrinsic, 2)

    def _quote(self, symbol: str, strike: float, right: Right, now: dt.datetime) -> RawQuote:
        value = self._fake_value(strike, right)
        # Wings quote wider in relative terms, as they really do.
        half_spread = max(0.01, round(value * 0.05, 2))
        bid = max(0.0, round(value - half_spread, 2))
        ask = round(value + half_spread, 2)
        quote = RawQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            bid_size=self._rng.randint(1, 200),
            ask_size=self._rng.randint(1, 200),
            quote_ts=now,
            open_interest=self._rng.randint(0, 5000),
            volume=self._rng.randint(0, 2000),
        )
        if self.defect_rate and self._rng.random() < self.defect_rate:
            quote = self._corrupt(quote, now)
        return quote

    def _corrupt(self, quote: RawQuote, now: dt.datetime) -> RawQuote:
        """Introduce one of the defects a real feed produces."""
        from dataclasses import replace

        match self._rng.randrange(5):
            case 0:  # crossed
                return replace(quote, bid=quote.ask, ask=quote.bid)
            case 1:  # no bid, as the wings do
                return replace(quote, bid=0.0)
            case 2:  # stale print
                return replace(quote, quote_ts=now - dt.timedelta(minutes=2))
            case 3:  # vendor dropped a side
                return replace(quote, ask=None)
            case _:  # locked
                return replace(quote, ask=quote.bid)
