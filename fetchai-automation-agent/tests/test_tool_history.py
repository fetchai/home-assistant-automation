"""The recorder history tool: the window it asks for, and what it makes of the
answer.

fetch_history itself is covered in test_history_fetch.py — the request it builds
and the shapes it reads. What is new here is the layer above it: the tool the
LLM calls. Two things matter and neither is obvious. The transitions are put
through the same core.observer filter the behavioural record uses, so a sensor
reporting every few seconds does not spend the model's whole context; but the
SUMMARY is computed from the raw series, because the readings that filter drops
are exactly the ones a "what was the highest" question is about.

The third is a matter of honesty rather than volume: an empty answer has to say
it is empty and why, because a bare empty list is an absence the model fills in.
"""
from datetime import datetime, timezone
import json

import pytest

from tools.tool_homeassistant import history


def _iso(ts) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _now() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def _series(entity_id, entries):
    """One recorder series: (seconds_ago, state) pairs, as HA would answer."""
    now = _now()
    return [[
        {"entity_id": entity_id, "state": str(state), "last_changed": _iso(now - ago)}
        for ago, state in entries
    ]]


@pytest.fixture
def tool():
    return history.tool_ha_entity_history()


@pytest.fixture
def paths(monkeypatch):
    """Captures what the tool asks Home Assistant for, answering with nothing."""
    seen = []

    def _get(path, **kwargs):
        seen.append(path)
        return True, []

    monkeypatch.setattr(history, "_ha_get", _get)
    return seen


def _answering(monkeypatch, payload):
    monkeypatch.setattr(history, "_ha_get", lambda path, **kwargs: (True, payload))


def _run(tool, monkeypatch, entries, **kwargs):
    _answering(monkeypatch, _series("light.hall", entries))
    return json.loads(tool._run(entity_id="light.hall", **kwargs))


class TestTheWindowItAsksFor:
    def test_it_defaults_to_a_day(self, tool, paths):
        result = json.loads(tool._run(entity_id="light.hall"))

        assert result["window"]["hours"] == 24
        # A day fits in one chunk, so one request covers the whole window.
        assert len(paths) == 1

    def test_hours_are_clamped_to_what_the_recorder_can_hold(self, tool, paths):
        result = json.loads(tool._run(entity_id="light.hall", hours=10_000))

        assert result["window"]["hours"] == history.MAX_HISTORY_HOURS

    def test_a_window_of_nothing_becomes_an_hour(self, tool, paths):
        assert json.loads(tool._run(entity_id="light.hall", hours=0))["window"]["hours"] == 1
        assert json.loads(tool._run(entity_id="light.hall", hours=-5))["window"]["hours"] == 1

    def test_an_unparseable_hours_falls_back_rather_than_failing(self, tool, paths):
        """The model supplies these. A bad number must not lose the question."""
        assert json.loads(tool._run(entity_id="light.hall", hours="soon"))["window"]["hours"] == 24

    def test_it_refuses_without_an_entity(self, tool, paths):
        assert "error" in json.loads(tool._run(entity_id=""))
        assert paths == [], "no entity means no request"


class TestWhenTheRecorderHasNothing:
    def test_the_absence_is_stated_rather_than_implied(self, tool, paths):
        result = json.loads(tool._run(entity_id="light.hall"))

        assert result["transitions"] == []
        # The reason matters as much as the emptiness: purged, excluded, or not
        # yet existing are all things the user can act on.
        assert "purged" in result["note"]
        assert "Do not guess" in result["note"]

    def test_no_summary_is_offered_for_a_window_with_no_data(self, tool, paths):
        assert "summary" not in json.loads(tool._run(entity_id="light.hall"))


