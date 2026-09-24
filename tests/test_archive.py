"""Archive writing and reading, without a broker."""

from __future__ import annotations

import datetime as dt

import pytest

from olv.state.archive import (
    REJECTION_DATASET,
    REQUIRED_FIELDS,
    flatten_rejections,
    is_archivable,
    partition_dir,
    read_archive,
    read_rejections,
    write_batch,
)

SESSION = dt.date(2026, 9, 9)


def row(symbol: str = "QQQ260909C00500000", **kwargs):
    base = {
        "symbol": symbol,
        "strike": 500.0,
        "right": "C",
        "bid": 1.0,
        "ask": 1.1,
        "mid": 1.05,
        "spread": 0.1,
        "quote_ts": "2026-09-09T15:00:00+00:00",
        "observed_at": "2026-09-09T15:00:00+00:00",
        "feed_source": "synthetic",
    }
    base.update(kwargs)
    return base


class TestIsArchivable:
    def test_a_complete_row_passes(self):
        assert is_archivable(row())

    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_missing_required_field_fails(self, field):
        assert not is_archivable(row(**{field: None}))

    @pytest.mark.parametrize("payload", [None, [], "{}", 42, {}])
    def test_non_rows_fail(self, payload):
        """Junk on the topic must not reach the schema.

        This exists because a leftover `{"seq": 0}` from an unrelated test
        crashed a whole archiver run — losing a session that cannot be
        re-observed is far worse than dropping one message.
        """
        assert not is_archivable(payload)


class TestPartitioning:
    def test_hive_layout(self, tmp_path):
        path = partition_dir(tmp_path, "qqq", SESSION)
        assert path.parts[-2:] == ("ticker=QQQ", "date=2026-09-09")

    def test_rows_land_in_their_session_partition(self, tmp_path):
        write_batch([row()], tmp_path, "QQQ")
        assert list(partition_dir(tmp_path, "QQQ", SESSION).glob("*.parquet"))

    def test_a_batch_spanning_dates_is_split(self, tmp_path):
        rows = [row(), row(symbol="QQQ260910C00500000", observed_at="2026-09-10T15:00:00+00:00")]
        written = write_batch(rows, tmp_path, "QQQ")
        assert len(written) == 2

    def test_empty_batch_writes_nothing(self, tmp_path):
        assert write_batch([], tmp_path, "QQQ") == []


class TestReadArchive:
    def test_round_trip(self, tmp_path):
        write_batch([row(), row(symbol="QQQ260909P00500000")], tmp_path, "QQQ")
        assert len(read_archive(tmp_path, "QQQ")) == 2

    def test_missing_archive_reads_empty(self, tmp_path):
        assert read_archive(tmp_path, "QQQ").is_empty()

    def test_duplicates_are_collapsed(self, tmp_path):
        """At-least-once delivery makes replayed batches expected."""
        write_batch([row()], tmp_path, "QQQ")
        write_batch([row()], tmp_path, "QQQ")
        assert len(read_archive(tmp_path, "QQQ")) == 1

    def test_same_contract_at_a_later_instant_is_not_a_duplicate(self, tmp_path):
        write_batch([row()], tmp_path, "QQQ")
        write_batch([row(quote_ts="2026-09-09T15:00:01+00:00")], tmp_path, "QQQ")
        assert len(read_archive(tmp_path, "QQQ")) == 2

    def test_can_read_one_session(self, tmp_path):
        write_batch(
            [row(), row(symbol="QQQ260910C00500000", observed_at="2026-09-10T15:00:00+00:00")],
            tmp_path,
            "QQQ",
        )
        assert len(read_archive(tmp_path, "QQQ", SESSION)) == 1


