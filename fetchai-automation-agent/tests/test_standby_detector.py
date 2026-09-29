"""The standby detector's rules, against synthetic transition lists.

Everything the detector decides is decided by pure functions taking an ordered
list of rows, and that is what is exercised here — not a database. The reason
is the evidence floor: a candidate needs fourteen days of coverage and ten
qualifying nights, and nothing in this repo can plant that much history (the
scenario harness has six entities, none of them a power sensor, and the history
planter stops at nine days because Home Assistant's recorder purges at ten). As
list literals, fourteen days is a loop.

The one exception is the bottom class, which drives the new range read on
core/record.py against real SQLite — including the rule that a scan of the
record may not hold the write lock.
"""
from datetime import date, datetime, timezone
import sqlite3
import time

import pytest

from core import activity
from core import record
from core import suggestions
from core import watchlist
from core.detectors import levels
from core.detectors import standby
from tools.tool_homeassistant import registry


DAY = 86400
HEARTBEAT = 600  # comfortably inside observer.NUMERIC_MAX_GAP_SECONDS

# Noon on a fixed day, so every night window below is arithmetic rather than a
# function of when the suite happens to run.
NOW = int(datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc).timestamp())
WINDOW_START = NOW - standby.LOOKBACK_DAYS * DAY

# With the window above, every night from the 1st to the 30th of June falls
# whole inside it — thirty of them.
ALL_NIGHTS = [date(2026, 6, d) for d in range(1, 31)]

SENSOR = "sensor.tv_power"
SWITCH = "switch.tv"

