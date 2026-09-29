"""The replay engine, against synthetic transition lists.

Everything core/replay.py decides is decided by pure functions taking a config
and an ordered list of rows, and that is what is exercised here — no database,
no Home Assistant, no clock the test did not hand over. The reason is the same
one WP4 gives: the evidence floor is ten credited nights out of thirty, and
nothing in this repo can plant that much history. As list literals, a month is
a loop.

The configs are built by core/builder.py rather than written out, wherever the
shape under test is the one it emits. That pairing is the point — replay's
supported subset is defined as "exactly what the builder produces", and a test
suite that hand-wrote the config it then replayed could keep passing after the
two had drifted apart. The hand-written ones are only for the shapes the
builder deliberately never writes.

ONE GRADER, ONE TEST MODULE
---------------------------
Both shapes are exercised here rather than in a second file, because the thing
being checked about the second one is mostly that it did not disturb the first.
Every assertion above the schedule section is unchanged from before the shape
existed, and that is the single most important check in this module: the night
path is what is on live installs.

The schedule window is stated as core/window.py specs and its occurrence counts
are taken from `window.windows` rather than written as literals. That is not a
convenience — `run` places the CONFIG'S own two instants and filters the
weekdays itself, so asserting the two agree is a real cross-check of the rule
the seam already owns, made against the rule the automation states.
"""
from datetime import date, datetime, timezone
import copy
import time

import pytest

from core import builder
from core import replay
from core import window
from core.detectors import schedule
from core.detectors import standby


DAY = 86400
HEARTBEAT = 600  # comfortably inside observer.NUMERIC_MAX_GAP_SECONDS

# Noon on a fixed day, so every night window below is arithmetic rather than a
# function of when the suite happens to run.
NOW = int(datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc).timestamp())
WINDOW_START = NOW - standby.LOOKBACK_DAYS * DAY

# With the window above, the nights of the 1st to the 30th of June fall whole
# inside it — thirty of them. The night of the 31st of May starts before the
# window does and is not examined.
ALL_NIGHTS = 30

