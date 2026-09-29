"""What the listener does with the events it receives.

The socket itself is out of reach — a daemon thread holding a websocket is not
something this suite can hold still — so the loop was built with its judgement
in one function, handle_message, and that is what is tested here. Everything
that decides whether a state change becomes a row in the record goes through
it: the watchlist gate, the observer rules, the duration bookkeeping and the
counters the Record tab reports.

The gate is the part worth being strict about. An entity nobody selected must
produce nothing at all — no row, no name, no counter — because that promise is
the whole basis of the record being opt-in.
"""
import pytest

import conftest
from core import observation
from core import record
from core import watchlist
# Imported for its side effects, exactly as main.py does it: this is also the
# only place the suite proves the task module imports cleanly at all, which a
# broken import would otherwise only reveal when the add-on failed to start.
from tasks import observation as observation_task

REAL_DEPS = not conftest.STUBBED


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    record.close()
    monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "record.db"))
    monkeypatch.setattr(watchlist, "STORE_PATH", str(tmp_path / "watchlist.json"))
    monkeypatch.setattr(watchlist, "_state", None)
    monkeypatch.setattr(watchlist, "_resolved_set", frozenset())
    observation.reset()
    yield
    record.close()
    watchlist._state = None
    watchlist._resolved_set = frozenset()
    observation.reset()


@pytest.fixture
def watching():
    watchlist.set_selection([], ["light.hall"], {})


T09 = 1787562000


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _event(entity_id="light.hall", new="on", old="off", ts=T09):
    return {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": _iso(ts),
            "data": {
                "entity_id": entity_id,
                "new_state": {"state": new, "last_changed": _iso(ts), "attributes": {}},
                "old_state": {"state": old, "last_changed": _iso(ts - 600)} if old else None,
            },
        },
    }


class TestTheWatchlistGate:
    def test_an_unwatched_entity_produces_nothing_at_all(self):
        assert observation.handle_message(_event("light.not_selected")) is False

        assert record.summary() == []
        # Not even counted: the promise is that an unselected entity is not
        # observed, and a counter ticking for it would be observation.
        assert observation.status()["seen"] == 0

    def test_a_watched_entity_is_recorded(self, watching):
        assert observation.handle_message(_event()) is True

        rows = record.recent("light.hall")
        assert [r["state"] for r in rows] == ["on"]
        assert rows[0]["source"] == "live"

    def test_selecting_an_entity_starts_it_being_recorded_immediately(self):
        observation.handle_message(_event())
        assert record.summary() == []

        watchlist.set_selection([], ["light.hall"], {})
        observation.handle_message(_event(ts=T09 + 60))

        # No signalling between the panel and the loop: the gate is read per
        # event, so a save takes effect on the very next one.
        assert len(record.recent("light.hall")) == 1

    def test_deselecting_stops_it_just_as_immediately(self, watching):
        observation.handle_message(_event())
        watchlist.set_selection([], [], {})

        observation.handle_message(_event(new="off", old="on", ts=T09 + 60))

        assert [r["state"] for r in record.recent("light.hall")] == ["on"]


class TestTheRulesAreApplied:
    def test_an_attribute_only_change_is_seen_but_not_stored(self, watching):
        observation.handle_message(_event(new="on", ts=T09))
        assert observation.handle_message(_event(new="on", old="on", ts=T09 + 60)) is False

        assert len(record.recent("light.hall")) == 1
        # Seen but not recorded is the shape of healthy filtering, and the
        # Record tab shows both numbers for exactly that reason.
        assert observation.status()["seen"] == 2
        assert observation.status()["recorded"] == 1

    def test_flapping_is_collapsed(self, watching):
        observation.handle_message(_event(new="on", ts=T09))
        observation.handle_message(_event(new="off", old="on", ts=T09 + 1))

        assert len(record.recent("light.hall")) == 1

    def test_durations_are_measured_between_recorded_rows(self, watching):
        observation.handle_message(_event(new="on", ts=T09))
        observation.handle_message(_event(new="off", old="on", ts=T09 + 600))

        latest = record.recent("light.hall")[0]
        assert latest["prev_state"] == "on"
        assert latest["prev_duration"] == 600

    def test_a_numeric_sensor_is_thresholded(self):
        watchlist.set_selection([], ["sensor.power"], {})

        observation.handle_message(_event("sensor.power", new="100.0", old=None, ts=T09))
        observation.handle_message(_event("sensor.power", new="100.2", old="100.0", ts=T09 + 60))
        observation.handle_message(_event("sensor.power", new="140.0", old="100.2", ts=T09 + 120))

        assert [r["state"] for r in record.recent("sensor.power")] == ["140.0", "100.0"]

    def test_an_event_with_no_timestamp_still_lands(self, watching):
        event = _event()
        event["event"]["data"]["new_state"]["last_changed"] = None
        event["event"]["time_fired"] = None

        assert observation.handle_message(event) is True