# The evening the television is actually watched. Every series below carries
# one, because a device with a single level is a router and is rejected before
# it can be a candidate — a flat 12 W is not a standby draw, it is a device
# doing its job. Deliberately clear of the night window, so nothing any night
# assertion here turns on is touched by it.
USE_START_HOUR, USE_END_HOUR = 20, 22
IN_USE = "120.0"


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Pins the process timezone.

    The night window is LOCAL — deliberately, because "night" is a statement
    about the household's clock — so without this the tests would drift with
    the developer's. SQLite's and time.mktime's notion of local time both come
    from the C library, which tzset() changes and monkeypatch does not restore.
    """
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# --------------------------------------------------------------------------
# building a series the record would plausibly have stored
# --------------------------------------------------------------------------
def _in_night(ts) -> bool:
    return standby.NIGHT.start_hour <= time.localtime(ts).tm_hour < standby.NIGHT.end_hour


def _day_of(ts):
    return datetime.fromtimestamp(ts).date()


def _in_use(ts) -> bool:
    return USE_START_HOUR <= time.localtime(ts).tm_hour < USE_END_HOUR


def _series(state="12.0", source="live", start=WINDOW_START, end=NOW, step=HEARTBEAT,
            skip=None, active=IN_USE):
    """A heartbeat row every `step` seconds, the way NUMERIC_MAX_GAP_SECONDS
    leaves one on a value that never moves.

    `state` and `source` may be plain values or callables of the timestamp;
    `skip` is a predicate for rows to leave out, which is how a gap is made.

    `active` overrides `state` during the evening hours, so what comes out is a
    device with two levels rather than one. Set it to None for the router case:
    a series that never leaves its own level.
    """
    def value(fn, ts):
        return fn(ts) if callable(fn) else fn

    return [
        {"ts": ts,
         "state": str(active if (active is not None and _in_use(ts))
                      else value(state, ts)),
         "prev_state": None,
         "prev_duration": step, "source": value(source, ts)}
        for ts in range(int(start), int(end), step)
        if not (skip and skip(ts))
    ]


def _only_on_nights(good_dates, good, bad):
    """A state callable: `good` during the night on those dates, `bad` during
    the night on any other, `good` outside the night entirely."""
    good_dates = set(good_dates)

    def state(ts):
        if _in_night(ts) and _day_of(ts) not in good_dates:
            return bad
        return good
    return state


def _candidate(rows, switch=SWITCH, **kwargs):
    return standby.find_candidate(SENSOR, rows, switch, now=NOW, **kwargs)


# --------------------------------------------------------------------------
class TestOneNight:
    """assess_night in isolation — every disqualification has its own reason,
    and the reason is what the evidence later reports."""

    NIGHT_START, NIGHT_END = standby.night_bounds(date(2026, 6, 15))

    def _assess(self, rows):
        return standby.assess_night(rows, self.NIGHT_START, self.NIGHT_END)

    def test_a_flat_night_qualifies_at_the_level_it_never_went_under(self):
        rows = _series(state=lambda ts: "12.0" if not _in_night(ts) else "13.5")

        verdict = self._assess(rows)

        assert verdict["qualifies"] is True
        # The carried-in row before 01:00 reads 12.0 and holds until the first
        # row inside the window, so the minimum is the value in force at 01:00
        # — not the lowest row that happens to sit inside the brackets.
        assert verdict["watts"] == 12.0

    def test_a_drop_below_the_floor_disqualifies_the_night(self):
        floor_breach = self.NIGHT_START + 2 * 3600
        rows = _series(state=lambda ts: "0.0" if abs(ts - floor_breach) < 300 else "12.0")

        verdict = self._assess(rows)

        assert verdict["qualifies"] is False
        assert verdict["reason"] == standby.REASON_BELOW_FLOOR

    def test_a_device_doing_work_is_not_a_standby_plateau(self):
        spike = self.NIGHT_START + 3600
        rows = _series(state=lambda ts: "180.0" if abs(ts - spike) < 300 else "12.0")

        verdict = self._assess(rows)

        assert verdict["qualifies"] is False
        assert verdict["reason"] == standby.REASON_IN_USE

    def test_a_plateau_that_wanders_too_far_is_not_flat(self):
        wobble = self.NIGHT_START + 3600
        rows = _series(state=lambda ts: "25.0" if abs(ts - wobble) < 300 else "12.0")

        verdict = self._assess(rows)

        assert verdict["qualifies"] is False
        assert verdict["reason"] == standby.REASON_NOT_FLAT

    def test_a_two_hour_gap_is_missing_coverage_not_a_flat_reading(self):
        """The trap the module exists around. Two hours of silence looks
        exactly like two hours of a value that never moved — and reading it
        that way would report a rock-solid plateau over a stretch where the
        agent was not watching at all."""
        hole_from = self.NIGHT_START + 3600
        hole_to = hole_from + 2 * 3600
        rows = _series(state="12.0", skip=lambda ts: hole_from <= ts < hole_to)

        verdict = self._assess(rows)

        assert verdict["qualifies"] is False
        assert verdict["reason"] == standby.REASON_NO_COVERAGE

    def test_a_gap_the_heartbeat_explains_is_not_a_gap(self):
        """One missed heartbeat is a restart, not a blind spot — otherwise
        every add-on update would disqualify a night."""
        rows = _series(state="12.0", step=standby.COVERAGE_MAX_GAP_SECONDS)

        assert self._assess(rows)["qualifies"] is True

    def test_a_night_carried_by_statistics_rows_does_not_qualify(self):
        """Bucket means cannot show that a value never dropped below a floor —
        four minutes switched off inside a five-minute bucket leaves the same
        mean — and "never drops below" is the whole claim."""
        rows = _series(
            state="12.0",
            source=lambda ts: "statistics" if _in_night(ts) else "live")

        verdict = self._assess(rows)

        assert verdict["qualifies"] is False
        assert verdict["reason"] == standby.REASON_STATISTICS

    def test_unavailable_is_not_zero_watts(self):
        gone = self.NIGHT_START + 3600
        rows = _series(state=lambda ts: "unavailable" if abs(ts - gone) < 300 else "12.0")

        verdict = self._assess(rows)

        assert verdict["qualifies"] is False
        # Not below_floor: the device may have been drawing anything at all,
        # and recording it as a drop would invent an observation.
        assert verdict["reason"] == standby.REASON_UNAVAILABLE

    def test_silence_after_an_unavailable_row_is_the_entity_not_the_agent(self):
        """Both silences look identical in the record — no rows — and they are
        different facts. observer.should_record writes 'unavailable' once and
        then suppresses the repeats, so an entity offline for three hours
        leaves exactly one row; calling that "the agent was not looking" would
        put a falsehood in a blob a human reads."""
        gone_at = self.NIGHT_START + 3600
        rows = ([r for r in _series(state="12.0") if r["ts"] < gone_at]
                + [{"ts": gone_at, "state": "unavailable", "prev_state": "12.0",
                    "prev_duration": 600, "source": "live"}]
                + [r for r in _series(state="12.0") if r["ts"] > self.NIGHT_END])

        verdict = self._assess(rows)

        assert verdict["qualifies"] is False
        assert verdict["reason"] == standby.REASON_UNAVAILABLE

    def test_silence_after_a_reading_is_still_the_agent_not_looking(self):
        """The other half of the same rule: a gap that opens after a NUMBER is
        a restart, a prune, an entity not yet watched — and must not be
        reported as the device having disappeared."""
        hole_from = self.NIGHT_START + 3600
        rows = _series(state="12.0",
                       skip=lambda ts: hole_from <= ts < hole_from + 2 * 3600)

        assert self._assess(rows)["reason"] == standby.REASON_NO_COVERAGE

    def test_no_row_before_the_window_is_missing_coverage(self):
        rows = _series(state="12.0", start=self.NIGHT_START + 60)

        assert self._assess(rows)["reason"] == standby.REASON_NO_COVERAGE

    def test_silence_after_the_last_row_is_a_gap_too(self):
        rows = _series(state="12.0", end=self.NIGHT_END - 2 * 3600)

        assert self._assess(rows)["reason"] == standby.REASON_NO_COVERAGE


# --------------------------------------------------------------------------
class TestTheEvidenceFloor:
    """§7.1: under the threshold a candidate is discarded, not shown with a
    weak number."""

    def test_a_clean_plateau_over_the_whole_window_is_found(self):
        candidate = _candidate(_series(state="12.0"))

        assert candidate is not None
        assert candidate["key"] == f"standby:{SENSOR}"
        assert candidate["kind"] == "standby"
        assert candidate["entities"] == [SENSOR, SWITCH]
        assert candidate["evidence"]["watts"] == 12.0
        assert candidate["evidence"]["nights"] == len(ALL_NIGHTS)
        # 12 W across the four observed hours, thirty days to the month.
        assert candidate["evidence"]["kwh_per_month"] == 1.44

    def test_nine_qualifying_nights_is_not_enough(self):
        rows = _series(state=_only_on_nights(ALL_NIGHTS[:9], "12.0", "unavailable"))

        # The other twenty-one nights are unreadable rather than observed, so
        # the qualifying FRACTION is a clean 1.0 and only the count can bite.
        assert _candidate(rows) is None

    def test_ten_qualifying_nights_is(self):
        rows = _series(state=_only_on_nights(ALL_NIGHTS[:10], "12.0", "unavailable"))

        candidate = _candidate(rows)

        assert candidate is not None
        assert candidate["evidence"]["nights"] == 10

    def test_a_record_that_does_not_reach_back_far_enough_is_a_normal_no(self):
        """Twelve qualifying nights, but only thirteen days of the record to
        have seen them in. A young install is not an error."""
        rows = _series(state="12.0", start=NOW - 12 * DAY)

        assert _candidate(rows) is None

    def test_an_occasional_plateau_is_not_a_standing_draw(self):
        """Ten plateau nights out of thirty OBSERVED ones. Projecting that
        across every day of a month is the overstatement §7.2 refuses — the
        count is met and the fraction is what refuses it."""
        rows = _series(state=_only_on_nights(ALL_NIGHTS[:10], "12.0", "0.0"))

        assert _candidate(rows) is None

    def test_a_genuine_drop_below_the_floor_sinks_the_candidate(self):
        def state(ts):
            # One four-minute dip to nothing in the middle of every night.
            midnight_dip = _in_night(ts) and time.localtime(ts).tm_hour == 3
            return "0.0" if midnight_dip else "12.0"

        assert _candidate(_series(state=state)) is None

    def test_a_window_of_two_hour_gaps_is_not_a_window_of_plateaus(self):
        def missing(ts):
            return _in_night(ts) and 2 <= time.localtime(ts).tm_hour < 4

        assert _candidate(_series(state="12.0", skip=missing)) is None

    def test_a_window_carried_by_statistics_is_not_a_candidate(self):
        rows = _series(state="12.0",
                       source=lambda ts: "statistics" if _in_night(ts) else "live")

        assert _candidate(rows) is None


# --------------------------------------------------------------------------
class TestWhatTheEvidenceSays:
    """§9: every number travels with the observation behind it, and the
    exclusions are part of the observation."""

    def test_the_excluded_nights_are_counted_by_reason(self):
        rows = _series(
            state=_only_on_nights(ALL_NIGHTS[:26], "12.0", "unavailable"),
            source=lambda ts: ("statistics"
                               if _in_night(ts) and _day_of(ts) in ALL_NIGHTS[26:28]
                               else "live"))

        evidence = _candidate(rows)["evidence"]

        assert evidence["nights"] == 26
        assert evidence["nights_examined"] == 30
        assert evidence["excluded"] == {standby.REASON_STATISTICS: 2,
                                        standby.REASON_UNAVAILABLE: 2}

    def test_the_reported_watts_is_the_level_never_gone_under(self):
        rows = _series(state=lambda ts: "13.8" if _in_night(ts) else "11.2")

        evidence = _candidate(rows)["evidence"]

        assert evidence["watts"] == 11.2
        assert evidence["watts_range"] == [11.2, 11.2]

    def test_the_label_this_detector_writes_has_not_moved_by_a_character(self):
        """core/verify.py compares the `night_window` a baseline stored last
        week against the one measured today and reports the verification as not
        comparable when they differ. Homes are mid-verification against this
        literal, so it is asserted as one: a comparison against
        window.label(standby.NIGHT) would agree with whatever the renderer
        became.
        """
        assert standby.night_window_label() == "01:00-05:00 local"

    def test_the_evidence_names_the_window_and_the_rules_behind_it(self):
        evidence = _candidate(_series(state="12.0"))["evidence"]

        assert evidence["night_window"] == "01:00-05:00 local"
        assert evidence["hours_per_day"] == 4
        assert evidence["window"]["days"] == standby.LOOKBACK_DAYS
        assert evidence["window"]["to"] == "2026-06-30"
        assert evidence["thresholds"]["floor_w"] == standby.STANDBY_MIN_WATTS
        assert evidence["covered_days"] >= standby.MIN_COVERAGE_DAYS

    def test_the_month_is_projected_over_the_nights_actually_seen(self):
        """A device present on ten nights of thirty is credited with ten. It
        once got the same 1.44 kWh as one that idled every single night —
        MIN_QUALIFYING_FRACTION could not catch it, because the nights it was
        absent were dropped from that rule's denominator."""
        rows = _series(state=_only_on_nights(ALL_NIGHTS[:10], "12.0", "unavailable"))

        evidence = _candidate(rows)["evidence"]

        assert evidence["nights"] == 10
        assert evidence["nights_per_month"] == 10.0
        # 12 W x 4 h x 10 nights, not x 30.
        assert evidence["kwh_per_month"] == 0.48

    def test_a_plateau_every_night_is_projected_over_every_night(self):
        evidence = _candidate(_series(state="12.0"))["evidence"]

        assert evidence["nights_per_month"] == 30.0
        assert evidence["kwh_per_month"] == 1.44

    def test_history_outside_the_judged_window_is_not_coverage_of_it(self):
        """A caller reading ninety days and asking for thirty to be judged: the
        coverage floor must be met inside the window, and the reported
        covered_days must not contradict the window printed beside it."""
        rows = _series(state=_only_on_nights(ALL_NIGHTS[:10], "12.0", "unavailable"),
                       start=WINDOW_START - 60 * DAY)

        evidence = _candidate(rows)["evidence"]

        assert evidence["window"]["days"] == 30
        assert evidence["covered_days"] <= 31

    def test_a_kilowatt_scale_sensor_is_read_in_watts(self):
        rows = _series(state="0.012", active="0.120")

        candidate = _candidate(rows, scale=1000.0)

        assert candidate["evidence"]["watts"] == 12.0


