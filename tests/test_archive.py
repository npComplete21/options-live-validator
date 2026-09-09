"""Archive writing and reading, without a broker."""

from __future__ import annotations

import datetime as dt

import pytest

from olv.state.archive import (
    REQUIRED_FIELDS,
    is_archivable,
    partition_dir,
    read_archive,
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
