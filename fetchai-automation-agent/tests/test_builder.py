"""The candidate-to-automation builder, against candidates written as literals.

Nothing here needs a database or a home. `build_config` is pure by design —
the same split core/observer.py has against core/observation.py — so the shape
of the automation, the stability of its id and every refusal are exercised
against a dict. Only the preflight class stubs Home Assistant, and it stubs it
the way tests/test_ha_automations.py does: by replacing
`_validate_by_test_create`, which is the one function that talks to a home.

The load-bearing assertions are the ones that compare against the detector's
own constants rather than against 1, 5 or 30. A copied number here would let
the builder and the detector drift apart, and the moment they do the estimate
printed beside a suggestion stops describing the automation being proposed.
"""
import json

import pytest

from core import builder
from core import window
from core.detectors import schedule
from core.detectors import standby
from tools.tool_homeassistant import automations


SENSOR = "sensor.tv_power"
SWITCH = "switch.living_room_tv"


def _candidate(entity=SENSOR, switch=SWITCH, **evidence):
    """A validated standby candidate, shaped the way find_candidate returns one."""
    ev = {
        "entity": entity,
        "switch": switch,
        "watts": 12.0,
        "watts_range": [11.5, 12.5],
        "hours_per_day": standby.NIGHT_HOURS,
        "kwh_per_month": 1.01,
        "nights_per_month": 21.0,
        "nights": 21,
        "nights_observed": 24,
        "nights_examined": 30,
        "covered_days": 30,
        "night_window": standby.night_window_label(),
    }
    ev.update(evidence)
    return {"key": f"{standby.KIND}:{entity}", "kind": standby.KIND,
            "entities": [entity, switch], "evidence": ev}


def _built(candidate=None, **kwargs):
    result, error = builder.build_config(candidate or _candidate(), **kwargs)
    assert error is None, error
    return result


def _triggers(config):
    return {t["id"]: t for t in config["trigger"]}


def _branches(config):
    """The choose branches, keyed by the trigger id each one answers to."""
    out = {}
    for branch in config["action"][0]["choose"]:
        trigger_id = next(c["id"] for c in branch["conditions"]
                          if c["condition"] == "trigger")
        out[trigger_id] = branch
    return out


# --------------------------------------------------------------------------
class TestTheShape:
    """Off at night start behind an idle check, on again at night end."""

    def test_both_triggers_live_in_one_automation(self):
        """§9's one-suggestion-per-approval rule, in the config: a pair of
        automations would be two journal rows the user answered with one word,
        and rolling back would be two operations where one was recorded."""
        config = _built()["config"]

        assert len(config["trigger"]) == 2
        assert set(_triggers(config)) == {builder.TRIGGER_OFF, builder.TRIGGER_ON}
        assert all(t["platform"] == "time" for t in config["trigger"])

    def test_the_off_branch_is_guarded_by_the_idle_condition(self):
        branch = _branches(_built()["config"])[builder.TRIGGER_OFF]
        guard = next(c for c in branch["conditions"] if c["condition"] == "numeric_state")

        assert guard["entity_id"] == SENSOR
        assert branch["sequence"] == [
            {"service": "switch.turn_off", "target": {"entity_id": SWITCH}}]

    def test_the_on_branch_is_unconditional(self):
        """Restoring power is what makes the change invisible. A condition here
        is a way for the device to stay off all day."""
        branch = _branches(_built()["config"])[builder.TRIGGER_ON]

        assert [c["condition"] for c in branch["conditions"]] == ["trigger"]
        assert branch["sequence"] == [
            {"service": "switch.turn_on", "target": {"entity_id": SWITCH}}]

    def test_the_idle_check_is_not_a_top_level_condition(self):
        """A top-level numeric_state would gate the turn-on too, and a device
        that is off reads zero watts — so it would never come back."""
        assert _built()["config"]["condition"] == []

    def test_mode_is_single(self):
        assert _built()["config"]["mode"] == "single"

    def test_the_config_is_one_home_assistant_would_recognise(self):
        """The three keys _preflight_automation requires, present and of the
        right type before any home is asked."""
        config = _built()["config"]

        assert isinstance(config["alias"], str) and config["alias"]
        assert isinstance(config["trigger"], list)
        assert isinstance(config["action"], list)


