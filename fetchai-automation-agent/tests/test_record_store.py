"""The behavioural record itself, exercised against real SQLite in a temp
directory.

Nothing is stubbed here and nothing needs to be: core/record.py is stdlib
sqlite3, so this is the actual storage engine the add-on runs, doing the
actual queries the panel will issue. What it is NOT is a test of the
observation thread that feeds it — the rules that decide what reaches these
functions live in core/observer.py and are covered in test_observer_rules.py.
"""
import os
import sqlite3
import time

import pytest

from core import record


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """A private database per test.

    record keeps a thread-local connection and an entity-id cache, so pointing
    DB_PATH somewhere new is not enough on its own — close() drops both."""
    record.close()
    monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "record.db"))
    yield
    record.close()


DAY = 86400


@pytest.fixture
def utc(monkeypatch):
    """Pins the process timezone for the hour-of-day assertions.

    SQLite's 'localtime' modifier reads the C library's timezone, which is set
    by tzset() and NOT restored when monkeypatch puts TZ back — so the reset
    has to be explicit, or this test silently shifts every later one that
    touches local time.
    """
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _seed(entity_id="light.hall", rows=()):
    for ts, state, prev_state, prev_duration in rows:
        record.record_transition(entity_id, ts, state, prev_state, prev_duration)


class TestWritingAndReadingBack:
    def test_a_transition_comes_back_as_it_went_in(self):
        assert record.record_transition("light.hall", 1000, "on", "off", 300)

        rows = record.recent("light.hall")
        assert len(rows) == 1
        assert rows[0]["ts"] == 1000
        assert rows[0]["state"] == "on"
        assert rows[0]["prev_state"] == "off"
        assert rows[0]["prev_duration"] == 300
        assert rows[0]["source"] == "live"

    def test_transitions_come_back_newest_first(self):
        _seed(rows=[(1000, "on", None, None), (2000, "off", "on", 1000), (3000, "on", "off", 1000)])

        assert [r["ts"] for r in record.recent("light.hall")] == [3000, 2000, 1000]

    def test_the_limit_takes_the_newest_not_the_oldest(self):
        _seed(rows=[(1000, "on", None, None), (2000, "off", "on", 1000), (3000, "on", "off", 1000)])

        assert [r["ts"] for r in record.recent("light.hall", limit=2)] == [3000, 2000]

    def test_entities_do_not_bleed_into_each_other(self):
        record.record_transition("light.hall", 1000, "on")
        record.record_transition("light.kitchen", 1000, "off")

        assert [r["state"] for r in record.recent("light.hall")] == ["on"]
        assert [r["state"] for r in record.recent("light.kitchen")] == ["off"]

    def test_reading_an_entity_that_was_never_watched_is_empty_not_an_error(self):
        assert record.recent("light.never_seen") == []
        assert record.last_ts("light.never_seen") is None
        assert record.last_state("light.never_seen") is None

    def test_an_entity_id_is_stored_once_however_many_events_it_has(self):
        _seed(rows=[(1000, "on", None, None), (2000, "off", "on", 1000)])

        conn = sqlite3.connect(record.DB_PATH)
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
        conn.close()

    def test_the_store_survives_being_reopened(self):
        record.record_transition("light.hall", 1000, "on")
        record.close()  # as if the add-on had restarted

        assert record.last_ts("light.hall") == 1000


class TestDuplicatesAreImpossible:
    """Backfill re-reads windows the live listener may already have covered.
    The UNIQUE index is what makes that overlap harmless rather than a double
    count, and it is the single thing standing between a restart loop and a
    record that inflates every time the add-on bounces."""

    def test_the_same_transition_twice_is_stored_once(self):
        assert record.record_transition("light.hall", 1000, "on")
        assert not record.record_transition("light.hall", 1000, "on")

        assert len(record.recent("light.hall")) == 1

    def test_a_batch_overlapping_existing_rows_only_adds_the_new_ones(self):
        record.record_transition("light.hall", 1000, "on")

        added = record.record_many("light.hall", [
            (1000, "on", None, None),
            (2000, "off", "on", 1000),
        ])

        assert added == 1
        assert len(record.recent("light.hall")) == 2

    def test_replaying_an_entire_backfill_changes_nothing(self):
        batch = [(1000, "on", None, None), (2000, "off", "on", 1000)]
        record.record_many("light.hall", batch)

        assert record.record_many("light.hall", batch) == 0
        assert len(record.recent("light.hall")) == 2

    def test_a_batch_is_marked_as_backfilled_by_default(self):
        record.record_many("light.hall", [(1000, "on", None, None)])

        # Provenance matters: the recorder's history is slightly lower fidelity
        # than live capture, and the panel says which is which.
        assert record.recent("light.hall")[0]["source"] == "history"


