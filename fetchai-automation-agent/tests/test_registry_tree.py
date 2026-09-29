"""The device/entity tree the observation picker renders.

build_registry_tree is pure, so this drives it with the registry shapes a real
home actually produces — including the awkward ones the existing area tests
already taught this suite to expect: an entity with no device, a device with no
area, an entity pointing at an area that no longer exists.

What is NOT covered here is fetch_registry, which talks to Home Assistant.
This suite proves it is never called; the tree it feeds is what gets checked.
"""
from tools.tool_homeassistant import registry


AREAS = [
    {"area_id": "kitchen", "name": "Kitchen"},
    {"area_id": "hall", "name": "Hall"},
]

DEVICES = [
    {"id": "dev_lamp", "name": "Hall lamp", "area_id": "hall",
     "manufacturer": "IKEA", "model": "TRADFRI"},
    {"id": "dev_fridge", "name": "Fridge", "name_by_user": "Kitchen fridge", "area_id": "kitchen"},
    {"id": "dev_homeless", "name": "Unplaced sensor"},
]

ENTITIES = [
    {"entity_id": "light.hall", "device_id": "dev_lamp"},
    {"entity_id": "sensor.hall_lamp_power", "device_id": "dev_lamp"},
    {"entity_id": "sensor.fridge_temp", "device_id": "dev_fridge"},
    {"entity_id": "binary_sensor.homeless_motion", "device_id": "dev_homeless"},
    # An entity whose own area overrides its device's — what the HA UI does.
    {"entity_id": "switch.moved", "device_id": "dev_lamp", "area_id": "kitchen"},
    # No device at all: a YAML template sensor placed directly in an area.
    {"entity_id": "sensor.template_thing", "area_id": "kitchen"},
    # Points at an area that no longer exists.
    {"entity_id": "switch.orphan", "area_id": "area_that_went_away"},
    # Disabled by Home Assistant: produces no state and never will.
    {"entity_id": "sensor.disabled_one", "device_id": "dev_fridge", "disabled_by": "integration"},
]

STATES = {
    "light.hall": {"state": "on", "attributes": {"friendly_name": "Hall light"}},
    "sensor.hall_lamp_power": {"state": "4.2", "attributes": {"friendly_name": "Hall lamp power"}},
    "sensor.fridge_temp": {"state": "4.0", "attributes": {"friendly_name": "Fridge temperature"}},
    "binary_sensor.homeless_motion": {"state": "off", "attributes": {}},
    "switch.moved": {"state": "off", "attributes": {"friendly_name": "Moved switch"}},
    "sensor.template_thing": {"state": "7", "attributes": {"friendly_name": "Template thing"}},
    "switch.orphan": {"state": "off", "attributes": {}},
    "automation.morning": {"state": "on", "attributes": {"friendly_name": "Morning"}},
}


def _tree(devices=(), entities=()):
    return registry.build_registry_tree(
        AREAS, DEVICES, ENTITIES, STATES, {"devices": list(devices), "entities": list(entities)}
    )


def _area(tree, name):
    return next(a for a in tree["areas"] if a["name"] == name)


def _device(tree, area_name, device_name):
    return next(d for d in _area(tree, area_name)["devices"] if d["name"] == device_name)


def _entity_ids(node):
    return [e["entity_id"] for e in node["entities"]]


class TestEntitiesByDevice:
    def test_it_groups_entities_under_their_device(self):
        assert registry.entities_by_device(ENTITIES)["dev_lamp"] == [
            "light.hall", "sensor.hall_lamp_power", "switch.moved",
        ]

    def test_entities_without_a_device_are_left_out(self):
        by_device = registry.entities_by_device(ENTITIES)

        assert "sensor.template_thing" not in sum(by_device.values(), [])

    def test_malformed_rows_are_skipped_rather_than_raising(self):
        assert registry.entities_by_device([None, {}, {"entity_id": "x.y"}, "nope"]) == {}
        assert registry.entities_by_device(None) == {}

    def test_a_disabled_entity_is_left_out_of_its_device_too(self):
        # Ticking "Fridge" in the picker must not silently pull in an entity
        # that was never shown as a row under it.
        assert registry.entities_by_device(ENTITIES)["dev_fridge"] == ["sensor.fridge_temp"]


