"""core/timeline.py: turning a series of record rows into runs.

PURE, like test_window.py: list literals in, tuples out, nothing opens a
database. The DST test pins the module's central claim — a run's length is
always `to_ts - from_ts`, never a wall-clock subtraction — against a real
zone with a real clock change, the same way test_window.py does.
"""
import time

import pytest

from core import timeline


def _row(ts, state):
    return {"ts": ts, "state": state}


class TestMergingSameStateRows:
    def test_consecutive_rows_in_the_same_state_merge_into_one_run(self):
        rows = [_row(100, "on"), _row(150, "on"), _row(200, "off")]

        assert timeline.runs(rows, 0, 300) == [(100, 200, "on"), (200, 300, "off")]

    def test_a_row_that_restates_the_known_state_before_merges_too(self):
        rows = [_row(150, "off"), _row(200, "on")]

        assert timeline.runs(rows, 100, 300, state_before="off") == [
            (100, 200, "off"), (200, 300, "on"),
        ]


class TestStateBefore:
    def test_known_opens_the_first_run_at_start(self):
        rows = [_row(150, "on")]

        assert timeline.runs(rows, 100, 300, state_before="off") == [
            (100, 150, "off"), (150, 300, "on"),
        ]

    def test_unknown_opens_the_first_run_at_the_first_row_instead(self):
        rows = [_row(150, "on")]

        assert timeline.runs(rows, 100, 300, state_before=None) == [(150, 300, "on")]

    def test_unknown_and_no_rows_answers_nothing_rather_than_guessing(self):
        assert timeline.runs([], 100, 300, state_before=None) == []

    def test_known_and_no_rows_is_one_run_covering_the_whole_window(self):
        assert timeline.runs([], 100, 300, state_before="on") == [(100, 300, "on")]


class TestClipping:
    def test_both_edges_are_clipped_to_the_window_not_to_the_data(self):
        rows = [_row(150, "on"), _row(180, "off")]

        result = timeline.runs(rows, 100, 300, state_before="off")

        assert result[0][0] == 100    # the first run opens at start, not at row 0
        assert result[-1][1] == 300   # the last run closes at end, not at its own row

    def test_a_row_before_start_never_opens_a_run_before_it(self):
        """runs() trusts the window it is given rather than re-deriving it from
        the rows — a row at or before `start` (even in a different state from
        `state_before`) must not move a run's opening earlier than the window
        actually asked for."""
        rows = [_row(90, "special"), _row(150, "off")]

        result = timeline.runs(rows, 100, 300, state_before="on")

        assert result == [(100, 150, "on"), (150, 300, "off")]
        assert all(frm >= 100 for frm, _to, _state in result)


class TestTheEmptyWindow:
    def test_start_equal_to_end_is_empty(self):
        assert timeline.runs([_row(150, "on")], 200, 200, state_before="off") == []

    def test_start_after_end_is_empty(self):
        assert timeline.runs([_row(150, "on")], 300, 200, state_before="off") == []


class TestADSTChange:
    """Europe/London, 29 March 2026: clocks go from 01:00 GMT straight to
    02:00 BST, so a run spanning that night is 23 real hours long though its
    two ends are calendar-adjacent evenings 24 hours apart on paper."""

    @pytest.fixture(autouse=True)
    def london(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/London")
        time.tzset()
        yield
        monkeypatch.undo()
        time.tzset()

    def _local(self, year, month, day, hour):
        return int(time.mktime((year, month, day, hour, 0, 0, 0, 0, -1)))

    def test_the_run_that_crosses_it_is_23_hours_not_24(self):
        start = self._local(2026, 3, 28, 20)
        end = self._local(2026, 3, 29, 20)

        result = timeline.runs([], start, end, state_before="on")

        assert result == [(start, end, "on")]
        from_ts, to_ts, _ = result[0]
        assert to_ts - from_ts == 23 * 3600

    def test_a_change_landing_inside_the_night_still_gets_its_real_length(self):
        start = self._local(2026, 3, 28, 20)
        # 04:00 BST on the 29th: two hours after the clocks jumped, 8 hours
        # nominal after `start` but only 7 real hours after it.
        change_at = self._local(2026, 3, 29, 4)
        end = self._local(2026, 3, 29, 20)

        result = timeline.runs([_row(change_at, "off")], start, end, state_before="on")

        assert result == [(start, change_at, "on"), (change_at, end, "off")]
        assert result[0][1] - result[0][0] == 7 * 3600
        assert result[1][1] - result[1][0] == 16 * 3600


class TestIsNumeric:
    def test_empty_is_not_numeric(self):
        assert timeline.is_numeric([]) is False

    def test_every_row_parsing_as_a_number_is_numeric(self):
        rows = [_row(1, "21.5"), _row(2, "-3"), _row(3, "22")]
        assert timeline.is_numeric(rows) is True

    def test_one_non_numeric_state_is_enough_to_refuse_it(self):
        rows = [_row(1, "21.5"), _row(2, "on")]
        assert timeline.is_numeric(rows) is False

    def test_a_none_state_is_not_numeric(self):
        assert timeline.is_numeric([_row(1, None)]) is False