class TestResumePoints:
    def test_last_ts_is_where_backfill_resumes_from(self):
        _seed(rows=[(1000, "on", None, None), (2500, "off", "on", 1500)])

        assert record.last_ts("light.hall") == 2500

    def test_last_state_seeds_the_live_loop_after_a_restart(self):
        _seed(rows=[(1000, "on", None, None), (2500, "off", "on", 1500)])

        # Without this the first event after a restart would be judged against
        # nothing and stored even if it changed nothing.
        assert record.last_state("light.hall") == {"state": "off", "ts": 2500}


class TestSummary:
    def test_it_reports_span_count_and_current_state_per_entity(self):
        _seed("light.hall", [(1000, "on", None, None), (2000, "off", "on", 1000)])
        _seed("switch.fan", [(1500, "on", None, None)])

        rows = {r["entity_id"]: r for r in record.summary()}

        assert rows["light.hall"]["events"] == 2
        assert rows["light.hall"]["first_seen"] == 1000
        assert rows["light.hall"]["last_seen"] == 2000
        assert rows["light.hall"]["last_state"] == "off"
        assert rows["switch.fan"]["events"] == 1

    def test_it_is_ordered_by_most_recently_active(self):
        _seed("light.hall", [(1000, "on", None, None)])
        _seed("switch.fan", [(5000, "on", None, None)])

        assert [r["entity_id"] for r in record.summary()] == ["switch.fan", "light.hall"]

    def test_it_counts_how_much_of_each_entity_was_backfilled(self):
        record.record_many("light.hall", [(1000, "on", None, None)])
        record.record_transition("light.hall", 2000, "off", "on", 1000)

        assert record.summary()[0]["backfilled"] == 1

    def test_an_empty_record_summarises_to_nothing(self):
        assert record.summary() == []


class TestProfileIsComputedOnRead:
    def test_transitions_land_in_their_hour_and_weekday_buckets(self, utc, monkeypatch):
        # 2026-08-24 is a Monday. 09:00 and 21:00 UTC.
        _seed(rows=[(1787562000, "on", None, None), (1787605200, "off", "on", 43200)])
        monkeypatch.setattr(record, "_now", lambda: 1787605200)

        profile = record.profile("light.hall", days=30)

        assert profile["by_hour"][9] == 1
        assert profile["by_hour"][21] == 1
        assert profile["by_day"][0] == 2  # Monday first, not Sunday
        assert profile["events"] == 2

    def test_on_fraction_is_the_share_of_time_spent_on(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 4000)
        # on for 300s, then off for 700s.
        _seed(rows=[(1000, "on", "off", 100), (1300, "off", "on", 300), (2000, "on", "off", 700)])

        assert record.profile("light.hall")["on_fraction"] == pytest.approx(300 / 1100)

    def test_mean_on_duration_averages_only_the_on_stretches(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 4000)
        _seed(rows=[(1000, "off", "on", 100), (2000, "off", "on", 300), (3000, "on", "off", 900)])

        assert record.profile("light.hall")["mean_on_duration"] == pytest.approx(200)

    def test_on_fraction_is_none_for_an_entity_with_no_on_shaped_history(self, monkeypatch):
        """A cover has never once been 'on' — open/closed is its whole
        vocabulary — so on_fraction must not silently read as "always off"."""
        monkeypatch.setattr(record, "_now", lambda: 4000)
        _seed("cover.bedroom_blind", [
            (1000, "open", "closed", 100),
            (2000, "closed", "open", 1000),
        ])

        assert record.profile("cover.bedroom_blind")["on_fraction"] is None

    def test_on_fraction_is_none_for_a_climate_entity(self, monkeypatch):
        """climate legitimately uses the literal state 'off' — the near-miss
        case that makes it worth testing separately from cover."""
        monkeypatch.setattr(record, "_now", lambda: 4000)
        _seed("climate.thermostat", [
            (1000, "heat", "off", 100),
            (2000, "off", "heat", 1000),
        ])

        assert record.profile("climate.thermostat")["on_fraction"] is None

    def test_on_fraction_is_none_for_a_numeric_sensor(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 4000)
        _seed("sensor.temperature", [
            (1000, "21.3", "20.9", 100),
            (2000, "21.8", "21.3", 1000),
        ])

        assert record.profile("sensor.temperature")["on_fraction"] is None

    def test_on_fraction_is_still_zero_for_a_binary_entity_genuinely_always_off(self, monkeypatch):
        """A light that HAS been 'on' earlier in its full history, but is off
        throughout the requested window, must still report 0.0 — the fix must
        distinguish "no on-concept at all" from "on-concept, zero right now",
        not turn every zero into None."""
        monkeypatch.setattr(record, "_now", lambda: 40 * DAY)
        _seed("light.hall", [
            (1 * DAY, "on", None, None),          # on-shaped history exists, but...
            (15 * DAY, "off", "off", 5000),       # ...outside the 30-day window (since = 10*DAY)
            (20 * DAY, "off", "off", 3000),       # it never is 'on'
        ])

        profile = record.profile("light.hall", days=30)

        assert profile["events"] == 2  # only the two rows inside the window
        assert profile["on_fraction"] == 0.0

    def test_no_data_reads_as_unknown_rather_than_zero(self):
        profile = record.profile("light.never_seen")

        # "never on" and "nothing recorded yet" must not render identically —
        # the whole point of the Record tab is telling those two apart.
        assert profile["on_fraction"] is None
        assert profile["mean_on_duration"] is None
        assert profile["events"] == 0
        assert profile["by_hour"] == [0] * 24

    def test_events_outside_the_window_are_excluded(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(10 * DAY, "on", None, None), (99 * DAY, "off", "on", 100)])

        assert record.profile("light.hall", days=30)["events"] == 1


