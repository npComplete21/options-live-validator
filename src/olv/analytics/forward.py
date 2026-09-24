"""The forward, measured from the chain rather than assumed.

Black-Scholes prices off a forward, not a spot. The usual route is
``F = S * exp((r - q) * tau)``, which imports two assumptions — a risk-free
rate and a dividend/borrow yield — into every implied vol we record.

At 0DTE the error that route introduces is small but not ignorable. With
``tau = 0.0038`` and ``r - q = 4%`` on a $500 underlying, ``F - S`` is about
7.6 cents: roughly a tick, and a large fraction of a wing quoted $0.05 bid.

Put-call parity gives the forward directly from quotes we already have::

    C - P = df * (F - K)      =>      F = K + (C - P) / df

Two things make this the better trade at 0DTE:

*   **The assumption that mattered is gone.** Using spot for the forward is
    wrong by ``S * (exp((r-q)*tau) - 1)``, the ~7.6 cents above. Parity removes
    it and replaces it with quote noise, which is measurable and is reported.
*   **The assumption that remains does not matter.** ``df`` is still needed, and
    is left at 1.0 by default. That is not a shrug: the term it scales is
    ``C - P``, which near the money is cents, so a df error of 1.5e-4 moves the
    forward by well under a hundredth of a cent. The 7.6-cent error lived in the
    ``S -> F`` step, not here.

What comes back is also a *result*, not just an input. ``basis`` (``F - S``) is
the market's own rate, dividend and borrow bundled together and observed rather
than assumed, and ``dispersion`` across strikes is a data-quality signal that
belongs in the surface report: a chain whose strikes disagree about the forward
is a chain whose implied vols should not be trusted that instant.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

#: Parity is solved where both sides are quotable, which in practice means near
#: the money. Far from it one side is mostly intrinsic and the other is nearly
#: worthless, so ``C - P`` is dominated by the spread rather than by the
#: forward, and including those strikes adds noise, not information.
DEFAULT_MAX_STRIKES = 8

#: At 0DTE the discount factor is within 1.5e-4 of one, and it multiplies a
#: quantity measured in cents. See the module docstring.
UNIT_DISCOUNT = 1.0


class NoParityPairError(ValueError):
    """No strike had both a call and a put with a usable mid."""


@dataclass(frozen=True, slots=True)
class ForwardEstimate:
    """A forward, and enough context to judge whether to believe it."""

    forward: float
    spot: float
    strikes_used: int
    dispersion: float
    method: str

    @property
    def basis(self) -> float:
        """``F - S`` — rate, dividend and borrow as the market actually prices
        them, rather than as we would have assumed them."""
        return self.forward - self.spot

    def __str__(self) -> str:
        return (
            f"F={self.forward:.4f} (spot {self.spot:.4f}, basis {self.basis:+.4f}) "
            f"via {self.method} on {self.strikes_used} strikes, dispersion {self.dispersion:.4f}"
        )


def parity_forward_per_strike(
    calls: dict[float, float],
    puts: dict[float, float],
    *,
    discount_factor: float = UNIT_DISCOUNT,
) -> dict[float, float]:
    """``F_k = K + (C - P) / df`` for every strike quoting both sides."""
    if discount_factor <= 0:
        raise ValueError(f"discount factor must be positive, got {discount_factor}")
    return {
        strike: strike + (calls[strike] - puts[strike]) / discount_factor
        for strike in sorted(calls.keys() & puts.keys())
    }


def implied_forward(
    calls: dict[float, float],
    puts: dict[float, float],
    spot: float,
    *,
    discount_factor: float = UNIT_DISCOUNT,
    max_strikes: int = DEFAULT_MAX_STRIKES,
    allow_spot_fallback: bool = True,
) -> ForwardEstimate:
    """Estimate the forward from put-call parity, nearest the money outward.

    The estimator is a **median**, not a mean. One crossed or stale quote moves
    a mean by its full error; the median ignores it. At 0DTE single bad prints
    are routine rather than exceptional, which is the whole reason the hygiene
    gate exists, and the forward feeds every implied vol in the snapshot — so it
    is the wrong place to be averaging in an outlier.

    ``allow_spot_fallback=False`` turns an unusable chain into an error instead
    of a silent downgrade to ``F = S``. Calibration work should set it: a
    forward that quietly became spot is exactly the 7.6-cent bias this module
    exists to remove, and it would not otherwise announce itself.
    """
    if max_strikes < 1:
        raise ValueError(f"max_strikes must be at least 1, got {max_strikes}")

    per_strike = parity_forward_per_strike(calls, puts, discount_factor=discount_factor)
    if not per_strike:
        if not allow_spot_fallback:
            raise NoParityPairError(
                "no strike quoted both a call and a put, so the forward cannot be "
                "measured. Refusing to fall back to spot: at 0DTE that is a "
                "silent bias of roughly a tick, which is a large fraction of a "
                "wing's premium."
            )
        return ForwardEstimate(
            forward=spot, spot=spot, strikes_used=0, dispersion=0.0, method="spot_fallback"
        )

    nearest = sorted(per_strike, key=lambda k: abs(k - spot))[:max_strikes]
    estimates = [per_strike[k] for k in nearest]

    return ForwardEstimate(
        forward=statistics.median(estimates),
        spot=spot,
        strikes_used=len(estimates),
        dispersion=statistics.stdev(estimates) if len(estimates) > 1 else 0.0,
        method="put_call_parity",
    )