# --------------------------------------------------------------------------
class TestItAgreesWithTheDetector:
    """The hours and the ceiling are imported, never restated. These assertions
    are written against standby's constants on purpose: a copy here would pass
    while the two modules disagreed, which is the failure being guarded."""

    def test_the_hours_are_the_detectors_night(self):
        triggers = _triggers(_built()["config"])

        assert triggers[builder.TRIGGER_OFF]["at"] == f"{standby.NIGHT.start_hour:02d}:00:00"
        assert triggers[builder.TRIGGER_ON]["at"] == f"{standby.NIGHT.end_hour:02d}:00:00"

    def test_a_window_that_names_its_weekdays_is_refused_not_fired_every_day(
            self, monkeypatch):
        """This shape has nowhere to put the days. A config that ignored the
        five it was given would be replayed over occurrences it would never
        have fired on, and proposed with a saving measured over ones it would.
        """
        monkeypatch.setattr(standby, "NIGHT",
                            window.spec("08:30", "17:00", days=window.WEEKDAYS))

        result, error = builder.build_config(_candidate())

        assert result is None
        assert "Mon-Fri" in error

    def test_a_window_that_crosses_midnight_is_refused_here_not_at_the_grader(
            self, monkeypatch):
        """Home Assistant would run such a rule correctly, but replay.plan_for
        bounds the span by subtracting one clock time from the other and
        refuses a negative one. Built here, the candidate would be discarded at
        grading under a message about the supply being restored before it is
        cut — a true sentence about the config and the wrong explanation.
        """
        monkeypatch.setattr(standby, "NIGHT", window.spec("22:00", "06:00"))

        result, error = builder.build_config(_candidate())

        assert result is None
        assert "crosses midnight" in error

    def test_the_ceiling_is_the_detectors_ceiling(self):
        branch = _branches(_built()["config"])[builder.TRIGGER_OFF]
        guard = next(c for c in branch["conditions"] if c["condition"] == "numeric_state")

        assert guard["below"] == standby.STANDBY_MAX_WATTS

    def test_a_kilowatt_sensor_gets_the_ceiling_in_its_own_units(self):
        """The condition compares the sensor's RAW reading. `below: 30` on a
        sensor reporting kW is always true, and the device goes off mid-film."""
        branch = _branches(_built(scale=1000.0)["config"])[builder.TRIGGER_OFF]
        guard = next(c for c in branch["conditions"] if c["condition"] == "numeric_state")

        assert guard["below"] == pytest.approx(standby.STANDBY_MAX_WATTS / 1000.0)

    def test_the_readback_states_the_ceiling_in_watts_whatever_the_sensor_reports(self):
        """The config's `below` is in the sensor's units; the sentence a person
        approves is in watts. On a kW sensor the two are 0.03 and 30, and
        printing the config's number with a W after it is a figure wrong by a
        thousand in the artefact the whole approval rests on."""
        readback = _built(scale=1000.0)["readback"]

        assert f"less than {standby.STANDBY_MAX_WATTS} W" in readback
        assert "0.03 W" not in readback

    @pytest.mark.parametrize("scale", [0, -1, None, "W", float("nan"), float("inf")])
    def test_a_scale_that_is_not_a_positive_number_produces_nothing(self, scale):
        result, error = builder.build_config(_candidate(), scale=scale)

        assert result is None
        assert "unit" in error

    def test_a_nan_scale_never_reaches_the_config(self):
        """NaN compares False against every operator, so a bare `<= 0` guard
        passes it. `below: NaN` then serialises, json.loads accepts it so
        preflight waves it through, and every comparison against NaN is false —
        an approved automation that silently never switches anything off."""
        result, error = builder.build_config(_candidate(), scale=float("nan"))

        assert result is None and error


# --------------------------------------------------------------------------
class TestWhatAPersonReads:
    def test_the_alias_names_the_device_not_the_machinery(self):
        alias = _built(name="Living room TV")["config"]["alias"]

        assert "Living room TV" in alias
        for machinery in ("standby", "candidate", "suggestion", SENSOR, SWITCH):
            assert machinery not in alias

    def test_without_a_friendly_name_the_entity_id_is_turned_into_words(self):
        """The alias is the one piece of this text seen months later by someone
        who never saw the suggestion — a raw entity id there is a worse label
        than an imperfect one."""
        assert _built()["config"]["alias"].startswith("Living room tv")

    def test_the_description_says_the_agent_wrote_it_and_why(self):
        """The only trace inside Home Assistant itself. Everywhere else the
        reasoning is in the add-on's journal, which someone editing automations
        in HA is not looking at."""
        description = _built(name="Living room TV")["config"]["description"]

        assert "agent" in description.lower()
        assert "12.0 W" in description
        assert "21 of the last 30 nights" in description
        assert standby.KIND not in description.lower().split()

    def test_the_readback_states_the_dead_hours(self):
        """§9 artefact 3. A user who does not know the device is unreachable
        between those hours discovers it at 02:00."""
        readback = _built(name="Living room TV")["readback"]

        assert f"{standby.NIGHT.start_hour:02d}:00" in readback
        assert f"{standby.NIGHT.end_hour:02d}:00" in readback
        assert "remote" in readback
        assert "not respond" in readback

    def test_the_readback_carries_the_evidence_it_was_built_from(self):
        readback = _built()["readback"]

        assert "12.0 W" in readback
        assert "1.01 kWh" in readback

    def test_the_description_reads_as_a_sentence_when_evidence_is_thin(self):
        """Guarding a value but not the preposition around it is how "measured
        continuously between  without being used" gets written into
        automations.yaml — a half-finished sentence in the one place this
        reasoning is visible from inside Home Assistant."""
        thin = _candidate()
        thin["evidence"] = {"entity": SENSOR, "switch": SWITCH}

        description = _built(thin, name="Amp")["config"]["description"]

        assert "  " not in description
        assert "between " not in description
        assert "continuously without being used" in description

    def test_a_candidate_with_no_numbers_still_reads_as_a_sentence(self):
        """The evidence blob is the detector's, and a second detector's may not
        carry every key. The readback drops the claim rather than printing None."""
        thin = _candidate()
        thin["evidence"] = {"entity": SENSOR, "switch": SWITCH}

        readback = _built(thin)["readback"]

        assert "None" not in readback
        assert "not respond" in readback