class TestPrune:
    def test_it_drops_events_past_the_window_and_keeps_the_rest(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(5 * DAY, "on", None, None), (99 * DAY, "off", "on", 100)])

        assert record.prune(retention_days=30) == 1
        assert [r["ts"] for r in record.recent("light.hall")] == [99 * DAY]

    def test_pruning_a_record_with_nothing_old_removes_nothing(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(99 * DAY, "on", None, None)])

        assert record.prune(retention_days=30) == 0

    def test_it_defaults_to_the_configured_retention(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 1000 * DAY)
        monkeypatch.setattr(record, "RETENTION_DAYS", 90)
        _seed(rows=[(800 * DAY, "on", None, None), (999 * DAY, "off", "on", 100)])

        assert record.prune() == 1


class TestPruneCollapsesIntoProfiles:
    """What §6.2 promises and the old one-line DELETE did not do: an event
    past retention degrades into an aggregate rather than ceasing to exist.
    The oracle for "did the collapse capture the right numbers" is profile()
    itself, read BEFORE the prune that would otherwise have thrown them away —
    if the merged answer after pruning does not match, the collapse is wrong,
    not just the assertion.
    """

    def test_the_merged_profile_matches_what_a_live_scan_would_have_said(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[
            (1 * DAY, "on", None, None),
            (2 * DAY, "off", "on", 1 * DAY),
            (3 * DAY, "on", "off", 1 * DAY),
            (99 * DAY, "off", "on", 96 * DAY),
        ])
        before = record.profile("light.hall", days=365)

        record.prune(retention_days=30)
        after = record.profile("light.hall", days=365)

        assert after["events"] == before["events"]
        assert after["by_hour"] == before["by_hour"]
        assert after["by_day"] == before["by_day"]
        assert after["on_fraction"] == pytest.approx(before["on_fraction"])
        assert after["mean_on_duration"] == pytest.approx(before["mean_on_duration"])

    def test_a_second_prune_with_nothing_new_past_cutoff_does_not_double_count(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(1 * DAY, "on", None, None), (99 * DAY, "off", "on", 98 * DAY)])

        record.prune(retention_days=30)
        once = record.profile("light.hall", days=365)
        record.prune(retention_days=30)
        twice = record.profile("light.hall", days=365)

        assert twice == once

    def test_two_separate_prune_runs_merge_additively(self, monkeypatch):
        """The record started reaching back 60 days, then later 30 — the same
        shape a live add-on takes as the retention window rolls forward hour
        by hour, just compressed into two steps."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[
            (1 * DAY, "on", None, None),
            (40 * DAY, "off", "on", 39 * DAY),
            (99 * DAY, "on", "off", 59 * DAY),
        ])
        before = record.profile("light.hall", days=365)

        record.prune(retention_days=60)  # collapses the day-1 event only
        record.prune(retention_days=30)  # collapses the day-40 event too
        after = record.profile("light.hall", days=365)

        assert after["events"] == before["events"] == 3
        assert after["by_hour"] == before["by_hour"]
        assert after["mean_on_duration"] == pytest.approx(before["mean_on_duration"])

    def test_mean_on_duration_is_not_just_luckily_right(self, monkeypatch):
        """A merge that summed durations but not counts, or vice versa, would
        still pass a test built from symmetric on/off timing. This one is not:
        on-stretches are short, off-stretches are long, so a wrong denominator
        shows up as a wrong mean rather than a coincidence."""
        monkeypatch.setattr(record, "_now", lambda: 20 * DAY)
        rows = []
        ts = 1 * DAY
        prev_state, prev_dur = None, None
        for i in range(40):
            state = "on" if i % 2 == 0 else "off"
            rows.append((ts, state, prev_state, prev_dur))
            prev_state = state
            prev_dur = 100 if state == "on" else 900
            ts += prev_dur
        _seed(rows=rows)
        before = record.profile("light.hall", days=365)

        record.prune(retention_days=5)
        after = record.profile("light.hall", days=365)

        assert after["mean_on_duration"] == pytest.approx(before["mean_on_duration"])
        assert after["mean_on_duration"] == pytest.approx(100, abs=1)

    def test_events_within_retention_are_never_collapsed(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(99 * DAY, "on", None, None)])

        record.prune(retention_days=30)

        assert record.profile("light.hall", days=365)["includes_collapsed_history"] is False


class TestReadingBeyondRetention:
    """profile()'s contract once some history has been collapsed: unchanged
    when the window stays inside live retention, honestly widened — and
    honestly LABELLED — when it does not.
    """

    def test_a_window_inside_retention_is_untouched_by_a_collapse_elsewhere(self, monkeypatch):
        """Regression pin: today's exact behaviour for a request that never
        reaches into collapsed territory must not change at all."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(1 * DAY, "on", None, None), (99 * DAY, "off", "on", 98 * DAY)])
        record.prune(retention_days=30)

        profile = record.profile("light.hall", days=5)

        assert profile["events"] == 1
        assert profile["includes_collapsed_history"] is False

    def test_a_window_reaching_into_collapsed_history_says_so(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(1 * DAY, "on", None, None), (99 * DAY, "off", "on", 98 * DAY)])
        record.prune(retention_days=30)

        profile = record.profile("light.hall", days=365)

        assert profile["events"] == 2
        assert profile["includes_collapsed_history"] is True

    def test_it_is_robust_to_retention_changing_after_the_fact(self, monkeypatch):
        """The comparison is against the persisted collapse boundary, not the
        live RETENTION_DAYS constant — so raising or lowering retention after
        some data is already collapsed cannot make this lie."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(1 * DAY, "on", None, None), (99 * DAY, "off", "on", 98 * DAY)])
        record.prune(retention_days=30)

        monkeypatch.setattr(record, "RETENTION_DAYS", 400)

        profile = record.profile("light.hall", days=365)
        assert profile["includes_collapsed_history"] is True
        assert profile["events"] == 2


class TestTheCitableFigures:
    """`changes_to_state` and `on_hours`: the printed count and hours, live
    events only, over [window_start, now) with window_start = max(now - days,
    first_ts). That they match a recount from record.between() rows over the
    window the tool prints is pinned in test_tool_record.py."""

    def test_changes_are_counted_per_state_inside_the_window(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(50 * DAY, "on", None, None),          # before the window: out
                    (95 * DAY, "on", "off", 100),
                    (96 * DAY, "off", "on", DAY),
                    (97 * DAY, "unavailable", "off", DAY),
                    (98 * DAY, "on", "unavailable", DAY),
                    (100 * DAY, "off", "on", 2 * DAY)])    # at now: out, [since, now)

        profile = record.profile("light.hall", days=10)

        assert profile["changes_to_state"] == {"on": 2, "off": 1, "unavailable": 1}
        assert profile["window_start"] == 90 * DAY
        # `events` stays what it was: every change, any state, ts >= since.
        assert profile["events"] == 5

    def test_on_hours_clips_a_span_that_started_before_the_window(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(80 * DAY, "on", None, None),
                    # on from day 88 to day 91: only day 90 -> 91 is inside.
                    (91 * DAY, "off", "on", 3 * DAY),
                    (92 * DAY, "on", "off", DAY),
                    (92 * DAY + 1800, "off", "on", 1800)])

        profile = record.profile("light.hall", days=10)

        assert profile["on_hours"] == pytest.approx(24 + 0.5)

    def test_a_span_wholly_before_the_window_adds_nothing(self, monkeypatch):
        """Its closing row is inside, its span is not — a negative overlap
        must not subtract."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(80 * DAY, "on", None, None),
                    (91 * DAY, "off", "on", 0),
                    (95 * DAY, "on", "off", 4 * DAY)])

        assert record.profile("light.hall", days=10)["on_hours"] == 0

    def test_an_entity_with_no_on_state_has_no_on_hours(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed("cover.garage", rows=[(95 * DAY, "open", None, None),
                                    (96 * DAY, "closed", "open", DAY)])

        profile = record.profile("cover.garage", days=10)

        assert profile["on_hours"] is None
        assert profile["changes_to_state"] == {"open": 1, "closed": 1}

    def test_an_on_entity_with_nothing_in_the_window_has_zero_not_none(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(50 * DAY, "on", None, None), (51 * DAY, "off", "on", DAY)])

        profile = record.profile("light.hall", days=10)

        assert profile["on_hours"] == 0
        assert profile["changes_to_state"] == {}

    def test_the_window_starts_no_earlier_than_the_record(self, monkeypatch):
        """C-3 refuses a claim starting before first_ts, and the first row's
        span must not be counted from before it either."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(95 * DAY, "off", "on", 10 * DAY),       # span from day 85: clipped to 95
                    (96 * DAY, "on", "off", DAY),
                    (96 * DAY + 3600, "off", "on", 3600)])

        profile = record.profile("light.hall", days=30)

        assert profile["window_start"] == 95 * DAY == record.first_ts("light.hall")
        assert profile["on_hours"] == pytest.approx(1.0)

    def test_collapsed_history_is_never_folded_into_them(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(1 * DAY, "on", None, None),
                    (2 * DAY, "off", "on", DAY),
                    (95 * DAY, "on", "off", 93 * DAY),
                    (95 * DAY + 7200, "off", "on", 7200)])
        record.prune(retention_days=30)

        profile = record.profile("light.hall", days=365)

        assert profile["includes_collapsed_history"] is True
        assert profile["events"] == 4                       # widened, as before
        assert profile["changes_to_state"] == {"on": 1, "off": 1}
        assert profile["on_hours"] == pytest.approx(2.0)    # the collapsed day is not in it
        # first_ts reaches into the collapsed row, so the window does too —
        # and there are no live rows there to count.
        assert profile["window_start"] == 1 * DAY


class TestHowFarBackTheRecordReaches:
    """first_ts(), summary()'s first_seen and stats()'s oldest all answer "how
    far back does the record reach" — and without reading profiles as well as
    events, that answer creeps forward by itself every time prune() runs,
    which is a second, quieter version of the same data-loss bug.
    """

    def test_first_ts_survives_its_own_events_being_pruned(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(1 * DAY, "on", None, None), (99 * DAY, "off", "on", 98 * DAY)])

        record.prune(retention_days=30)

        assert record.first_ts("light.hall") == 1 * DAY

    def test_summarys_first_seen_survives_too_while_the_entity_still_has_live_rows(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(1 * DAY, "on", None, None), (99 * DAY, "off", "on", 98 * DAY)])

        record.prune(retention_days=30)

        rows = {r["entity_id"]: r for r in record.summary()}
        assert rows["light.hall"]["first_seen"] == 1 * DAY

    def test_stats_oldest_survives_even_when_an_entity_has_no_live_rows_left(self, monkeypatch):
        """The one place this fix reaches further than summary() does on
        purpose: stats() is a whole-store total, not a per-entity join, so it
        stays right even for an entity that has been fully collapsed — the
        known gap this plan does not close for summary() itself."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        _seed(rows=[(1 * DAY, "on", None, None), (2 * DAY, "off", "on", 1 * DAY)])

        record.prune(retention_days=30)  # both rows collapse — nothing live left

        assert record.summary() == []  # the known, documented gap
        assert record.stats()["oldest"] == 1 * DAY  # but this still knows


