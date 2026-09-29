"""tools/tool_record_timeline.py — F-WP2's timeline tool.

Run against a real record.db built through core/record.py, like
test_tool_record.py and test_tool_record_query.py: everything underneath is
stdlib SQLite, so stubbing it would only test the stub. What is tested here is
what the tool does with core/timeline.py's answer — the caps, the window
resolution, and the same three shapes of emptiness test_tool_record.py
already names (not watched / watched-but-nothing / nothing-in-this-window),
plus the numeric case that is new to this tool.
"""
from datetime import datetime, timedelta
import json

import pytest

from core import observation
from core import record
from core import watchlist
from tools import tool_record_timeline as trt


DAY = 86400
HOUR = 3600


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    """A private record, an empty watchlist and an empty name cache per test —
    the same isolation test_tool_record.py's fixture gives its tools."""
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


def _timeline(entity_ids, **kwargs):
    return json.loads(trt.tool_record_timeline()._run(entity_ids=list(entity_ids), **kwargs))


def _local(ts) -> str:
    return datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")


class TestRunsForTwoEntities:
    def test_each_entitys_runs_are_reported_separately(self, now):
        _watch("light.kitchen", "binary_sensor.hall_motion")
        record.record_transition("light.kitchen", now - 3 * HOUR, "off")
        record.record_transition("light.kitchen", now - 2 * HOUR, "on", "off", HOUR)
        record.record_transition("binary_sensor.hall_motion", now - HOUR, "off")
        record.record_transition("binary_sensor.hall_motion", now - 30 * 60, "on", "off", 30 * 60)
        observation.remember_names({"light.kitchen": "Kitchen light"})

        result = _timeline(["light.kitchen", "binary_sensor.hall_motion"],
                           start=_local(now - 4 * HOUR), end=_local(now))

        assert [e["entity_id"] for e in result["entities"]] == \
            ["light.kitchen", "binary_sensor.hall_motion"]
        kitchen = result["entities"][0]
        assert kitchen["name"] == "Kitchen light"
        assert kitchen["watched"] is True
        assert kitchen["truncated"] is False
        assert kitchen["note"] == ""
        states = [r["state"] for r in kitchen["runs"]]
        assert states == ["off", "on"]
        assert kitchen["run_count"] == 2
        # The run open when the window is asked for is clipped to its edge,
        # never reported as running past what was actually asked about.
        assert kitchen["runs"][-1]["to"] == _local(now)

    def test_a_run_carries_its_real_duration_in_seconds(self, now):
        _watch("light.kitchen")
        record.record_transition("light.kitchen", now - 2 * HOUR, "off")
        record.record_transition("light.kitchen", now - HOUR, "on", "off", HOUR)

        result = _timeline(["light.kitchen"], start=_local(now - 3 * HOUR), end=_local(now))

        first = result["entities"][0]["runs"][0]
        assert first["state"] == "off"
        assert first["lasted_seconds"] == HOUR   # the actual span of that run, not the 3h window

    def test_a_window_opening_mid_state_uses_state_before(self, now):
        """Without record.state_at the tool could only say the light turned
        off at now-1h and report nothing before that; with it, the run already
        under way when the window opens is not lost."""
        _watch("light.kitchen")
        record.record_transition("light.kitchen", now - 5 * HOUR, "on")
        record.record_transition("light.kitchen", now - HOUR, "off", "on", 4 * HOUR)

        result = _timeline(["light.kitchen"], start=_local(now - 3 * HOUR), end=_local(now))

        runs = result["entities"][0]["runs"]
        assert runs[0]["state"] == "on"
        assert runs[0]["from"] == _local(now - 3 * HOUR)   # clipped to the window, not 5h back
        assert runs[1]["state"] == "off"


class TestTheCap:
    def test_more_runs_than_the_cap_are_truncated(self, now, monkeypatch):
        monkeypatch.setattr(trt, "TIMELINE_MAX_RUNS_PER_ENTITY", 3)
        _watch("switch.flappy")
        state = "off"
        for i in range(10):
            state = "on" if state == "off" else "off"
            record.record_transition("switch.flappy", now - (10 - i) * 60, state,
                                     "on" if state == "off" else "off", 60)

        result = _timeline(["switch.flappy"], start=_local(now - HOUR), end=_local(now))

        entity = result["entities"][0]
        assert entity["run_count"] == 3
        assert len(entity["runs"]) == 3
        assert entity["truncated"] is True

    def test_within_the_cap_is_not_truncated(self, now, monkeypatch):
        monkeypatch.setattr(trt, "TIMELINE_MAX_RUNS_PER_ENTITY", 300)
        _watch("light.kitchen")
        record.record_transition("light.kitchen", now - 2 * HOUR, "off")
        record.record_transition("light.kitchen", now - HOUR, "on", "off", HOUR)

        result = _timeline(["light.kitchen"], start=_local(now - 3 * HOUR), end=_local(now))

        assert result["entities"][0]["truncated"] is False