# --------------------------------------------------------------------------
class TestTheIdIsStable:
    def test_the_same_candidate_twice_produces_the_same_id_and_config(self):
        """The review pass re-derives the same candidate every morning. Home
        Assistant's own ids are milliseconds since the epoch — different every
        time they are asked — which would leave two automations doing the same
        thing in one house."""
        first = _built()
        second = _built()

        assert first["automation_id"] == second["automation_id"]
        assert first["config"] == second["config"]
        assert first["config_json"] == second["config_json"]

    def test_evidence_that_moved_does_not_move_the_id(self):
        """WP4's window slides daily, so every number in the evidence moves.
        The id is derived from the key, and the key is derived from the entity."""
        moved = _built(_candidate(watts=13.4, nights=19, kwh_per_month=2.2))

        assert moved["automation_id"] == _built()["automation_id"]

    def test_different_entities_get_different_ids(self):
        other = _built(_candidate(entity="sensor.amp_power", switch="switch.amp"))

        assert other["automation_id"] != _built()["automation_id"]

    def test_the_id_is_recognisable_as_this_agents(self):
        automation_id = _built()["automation_id"]

        assert automation_id.startswith(builder.ID_PREFIX)
        assert not automation_id.removeprefix(builder.ID_PREFIX).isdigit()

    def test_it_is_derived_from_the_suggestion_key_and_nothing_else(self):
        assert _built()["automation_id"] == builder.automation_id_for(f"{standby.KIND}:{SENSOR}")


# --------------------------------------------------------------------------
class TestWhatItRefuses:
    def test_a_candidate_with_no_switch_produces_no_config(self):
        """WP4 discards these before they become candidates. The rule is stated
        again where the config would be built, so a second detector cannot
        arrive without it."""
        result, error = builder.build_config(_candidate(switch=None))

        assert result is None
        assert "no controllable switch" in error

    def test_a_switch_that_is_not_a_switch_is_refused(self):
        """`switch.turn_off` on a light entity is not a service that exists,
        and the failure would come back as a message about Home Assistant
        rather than about the pairing that was wrong."""
        result, error = builder.build_config(_candidate(switch="light.living_room"))

        assert result is None
        assert "light.living_room" in error

    def test_a_candidate_with_no_sensor_produces_no_config(self):
        result, error = builder.build_config(_candidate(entity=None))

        assert result is None
        assert "power sensor" in error

    @pytest.mark.parametrize("candidate", [None, {}, [], "standby:sensor.tv_power"])
    def test_nothing_shaped_like_a_candidate_produces_nothing(self, candidate):
        result, error = builder.build_config(candidate)

        assert result is None and error

    def test_an_unknown_kind_is_refused_rather_than_given_the_standby_shape(self):
        other = _candidate()
        other["kind"] = "idle_heating"

        result, error = builder.build_config(other)

        assert result is None
        assert "idle_heating" in error


