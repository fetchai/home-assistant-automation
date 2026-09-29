"""The verification pass: §7.2's honest part, and the four decisions it owns.

Everything core/verify.py DECIDES is a pure function over a suggestion row and
a list of transitions, and that is what the first half of this file exercises —
as literals, no database, no Home Assistant. The reason is WP4's and WP5's: a
rule reachable only through a store is a rule that cannot be argued with, and
these are the rules that decide whether a number is put in front of a person.

The second half is the loop closing, with nothing between the modules stubbed
except Home Assistant itself: the record is planted for real, the detector
finds the waste, the pass proposes it, the gate applies it, the baseline is
measured, a week of "after" is planted, and the report comes out with a range
in it. That is design §13's Phase 2 done when — "a real home receives a real
suggestion, applies it, and gets a measured result a week later".
"""
from datetime import datetime, timezone
import json
import time

import pytest

import conftest
from core import activity
from core import artefacts
from core import gate
from core import observer
from core import record
from core import review
from core import rollback
from core import suggestions
from core import verify
from core import watchlist
from core import window
from core.detectors import schedule
from core.detectors import standby
from tools.tool_homeassistant import automations, registry


DAY = 86400
HEARTBEAT = 600  # comfortably inside observer.NUMERIC_MAX_GAP_SECONDS

# Noon on a fixed day, so every night window below is arithmetic rather than a
# function of when the suite happens to run. Noon is past REVIEW_HOUR, which
# the pipeline tests lean on.
NOW = int(datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc).timestamp())
WINDOW_START = NOW - standby.LOOKBACK_DAYS * DAY

SENSOR = "sensor.tv_power"
SWITCH = "switch.tv"
KEY = f"{standby.KIND}:{SENSOR}"
AUTOMATION_ID = "1748000000000"


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Pins the process timezone. Night windows are local by design, so without
    this the tests drift with the developer's zone."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# --------------------------------------------------------------------------
# literals of the two shapes everything here reads
# --------------------------------------------------------------------------
def _rows(state="12.0", start=None, end=None):
    """A heartbeat row every ten minutes, the way NUMERIC_MAX_GAP_SECONDS
    leaves one on a value that never moves. `state` may be a callable."""
    start = NOW - verify.AFTER_DAYS * DAY if start is None else start
    end = NOW if end is None else end
    return [{"ts": ts, "state": str(state(ts) if callable(state) else state),
             "source": "live"}
            for ts in range(int(start), int(end), HEARTBEAT)]


def _week(state="12.0", end=None, days=None):
    """One measured week, of the shape both halves of a comparison have."""
    end = NOW if end is None else end
    days = verify.AFTER_DAYS if days is None else days
    return verify.measure(_rows(state, end - days * DAY, end), end, days)


