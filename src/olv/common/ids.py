"""Run identifiers and idempotency keys.

Deliberately parallel to options-backtest-lab's ``run_id`` scheme (its section
7) so a live run and the backtest run it corresponds to are recognisably the
same parameter set. The shared piece is the *canonical parameter hash*: if the
two repos hash parameters differently, the identifiers stop being comparable
and the link between a live run and its backtest becomes a claim rather than a
fact.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping
from typing import Any

PARAM_HASH_LENGTH = 8
LIVE_SUFFIX = "live"


def canonical_json(params: Mapping[str, Any]) -> str:
    """Serialise a parameter set so equal configurations produce equal bytes.

    Sorted keys, no insignificant whitespace, and no NaN/Infinity — which JSON
    does not define and which would make a hash unstable across readers.
    """
    return json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def param_hash(params: Mapping[str, Any], length: int = PARAM_HASH_LENGTH) -> str:
    """Short stable digest of a parameter set."""
    digest = hashlib.sha256(canonical_json(params).encode()).hexdigest()
    return digest[:length]


def validation_run_id(
    strategy: str,
    instrument: str,
    params: Mapping[str, Any],
    when: dt.datetime,
) -> str:
    """``<strategy>-<instrument>-<UTC timestamp>-<param hash>-live``.

    The timestamp keeps runs sortable; the hash makes identical configurations
    detectable and makes two runs that differ only in parameters impossible to
    confuse.
    """
    if when.tzinfo is None:
        raise ValueError("run timestamps must be timezone-aware")
    stamp = when.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{strategy}-{instrument}-{stamp}-{param_hash(params)}-{LIVE_SUFFIX}"


def idempotency_key(run_id: str, decision_at: dt.datetime, leg_id: str) -> str:
    """Deterministic key making a duplicate order submission a no-op.

    The state store's transactional offset write (section 12) protects internal
    state against at-least-once redelivery; this protects the *external* side
    effect. Both are needed — a transaction cannot roll back an order the
    broker already accepted.
    """
    if decision_at.tzinfo is None:
        raise ValueError("decision timestamps must be timezone-aware")
    stamp = decision_at.astimezone(dt.UTC).isoformat(timespec="microseconds")
    return hashlib.sha256(f"{run_id}|{stamp}|{leg_id}".encode()).hexdigest()[:32]