class TestStats:
    def test_it_reports_the_whole_records_shape(self):
        _seed("light.hall", [(1000, "on", None, None)])
        record.record_many("switch.fan", [(2000, "on", None, None)])

        stats = record.stats()

        assert stats["events"] == 2
        assert stats["backfilled"] == 1
        assert stats["entities"] == 2
        assert stats["oldest"] == 1000
        assert stats["newest"] == 2000
        assert stats["bytes"] > 0

    def test_an_empty_record_reports_zeroes_not_nulls(self):
        stats = record.stats()

        assert stats["events"] == 0
        assert stats["entities"] == 0


class TestItNeverTakesTheAgentDown:
    """A record that cannot be written is a lost observation. It must not also
    be a failed service call or a crashed agent."""

    def test_a_broken_database_file_does_not_raise_on_write(self, tmp_path, monkeypatch):
        broken = tmp_path / "broken.db"
        broken.write_text("this is not a database")
        record.close()
        monkeypatch.setattr(record, "DB_PATH", str(broken))

        assert record.record_transition("light.hall", 1000, "on") is False

    def test_a_broken_database_file_does_not_raise_on_read(self, tmp_path, monkeypatch):
        broken = tmp_path / "broken.db"
        broken.write_text("this is not a database")
        record.close()
        monkeypatch.setattr(record, "DB_PATH", str(broken))

        assert record.recent("light.hall") == []
        assert record.summary() == []
        assert record.stats()["events"] == 0
        assert record.profile("light.hall")["events"] == 0
        assert record.prune() == 0

    def test_an_incomplete_transition_is_refused_quietly(self):
        assert record.record_transition("", 1000, "on") is False
        assert record.record_transition("light.hall", 1000, None) is False
        assert record.record_many("light.hall", []) == 0

    def test_the_directory_is_created_if_it_does_not_exist(self, tmp_path, monkeypatch):
        record.close()
        monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "sub" / "dir" / "record.db"))

        assert record.record_transition("light.hall", 1000, "on")
        assert os.path.exists(record.DB_PATH)

    def test_a_broken_database_file_does_not_raise_on_relations(self, tmp_path, monkeypatch):
        broken = tmp_path / "broken.db"
        broken.write_text("this is not a database")
        record.close()
        monkeypatch.setattr(record, "DB_PATH", str(broken))

        assert record.scan_relations() == 0
        assert record.relations() == []


