"""The mode guard is the one piece of this repo where a bug costs real money."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from olv.common.mode import (
    ENV_LIVE_ACK,
    ENV_LIVE_BASE_URL,
    ENV_MODE,
    LIVE_ACK_PHRASE,
    ExecutionMode,
    ModeGuardError,
    mode_from_env,
    require_paper,
    resolve_endpoint,
)

SRC = Path(__file__).resolve().parents[1] / "src"

#: Production hostnames that must never appear in the source tree. The live
#: base URL is supplied through the environment precisely so that this stays
#: true and the committed code cannot reach production.
FORBIDDEN_HOST_PATTERNS = [
    re.compile(r"(?<!paper-)api\.alpaca\.markets"),
    re.compile(r"(?<!sandbox\.)api\.tradier\.com"),
]


class TestModeFromEnv:
    def test_defaults_to_paper_when_unset(self):
        assert mode_from_env({}) is ExecutionMode.PAPER

    def test_reads_explicit_paper_and_live(self):
        assert mode_from_env({ENV_MODE: "paper"}) is ExecutionMode.PAPER
        assert mode_from_env({ENV_MODE: "live"}) is ExecutionMode.LIVE

    def test_is_case_and_whitespace_tolerant(self):
        assert mode_from_env({ENV_MODE: "  LIVE  "}) is ExecutionMode.LIVE

    @pytest.mark.parametrize("value", ["liv", "LIVE_", "true", "1", "production", ""])
    def test_unrecognised_value_raises_rather_than_defaulting(self, value):
        """A typo must not silently select paper *or* live."""
        with pytest.raises(ModeGuardError, match="not a valid mode"):
            mode_from_env({ENV_MODE: value})


class TestPaperResolution:
    def test_resolves_known_paper_broker(self):
        endpoint = resolve_endpoint("alpaca", ExecutionMode.PAPER, env={})
        assert endpoint.mode is ExecutionMode.PAPER
        assert "paper" in endpoint.base_url

    def test_unknown_broker_raises(self):
        with pytest.raises(ModeGuardError, match="no paper endpoint known"):
            resolve_endpoint("nonesuch", ExecutionMode.PAPER, env={})

    def test_live_looking_key_in_paper_mode_is_refused(self):
        with pytest.raises(ModeGuardError, match="live.*key pattern"):
            resolve_endpoint("alpaca", ExecutionMode.PAPER, api_key="AKDEADBEEF123", env={})

    def test_paper_key_is_accepted(self):
        endpoint = resolve_endpoint("alpaca", ExecutionMode.PAPER, api_key="PKDEADBEEF123", env={})
        assert endpoint.mode is ExecutionMode.PAPER

    def test_banner_names_the_mode(self):
        assert "PAPER MODE" in resolve_endpoint("alpaca", ExecutionMode.PAPER, env={}).banner()


class TestLiveResolution:
    """Live requires three separate deliberate acts, none of which have defaults."""

    LIVE_URL = "https://broker.example.com"

    def test_refused_without_acknowledgement(self):
        with pytest.raises(ModeGuardError, match=ENV_LIVE_ACK):
            resolve_endpoint("alpaca", ExecutionMode.LIVE, env={ENV_LIVE_BASE_URL: self.LIVE_URL})

    def test_refused_with_wrong_acknowledgement(self):
        env = {ENV_LIVE_BASE_URL: self.LIVE_URL, ENV_LIVE_ACK: "yes"}
        with pytest.raises(ModeGuardError, match=ENV_LIVE_ACK):
            resolve_endpoint("alpaca", ExecutionMode.LIVE, env=env)

    def test_refused_without_base_url(self):
        with pytest.raises(ModeGuardError, match=ENV_LIVE_BASE_URL):
            resolve_endpoint("alpaca", ExecutionMode.LIVE, env={ENV_LIVE_ACK: LIVE_ACK_PHRASE})

    def test_refused_for_non_https(self):
        env = {ENV_LIVE_ACK: LIVE_ACK_PHRASE, ENV_LIVE_BASE_URL: "http://broker.example.com"}
        with pytest.raises(ModeGuardError, match="https"):
            resolve_endpoint("alpaca", ExecutionMode.LIVE, env=env)

    def test_refused_when_given_a_paper_host(self):
        """Incoherent configuration fails rather than being guessed at."""
        env = {ENV_LIVE_ACK: LIVE_ACK_PHRASE, ENV_LIVE_BASE_URL: "https://paper-api.example.com"}
        with pytest.raises(ModeGuardError, match="paper-looking"):
            resolve_endpoint("alpaca", ExecutionMode.LIVE, env=env)

    def test_succeeds_only_with_everything_supplied(self):
        env = {ENV_LIVE_ACK: LIVE_ACK_PHRASE, ENV_LIVE_BASE_URL: self.LIVE_URL}
        endpoint = resolve_endpoint("alpaca", ExecutionMode.LIVE, env=env)
        assert endpoint.mode is ExecutionMode.LIVE
        assert "REAL MONEY" in endpoint.banner()

    def test_require_paper_rejects_a_live_endpoint(self):
        env = {ENV_LIVE_ACK: LIVE_ACK_PHRASE, ENV_LIVE_BASE_URL: self.LIVE_URL}
        endpoint = resolve_endpoint("alpaca", ExecutionMode.LIVE, env=env)
        with pytest.raises(ModeGuardError, match="paper-only"):
            require_paper(endpoint)


class TestSourceTreeContainsNoProductionHosts:
    """The structural guard: production is unreachable from the committed code.

    Every other check is a runtime assertion that a determined mistake could
    route around. This one is a property of the repository itself.
    """

    def test_no_production_hostname_in_src(self):
        offenders = []
        for path in SRC.rglob("*.py"):
            text = path.read_text()
            for pattern in FORBIDDEN_HOST_PATTERNS:
                if pattern.search(text):
                    offenders.append(f"{path.relative_to(SRC)}: {pattern.pattern}")
        assert not offenders, "production hostnames must never be committed: " + "; ".join(
            offenders
        )

    def test_the_guard_itself_would_catch_a_violation(self):
        """A canary, so the test above cannot rot into vacuous truth."""
        assert FORBIDDEN_HOST_PATTERNS[0].search("https://api.alpaca.markets/v2")
        assert not FORBIDDEN_HOST_PATTERNS[0].search("https://paper-api.alpaca.markets/v2")
