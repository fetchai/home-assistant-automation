"""Wiring tests for /rest/observation/* — the code that CALLS the watchlist,
the record and the backfill, not those pieces themselves.

The layer beneath is already covered: test_watchlist.py proves a selection
resolves correctly, test_record_store.py proves the queries, test_backfill.py
proves the recovery. What none of them prove is that the endpoint the panel
actually calls consults them — that saving a selection reaches the watchlist,
that a registry it could not fetch does not silently narrow what is watched,
or that an entity with no data yet still appears as "waiting" rather than
vanishing from the tab meant to show it.

Home Assistant is monkeypatched out at exactly one seam — registry.fetch_registry
— which is the same boundary the rest of this suite draws.
"""
import asyncio

import pytest

import conftest
from core import observation
from core import protocols
from core import record
from core import watchlist
from rest import rest_observation

REAL_DEPS = not conftest.STUBBED


AREAS = [{"area_id": "hall", "name": "Hall"}]
DEVICES = [{"id": "dev_lamp", "name": "Hall lamp", "area_id": "hall"}]
ENTITIES = [
    {"entity_id": "light.hall", "device_id": "dev_lamp"},
    {"entity_id": "sensor.hall_lamp_power", "device_id": "dev_lamp"},
    {"entity_id": "switch.loose"},
]
STATES = {
    "light.hall": {"state": "on", "attributes": {"friendly_name": "Hall light"}},
    "sensor.hall_lamp_power": {"state": "4.2", "attributes": {"friendly_name": "Hall lamp power"}},
    "switch.loose": {"state": "off", "attributes": {"friendly_name": "Loose switch"}},
}
SNAPSHOT = {"areas": AREAS, "devices": DEVICES, "entities": ENTITIES, "states": STATES}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A private record, a private watchlist and a clean link status per test."""
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
def ha(monkeypatch):
    """Home Assistant answering with the canned snapshot, and backfill stubbed
    so no test reaches the recorder."""
    spawned = []
    monkeypatch.setattr(rest_observation.registry, "fetch_registry", lambda: (True, SNAPSHOT))
    monkeypatch.setattr(rest_observation, "_spawn_backfill", lambda ids: spawned.append(list(ids)))
    return spawned


@pytest.fixture
def ha_down(monkeypatch):
    monkeypatch.setattr(
        rest_observation.registry, "fetch_registry",
        lambda: (False, "HA websocket request failed: connection refused"),
    )
    monkeypatch.setattr(
        rest_observation, "_spawn_backfill",
        lambda ids: pytest.fail("must not backfill when the registry could not be read"),
    )


def _request(**kwargs):
    return protocols.ObservationRequest(**kwargs)


class TestRegistryEndpoint:
    def test_it_returns_the_tree_with_the_selection_marked(self, ha):
        watchlist.set_selection(["dev_lamp"], [], {"dev_lamp": ["light.hall"]})

        response = _run(rest_observation.handle_registry(None, _request()))

        assert response.error == ""
        assert [a["name"] for a in response.areas] == ["Hall", "No area"]
        assert response.selection["devices"] == ["dev_lamp"]
        assert response.watched_count == 2  # both of the device's entities

    def test_a_registry_that_cannot_be_read_is_reported_not_raised(self, ha_down):
        response = _run(rest_observation.handle_registry(None, _request()))

        # An empty tree with no explanation would read as "this home has no
        # devices", which is a very different thing from "HA is unreachable".
        assert "connection refused" in response.error
        assert response.areas == []

    def test_it_caches_friendly_names_for_the_record_tab(self, ha):
        _run(rest_observation.handle_registry(None, _request()))

        # So a Record refresh costs no Home Assistant call of its own.
        assert observation.name_for("light.hall") == "Hall light"


class TestSelectionEndpoint:
    def test_saving_a_selection_reaches_the_watchlist(self, ha):
        _run(rest_observation.handle_selection(
            None, _request(devices=["dev_lamp"], entities=["switch.loose"])
        ))

        assert watchlist.is_watched("light.hall")
        assert watchlist.is_watched("sensor.hall_lamp_power")
        assert watchlist.is_watched("switch.loose")

    def test_the_response_reflects_what_was_just_saved(self, ha):
        response = _run(rest_observation.handle_selection(None, _request(devices=["dev_lamp"])))

        assert response.selection["devices"] == ["dev_lamp"]
        assert response.watched_count == 2

    def test_newly_watched_entities_are_backfilled(self, ha):
        _run(rest_observation.handle_selection(None, _request(devices=["dev_lamp"])))

        # Ticking a device should pay off immediately, not in a week's time.
        assert ha == [["light.hall", "sensor.hall_lamp_power"]]

    def test_only_the_newly_watched_are_backfilled(self, ha):
        _run(rest_observation.handle_selection(None, _request(devices=["dev_lamp"])))
        ha.clear()

        _run(rest_observation.handle_selection(
            None, _request(devices=["dev_lamp"], entities=["switch.loose"])
        ))

        assert ha == [["switch.loose"]]

    def test_deselecting_everything_backfills_nothing(self, ha):
        _run(rest_observation.handle_selection(None, _request(devices=["dev_lamp"])))
        ha.clear()

        _run(rest_observation.handle_selection(None, _request()))

        assert ha == []
        assert watchlist.watched_entities() == []

    def test_a_registry_that_cannot_be_read_saves_nothing(self, ha, ha_down):
        watchlist.set_selection(["dev_lamp"], [], {"dev_lamp": ["light.hall"]})

        response = _run(rest_observation.handle_selection(None, _request(devices=["dev_lamp"])))

        # Resolving against a registry we could not fetch would drop every
        # entity reached through a device — a selection the user never made.
        assert response.error
        assert watchlist.is_watched("light.hall")


class TestRecordEndpoint:
    def test_it_reports_what_the_record_holds(self, ha):
        record.record_transition("light.hall", 1000, "on", "off", 300)
        record.record_transition("light.hall", 2000, "off", "on", 1000)

        response = _run(rest_observation.handle_record(None, _request()))

        row = next(r for r in response.entities if r["entity_id"] == "light.hall")
        assert row["events"] == 2
        assert row["last_state"] == "off"
        assert response.totals["events"] == 2

    def test_a_watched_entity_with_no_data_yet_still_appears(self, ha):
        watchlist.set_selection([], ["light.hall"], {})

        response = _run(rest_observation.handle_record(None, _request()))

        # "Selected, waiting" and "not selected" are different states, and this
        # tab exists to tell them apart.
        row = next(r for r in response.entities if r["entity_id"] == "light.hall")
        assert row["events"] == 0
        assert row["watched"]

    def test_history_kept_for_an_entity_no_longer_watched_is_labelled(self, ha):
        record.record_transition("light.hall", 1000, "on")

        response = _run(rest_observation.handle_record(None, _request()))

        row = next(r for r in response.entities if r["entity_id"] == "light.hall")
        assert row["events"] == 1
        assert not row["watched"]

    def test_watched_entities_sort_ahead_of_withdrawn_ones(self, ha):
        record.record_transition("sensor.old", 5000, "1")
        watchlist.set_selection([], ["light.hall"], {})
        record.record_transition("light.hall", 1000, "on")

        response = _run(rest_observation.handle_record(None, _request()))

        assert response.entities[0]["entity_id"] == "light.hall"

    def test_rows_are_named_from_the_cache_and_fall_back_to_the_id(self, ha):
        record.record_transition("light.hall", 1000, "on")
        record.record_transition("sensor.never_seen_by_registry", 1000, "1")
        _run(rest_observation.handle_registry(None, _request()))

        response = _run(rest_observation.handle_record(None, _request()))
        names = {r["entity_id"]: r["name"] for r in response.entities}

        assert names["light.hall"] == "Hall light"
        assert names["sensor.never_seen_by_registry"] == "sensor.never_seen_by_registry"

    def test_it_reports_the_health_of_the_link(self, ha):
        response = _run(rest_observation.handle_record(None, _request()))

        # An empty record and a dead listener look identical without this.
        assert response.status["connected"] is False
        assert response.status["recorded"] == 0

    def test_an_empty_record_and_empty_watchlist_is_not_an_error(self, ha):
        response = _run(rest_observation.handle_record(None, _request()))

        assert response.entities == []
        assert response.error == ""
        assert response.totals["events"] == 0

    def test_it_includes_named_relations_across_the_record(self, ha):
        watchlist.set_selection([], ["light.hall", "switch.loose"], {})
        for start in (1000, 2000, 3000):
            record.record_transition("switch.loose", start, "on", "off", 1)
            record.record_transition("light.hall", start + 30, "on", "off", 1)
        record.scan_relations()
        _run(rest_observation.handle_registry(None, _request()))  # warms the name cache

        response = _run(rest_observation.handle_record(None, _request()))

        assert len(response.relations) == 1
        row = response.relations[0]
        assert row["entity_a"] == "switch.loose"
        assert row["entity_b"] == "light.hall"
        assert row["entity_a_name"] == "Loose switch"
        assert row["entity_b_name"] == "Hall light"
        assert row["count"] == 3


class TestEntityEndpoint:
    def test_it_returns_the_transitions_and_the_profile(self, ha):
        record.record_transition("light.hall", 1000, "on", "off", 300)
        record.record_transition("light.hall", 2000, "off", "on", 1000)

        response = _run(rest_observation.handle_entity(None, _request(entity_id="light.hall")))

        assert [r["ts"] for r in response.recent] == [2000, 1000]
        assert response.profile["by_hour"] and len(response.profile["by_hour"]) == 24

    def test_it_honours_the_requested_limit(self, ha):
        for ts in range(1000, 1010):
            record.record_transition("light.hall", ts * 10, str(ts % 2))

        response = _run(rest_observation.handle_entity(
            None, _request(entity_id="light.hall", limit=3)
        ))

        assert len(response.recent) == 3

    def test_asking_about_nothing_is_refused_rather_than_answered_emptily(self, ha):
        response = _run(rest_observation.handle_entity(None, _request(entity_id="  ")))

        assert response.error == "entity_id is required"
        assert response.recent == []

    def test_an_entity_with_no_history_answers_emptily_without_error(self, ha):
        response = _run(rest_observation.handle_entity(None, _request(entity_id="light.never")))

        assert response.recent == []
        assert response.profile["events"] == 0
        assert response.error == ""

    def test_it_includes_named_relations_triggered_by_this_entity(self, ha):
        watchlist.set_selection([], ["light.hall", "switch.loose"], {})
        for start in (1000, 2000, 3000):
            record.record_transition("switch.loose", start, "on", "off", 1)
            record.record_transition("light.hall", start + 30, "on", "off", 1)
        record.scan_relations()
        _run(rest_observation.handle_registry(None, _request()))  # warms the name cache

        response = _run(rest_observation.handle_entity(None, _request(entity_id="switch.loose")))

        assert len(response.relations) == 1
        assert response.relations[0]["entity_b_name"] == "Hall light"

    def test_an_entity_that_never_triggers_anything_has_no_relations(self, ha):
        watchlist.set_selection([], ["light.hall", "switch.loose"], {})
        for start in (1000, 2000, 3000):
            record.record_transition("switch.loose", start, "on", "off", 1)
            record.record_transition("light.hall", start + 30, "on", "off", 1)
        record.scan_relations()

        response = _run(rest_observation.handle_entity(None, _request(entity_id="light.hall")))

        assert response.relations == []


@pytest.mark.skipif(
    REAL_DEPS,
    reason="_rest_routes is bookkeeping conftest's Agent stub adds for introspection — "
           "the real uagents.Agent has no such attribute.",
)
class TestTheRoutesThePanelCalls:
    def test_all_four_endpoints_are_registered(self):
        from core.uagent import agent_1

        assert agent_1._rest_routes["/rest/observation/registry"] is rest_observation.handle_registry
        assert agent_1._rest_routes["/rest/observation/selection"] is rest_observation.handle_selection
        assert agent_1._rest_routes["/rest/observation/record"] is rest_observation.handle_record
        assert agent_1._rest_routes["/rest/observation/entity"] is rest_observation.handle_entity
