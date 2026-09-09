"""Wire and storage types for the recorded chain.

Every record carries ``feed_source``. Synthetic quotes exist so the pipeline
can be exercised without a vendor account, and the one thing that must never
happen is fabricated data being mistaken for observed data in a later
analysis. Tagging at the source and persisting the tag into Parquet makes that
structurally impossible rather than a matter of remembering — the same
discipline as the ``confidence: directional_only`` marker in backtest-lab.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

SYNTHETIC_SOURCE = "synthetic"


class Right(StrEnum):
    CALL = "C"
    PUT = "P"


class RejectReason(StrEnum):
    """Why a quote never reached the topic. See docs/IMPLEMENTATION_PLAN.md section 9.

    Rejections are counted and published rather than dropped: at 0DTE the wings
    go untradeable for stretches of the session, and how often a strategy
    *could not have traded* is a first-class result.
    """

    CROSSED = "crossed"  # bid > ask
    LOCKED = "locked"  # bid == ask
    ZERO_BID = "zero_bid"  # no bid: cannot be sold, common in the wings
    MISSING = "missing"  # absent bid or ask
    NON_FINITE = "non_finite"  # NaN/inf from the vendor
    NEGATIVE = "negative"  # negative price or size
    STALE = "stale"  # quote older than the freshness budget
    UNDERLYING_SKEW = "underlying_skew"  # spot and option observed too far apart
    ABSURD_WIDTH = "absurd_width"  # spread implausible even for a 0DTE wing
    WRONG_EXPIRY = "wrong_expiry"  # vendor returned a contract outside the watch set


def occ_symbol(underlying: str, expiry: dt.date, right: Right, strike: float) -> str:
    """Build an OCC-style contract symbol, e.g. ``QQQ260909C00500000``.

    The OCC standard pads the root to six characters with spaces; broker APIs
    almost universally use the unpadded form, which is what this produces.
    """
    strike_thousandths = int(round(strike * 1000))
    if strike_thousandths <= 0:
        raise ValueError(f"strike must be positive, got {strike}")
    return f"{underlying.upper()}{expiry:%y%m%d}{right.value}{strike_thousandths:08d}"


def parse_occ_symbol(symbol: str) -> tuple[str, dt.date, Right, float]:
    """Inverse of :func:`occ_symbol`. Raises on anything malformed."""
    if len(symbol) < 16:
        raise ValueError(f"not an OCC symbol: {symbol!r}")
    root, rest = symbol[:-15], symbol[-15:]
    if not root:
        raise ValueError(f"missing underlying root: {symbol!r}")
    try:
        expiry = dt.datetime.strptime(rest[:6], "%y%m%d").date()
        right = Right(rest[6])
        strike = int(rest[7:]) / 1000
    except (ValueError, KeyError) as exc:
        raise ValueError(f"not an OCC symbol: {symbol!r}") from exc
    return root, expiry, right, strike


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """One contract's top-of-book at one instant, as observed."""

    symbol: str
    underlying: str
    expiry: dt.date
    strike: float
    right: Right
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    quote_ts: dt.datetime
    feed_source: str
    open_interest: int | None = None
    volume: int | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["right"] = self.right.value
        row["mid"] = self.mid
        row["spread"] = self.spread
        return row


@dataclass(frozen=True, slots=True)
class RejectedQuote:
    """A quote the gate refused, kept with its reason."""

    symbol: str
    reason: RejectReason
    bid: float | None
    ask: float | None
    quote_ts: dt.datetime | None
    observed_at: dt.datetime
    feed_source: str

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["reason"] = self.reason.value
        return row


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    """A whole-chain observation at one instant.

    This is the unit published to ``chain.<ticker>``, and the reason that topic
    has exactly one partition: a strategy decision needs the chain coherent,
    not reassembled from interleaved per-contract updates.
    """

    underlying: str
    expiry: dt.date
    underlying_price: float
    underlying_ts: dt.datetime
    observed_at: dt.datetime
    quotes: tuple[OptionQuote, ...]
    rejected: tuple[RejectedQuote, ...]
    feed_source: str

    @property
    def session_date(self) -> dt.date:
        return self.observed_at.date()

    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.rejected:
            counts[item.reason.value] = counts.get(item.reason.value, 0) + 1
        return counts