# --------------------------------------------------------------------------
class TestThePreflight:
    """The only Home Assistant traffic this module makes, stubbed the way
    tests/test_ha_automations.py stubs it — at `_validate_by_test_create`,
    the one function that talks to a home."""

    @pytest.fixture
    def ha_accepts(self, monkeypatch):
        seen = []

        def _accept(cfg):
            seen.append(cfg)
            return True, "1748000000000"

        monkeypatch.setattr(automations, "_validate_by_test_create", _accept)
        return seen

    def test_a_valid_config_comes_back_with_the_build(self, ha_accepts):
        out = builder.build(_candidate(), name="Living room TV")

        assert out["ok"] is True
        assert out["automation_id"] == builder.automation_id_for(f"{standby.KIND}:{SENSOR}")
        assert out["config"]["mode"] == "single"
        assert len(ha_accepts) == 1

    def test_the_string_preflighted_is_the_string_returned(self, ha_accepts):
        """The caller hands config_json straight to the gate. If the two were
        serialised separately, one config could be validated and another asked
        about."""
        out = builder.build(_candidate())

        assert json.loads(out["config_json"]) == out["config"]
        assert ha_accepts[0]["alias"] == out["config"]["alias"]
        assert ha_accepts[0]["trigger"] == out["config"]["trigger"]

    def test_a_rejection_is_returned_instead_of_a_config(self, monkeypatch):
        """A config that will not validate is not a suggestion. The caller's
        correct response is to not validate the candidate at all, so it has to
        be told rather than handed something broken."""
        monkeypatch.setattr(automations, "_validate_by_test_create",
                            lambda cfg: (False, "unknown service switch.turn_off"))

        out = builder.build(_candidate())

        assert out["ok"] is False
        assert "unknown service switch.turn_off" in out["error"]
        assert "config" not in out

    def test_a_refusal_before_preflight_never_reaches_home_assistant(self, monkeypatch):
        def _boom(cfg):
            raise AssertionError("preflight ran on a candidate that produced no config")

        monkeypatch.setattr(automations, "_validate_by_test_create", _boom)

        assert builder.build(_candidate(switch=None))["ok"] is False

    def test_the_preflight_does_not_write_at_the_stable_id(self, monkeypatch):
        """The worry the stable id raises: preflight test-creates and then
        deletes, so a preflight running against the real id would delete the
        automation it was checking. It mints its own temporary id instead, and
        these two never touch."""
        paths = []
        monkeypatch.setattr(automations, "_ha_post",
                            lambda path, body=None: (paths.append(path), (True, {}))[1])
        monkeypatch.setattr(automations, "_ha_delete",
                            lambda path: (paths.append(path), (True, {}))[1])

        out = builder.build(_candidate())

        assert out["ok"] is True
        assert paths, "preflight made no request at all"
        assert not any(out["automation_id"] in path for path in paths)


# --------------------------------------------------------------------------
# The second shape: a window that names its weekdays.
#
# Written against schedule.py's own vocabulary and against the per-device
# floor in the candidate's evidence, for the reason the module docstring at the
# top of this file gives: an assertion copied from a constant would pass while
# the builder and the detector disagreed, and here the two do not even share a
# constant to disagree about.
# --------------------------------------------------------------------------
SCHEDULE_FLOOR = 21.5


def _schedule_candidate(entity=SENSOR, switch=SWITCH, start="07:00", end="17:00",
                        days=(0, 1, 2, 3, 4), **evidence):
    """A validated schedule candidate, shaped the way schedule.find_candidate
    returns one — occurrences rather than nights, and the window carried both
    as prose and as a value."""
    spec = window.spec(start, end, days)
    ev = {
        "entity": entity,
        "switch": switch,
        "watts": 8.4,
        "watts_range": [8.1, 9.0],
        "hours_per_occurrence": window.hours(spec),
        "kwh_per_month": 2.63,
        "occurrences_per_month": 19.8,
        "occurrences": 12,
        "occurrences_observed": 12,
        "occurrences_examined": 13,
        "excluded": {},
        "idle_watts": 8.0,
        "active_floor": SCHEDULE_FLOOR,
        "schedule_window": window.label(spec),
        "schedule": {"start": start, "end": end, "days": sorted(spec.days)},
        "thresholds": {"idle_below_w": SCHEDULE_FLOOR, "min_occurrences": 8},
    }
    ev.update(evidence)
    return {"key": f"{schedule.KIND}:{entity}", "kind": schedule.KIND,
            "entities": [entity, switch], "evidence": ev}


def _guard(config, trigger_id):
    branch = _branches(config)[trigger_id]
    return next(c for c in branch["conditions"] if c["condition"] == "numeric_state")


def _weekday_condition(config, trigger_id):
    branch = _branches(config)[trigger_id]
    return next((c for c in branch["conditions"] if c["condition"] == "time"), None)


