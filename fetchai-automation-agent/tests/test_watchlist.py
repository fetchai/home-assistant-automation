"""The watchlist is the gate on the behavioural record: an entity that is not
in it is never watched and never stored. These tests are therefore about two
things — that a selection resolves to exactly the entities the user ticked,
and that every way of losing or corrupting the file leaves the agent watching
LESS rather than more.

core/watchlist.py is pure stdlib, so this runs against the real module whether
or not the heavy dependencies are installed.
"""
import json

import pytest

from core import watchlist


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    """Points the store at a temp file and clears the module singletons.

    conftest already redirects DATA_DIR away from the live /data volume; this
    additionally isolates each test from the last, since watchlist caches both
    the state dict and the membership set at module level.
    """
    monkeypatch.setattr(watchlist, "STORE_PATH", str(tmp_path / "watchlist.json"))
    monkeypatch.setattr(watchlist, "_state", None)
    monkeypatch.setattr(watchlist, "_resolved_set", frozenset())
    yield
    watchlist._state = None
    watchlist._resolved_set = frozenset()


BY_DEVICE = {
    "dev_lamp": ["light.hall", "sensor.hall_lamp_power"],
    "dev_motion": ["binary_sensor.hall_motion", "sensor.hall_motion_battery"],
    "dev_unwatched": ["switch.garage"],
}


class TestResolveSelection:
    """The pure function, driven with canned registry data."""

    def test_a_device_brings_all_of_its_entities(self):
        assert watchlist.resolve_selection(["dev_lamp"], [], BY_DEVICE) == [
            "light.hall", "sensor.hall_lamp_power",
        ]

    def test_hand_picked_entities_are_watched_without_their_device(self):
        assert watchlist.resolve_selection([], ["binary_sensor.hall_motion"], BY_DEVICE) == [
            "binary_sensor.hall_motion",
        ]

    def test_an_entity_with_no_device_at_all_is_still_watchable(self):
        # Helpers and template sensors commonly have no device entry, and the
        # picker has to be able to offer them.
        assert watchlist.resolve_selection([], ["sensor.template_thing"], BY_DEVICE) == [
            "sensor.template_thing",
        ]

    def test_the_two_lists_are_a_union_not_an_override(self):
        resolved = watchlist.resolve_selection(["dev_lamp"], ["switch.garage"], BY_DEVICE)
        assert resolved == ["light.hall", "sensor.hall_lamp_power", "switch.garage"]

    def test_overlap_is_not_double_counted(self):
        resolved = watchlist.resolve_selection(["dev_lamp"], ["light.hall"], BY_DEVICE)
        assert resolved == ["light.hall", "sensor.hall_lamp_power"]

    def test_an_unknown_device_contributes_nothing_rather_than_raising(self):
        # A device can disappear from the registry between the panel rendering
        # it and the save arriving.
        assert watchlist.resolve_selection(["dev_gone"], [], BY_DEVICE) == []

    def test_nothing_selected_resolves_to_nothing(self):
        assert watchlist.resolve_selection([], [], BY_DEVICE) == []
        assert watchlist.resolve_selection(None, None, None) == []