# --------------------------------------------------------------------------
class TestTheDeviceHasTwoLevels:
    """The router, the NAS and the aquarium pump, and why none of them reaches
    a person.

    Every one of them passes the four night tests cleanly — small, steady,
    all-night, every night — because those tests filter shape and a device
    quietly doing its job has exactly the shape of a device on standby. What
    separates them is somewhere else in the window entirely: the television has
    an in-use level in the daytime series and the router has never had one, and
    without it "15 W" is not standby draw, it is just what this device draws.
    """

    def test_a_television_with_a_second_level_is_still_a_candidate(self):
        """The half that must not regress. The device idles at 12 W all night
        and is watched at 120 W in the evening, which is the whole population
        this detector exists for."""
        candidate = _candidate(_series(state="12.0"))

        assert candidate is not None
        assert candidate["evidence"]["nights"] == len(ALL_NIGHTS)

    def test_a_flat_draw_that_never_moves_is_a_router(self):
        """Thirty perfect nights and not one moment of use anywhere in the
        window. Nothing downstream would have caught this — the runtime guard
        does not fire at 15 W and the replay looks for a device being used
        after a cut, which for a router never happens."""
        rows = _series(state="15.0", active=None)

        # It really did pass everything else...
        assert standby.measure_nights(rows, standby.night_windows(
            WINDOW_START, NOW))["nights"] == len(ALL_NIGHTS)
        assert _candidate(rows) is None

    def test_a_second_level_only_just_above_the_first_is_not_a_second_level(self):
        """A device that drifts between 12 W and 20 W has one level with a
        wobble on it, not two — PLATEAU_SPREAD_FRACTION already says as much at
        a smaller scale, and a DVR idling at 15 W and recording at 25 W is
        exactly the device §6 says must keep its power."""
        assert _candidate(_series(state="12.0", active="20.0")) is None

    def test_a_device_seen_high_twice_has_not_shown_a_second_state(self):
        """Two readings are a spike, a meter glitch or one unexplained event.
        A cluster has to be occupied before it can be a state."""
        spikes = {WINDOW_START + 5 * DAY + 12 * 3600,
                  WINDOW_START + 9 * DAY + 12 * 3600}
        rows = _series(state=lambda ts: "120.0" if ts in spikes else "12.0",
                       active=None)

        assert len([r for r in rows if r["state"] == "120.0"]) == 2
        assert _candidate(rows) is None

    def test_unavailable_is_not_a_low_cluster_either(self):
        """The same rule assess_night applies, applied here: an entity that was
        gone was not drawing zero. Counting it low would give this router an
        idle level of nothing, and then every one of its own 15 W readings
        would look like a device in use."""
        numeric = set(ALL_NIGHTS[:12])
        rows = _series(
            state=lambda ts: "15.0" if _day_of(ts) in numeric else "unavailable",
            active=None)

        # Twelve readable nights, all of them flat 15 W plateaus, and well past
        # MIN_LEVEL_READINGS — so the two-level test is the only thing left
        # that can refuse it.
        assert _candidate(rows) is None

    def test_a_plug_the_user_already_switches_off_has_still_shown_one_level(self):
        """The device is dark 08:00-21:00 because somebody already put it on a
        timer, and holds 12 W the rest of the time. Read naively the OFF level
        is its idle one and the standby draw itself becomes the proof it is
        sometimes in use — so the detector would offer to cut a device the
        household already cuts for thirteen hours a day."""
        def state(ts):
            return "0.0" if 8 <= time.localtime(ts).tm_hour < 21 else "12.0"

        rows = _series(state=state, active=None)

        # Every night is a clean 12 W plateau, so nothing above this refuses it.
        assert standby.measure_nights(rows, standby.night_windows(
            WINDOW_START, NOW))["nights"] == len(ALL_NIGHTS)
        assert _candidate(rows) is None

    def test_the_in_use_level_has_to_be_above_the_plateau_being_cut(self):
        """The same failure without the zeros: 2 W of deep sleep by day, 12 W
        all night. The "active cluster" found is the night plateau, and the
        card would claim a 12 W standby draw and an in-use level of 6 W in the
        same breath."""
        def state(ts):
            return "2.0" if 8 <= time.localtime(ts).tm_hour < 21 else "12.0"

        rows = _series(state=state, active=None)

        levels = standby.device_levels(rows)
        assert levels["active_floor"] < 12.0, "the cluster really is the plateau"
        assert _candidate(rows) is None

    def test_a_sensor_that_reports_export_as_a_negative_number_is_not_bimodal(self):
        """Where the arithmetic degenerates. A negative median puts the active
        floor below every reading the device has ever had, so everything clears
        it — and "-200 W" is not an idle level anybody can be shown."""
        def state(ts):
            return "-200.0" if 7 <= time.localtime(ts).tm_hour < 20 else "12.0"

        assert standby.device_levels(_series(state=state, active=None)) is None

    def test_the_evidence_carries_the_two_levels_it_measured(self):
        """§9: the observation travels with the number, and this is the
        observation behind "this is standby draw rather than a device at
        work"."""
        evidence = _candidate(_series(state="12.0"))["evidence"]

        assert evidence["idle_watts"] == 12.0
        # The bin edge immediately above the idle cluster's own readings —
        # where the record stops describing this device as idle, not a
        # multiple of the level it idles at. Every reading in that cluster is
        # exactly 12.0, so the boundary sits one bin above it (levels.BIN_RATIO
        # is 1.10), and is recorded and, in this work package, used by
        # nothing.
        assert evidence["active_floor"] == 13.1

    def test_the_daytime_series_is_what_it_reads(self):
        """The point of reading the whole window. The in-use level exists only
        in the daytime rows, so a check restricted to the hours the claim is
        about could never see the thing that makes the claim true."""
        night_only = [row for row in _series(state="12.0") if _in_night(row["ts"])]

        assert standby.device_levels(_series(state="12.0")) is not None
        assert standby.device_levels(night_only) is None

    def test_too_little_data_to_judge_is_a_no_rather_than_a_yes(self):
        """Reachable only from here: find_candidate's own floors guarantee ten
        four-hour nights with no long gap inside one, which is more readings
        than this floor asks for. It falls the same way as the check it guards,
        because "we could not tell whether this is a router" is not a weaker
        version of "this is a standby draw"."""
        rows = _series(state="12.0", start=NOW - 3 * 3600)

        assert len(rows) < standby.MIN_LEVEL_READINGS
        assert standby.device_levels(rows) is None

    def test_a_kilowatt_scale_device_is_judged_in_watts_too(self):
        rows = _series(state="0.012", active="0.120")

        # Same two levels as the plain-watts television above, once scaled —
        # the histogram runs on watts, so a kW sensor has to land on the same
        # bins a W sensor reading the same device would.
        assert standby.device_levels(rows, scale=1000.0) == {
            "idle_watts": 12.0, "active_floor": 13.1}

    def test_a_device_used_more_than_half_the_window_is_now_found(self):
        """The false negative this work package exists to fix. A desktop or an
        always-on media player — in use 08:00-21:00, thirteen hours of the
        twenty-four — idles only overnight and for the early morning. Its
        median sits ON the active level, which is exactly what made the
        2.13.0 median-and-multiple rule reject it: the floor moved to three
        times an already-active number and nothing could clear it. A
        histogram does not care which cluster is bigger."""
        def state(ts):
            hour = time.localtime(ts).tm_hour
            return "120.0" if 8 <= hour < 21 else "12.0"

        rows = _series(state=state, active=None)
        values = [float(row["state"]) for row in rows]
        active_count = sum(1 for v in values if v == 120.0)
        idle_count = sum(1 for v in values if v == 12.0)

        # Confirms the premise: more than half the series — and so the median
        # of the whole window — sits on the active level.
        assert active_count > idle_count
        assert standby._median(values) == 120.0

        assert standby.device_levels(rows) == {
            "idle_watts": 12.0, "active_floor": 13.1}
        assert _candidate(rows) is not None

    def test_the_active_cluster_may_outnumber_the_idle_cluster(self):
        """Decision 2 in isolation: idle is the LOWEST qualifying mode, not
        the one with the most readings in it. Built the other way round from
        the television fixture — active held for most of the day — so idle
        stays the minority cluster throughout."""
        def state(ts):
            hour = time.localtime(ts).tm_hour
            return "120.0" if 6 <= hour < 23 else "12.0"

        rows = _series(state=state, active=None)
        values = [float(row["state"]) for row in rows]
        assert sum(1 for v in values if v == 120.0) > sum(1 for v in values if v == 12.0)

        assert standby.device_levels(rows)["idle_watts"] == 12.0

    def test_an_off_cluster_below_the_floor_is_not_mistaken_for_idle(self):
        """Decision 5, the failure it exists to prevent: three clusters, not
        two. The device is genuinely switched off 09:00-18:00 (readings near
        zero), idles at 12 W the rest of the time including all night, and is
        watched at 120 W in the evening. Picking the lowest mode WITHOUT first
        excluding the off cluster would answer 0-ish W idle and collapse
        active_floor to almost nothing — every device would then pass. The
        floor keeps the off cluster out of the histogram entirely, so the
        lowest mode that can form is the real idle level."""
        def state(ts):
            hour = time.localtime(ts).tm_hour
            if 9 <= hour < 18:
                return "0.3"
            return "12.0"

        rows = _series(state=state, active=IN_USE)

        assert standby.device_levels(rows) == {
            "idle_watts": 12.0, "active_floor": 13.1}
        assert _candidate(rows) is not None

    def test_a_level_just_above_the_floor_is_not_mistaken_for_off(self):
        """Decision 5's other direction: the floor must not be so eager that
        it eats a genuinely low idle level. 1.5 W is real — comfortably above
        IDLE_FLOOR_WATTS — and has to survive as the idle mode rather than
        being swept out with the off cluster."""
        rows = _series(state="1.5", active="60.0")

        assert standby.device_levels(rows) == {
            "idle_watts": 1.5, "active_floor": 1.6}

    def test_a_stray_mid_ramp_reading_does_not_defeat_a_real_valley(self):
        """What VALLEY_MAX_OCCUPANCY_FRACTION used to be for, before it was
        retired as dead weight: "a device transitioning between states can
        leave one genuine reading mid-ramp" sitting in what would otherwise
        be an empty valley. MIN_MODE_READINGS is what actually catches this
        (see the comment above VALLEY_MIN_WIDTH_BINS in levels.py) — a
        handful of stray readings, however far from either cluster, never
        accumulate the twenty a real state needs, so idle and active are
        found exactly as if the stray readings were never there."""
        idle_index = levels._bin_index(12.0)
        active_index = levels._bin_index(500.0)
        stray_index = (idle_index + active_index) // 2
        stray_watts, _ = levels._bin_edges(stray_index)

        # Comfortably clear of both clusters on this scale, so the stray
        # reading stands as its own separate (and disqualified) mode rather
        # than being merged away by width alone — the exact shape a working
        # occupancy check would have had something to say about.
        assert stray_index - idle_index - 1 >= levels.VALLEY_MIN_WIDTH_BINS
        assert active_index - stray_index - 1 >= levels.VALLEY_MIN_WIDTH_BINS

        # Three stray readings across the whole month — far short of the
        # twenty MIN_MODE_READINGS asks of a real state.
        stray_ts = {WINDOW_START + 5 * DAY, WINDOW_START + 15 * DAY,
                    WINDOW_START + 25 * DAY}

        def state(ts):
            return str(stray_watts) if ts in stray_ts else "12.0"

        rows = _series(state=state, active="500.0")

        assert standby.device_levels(rows) == {
            "idle_watts": 12.0,
            "active_floor": round(levels._bin_edges(idle_index)[1], 1)}


