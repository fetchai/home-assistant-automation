"""The two tools that read the add-on's own behavioural record.

Run against real SQLite and the real watchlist, like test_record_store.py and
test_watchlist.py: everything under these tools is stdlib, so stubbing any of it
would only test the stubs. The queries themselves are covered there — what is
tested here is what the tools do with the answers.

Most of that is about the shapes of emptiness. "Nothing recorded" has three
causes — nothing is watched, this entity is not watched, or it is watched and
has not moved yet — and each one points the user somewhere different. A tool
that returns zeroes for all three teaches the agent to say "no data", which is
the one answer that helps nobody.
"""
from datetime import datetime
import json

import pytest

from core import observation
from core import record
from core import watchlist
from core import window
from tools import tool_record


DAY = 86400


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    """A private record, an empty watchlist and an empty name cache per test."""
    record.close()
    monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "record.db"))
    monkeypatch.setattr(watchlist, "STORE_PATH", str(tmp_path / "watchlist.json"))
    monkeypatch.setattr(watchlist, "_state", None)
    monkeypatch.setattr(watchlist, "_resolved_set", frozenset())
    monkeypatch.setattr(observation, "_names", {})
    yield
    record.close()
    watchlist._state = None
    watchlist._resolved_set = frozenset()


@pytest.fixture
def now():
    return int(datetime.now().timestamp())


def _watch(*entity_ids):
    watchlist.set_selection([], list(entity_ids), {})


def _overview():
    return json.loads(tool_record.tool_record_overview()._run())


def _profile(entity_id, **kwargs):
    return json.loads(tool_record.tool_record_profile()._run(entity_id=entity_id, **kwargs))


def _patterns(entity_id="", **kwargs):
    return json.loads(tool_record.tool_record_relations()._run(entity_id=entity_id, **kwargs))


class TestTheOverviewWhenNothingIsWatched:
    """The state of every fresh install. It is not a fault, and it is not
    "no data" either — it is a thing the user can act on in the panel."""

    def test_it_says_so_and_says_where_to_fix_it(self):
        result = _overview()

        assert result["watching"]["entities"] == 0
        assert "Watching tab" in result["watching"]["note"]

    def test_the_totals_are_still_reported(self):
        assert _overview()["record"]["events"] == 0


class TestTheOverview:
    def test_a_watched_entity_with_nothing_recorded_still_appears(self):
        """"Selected, waiting" and "not selected" are different answers."""
        _watch("light.hall")

        rows = _overview()["entities"]

        assert [r["entity_id"] for r in rows] == ["light.hall"]
        assert rows[0]["watched"] is True
        assert rows[0]["events"] == 0

    def test_it_counts_what_was_recorded(self, now):
        _watch("light.hall")
        record.record_transition("light.hall", now - 600, "on")
        record.record_transition("light.hall", now - 300, "off", "on", 300)

        row = _overview()["entities"][0]

        assert row["events"] == 2
        assert row["last_state"] == "off"

    def test_friendly_names_come_from_the_cache_and_fall_back_to_the_id(self):
        _watch("light.hall")
        observation.remember_names({"light.hall": "Hall light"})

        assert _overview()["entities"][0]["name"] == "Hall light"

    def test_timestamps_are_local_and_readable(self, now):
        _watch("light.hall")
        record.record_transition("light.hall", now - 600, "on")

        datetime.strptime(_overview()["entities"][0]["last_seen"], "%Y-%m-%d %H:%M:%S")

    def test_it_reports_the_listener_so_an_empty_record_can_be_explained(self):
        """An empty Record tab and a dead socket look identical from the data
        alone; the agent must not report the second as the first."""
        listener = _overview()["listener"]

        assert set(listener) >= {"running", "connected", "recording_since", "last_error"}

    def test_a_large_home_is_capped(self, now, monkeypatch):
        monkeypatch.setattr(tool_record, "MAX_ENTITIES", 3)
        _watch(*[f"light.l{i}" for i in range(10)])

        result = _overview()

        assert len(result["entities"]) == 3
        assert result["total_entities"] == 10
        assert result["_truncated"] is True


class TestTheProfileWhenThereIsNothing:
    def test_an_unwatched_entity_is_told_apart_from_a_quiet_one(self):
        result = _profile("light.unknown")

        assert result["watched"] is False
        assert "not being watched" in result["note"]
        # The other source exists and the agent should be pointed at it.
        assert "Get Home Assistant entity history" in result["note"]

    def test_a_watched_entity_that_has_not_moved_yet_says_that_instead(self):
        _watch("light.hall")

        result = _profile("light.hall")

        assert result["watched"] is True
        assert "nothing has been recorded" in result["note"]

    def test_no_profile_is_offered_where_there_is_nothing_to_profile(self):
        """A zeroed histogram reads like a measurement. There isn't one."""
        assert "profile" not in _profile("light.hall")

    def test_it_refuses_without_an_entity(self):
        assert "error" in _profile("")


