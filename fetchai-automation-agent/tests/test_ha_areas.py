"""Area resolution — how 'the kitchen light' becomes a concrete entity_id.

resolve_area and entities_in_area take the registry index and the states as
ARGUMENTS, so they are pure and need no Home Assistant. The tool wrapper is
covered too, by monkeypatching the two fetch functions it calls; that seam
already exists in the module.

The "unambiguous" flag matters more than it looks: the system prompt tells the
model to act on a single match instead of asking the user which entity they
meant, so this is where "stop asking me obvious questions" is actually decided.
"""
import json

import pytest

from tools.tool_homeassistant import areas
from tools.tool_homeassistant.areas import entities_in_area, resolve_area


class TestResolveArea:
    @pytest.mark.parametrize("needle", ["kitchen", "Kitchen", "KITCHEN", "  kitchen  "])
    def test_exact_match_on_name_or_id_is_case_insensitive(self, area_index, needle):
        assert resolve_area(area_index, needle) == ["kitchen"]

    def test_exact_match_on_an_alias(self, area_index):
        assert resolve_area(area_index, "cooking room") == ["kitchen"]

    def test_match_on_area_id_even_when_the_name_is_empty(self, area_index):
        assert resolve_area(area_index, "nameless") == ["nameless"]

    @pytest.mark.parametrize("needle", ["", "   ", None])
    def test_empty_input_matches_nothing(self, area_index, needle):
        assert resolve_area(area_index, needle) == []

    def test_no_match_returns_empty(self, area_index):
        assert resolve_area(area_index, "garage") == []

    def test_partial_needle_inside_name(self, area_index):
        assert resolve_area(area_index, "living") == ["living_room"]

    def test_partial_name_inside_needle(self, area_index):
        """'turn off the kitchen area' still resolves to the kitchen."""
        assert resolve_area(area_index, "the kitchen area") == ["kitchen"]

    def test_exact_matches_win_outright_over_partial_ones(self):
        index = {"areas": {
            "kitchen": {"area_id": "kitchen", "name": "Kitchen", "aliases": [], "floor_id": None},
            "kitchen_annex": {"area_id": "kitchen_annex", "name": "Kitchen annex",
                              "aliases": [], "floor_id": None},
        }, "entity_area": {}}
        assert resolve_area(index, "kitchen") == ["kitchen"]

    def test_several_partial_matches_are_all_returned(self):
        index = {"areas": {
            "bath_up": {"area_id": "bath_up", "name": "Upstairs bathroom", "aliases": [], "floor_id": None},
            "bath_down": {"area_id": "bath_down", "name": "Downstairs bathroom", "aliases": [], "floor_id": None},
        }, "entity_area": {}}
        assert sorted(resolve_area(index, "bathroom")) == ["bath_down", "bath_up"]

    def test_a_very_short_area_name_matches_almost_anything(self):
        """Documented footgun: the `name in needle` half of the partial match
        means a one-character area name matches nearly every query. Pinned so a
        future fix is a deliberate change rather than an accident."""
        index = {"areas": {
            "a": {"area_id": "a", "name": "A", "aliases": [], "floor_id": None},
        }, "entity_area": {}}
        assert resolve_area(index, "the bathroom") == ["a"]


class TestEntitiesInArea:
    def test_returns_only_entities_of_the_requested_areas(self, area_index, ha_states):
        out = entities_in_area(area_index, ["bedroom"], ha_states)
        assert [e["entity_id"] for e in out] == ["switch.bedroom_fan"]

    def test_domain_filter(self, area_index, ha_states):
        out = entities_in_area(area_index, ["kitchen"], ha_states, domain="light")
        assert [e["entity_id"] for e in out] == ["light.kitchen_ceiling"]

    def test_no_domain_filter_keeps_every_domain(self, area_index, ha_states):
        out = entities_in_area(area_index, ["kitchen"], ha_states)
        assert [e["entity_id"] for e in out] == [
            "light.kitchen_ceiling", "sensor.kitchen_temperature",
        ]

    def test_results_are_sorted_by_entity_id(self, area_index, ha_states):
        out = entities_in_area(area_index, ["living_room"], ha_states)
        ids = [e["entity_id"] for e in out]
        assert ids == sorted(ids)

    def test_no_areas_requested_returns_nothing(self, area_index, ha_states):
        assert entities_in_area(area_index, [], ha_states) == []

    def test_state_without_attributes_yields_an_empty_friendly_name(self, area_index, ha_states):
        out = entities_in_area(area_index, ["living_room"], ha_states, domain="light")
        strip = next(e for e in out if e["entity_id"] == "light.living_room_strip")
        assert strip["friendly_name"] == ""
        assert strip["state"] == "off"

    def test_entity_absent_from_states_entirely_is_still_listed(self, area_index, ha_states):
        """light.orphan has no state row at all — the `or {}` guards keep it in
        the list rather than raising, just with empty name and state."""
        out = entities_in_area(area_index, ["area_that_does_not_exist"], ha_states)
        assert [e["entity_id"] for e in out] == ["light.orphan"]
        assert out[0]["friendly_name"] == ""
        assert out[0]["state"] == ""

    def test_area_name_falls_back_to_the_raw_id_when_the_area_is_unknown(self, area_index, ha_states):
        """The entity registry can reference an area_id the area registry no
        longer has; the raw id is shown rather than a blank."""
        out = entities_in_area(area_index, ["area_that_does_not_exist"], ha_states)
        assert out[0]["area"] == "area_that_does_not_exist"

    def test_an_area_with_an_empty_name_reports_an_empty_area(self, area_index, ha_states):
        out = entities_in_area(area_index, ["nameless"], ha_states)
        assert out[0]["area"] == ""


class TestUnambiguousContract:
    """The flag the system prompt tells the model to act on."""

    @staticmethod
    def _run(monkeypatch, index, states, **kwargs):
        monkeypatch.setattr(areas, "build_area_index", lambda: (True, index))
        monkeypatch.setattr(areas, "_states_by_entity", lambda: (True, states))
        return json.loads(areas.tool_ha_list_area_entities()._run(**kwargs))

    def test_a_single_match_is_unambiguous(self, monkeypatch, area_index, ha_states):
        out = self._run(monkeypatch, area_index, ha_states, area="kitchen", domain="light")
        assert out["total"] == 1
        assert out["unambiguous"] is True
        assert out["matched_areas"] == ["Kitchen"]

    def test_two_matches_are_ambiguous(self, monkeypatch, area_index, ha_states):
        out = self._run(monkeypatch, area_index, ha_states, area="living room", domain="light")
        assert out["total"] == 2
        assert out["unambiguous"] is False

    def test_no_matches_is_not_unambiguous(self, monkeypatch, area_index, ha_states):
        out = self._run(monkeypatch, area_index, ha_states, area="bedroom", domain="light")
        assert out["total"] == 0
        assert out["unambiguous"] is False

    def test_a_missing_area_argument_is_an_error(self):
        out = json.loads(areas.tool_ha_list_area_entities()._run(area=""))
        assert "error" in out

    def test_an_unknown_area_reports_the_available_ones(self, monkeypatch, area_index, ha_states):
        """So the model can name the real rooms instead of asking an open question."""
        out = self._run(monkeypatch, area_index, ha_states, area="garage")
        assert "error" in out
        assert out["available_areas"] == sorted(out["available_areas"])
        assert "Kitchen" in out["available_areas"]