class TestTheShapesOfEmptiness:
    def test_an_unwatched_entity_says_so(self, now):
        result = _timeline(["light.never_selected"], days=1)

        entity = result["entities"][0]
        assert entity["watched"] is False
        assert entity["runs"] == []
        assert "not being watched" in entity["note"]
        assert "Watching tab" in entity["note"]

    def test_an_unwatched_entity_with_history_is_not_shown_and_not_called_empty(self, now):
        """Unwatching deletes nothing, so "the record holds nothing" would be
        false; the rows are withheld because it is not watched (2.21.3)."""
        _watch("light.lamp")
        record.record_transition("light.lamp", now - HOUR, "on")
        _watch()

        entity = _timeline(["light.lamp"], days=1)["entities"][0]
        assert entity["runs"] == []
        assert "not shown" in entity["note"]
        assert "holds nothing" not in entity["note"]

    def test_a_watched_entity_with_nothing_recorded_says_so_differently(self, now):
        _watch("light.hall")

        entity = _timeline(["light.hall"], days=1)["entities"][0]

        assert entity["watched"] is True
        assert entity["runs"] == []
        assert "nothing has been recorded" in entity["note"]

    def test_a_watched_entity_with_history_but_none_in_this_window_says_so(self, now):
        """A window entirely BEFORE the entity's first ever recording: unlike
        the mid-state case above, state_before is also unknown here, so there
        is truly nothing this window can report."""
        _watch("light.hall")
        record.record_transition("light.hall", now - DAY, "on")

        entity = _timeline(["light.hall"], start=_local(now - 20 * DAY),
                           end=_local(now - 15 * DAY))["entities"][0]

        assert entity["runs"] == []
        assert "requested window" in entity["note"]
        assert "record reaches back to" in entity["note"]


class TestANumericEntity:
    def test_no_runs_and_a_note_pointing_at_the_query_tool(self, now):
        _watch("sensor.office_temp")
        record.record_transition("sensor.office_temp", now - 2 * HOUR, "20.5")
        record.record_transition("sensor.office_temp", now - HOUR, "21.0", "20.5", HOUR)

        entity = _timeline(["sensor.office_temp"], start=_local(now - 3 * HOUR),
                           end=_local(now))["entities"][0]

        assert entity["runs"] == []
        assert entity["run_count"] == 0
        assert "Query the behavioural record" in entity["note"]

    def test_a_numeric_state_before_the_window_with_no_rows_inside_it_is_still_numeric(self, now):
        """The sample the numeric check runs over has to include state_before
        when the window itself is quiet, or a numeric sensor that simply did
        not change this week would be handed to runs() as though it were a
        light, and reported as one very long "run"."""
        _watch("sensor.office_temp")
        record.record_transition("sensor.office_temp", now - 10 * DAY, "20.5")

        entity = _timeline(["sensor.office_temp"], start=_local(now - DAY),
                           end=_local(now))["entities"][0]

        assert entity["runs"] == []
        assert "Query the behavioural record" in entity["note"]


class TestDaysVersusExplicitWindow:
    def test_the_default_is_seven_days_back_from_now(self, now):
        result = _timeline(["light.hall"])

        assert result["start"] == _local(now - 7 * DAY)
        assert result["end"] == _local(now)

    def test_days_is_honoured_and_clamped_to_the_ceiling(self, now):
        assert _timeline(["light.hall"], days=2)["start"] == _local(now - 2 * DAY)
        assert _timeline(["light.hall"], days=9999)["start"] == _local(now - trt.MAX_DAYS * DAY)

    def test_explicit_start_and_end_override_days(self, now):
        start = _local(now - 5 * DAY)
        end = _local(now - DAY)

        result = _timeline(["light.hall"], start=start, end=end, days=1)

        assert result["start"] == start
        assert result["end"] == end

    def test_end_now_resolves_to_the_current_instant(self, now):
        result = _timeline(["light.hall"], start=_local(now - DAY), end="now")

        assert result["end"] == _local(now)


class TestMalformedInputIsAnErrorNeverATraceback:
    def test_no_entity_ids(self):
        assert "entity_ids" in _timeline([])["error"]

    def test_start_without_end(self):
        assert "error" in _timeline(["light.hall"], start="2026-01-01 00:00")

    def test_an_unparseable_time(self):
        assert "error" in _timeline(["light.hall"], start="not a time", end="also not")

    def test_start_after_end(self, now):
        result = _timeline(["light.hall"], start=_local(now), end=_local(now - HOUR))
        assert "error" in result

    def test_more_than_the_entity_cap_is_kept_rather_than_refused(self, now):
        many = [f"light.l{i}" for i in range(trt.MAX_ENTITIES + 3)]

        result = _timeline(many, days=1)

        assert len(result["entities"]) == trt.MAX_ENTITIES