SENSOR = "sensor.tv_power"
SWITCH = "switch.tv"


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Pins the process timezone.

    The trigger times are LOCAL wall-clock times, so without this the tests
    would drift with the developer's zone. tzset() is what time.mktime and
    time.localtime read, and monkeypatch does not restore it.
    """
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# --------------------------------------------------------------------------
# a candidate, a config, and a series the record would plausibly have stored
# --------------------------------------------------------------------------
def _evidence(**overrides):
    evidence = {
        "entity": SENSOR,
        "switch": SWITCH,
        "watts": 12.0,
        "hours_per_day": standby.NIGHT_HOURS,
        "kwh_per_month": 1.44,
        "nights": 30,
        "nights_examined": ALL_NIGHTS,
        "night_window": "01:00-05:00 local",
    }
    evidence.update(overrides)
    return evidence


def _candidate(**overrides):
    return {
        "key": f"{standby.KIND}:{SENSOR}",
        "kind": standby.KIND,
        "entities": [SENSOR, SWITCH],
        "evidence": _evidence(**overrides),
    }


def _config(scale: float = 1.0):
    """The real thing core/builder.py emits, for this sensor at this scale."""
    result, error = builder.build_config(_candidate(), scale=scale)
    assert error is None, error
    return result["config"]


def _in_night(ts) -> bool:
    return standby.NIGHT.start_hour <= time.localtime(ts).tm_hour < standby.NIGHT.end_hour


def _day_of(ts):
    return datetime.fromtimestamp(ts).date()


def _series(state="12.0", source="live", start=WINDOW_START, end=NOW, step=HEARTBEAT,
            skip=None):
    """A heartbeat row every `step` seconds, the way NUMERIC_MAX_GAP_SECONDS
    leaves one on a value that never moves.

    `state` and `source` may be plain values or callables of the timestamp;
    `skip` is a predicate for rows to leave out, which is how a gap is made.
    """
    def value(fn, ts):
        return fn(ts) if callable(fn) else fn

    return [
        {"ts": ts, "state": str(value(state, ts)), "prev_state": None,
         "prev_duration": step, "source": value(source, ts)}
        for ts in range(int(start), int(end), step)
        if not (skip and skip(ts))
    ]


def _at_hour(day, hour):
    """The instant a wall-clock hour falls on, on one local date."""
    return int(time.mktime((day.year, day.month, day.day, hour, 0, 0, 0, 0, -1)))


def _on_nights(days, during, otherwise, from_hour=None, to_hour=None):
    """A state callable: `during` inside the given hours on those dates,
    `otherwise` everywhere else."""
    days = set(days)
    low = standby.NIGHT.start_hour if from_hour is None else from_hour
    high = standby.NIGHT.end_hour if to_hour is None else to_hour

    def state(ts):
        local = time.localtime(ts)
        if _day_of(ts) in days and low <= local.tm_hour < high:
            return during
        return otherwise
    return state


def _run(rows, config=None, scale=1.0, evidence=None, now=NOW, entity_id=SENSOR,
         kind=standby.KIND):
    return replay.run(config if config is not None else _config(scale),
                      entity_id, rows, kind, scale=scale, evidence=evidence, now=now)


def _nights_on(blob, outcome):
    return [n for n in blob["nights"] if n["outcome"] == outcome]


JUNE = [date(2026, 6, d) for d in range(1, 31)]


# --------------------------------------------------------------------------
class TestTheSupportedSubset:
    """Reject by default. A config outside the shape builder.py emits is
    refused, and a refusal discards the candidate — it never produces a
    suggestion carrying a weaker number."""

    def test_the_builders_own_config_is_replayable(self):
        plan, error = replay.plan_for(_config())

        assert error is None
        assert plan["switch"] == SWITCH
        assert plan["guard"]["entity_id"] == SENSOR
        assert plan["nominal_span"] == standby.NIGHT_HOURS * 3600

    def test_an_unsupported_trigger_platform_is_refused_not_approximated(self):
        config = _config()
        config["trigger"][0] = {"platform": "state", "entity_id": SENSOR,
                                "id": builder.TRIGGER_OFF}

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "state" in error

    def test_a_non_empty_top_level_condition_is_refused(self):
        """It would gate every branch, including the one that switches the
        device back on — so the rule graded would not be the rule written."""
        config = _config()
        config["condition"] = [{"condition": "sun", "after": "sunset"}]

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "top-level condition" in error

    def test_an_unsupported_condition_inside_a_branch_is_refused(self):
        config = _config()
        config["action"][0]["choose"][0]["conditions"].append(
            {"condition": "state", "entity_id": "binary_sensor.home", "state": "off"})

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "'state' condition" in error

    def test_a_for_on_the_guard_is_refused(self):
        """The shape the record is least faithful to. A transition log answers
        'below the threshold continuously for five minutes' by guessing."""
        config = _config()
        config["action"][0]["choose"][0]["conditions"][1]["for"] = "00:05:00"

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "for" in error

    def test_a_service_this_does_not_model_is_refused(self):
        config = _config()
        config["action"][0]["choose"][0]["sequence"] = [
            {"service": "notify.mobile_app", "target": {"entity_id": SWITCH}}]

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "notify.mobile_app" in error

    def test_a_mode_other_than_single_is_refused(self):
        config = _config()
        config["mode"] = "queued"

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "queued" in error

    def test_a_top_level_key_nobody_writes_is_refused(self):
        config = _config()
        config["variables"] = {"threshold": 30}

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "variables" in error

    def test_a_guarded_restore_branch_is_refused(self):
        """A condition on the turn-on is how a device stays off all day."""
        config = _config()
        config["action"][0]["choose"][1]["conditions"].append(
            {"condition": "numeric_state", "entity_id": SENSOR, "below": 1})

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "restoring branch" in error

    def test_a_supply_restored_before_it_is_cut_is_refused(self):
        """No span can be bounded, so no saving can be computed — and wrapping
        to the next day would be this module inventing a rule."""
        config = _config()
        config["trigger"][1]["at"] = "00:30:00"

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "before it is cut" in error

    def test_a_branch_matching_no_declared_trigger_is_refused(self):
        config = _config()
        config["action"][0]["choose"][0]["conditions"][0]["id"] = "some_other_trigger"

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "some_other_trigger" in error

    def test_a_series_belonging_to_another_entity_is_refused(self):
        """The one mismatch that produces a clean blob and a plausible saving
        with no symptom anywhere downstream: a real rule, a real series, and
        neither of them describing the other."""
        result, error = _run(_series(state="12.0"), entity_id="sensor.fridge_power")

        assert result is None
        assert "sensor.fridge_power" in error and SENSOR in error

    def test_the_entity_the_series_belongs_to_is_not_optional(self):
        """A default would make forgetting it silent.

        Every other argument is supplied by name, so the TypeError can only be
        about this one. Left positional, it would be raised by whichever
        parameter happened to be unfilled — and the test would go on passing
        after `entity_id` had gained a default, which is the one thing it is
        here to prevent."""
        with pytest.raises(TypeError):
            replay.run(_config(), rows=_series(), kind=standby.KIND, scale=1.0)

    def test_an_unreplayable_config_never_reaches_the_arithmetic(self):
        """The refusal is the whole answer: no blob, no number, nothing a
        caller could mistake for a validated candidate."""
        config = _config()
        config["mode"] = "restart"

        result, error = _run(_series(), config=config)

        assert result is None
        assert error


# --------------------------------------------------------------------------
class TestWouldItHaveFired:
    """Question 1: the trigger instants are enumerated from the calendar, and
    the interesting number is on how many of them the guard would have passed."""

    def test_a_clean_config_over_clean_nights_fires_every_night(self):
        result, error = _run(_series(state="12.0"))

        assert error is None
        blob = result["replay"]
        assert blob["nights_examined"] == ALL_NIGHTS
        assert blob["nights_fired"] == ALL_NIGHTS
        assert blob["nights_saved"] == ALL_NIGHTS
        assert blob["nights_guard_failed"] == 0
        assert blob["nights_wrong"] == 0
        assert blob["outcomes"] == {replay.OUTCOME_SAVED: ALL_NIGHTS}

    def test_the_guard_failing_at_the_cutting_instant_lowers_the_count(self):
        """The device was still working at 01:00, so the rule left it alone —
        which is the rule behaving correctly, not the night being unreadable."""
        busy = JUNE[:5]
        rows = _series(state=_on_nights(busy, "100.0", "12.0", from_hour=0,
                                        to_hour=standby.NIGHT.start_hour + 1))

        result, error = _run(rows)

        assert error is None
        blob = result["replay"]
        assert blob["nights_guard_failed"] == 5
        assert blob["nights_fired"] == ALL_NIGHTS - 5
        assert blob["reasons"][replay.OUTCOME_NOT_FIRED] == {standby.REASON_IN_USE: 5}
        # ...and every one of them says which night and why, because "25 of 30"
        # without the other five is the number without the observation.
        not_fired = _nights_on(blob, replay.OUTCOME_NOT_FIRED)
        assert [n["date"] for n in not_fired] == [d.isoformat() for d in busy]
        assert all(n["reason"] == standby.REASON_IN_USE for n in not_fired)

    def test_every_examined_night_carries_exactly_one_outcome(self):
        rows = _series(state=_on_nights(JUNE[:4], "100.0", "12.0", from_hour=0,
                                        to_hour=standby.NIGHT.start_hour + 1))

        blob = _run(rows)[0]["replay"]

        assert len(blob["nights"]) == blob["nights_examined"]
        assert sum(blob["outcomes"].values()) == blob["nights_examined"]

    def test_too_few_credited_nights_is_a_refusal_not_a_weaker_number(self):
        """§7.1: a candidate below the evidence threshold is discarded."""
        rows = _series(state=_on_nights(JUNE[:25], "100.0", "12.0", from_hour=0,
                                        to_hour=standby.NIGHT.start_hour + 1))

        result, error = _run(rows)

        assert result is None
        assert f"below the floor of {replay.MIN_SAVED_NIGHTS}" in error


# --------------------------------------------------------------------------
class TestWouldItHaveFiredWrongly:
    """Question 2, and the only one replay can answer that the detector
    cannot: the guard passed, and then the household used the device."""

    def test_a_device_used_at_three_in_the_morning_marks_the_night_wrong(self):
        interrupted = JUNE[10:13]
        rows = _series(state=_on_nights(interrupted, "180.0", "12.0",
                                        from_hour=3, to_hour=4))

        blob = _run(rows)[0]["replay"]

        assert blob["nights_wrong"] == 3
        assert blob["would_have_interrupted"] is True
        wrong = _nights_on(blob, replay.OUTCOME_WRONG)
        assert [n["date"] for n in wrong] == [d.isoformat() for d in interrupted]
        assert all(n["reason"] == replay.REASON_USED_LATER for n in wrong)
        # The observation behind the verdict: what was drawn, and when.
        assert all(n["peak_w"] == 180.0 for n in wrong)
        assert all(n["peak_at"].startswith("03:") for n in wrong)

    def test_a_wrong_night_fired_and_was_not_credited(self):
        """It is counted under question 1 — the rule really would have run —
        and pays nothing towards question 3."""
        rows = _series(state=_on_nights(JUNE[10:13], "180.0", "12.0",
                                        from_hour=3, to_hour=4))

        blob = _run(rows)[0]["replay"]

        assert blob["nights_fired"] == ALL_NIGHTS
        assert blob["nights_saved"] == ALL_NIGHTS - 3
        assert blob["nights_wrong"] == 3

    def test_use_is_proof_even_where_the_rest_of_the_night_is_unreadable(self):
        """The asymmetry, in the direction it runs. A spike that IS visible is
        proof whatever else the night is missing; the reverse is not."""
        night = JUNE[10]
        spike_from = _at_hour(night, 3)
        hole_from = _at_hour(night, 4)
        rows = _series(
            state=lambda ts: "180.0" if spike_from <= ts < spike_from + 600 else "12.0",
            skip=lambda ts: hole_from <= ts < hole_from + 3600)

        blob = _run(rows)[0]["replay"]

        assert blob["nights_wrong"] == 1
        assert _nights_on(blob, replay.OUTCOME_WRONG)[0]["date"] == night.isoformat()

    def test_a_clean_night_is_never_called_proof_that_the_rule_was_right(self):
        """There is nothing in the blob that claims it. The most a credited
        night says is that no use was recorded — `nights_saved`, not
        `nights_correct` — and §7.1 is why."""
        blob = _run(_series(state="12.0"))[0]["replay"]

        assert blob["would_have_interrupted"] is False
        assert "nights_correct" not in blob


# --------------------------------------------------------------------------
class TestFidelity:
    """The record is a jitter-filtered transition log. WP4's rules apply here
    unchanged, and each exclusion keeps its own reason."""

    def test_a_statistics_backed_night_and_a_coverage_gap_are_told_apart(self):
        stats_night = JUNE[5]
        gap_night = JUNE[15]
        hole_from = _at_hour(gap_night, 2)
        rows = _series(
            state="12.0",
            source=lambda ts: ("statistics" if _day_of(ts) == stats_night and _in_night(ts)
                               else "live"),
            skip=lambda ts: hole_from <= ts < hole_from + 2 * 3600)

        blob = _run(rows)[0]["replay"]

        assert blob["reasons"][replay.OUTCOME_UNREADABLE] == {standby.REASON_STATISTICS: 1}
        assert blob["reasons"][replay.OUTCOME_NO_CREDIT] == {standby.REASON_NO_COVERAGE: 1}
        assert blob["nights_saved"] == ALL_NIGHTS - 2
        by_date = {n["date"]: n for n in blob["nights"]}
        # The bucket mean is in force at the cutting instant, so the guard
        # itself cannot be read: a mean cannot say what the sensor showed at
        # 01:00. The gap is later, so the rule did fire — it just bought
        # nothing anybody can vouch for.
        assert by_date[stats_night.isoformat()]["outcome"] == replay.OUTCOME_UNREADABLE
        assert by_date[gap_night.isoformat()]["outcome"] == replay.OUTCOME_NO_CREDIT

    def test_one_reason_arriving_from_two_questions_stays_told_apart(self):
        """REASON_IN_USE reaches the blob from both ends of the night: a guard
        that failed at 01:00, and a span standby.assess_night rejected because
        its carried row holds a value from before the cutting instant. Rolled
        into one tally they read as guard failures, contradicting
        `nights_guard_failed` in the same blob."""
        night = JUNE[10]
        cut = _at_hour(night, standby.NIGHT.start_hour)
        # In use until 00:59, idle from 01:00 exactly — the guard passes.
        rows = _series(state=lambda ts: "180.0" if cut - 600 <= ts < cut else "12.0")

        blob = _run(rows)[0]["replay"]

        assert blob["nights_guard_failed"] == 0
        assert blob["nights_fired"] == ALL_NIGHTS
        assert replay.OUTCOME_NOT_FIRED not in blob["reasons"]
        assert blob["reasons"][replay.OUTCOME_NO_CREDIT] == {standby.REASON_IN_USE: 1}

    def test_a_gap_the_heartbeat_explains_is_not_a_gap(self):
        """One missed heartbeat is a restart, not a blind spot — otherwise
        every add-on update would cost a night."""
        rows = _series(state="12.0", step=standby.COVERAGE_MAX_GAP_SECONDS)

        blob = _run(rows)[0]["replay"]

        assert blob["nights_saved"] == ALL_NIGHTS

    def test_unavailable_is_not_zero_watts_at_the_cutting_instant(self):
        """Home Assistant would have evaluated the guard false and not fired.
        The night is still excluded rather than counted as a guard failure: the
        device may have been drawing anything at all, and a night that cannot
        be read is not evidence about it either way."""
        gone = JUNE[7]
        rows = _series(state=_on_nights([gone], "unavailable", "12.0", from_hour=0,
                                        to_hour=standby.NIGHT.start_hour + 1))

        blob = _run(rows)[0]["replay"]

        by_date = {n["date"]: n for n in blob["nights"]}
        assert by_date[gone.isoformat()]["outcome"] == replay.OUTCOME_UNREADABLE
        assert by_date[gone.isoformat()]["reason"] == standby.REASON_UNAVAILABLE
        assert blob["nights_guard_failed"] == 0

    def test_a_night_the_device_really_dropped_to_nothing_pays_nothing(self):
        """The guard passes on a device already switched off, so the rule
        fires — and there was no draw to avoid. Credited at the detector's own
        word for it rather than at the median of the other nights."""
        already_off = JUNE[3]
        rows = _series(state=_on_nights([already_off], "0.0", "12.0"))

        blob = _run(rows)[0]["replay"]

        by_date = {n["date"]: n for n in blob["nights"]}
        assert by_date[already_off.isoformat()]["outcome"] == replay.OUTCOME_NO_CREDIT
        assert by_date[already_off.isoformat()]["reason"] == standby.REASON_BELOW_FLOOR
        assert blob["nights_fired"] == ALL_NIGHTS
        assert blob["nights_saved"] == ALL_NIGHTS - 1


# --------------------------------------------------------------------------
class TestUnits:
    """The factor of a thousand. `below` is in the sensor's RAW reported units
    and the record stores raw states, so replay compares raw against raw and
    must not apply the scale a second time."""

    KILOWATT_SCALE = standby.POWER_UNITS["kW"]

    def test_a_kilowatt_config_compares_correctly_against_kilowatt_rows(self):
        # 12 W reported by a sensor whose unit is kW.
        rows = _series(state="0.012")
        config = _config(scale=self.KILOWATT_SCALE)
        assert config["action"][0]["choose"][0]["conditions"][1]["below"] == 0.03

        result, error = _run(rows, config=config, scale=self.KILOWATT_SCALE)

        assert error is None
        blob = result["replay"]
        # Under a double-scaled comparison this is 12.0 against 0.03 and the
        # count is zero on every night.
        assert blob["nights_fired"] == ALL_NIGHTS
        assert blob["nights_saved"] == ALL_NIGHTS
        # And the watts figure is watts, not milliwatts.
        assert blob["watts"] == 12.0

    def test_the_threshold_is_reported_in_both_units_and_labelled(self):
        """The pair that would have caught the kilowatt bug on sight."""
        config = _config(scale=self.KILOWATT_SCALE)

        blob = _run(_series(state="0.012"), config=config,
                    scale=self.KILOWATT_SCALE)[0]["replay"]

        assert blob["guard"]["below"] == 0.03
        assert blob["guard"]["below_w"] == standby.STANDBY_MAX_WATTS

    def test_a_kilowatt_sensor_in_use_still_fails_the_guard(self):
        """0.1 kW is 100 W. Raw against raw, it is above 0.03 and the rule
        leaves the device alone."""
        rows = _series(state=_on_nights(JUNE[:4], "0.1", "0.012", from_hour=0,
                                        to_hour=standby.NIGHT.start_hour + 1))
        config = _config(scale=self.KILOWATT_SCALE)

        blob = _run(rows, config=config, scale=self.KILOWATT_SCALE)[0]["replay"]

        assert blob["nights_guard_failed"] == 4

    def test_an_unusable_scale_is_refused_rather_than_defaulted(self):
        """NaN compares False against every operator, so a bare `<= 0` lets it
        through and every watt figure in the blob comes out NaN. The builder
        refuses to write a threshold from one; this refuses to grade against
        one, and neither relies on the other having done so."""
        result, error = _run(_series(), config=_config(), scale=float("nan"))

        assert result is None
        assert "scale" in error


# --------------------------------------------------------------------------
class TestWhatWouldItHaveSaved:
    """Question 3, and the rule that bounds it: replay may only tighten the
    detector's estimate, never inflate it."""

    def test_the_saving_is_the_median_draw_over_the_credited_nights(self):
        blob = _run(_series(state="12.0"))[0]["replay"]

        assert blob["watts"] == 12.0
        assert blob["hours_per_night"] == float(standby.NIGHT_HOURS)
        assert blob["nights_per_month"] == float(standby.PROJECTION_DAYS_PER_MONTH)
        assert blob["kwh_per_month"] == round(
            12.0 * standby.NIGHT_HOURS * standby.PROJECTION_DAYS_PER_MONTH / 1000.0, 2)

    def test_nights_that_pay_nothing_are_out_of_the_numerator_not_the_denominator(self):
        """A month of nights is what the figure is projected over, and a night
        the rule could not be credited for is not evidence the device was
        drawing anything."""
        rows = _series(state=_on_nights(JUNE[:6], "180.0", "12.0", from_hour=3, to_hour=4))

        blob = _run(rows)[0]["replay"]

        assert blob["nights_saved"] == ALL_NIGHTS - 6
        assert blob["nights_per_month"] == round(
            (ALL_NIGHTS - 6) / ALL_NIGHTS * standby.PROJECTION_DAYS_PER_MONTH, 1)

    def test_the_replayed_saving_never_exceeds_the_detectors(self):
        """Driven against the detector itself over the same rows, so the two
        figures are the ones that would really sit beside each other."""
        rows = _series(state=_on_nights(JUNE[:5], "180.0", "12.0", from_hour=3, to_hour=4))
        candidate = standby.find_candidate(SENSOR, copy.deepcopy(rows), SWITCH, now=NOW)
        assert candidate, "the detector should find this one"

        blob = _run(rows, evidence=candidate["evidence"])[0]["replay"]

        assert blob["kwh_per_month"] <= candidate["evidence"]["kwh_per_month"]

    def test_a_figure_above_the_detectors_is_reported_as_the_detectors(self):
        """Two components disagreeing about one house is not resolved by
        shipping the nicer number."""
        result, error = _run(_series(state="12.0"),
                             evidence=_evidence(kwh_per_month=0.5, watts=5.0,
                                                nights_per_month=25.0))

        assert error is None
        assert result["estimate"]["basis"] == "detector"
        assert result["estimate"]["kwh_per_month"] == 0.5
        # ...and the replay's own measurements are still there to compare
        # against the claim, under their own names.
        assert result["replay"]["kwh_per_month"] == 1.44
        assert result["replay"]["bounded_by_detector"] is True

    def test_the_reported_estimate_is_always_one_calculation(self):
        """§9 renders it as a sentence a reader can do the arithmetic on. A
        kWh figure taken from the detector beside three numbers taken from the
        replay is a sentence that does not come out."""
        result, _error = _run(_series(state="12.0"),
                              evidence=_evidence(kwh_per_month=0.5, watts=5.0,
                                                 hours_per_day=4, nights_per_month=25.0))

        estimate = result["estimate"]
        assert estimate["basis"] == "detector"
        product = (estimate["watts"] * estimate["hours_per_night"]
                   * estimate["nights_per_month"] / 1000.0)
        assert round(product, 2) == estimate["kwh_per_month"]

    def test_a_component_the_detector_never_stated_is_left_out_not_borrowed(self):
        """Borrowing the replay's number back for a missing field would put the
        inconsistency straight back in."""
        evidence = _evidence(kwh_per_month=0.5)
        evidence.pop("watts")

        estimate = _run(_series(state="12.0"), evidence=evidence)[0]["estimate"]

        assert estimate["kwh_per_month"] == 0.5
        assert "watts" not in estimate

    def test_a_figure_within_the_detectors_says_it_came_from_the_replay(self):
        result, _error = _run(_series(state="12.0"), evidence=_evidence(kwh_per_month=99.0))

        assert result["replay"]["bounded_by_detector"] is False
        assert result["estimate"]["basis"] == "replay"
        assert result["estimate"]["kwh_per_month"] == result["replay"]["kwh_per_month"]

    def test_the_estimate_and_the_blob_never_disagree(self):
        """They go to suggestions.mark_validated as one fact, so the caller
        never re-derives one from the other."""
        result, _error = _run(_series(state="12.0"))

        estimate, blob = result["estimate"], result["replay"]
        for field in ("kwh_per_month", "watts", "hours_per_night", "nights_per_month"):
            assert estimate[field] == blob[field]