class TestSelectionIsPersistedAndGates:
    def test_setting_a_selection_makes_exactly_those_entities_watched(self):
        watchlist.set_selection(["dev_lamp"], ["binary_sensor.hall_motion"], BY_DEVICE)

        assert watchlist.is_watched("light.hall")
        assert watchlist.is_watched("sensor.hall_lamp_power")
        assert watchlist.is_watched("binary_sensor.hall_motion")
        assert not watchlist.is_watched("switch.garage")
        assert not watchlist.is_watched("sensor.hall_motion_battery")

    def test_an_empty_watchlist_watches_nothing(self):
        assert watchlist.watched_entities() == []
        assert not watchlist.is_watched("light.hall")

    def test_the_selection_survives_a_restart(self, monkeypatch):
        watchlist.set_selection(["dev_lamp"], [], BY_DEVICE)

        # Simulate a fresh process: drop the caches, keep the file.
        monkeypatch.setattr(watchlist, "_state", None)
        monkeypatch.setattr(watchlist, "_resolved_set", frozenset())

        assert watchlist.is_watched("light.hall")
        assert watchlist.selection()["devices"] == ["dev_lamp"]

    def test_deselecting_a_device_stops_the_watching_immediately(self):
        watchlist.set_selection(["dev_lamp"], [], BY_DEVICE)
        watchlist.set_selection([], [], BY_DEVICE)

        # Not "at the next re-resolve" — the moment the user unticks it. The
        # window between the two would be recording data nobody asked for.
        assert not watchlist.is_watched("light.hall")
        assert watchlist.watched_entities() == []

    def test_the_user_facing_selection_is_kept_as_expressed(self):
        watchlist.set_selection(["dev_lamp"], ["switch.garage"], BY_DEVICE)
        selection = watchlist.selection()

        # The picker has to be able to redraw the device as ticked, which a
        # flattened entity list could not tell it.
        assert selection["devices"] == ["dev_lamp"]
        assert selection["entities"] == ["switch.garage"]
        assert "light.hall" in selection["resolved"]


class TestReResolve:
    def test_a_new_entity_on_a_watched_device_starts_being_watched(self):
        watchlist.set_selection(["dev_lamp"], [], BY_DEVICE)

        grown = dict(BY_DEVICE, dev_lamp=BY_DEVICE["dev_lamp"] + ["sensor.hall_lamp_energy"])
        added = watchlist.resolve(grown)

        assert added == ["sensor.hall_lamp_energy"]
        assert watchlist.is_watched("sensor.hall_lamp_energy")

    def test_re_resolving_an_unchanged_registry_reports_no_additions(self):
        watchlist.set_selection(["dev_lamp"], [], BY_DEVICE)
        assert watchlist.resolve(BY_DEVICE) == []

    def test_an_entity_removed_from_a_watched_device_stops_being_watched(self):
        watchlist.set_selection(["dev_lamp"], [], BY_DEVICE)

        shrunk = dict(BY_DEVICE, dev_lamp=["light.hall"])
        watchlist.resolve(shrunk)

        assert not watchlist.is_watched("sensor.hall_lamp_power")

    def test_a_hand_picked_entity_survives_its_device_losing_it(self):
        watchlist.set_selection([], ["sensor.hall_lamp_power"], BY_DEVICE)
        watchlist.resolve(dict(BY_DEVICE, dev_lamp=["light.hall"]))

        # It was picked directly, so the device's contents never governed it.
        assert watchlist.is_watched("sensor.hall_lamp_power")


class TestItFailsClosed:
    def test_a_corrupt_file_leaves_the_agent_watching_nothing(self, tmp_path, monkeypatch):
        path = tmp_path / "watchlist.json"
        path.write_text("{ this is not json")
        monkeypatch.setattr(watchlist, "STORE_PATH", str(path))
        monkeypatch.setattr(watchlist, "_state", None)
        monkeypatch.setattr(watchlist, "_resolved_set", frozenset())

        assert watchlist.watched_entities() == []
        assert not watchlist.is_watched("light.hall")

    def test_a_file_of_the_wrong_shape_is_rejected_rather_than_half_read(self, tmp_path, monkeypatch):
        path = tmp_path / "watchlist.json"
        path.write_text(json.dumps({"version": 1, "resolved": "light.hall"}))
        monkeypatch.setattr(watchlist, "STORE_PATH", str(path))
        monkeypatch.setattr(watchlist, "_state", None)
        monkeypatch.setattr(watchlist, "_resolved_set", frozenset())

        assert watchlist.watched_entities() == []

    def test_a_missing_file_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watchlist, "STORE_PATH", str(tmp_path / "nope" / "watchlist.json"))
        monkeypatch.setattr(watchlist, "_state", None)
        monkeypatch.setattr(watchlist, "_resolved_set", frozenset())

        assert watchlist.selection()["devices"] == []
