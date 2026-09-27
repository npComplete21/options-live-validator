"""The 0DTE tournament, as files.

The *vocabulary* is shared — every rule and selector here is defined in
``obl.strategy``, pinned by tag, so a strategy means the same thing in both
repos. The *strategies* are this repo's, because ``options-backtest-lab``
cannot backtest 0DTE (plan section 0) and has no use for them.

That split is the point of section 4: one DSL, contributed upstream, rather
than a fork that leaves the live and backtested strategies only *claimed* to be
the same.

Strategies are files, so registration is a directory listing.
"""

from __future__ import annotations

import functools
from pathlib import Path

from obl.strategy.spec import StrategySpec

LIBRARY = Path(__file__).parent / "library"


class UnknownStrategyError(LookupError):
    """No strategy by that name. Fatal rather than skipped: a tournament
    quietly running four of its five members is not the experiment."""


@functools.cache
def _index() -> dict[str, Path]:
    return {p.stem: p for p in sorted(LIBRARY.glob("*.yaml"))}


def all_names() -> list[str]:
    return sorted(_index())


def get(name: str) -> StrategySpec:
    try:
        return StrategySpec.from_yaml(_index()[name])
    except KeyError:
        raise UnknownStrategyError(f"unknown strategy {name!r}; known: {all_names()}") from None


def tournament() -> dict[str, StrategySpec]:
    """Every strategy in the library, loaded.

    Loading them together is deliberate: a malformed member should fail the
    whole tournament at startup rather than silently reduce the field, since a
    comparison is only meaningful across the set it claimed to run.
    """
    return {name: get(name) for name in all_names()}