# --------------------------------------------------------------------------
class TestDaylightSaving:
    """Thirty days of LOCAL times crosses a transition twice a year, and a
    night is then 23 or 25 hours long."""

    @pytest.fixture(autouse=True)
    def amsterdam(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Amsterdam")
        time.tzset()
        yield
        monkeypatch.undo()
        time.tzset()

    def _hand_config(self, cut, restore):
        """The builder's shape at hours the builder does not write, because the
        skipped hour in this zone is 02:00 and the builder's is 01:00."""
        config = _config()
        config["trigger"][0]["at"] = cut
        config["trigger"][1]["at"] = restore
        return config

    def test_a_night_whose_trigger_hour_does_not_exist_is_not_a_guard_failure(self):
        """On 2026-03-29 the clocks go 02:00 -> 03:00 and 02:00 local never
        happens. Home Assistant would not have fired, and "the clock skipped
        it" is a different fact from "the device was in use"."""
        now = int(time.mktime((2026, 4, 10, 12, 0, 0, 0, 0, -1)))
        rows = _series(state="12.0", start=now - standby.LOOKBACK_DAYS * DAY, end=now)

        blob = _run(rows, config=self._hand_config("02:00:00", "05:00:00"),
                    now=now)[0]["replay"]

        skipped = [n for n in blob["nights"] if n["outcome"] == replay.OUTCOME_NO_TRIGGER]
        assert [n["date"] for n in skipped] == ["2026-03-29"]
        assert skipped[0]["reason"] == replay.REASON_NO_TRIGGER
        assert blob["nights_guard_failed"] == 0
        # Still counted as examined, so it cannot quietly vanish out of the
        # denominator the monthly figure is projected over.
        assert blob["nights_examined"] == len(blob["nights"])
        assert any(n["date"] == "2026-03-29" for n in blob["nights"])

    def test_a_twenty_five_hour_night_is_credited_at_the_nominal_span(self):
        """On 2026-10-25 the clocks go back and 01:00 to 05:00 is five real
        hours. Four is what the config describes, and replay may only tighten."""
        now = int(time.mktime((2026, 11, 5, 12, 0, 0, 0, 0, -1)))
        rows = _series(state="12.0", start=now - standby.LOOKBACK_DAYS * DAY, end=now)

        blob = _run(rows, now=now)[0]["replay"]

        by_date = {n["date"]: n for n in blob["nights"]}
        long_night = by_date["2026-10-25"]
        assert long_night["outcome"] == replay.OUTCOME_SAVED
        assert long_night["hours"] == float(standby.NIGHT_HOURS)


# --------------------------------------------------------------------------
# The second shape: a window that names its weekdays.
#
# Its own fixtures, deliberately separate from the night ones above. The
# detector reads ninety days rather than thirty, its floor is eight
# occurrences rather than ten nights, and its guard is written from the
# device's own active floor rather than from a constant shared with anything —
# so a fixture reaching for a night constant would be asserting against a
# number neither this shape nor its detector uses.
# --------------------------------------------------------------------------
SCHEDULE_FLOOR = 21.5           # this device's own in-use level, in watts
SCHEDULE_IDLE = "8.0"
SCHEDULE_BUSY = "100.0"

# Ninety days back from the same fixed noon, which is what schedule.py reads.
SCHEDULE_START = NOW - schedule.LOOKBACK_DAYS * DAY

# The pattern the proposal opens with: a household out from 07:00 to 17:00 on
# weekdays.
OFFICE = window.spec("07:00", "17:00", window.WEEKDAYS)


def _schedule_evidence(spec=OFFICE, **overrides):
    """A schedule candidate's evidence, shaped the way
    schedule.find_candidate writes one — occurrences rather than nights, and
    the window carried both as prose and as a value."""
    evidence = {
        "entity": SENSOR,
        "switch": SWITCH,
        "watts": 8.4,
        "hours_per_occurrence": window.hours(spec),
        "kwh_per_month": 2.63,
        "occurrences_per_month": 19.8,
        "occurrences": 12,
        "occurrences_observed": 12,
        "occurrences_examined": 13,
        "idle_watts": 8.0,
        "active_floor": SCHEDULE_FLOOR,
        "schedule_window": window.label(spec),
        "schedule": {"start": window.starts_at(spec), "end": window.ends_at(spec),
                     "days": sorted(spec.days)},
        "thresholds": {"idle_below_w": SCHEDULE_FLOOR,
                       "min_occurrences": schedule.MIN_OCCURRENCES},
    }
    evidence.update(overrides)
    return evidence


def _schedule_candidate(spec=OFFICE, **overrides):
    return {
        "key": f"{schedule.KIND}:{SENSOR}",
        "kind": schedule.KIND,
        "entities": [SENSOR, SWITCH],
        "evidence": _schedule_evidence(spec, **overrides),
    }


def _schedule_config(spec=OFFICE, scale: float = 1.0):
    """The real thing core/builder.py emits for a schedule candidate."""
    result, error = builder.build_config(_schedule_candidate(spec), scale=scale)
    assert error is None, error
    return result["config"]


def _cut_branch(config):
    return config["action"][0]["choose"][0]


def _restore_branch(config):
    return config["action"][0]["choose"][1]


def _occurrences_of(spec, start=SCHEDULE_START, end=NOW):
    """The dates window.py would place whole occurrences of this spec on.

    The seam's own answer, used as the expected value: `run` never calls it —
    it places the config's two instants and filters the weekdays itself — so
    agreement between the two is a cross-check rather than a tautology."""
    return [day for day, _start, _end in window.windows(spec, int(start), int(end))]


def _in_window(ts, spec=OFFICE) -> bool:
    local = time.localtime(ts)
    return (local.tm_wday in spec.days
            and spec.start_hour <= local.tm_hour < spec.end_hour)


def _idle_in_window(idle=SCHEDULE_IDLE, busy=SCHEDULE_BUSY, spec=OFFICE, only=None):
    """A state callable: idle inside the window, busy everywhere else.

    `only` narrows the idle dates to a set, which is how an occurrence is made
    to fail the guard — the device is simply in use at the cutting instant."""
    dates = None if only is None else set(only)

    def state(ts):
        if _in_window(ts, spec) and (dates is None or _day_of(ts) in dates):
            return idle
        return busy
    return state


def _schedule_series(state=None, spec=OFFICE, start=SCHEDULE_START, end=NOW, **kwargs):
    return _series(state if state is not None else _idle_in_window(spec=spec),
                   start=start, end=end, **kwargs)


def _run_schedule(rows, config=None, spec=OFFICE, scale=1.0, evidence=None,
                  now=NOW, entity_id=SENSOR, kind=schedule.KIND):
    return replay.run(config if config is not None else _schedule_config(spec, scale),
                      entity_id, rows, kind, scale=scale, evidence=evidence, now=now)


def _occurrences_on(blob, outcome):
    return [o for o in blob["occurrences"] if o["outcome"] == outcome]


# --------------------------------------------------------------------------
class TestTheSecondShapesSubset:
    """A `condition: time` naming weekdays on the cutting branch is the whole
    of what the second shape adds, and everything around it is refused as
    hard as before."""

    def test_the_builders_own_schedule_config_is_replayable(self):
        plan, error = replay.plan_for(_schedule_config())

        assert error is None
        assert plan["restricted"] is True
        assert plan["days"] == window.WEEKDAYS
        assert plan["guard"]["below"] == SCHEDULE_FLOOR
        assert plan["nominal_span"] == window.hours(OFFICE) * 3600

    def test_a_night_config_restricts_nothing_and_says_so(self):
        """The structural difference the kind is checked against, read off the
        shape the builder has always written."""
        plan, error = replay.plan_for(_config())

        assert error is None
        assert plan["restricted"] is False
        assert plan["days"] == window.EVERY_DAY

    @pytest.mark.parametrize("key, value", [("after", "07:00:00"),
                                            ("before", "17:00:00")])
    def test_a_time_condition_restricting_the_hours_is_refused(self, key, value):
        """It would move the instants the grading is placed at, which is the
        one thing this module may not approximate. Refused by the mechanism
        every unknown key is refused by."""
        config = _schedule_config()
        _cut_branch(config)["conditions"][1][key] = value

        plan, error = replay.plan_for(config)

        assert plan is None
        assert key in error

    def test_a_time_condition_naming_no_weekdays_is_refused(self):
        """A rule that can never fire, and guessing it meant every day is the
        widening the builder refuses from the other end."""
        config = _schedule_config()
        _cut_branch(config)["conditions"][1]["weekday"] = []

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "no weekdays" in error

    def test_a_day_home_assistant_does_not_spell_is_refused_not_dropped(self):
        """Dropping it would take an occurrence out of the denominator without
        anything saying so."""
        config = _schedule_config()
        _cut_branch(config)["conditions"][1]["weekday"] = ["mon", "Tuesday"]

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "Tuesday" in error

    def test_a_weekday_restriction_on_the_restoring_branch_is_refused(self):
        """The same failure a guard there is, one weekday further on: a device
        cut on Friday would stay cut until Monday."""
        config = _schedule_config()
        _restore_branch(config)["conditions"].append(
            {"condition": "time", "weekday": ["mon"]})

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "restoring branch" in error

    def test_a_guarded_restoring_branch_is_still_refused_in_this_shape(self):
        config = _schedule_config()
        _restore_branch(config)["conditions"].append(
            {"condition": "numeric_state", "entity_id": SENSOR, "below": 1})

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "restoring branch" in error

    def test_two_weekday_restrictions_on_the_cut_are_refused_not_intersected(self):
        """Intersecting them would be this module deciding what a config it
        did not build meant."""
        config = _schedule_config()
        _cut_branch(config)["conditions"].append(
            {"condition": "time", "weekday": ["mon", "tue"]})

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "more than one weekday restriction" in error

    def test_a_non_empty_top_level_condition_is_still_refused(self):
        """The weekdays go in the cutting branch. At the top level they would
        gate the restore too."""
        config = _schedule_config()
        config["condition"] = [{"condition": "time", "weekday": ["mon"]}]

        plan, error = replay.plan_for(config)

        assert plan is None
        assert "top-level condition" in error


# --------------------------------------------------------------------------
class TestWhichShapeIsBeingGraded:
    """The kind is what the builder built, and it is checked against the
    config rather than sniffed out of it."""

    def test_the_kind_is_not_optional(self):
        """A default would make forgetting it silent, which is the argument
        for `entity_id` being required and the failure is the same size."""
        with pytest.raises(TypeError):
            replay.run(_config(), SENSOR, _series(), scale=1.0)

    def test_a_night_kind_over_a_config_that_names_weekdays_is_refused(self):
        """WP5 argued this exact failure from the other end: a night rule
        somebody hand-edited a `condition: time` into would otherwise be
        graded over the five days that condition names rather than the seven
        it fires on."""
        config = _config()
        _cut_branch(config)["conditions"].insert(
            1, {"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]})

        result, error = _run(_series(), config=config, kind=standby.KIND)

        assert result is None
        assert standby.KIND in error and "weekdays" in error

    def test_a_schedule_kind_over_a_config_that_names_none_is_refused(self):
        """And the other way round: a schedule rule whose restriction was
        edited away would be graded over two days it never ran on."""
        result, error = _run_schedule(_schedule_series(), config=_config())

        assert result is None
        assert schedule.KIND in error and "every day" in error

    def test_a_kind_with_no_shape_is_refused_not_given_the_closest_one(self):
        result, error = _run(_series(), kind="occupancy")

        assert result is None
        assert "occupancy" in error

    def test_the_check_does_not_reach_the_arithmetic(self):
        result, error = _run_schedule(_schedule_series(), config=_config())

        assert result is None
        assert error


# --------------------------------------------------------------------------
class TestWhichDatesAreOccurrences:
    """Two ways a date can fail to be an occurrence, and they must not
    collapse into one: a Saturday under a Mon-Fri rule is not an occurrence at
    all, while a date the clock ate the trigger instant on is one that was
    examined."""

    def test_a_saturday_under_a_weekday_rule_never_enters_the_denominator(self):
        blob = _run_schedule(_schedule_series())[0]["replay"]

        weekdays = {date.fromisoformat(o["date"]).weekday()
                    for o in blob["occurrences"]}
        assert weekdays == set(window.WEEKDAYS)
        assert blob["occurrences_examined"] == len(blob["occurrences"])

    def test_the_dates_examined_are_the_ones_the_window_seam_would_place(self):
        """`run` filters the weekdays itself and places the config's own two
        instants; window.windows does the same for a spec. Disagreement is a
        denominator nobody can reproduce."""
        blob = _run_schedule(_schedule_series())[0]["replay"]

        assert [o["date"] for o in blob["occurrences"]] == [
            day.isoformat() for day in _occurrences_of(OFFICE)]

    def test_a_weekend_the_device_was_busy_costs_the_rule_nothing(self):
        """It is not an occurrence the rule declined. The rule was not asked."""
        blob = _run_schedule(_schedule_series())[0]["replay"]

        assert blob["occurrences_guard_failed"] == 0
        assert blob["outcomes"] == {replay.OUTCOME_SAVED: blob["occurrences_examined"]}

    def test_a_weekday_rule_is_projected_over_weekdays_not_over_a_month(self):
        """Thirty occurrences a month for a Mon-Fri window would be a claim
        about twenty-one occurrences that never happen."""
        blob = _run_schedule(_schedule_series())[0]["replay"]

        assert blob["occurrences_per_month"] == round(
            blob["occurrences_saved"] / blob["occurrences_examined"]
            * schedule.PROJECTION_DAYS_PER_MONTH * len(window.WEEKDAYS) / 7.0, 1)
        assert blob["occurrences_per_month"] < schedule.PROJECTION_DAYS_PER_MONTH

    def test_the_hours_window_names_the_weekdays_it_was_graded_over(self):
        """"07:00:00-17:00:00 local" beside a rule that only runs Monday to
        Friday claims more hours than were measured."""
        blob = _run_schedule(_schedule_series())[0]["replay"]

        assert blob["hours_window"] == "07:00:00-17:00:00 local Mon-Fri"

    def test_a_night_rules_hours_window_is_unchanged(self):
        blob = _run(_series())[0]["replay"]

        assert blob["hours_window"] == "01:00:00-05:00:00 local"


# --------------------------------------------------------------------------
class TestTheFloorIsTheDetectorsOwn:
    """A replay crediting fewer occurrences than the measurement it checks is
    a replay that disagrees with it — and which measurement that is depends on
    which detector raised the candidate."""

    def _credited_on(self, count):
        """Rows where the device is idle across the window on exactly `count`
        of its occurrences, and in use at the cutting instant on the rest."""
        return _schedule_series(_idle_in_window(only=_occurrences_of(OFFICE)[-count:]))

    def test_a_schedule_clears_its_own_detectors_floor_below_the_nights_one(self):
        """Nine credited occurrences is under standby's ten and over
        schedule's eight, which is the whole of what "per kind" buys."""
        blob = _run_schedule(self._credited_on(9))[0]["replay"]

        assert blob["occurrences_saved"] == 9
        assert schedule.MIN_OCCURRENCES <= 9 < standby.MIN_QUALIFYING_NIGHTS

    def test_below_the_schedules_own_floor_is_a_refusal(self):
        result, error = _run_schedule(self._credited_on(7))

        assert result is None
        assert f"below the floor of {replay.MIN_SAVED_OCCURRENCES}" in error
        assert "occurrences would have been saved" in error

    def test_the_floor_travels_in_the_blob_under_its_own_name(self):
        blob = _run_schedule(_schedule_series())[0]["replay"]

        assert blob["thresholds"]["min_saved_occurrences"] == schedule.MIN_OCCURRENCES
        assert "min_saved_nights" not in blob["thresholds"]

    def test_the_nights_floor_is_untouched(self):
        blob = _run(_series())[0]["replay"]

        assert blob["thresholds"]["min_saved_nights"] == standby.MIN_QUALIFYING_NIGHTS
        assert "min_saved_occurrences" not in blob["thresholds"]


# --------------------------------------------------------------------------
class TestTheUsedLaterCeilingIsThisDevices:
    """Question 2's ceiling. A night rule asks whether the draw passed the
    global standby level; a schedule rule asks whether it passed THIS device's
    active floor, which is the level its own card quotes."""

    def _spike_on(self, dates, watts, spec=OFFICE, idle=SCHEDULE_IDLE):
        dates = set(dates)
        spike = str(watts)

        def state(ts):
            if not _in_window(ts, spec):
                return SCHEDULE_BUSY
            if _day_of(ts) in dates and time.localtime(ts).tm_hour == 10:
                return spike
            return idle
        return state

    def test_draw_over_this_devices_floor_is_proof_the_rule_would_be_wrong(self):
        """25 W is comfortably under the global standby ceiling, so a night
        rule would have credited this occurrence. It is over this device's own
        active floor, which is what the card says the guard means."""
        assert SCHEDULE_FLOOR < 25.0 < standby.STANDBY_MAX_WATTS
        interrupted = _occurrences_of(OFFICE)[10:13]

        blob = _run_schedule(_schedule_series(self._spike_on(interrupted, 25.0)))[0]["replay"]

        assert blob["occurrences_wrong"] == 3
        assert blob["would_have_interrupted"] is True
        wrong = _occurrences_on(blob, replay.OUTCOME_WRONG)
        assert [o["date"] for o in wrong] == [d.isoformat() for d in interrupted]
        assert all(o["reason"] == replay.REASON_USED_LATER for o in wrong)
        assert all(o["peak_w"] == 25.0 for o in wrong)

    def test_the_ceiling_is_reported_as_the_number_the_grading_used(self):
        blob = _run_schedule(_schedule_series())[0]["replay"]

        assert blob["thresholds"]["ceiling_w"] == SCHEDULE_FLOOR
        assert blob["guard"]["below_w"] == SCHEDULE_FLOOR

    def test_a_night_rules_ceiling_is_still_the_global_one(self):
        blob = _run(_series())[0]["replay"]

        assert blob["thresholds"]["ceiling_w"] == standby.STANDBY_MAX_WATTS


# --------------------------------------------------------------------------
class TestTheUnitLineInTheSecondShape:
    """Raw against raw for the guard, watts against watts for the peak — and
    on a kilowatt sensor the two differ by a factor of a thousand in a shape
    whose threshold comes out of the evidence rather than a constant."""

    KILOWATT_SCALE = standby.POWER_UNITS["kW"]

    def _kw_config(self):
        config = _schedule_config(scale=self.KILOWATT_SCALE)
        assert _cut_branch(config)["conditions"][2]["below"] == round(
            SCHEDULE_FLOOR / self.KILOWATT_SCALE, 6)
        return config

    def test_a_kilowatt_schedule_compares_correctly_against_kilowatt_rows(self):
        rows = _schedule_series(_idle_in_window(idle="0.008", busy="0.1"))

        result, error = _run_schedule(rows, config=self._kw_config(),
                                      scale=self.KILOWATT_SCALE)

        assert error is None
        blob = result["replay"]
        assert blob["occurrences_fired"] == blob["occurrences_examined"]
        assert blob["occurrences_saved"] == blob["occurrences_examined"]
        # Watts, not milliwatts, and the threshold in both units beside it.
        assert blob["watts"] == 8.0
        assert blob["guard"]["below_w"] == SCHEDULE_FLOOR
        assert blob["thresholds"]["ceiling_w"] == SCHEDULE_FLOOR

    def test_a_kilowatt_peak_is_judged_against_the_floor_in_watts(self):
        """0.025 kW is 25 W. Against the raw threshold 0.0215 it is over; the
        peak comparison has to reach that conclusion in watts."""
        interrupted = _occurrences_of(OFFICE)[5:7]

        def state(ts):
            if not _in_window(ts):
                return "0.1"
            if _day_of(ts) in set(interrupted) and time.localtime(ts).tm_hour == 10:
                return "0.025"
            return "0.008"

        blob = _run_schedule(_schedule_series(state), config=self._kw_config(),
                             scale=self.KILOWATT_SCALE)[0]["replay"]

        assert blob["occurrences_wrong"] == 2
        assert all(o["peak_w"] == 25.0
                   for o in _occurrences_on(blob, replay.OUTCOME_WRONG))


# --------------------------------------------------------------------------
class TestWhatCreditsAnOccurrence:
    """The credit check is the detector's own, and the two detectors are
    claiming different things: a night must be a PLATEAU, a schedule window
    must stay under this device's active floor."""

    def test_a_window_that_moves_but_stays_under_the_floor_is_credited(self):
        """The claim on the card is "drawing less than 21.5 W", not "flat" —
        and standby's own check refuses this same span."""
        def state(ts):
            # In use at 25 W outside the window, so that standby's carried row
            # does not reject the span for being in use before it can reject it
            # for moving — which is the comparison this test is making.
            if not _in_window(ts):
                return "25.0"
            return "18.0" if time.localtime(ts).tm_hour % 2 else "5.0"

        rows = _schedule_series(state)
        blob = _run_schedule(rows)[0]["replay"]

        assert blob["occurrences_saved"] == blob["occurrences_examined"]
        # ...and the contrast, stated rather than implied.
        start, end = window.bounds(OFFICE, _occurrences_of(OFFICE)[5])
        assert standby.assess_night(rows, start, end)["reason"] == standby.REASON_NOT_FLAT

    def test_a_device_already_off_across_the_window_pays_nothing(self):
        """Under the idle floor there is no standing draw to avoid, and the
        detector's own word for it is what reaches the blob."""
        dark = _occurrences_of(OFFICE)[3]

        def state(ts):
            if not _in_window(ts):
                return SCHEDULE_BUSY
            return "0.5" if _day_of(ts) == dark else SCHEDULE_IDLE

        blob = _run_schedule(_schedule_series(state))[0]["replay"]

        by_date = {o["date"]: o for o in blob["occurrences"]}
        assert by_date[dark.isoformat()]["outcome"] == replay.OUTCOME_NO_CREDIT
        assert by_date[dark.isoformat()]["reason"] == standby.REASON_BELOW_FLOOR
        assert blob["occurrences_fired"] == blob["occurrences_examined"]

    def test_a_blind_spot_inside_the_window_pays_nothing(self):
        gap_day = _occurrences_of(OFFICE)[15]
        hole_from = _at_hour(gap_day, 10)
        rows = _schedule_series(skip=lambda ts: hole_from <= ts < hole_from + 2 * 3600)

        blob = _run_schedule(rows)[0]["replay"]

        by_date = {o["date"]: o for o in blob["occurrences"]}
        assert by_date[gap_day.isoformat()]["outcome"] == replay.OUTCOME_NO_CREDIT
        assert by_date[gap_day.isoformat()]["reason"] == standby.REASON_NO_COVERAGE

    def test_a_bucket_mean_inside_the_window_pays_nothing(self):
        """A mean cannot evidence that a value stayed below a threshold, which
        is the whole claim."""
        stats_day = _occurrences_of(OFFICE)[6]
        rows = _schedule_series(
            source=lambda ts: ("statistics"
                               if _day_of(ts) == stats_day
                               and time.localtime(ts).tm_hour == 10 else "live"))

        blob = _run_schedule(rows)[0]["replay"]

        by_date = {o["date"]: o for o in blob["occurrences"]}
        assert by_date[stats_day.isoformat()]["outcome"] == replay.OUTCOME_NO_CREDIT
        assert by_date[stats_day.isoformat()]["reason"] == standby.REASON_STATISTICS

    def test_the_entity_going_away_inside_the_window_pays_nothing(self):
        gone = _occurrences_of(OFFICE)[8]

        def state(ts):
            if not _in_window(ts):
                return SCHEDULE_BUSY
            if _day_of(ts) == gone and time.localtime(ts).tm_hour == 10:
                return "unavailable"
            return SCHEDULE_IDLE

        blob = _run_schedule(_schedule_series(state))[0]["replay"]

        by_date = {o["date"]: o for o in blob["occurrences"]}
        assert by_date[gone.isoformat()]["outcome"] == replay.OUTCOME_NO_CREDIT
        assert by_date[gone.isoformat()]["reason"] == standby.REASON_UNAVAILABLE

    def test_a_reading_exactly_at_the_floor_is_the_detectors_own_word_for_it(self):
        """Not proof of use — the peak test is strict — and not idle either.
        schedule.py calls that cell ACTIVE and this reports it as the reason
        standby.py has always used for the same fact about a span."""
        touched = _occurrences_of(OFFICE)[9]

        def state(ts):
            if not _in_window(ts):
                return SCHEDULE_BUSY
            if _day_of(ts) == touched and time.localtime(ts).tm_hour == 10:
                return str(SCHEDULE_FLOOR)
            return SCHEDULE_IDLE

        blob = _run_schedule(_schedule_series(state))[0]["replay"]

        by_date = {o["date"]: o for o in blob["occurrences"]}
        assert by_date[touched.isoformat()]["outcome"] == replay.OUTCOME_NO_CREDIT
        assert by_date[touched.isoformat()]["reason"] == standby.REASON_IN_USE
        assert blob["occurrences_wrong"] == 0

    def test_the_saving_is_the_median_of_the_per_occurrence_minima(self):
        blob = _run_schedule(_schedule_series())[0]["replay"]

        assert blob["watts"] == 8.0
        assert blob["hours_per_occurrence"] == float(window.hours(OFFICE))
        assert blob["kwh_per_month"] == round(
            8.0 * window.hours(OFFICE) * blob["occurrences_per_month"] / 1000.0, 2)


# --------------------------------------------------------------------------
class TestTheFieldNames:
    """A schedule blob speaks in occurrences and a standby blob does not move,
    because three modules read the second set by name and those blobs are on
    disk on live installs."""

    NIGHT_COUNTS = ("nights_examined", "nights_fired", "nights_guard_failed",
                    "nights_wrong", "nights_saved", "nights_per_month",
                    "hours_per_night", "nights")
    OCCURRENCE_COUNTS = ("occurrences_examined", "occurrences_fired",
                         "occurrences_guard_failed", "occurrences_wrong",
                         "occurrences_saved", "occurrences_per_month",
                         "hours_per_occurrence", "occurrences")
    SHARED = ("would_have_interrupted", "bounded_by_detector", "outcomes",
              "reasons", "guard", "switch", "window", "hours_window",
              "thresholds", "watts", "kwh_per_month")

    def test_a_schedule_blob_counts_occurrences_and_nothing_else(self):
        blob = _run_schedule(_schedule_series())[0]["replay"]

        assert all(field in blob for field in self.OCCURRENCE_COUNTS)
        assert not [field for field in self.NIGHT_COUNTS if field in blob]

    def test_a_night_blob_is_exactly_what_it_always_was(self):
        """The check this whole work package is measured by: nothing on disk
        moved, and `review.disqualified` still finds `nights_wrong`."""
        blob = _run(_series())[0]["replay"]

        assert all(field in blob for field in self.NIGHT_COUNTS)
        assert not [field for field in self.OCCURRENCE_COUNTS if field in blob]

    def test_everything_that_is_not_a_count_of_nights_keeps_one_name(self):
        night = _run(_series())[0]["replay"]
        weekly = _run_schedule(_schedule_series())[0]["replay"]

        assert all(field in night and field in weekly for field in self.SHARED)

    def test_the_estimate_falls_back_to_the_schedule_detectors_own_spelling(self):
        """schedule.measure writes `hours_per_occurrence` and
        `occurrences_per_month`, so the map for this shape is an identity —
        both sides were free to agree and did."""
        evidence = _schedule_evidence(kwh_per_month=0.32, watts=3.0,
                                      hours_per_occurrence=6.0,
                                      occurrences_per_month=18.0)

        result, error = _run_schedule(_schedule_series(), evidence=evidence)

        assert error is None
        estimate = result["estimate"]
        assert estimate["basis"] == "detector"
        assert estimate["kwh_per_month"] == 0.32
        assert estimate["hours_per_occurrence"] == 6.0
        assert estimate["occurrences_per_month"] == 18.0
        assert "hours_per_night" not in estimate
        assert result["replay"]["bounded_by_detector"] is True

    def test_the_reported_estimate_is_still_one_calculation(self):
        evidence = _schedule_evidence(kwh_per_month=0.32, watts=3.0,
                                      hours_per_occurrence=6.0,
                                      occurrences_per_month=18.0)

        estimate = _run_schedule(_schedule_series(), evidence=evidence)[0]["estimate"]

        product = (estimate["watts"] * estimate["hours_per_occurrence"]
                   * estimate["occurrences_per_month"] / 1000.0)
        assert round(product, 2) == estimate["kwh_per_month"]

    def test_the_estimate_and_the_blob_never_disagree(self):
        result, _error = _run_schedule(_schedule_series())

        estimate, blob = result["estimate"], result["replay"]
        for field in ("kwh_per_month", "watts", "hours_per_occurrence",
                      "occurrences_per_month"):
            assert estimate[field] == blob[field]


# --------------------------------------------------------------------------
class TestHowFarBack:
    """The lookback is the detector's own too. One number applied to both
    would grade a ninety-day claim over thirty days of record."""

    def test_a_schedule_is_graded_over_ninety_days_by_default(self):
        blob = _run_schedule(_schedule_series())[0]["replay"]

        assert blob["window"]["days"] == schedule.LOOKBACK_DAYS

    def test_a_night_is_graded_over_thirty_days_by_default(self):
        blob = _run(_series())[0]["replay"]

        assert blob["window"]["days"] == standby.LOOKBACK_DAYS

    def test_a_caller_that_states_one_is_still_obeyed(self):
        result, error = replay.run(_schedule_config(), SENSOR, _schedule_series(),
                                   schedule.KIND, scale=1.0, now=NOW, lookback_days=45)

        assert error is None
        assert result["replay"]["window"]["days"] == 45


# --------------------------------------------------------------------------
class TestDaylightSavingOnAWeekdayRule:
    """A weekday restriction and a clock change are independent, and the order
    they are applied in is what keeps a skipped instant and a date the rule
    never named from reading as the same thing."""

    # Both transitions in this zone fall on a Sunday, so a rule that names the
    # weekend is what puts them inside the denominator at all — which is the
    # distinction being tested.
    WEEKEND_EARLY = window.spec("02:00", "05:00", window.WEEKENDS)
    WEEKEND_NIGHT = window.spec("01:00", "05:00", window.WEEKENDS)

    @pytest.fixture(autouse=True)
    def amsterdam(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Amsterdam")
        time.tzset()
        yield
        monkeypatch.undo()
        time.tzset()

    def _at(self, year, month, day):
        return int(time.mktime((year, month, day, 12, 0, 0, 0, 0, -1)))

    def _rows(self, spec, now):
        return _schedule_series(_idle_in_window(spec=spec), spec=spec,
                                start=now - schedule.LOOKBACK_DAYS * DAY, end=now)

    def test_a_skipped_trigger_instant_is_examined_not_a_guard_failure(self):
        """On 2026-03-29 the clocks go 02:00 -> 03:00 and 02:00 local never
        happens. That Sunday IS an occurrence of a weekend rule, and it is
        counted as one."""
        spec, now = self.WEEKEND_EARLY, self._at(2026, 4, 10)

        blob = _run_schedule(self._rows(spec, now), spec=spec, now=now)[0]["replay"]

        skipped = _occurrences_on(blob, replay.OUTCOME_NO_TRIGGER)
        assert [o["date"] for o in skipped] == ["2026-03-29"]
        assert skipped[0]["reason"] == replay.REASON_NO_TRIGGER
        assert blob["occurrences_guard_failed"] == 0
        assert blob["occurrences_examined"] == len(blob["occurrences"])

    def test_a_date_the_rule_does_not_name_is_absent_however_odd_its_clock(self):
        """The same date under a Mon-Fri rule is not an occurrence at all, and
        must not read as one the clock skipped — that would put a date the
        rule never ran on into the denominator under a real-looking reason."""
        now = self._at(2026, 4, 10)
        rows = _schedule_series(start=now - schedule.LOOKBACK_DAYS * DAY, end=now)

        blob = _run_schedule(rows, now=now)[0]["replay"]

        assert "2026-03-29" not in [o["date"] for o in blob["occurrences"]]
        assert replay.OUTCOME_NO_TRIGGER not in blob["outcomes"]

    def test_a_twenty_five_hour_occurrence_is_credited_at_the_nominal_span(self):
        """On 2026-10-25 the clocks go back and 01:00 to 05:00 is five real
        hours. Four is what the config describes, and replay may only
        tighten."""
        spec, now = self.WEEKEND_NIGHT, self._at(2026, 11, 5)

        blob = _run_schedule(self._rows(spec, now), spec=spec, now=now)[0]["replay"]

        by_date = {o["date"]: o for o in blob["occurrences"]}
        assert by_date["2026-10-25"]["outcome"] == replay.OUTCOME_SAVED
        assert by_date["2026-10-25"]["hours"] == float(window.hours(spec))