class TestRestartsAndJunk:
    def test_it_picks_up_where_the_record_left_off(self, watching):
        record.record_transition("light.hall", T09, "on", "off", 60)
        observation.reset()  # as if the add-on had restarted

        # The same state again is not a transition, even though this process
        # has never seen the entity before.
        assert observation.handle_message(_event(new="on", ts=T09 + 600)) is False
        assert len(record.recent("light.hall")) == 1

    def test_a_genuine_change_after_a_restart_is_recorded_against_the_old_row(self, watching):
        record.record_transition("light.hall", T09, "on", "off", 60)
        observation.reset()

        observation.handle_message(_event(new="off", old="on", ts=T09 + 600))

        latest = record.recent("light.hall")[0]
        assert latest["state"] == "off"
        assert latest["prev_duration"] == 600

    def test_messages_that_are_not_state_changes_are_ignored(self, watching):
        for message in (None, {}, {"type": "result", "success": True},
                        {"event": {"event_type": "call_service", "data": {}}}):
            assert observation.handle_message(message) is False

        assert observation.status()["seen"] == 0

    def test_an_entity_disappearing_is_not_a_transition(self, watching):
        event = _event()
        event["event"]["data"]["new_state"] = None

        assert observation.handle_message(event) is False


class TestStatusReporting:
    def test_a_fresh_process_reports_itself_as_not_connected(self):
        status = observation.status()

        assert status["connected"] is False
        assert status["running"] is False
        assert status["recorded"] == 0
        assert status["live_since"] is None

    def test_connecting_and_dropping_are_both_visible(self):
        observation.note_connected()
        assert observation.status()["connected"] is True
        live_since = observation.status()["live_since"]

        observation.note_disconnected("Home Assistant closed the websocket")
        status = observation.status()

        assert status["connected"] is False
        assert "closed the websocket" in status["last_error"]
        # The coverage claim survives a blip: live_since marks when this
        # process began covering events, and a reconnect does not restart it.
        assert status["live_since"] == live_since

    def test_reconnecting_clears_the_error(self):
        observation.note_disconnected("boom")
        observation.note_connected()

        assert observation.status()["last_error"] == ""

    def test_backfill_reach_keeps_the_earliest(self):
        observation.note_backfilled(5000)
        observation.note_backfilled(9000)

        # A later, smaller gap does not undo how far back the record reaches.
        assert observation.status()["backfilled_to"] == 5000

    def test_names_fall_back_to_the_entity_id(self):
        observation.remember_names({"light.hall": "Hall light", "light.blank": ""})

        assert observation.name_for("light.hall") == "Hall light"
        assert observation.name_for("light.blank") == "light.blank"
        assert observation.name_for("light.unknown") == "light.unknown"


@pytest.mark.skipif(
    REAL_DEPS,
    reason="_event_handlers/_interval_handlers are bookkeeping conftest's Agent stub "
           "adds for introspection — the real uagents.Agent exposes no such attributes.",
)
class TestTheAgentIsWiredToObserve:
    """Structural safety net, stub-only: the listener is only ever started by
    a startup handler, so a registration that quietly stopped happening would
    leave the record permanently empty with nothing else failing."""

    def test_the_listener_is_started_at_startup(self):
        from core.uagent import agent_1

        assert observation_task.start_observing in agent_1._event_handlers["startup"]

    def test_resolution_and_retention_are_registered_as_intervals(self):
        from core.uagent import agent_1

        assert observation_task.resolve_watchlist in agent_1._interval_handlers
        assert observation_task.prune_record in agent_1._interval_handlers
