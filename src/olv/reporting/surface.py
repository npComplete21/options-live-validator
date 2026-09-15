"""Report C — surface and assumptions. Plan section 13.

Section 13 asks this report for four things. Two are available today and two
are not, and the report says which rather than quietly rendering three
quarters of itself:

===========================================  ===========================================
asked for                                    status
===========================================  ===========================================
``sigma_market`` by strike and time of day   available
observed half-spread in vol points           available
the fitted intraday vol curve (section 3)    **unavailable** - needs a recorded session
quote rejection rates (section 9)            **unavailable** - not archived, see below
===========================================  ===========================================

*The vol curve.* Section 3 makes the curve a measured input, not a modelled
one: intraday volatility is U-shaped, so flat session time still misstates tau
through the day, and the weighting has to come from observed data.
``obl.timebase.VolWeightedClock`` raises until it exists. No real session has
been recorded, and fitting the curve to the synthetic feed would calibrate the
largest modelling lever in the repo against numbers that feed's own docstring
forbids using in analysis. So the section is reported as blocked, with the
reason, rather than filled in.

*Rejection rates.* Section 9 calls these a first-class result - at 0DTE the
wings go untradeable for stretches, and how often a strategy *could not have
traded* is a finding in itself. They are published, on ``chain.<ticker>``, but
the archiver consumes ``quotes.<ticker>`` only, so they live at Kafka's seven
day retention and never reach Parquet. A first-class result with a seven-day
memory is not a permanent record, and section 0 makes the recording the
deliverable. Fixing that is an archiver change, not a reporting one.

What this report adds beyond section 13's list is the **assumption census**:
the measured forward and its dispersion per snapshot, and the share of the
chain carrying no usable vol at all. Both are the same kind of quantity as
rejection rates - they say how much of the recording can actually be believed -
and both are free now that the forward is measured rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from olv.analytics.surface import IVStatus, forward_series, stats_for

#: Moneyness buckets in log(K/F). The 0DTE band is |k| <= 0.05 (section 9), so
#: these split *within* it rather than spanning a conventional multi-week
#: surface. Named rather than numbered because the wings and the body are where
#: the spread behaves differently, and that is the comparison this table exists
#: to make.
MONEYNESS_EDGES = (-0.05, -0.02, -0.005, 0.005, 0.02, 0.05)
MONEYNESS_LABELS = (
    "put wing (beyond band)",
    "put wing",
    "put body",
    "at the money",
    "call body",
    "call wing",
    "call wing (beyond band)",
)

UNAVAILABLE_VOL_CURVE = (
    "no recorded session: the intraday weighting curve is a measured input "
    "(section 3), and fitting it to synthetic quotes would calibrate tau "
    "against numbers the synthetic feed forbids using in analysis"
)
UNAVAILABLE_REJECTIONS = (
    "not archived: rejections are published to chain.<ticker> but the archiver "
    "consumes quotes.<ticker> only, so they expire with Kafka's 7-day retention "
    "and never reach Parquet"
)


def _bucket(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        pl.col("log_moneyness")
        .cut(list(MONEYNESS_EDGES), labels=list(MONEYNESS_LABELS))
        .alias("moneyness_bucket")
    )


def sigma_by_strike_and_time(enriched: pl.DataFrame) -> pl.DataFrame:
    """``sigma_market`` by strike and time of day.

    Calls and puts are averaged rather than reported separately: on an
    arbitrage-free chain they carry the *same* implied vol, so splitting them
    would double every row and invite a reader to treat two views of one number
    as two observations.
    """
    priced = enriched.filter(pl.col("iv_status") == IVStatus.OK)
    if priced.is_empty():
        return pl.DataFrame()
    return (
        priced.group_by("observed_at", "strike")
        .agg(
            pl.col("sigma_mid").mean().alias("sigma_market"),
            pl.col("log_moneyness").mean().alias("log_moneyness"),
            pl.len().alias("contracts"),
        )
        .sort("observed_at", "strike")
    )


def half_spread_in_vol_points(enriched: pl.DataFrame) -> pl.DataFrame:
    """The spread in the unit the strategy trades in, by moneyness.

    Section 10's working hypothesis is that this term, not the volatility
    premium, decides whether the program reaches its return target. Reported by
    moneyness because that is where it varies: the wings quote wider in vol
    terms than the body even when they look similar in cents.
    """
    usable = enriched.filter(pl.col("vol_half_spread").is_finite())
    if usable.is_empty():
        return pl.DataFrame()
    return (
        _bucket(usable)
        .group_by("moneyness_bucket")
        .agg(
            pl.col("vol_half_spread").median().alias("median_vol_half_spread"),
            pl.col("vol_half_spread").quantile(0.9).alias("p90_vol_half_spread"),
            pl.col("spread").median().alias("median_spread_cash"),
            pl.len().alias("quotes"),
        )
        .sort("moneyness_bucket")
    )


def iv_status_census(enriched: pl.DataFrame) -> pl.DataFrame:
    """How much of the recorded chain carries a usable vol at all.

    The same kind of result as a rejection rate: a chain that cannot be priced
    is a chain a strategy could not have acted on, whatever the quotes looked
    like.
    """
    if enriched.is_empty():
        return pl.DataFrame()
    total = enriched.height
    return (
        enriched.group_by("iv_status")
        .len()
        .with_columns((pl.col("len") / total).alias("share"))
        .sort("len", descending=True)
    )


@dataclass(frozen=True)
class SurfaceReport:
    """Report C. ``blocked`` names what section 13 asked for and why it is absent."""

    sigma_surface: pl.DataFrame
    half_spread: pl.DataFrame
    forwards: pl.DataFrame
    status_census: pl.DataFrame
    feed_sources: tuple[str, ...]
    blocked: dict[str, str]

    @property
    def is_synthetic(self) -> bool:
        """True if any row came from the synthetic feed.

        Load-bearing: every number in a synthetic report is a property of a
        fixture generator, not of a market, and the report says so on its face.
        """
        return any(src == "synthetic" for src in self.feed_sources)

    def render(self) -> str:
        lines = ["# Report C - surface and assumptions", ""]
        if self.is_synthetic:
            lines += [
                "> **SYNTHETIC DATA - NOT A MARKET OBSERVATION.**",
                "> Every figure below is a property of the fixture generator in",
                "> `olv.feed.synthetic`, whose own docstring forbids using its numbers",
                f"> in analysis. Feed sources present: {', '.join(self.feed_sources)}.",
                "",
            ]
        lines += [f"Feed sources: {', '.join(self.feed_sources) or 'none'}", ""]

        for title, frame in (
            ("## sigma_market by strike and time of day", self.sigma_surface),
            ("## Observed half-spread in vol points", self.half_spread),
            ("## Measured forward, basis and dispersion", self.forwards),
            ("## Implied-vol status census", self.status_census),
        ):
            lines += [title, ""]
            lines += ["_no rows_" if frame.is_empty() else str(frame), ""]

        if self.blocked:
            lines += ["## Blocked - asked for by section 13, not available", ""]
            lines += [f"- **{name}** - {why}" for name, why in sorted(self.blocked.items())]
            lines += [""]
        return "\n".join(lines)


def build(enriched: pl.DataFrame, raw: pl.DataFrame | None = None) -> SurfaceReport:
    """Assemble report C from an enriched archive.

    ``raw`` is the pre-enrichment frame, used only for the forward series, which
    is measured from quotes rather than derived from anything enrichment added.
    """
    source = raw if raw is not None else enriched
    sources = (
        tuple(sorted(set(source["feed_source"].to_list())))
        if "feed_source" in source.columns and not source.is_empty()
        else ()
    )
    return SurfaceReport(
        sigma_surface=sigma_by_strike_and_time(enriched),
        half_spread=half_spread_in_vol_points(enriched),
        forwards=forward_series(source) if not source.is_empty() else pl.DataFrame(),
        status_census=iv_status_census(enriched),
        feed_sources=sources,
        blocked={
            "intraday vol curve (section 3)": UNAVAILABLE_VOL_CURVE,
            "quote rejection rates (section 9)": UNAVAILABLE_REJECTIONS,
        },
    )


def summarise(enriched: pl.DataFrame) -> str:
    """One line for a log or a CLI tail."""
    return str(stats_for(enriched))