class TestRelations:
    """Design §6.1's third record layer: observed co-occurrences — how often
    entity B changes shortly after entity A does. Fixed size, rebuilt in full
    on every scan rather than grown."""

    def _seed_pair(self, a="binary_sensor.motion", b="light.kitchen", offsets=(), gap=60):
        """Plants one A event and, `gap` seconds later, one B event, for each
        offset — i.e. `len(offsets)` separate A-then-B occurrences."""
        for i, start in enumerate(offsets):
            record.record_transition(a, start, "on", "off", 1)
            record.record_transition(b, start + gap, "on", "off", 1)

    def test_a_pair_within_the_window_at_minimum_support_is_reported(self):
        self._seed_pair(offsets=[1000, 2000, 3000], gap=60)

        assert record.scan_relations(window_seconds=120, min_support=3) == 1
        rows = record.relations()
        assert len(rows) == 1
        assert rows[0]["entity_a"] == "binary_sensor.motion"
        assert rows[0]["entity_b"] == "light.kitchen"
        assert rows[0]["count"] == 3

    def test_a_pair_outside_the_window_is_not_counted(self):
        self._seed_pair(offsets=[1000], gap=600)  # 10 minutes later

        record.scan_relations(window_seconds=120, min_support=1)

        assert record.relations() == []

    def test_a_pair_below_minimum_support_does_not_appear(self):
        self._seed_pair(offsets=[1000, 2000], gap=60)  # only twice

        record.scan_relations(window_seconds=120, min_support=3)

        assert record.relations() == []

    def test_one_a_event_followed_by_a_flickering_b_counts_once_not_many_times(self):
        record.record_transition("binary_sensor.motion", 1000, "on", "off", 1)
        record.record_transition("light.kitchen", 1010, "on", "off", 1)
        record.record_transition("light.kitchen", 1020, "off", "on", 10)
        record.record_transition("light.kitchen", 1030, "on", "off", 10)

        record.scan_relations(window_seconds=120, min_support=1)

        rows = record.relations("binary_sensor.motion")
        assert len(rows) == 1
        assert rows[0]["count"] == 1

    def test_scanning_twice_with_no_new_events_is_idempotent(self):
        self._seed_pair(offsets=[1000, 2000, 3000], gap=60)
        record.scan_relations(window_seconds=120, min_support=3)
        first = record.relations()

        record.scan_relations(window_seconds=120, min_support=3)
        second = record.relations()

        assert first == second

    def test_a_pair_fades_once_its_evidence_ages_past_retention(self, monkeypatch):
        """The main behavioural consequence of a full recompute rather than an
        incremental one: a habit that stopped happening a long time ago is not
        remembered forever."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        self._seed_pair(offsets=[1 * DAY, 1 * DAY + 60, 1 * DAY + 120], gap=60)
        record.scan_relations(window_seconds=120, min_support=3)
        assert record.relations() != []

        record.prune(retention_days=30)
        record.scan_relations(window_seconds=120, min_support=3)

        assert record.relations() == []

    def test_directions_are_tracked_independently(self):
        # A -> B three times, and separately B -> A twice.
        self._seed_pair(a="switch.fan", b="light.kitchen", offsets=[1000, 2000, 3000], gap=30)
        self._seed_pair(a="light.kitchen", b="switch.fan", offsets=[5000, 6000], gap=30)

        record.scan_relations(window_seconds=120, min_support=2)

        fan_first = {(r["entity_a"], r["entity_b"]): r["count"] for r in record.relations("switch.fan")}
        light_first = {(r["entity_a"], r["entity_b"]): r["count"] for r in record.relations("light.kitchen")}
        assert fan_first == {("switch.fan", "light.kitchen"): 3}
        assert light_first == {("light.kitchen", "switch.fan"): 2}

    def test_relations_for_an_unrelated_entity_is_empty(self):
        self._seed_pair(offsets=[1000, 2000, 3000], gap=60)
        record.scan_relations(window_seconds=120, min_support=3)

        assert record.relations("climate.thermostat") == []

    def test_the_window_edge_is_inclusive_and_one_second_past_it_is_not(self):
        """The boundary the SQL join spelled `b.ts <= a.ts + window`, kept
        exactly as it was when the join became a single ordered pass."""
        record.record_transition("binary_sensor.motion", 1000, "on", "off", 1)
        record.record_transition("light.kitchen", 1120, "on", "off", 1)   # exactly at the edge
        record.record_transition("binary_sensor.motion", 5000, "on", "off", 1)
        record.record_transition("switch.fan", 5121, "on", "off", 1)      # one second past it

        record.scan_relations(window_seconds=120, min_support=1)

        pairs = {(r["entity_a"], r["entity_b"]) for r in record.relations()}
        assert ("binary_sensor.motion", "light.kitchen") in pairs
        assert ("binary_sensor.motion", "switch.fan") not in pairs

    def test_two_entities_changing_at_the_same_second_are_not_a_response(self):
        """Simultaneous is not "after". Neither ordering of the pair is
        evidence that either one answered the other."""
        record.record_transition("binary_sensor.motion", 1000, "on", "off", 1)
        record.record_transition("light.kitchen", 1000, "on", "off", 1)

        record.scan_relations(window_seconds=120, min_support=1)

        assert record.relations() == []

    def test_one_a_event_followed_by_two_entities_counts_for_both_pairs(self):
        for start in (1000, 2000, 3000):
            record.record_transition("binary_sensor.motion", start, "on", "off", 1)
            record.record_transition("light.kitchen", start + 10, "on", "off", 1)
            record.record_transition("switch.fan", start + 20, "on", "off", 1)

        record.scan_relations(window_seconds=120, min_support=3)

        counts = {(r["entity_a"], r["entity_b"]): r["count"]
                  for r in record.relations("binary_sensor.motion")}
        assert counts == {("binary_sensor.motion", "light.kitchen"): 3,
                          ("binary_sensor.motion", "switch.fan"): 3}

    def test_since_and_until_are_the_first_and_last_triggering_event(self):
        self._seed_pair(offsets=[1000, 2000, 3000], gap=60)

        record.scan_relations(window_seconds=120, min_support=3)

        row = record.relations()[0]
        assert row["since"] == 1000
        assert row["until"] == 3000

    def test_the_record_still_accepts_writes_while_a_scan_is_running(self):
        """The regression this rewrite exists for: the scan used to open a
        write transaction and then do all of its reading inside it, so every
        observation the listener tried to store timed out against that lock.
        Reading now happens with no transaction open at all — which is what
        lets another connection write while the scan is mid-pass."""
        import sqlite3
        self._seed_pair(offsets=[1000, 2000, 3000], gap=60)
        record._connect()

        written = []
        real_counts = record._relation_counts

        def counting(conn_, window, since):
            # The scan's own connection must not have a transaction open here:
            # it is about to read the whole events table, and a write
            # transaction opened first would be held for all of it.
            assert conn_.in_transaction is False
            other = sqlite3.connect(record.DB_PATH, timeout=1)
            try:
                other.execute(
                    "INSERT OR IGNORE INTO events (entity, ts, state, source)"
                    " VALUES (1, 999999, 'on', 'live')"
                )
                other.commit()
                written.append(True)
            finally:
                other.close()
            return real_counts(conn_, window, since)

        record._relation_counts = counting
        try:
            assert record.scan_relations(window_seconds=120, min_support=3) == 1
        finally:
            record._relation_counts = real_counts
        assert written == [True]


class TestTheSizeCap:
    """§6.2's other half. RETENTION_DAYS bounds the record in time; on a home
    with fast-reporting sensors that bounded nothing, which is what 2.7.0 and
    2.7.1 showed. enforce_cap() is the size-driven trigger that re-runs the
    same collapse with a shorter window — so what these assert is that under
    the cap nothing moves, over it the window shortens, and either way the
    events that go are collapsed into profiles rather than lost.
    """

    def _seed_days(self, entity_id="light.hall", days=(), now=100 * DAY):
        for d in days:
            record.record_transition(entity_id, d * DAY, "on", "off", 60)

    def test_under_the_cap_it_does_nothing_at_all(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        self._seed_days(days=[1, 40, 99])

        assert record.enforce_cap(max_bytes=64 * 1024 * 1024) == 0
        assert [r["ts"] for r in record.recent("light.hall")] == [99 * DAY, 40 * DAY, 1 * DAY]

    def test_a_cap_of_zero_disables_it(self, monkeypatch):
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        monkeypatch.setattr(record, "_used_bytes", lambda conn: 10 ** 9)
        self._seed_days(days=[1, 99])

        assert record.enforce_cap(max_bytes=0) == 0
        assert len(record.recent("light.hall")) == 2

    def test_over_the_cap_the_window_shortens_until_the_record_fits(self, monkeypatch):
        """Halving from RETENTION_DAYS: 90 -> 45 -> 22. The stub reports the
        record as oversized until the day-30 event has gone, which only the
        22-day window reaches."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        monkeypatch.setattr(record, "RETENTION_DAYS", 90)
        self._seed_days(days=[10, 70, 99])  # 90, 30 and 1 days old

        def used(conn):
            still_old = conn.execute("SELECT COUNT(*) AS n FROM events WHERE ts < ?",
                                     (100 * DAY - 25 * DAY,)).fetchone()["n"]
            return 100 if still_old else 10

        monkeypatch.setattr(record, "_used_bytes", used)

        assert record.enforce_cap(max_bytes=100) == 2
        assert [r["ts"] for r in record.recent("light.hall")] == [99 * DAY]

    def test_what_the_cap_removes_is_collapsed_and_not_discarded(self, monkeypatch):
        """The promise the whole feature turns on: profiles are never
        discarded, so an early collapse costs sub-day resolution and nothing
        else. The oracle is profile(), which reads the collapsed row back."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        monkeypatch.setattr(record, "RETENTION_DAYS", 90)
        self._seed_days(days=[10, 99])
        monkeypatch.setattr(record, "_used_bytes", lambda conn: 100)

        record.enforce_cap(max_bytes=100)

        assert [r["ts"] for r in record.recent("light.hall")] == [99 * DAY]
        profile = record.profile("light.hall", days=365)
        assert profile["events"] == 2                      # the collapsed one still counts
        assert profile["includes_collapsed_history"] is True
        assert record.first_ts("light.hall") == 10 * DAY   # and the record still knows how far back it reaches

    def test_the_flag_follows_the_earlier_boundary_not_the_setting(self, monkeypatch):
        """The honesty check. An early collapse moves the persisted boundary
        forward, so a window that would have been entirely live under the
        configured retention now reaches into collapsed history — and has to
        say so."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        monkeypatch.setattr(record, "RETENTION_DAYS", 90)
        self._seed_days(days=[80, 99])  # 20 and 1 days old — both inside 90
        monkeypatch.setattr(record, "_used_bytes", lambda conn: 100)

        assert record.profile("light.hall", days=30)["includes_collapsed_history"] is False
        record.enforce_cap(max_bytes=100)  # squeezed to the 7-day floor
        assert record.profile("light.hall", days=30)["includes_collapsed_history"] is True

    def test_it_stops_at_the_floor_rather_than_emptying_the_record(self, monkeypatch):
        """A cap nothing can satisfy does not become a cap that deletes
        everything: CAP_MIN_RETENTION_DAYS holds and the shortfall is logged."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        monkeypatch.setattr(record, "RETENTION_DAYS", 90)
        monkeypatch.setattr(record, "_used_bytes", lambda conn: 10 ** 9)
        self._seed_days(days=[1, 97, 99])  # the last two are inside the 7-day floor

        record.enforce_cap(max_bytes=1024)

        assert [r["ts"] for r in record.recent("light.hall")] == [99 * DAY, 97 * DAY]

    def test_used_bytes_falls_when_events_go_even_though_the_file_does_not(self, monkeypatch):
        """Why the cap measures pages and not os.path.getsize(): SQLite frees
        pages to a freelist and reuses them rather than shrinking the file, so
        a cap read off the file size would ratchet the window down to its floor
        on a number that was never going to move."""
        monkeypatch.setattr(record, "_now", lambda: 100 * DAY)
        for i in range(4000):
            record.record_transition("light.hall", 1 * DAY + i, "on", "off", 1)

        conn = record._connect()
        before_used = record._used_bytes(conn)
        before_file = os.path.getsize(record.DB_PATH)

        record.prune(retention_days=30)

        assert record._used_bytes(record._connect()) < before_used
        assert os.path.getsize(record.DB_PATH) >= before_file

    def test_stats_reports_the_cap_next_to_the_size_it_bounds(self, monkeypatch):
        monkeypatch.setattr(record, "MAX_BYTES", 256 * 1024 * 1024)
        record.record_transition("light.hall", 1000, "on", "off", 1)

        totals = record.stats()
        assert totals["max_bytes"] == 256 * 1024 * 1024
        assert totals["capped"] is False

        monkeypatch.setattr(record, "MAX_BYTES", 1024)
        assert record.stats()["capped"] is True