# --------------------------------------------------------------------------
class TestSomethingToActOn:
    def test_a_sensor_with_no_controllable_switch_produces_no_candidate(self):
        assert _candidate(_series(state="12.0"), switch=None) is None

    def test_the_switch_on_the_same_device_is_the_one(self):
        by_device = {"dev1": ["sensor.tv_power", "switch.tv", "sensor.tv_voltage"]}

        assert standby.controllable_switch(SENSOR, "dev1", by_device) == "switch.tv"

    def test_a_device_with_no_switch_pairs_to_nothing(self):
        by_device = {"dev1": ["sensor.tv_power", "sensor.tv_voltage"]}

        assert standby.controllable_switch(SENSOR, "dev1", by_device) is None

    def test_two_switches_on_one_device_is_a_guess_and_is_refused(self):
        """A multi-outlet plug: which relay carries the measured load is not
        something the registry says, and cutting the wrong socket is worse
        than saying nothing."""
        by_device = {"dev1": ["sensor.tv_power", "switch.socket_1", "switch.socket_2"]}

        assert standby.controllable_switch(SENSOR, "dev1", by_device) is None

    def test_a_sensor_on_no_device_pairs_to_nothing(self):
        assert standby.controllable_switch(SENSOR, None, {"dev1": ["switch.tv"]}) is None