class TestTheTreeShape:
    def test_devices_sit_under_their_area_with_their_entities(self):
        device = _device(_tree(), "Hall", "Hall lamp")

        assert _entity_ids(device) == ["light.hall", "sensor.hall_lamp_power"]
        assert device["manufacturer"] == "IKEA"

    def test_a_user_renamed_device_shows_the_users_name(self):
        assert _device(_tree(), "Kitchen", "Kitchen fridge")["device_id"] == "dev_fridge"

    def test_an_entity_overriding_its_devices_area_moves_with_it(self):
        # switch.moved belongs to the hall lamp but was placed in the kitchen.
        kitchen_devices = [d["name"] for d in _area(_tree(), "Kitchen")["devices"]]

        assert "Hall lamp" in kitchen_devices
        assert _entity_ids(_device(_tree(), "Kitchen", "Hall lamp")) == ["switch.moved"]

    def test_an_entity_with_no_device_hangs_off_its_area_directly(self):
        assert _entity_ids(_area(_tree(), "Kitchen")) == ["sensor.template_thing"]

    def test_things_with_no_area_get_a_bucket_rather_than_disappearing(self):
        no_area = _area(_tree(), "No area")

        # These are real entities a user may well want to watch — dropping them
        # would make them unwatchable through the panel.
        assert [d["name"] for d in no_area["devices"]] == ["Unplaced sensor"]
        assert _entity_ids(no_area) == ["switch.orphan"]

    def test_an_entity_pointing_at_a_vanished_area_lands_in_that_bucket(self):
        assert "switch.orphan" in _entity_ids(_area(_tree(), "No area"))

    def test_the_no_area_bucket_sorts_last(self):
        assert _tree()["areas"][-1]["name"] == "No area"

    def test_an_entity_present_only_in_states_is_still_offered(self):
        tree = registry.build_registry_tree(
            AREAS, DEVICES, ENTITIES,
            dict(STATES, **{"sensor.yaml_only": {"state": "1", "attributes": {}}}),
            {"devices": [], "entities": []},
        )
        assert "sensor.yaml_only" in _entity_ids(_area(tree, "No area"))

    def test_an_empty_home_produces_an_empty_tree(self):
        tree = registry.build_registry_tree([], [], [], {}, {})

        assert tree["areas"] == []
        assert tree["watched_count"] == 0


class TestWhatIsOffered:
    def test_domains_the_agent_cannot_reason_over_are_not_offered(self):
        # automation.morning is the agent's own output, not an observation.
        offered = [
            e["entity_id"]
            for area in _tree()["areas"]
            for e in area["entities"] + sum((d["entities"] for d in area["devices"]), [])
        ]
        assert "automation.morning" not in offered

    def test_those_entities_are_counted_so_the_panel_can_say_so(self):
        assert _tree()["hidden_count"] >= 1

    def test_a_disabled_entity_is_not_offered(self):
        # It produces no state and never will, so watching it would watch nothing.
        assert _entity_ids(_device(_tree(), "Kitchen", "Kitchen fridge")) == ["sensor.fridge_temp"]

    def test_entities_carry_their_current_state_for_the_picker(self):
        light = _device(_tree(), "Hall", "Hall lamp")["entities"][0]

        assert light["name"] == "Hall light"
        assert light["state"] == "on"
        assert light["domain"] == "light"

    def test_an_entity_with_no_friendly_name_falls_back_to_its_id(self):
        motion = _device(_tree(), "No area", "Unplaced sensor")["entities"][0]

        assert motion["name"] == "binary_sensor.homeless_motion"


class TestSelectionIsReflectedBack:
    def test_a_selected_device_marks_it_and_all_its_entities(self):
        device = _device(_tree(devices=["dev_lamp"]), "Hall", "Hall lamp")

        assert device["watched"]
        assert not device["partial"]
        assert all(e["watched"] for e in device["entities"])

    def test_one_picked_entity_makes_its_device_partial_not_watched(self):
        device = _device(_tree(entities=["light.hall"]), "Hall", "Hall lamp")

        # The picker draws these as two different checkbox states, so they must
        # not collapse into each other.
        assert not device["watched"]
        assert device["partial"]
        assert [e["watched"] for e in device["entities"]] == [True, False]

    def test_every_entity_picked_individually_reads_as_fully_ticked(self):
        device = _device(
            _tree(entities=["light.hall", "sensor.hall_lamp_power"]), "Hall", "Hall lamp"
        )
        assert not device["partial"]
        assert device["watched_count"] == 2

    def test_nothing_selected_marks_nothing(self):
        tree = _tree()

        assert tree["watched_count"] == 0
        assert not any(d["watched"] or d["partial"]
                       for a in tree["areas"] for d in a["devices"])

    def test_the_watched_count_spans_devices_and_loose_entities(self):
        tree = _tree(devices=["dev_lamp"], entities=["sensor.template_thing"])

        # dev_lamp contributes light.hall and sensor.hall_lamp_power in the
        # hall, plus switch.moved which sits in the kitchen.
        assert tree["watched_count"] == 4

    def test_a_selection_naming_something_that_no_longer_exists_is_harmless(self):
        tree = _tree(devices=["dev_gone"], entities=["light.gone"])

        assert tree["watched_count"] == 0