def _row(**overrides):
    """An applied suggestion, of the shape core/suggestions.py hands back."""
    row = {
        "id": 1,
        "key": KEY,
        "kind": standby.KIND,
        "state": "applied",
        "change_id": 7,
        "entities": [SENSOR, SWITCH],
        "evidence": {"entity": SENSOR, "switch": SWITCH, "watts": 12.0,
                     "night_window": standby.night_window_label()},
        "estimate": {"basis": "replay", "kwh_per_month": 1.44, "watts": 12.0,
                     "hours_per_night": 4.0, "nights_per_month": 30.0},
        "replay": {"nights_saved": 30, "nights_wrong": 0, "nights_examined": 30},
        "config": {"alias": "TV socket off overnight"},
        "baseline": _week("12.0"),
        "verify_after": NOW,
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# measuring a week, and what an empty one means
# --------------------------------------------------------------------------
class TestMeasuringOneWeek:
    """The same statistic on both sides, and never a second implementation."""

    def test_it_is_the_detectors_own_arithmetic(self):
        """Not "the same idea as" — literally the same call, so the before and
        the after cannot drift apart."""
        end, days = NOW, verify.AFTER_DAYS
        rows = _rows("12.0", end - days * DAY, end)
        nights = standby.night_windows(end - days * DAY, end)

        measured = verify.measure(rows, end, days)

        assert measured["watts"] == standby.measure_nights(rows, nights)["watts"]

    def test_a_week_of_standby_is_read_as_such(self):
        week = _week("12.0")

        assert week["watts"] == 12.0
        assert week["nights"] == week["nights_examined"] > 0
        assert verify.readable(week) is True

    def test_a_week_the_automation_worked_on_qualifies_no_nights_and_is_still_read(self):
        """THE distinction the whole module rests on. A working automation
        switches the device off, so no night is a plateau — and the nights are
        excluded as `below_floor`, which counted them as OBSERVED. A floor on
        `nights` rather than `nights_observed` would declare every success
        unmeasurable."""
        week = _week("0.0")

        assert week["nights"] == 0
        assert week["watts"] is None
        assert week["excluded"] == {standby.REASON_BELOW_FLOOR: week["nights_examined"]}
        assert verify.readable(week) is True

    def test_a_week_nobody_watched_is_not_read(self):
        assert verify.readable(verify.measure([], NOW, verify.AFTER_DAYS)) is False

    def test_and_neither_is_one_with_only_a_night_or_two_in_it(self):
        """A majority of the week on each side, or it is not half of a
        comparison."""
        week = verify.measure(_rows("12.0", NOW - 2 * DAY, NOW), NOW, verify.AFTER_DAYS)

        assert 0 < week["nights_observed"] < verify.MIN_NIGHTS_READ
        assert verify.readable(week) is False

    def test_the_window_it_covers_is_stated_rather_than_left_to_be_derived(self):
        """§7.2: state the comparison window. Both halves carry their own."""
        week = _week("12.0")

        assert week["window"]["days"] == verify.AFTER_DAYS
        assert week["window"]["to"] == "2026-06-30"
        assert week["night_window"] == standby.night_window_label()

    def test_both_sides_are_the_same_length_of_week(self):
        """"Compare like periods" as a property of the constants rather than a
        promise: a thirty-day before against a seven-day after is a difference
        in the measurement, not in the home."""
        assert verify.BASELINE_DAYS == verify.AFTER_DAYS
        assert review.BASELINE_DAYS == review.VERIFY_AFTER_DAYS == verify.AFTER_DAYS


# --------------------------------------------------------------------------
# 1. the noise floor
# --------------------------------------------------------------------------
class TestTheNoiseFloor:
    """§7.2: give a range, or say the effect is too small to separate from
    noise. This is the line between the two."""

    def test_it_is_twice_the_smallest_movement_the_record_writes_down(self):
        """Two levels closer than the record's own resolution are one level it
        could not tell apart, and a difference of two such numbers carries that
        uncertainty twice."""
        assert verify.NOISE_FLOOR_WATTS == 2 * observer.NUMERIC_MIN_ABS_CHANGE

    def test_which_is_also_what_a_successful_week_can_still_be_hiding(self):
        """A night excluded as `below_floor` dropped under
        standby.STANDBY_MIN_WATTS — it did not reach zero. The residual is
        therefore bounded by the same number, which is why it is carried by the
        floor rather than added to the after side a second time."""
        assert verify.NOISE_FLOOR_WATTS == standby.STANDBY_MIN_WATTS

    def test_on_the_kwh_axis_it_is_sized_to_the_claim_being_guarded(self):
        """A device seen on ten nights of the month claims a third as much as
        one seen on thirty, so a fixed kWh floor would wave the first through
        and stop the second."""
        often = verify.floor_kwh({"hours_per_day": 4, "nights_per_month": 30.0})
        rarely = verify.floor_kwh({"hours_per_day": 4, "nights_per_month": 10.0})

        assert often == pytest.approx(verify.NOISE_FLOOR_WATTS * 4 * 30 / 1000.0)
        assert rarely == pytest.approx(often / 3)

    def test_a_baseline_that_says_nothing_falls_back_to_the_detectors_projection(self):
        assert verify.floor_kwh({}) == pytest.approx(
            verify.NOISE_FLOOR_WATTS * standby.NIGHT_HOURS
            * standby.PROJECTION_DAYS_PER_MONTH / 1000.0)


class TestTheIntervalAWeekAmountsTo:
    def test_a_measured_week_is_its_watts_range_over_its_own_nights(self):
        week = _week("12.0")

        low, high = verify.kwh_interval(week)

        assert low == pytest.approx(
            week["watts_range"][0] * week["hours_per_day"] * week["nights_per_month"] / 1000.0)
        assert high >= low

    def test_a_week_the_device_was_off_on_is_zero_and_not_unknown(self):
        assert verify.kwh_interval(_week("0.0")) == [0.0, 0.0]

    def test_a_week_nobody_read_is_unknown_and_not_zero(self):
        """The difference between "could not measure" and "saved nothing", in
        one return value."""
        assert verify.kwh_interval(verify.measure([], NOW, verify.AFTER_DAYS)) is None


# --------------------------------------------------------------------------
# the judgement
# --------------------------------------------------------------------------
class TestComparingTheTwoWeeks:
    def test_a_device_that_stopped_drawing_is_a_saving_with_a_range(self):
        verdict = verify.compare(_row(), _rows("0.0"), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_SAVED
        low, high = verdict["saved_range_kwh_per_month"]
        assert 0 < low <= high
        assert verdict["saved_watts"] == 12.0

    def test_and_the_range_is_never_replaced_by_a_single_confident_number(self):
        """§7.2's whole objection: a figure the user can disprove damages trust
        faster than reporting nothing.

        A device whose draw never wavered has no spread at all, so the interval
        would collapse to one figure — which is why it is widened by the floor,
        the amount the difference is uncertain by. The midpoint is recorded and
        the rendering may not print it alone.
        """
        verdict = verify.compare(_row(), _rows("0.0"), NOW, now=NOW)
        low, high = verdict["saved_range_kwh_per_month"]

        assert low < verdict["saved_kwh_per_month"] < high
        assert high - low >= 2 * verdict["floor_kwh_per_month"]
        assert _range_text(verdict) in artefacts.verification_result({"verdict": verdict})

    def test_a_week_that_looks_the_same_is_reported_as_no_change(self):
        verdict = verify.compare(_row(), _rows("12.0"), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_NO_CHANGE

    def test_a_difference_inside_the_floor_is_no_change_too(self):
        """Half a watt is under the record's own resolution. Two weeks that
        differ by it differ by nothing anybody can demonstrate."""
        verdict = verify.compare(_row(), _rows("11.6"), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_NO_CHANGE

    def test_but_a_device_drawing_measurably_more_is_said_so_out_loud(self):
        """Not rounded away into "no change": this is where the user most wants
        the rollback, and an agent that only reported its wins would be grading
        its own homework."""
        verdict = verify.compare(_row(), _rows("20.0"), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_WORSE

    def test_a_week_that_could_not_be_read_is_never_a_saving_of_everything(self):
        verdict = verify.compare(_row(), [], NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_UNMEASURED
        assert verdict["saved_kwh_per_month"] is None
        assert verdict["saved_range_kwh_per_month"] is None

    def test_and_says_how_much_of_the_week_it_could_read(self):
        """§9: the observation travels with the number, including when there is
        no number."""
        verdict = verify.compare(_row(), [], NOW, now=NOW)

        assert str(verify.MIN_NIGHTS_READ) in verdict["reason"]
        assert "could be read" in verdict["reason"]

    def test_a_baseline_that_was_never_measured_is_not_a_saving_of_nothing(self):
        verdict = verify.compare(
            _row(baseline=verify.measure([], NOW - verify.AFTER_DAYS * DAY,
                                         verify.AFTER_DAYS)),
            _rows("0.0"), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_NO_BASELINE
        assert verdict["saved_range_kwh_per_month"] is None

    def test_two_weeks_measured_over_different_hours_are_not_compared_at_all(self):
        """"Compare like periods" as a check rather than an assumption. The
        night window is one of the three constants the detector's own docstrings
        name as least settled."""
        moved = dict(_week("12.0"), night_window="00:00-06:00 local")

        verdict = verify.compare(_row(baseline=moved), _rows("0.0"), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_NO_BASELINE
        assert "01:00-05:00 local" in verdict["reason"]

    def test_the_verdict_states_both_windows(self):
        verdict = verify.compare(_row(), _rows("0.0"), NOW, now=NOW)

        assert verdict["before"]["window"]["days"] == verify.BASELINE_DAYS
        assert verdict["after"]["window"]["days"] == verify.AFTER_DAYS
        assert verdict["after"]["window"]["to"] == "2026-06-30"

    def test_and_carries_whose_estimate_it_is_being_compared_against(self):
        """§7.1 spent a module keeping the two apart; a report naming the wrong
        side would give it all back."""
        verdict = verify.compare(_row(), _rows("0.0"), NOW, now=NOW)

        assert verdict["estimate_basis"] == "replay"
        assert verdict["estimate_kwh_per_month"] == 1.44

    def test_a_detector_sourced_estimate_is_carried_as_the_detectors(self):
        row = _row(estimate={"basis": "detector", "kwh_per_month": 1.2})

        verdict = verify.compare(row, _rows("0.0"), NOW, now=NOW)

        assert verdict["estimate_basis"] == "detector"

    def test_nothing_in_the_judgement_touches_a_store_or_a_clock(self):
        """Pure, so a week can be stated as a literal — which is the only way
        the rules above are exercisable at all."""
        verdict = verify.compare(_row(), _rows("0.0"), NOW, now=NOW)

        assert verdict["verified_at"] == NOW


def _range_text(verdict):
    low, high = verdict["saved_range_kwh_per_month"]
    return f"between {round(low, 2)} and {round(high, 2)} kWh a month"


class TestTryingAgainLater:
    """Decision 1's second half: one of the two weeks can change, and one
    cannot."""

    def test_an_unread_week_is_worth_measuring_again(self):
        assert verify.should_retry({"outcome": verify.OUTCOME_UNMEASURED, "attempts": 1}) is True

    def test_but_not_forever(self):
        """A suggestion that never leaves `applied` is one §12 can never
        count."""
        assert verify.should_retry({"outcome": verify.OUTCOME_UNMEASURED,
                                    "attempts": verify.MAX_ATTEMPTS}) is False

    def test_a_missing_baseline_is_never_retried(self):
        """The before is a photograph taken at apply time and the automation
        has been running over the record ever since. There is nothing to wait
        for."""
        assert verify.should_retry({"outcome": verify.OUTCOME_NO_BASELINE, "attempts": 1}) is False

    def test_and_neither_is_a_measurement_that_happened(self):
        for outcome in (verify.OUTCOME_SAVED, verify.OUTCOME_NO_CHANGE, verify.OUTCOME_WORSE):
            assert verify.should_retry({"outcome": outcome, "attempts": 1}) is False

    def test_the_retry_is_another_like_period(self):
        """Lengthening the window instead would stop it being a comparison with
        the seven-night baseline."""
        assert verify.RETRY_AFTER_DAYS == verify.AFTER_DAYS


# --------------------------------------------------------------------------
# 2. was the rule even running
# --------------------------------------------------------------------------
def _config(at_off="01:00:00", at_on="05:00:00", below=30.0, switch=SWITCH, sensor=SENSOR):
    """A config of the shape core/builder.py writes, which is the only shape
    core/replay.py will parse."""
    return {
        "alias": "TV socket off overnight",
        "description": "",
        "trigger": [{"platform": "time", "at": at_off, "id": "night_start"},
                    {"platform": "time", "at": at_on, "id": "night_end"}],
        "condition": [],
        "action": [{"choose": [
            {"conditions": [{"condition": "trigger", "id": "night_start"},
                            {"condition": "numeric_state", "entity_id": sensor,
                             "below": below}],
             "sequence": [{"service": "switch.turn_off", "target": {"entity_id": switch}}]},
            {"conditions": [{"condition": "trigger", "id": "night_end"}],
             "sequence": [{"service": "switch.turn_on", "target": {"entity_id": switch}}]},
        ]}],
        "mode": "single",
    }


def _change(**overrides):
    change = {"id": 7, "status": "applied", "decision": "approved",
              "tool": "Create Home Assistant automation",
              "reversibility": "reversible",
              "inverse": {"op": "delete_automation", "automation_id": AUTOMATION_ID}}
    change.update(overrides)
    return change


class TestWhetherTheRuleWasRunning:
    """A week measured while the automation was off, edited or gone is not a
    verification of anything."""

    def test_an_untouched_automation_is_nothing_to_report(self):
        assert verify.still_running(_change(), _config(), True, _config(), "on") == ("", "")

    def test_home_assistants_own_additions_are_not_an_edit(self):
        """It adds `id` on the way out and fills in mode/description/condition
        on the way in. A dict comparison would report an edit every single
        time, and a check that always fires is a check nobody keeps."""
        live = dict(_config(), id=AUTOMATION_ID)

        assert verify.still_running(_change(), _config(), True, live, "on") == ("", "")

    def test_a_moved_trigger_time_is(self):
        gone, not_running = verify.still_running(
            _change(), _config(), True, _config(at_off="02:00:00"), "on")

        assert gone == ""
        assert "edited" in not_running

    def test_so_is_a_moved_threshold_or_a_different_switch(self):
        for edited in (_config(below=5.0), _config(switch="switch.hifi")):
            _, not_running = verify.still_running(_change(), _config(), True, edited, "on")
            assert "edited" in not_running

    def test_a_config_edited_into_something_unrecognisable_is_an_edit_too(self):
        _, not_running = verify.still_running(
            _change(), _config(), True, {"alias": "something else"}, "on")

        assert "edited" in not_running

    def test_a_disabled_automation_did_not_run(self):
        gone, not_running = verify.still_running(_change(), _config(), True, _config(), "off")

        assert gone == ""
        assert "switched off" in not_running

    def test_but_an_unavailable_one_is_not_evidence_of_anything(self):
        """Home Assistant drops these while it is starting. The measurement
        says what it found rather than this claiming the rule was off."""
        assert verify.still_running(
            _change(), _config(), True, _config(), "unavailable") == ("", "")

    def test_a_deleted_automation_leaves_nothing_to_keep_or_roll_back(self):
        gone, not_running = verify.still_running(_change(), _config(), False, None, "")

        assert not_running == ""
        assert "no longer in Home Assistant" in gone

    def test_but_one_failed_read_alone_never_claims_that(self):
        """A single failed GET is as likely to be a slow Home Assistant as a
        missing automation, and retiring a suggestion over one is the silent
        loss §11 exists to prevent. The entity is plainly there and on."""
        assert verify.still_running(_change(), _config(), False, None, "on") == ("", "")

    def test_a_change_already_rolled_back_is_not_verified_at_all(self):
        gone, _ = verify.still_running(_change(undone_by=9), _config(), True, _config(), "on")

        assert "rolled back as change #9" in gone

    def test_a_change_with_no_recorded_automation_is_reported_not_retired(self):
        """The one case core/gate.py cannot record an id for. Nothing can be
        checked and nothing can be undone — but the user still has a decision,
        so it is a report rather than a silent withdrawal."""
        gone, not_running = verify.still_running(
            _change(inverse=None), _config(), True, None, "")

        assert gone == ""
        assert "no record of which automation" in not_running


class TestReadingASuggestionsEntities:
    """`measure_week` is on the path that must always answer with a blob:
    core/review.py's `settle` calls it with an automation already live in
    someone's home, and anything that raises there leaves the suggestion at
    `proposed` with no baseline at all."""

    def test_the_switch_is_read_off_the_evidence(self):
        assert verify._switch_of(_row()) == SWITCH

    def test_a_row_carrying_one_entity_has_no_switch_rather_than_raising(self):
        one = {"entities": [SENSOR], "evidence": {"entity": SENSOR}}

        assert verify._switch_of(one) is None
        assert verify.measure_week(one, NOW, days=verify.AFTER_DAYS,
                                   snapshot={"states": {}, "entities": []})["switch"] is None

    def test_and_neither_does_a_row_carrying_none(self):
        assert verify._switch_of({}) is None
        assert verify._switch_of({"entities": []}) is None


class TestFindingTheAutomationsSwitch:
    def test_it_is_matched_on_the_config_id_in_the_attributes(self):
        """Home Assistant names the entity after the alias and keeps the config
        id in an attribute, so the journal's id cannot be looked up directly."""
        states = {"automation.tv_socket_off_overnight":
                  {"state": "on", "attributes": {"id": AUTOMATION_ID}},
                  "light.kitchen": {"state": "on", "attributes": {"id": AUTOMATION_ID}}}

        assert verify.automation_state(states, AUTOMATION_ID) == "on"

    def test_and_an_automation_nothing_carries_is_simply_absent(self):
        assert verify.automation_state({}, AUTOMATION_ID) == ""
        assert verify.automation_state(None, None) == ""


# --------------------------------------------------------------------------
# the second window: literals of a suggestion whose window names its weekdays
# --------------------------------------------------------------------------
# Nothing produces one of these yet — core/review.py still calls `standby.scan`
# and only that — so everything below is stated as a literal, which is what the
# pure half of core/verify.py is for.
OFFICE_SENSOR = "sensor.printer_power"
OFFICE_SWITCH = "switch.printer"
OFFICE = window.spec("07:00", "17:00", days=window.WEEKDAYS)
OFFICE_KEY = f"{schedule.KIND}:{OFFICE_SENSOR}"
OFFICE_SHAPE = verify.Shape(schedule.KIND, OFFICE, 30.0)

# This device's own in-use floor, which is the whole of decision 7: it is a
# finding about one printer and there is no constant anywhere that is it.
IDLE_BELOW = 30.0


def _office_rows(idle="15.0", busy="120.0", start=None, end=None):
    """A printer that sits at `idle` all day and is used in the evening.

    The in-use stretch is clear of 07:00-17:00 on purpose. A device still busy
    at 06:55 carries that reading across the window's first minutes, and
    `schedule._cell_verdict` calls the whole occurrence ACTIVE for it — which
    is correct, and is also why the detector would never have proposed this
    window in the first place.
    """
    start = NOW - verify.days_for(_office_row()) * DAY if start is None else start
    end = NOW if end is None else end

    def state(ts):
        return busy if 18 <= datetime.fromtimestamp(ts).hour < 22 else idle
    return _rows(state, start, end)


def _office_evidence(**overrides):
    """The evidence blob core/detectors/schedule.py writes, trimmed to what
    this module reads: the window as a VALUE, and the device's own floor."""
    evidence = {
        "entity": OFFICE_SENSOR,
        "switch": OFFICE_SWITCH,
        "watts": 15.0,
        "hours_per_occurrence": 10,
        "occurrences_per_month": 21.4,
        "idle_watts": 15.0,
        "active_floor": IDLE_BELOW,
        "schedule_window": window.label(OFFICE),
        "schedule": {"start": "07:00", "end": "17:00", "days": sorted(OFFICE.days)},
        "thresholds": {"idle_below_w": IDLE_BELOW},
    }
    evidence.update(overrides)
    return {key: value for key, value in evidence.items() if value is not None}


def _office_week(idle="15.0", end=None, days=None):
    """One measured week of that window, of the shape both halves of its
    comparison have."""
    end = NOW if end is None else end
    days = verify._weeks_for(OFFICE) * 7 if days is None else days
    return verify.measure(_office_rows(idle, start=end - days * DAY, end=end),
                          end, days, shape=OFFICE_SHAPE)


def _office_row(**overrides):
    """An applied schedule suggestion. `baseline` is filled in lazily so the
    literal above can be built from the shape rather than from this."""
    row = {
        "id": 2,
        "key": OFFICE_KEY,
        "kind": schedule.KIND,
        "state": "applied",
        "change_id": 8,
        "entities": [OFFICE_SENSOR, OFFICE_SWITCH],
        "evidence": _office_evidence(),
        "estimate": {"basis": "replay", "kwh_per_month": 3.21, "watts": 15.0,
                     "hours_per_occurrence": 10, "occurrences_per_month": 21.4},
        "replay": {"guard": {"entity": OFFICE_SENSOR, "below": IDLE_BELOW,
                             "below_w": IDLE_BELOW}},
        "config": {"alias": "Printer off on a schedule"},
        "verify_after": NOW,
    }
    row.update(overrides)
    return row


def _applied_office(**overrides):
    """...and the same row with the "before" on it."""
    return _office_row(baseline=_office_week("15.0"), **overrides)


class TestWhatAWeekIsMeasuredOver:
    """Decisions 7 and 8: the shape comes off the suggestion, never off the
    live detector."""

    def test_a_standby_suggestion_is_the_night_the_detector_declares(self):
        """The night is a declaration about the HOUSE, so it may be read off
        the detector — and there is no per-device level to recover, because
        `assess_night` judges against the global ceiling."""
        shape, why_not = verify.shape_of(_row())

        assert why_not == ""
        assert (shape.kind, shape.spec, shape.idle_below) == (
            standby.KIND, standby.NIGHT, None)

    def test_a_row_with_no_kind_at_all_is_read_as_one(self):
        """`kind` is NOT NULL in core/suggestions.py, so the only rows without
        one were written before there was a second shape."""
        shape, _ = verify.shape_of({"evidence": {"entity": SENSOR}})

        assert shape.spec == standby.NIGHT

    def test_a_schedule_suggestion_is_the_window_its_own_evidence_names(self):
        """A FINDING about one device, rebuilt from the stored value through
        the same function that wrote the automation."""
        shape, why_not = verify.shape_of(_office_row())

        assert why_not == ""
        assert shape.kind == schedule.KIND
        assert shape.spec == OFFICE
        assert window.label(shape.spec) == "07:00-17:00 local Mon-Fri"

    def test_a_kind_nothing_here_measures_is_refused_rather_than_read_as_a_night(self):
        """Measuring it as a night would report a week of something nobody
        asked about as a week of this rule."""
        shape, why_not = verify.shape_of(_row(kind="humidity"))

        assert shape is None
        assert "humidity" in why_not

    def test_a_window_whose_weekdays_were_lost_is_refused_and_never_widened(self):
        """`builder._spec_from`'s rule, and this is the reader that would
        otherwise measure the two days the evidence never looked at."""
        row = _office_row(evidence=_office_evidence(
            schedule={"start": "07:00", "end": "17:00"}))

        shape, why_not = verify.shape_of(row)

        assert shape is None
        assert "days" in why_not

    def test_and_so_is_one_that_was_never_recorded(self):
        shape, why_not = verify.shape_of(_office_row(evidence={}))

        assert shape is None
        assert "window it found" in why_not


class TestTheLevelAScheduleWeekIsJudgedAgainst:
    """Decision 7: it comes from what was stored, never from the week being
    measured."""

    def test_it_is_the_one_the_rule_was_graded_with(self):
        assert verify.shape_of(_office_row())[0].idle_below == IDLE_BELOW

    def test_the_graded_threshold_comes_first(self):
        """`replay.guard.below_w` is the level the automation that actually ran
        carries, and the number the grading used — so the grading, the sentence
        the card printed and this measurement are one figure."""
        row = _office_row(replay={"guard": {"below_w": 22.0}},
                          evidence=_office_evidence(active_floor=99.0))

        assert verify.shape_of(row)[0].idle_below == 22.0

    def test_then_the_detectors_own_finding(self):
        row = _office_row(replay={}, evidence=_office_evidence(active_floor=44.0,
                                                              thresholds={}))

        assert verify.shape_of(row)[0].idle_below == 44.0

    def test_and_a_threshold_that_would_classify_nothing_is_not_one(self):
        """`below: NaN` serialises, survives the round trip and then makes
        every comparison against it false."""
        row = _office_row(replay={"guard": {"below_w": float("nan")}},
                          evidence=_office_evidence(active_floor=44.0, thresholds={}))

        assert verify.shape_of(row)[0].idle_below == 44.0

    def test_a_level_nobody_recorded_is_refused_rather_than_guessed(self):
        """There is no global schedule floor to fall back to, which is
        `builder._floor_from`'s whole point."""
        row = _office_row(replay={}, evidence=_office_evidence(active_floor=None,
                                                              thresholds={}))

        shape, why_not = verify.shape_of(row)

        assert shape is None
        assert "judged against" in why_not

    def test_a_week_the_device_was_off_through_is_measured_against_it(self):
        """THE decision, demonstrated. Re-deriving the floor from the week
        being measured would histogram a device the automation now switches OFF
        for most of that window — a different threshold, so a different
        measurement, and a comparison between two things rather than two
        measurements of one thing.

        Against the STORED level the same week reads the way a working
        automation looks: observed throughout, nothing qualifying, and zero
        rather than unknown.
        """
        shape, _ = verify.shape_of(_office_row())

        week = verify.measure(_office_rows("0.2"), NOW, 14, shape=shape)

        assert week["occurrences"] == 0
        assert week["occurrences_observed"] == week["occurrences_examined"] > 0
        assert week["excluded"] == {
            standby.REASON_BELOW_FLOOR: week["occurrences_examined"]}
        assert verify.readable(week) is True
        assert verify.kwh_interval(week) == [0.0, 0.0]


class TestHowLongAWeekIs:
    """Decision 5. The figure held constant is OCCURRENCES, not days."""

    def test_a_rule_that_happens_every_day_is_measured_over_seven(self):
        """Unchanged, and that is the point of deriving the length rather than
        moving the constant: every suggestion already on disk keeps the promise
        it was made."""
        assert verify.days_for(_row()) == verify.AFTER_DAYS == 7

    def test_a_weekday_rule_is_measured_over_a_fortnight(self):
        """Five occurrences is what the proposal calls not defensible."""
        assert verify.days_for(_office_row()) == 14

    def test_a_weekend_rule_gets_four_weeks_which_a_flat_fourteen_would_not(self):
        """The case the proposal never mentions: a fortnight offers a Sat-Sun
        rule four occurrences, fewer than the five it already refused."""
        weekend = _office_row(evidence=_office_evidence(schedule={
            "start": "07:00", "end": "17:00", "days": sorted(window.WEEKENDS)}))

        assert verify.days_for(weekend) == 28
        assert len(window.windows(window.spec("07:00", "17:00", window.WEEKENDS),
                                  NOW - 14 * DAY, NOW)) < verify.AFTER_DAYS

    def test_a_rule_on_one_weekday_waits_seven_weeks_rather_than_reporting_on_two(self):
        """The tail case, and the honest answer: a rule that acts once a week
        has to be watched for seven weeks to be seen acting seven times."""
        weekly = _office_row(evidence=_office_evidence(schedule={
            "start": "07:00", "end": "17:00", "days": [window.WEDNESDAY]}))

        assert verify.days_for(weekly) == 49

    def test_every_length_is_whole_weeks_so_no_weekday_is_weighed_twice(self):
        """Ten days over a Mon-Fri rule holds two Mondays and one Friday."""
        for named in range(1, 8):
            row = _office_row(evidence=_office_evidence(schedule={
                "start": "07:00", "end": "17:00", "days": list(range(named))}))

            days = verify.days_for(row)

            assert days % 7 == 0
            assert days // 7 * named >= verify.AFTER_DAYS

    def test_a_row_whose_window_was_lost_still_gets_a_length(self):
        """The attempt is going to report a refusal; it still wants a window
        printed beside it."""
        assert verify.days_for(_office_row(evidence={})) == verify.AFTER_DAYS

    def test_the_before_and_the_after_are_one_length_for_one_suggestion(self):
        """Or the subtraction is measuring the calendar."""
        verdict = verify.compare(_applied_office(), _office_rows("0.2"), NOW, now=NOW)

        assert verdict["before"]["window"]["days"] == 14
        assert verdict["after"]["window"]["days"] == 14

    def test_and_differ_between_two_suggestions_of_different_shapes(self):
        """Per suggestion, never global — which is what leaves a seven-day
        baseline on disk being compared against a seven-day after."""
        assert verify.days_for(_row()) == 7
        assert verify.days_for(_office_row()) == 14

    def test_reviews_baseline_is_measured_over_the_length_it_hands_over(self, monkeypatch):
        """`settle` reads `days_for` once and uses it for both the baseline's
        window and the week to wait. Asserted on the call rather than on the
        constants, because two call sites agreeing is what this stopped
        relying on."""
        asked = []
        monkeypatch.setattr(
            verify, "measure_week",
            lambda row, end, days=None, measured_at=None: asked.append(days) or {})
        row = _office_row()

        review._baseline(row, NOW, NOW, verify.days_for(row))

        assert asked == [14]


class TestHowMuchOfAWeekHasToHaveBeenRead:
    """MIN_NIGHTS_READ, derived from the week rather than typed as a number."""

    def test_a_night_week_is_still_a_majority_of_seven(self):
        assert verify.min_read(_week("12.0")) == verify.MIN_NIGHTS_READ == 4

    def test_a_weekday_fortnight_is_a_majority_of_its_own_occurrences(self):
        """Holding it to the night's four would accept a week over half of
        which was missing."""
        week = _office_week("15.0")

        assert verify.min_read(week) == (week["occurrences_examined"] // 2) + 1
        assert verify.min_read(week) > verify.MIN_NIGHTS_READ

    def test_a_blob_that_cannot_say_falls_back_to_the_every_day_floor(self):
        """Never to the majority of nothing, which is one."""
        assert verify.min_read({}) == verify.MIN_NIGHTS_READ

    def test_the_sentence_a_half_read_week_produces_names_its_own_noun(self):
        """"3 of the 10 nights" is a lie about a rule that runs at 11:00 on a
        Tuesday."""
        verdict = verify.compare(_applied_office(),
                                 _office_rows("15.0", start=NOW - 2 * DAY), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_UNMEASURED
        assert "occurrences in the week could be read" in verdict["reason"]
        assert str(verify.min_read(verdict["after"])) in verdict["reason"]


class TestTheFieldNamesAWeekIsReadThrough:
    """Decision 9. One table, five readers."""

    def test_a_schedule_week_says_occurrences(self):
        week = _office_week("15.0")

        assert week["kind"] == schedule.KIND
        assert week["occurrences"] == week["occurrences_observed"] > 0
        assert week["hours_per_occurrence"] == 10
        assert week["watts"] == 15.0
        assert "nights" not in week and "nights_observed" not in week

    def test_and_readable_the_interval_and_the_floor_all_read_them(self):
        week = _office_week("15.0")

        low, high = verify.kwh_interval(week)

        assert verify.readable(week) is True
        assert low == pytest.approx(week["watts_range"][0] * week["hours_per_occurrence"]
                                    * week["occurrences_per_month"] / 1000.0)
        assert high >= low
        assert verify.floor_kwh(week) == pytest.approx(
            verify.NOISE_FLOOR_WATTS * week["hours_per_occurrence"]
            * week["occurrences_per_month"] / 1000.0)

    def test_a_standby_week_does_not_move_by_a_character(self):
        """Those names are on disk in activity.db on live installs."""
        week = _week("12.0")

        assert week["kind"] == standby.KIND
        assert week["nights"] == week["nights_examined"] > 0
        assert week["hours_per_day"] == standby.NIGHT_HOURS
        assert week["night_window"] == "01:00-05:00 local"
        assert "occurrences" not in week

    def test_and_a_blob_written_before_there_was_a_second_shape_is_read_as_one(self):
        """Every measurement on disk is a night and none of them records a
        kind."""
        stored = {name: value for name, value in _week("12.0").items() if name != "kind"}

        assert verify.readable(stored) is True
        assert verify.kwh_interval(stored) == verify.kwh_interval(_week("12.0"))


class TestTheNoiseFloorOverTwoWindows:
    """Decision 10: the number does not move, its size on the kWh axis does."""

    def test_both_detectors_floors_are_the_same_number_by_construction(self):
        """The residual a successful week can hide is bounded by the floor the
        detector excluded it under, and all three of these are
        2 x NUMERIC_MIN_ABS_CHANGE."""
        assert verify.NOISE_FLOOR_WATTS == standby.STANDBY_MIN_WATTS
        assert verify.NOISE_FLOOR_WATTS == schedule.IDLE_MIN_WATTS

    def test_but_it_is_sized_to_the_claim_it_guards(self):
        """A ten-hour window seen on 21 weekday occurrences is a bigger claim
        than a four-hour one seen on 30 nights."""
        night = verify.floor_kwh({"hours_per_day": 4, "nights_per_month": 30.0})
        weekday = verify.floor_kwh({"kind": schedule.KIND, "hours_per_occurrence": 10,
                                    "occurrences_per_month": 21.4})

        assert weekday == pytest.approx(verify.NOISE_FLOOR_WATTS * 10 * 21.4 / 1000.0)
        assert weekday > night

    def test_and_a_schedule_blob_that_says_nothing_never_borrows_the_nights_hours(self):
        """There is no default schedule window, so the last resort is that
        detector's own shortest run rather than four hours it never proposed."""
        assert verify.floor_kwh({"kind": schedule.KIND}) == pytest.approx(
            verify.NOISE_FLOOR_WATTS * schedule.MIN_RUN_SLOTS * schedule.SLOT_MINUTES / 60.0
            * schedule.PROJECTION_DAYS_PER_MONTH / 1000.0)


class TestComparingLikePeriodsWhenTheLengthsCanDiffer:
    """Decision 6: the assumption that both windows are AFTER_DAYS long is
    gone, so it is checked."""

    def test_two_weeks_of_different_lengths_are_not_compared_at_all(self):
        week = _week("12.0")
        fortnight = dict(week, window=dict(week["window"], days=14))

        assert "same length of week" in verify._like_periods(week, fortnight)
        assert verify._like_periods(week, week) == ""

    def test_and_the_hours_are_still_checked_as_the_label(self):
        """What the baseline stored a week ago and what §9 printed."""
        moved = dict(_office_week("15.0"),
                     night_window=window.label(window.spec("07:00", "17:00")))

        mismatch = verify._like_periods(moved, _office_week("15.0"))

        assert "Mon-Fri" in mismatch
        assert "same thing" in mismatch

    def test_a_rule_whose_window_cannot_be_rebuilt_is_reported_and_never_retried(self):
        """Both halves went through the same refusal, so there is nothing to
        wait for — a stored blob does not become readable."""
        verdict = verify.compare(_applied_office(evidence={}),
                                 _office_rows("0.2"), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_NO_BASELINE
        assert verdict["saved_range_kwh_per_month"] is None
        assert verify.should_retry(verdict) is False


class TestVerifyingARuleThatNamesItsWeekdays:
    def test_a_device_switched_off_through_its_window_is_a_saving_with_a_range(self):
        verdict = verify.compare(_applied_office(), _office_rows("0.2"), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_SAVED
        low, high = verdict["saved_range_kwh_per_month"]
        assert 0 < low <= high
        assert verdict["saved_watts"] == 15.0

    def test_measured_over_its_own_window_and_not_over_the_night(self):
        """Decision 8, as the thing a reader can check: the blob says which
        hours and which weekdays it covered."""
        verdict = verify.compare(_applied_office(), _office_rows("0.2"), NOW, now=NOW)

        assert verdict["after"]["kind"] == schedule.KIND
        assert verdict["after"]["night_window"] == "07:00-17:00 local Mon-Fri"
        assert standby.night_window_label() not in str(verdict["after"])

    def test_a_week_that_looks_the_same_is_reported_as_no_change(self):
        verdict = verify.compare(_applied_office(), _office_rows("15.0"), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_NO_CHANGE

    def test_a_device_drawing_measurably_more_is_said_so_out_loud(self):
        verdict = verify.compare(_applied_office(), _office_rows("25.0"), NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_WORSE

    def test_and_a_week_nobody_watched_is_never_a_saving_of_everything(self):
        verdict = verify.compare(_applied_office(), [], NOW, now=NOW)

        assert verdict["outcome"] == verify.OUTCOME_UNMEASURED
        assert verdict["saved_range_kwh_per_month"] is None
        assert verify.should_retry(verdict) is True


class TestMeasuringOneWeekOfEitherShape:
    """`measure_week` is the I/O around `measure`, and it is on the path that
    must always answer with a blob."""

    def test_it_sizes_itself_from_the_suggestions_own_window(self):
        blob = verify.measure_week(_office_row(), NOW,
                                   snapshot={"states": {}, "entities": []})

        assert blob["kind"] == schedule.KIND
        assert blob["window"]["days"] == 14
        assert blob["night_window"] == "07:00-17:00 local Mon-Fri"

    def test_a_window_it_cannot_rebuild_gets_no_label_rather_than_the_nights(self):
        """Inventing the night's would compare the after against hours nothing
        was ever measured over."""
        blob = verify.measure_week(_office_row(evidence={"entity": OFFICE_SENSOR}), NOW,
                                   snapshot={"states": {}, "entities": []})

        assert blob["night_window"] == ""
        assert "window it found" in blob["error"]
        assert blob["kind"] == schedule.KIND

    def test_and_a_standby_baseline_still_records_the_live_night(self):
        blob = verify.measure_week(_row(), NOW, days=verify.AFTER_DAYS,
                                   snapshot={"states": {}, "entities": []})

        assert blob["night_window"] == standby.night_window_label() == "01:00-05:00 local"
        assert blob["window"]["days"] == verify.AFTER_DAYS


# --------------------------------------------------------------------------
# decision 11: the weekdays are part of the rule
# --------------------------------------------------------------------------
def _schedule_config(days=("mon", "tue", "wed", "thu", "fri"), at_off="07:00:00",
                     at_on="17:00:00", below=IDLE_BELOW, switch=OFFICE_SWITCH,
                     sensor=OFFICE_SENSOR):
    """A config of the shape core/builder.py's schedule shape writes: the same
    one automation, with a `condition: time` on the cutting branch alone."""
    return {
        "alias": "Printer off on a schedule",
        "description": "",
        "trigger": [{"platform": "time", "at": at_off, "id": "schedule_start"},
                    {"platform": "time", "at": at_on, "id": "schedule_end"}],
        "condition": [],
        "action": [{"choose": [
            {"conditions": [{"condition": "trigger", "id": "schedule_start"},
                            {"condition": "time", "weekday": list(days)},
                            {"condition": "numeric_state", "entity_id": sensor,
                             "below": below}],
             "sequence": [{"service": "switch.turn_off", "target": {"entity_id": switch}}]},
            {"conditions": [{"condition": "trigger", "id": "schedule_end"}],
             "sequence": [{"service": "switch.turn_on", "target": {"entity_id": switch}}]},
        ]}],
        "mode": "single",
    }


def _with_weekdays(config, days):
    """The same config with a weekday restriction added to the cutting
    branch."""
    edited = json.loads(json.dumps(config))
    edited["action"][0]["choose"][0]["conditions"].insert(
        1, {"condition": "time", "weekday": list(days)})
    return edited


class TestWhetherTheWeekdaysAreStillTheRules:
    """The line 2.14.3 named as this work package's: a rule edited from Mon-Fri
    to every day is not the rule that was graded, and until now it verified as
    though it were."""

    def test_an_untouched_weekly_rule_is_nothing_to_report(self):
        assert verify.still_running(_change(), _schedule_config(), True,
                                    _schedule_config(), "on") == ("", "")

    def test_a_rule_widened_from_weekdays_to_every_day_is_an_edit(self):
        """It now cuts power on two days nothing ever measured."""
        _, not_running = verify.still_running(
            _change(), _schedule_config(), True,
            _schedule_config(days=("mon", "tue", "wed", "thu", "fri", "sat", "sun")), "on")

        assert "edited" in not_running

    def test_and_so_is_one_narrowed(self):
        _, not_running = verify.still_running(
            _change(), _schedule_config(), True,
            _schedule_config(days=("mon", "tue")), "on")

        assert "edited" in not_running

    def test_a_night_rule_that_gained_a_weekday_restriction_is_an_edit_too(self):
        """One expression covers both shapes: `plan_for` answers EVERY_DAY when
        nothing restricts the cut."""
        _, not_running = verify.still_running(
            _change(), _config(), True,
            _with_weekdays(_config(), ("mon", "tue", "wed", "thu", "fri")), "on")

        assert "edited" in not_running

    def test_but_a_seven_day_restriction_written_out_longhand_is_not(self):
        """It fires on exactly the same dates. A check that fired on a
        difference the measurement cannot see is the other way to build one
        nobody keeps."""
        longhand = _with_weekdays(
            _config(), ("mon", "tue", "wed", "thu", "fri", "sat", "sun"))

        assert verify.still_running(_change(), _config(), True, longhand, "on") == ("", "")

    def test_the_other_four_things_are_still_compared(self):
        """The weekdays are an addition to the check, not a replacement."""
        for edited in (_schedule_config(at_off="08:00:00"),
                       _schedule_config(at_on="16:00:00"),
                       _schedule_config(below=5.0),
                       _schedule_config(switch="switch.hifi")):
            _, not_running = verify.still_running(
                _change(), _schedule_config(), True, edited, "on")

            assert "edited" in not_running



# --------------------------------------------------------------------------
# the loop closing, against real stores
# --------------------------------------------------------------------------
@pytest.fixture
def stores(tmp_path, monkeypatch):
    record.close()
    suggestions.close()
    activity.close()
    monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "record.db"))
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setattr(review, "DIGEST_PATH", str(tmp_path / "digest.json"))
    yield
    record.close()
    suggestions.close()
    activity.close()


def _snapshot(automation_state="on"):
    """A one-device home, plus the automation the agent wrote in it."""
    states = {
        SENSOR: {"state": "12.0", "attributes": {
            "unit_of_measurement": "W", "device_class": "power"}},
        SWITCH: {"state": "on", "attributes": {"friendly_name": "TV socket"}},
    }
    if automation_state:
        states["automation.tv_socket_off_overnight"] = {
            "state": automation_state, "attributes": {"id": AUTOMATION_ID}}
    return {
        "areas": [],
        "devices": [{"id": "dev1", "name": "TV plug"}],
        "entities": [{"entity_id": SENSOR, "device_id": "dev1"},
                     {"entity_id": SWITCH, "device_id": "dev1"}],
        "states": states,
    }


@pytest.fixture
def home(monkeypatch):
    snapshot = _snapshot()
    monkeypatch.setattr(watchlist, "watched_entities", lambda: [SENSOR, SWITCH])
    monkeypatch.setattr(registry, "fetch_registry", lambda: (True, snapshot))
    return snapshot


@pytest.fixture
def offline_ha(monkeypatch):
    """Home Assistant, and only Home Assistant. The record, the detector, the
    builder, the replay, the gate and both stores all run for real."""
    monkeypatch.setattr(automations, "_validate_by_test_create", lambda cfg: (True, "temp-id"))
    monkeypatch.setattr(gate, "_ha_get", lambda path: (False, "not found"))
    monkeypatch.setattr(automations, "read_automation",
                        lambda automation_id: (True, _config()))
    monkeypatch.setattr(verify, "read_automation", lambda automation_id: (True, _config()))


def _plant(state="12.0", start=WINDOW_START, end=NOW, entity_id=SENSOR, active="120.0"):
    """`active` is what the device draws between eight and ten in the evening.
    The detector rejects a device with a single level — a flat draw and no
    in-use state above it is a router, not a standby load — and every series
    here has to get past it to reach the thing being verified. It is clear of
    the night window, which is the only stretch anything below measures."""
    for ts in range(int(start), int(end), HEARTBEAT):
        in_use = 20 <= datetime.fromtimestamp(ts).hour < 22
        value = str(active if (active is not None and in_use)
                    else (state(ts) if callable(state) else state))
        record.record_transition(entity_id, ts, value, value, HEARTBEAT)


def _applied(now=NOW, result=None):
    """One suggestion taken all the way to `applied`, with its baseline.

    The whole pipeline, not a fixture that writes a row: the detector finds the
    waste, the pass proposes it, the gate applies it, and review.settle captures
    the "before" — which is the thing WP9 reads.
    """
    review.run_once(now=now)
    row = suggestions.open_for(KEY)
    real_now = activity._now
    activity._now = lambda: int(now)
    try:
        activity.record_decision(
            row["change_id"], "approved", status="applied",
            result=result or json.dumps({"created": True, "automation_id": AUTOMATION_ID}),
            inverse={"op": "delete_automation", "automation_id": AUTOMATION_ID})
    finally:
        activity._now = real_now
    assert review.settle(row["change_id"], now=now) is True
    return suggestions.suggestion(row["id"])


class TestTheLoopClosing:
    """Design §13's done-when: a real suggestion, applied, and a measured result
    a week later."""

    def _a_week_later(self, applied_at=NOW):
        """The automation doing its job: the device off through every night
        window, drawing normally the rest of the day."""
        def state(ts):
            moment = datetime.fromtimestamp(ts)
            return "0.5" if standby.NIGHT.start_hour <= moment.hour < standby.NIGHT.end_hour \
                else "12.0"
        _plant(state=state, start=applied_at, end=applied_at + verify.AFTER_DAYS * DAY)
        return applied_at + verify.AFTER_DAYS * DAY

    def test_a_week_later_the_agent_reports_what_it_measured(self, stores, home, offline_ha):
        _plant()
        row = _applied()
        assert row["verify_after"] == NOW + verify.AFTER_DAYS * DAY
        later = self._a_week_later()

        assert review.run_once(now=later)["verified"] == 1

        reported = suggestions.suggestion(row["id"])
        assert reported["state"] == "verified"
        verdict = reported["verdict"]
        assert verdict["outcome"] == verify.OUTCOME_SAVED
        low, high = verdict["saved_range_kwh_per_month"]
        assert 0 < low <= high

    def test_the_report_states_both_windows_and_gives_a_range(self, stores, home, offline_ha):
        """§7.2, as the sentence a person actually reads."""
        _plant()
        row = _applied()
        later = self._a_week_later()
        review.run_once(now=later)

        text = artefacts.verification_text(suggestions.suggestion(row["id"]))

        assert "2026-06-30" in text and "2026-07-07" in text
        assert "between" in text and "kWh a month" in text
        assert "roll it back" in text or "roll back" in text.lower()

    def test_and_names_whose_estimate_it_is_comparing_against(self, stores, home, offline_ha):
        _plant()
        row = _applied()
        review.run_once(now=self._a_week_later())

        text = artefacts.verification_text(suggestions.suggestion(row["id"]))

        assert "the estimate was" in text
        assert "replaying the rule against the record" in text

    def test_the_report_reaches_the_user_through_the_outbox_and_is_not_urgent(
            self, stores, home, offline_ha):
        """Decision 4. §11 reserves the push for what cannot wait, and a weekly
        report is the definition of something that can."""
        _plant()
        _applied()
        review.run_once(now=self._a_week_later())

        queued = [m for m in activity.messages() if m["kind"] == "verification"]

        assert len(queued) == 1
        assert queued[0]["urgency"] == "normal"
        assert "kWh a month" in queued[0]["body"]

    def test_a_device_that_kept_drawing_is_reported_as_no_change(
            self, stores, home, offline_ha):
        _plant()
        _applied()
        _plant(start=NOW, end=NOW + verify.AFTER_DAYS * DAY)

        review.run_once(now=NOW + verify.AFTER_DAYS * DAY)

        assert suggestions.open_for(KEY)["verdict"]["outcome"] == verify.OUTCOME_NO_CHANGE

    def test_the_baseline_and_the_week_to_wait_are_one_length(
            self, stores, home, offline_ha):
        """`review.settle` reads `verify.days_for` once and uses it for both,
        so the two ends of the comparison cannot disagree about how long a week
        is. Seven, for every rule any home has today."""
        _plant()
        row = _applied()

        assert verify.days_for(row) == verify.AFTER_DAYS
        assert row["baseline"]["window"]["days"] == verify.days_for(row)
        assert row["verify_after"] == NOW + verify.days_for(row) * DAY

    def test_nothing_is_verified_before_its_week_is_up(self, stores, home, offline_ha):
        _plant()
        _applied()

        assert review.run_once(now=NOW + 2 * DAY)["verified"] == 0
        assert suggestions.open_for(KEY)["state"] == "applied"

    def test_it_runs_from_the_daily_pass_rather_than_a_schedule_of_its_own(self):
        """One interval, one registry round trip. A second schedule would be a
        second thing to keep in step with REVIEW_HOUR."""
        import tasks.review as review_task

        assert [name for name in dir(review_task) if name.endswith("_pass")] == ["review_pass"]

    def test_one_registry_read_serves_the_whole_pass(self, stores, home, offline_ha,
                                                     monkeypatch):
        _plant()
        _applied()
        self._a_week_later()
        reads = []
        snapshot = _snapshot()

        def _counted():
            reads.append(1)
            return True, snapshot

        monkeypatch.setattr(registry, "fetch_registry", _counted)

        review.run_once(now=NOW + verify.AFTER_DAYS * DAY)

        assert len(reads) == 1


class TestAWeekThatCouldNotBeRead:
    """Decision 1. Never "it saved nothing"."""

    def test_it_is_measured_again_next_week_rather_than_reported(
            self, stores, home, offline_ha):
        _plant()
        row = _applied()

        assert review.run_once(now=NOW + verify.AFTER_DAYS * DAY)["verified"] == 1

        waiting = suggestions.suggestion(row["id"])
        assert waiting["state"] == "applied"
        assert waiting["verify_after"] == NOW + 2 * verify.AFTER_DAYS * DAY
        assert waiting["verdict"]["outcome"] == verify.OUTCOME_UNMEASURED
        assert waiting["verdict"]["attempts"] == 1

    def test_and_eventually_reported_as_unmeasured_rather_than_waited_on_forever(
            self, stores, home, offline_ha):
        _plant()
        row = _applied()

        for attempt in range(1, verify.MAX_ATTEMPTS + 1):
            review.run_once(now=NOW + attempt * verify.AFTER_DAYS * DAY)

        reported = suggestions.suggestion(row["id"])
        assert reported["state"] == "verified"
        assert reported["verdict"]["outcome"] == verify.OUTCOME_UNMEASURED
        assert reported["verdict"]["attempts"] == verify.MAX_ATTEMPTS

    def test_and_that_report_says_could_not_measure_never_saved_nothing(
            self, stores, home, offline_ha):
        _plant()
        row = _applied()
        for attempt in range(1, verify.MAX_ATTEMPTS + 1):
            review.run_once(now=NOW + attempt * verify.AFTER_DAYS * DAY)

        text = artefacts.verification_text(suggestions.suggestion(row["id"]))

        assert "No measured result" in text
        assert "not a week in which nothing was saved" in text
        assert "0 kWh" not in text

    def test_and_the_rollback_is_still_offered(self, stores, home, offline_ha):
        """§7.2's rule with no exception in it — including the case where there
        is no number at all."""
        _plant()
        row = _applied()
        for attempt in range(1, verify.MAX_ATTEMPTS + 1):
            review.run_once(now=NOW + attempt * verify.AFTER_DAYS * DAY)

        report = artefacts.verification(suggestions.suggestion(row["id"]))

        assert report["undoable"] is True
        assert "roll it back" in report["choice"]

    def test_a_registry_that_could_not_be_read_costs_a_day_not_an_attempt(
            self, stores, home, offline_ha, monkeypatch):
        """`verify_after` is already in the past, so tomorrow tries again — and
        a Home Assistant that was briefly unreachable does not spend a third of
        the retries."""
        _plant()
        row = _applied()
        monkeypatch.setattr(registry, "fetch_registry", lambda: (False, "HA is down"))

        assert review.run_once(now=NOW + verify.AFTER_DAYS * DAY)["verified"] == 0

        waiting = suggestions.suggestion(row["id"])
        assert waiting["state"] == "applied"
        assert waiting["verify_after"] == NOW + verify.AFTER_DAYS * DAY
        assert waiting["verdict"] is None


class TestAnAutomationThatWasNotRunning:
    def test_a_disabled_one_is_reported_rather_than_measured(
            self, stores, home, offline_ha, monkeypatch):
        _plant()
        row = _applied()
        _plant(state="0.5", start=NOW, end=NOW + verify.AFTER_DAYS * DAY)
        monkeypatch.setattr(registry, "fetch_registry",
                            lambda: (True, _snapshot(automation_state="off")))

        review.run_once(now=NOW + verify.AFTER_DAYS * DAY)

        reported = suggestions.suggestion(row["id"])
        assert reported["state"] == "verified"
        assert reported["verdict"]["outcome"] == verify.OUTCOME_NOT_RUNNING
        assert reported["verdict"]["saved_kwh_per_month"] is None
        assert "switched off in Home Assistant" in reported["verdict"]["reason"]

    def test_an_edited_one_is_too(self, stores, home, offline_ha, monkeypatch):
        """A measurement over a week a different rule was running is not a
        verification of the rule that was graded."""
        _plant()
        row = _applied()
        _plant(state="0.5", start=NOW, end=NOW + verify.AFTER_DAYS * DAY)
        monkeypatch.setattr(verify, "read_automation",
                            lambda automation_id: (True, _config(at_off="03:00:00")))

        review.run_once(now=NOW + verify.AFTER_DAYS * DAY)

        verdict = suggestions.suggestion(row["id"])["verdict"]
        assert verdict["outcome"] == verify.OUTCOME_NOT_RUNNING
        assert "edited" in verdict["reason"]

    def test_a_deleted_one_retires_the_suggestion_and_says_so(
            self, stores, home, offline_ha, monkeypatch):
        """§11: the user is told which and why. There is nothing left to keep
        or roll back, so parking it at `verified` would be a question with no
        meaning."""
        _plant()
        row = _applied()
        monkeypatch.setattr(registry, "fetch_registry",
                            lambda: (True, _snapshot(automation_state=None)))
        monkeypatch.setattr(verify, "read_automation",
                            lambda automation_id: (False, "not found"))

        review.run_once(now=NOW + verify.AFTER_DAYS * DAY)

        retired = suggestions.suggestion(row["id"])
        assert retired["state"] == "retired"
        assert "no longer in Home Assistant" in retired["retired_reason"]
        assert any(m["kind"] == "verification_withdrawn" for m in activity.messages())

    def test_a_change_the_user_already_undid_is_withdrawn_too(
            self, stores, home, offline_ha):
        """§11 offers the rollback "without waiting for the weekly report", so
        this is an ordinary path rather than an odd one."""
        _plant()
        row = _applied()
        activity.mark_undone(row["change_id"], 99)

        review.run_once(now=NOW + verify.AFTER_DAYS * DAY)

        retired = suggestions.suggestion(row["id"])
        assert retired["state"] == "retired"
        assert "rolled back as change #99" in retired["retired_reason"]


class TestTheAnswer:
    """Decision 3: `kept` and `rolled_back` come from a person, and the undo is
    core/rollback.py's."""

    def _reported(self):
        """A suggestion all the way to a delivered report, with a week of
        "after" the automation demonstrably worked over."""
        _plant()
        row = _applied()

        def state(ts):
            moment = datetime.fromtimestamp(ts)
            return "0.5" if standby.NIGHT.start_hour <= moment.hour < standby.NIGHT.end_hour \
                else "12.0"
        _plant(state=state, start=NOW, end=NOW + verify.AFTER_DAYS * DAY)
        review.run_once(now=NOW + verify.AFTER_DAYS * DAY)
        return suggestions.suggestion(row["id"])

    def test_the_code_stops_at_verified(self, stores, home, offline_ha):
        assert self._reported()["state"] == "verified"

    def test_keeping_it_changes_nothing_in_the_home(self, stores, home, offline_ha,
                                                    monkeypatch):
        row = self._reported()

        def _boom(*args, **kwargs):
            raise AssertionError("keeping a change rolled something back")

        monkeypatch.setattr(rollback, "roll_back", _boom)

        answered = verify.answer(row["id"], keep=True, session_id="panel")

        assert answered["ok"] is True
        assert suggestions.suggestion(row["id"])["state"] == "kept"

    def test_rolling_it_back_runs_the_inverse_the_gate_recorded(
            self, stores, home, offline_ha, monkeypatch):
        """Not a second rollback path and not re-gated: the operation was
        pre-authorised when the change was approved."""
        row = self._reported()
        deleted = []
        monkeypatch.setattr(rollback, "delete_automation",
                            lambda automation_id: (deleted.append(automation_id), (True, ""))[1])

        answered = verify.answer(row["id"], keep=False, session_id="panel")

        assert answered["ok"] is True
        assert deleted == [AUTOMATION_ID]
        assert suggestions.suggestion(row["id"])["state"] == "rolled_back"

    def test_and_never_goes_near_the_approval_gate(self, stores, home, offline_ha,
                                                   monkeypatch):
        row = self._reported()

        def _boom(*args, **kwargs):
            raise AssertionError("a verification answer reached the approval gate")

        monkeypatch.setattr(gate, "apply", _boom)
        monkeypatch.setattr(gate, "pending_for", _boom)
        monkeypatch.setattr(rollback, "delete_automation", lambda automation_id: (True, ""))

        assert verify.answer(row["id"], keep=False)["ok"] is True

    def test_a_rollback_that_failed_leaves_the_report_open_and_says_why(
            self, stores, home, offline_ha, monkeypatch):
        """§11: explicit and actionable, never silently swallowed. Moving the
        row to `rolled_back` over an undo that did not happen would be the
        audit trail lying about a home."""
        row = self._reported()
        monkeypatch.setattr(rollback, "delete_automation",
                            lambda automation_id: (False, "HA said no"))

        answered = verify.answer(row["id"], keep=False)

        assert answered["ok"] is False
        assert "HA said no" in answered["message"]
        assert suggestions.suggestion(row["id"])["state"] == "verified"

    def test_answering_twice_moves_nothing_the_second_time(
            self, stores, home, offline_ha, monkeypatch):
        row = self._reported()
        monkeypatch.setattr(rollback, "delete_automation", lambda automation_id: (True, ""))
        verify.answer(row["id"], keep=True)

        again = verify.answer(row["id"], keep=False)

        assert again["ok"] is False
        assert suggestions.suggestion(row["id"])["state"] == "kept"

    def test_an_id_nothing_matches_is_refused_in_words(self, stores, home, offline_ha):
        assert "no suggestion" in verify.answer(9999, keep=True)["message"]

    def test_a_suggestion_that_was_never_verified_cannot_be_kept(
            self, stores, home, offline_ha):
        _plant()
        row = _applied()

        answered = verify.answer(row["id"], keep=True)

        assert answered["ok"] is False
        assert suggestions.suggestion(row["id"])["state"] == "applied"