# --------------------------------------------------------------------------
class TestTheKeyIsStable:
    """WP1's whole argument: the daily review re-derives the same key from the
    same waste every morning, so the same suggestion is never shown twice."""

    def test_the_same_waste_produces_the_same_key_on_a_later_run(self):
        first = standby.find_candidate(SENSOR, _series(state="12.0"), SWITCH, now=NOW)
        second = standby.find_candidate(
            SENSOR,
            _series(state="12.4", start=WINDOW_START + DAY, end=NOW + DAY),
            SWITCH, now=NOW + DAY)

        assert first is not None and second is not None
        # Different day, different window, different measured value — and the
        # key is derived from none of them.
        assert first["key"] == second["key"] == f"standby:{SENSOR}"
        assert first["evidence"] != second["evidence"]

    def test_the_same_input_twice_produces_the_same_candidate(self):
        rows = _series(state="12.0")

        assert _candidate(rows) == _candidate(list(rows))


# --------------------------------------------------------------------------
class TestRecognisingAPowerSensor:
    def test_a_watt_sensor_reads_one_to_one(self):
        assert standby.power_scale(
            {"attributes": {"unit_of_measurement": "W", "device_class": "power"}}) == 1.0

    def test_a_power_sensor_with_no_device_class_is_still_one(self):
        assert standby.power_scale({"attributes": {"unit_of_measurement": "W"}}) == 1.0

    def test_an_energy_meter_is_not_a_power_sensor(self):
        assert standby.power_scale(
            {"attributes": {"unit_of_measurement": "kWh", "device_class": "energy"}}) is None

    def test_a_temperature_in_a_unit_we_do_not_read_is_skipped(self):
        assert standby.power_scale(
            {"attributes": {"unit_of_measurement": "°C", "device_class": "temperature"}}) is None

    def test_an_offline_sensor_is_still_recognised_from_its_registry_row(self):
        """Home Assistant drops a sensor's unit while it is unavailable. Read
        only from the live state, a plug that happens to be offline when the
        review runs stops looking like a power sensor at all and is skipped
        before its thirty days of evidence are read."""
        offline = {"state": "unavailable", "attributes": {"friendly_name": "TV plug"}}

        assert standby.power_scale(offline, {"unit_of_measurement": "W",
                                             "original_device_class": "power"}) == 1.0

    def test_a_device_class_alone_never_invents_a_unit(self):
        """Watts and kilowatts differ by a factor of a thousand, and a saving
        overstated by that much is worse than one not found."""
        assert standby.power_scale({"state": "12.0", "attributes": {}},
                                   {"original_device_class": "power"}) is None

    def test_a_missing_state_is_not_a_power_sensor(self):
        assert standby.power_scale(None) is None