class TestTheProfile:
    @pytest.fixture(autouse=True)
    def recorded(self, now):
        _watch("light.hall")
        # Three evenings, an hour of "on" each — all more than a day ago, so a
        # one-day window is genuinely empty rather than nearly so.
        for day in (2, 3, 4):
            base = now - day * DAY
            record.record_transition("light.hall", base, "on")
            record.record_transition("light.hall", base + 3600, "off", "on", 3600)

    def test_it_reports_the_aggregates(self):
        profile = _profile("light.hall")["profile"]

        assert profile["events"] == 6
        assert profile["mean_on_duration_seconds"] == 3600
        assert 0 < profile["on_fraction"] <= 1

    def test_empty_hours_and_days_are_left_out(self):
        """24 and 7 mostly-zero buckets are context spent on nothing."""
        profile = _profile("light.hall")["profile"]

        assert all(n > 0 for n in profile["changes_by_hour"].values())
        assert all(day in
                   {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
                   for day in profile["changes_by_weekday"])

    def test_recent_transitions_come_back_newest_first_in_local_time(self):
        recent = _profile("light.hall")["recent"]

        assert recent[0]["state"] == "off"
        assert recent[0]["previous_state"] == "on"
        assert recent[0]["previous_lasted_seconds"] == 3600
        datetime.strptime(recent[0]["at"], "%Y-%m-%d %H:%M:%S")

    def test_the_limit_is_honoured_and_capped(self, monkeypatch):
        assert len(_profile("light.hall", limit=2)["recent"]) == 2

        monkeypatch.setattr(tool_record, "MAX_RECENT", 3)
        assert len(_profile("light.hall", limit=500)["recent"]) == 3

    def test_the_window_is_clamped_and_reported(self):
        assert _profile("light.hall", days=10_000)["days"] == tool_record.MAX_PROFILE_DAYS
        assert _profile("light.hall", days=0)["days"] == 1

    def test_an_unparseable_window_falls_back_rather_than_failing(self):
        """The model supplies these. A bad number must not lose the question."""
        assert _profile("light.hall", days="a month")["days"] == tool_record.DEFAULT_PROFILE_DAYS

    def test_it_prints_the_citable_figures_and_their_window(self):
        result = _profile("light.hall")
        profile = result["profile"]

        assert profile["changes_to_state"] == {"off": 3, "on": 3}
        assert profile["on_hours"] == 3.0
        # The record reaches back less than 30 days, so the window starts at it.
        assert profile["window_start"] == result["recorded_since"]
        assert profile["window_end"] == "now"

    def test_it_says_which_fields_are_not_counts_or_hours(self):
        fields = _profile("light.hall")["fields"]

        assert fields["events"] == "every change into any state — not a count of switch-ons"
        assert "not a count or hours" in fields["on_fraction"]
        assert "not a total" in fields["mean_on_duration_seconds"]

    def test_a_window_shorter_than_the_record_says_so(self):
        result = _profile("light.hall", days=1)

        assert result["profile"]["events"] == 0
        assert "Try a longer window" in result["note"]
        assert result["recorded_since"], "the record still reports how far back it reaches"


class TestTheProfileWhenHistoryHasBeenCollapsed:
    """Beyond retention, the record answers from a single running aggregate
    with no day-by-day resolution — the tool has to say that plainly rather
    than let a 400-day request read like an exact 400-day slice."""

    def test_it_says_the_window_is_not_exact_once_it_reaches_collapsed_history(self, now, monkeypatch):
        _watch("light.hall")
        record.record_transition("light.hall", now - 40 * DAY, "on")
        record.record_transition("light.hall", now - 39 * DAY, "off", "on", DAY)
        # prune() reads real wall-clock time by default; pin it to this test's
        # `now` so the collapse is deterministic regardless of when this runs.
        monkeypatch.setattr(record, "_now", lambda: now)
        record.prune(retention_days=10)

        result = _profile("light.hall", days=365)

        assert result["profile"]["events"] == 2
        assert "everything the record holds" in result["note"]
        assert "365" in result["note"]

    def test_a_window_still_entirely_inside_retention_gets_no_such_note(self, now):
        _watch("light.hall")
        record.record_transition("light.hall", now - DAY, "on")
        record.record_transition("light.hall", now - DAY + 3600, "off", "on", 3600)

        result = _profile("light.hall", days=30)

        assert "note" not in result


class TestTheProfilesFiguresAreTheRecords:
    """The printed counts and hours. The profile tool prints them from
    record.profile()'s SQL; this recounts them in Python from
    record.between() rows — the two independent paths the retired claim audit
    (free-hand F-9) used to be the second of. Over one planted series, the
    figure printed for the printed window must be the figure those rows give
    for that window, so a change to either side fails here, not as a number
    the model quotes wrongly in the field."""

    ENTITY = "light.hall"
    NOW = 1_790_000_000

    @pytest.fixture(autouse=True)
    def planted(self, monkeypatch):
        import random
        rng = random.Random(20260921)
        _watch(self.ENTITY)
        monkeypatch.setattr(record, "_now", lambda: self.NOW)
        # Forty days of on/off with the odd 'unavailable', spans up to ~6 h —
        # so a 30-day window cuts through a span, and a 60-day one starts at
        # the record's first row.
        ts, prev, prev_ts = self.NOW - 40 * DAY, None, None
        while ts < self.NOW:
            state = rng.choice([s for s in ("on", "off", "on", "off", "unavailable") if s != prev])
            record.record_transition(self.ENTITY, ts, state, prev,
                                     ts - prev_ts if prev_ts is not None else None)
            prev, prev_ts = state, ts
            ts += rng.randint(60, 6 * 3600)

    def _rows(self, start):
        return record.between(self.ENTITY, start, self.NOW)

    def _switch_ons(self, start):
        return sum(1 for r in self._rows(start) if r["state"] == "on")

    def _hours_on(self, start):
        """Each row closing an "on" span, clipped to [start, NOW)."""
        seconds = 0
        for r in self._rows(start):
            if r.get("prev_state") == "on" and r.get("prev_duration") is not None:
                overlap = min(r["ts"], self.NOW) - max(r["ts"] - r["prev_duration"], start)
                seconds += max(0, overlap)
        return seconds / 3600.0

    @pytest.mark.parametrize("days", [30, 60])
    def test_printed_figures_are_the_records_over_the_printed_window(self, days):
        profile = _profile(self.ENTITY, days=days)["profile"]
        start = window.local_epoch(profile["window_start"], self.NOW)
        assert window.local_epoch(profile["window_end"], self.NOW) == self.NOW
        if days == 60:
            assert start == record.first_ts(self.ENTITY)
        else:
            assert start == self.NOW - 30 * DAY

        assert profile["changes_to_state"]["on"] == self._switch_ons(start)
        assert profile["changes_to_state"]["on"] > record.RELATION_MIN_SUPPORT
        assert abs(profile["on_hours"] - self._hours_on(start)) <= 1e-6
        assert profile["on_hours"] > 0

    def test_events_is_not_the_switch_on_count(self):
        """The field bug itself: `events` counted every state, and was quoted."""
        profile = _profile(self.ENTITY, days=30)["profile"]
        start = window.local_epoch(profile["window_start"], self.NOW)
        assert profile["events"] != self._switch_ons(start)


class TestTheRelationsToolWhenThereIsNothing:
    def test_nothing_recorded_explains_the_threshold(self):
        result = _patterns()

        assert result["patterns"] == []
        assert str(record.RELATION_MIN_SUPPORT) in result["note"]
        assert str(record.RELATION_WINDOW_SECONDS) in result["note"]

    def test_an_unwatched_entity_is_told_apart_from_a_quiet_one(self):
        result = _patterns("binary_sensor.unknown")

        assert result["patterns"] == []
        assert "not being watched" in result["note"]


class TestTheRelationsTool:
    @pytest.fixture(autouse=True)
    def recorded(self):
        """binary_sensor.motion -> light.kitchen, three times, well inside the
        default window, and above the default minimum support. A second,
        unrelated pair is seeded too, so a clamped limit has something to cut."""
        _watch("binary_sensor.motion", "light.kitchen", "switch.fan", "climate.thermostat")
        for start in (1000, 2000, 3000):
            record.record_transition("binary_sensor.motion", start, "on", "off", 1)
            record.record_transition("light.kitchen", start + 30, "on", "off", 1)
        for start in (10000, 20000, 30000):
            record.record_transition("switch.fan", start, "on", "off", 1)
            record.record_transition("climate.thermostat", start + 30, "heat", "off", 1)
        observation.remember_names({
            "binary_sensor.motion": "Hall motion",
            "light.kitchen": "Kitchen light",
        })
        record.scan_relations()

    def test_it_reports_the_pattern_with_names_and_count(self):
        result = _patterns()

        assert len(result["patterns"]) == 2
        pattern = next(p for p in result["patterns"] if p["trigger"] == "binary_sensor.motion")
        assert pattern["trigger_name"] == "Hall motion"
        assert pattern["then"] == "light.kitchen"
        assert pattern["then_name"] == "Kitchen light"
        assert pattern["count"] == 3
        assert "note" not in result

    def test_filtering_by_entity_id_only_returns_its_own_patterns(self):
        result = _patterns("binary_sensor.motion")

        assert len(result["patterns"]) == 1
        assert result["patterns"][0]["trigger"] == "binary_sensor.motion"

    def test_an_entity_that_never_triggers_anything_reports_none_for_it(self):
        result = _patterns("light.kitchen")

        assert result["patterns"] == []
        assert "No recurring pattern" in result["note"]

    def test_the_limit_is_clamped(self, monkeypatch):
        monkeypatch.setattr(tool_record, "MAX_RELATIONS", 1)

        assert len(_patterns(limit=500)["patterns"]) == 1


class TestNothingOfAnUnwatchedEntityReachesTheModel:
    """2.21.4: unwatching deletes nothing from the record, and these tools are
    the path to ASI-1 in chat and in the daily analyst — so each shows nothing
    the record holds about an entity that is not watched now. The panel's
    Record tab (observation.coverage over REST) still lists it; that is local."""

    OLD = "switch.old"
    HALL = "light.hall"
    MARK = "sentinel_state"

    @pytest.fixture(autouse=True)
    def unwatched(self, now):
        _watch(self.OLD, self.HALL)
        record.record_transition(self.OLD, now - 2 * DAY, self.MARK)
        record.record_transition(self.OLD, now - DAY, "off", self.MARK, DAY)
        record.record_transition(self.HALL, now - 600, "on")
        record.record_transition(self.HALL, now - 300, "off", "on", 300)
        conn = record._connect()
        ids = {e: conn.execute("SELECT id FROM entities WHERE entity_id = ?", (e,)).fetchone()[0]
               for e in (self.OLD, self.HALL)}
        conn.executemany("INSERT INTO relations VALUES (?, ?, ?, 0, 10)", [
            (ids[self.OLD], ids[self.HALL], 9),
            (ids[self.HALL], ids[self.OLD], 8),
            (ids[self.HALL], ids[self.HALL], 7),
        ])
        conn.commit()
        _watch(self.HALL)

    def test_the_premise_the_record_still_holds_it(self):
        assert record.first_ts(self.OLD) is not None

    def test_the_overview_does_not_list_it(self):
        result = _overview()
        assert [r["entity_id"] for r in result["entities"]] == [self.HALL]
        assert self.OLD not in json.dumps(result)

    def test_the_overview_totals_count_watched_entities_only(self, now):
        totals = _overview()["record"]
        assert totals["events"] == 2
        assert totals["entities"] == 1
        assert totals["oldest"] == tool_record._local_iso(now - 600)
        assert totals["newest"] == tool_record._local_iso(now - 300)

    def test_the_profile_shows_nothing_of_it(self):
        result = _profile(self.OLD)
        assert result["watched"] is False
        assert "profile" not in result and "recent" not in result
        assert "recorded_since" not in result
        assert self.MARK not in json.dumps(result)
        assert "not shown" in result["note"]
        assert "holds nothing" not in result["note"]

    def test_the_patterns_need_both_entities_watched(self):
        pairs = [(p["trigger"], p["then"]) for p in _patterns()["patterns"]]
        assert pairs == [(self.HALL, self.HALL)]
        assert [(p["trigger"], p["then"]) for p in _patterns(self.HALL)["patterns"]] == pairs

    def test_the_patterns_of_it_are_not_shown(self):
        result = _patterns(self.OLD)
        assert result["patterns"] == []
        assert "not shown" in result["note"]

    def test_the_limit_counts_shown_patterns_only(self):
        assert len(_patterns(limit=1)["patterns"]) == 1

    def test_the_panel_still_lists_it(self):
        """observation.coverage feeds the Record tab over REST, never ASI-1."""
        rows = {r["entity_id"]: r for r in observation.coverage()}
        assert rows[self.OLD]["watched"] is False
        assert rows[self.OLD]["events"] == 2