class TestRejectionFlattening:
    """chain.<ticker> snapshot -> one row per rejected quote."""

    @staticmethod
    def _snapshot(n_rejected=2, n_quotes=5):
        return {
            "underlying": "QQQ",
            "expiry": "2026-09-09",
            "underlying_price": 500.0,
            "underlying_ts": "2026-09-09T13:45:00+00:00",
            "observed_at": "2026-09-09T13:45:00+00:00",
            "feed_source": "test",
            "quotes": [{"symbol": f"Q{i}"} for i in range(n_quotes)],
            "rejected": [
                {
                    "symbol": f"QQQ260909P0048{i}000",
                    "reason": "zero_bid",
                    "bid": None,
                    "ask": 0.05,
                    "quote_ts": None,
                    "observed_at": "2026-09-09T13:45:00+00:00",
                    "feed_source": "test",
                }
                for i in range(n_rejected)
            ],
        }

    def test_one_row_per_rejected_quote(self):
        assert len(flatten_rejections(self._snapshot(n_rejected=3))) == 3

    def test_a_snapshot_rejecting_nothing_yields_no_rows(self):
        assert flatten_rejections(self._snapshot(n_rejected=0)) == []

    def test_rows_carry_the_denominator(self):
        """A rejection *rate* needs an accepted count, and carrying it on the
        row means the rate is readable without joining back to the quotes
        dataset — which also makes the two independently checkable."""
        row = flatten_rejections(self._snapshot(n_rejected=2, n_quotes=7))[0]
        assert row["snapshot_accepted"] == 7
        assert row["snapshot_rejected"] == 2

    def test_rows_carry_snapshot_context(self):
        row = flatten_rejections(self._snapshot())[0]
        assert row["underlying"] == "QQQ"
        assert row["expiry"] == "2026-09-09"

    def test_the_reason_survives(self):
        """Section 9: rejections are counted and published by reason, never
        collapsed into a single 'bad quote' bucket."""
        assert flatten_rejections(self._snapshot())[0]["reason"] == "zero_bid"

    def test_a_malformed_entry_is_skipped_not_fatal(self):
        snap = self._snapshot(n_rejected=1)
        snap["rejected"].append("not a dict")
        assert len(flatten_rejections(snap)) == 1


class TestRejectionsArchiveSeparately:
    def test_rejections_land_in_their_own_dataset(self, tmp_path):
        """Beside the accepted quotes, not inside them: a reader who forgot a
        status filter would otherwise enrich crossed markets as tradeable."""
        rows = TestRejectionFlattening._snapshot()["rejected"]
        write_batch(rows, tmp_path, "QQQ", REJECTION_DATASET)
        assert list(partition_dir(tmp_path, "QQQ", SESSION, REJECTION_DATASET).glob("*.parquet"))
        assert not list(partition_dir(tmp_path, "QQQ", SESSION).glob("*.parquet"))

    def test_reading_back_deduplicates_on_symbol_and_instant(self, tmp_path):
        """At-least-once delivery makes duplicates expected. quote_ts is not a
        dedupe key because a MISSING rejection may have no timestamp at all."""
        rows = TestRejectionFlattening._snapshot(n_rejected=2)["rejected"]
        write_batch(rows, tmp_path, "QQQ", REJECTION_DATASET)
        write_batch(rows, tmp_path, "QQQ", REJECTION_DATASET)  # replayed batch
        assert len(read_rejections(tmp_path, "QQQ")) == 2

    def test_absent_dataset_reads_empty_rather_than_raising(self, tmp_path):
        assert read_rejections(tmp_path, "QQQ").is_empty()

    def test_the_two_datasets_do_not_collide(self, tmp_path):
        quotes = [{"symbol": "A", "quote_ts": "t", "observed_at": "2026-09-09T13:45:00+00:00"}]
        write_batch(quotes, tmp_path, "QQQ")
        write_batch(
            TestRejectionFlattening._snapshot()["rejected"], tmp_path, "QQQ", REJECTION_DATASET
        )
        assert len(read_archive(tmp_path, "QQQ")) == 1
        assert len(read_rejections(tmp_path, "QQQ")) == 2