# --------------------------------------------------------------------------
class TestTheRangeRead:
    """core/record.between — the one bounded read this detector needed, against
    real SQLite."""

    @pytest.fixture(autouse=True)
    def fresh_db(self, tmp_path, monkeypatch):
        record.close()
        monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "record.db"))
        yield
        record.close()

    def _seed(self, entity_id="sensor.tv_power", timestamps=(), source="live"):
        for ts in timestamps:
            record.record_transition(entity_id, ts, "12.0", "12.0", 600, source=source)

    def test_it_returns_the_half_open_window_oldest_first(self):
        self._seed(timestamps=[1000, 2000, 3000, 4000, 5000])

        rows = record.between("sensor.tv_power", 2000, 5000)

        assert [r["ts"] for r in rows] == [2000, 3000, 4000]

    def test_it_carries_the_source_the_night_rule_turns_on(self):
        self._seed(timestamps=[1000])
        self._seed(timestamps=[2000], source="statistics")

        assert [r["source"] for r in record.between("sensor.tv_power", 0, 9999)] == [
            "live", "statistics"]

    def test_entities_do_not_bleed_into_each_other(self):
        self._seed(entity_id="sensor.tv_power", timestamps=[1000])
        self._seed(entity_id="sensor.fridge_power", timestamps=[1000])

        assert len(record.between("sensor.tv_power", 0, 9999)) == 1

    def test_an_entity_the_record_has_never_seen_is_an_empty_window(self):
        assert record.between("sensor.nothing", 0, 9999) == []

    def test_it_reads_while_another_connection_holds_the_write_lock(self):
        """record.py's hardest rule. SQLite takes one writer at a time even in
        WAL, the observation thread writes constantly, and a scan that opens a
        write transaction first is how scan_relations once wedged the record.
        A read that takes no lock goes through while one is held."""
        self._seed(timestamps=[1000, 2000, 3000])
        conn = record._connect()

        writer = sqlite3.connect(record.DB_PATH, timeout=1)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "INSERT OR IGNORE INTO events (entity, ts, state, source)"
                " VALUES (1, 999999, '1.0', 'live')")

            rows = record.between("sensor.tv_power", 0, 9999)

            # Not [] — which is what the read returns when it gives up on a
            # locked database — and no transaction of its own.
            assert [r["ts"] for r in rows] == [1000, 2000, 3000]
            assert conn.in_transaction is False
        finally:
            writer.rollback()
            writer.close()


