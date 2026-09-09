"""Execution mode and the guards that keep this repo away from real money.

CLAUDE.md requires paper vs. live to be "an explicit, impossible-to-miss config
flag, defaulting to paper", and any code path touching a live key to be flagged
loudly. The design here, per docs/IMPLEMENTATION_PLAN.md (v1 section 4):

1.  An enum, never a bool. ``live=False`` inverts to disaster with one typo'd
    env var; ``MODE=live`` is at least unmistakable in a log line.
2.  The base URL is derived *from* the mode and nowhere else. The usual way
    this goes wrong is an SDK defaulting to a production host while the
    "paper" flag lives somewhere else entirely.
3.  **No production hostname appears in this repo.** The paper host is a
    literal; the live host has to be supplied through the environment and has
    no default. So the code as committed cannot reach production even if every
    other guard is subverted. ``tests/test_mode.py`` asserts this by grepping
    the source tree, which is why it must stay true.
4.  LIVE additionally requires an explicit acknowledgement variable and reads
    credentials from a different secret path than paper.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum

# Environment variables. Named verbosely on purpose: these should be
# recognisable in a shell history or a process listing.
ENV_MODE = "OLV_EXECUTION_MODE"
ENV_LIVE_BASE_URL = "OLV_LIVE_BROKER_BASE_URL"
ENV_LIVE_ACK = "OLV_I_UNDERSTAND_THIS_IS_REAL_MONEY"

LIVE_ACK_PHRASE = "yes-i-accept-real-financial-risk"

#: Paper endpoints are safe to hardcode: reaching one cannot lose money.
PAPER_BASE_URLS: dict[str, str] = {
    "alpaca": "https://paper-api.alpaca.markets",
    "tradier": "https://sandbox.tradier.com",
}

#: Key prefixes that indicate a *live* credential, per broker. Used to refuse
#: startup when a live-looking key is handed to paper mode.
#:
#: These follow each broker's documented convention (Alpaca live keys begin
#: "AK", paper keys "PK"). Verify against the broker's current docs when the
#: trial account is opened in Phase 5 — a stale prefix here fails open, which
#: is why it is not the only guard.
LIVE_KEY_PATTERNS: dict[str, re.Pattern[str]] = {
    "alpaca": re.compile(r"^AK[A-Z0-9]+$"),
}


class ExecutionMode(StrEnum):
    """How orders leave this process. There is deliberately no third value."""

    PAPER = "paper"
    LIVE = "live"


class ModeGuardError(RuntimeError):
    """Raised when the requested mode is unsafe or incoherently configured.

    Always fatal at startup. Never caught and downgraded.
    """


@dataclass(frozen=True)
class BrokerEndpoint:
    """A resolved broker target. Only :func:`resolve_endpoint` may build one."""

    broker: str
    mode: ExecutionMode
    base_url: str

    def banner(self) -> str:
        """One loud line for the startup log and every archived event."""
        rule = "=" * 72
        if self.mode is ExecutionMode.LIVE:
            return (
                f"\n{rule}\n"
                f"  *** LIVE TRADING — REAL MONEY — {self.broker.upper()} ***\n"
                f"  {self.base_url}\n"
                f"{rule}\n"
            )
        return (
            f"\n{rule}\n  PAPER MODE — no real orders — {self.broker} @ {self.base_url}\n{rule}\n"
        )


def mode_from_env(env: dict[str, str] | None = None) -> ExecutionMode:
    """Read the mode from the environment, defaulting to PAPER.

    An unset variable is PAPER. An unrecognised value is an error rather than a
    silent fallback: ``OLV_EXECUTION_MODE=LIVE_``, or a typo'd ``liv``, must
    not quietly select paper *or* live.
    """
    env = os.environ if env is None else env
    raw = env.get(ENV_MODE, ExecutionMode.PAPER.value).strip().lower()
    try:
        return ExecutionMode(raw)
    except ValueError as exc:
        valid = ", ".join(m.value for m in ExecutionMode)
        raise ModeGuardError(
            f"{ENV_MODE}={raw!r} is not a valid mode (expected one of: {valid})"
        ) from exc


def resolve_endpoint(
    broker: str,
    mode: ExecutionMode,
    *,
    api_key: str | None = None,
    env: dict[str, str] | None = None,
) -> BrokerEndpoint:
    """Derive the broker base URL from the mode, checking every guard.

    This is the only function in the repo permitted to produce a base URL.
    Anything else constructing one bypasses these checks.
    """
    env = os.environ if env is None else env
    broker = broker.strip().lower()

    if mode is ExecutionMode.PAPER:
        base_url = PAPER_BASE_URLS.get(broker)
        if base_url is None:
            known = ", ".join(sorted(PAPER_BASE_URLS))
            raise ModeGuardError(f"no paper endpoint known for broker {broker!r} (known: {known})")
        _assert_paper_host(broker, base_url)
        if api_key is not None:
            _assert_not_live_key(broker, api_key)
        return BrokerEndpoint(broker=broker, mode=mode, base_url=base_url)

    return _resolve_live_endpoint(broker, env)


def _resolve_live_endpoint(broker: str, env: dict[str, str]) -> BrokerEndpoint:
    """LIVE requires three separate, deliberate acts. None of them have defaults."""
    ack = env.get(ENV_LIVE_ACK, "").strip()
    if ack != LIVE_ACK_PHRASE:
        raise ModeGuardError(
            f"live mode requires {ENV_LIVE_ACK}={LIVE_ACK_PHRASE!r}; "
            "refusing to place real orders without it"
        )

    base_url = env.get(ENV_LIVE_BASE_URL, "").strip()
    if not base_url:
        raise ModeGuardError(
            f"live mode requires {ENV_LIVE_BASE_URL}; no production hostname is "
            "committed to this repository, by design"
        )
    if not base_url.startswith("https://"):
        raise ModeGuardError(f"live base URL must be https, got {base_url!r}")
    if _looks_like_paper_host(base_url):
        raise ModeGuardError(
            f"live mode was given a paper-looking host ({base_url!r}); "
            "this is incoherent, so refusing rather than guessing"
        )
    return BrokerEndpoint(broker=broker, mode=ExecutionMode.LIVE, base_url=base_url)


def _looks_like_paper_host(base_url: str) -> bool:
    lowered = base_url.lower()
    return any(token in lowered for token in ("paper", "sandbox", "localhost", "127.0.0.1"))


def _assert_paper_host(broker: str, base_url: str) -> None:
    if not _looks_like_paper_host(base_url):
        raise ModeGuardError(
            f"paper mode resolved to {base_url!r} for {broker!r}, which does not look "
            "like a paper or sandbox host; refusing to start"
        )


def _assert_not_live_key(broker: str, api_key: str) -> None:
    pattern = LIVE_KEY_PATTERNS.get(broker)
    if pattern is not None and pattern.match(api_key.strip()):
        raise ModeGuardError(
            f"the API key supplied for {broker!r} matches that broker's *live* key "
            "pattern while running in paper mode; refusing to start"
        )


def require_paper(endpoint: BrokerEndpoint) -> BrokerEndpoint:
    """Assert an endpoint is paper. Call at every order-submitting boundary.

    Phases 0-4 place no orders at all; Phase 5 adds the only module allowed to,
    and it calls this immediately before every submission.
    """
    if endpoint.mode is not ExecutionMode.PAPER:
        raise ModeGuardError(
            f"this code path is paper-only but the endpoint is {endpoint.mode.value}"
        )
    return endpoint
