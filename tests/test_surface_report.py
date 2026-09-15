"""Report C. See docs/IMPLEMENTATION_PLAN.md section 13.

The report's job is as much to say what it *cannot* show as to show the rest.
Two of section 13's four requested items are unavailable — the intraday vol
curve needs a recorded session, and rejection rates are published but never
archived — and a report that rendered three quarters of itself without saying
so would read as complete.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from olv.analytics.surface import enrich, normalise_timestamps
from olv.reporting import surface as report_c
from tests.test_surface import DEFAULT_INSTANTS, archive_frame, at


@pytest.fixture
def report() -> report_c.SurfaceReport:
    raw = archive_frame(instants=(at(9, 45), at(12, 0), at(15, 0)))
    return report_c.build(enrich(raw), raw)


class TestWhatSection13Asked_For:
    def test_sigma_market_is_reported_by_strike_and_time(self, report):
        assert {"observed_at", "strike", "sigma_market"} <= set(report.sigma_surface.columns)
        assert report.sigma_surface["observed_at"].n_unique() == 3

    def test_companion_contracts_are_averaged_not_double_counted(self, report):
        """Calls and puts carry the same implied vol on an arbitrage-free chain,
        so listing both would present one number as two observations."""
        assert set(report.sigma_surface["contracts"].to_list()) == {2}

    def test_half_spread_is_reported_in_vol_points_by_moneyness(self, report):
        assert "median_vol_half_spread" in report.half_spread.columns
        labels = set(report.half_spread["moneyness_bucket"].cast(pl.String).to_list())
        assert labels <= set(report_c.MONEYNESS_LABELS)

    def test_buckets_are_named_not_numbered(self):
        """The wings and the body are where the spread behaves differently, and
        that comparison is the table's reason to exist.

        Needs strikes that actually reach a wing: the default fixture spans
        495-505 around a 500 forward, which is entirely inside |k| <= 0.02.
        """
        wide = archive_frame(strikes=(470.0, 490.0, 500.0, 510.0, 530.0))
        report = report_c.build(enrich(wide), wide)
        labels = report.half_spread["moneyness_bucket"].cast(pl.String).to_list()
        assert any("wing" in label for label in labels)
        assert any("at the money" in label for label in labels)


class TestWhatItRefusesToShow:
    def test_the_vol_curve_is_reported_as_blocked(self, report):
        (key,) = [k for k in report.blocked if "vol curve" in k]
        assert "measured input" in report.blocked[key]

    def test_rejection_rates_are_reported_as_blocked(self, report):
        (key,) = [k for k in report.blocked if "rejection" in k]
        assert "never reach Parquet" in report.blocked[key]

    def test_blocked_items_appear_in_the_rendered_report(self, report):
        rendered = report.render()
        assert "## Blocked" in rendered
        assert rendered.count("- **") >= 2


class TestSyntheticDataAnnouncesItself:
    def test_a_synthetic_report_is_labelled_on_its_face(self):
        """Every figure in a synthetic report is a property of a fixture
        generator rather than of a market. The banner is the structural
        protection against that being forgotten a month later."""
        raw = archive_frame().with_columns(pl.lit("synthetic").alias("feed_source"))
        rendered = report_c.build(enrich(raw), raw).render()
        assert "SYNTHETIC DATA - NOT A MARKET OBSERVATION" in rendered

    def test_an_observed_report_carries_no_banner(self):
        raw = archive_frame().with_columns(pl.lit("alpaca-paper").alias("feed_source"))
        report = report_c.build(enrich(raw), raw)
        assert not report.is_synthetic
        assert "SYNTHETIC DATA" not in report.render()

    def test_a_mixed_recording_is_treated_as_synthetic(self):
        """One fabricated row is enough to disqualify the whole report: the
        alternative is a reader having to check which rows were real."""
        raw = archive_frame(instants=(at(9, 45), at(12, 0)))
        raw = raw.with_columns(
            pl.when(pl.col("observed_at") == at(12, 0))
            .then(pl.lit("synthetic"))
            .otherwise(pl.lit("alpaca-paper"))
            .alias("feed_source")
        )
        assert report_c.build(enrich(raw), raw).is_synthetic


class TestTheArchiveShapeIsWhatArrives:
    def test_iso_string_timestamps_are_parsed(self):
        """The archive stores timestamps as strings because that is how they
        crossed Kafka. A tau clock cannot be handed a string, and both drivers
        hand over the same shape — which is what lets one engine serve replay
        from a recording or from an offset."""
        raw = archive_frame().with_columns(
            pl.col("quote_ts").dt.to_string("%Y-%m-%dT%H:%M:%S%:z"),
            pl.col("observed_at").dt.to_string("%Y-%m-%dT%H:%M:%S%:z"),
            pl.col("expiry").dt.to_string("%Y-%m-%d"),
        )
        assert raw.schema["quote_ts"] == pl.String

        out = enrich(raw)
        assert out.filter(pl.col("iv_status") == "ok").height == raw.height

    def test_normalisation_is_idempotent(self):
        """Callers holding an already-typed frame should not have to know."""
        typed = archive_frame()
        assert normalise_timestamps(normalise_timestamps(typed)).equals(typed)


def test_empty_archive_reports_nothing_rather_than_crashing():
    """An empty recording is a normal outcome — a holiday, a halted session —
    and must render as an empty report rather than a traceback."""
    empty = archive_frame().head(0)
    report = report_c.build(enrich(empty), empty)
    assert "_no rows_" in report.render()
    assert "## Blocked" in report.render()


def test_enrich_of_an_empty_recording_keeps_the_full_schema():
    """So a consumer never has to branch on "did any row survive"."""
    enriched = enrich(archive_frame().head(0))
    assert {"tau", "sigma_mid", "vol_half_spread", "delta", "iv_status"} <= set(enriched.columns)


def test_report_covers_every_instant_in_the_recording():
    instants = tuple(at(10, 0) + dt.timedelta(minutes=45 * i) for i in range(4))
    raw = archive_frame(instants=instants)
    report = report_c.build(enrich(raw), raw)
    assert report.forwards.height == len(instants)
    assert set(report.forwards["method"]) == {"put_call_parity"}


def test_default_instant_is_the_plans_entry_trigger():
    """09:45 is section 8's entry trigger, so it is the instant the fixtures
    default to rather than an arbitrary mid-session time."""
    assert (at(9, 45),) == DEFAULT_INSTANTS