# --------------------------------------------------------------------------
class TestTheThinLayer:
    """scan() — the I/O half. Everything it decides is decided above; what is
    checked here is that it joins the registry to the record correctly, and
    that it hands the review pass everything that pass needs to grade what it
    found — without recording anything itself."""

    @pytest.fixture(autouse=True)
    def stores(self, tmp_path, monkeypatch):
        record.close()
        suggestions.close()
        monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "record.db"))
        monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
        yield
        record.close()
        suggestions.close()

    @pytest.fixture
    def home(self, monkeypatch):
        """A one-device home: a power sensor and the switch that feeds it."""
        snapshot = {
            "areas": [],
            "devices": [{"id": "dev1", "name": "TV plug"}],
            "entities": [{"entity_id": SENSOR, "device_id": "dev1"},
                         {"entity_id": SWITCH, "device_id": "dev1"}],
            "states": {
                SENSOR: {"state": "12.0", "attributes": {
                    "unit_of_measurement": "W", "device_class": "power"}},
                # The switch's own name, which is what the builder labels the
                # automation with — the sensor's would name the meter rather
                # than the thing being switched off.
                SWITCH: {"state": "on", "attributes": {"friendly_name": "TV socket"}},
            },
        }
        monkeypatch.setattr(watchlist, "watched_entities", lambda: [SENSOR, SWITCH])
        monkeypatch.setattr(registry, "fetch_registry", lambda: (True, snapshot))
        return snapshot

    def _plant(self, entity_id=SENSOR, state="12.0", active=IN_USE):
        for ts in range(WINDOW_START, NOW, HEARTBEAT):
            value = active if (active is not None and _in_use(ts)) else state
            record.record_transition(entity_id, ts, value, value, HEARTBEAT)

    def test_a_watched_plateau_becomes_a_candidate(self, home):
        self._plant()

        found = standby.scan(now=NOW)

        assert [c["key"] for c in found] == [f"standby:{SENSOR}"]
        assert found[0]["entities"] == [SENSOR, SWITCH]
        assert found[0]["evidence"]["watts"] == 12.0

    def test_the_series_it_judged_travels_with_the_candidate(self, home):
        """WP7 decision 4: the replay grades the rows the measurement came
        from, so scan hands them over rather than making its caller read the
        record a second time over a window that has moved."""
        self._plant()

        found = standby.scan(now=NOW)

        rows = found[0]["rows"]
        assert rows == record.between(SENSOR, WINDOW_START, NOW)
        # ...along with the two answers the registry round trip already has,
        # so that nothing downstream needs a second one.
        assert found[0]["scale"] == 1.0
        assert found[0]["name"] == "TV socket"

    def test_it_records_nothing_itself(self, home):
        """The upsert moved to the review pass, because deciding whether
        today's evidence differs enough from yesterday's to discard a replay is
        a decision about replays — and this module does not know what one is."""
        self._plant()

        assert standby.scan(now=NOW)

        assert suggestions.open_for(f"standby:{SENSOR}") is None

    def test_an_unwatched_home_is_scanned_for_nothing(self, home, monkeypatch):
        self._plant()
        monkeypatch.setattr(watchlist, "watched_entities", list)

        assert standby.scan(now=NOW) == []

    def test_a_registry_it_cannot_read_is_skipped_rather_than_guessed(self, home, monkeypatch):
        self._plant()
        monkeypatch.setattr(registry, "fetch_registry", lambda: (False, "HA is down"))

        assert standby.scan(now=NOW) == []

    def test_a_sensor_with_no_switch_is_never_even_read(self, home, monkeypatch):
        """No switch means no candidate, so there is nothing the record could
        say that would change the answer — and it is not asked."""
        self._plant()
        home["entities"] = [{"entity_id": SENSOR, "device_id": "dev1"}]
        reads = []
        monkeypatch.setattr(record, "between",
                            lambda *a, **kw: reads.append(a) or [])

        assert standby.scan(now=NOW) == []
        assert reads == []