class TestWhatItReports:
    def test_transitions_carry_local_times_not_epochs(self, tool, monkeypatch):
        result = _run(tool, monkeypatch, [(7200, "off"), (3600, "on")])

        at = result["transitions"][-1]["at"]
        # "2026-08-25 14:03:00" — readable, and in the home's own clock.
        datetime.strptime(at, "%Y-%m-%d %H:%M:%S")

    def test_it_says_how_long_the_previous_state_lasted(self, tool, monkeypatch):
        result = _run(tool, monkeypatch, [(7200, "off"), (3600, "on")])

        last = result["transitions"][-1]
        assert last["state"] == "on"
        assert last["previous_state"] == "off"
        assert last["previous_lasted_seconds"] == 3600

    def test_time_spent_in_each_state_is_summarised(self, tool, monkeypatch):
        result = _run(tool, monkeypatch, [(7200, "off"), (3600, "on")], hours=3)

        seconds = result["summary"]["seconds_per_state"]
        assert seconds["off"] == 3600
        # The final state runs to the end of the window, not to its own entry.
        assert seconds["on"] == pytest.approx(3600, abs=5)

    def test_the_first_state_is_clamped_to_the_window(self, tool, monkeypatch):
        """The recorder's first entry is the state as it stood when the window
        opened, and its last_changed is usually older than the window. Counting
        from it would report time that predates the period asked about."""
        result = _run(tool, monkeypatch, [(86400 * 5, "on")], hours=2)

        assert result["summary"]["seconds_per_state"]["on"] == pytest.approx(7200, abs=5)

    def test_the_source_and_window_are_always_named(self, tool, monkeypatch):
        """The prompt requires every figure to be attributed; the tool has to
        put the attribution within reach."""
        result = _run(tool, monkeypatch, [(3600, "on")])

        assert result["source"] == "Home Assistant recorder"
        assert result["window"]["from"] and result["window"]["to"]
        assert result["window"]["timezone"]


class TestNumericSensors:
    ENTRIES = [(600, "20.0"), (540, "20.1"), (480, "20.2"), (420, "25.0")]

    def test_small_readings_are_filtered_out_of_the_transitions(self, tool, monkeypatch):
        result = _run(tool, monkeypatch, self.ENTRIES)

        # core.observer records numeric sensors as threshold crossings; 20.1 and
        # 20.2 are not crossings, and this is what keeps a power sensor from
        # returning thousands of rows.
        assert [t["state"] for t in result["transitions"]] == ["20.0", "25.0"]

    def test_the_extremes_still_come_from_every_reading(self, tool, monkeypatch):
        result = _run(tool, monkeypatch, self.ENTRIES)

        numeric = result["summary"]["numeric"]
        assert numeric["min"] == 20.0
        assert numeric["max"] == 25.0
        assert numeric["readings"] == 4
        assert numeric["mean_of_readings"] == pytest.approx(21.325)

    def test_a_numeric_sensor_gets_no_time_per_state_table(self, tool, monkeypatch):
        """Every reading is its own state; the table would have one row each."""
        assert "seconds_per_state" not in _run(tool, monkeypatch, self.ENTRIES)["summary"]


class TestSize:
    def test_a_long_series_is_capped_at_the_newest_end(self, tool, monkeypatch):
        # Alternating, a minute apart, so every one of them is a real transition.
        entries = [(60 * (300 - i), "on" if i % 2 else "off") for i in range(300)]

        result = _run(tool, monkeypatch, entries, hours=6)

        assert result["total"] == 300
        assert result["_truncated"] is True
        assert len(result["transitions"]) == history.MAX_TRANSITIONS
        # Questions are about the recent end, so that is the end that survives.
        assert result["transitions"][-1]["state"] == entries[-1][1]

    def test_a_short_series_is_not_marked_truncated(self, tool, monkeypatch):
        result = _run(tool, monkeypatch, [(7200, "off"), (3600, "on")])

        assert result["_truncated"] is False
        assert result["total"] == 2


class TestTheSummaryOnItsOwn:
    """summarise_series is pure, so it can be pinned without a clock or a socket."""

    T = 1787562000

    def test_changes_counts_transitions_not_readings(self):
        series = [
            {"state": "on", "last_changed": _iso(self.T)},
            {"state": "on", "last_changed": _iso(self.T + 60)},
            {"state": "off", "last_changed": _iso(self.T + 120)},
        ]

        summary = history.summarise_series(series, self.T, self.T + 180)

        assert summary["readings"] == 3
        assert summary["changes"] == 1

    def test_an_empty_series_summarises_to_nothing_seen(self):
        assert history.summarise_series([], self.T, self.T + 60) == {"readings": 0, "changes": 0}

    def test_unusable_entries_are_skipped(self):
        series = [{"state": "on"}, "not a dict", {"last_changed": _iso(self.T)}]

        assert history.summarise_series(series, self.T, self.T + 60)["readings"] == 0