# --------------------------------------------------------------------------
class TestTheScheduleShape:
    """The weekday restriction goes in the cutting branch, and nowhere else."""

    def test_the_weekdays_restrict_the_cut(self):
        config = _built(_schedule_candidate())["config"]

        assert _weekday_condition(config, builder.SCHEDULE_TRIGGER_OFF) == {
            "condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]}

    def test_the_restoring_branch_stays_unconditional(self):
        """`replay.plan_for` refuses a config whose restoring branch carries a
        guard, and it is right to: a restriction on the turn-on is how a device
        cut on Friday evening stays cut until Monday morning. The unconditional
        daily turn-on is a safety net, not noise."""
        branch = _branches(_built(_schedule_candidate())["config"])[
            builder.SCHEDULE_TRIGGER_ON]

        assert [c["condition"] for c in branch["conditions"]] == ["trigger"]
        assert branch["sequence"] == [
            {"service": "switch.turn_on", "target": {"entity_id": SWITCH}}]

    def test_the_weekdays_are_not_a_top_level_condition(self):
        """A top-level condition gates both branches, which is the same failure
        the night shape's empty `condition: []` exists to avoid — one weekday
        further on."""
        assert _built(_schedule_candidate())["config"]["condition"] == []

    def test_a_cut_only_restriction_is_complete_because_runs_do_not_wrap(self):
        """schedule.py's decision 5: a run stops at local midnight, so both ends
        of every window it proposes fall on the same weekday. There is no
        occurrence whose cut is restricted and whose restore is not."""
        config = _built(_schedule_candidate())["config"]
        triggers = _triggers(config)

        assert (triggers[builder.SCHEDULE_TRIGGER_OFF]["at"]
                < triggers[builder.SCHEDULE_TRIGGER_ON]["at"])

    def test_both_triggers_still_live_in_one_automation(self):
        config = _built(_schedule_candidate())["config"]

        assert set(_triggers(config)) == {builder.SCHEDULE_TRIGGER_OFF,
                                          builder.SCHEDULE_TRIGGER_ON}
        assert all(t["platform"] == "time" for t in config["trigger"])
        assert config["mode"] == "single"

    def test_it_does_not_reuse_the_night_shapes_ids_or_alias(self):
        """"night_start" and "off overnight" are wrong English for a rule that
        cuts power at 07:00 on a Tuesday — and they are in people's
        automations.yaml, where `_parts_of` matches them literally."""
        config = _built(_schedule_candidate(), name="Living room TV")["config"]

        assert builder.TRIGGER_OFF not in _triggers(config)
        assert builder.TRIGGER_ON not in _triggers(config)
        assert not config["alias"].endswith(builder.ALIAS_SUFFIX)
        assert config["alias"] == f"Living room TV{builder.SCHEDULE_ALIAS_SUFFIX}"

    def test_a_window_on_every_weekday_still_states_its_days(self):
        """One line of YAML either way, and it is what lets `_parts_of` tell a
        seven-day rule from one whose restriction was edited away."""
        config = _built(_schedule_candidate(days=range(7)))["config"]

        assert _weekday_condition(config, builder.SCHEDULE_TRIGGER_OFF)["weekday"] == [
            "mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --------------------------------------------------------------------------
class TestTheScheduleGuard:
    """The threshold is this device's own floor, not the global ceiling."""

    def test_the_guard_is_the_devices_own_floor(self):
        """Every one of the candidate's 336 cells was judged against
        `active_floor`. A guard at 30 W beside a card claiming the device is
        idle below 21.5 W would be two claims about one rule."""
        guard = _guard(_built(_schedule_candidate())["config"],
                       builder.SCHEDULE_TRIGGER_OFF)

        assert guard["below"] == SCHEDULE_FLOOR
        assert guard["below"] != standby.STANDBY_MAX_WATTS
        assert guard["entity_id"] == SENSOR

    def test_the_night_shape_keeps_the_global_ceiling(self):
        """The asymmetry is deliberate: moving standby onto a per-device number
        would change automations users have already approved."""
        assert _guard(_built()["config"], builder.TRIGGER_OFF)["below"] == (
            standby.STANDBY_MAX_WATTS)

    def test_a_kilowatt_sensor_gets_the_floor_in_its_own_units(self):
        """`below` is compared against the sensor's RAW reading. `below: 21.5`
        on a kW sensor is a condition that is always true."""
        guard = _guard(_built(_schedule_candidate(), scale=1000.0)["config"],
                       builder.SCHEDULE_TRIGGER_OFF)

        assert guard["below"] == pytest.approx(SCHEDULE_FLOOR / 1000.0)

    def test_the_readback_quotes_the_floor_in_watts_on_a_kilowatt_sensor(self):
        """The config's number and the sentence's number state one threshold in
        two units. Printing the config's with a W after it is a figure wrong by
        a thousand in the artefact the approval rests on."""
        readback = _built(_schedule_candidate(), scale=1000.0)["readback"]

        assert f"less than {SCHEDULE_FLOOR} W" in readback
        assert "0.0215 W" not in readback

    def test_the_thresholds_blob_answers_when_the_level_does_not(self):
        candidate = _schedule_candidate()
        candidate["evidence"].pop("active_floor")

        guard = _guard(_built(candidate)["config"], builder.SCHEDULE_TRIGGER_OFF)

        assert guard["below"] == SCHEDULE_FLOOR

    @pytest.mark.parametrize("floor", [None, 0, -4, "idle", float("nan")])
    def test_no_usable_floor_is_a_refusal_not_the_global_ceiling(self, floor):
        """There is no honest constant to fall back to. `STANDBY_MAX_WATTS` is
        a claim about every device in the house, and writing it here would be a
        threshold nothing in the evidence beside it measured against."""
        candidate = _schedule_candidate(active_floor=floor)
        candidate["evidence"]["thresholds"] = {}

        result, error = builder.build_config(candidate)

        assert result is None
        assert "in-use level" in error


# --------------------------------------------------------------------------
class TestTheScheduleWindow:
    """Rebuilt from the evidence, never from a constant."""

    def test_the_triggers_are_the_windows_the_evidence_records(self):
        config = _built(_schedule_candidate(start="08:30", end="16:00"))["config"]
        triggers = _triggers(config)

        assert triggers[builder.SCHEDULE_TRIGGER_OFF]["at"] == "08:30:00"
        assert triggers[builder.SCHEDULE_TRIGGER_ON]["at"] == "16:00:00"

    def test_the_weekdays_are_the_ones_the_evidence_records(self):
        config = _built(_schedule_candidate(days=(5, 6)))["config"]

        assert _weekday_condition(config, builder.SCHEDULE_TRIGGER_OFF)["weekday"] == [
            "sat", "sun"]

    def test_the_night_detectors_window_has_no_say_in_it(self, monkeypatch):
        """A proposal raised last week has to describe the automation it would
        actually write. `standby.NIGHT` is a declaration about the house; this
        window is a finding about one device."""
        monkeypatch.setattr(standby, "NIGHT", window.spec("22:00", "23:00"))

        triggers = _triggers(_built(_schedule_candidate())["config"])

        assert triggers[builder.SCHEDULE_TRIGGER_OFF]["at"] == "07:00:00"

    def test_the_prose_label_is_never_parsed_for_the_window(self):
        """`schedule_window` is what a person is shown; `schedule` is the value
        the config is built from. A blob whose prose has gone stale still builds
        the rule its value describes."""
        candidate = _schedule_candidate(schedule_window="nonsense, actually")

        triggers = _triggers(_built(candidate)["config"])

        assert triggers[builder.SCHEDULE_TRIGGER_OFF]["at"] == "07:00:00"

    @pytest.mark.parametrize("blob", [None, {}, "07:00-17:00", [7, 17],
                                      {"start": "07:00"},
                                      {"start": "07:00", "end": "17:00", "days": []},
                                      {"start": "07:00", "end": "17:00", "days": "mon"},
                                      {"start": "0700", "end": "17:00", "days": [0]},
                                      {"start": "07:00", "end": "07:00", "days": [0]}])
    def test_a_blob_that_does_not_describe_a_window_is_a_refusal(self, blob):
        """Missing, malformed, or a window core/window.py refuses outright —
        all three come back as a sentence rather than as a traceback out of the
        daily pass or an approximation nobody measured."""
        result, error = builder.build_config(_schedule_candidate(schedule=blob))

        assert result is None
        assert error and "window" in error

    def test_a_window_crossing_midnight_is_still_refused(self):
        """schedule.py's runs cannot wrap, so this cannot arise from that
        detector. It is the belt for a candidate arriving from somewhere else:
        `window.spec("00:00", "00:00")` raises, and a traceback out of the
        review pass takes every other suggestion due that morning with it."""
        result, error = builder.build_config(
            _schedule_candidate(schedule={"start": "22:00", "end": "06:00",
                                          "days": [0, 1]}))

        assert result is None
        assert "crosses midnight" in error


# --------------------------------------------------------------------------
class TestWhatAPersonReadsOnASchedule:
    def test_the_readback_names_the_weekdays_and_the_hours(self):
        readback = _built(_schedule_candidate(), name="Living room TV")["readback"]

        assert "Mon-Fri" in readback
        assert "07:00" in readback and "17:00" in readback
        assert "10 hours" in readback

    def test_the_readback_says_plainly_that_the_device_will_not_work(self):
        """§6: the daytime window is the riskier one, because a wrong cut at
        11:00 on a Tuesday meets somebody who is at home. Idle is observable;
        needed is not."""
        readback = _built(_schedule_candidate(), name="Living room TV")["readback"]

        assert "genuinely off" in readback
        assert "not respond" in readback
        assert "at home" in readback

    def test_the_readback_carries_the_evidence_it_was_built_from(self):
        readback = _built(_schedule_candidate())["readback"]

        assert "8.4 W" in readback
        assert "2.63 kWh" in readback
        assert f"less than {SCHEDULE_FLOOR} W" in readback

    def test_a_seven_day_rule_does_not_list_seven_names(self):
        readback = _built(_schedule_candidate(days=range(7)))["readback"]

        assert "every day" in readback
        assert "Mon-Sun" not in readback

    def test_the_description_speaks_in_occurrences_not_nights(self):
        """schedule.py's decision 6: "nights" would be a lie about a window that
        happens at 11:00 on a Tuesday, and a description reading both sets of
        names is where the two vocabularies quietly become one."""
        description = _built(_schedule_candidate(),
                             name="Living room TV")["config"]["description"]

        assert "agent" in description.lower()
        assert "8.4 W" in description
        assert "12 of the last 13 occurrences" in description
        assert "07:00-17:00 local Mon-Fri" in description
        assert "night" not in description.lower()

    def test_the_description_reads_as_a_sentence_when_evidence_is_thin(self):
        thin = _schedule_candidate()
        thin["evidence"] = {"entity": SENSOR, "switch": SWITCH,
                            "active_floor": SCHEDULE_FLOOR,
                            "schedule": {"start": "07:00", "end": "17:00",
                                         "days": [0, 1, 2, 3, 4]}}

        built = _built(thin, name="Amp")

        assert "  " not in built["config"]["description"]
        assert "across " not in built["config"]["description"]
        assert "continuously without being used" in built["config"]["description"]
        assert "None" not in built["readback"]


# --------------------------------------------------------------------------
class TestReadingBothShapesBack:
    """`_parts_of` and `readback` over a STORED config — which is what WP8's
    surfaces have days after the proposal was raised."""

    def test_a_stored_night_config_still_round_trips(self):
        built = _built(name="Living room TV")

        again = builder.readback(built["config"], _candidate()["evidence"])

        assert again == built["readback"]

    def test_a_stored_schedule_config_round_trips(self):
        candidate = _schedule_candidate()
        built = _built(candidate, name="Living room TV")

        again = builder.readback(built["config"], candidate["evidence"])

        assert again == built["readback"]
        assert "Mon-Fri" in again

    def test_the_weekdays_are_read_back_out_of_the_config(self):
        """Not out of the evidence: the config is what will actually be
        written, and a stored blob whose window moved is the case this
        distinction exists for."""
        candidate = _schedule_candidate(days=(5, 6))
        config = _built(candidate)["config"]
        candidate["evidence"]["schedule"]["days"] = [0, 1, 2, 3, 4]

        assert "Sat, Sun" in builder.readback(config, candidate["evidence"])

    def test_a_schedule_config_whose_evidence_lost_its_floor_still_reads(self):
        """The clause states the guard without a number rather than dropping
        the guard: "it is left alone if it is still in use" is the part being
        agreed to, and the number is not what makes it true."""
        config = _built(_schedule_candidate())["config"]

        readback = builder.readback(config, {"watts": 8.4})

        assert "None" not in readback
        assert "still in use" in readback
        assert "not respond" in readback

    def test_a_caller_supplied_threshold_wins_for_either_shape(self):
        """core/replay.py graded the config and recorded `guard.below_w` — the
        source that knows THIS config best."""
        night = builder.readback(_built()["config"], {}, ceiling_w=12.0)
        weekly = builder.readback(_built(_schedule_candidate())["config"], {},
                                  ceiling_w=12.0)

        assert "less than 12.0 W" in night
        assert "less than 12.0 W" in weekly

    @pytest.mark.parametrize("config", [
        None, {}, "automation", {"alias": "Hallway light", "trigger": []},
        {"alias": "Something else", "trigger": [{"platform": "time", "at": "07:00:00",
                                                 "id": "morning"}]},
    ])
    def test_a_config_this_module_did_not_write_gets_no_readback(self, config):
        assert builder.readback(config, {}) == ""


# --------------------------------------------------------------------------
class TestTheTwoShapesShareADevice:
    def test_one_device_can_carry_both_without_collision(self):
        """`schedule:<entity>` and `standby:<entity>` are different keys, so a
        television with both a night plateau and a weekday one gets two
        automations rather than one overwriting the other."""
        night = _built()
        weekly = _built(_schedule_candidate())

        assert night["key"] != weekly["key"]
        assert night["automation_id"] != weekly["automation_id"]
        assert weekly["automation_id"] == builder.automation_id_for(
            f"{schedule.KIND}:{SENSOR}")

    def test_the_kind_travels_with_the_result(self):
        assert _built(_schedule_candidate())["kind"] == schedule.KIND
        assert _built()["kind"] == standby.KIND

    def test_a_candidate_with_no_key_falls_back_to_its_own_kind(self):
        candidate = _schedule_candidate()
        candidate.pop("key")

        assert _built(candidate)["key"] == f"{schedule.KIND}:{SENSOR}"

    def test_the_endpoints_are_read_the_same_way_for_either_detector(self):
        """Both detectors write the same two roles into the evidence, and
        `_endpoints` reads them rather than the entity list's order."""
        candidate = _schedule_candidate()
        candidate["entities"] = ["switch.wrong_way_round", "sensor.wrong_way_round"]

        guard = _guard(_built(candidate)["config"], builder.SCHEDULE_TRIGGER_OFF)

        assert guard["entity_id"] == SENSOR

    def test_a_schedule_candidate_with_no_switch_produces_no_config(self):
        result, error = builder.build_config(_schedule_candidate(switch=None))

        assert result is None
        assert "no controllable switch" in error

    def test_a_schedule_candidate_whose_switch_is_not_one_is_refused(self):
        result, error = builder.build_config(
            _schedule_candidate(switch="light.living_room"))

        assert result is None
        assert "light.living_room" in error

    @pytest.mark.parametrize("scale", [0, -1, None, "W", float("nan")])
    def test_a_scale_that_is_not_a_positive_number_produces_nothing(self, scale):
        result, error = builder.build_config(_schedule_candidate(), scale=scale)

        assert result is None
        assert "unit" in error


# --------------------------------------------------------------------------
class TestTheSchedulePreflight:
    """`condition: time` with a `weekday:` list is valid Home Assistant, and
    the preflight is what proves it — the same test-create the night shape
    goes through, against the same stub."""

    def test_a_schedule_config_preflights_like_any_other(self, monkeypatch):
        seen = []
        monkeypatch.setattr(automations, "_validate_by_test_create",
                            lambda cfg: (seen.append(cfg), (True, "1748000000000"))[1])

        out = builder.build(_schedule_candidate(), name="Living room TV")

        assert out["ok"] is True
        assert out["automation_id"] == builder.automation_id_for(
            f"{schedule.KIND}:{SENSOR}")
        assert len(seen) == 1
        assert seen[0]["action"] == out["config"]["action"]

    def test_the_string_preflighted_is_the_string_returned(self, monkeypatch):
        monkeypatch.setattr(automations, "_validate_by_test_create",
                            lambda cfg: (True, "1748000000000"))

        out = builder.build(_schedule_candidate())

        assert json.loads(out["config_json"]) == out["config"]

    def test_a_refusal_before_preflight_never_reaches_home_assistant(self, monkeypatch):
        def _boom(cfg):
            raise AssertionError("preflight ran on a candidate that produced no config")

        monkeypatch.setattr(automations, "_validate_by_test_create", _boom)

        assert builder.build(_schedule_candidate(schedule=None))["ok"] is False


# --------------------------------------------------------------------------
class TestTheShapeIsReadBackFromTheTriggers:
    """Which renderer a stored config gets is decided by its pair of trigger
    ids and by nothing else. Deciding from the weekday condition instead put
    the wrong threshold in the sentence the approval rests on."""

    def test_a_hand_edited_night_config_still_reads_as_a_night_rule(self):
        """A user who adds a `condition: time` to the cutting branch in Home
        Assistant has not turned the rule into a schedule. Rendered as one, the
        30 W ceiling drops out of the sentence entirely — standby evidence
        carries no per-device floor for `_floor_from` to find."""
        config = _built(name="Living room TV")["config"]
        branch = _branches(config)[builder.TRIGGER_OFF]
        branch["conditions"].insert(1, {"condition": "time", "weekday": ["mon", "tue"]})

        readback = builder.readback(config, _candidate()["evidence"])

        assert f"less than {standby.STANDBY_MAX_WATTS} W" in readback
        assert "every night" in readback
        assert "Mon, Tue" not in readback

    def test_a_schedule_config_that_lost_its_weekdays_keeps_its_own_threshold(self):
        """The mirror case, and the worse one: rendered as a night rule it
        would quote the global 30 W ceiling for a rule guarded at the device's
        own floor — a threshold the config does not contain."""
        candidate = _schedule_candidate()
        config = _built(candidate)["config"]
        branch = _branches(config)[builder.SCHEDULE_TRIGGER_OFF]
        branch["conditions"] = [c for c in branch["conditions"]
                                if c["condition"] != "time"]

        readback = builder.readback(config, candidate["evidence"])

        assert f"less than {SCHEDULE_FLOOR} W" in readback
        assert f"less than {standby.STANDBY_MAX_WATTS} W" not in readback
        assert "every night" not in readback


# --------------------------------------------------------------------------
class TestTwoWaysAScheduleConfigCouldClaimMoreThanWasMeasured:
    def test_a_blob_that_does_not_name_its_days_is_refused_not_widened(self):
        """Defaulting to every day is not a neutral reading of a truncated
        blob. It cuts the supply on the two days the evidence never looked at,
        beside a kWh figure `occurrences_per_month` scaled by the five it did
        name."""
        result, error = builder.build_config(
            _schedule_candidate(schedule={"start": "07:00", "end": "17:00"}))

        assert result is None
        assert "which days" in error

    def test_a_floor_that_rounds_away_is_refused_not_written_as_zero(self):
        """`below: 0.0` is a condition that is never true. It serialises,
        preflights cleanly, and leaves the user with an approved automation
        that silently never switches anything off."""
        result, error = builder.build_config(
            _schedule_candidate(active_floor=0.0004,
                                thresholds={"idle_below_w": 0.0004}),
            scale=1000.0)

        assert result is None
        assert "too small" in error

    def test_the_same_floor_is_fine_on_a_watt_sensor(self):
        """The refusal is about what survives the conversion, not about the
        level: the identical candidate on a W sensor writes a real threshold."""
        guard = _guard(_built(_schedule_candidate(active_floor=0.0004))["config"],
                       builder.SCHEDULE_TRIGGER_OFF)

        assert guard["below"] == pytest.approx(0.0004)
