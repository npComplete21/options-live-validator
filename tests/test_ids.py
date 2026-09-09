from __future__ import annotations

import datetime as dt

import pytest

from olv.common.ids import canonical_json, idempotency_key, param_hash, validation_run_id

WHEN = dt.datetime(2026, 9, 8, 14, 30, tzinfo=dt.UTC)


class TestCanonicalHashing:
    def test_key_order_does_not_change_the_hash(self):
        """Two configs differing only in dict ordering are the same config."""
        assert param_hash({"a": 1, "b": 2}) == param_hash({"b": 2, "a": 1})

    def test_different_values_change_the_hash(self):
        assert param_hash({"delta": 0.10}) != param_hash({"delta": 0.16})

    def test_nan_is_rejected(self):
        """JSON has no NaN; allowing it would make the hash reader-dependent."""
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})

    def test_hash_is_stable_across_runs(self):
        assert param_hash({"short_put_delta": 0.10, "short_call_delta": 0.10}) == param_hash(
            {"short_call_delta": 0.10, "short_put_delta": 0.10}
        )


class TestValidationRunId:
    def test_shape(self):
        run_id = validation_run_id("short_strangle_0dte", "QQQ", {"d": 0.1}, WHEN)
        assert run_id.startswith("short_strangle_0dte-QQQ-20260908T143000Z-")
        assert run_id.endswith("-live")

    def test_differing_params_produce_different_ids(self):
        a = validation_run_id("s", "QQQ", {"d": 0.10}, WHEN)
        b = validation_run_id("s", "QQQ", {"d": 0.16}, WHEN)
        assert a != b

    def test_naive_timestamp_is_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            validation_run_id("s", "QQQ", {}, dt.datetime(2026, 9, 8, 14, 30))


class TestIdempotencyKey:
    def test_same_decision_yields_same_key(self):
        """This is what makes a duplicate submission a no-op at the broker."""
        assert idempotency_key("run-1", WHEN, "short_put") == idempotency_key(
            "run-1", WHEN, "short_put"
        )

    def test_each_leg_gets_its_own_key(self):
        assert idempotency_key("run-1", WHEN, "short_put") != idempotency_key(
            "run-1", WHEN, "short_call"
        )

    def test_a_later_decision_is_a_different_order(self):
        later = WHEN + dt.timedelta(seconds=1)
        assert idempotency_key("run-1", WHEN, "leg") != idempotency_key("run-1", later, "leg")

    def test_runs_do_not_collide(self):
        assert idempotency_key("run-1", WHEN, "leg") != idempotency_key("run-2", WHEN, "leg")
