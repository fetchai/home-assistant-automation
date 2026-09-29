"""The review pass: the seven decisions it owns, and the pipeline carrying them.

Everything core/review.py DECIDES is a pure function over a suggestion row or
an evidence blob, and that is what the first half of this file exercises — as
literals, no database. The reason is the one WP4 and WP5 give: the evidence
floors downstream are fourteen days and ten nights, and nothing in this repo
can plant that much history through a fixture, so a rule reachable only through
a store is a rule that cannot be argued with.

The second half is the pipeline, and it is deliberately NOT stubbed apart at
the seams: the record is planted for real, the detector reads it, the builder
builds, the replay grades, the store records and the gate raises a journal row.
Only Home Assistant is stubbed — the preflight test-create, which is the one
thing here that would otherwise reach the network. What that buys is the thing
this work package is: proof that the five modules written against each other by
name actually fit together.
"""
from datetime import date, datetime, timezone
import json
import sqlite3
import time

import pytest

import conftest
from core import activity
from core import artefacts
from core import builder
from core import gate
from core import record
from core import replay
from core import review
from core import suggestions
from core import verify
from core import watchlist
from core.detectors import schedule, standby
from tools.tool_homeassistant import automations, registry


DAY = 86400
HEARTBEAT = 600  # comfortably inside observer.NUMERIC_MAX_GAP_SECONDS

# Noon on a fixed day, so every night window below is arithmetic rather than a
# function of when the suite happens to run. Noon is also past REVIEW_HOUR,
# which the cadence tests lean on.
NOW = int(datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc).timestamp())
WINDOW_START = NOW - standby.LOOKBACK_DAYS * DAY
SCHEDULE_WINDOW_START = NOW - schedule.LOOKBACK_DAYS * DAY

SENSOR = "sensor.tv_power"
SWITCH = "switch.tv"
KEY = f"{standby.KIND}:{SENSOR}"
SCHEDULE_KEY = f"{schedule.KIND}:{SENSOR}"


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Pins the process timezone.

    Night windows, REVIEW_HOUR and "which local day is it" are all local by
    design, so without this the tests drift with the developer's zone. tzset()
    is what time.mktime and time.localtime read, and monkeypatch does not
    restore it.
    """
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _evidence(**overrides):
    """An evidence blob of the shape standby.find_candidate writes."""
    evidence = {
        "entity": SENSOR,
        "switch": SWITCH,
        "watts": 12.0,
        "watts_range": [11.5, 12.5],
        "hours_per_day": standby.NIGHT_HOURS,
        "kwh_per_month": 1.44,
        "nights_per_month": 30.0,
        "nights": 30,
        "nights_observed": 30,
        "nights_examined": 30,
        "excluded": {},
        "covered_days": 30,
        "night_window": "01:00-05:00 local",
        "window": {"from": "2026-05-31", "to": "2026-06-30", "days": 30},
    }
    evidence.update(overrides)
    return evidence


def _schedule_evidence(**overrides):
    """An evidence blob of the shape schedule.find_candidate writes.

    Its two counting fields are exactly the ones the night's fingerprint could
    not read: `occurrences_per_month` where a night blob says
    `nights_per_month`, and `schedule_window` where it says `night_window`.
    """
    evidence = {
        "entity": SENSOR,
        "switch": SWITCH,
        "watts": 12.0,
        "watts_range": [11.5, 12.5],
        "hours_per_occurrence": 10.0,
        "kwh_per_month": 2.6,
        "occurrences_per_month": 21.7,
        "occurrences": 64,
        "occurrences_observed": 64,
        "occurrences_examined": 64,
        "excluded": {},
        "idle_watts": 12.0,
        "active_floor": 30.0,
        "schedule_window": "07:00-17:00 local Mon-Fri",
        "schedule": {"start": "07:00", "end": "17:00", "days": [0, 1, 2, 3, 4]},
        "window": {"from": "2026-04-01", "to": "2026-06-30", "days": 90},
    }
    evidence.update(overrides)
    return evidence


def _row(**overrides):
    """A suggestion row of the shape core/suggestions.py hands back."""
    row = {
        "id": 1,
        "key": KEY,
        "kind": standby.KIND,
        "state": "validated",
        "updated_at": NOW,
        "entities": [SENSOR, SWITCH],
        "evidence": _evidence(),
        "estimate": {"kwh_per_month": 1.44, "basis": "replay"},
        "replay": {"nights_saved": 30, "nights_wrong": 0, "nights_examined": 30},
        "config": {"alias": "TV socket off overnight"},
    }
    row.update(overrides)
    return row


def _schedule_row(**overrides):
    """...and the same row for the other detector's claim about one device."""
    row = {
        "id": 2,
        "key": SCHEDULE_KEY,
        "kind": schedule.KIND,
        "state": "validated",
        "updated_at": NOW,
        "entities": [SENSOR, SWITCH],
        "evidence": _schedule_evidence(),
        "estimate": {"kwh_per_month": 2.6, "basis": "replay"},
        "replay": {"occurrences_saved": 64, "occurrences_wrong": 0,
                   "occurrences_examined": 64},
        "config": {"alias": "TV socket off during the day"},
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# 1. evidence drift
# --------------------------------------------------------------------------
class TestWhenTheEvidenceHasChanged:
    """The window slides every morning, and a replay graded yesterday must not
    be thrown away because the calendar moved."""

    def test_a_window_that_slid_a_day_is_the_same_claim(self):
        yesterday = _evidence(window={"from": "2026-05-30", "to": "2026-06-29", "days": 30})
        today = _evidence()

        assert review.evidence_fingerprint(yesterday, standby.KIND) == \
            review.evidence_fingerprint(today, standby.KIND)

    def test_so_are_the_counts_of_how_it_was_reached(self):
        """covered_days, the excluded histogram and the observed count are
        bookkeeping about how the claim was reached, not the claim."""
        moved = _evidence(covered_days=27, nights_observed=28,
                          excluded={standby.REASON_NO_COVERAGE: 2},
                          watts_range=[10.0, 15.0])

        assert review.evidence_fingerprint(moved, standby.KIND) == \
            review.evidence_fingerprint(_evidence(), standby.KIND)

    def test_a_night_gained_or_lost_is_not_a_new_claim(self):
        """One night in or out of a thirty-night window is the slide itself."""
        assert review.evidence_fingerprint(_evidence(nights_per_month=29.0), standby.KIND) == \
            review.evidence_fingerprint(_evidence(nights_per_month=30.0), standby.KIND)

    def test_but_a_device_that_idles_half_as_often_is(self):
        assert review.evidence_fingerprint(_evidence(nights_per_month=15.0), standby.KIND) != \
            review.evidence_fingerprint(_evidence(nights_per_month=30.0), standby.KIND)

    def test_a_level_that_moved_a_watt_is(self):
        assert review.evidence_fingerprint(_evidence(watts=14.0), standby.KIND) != \
            review.evidence_fingerprint(_evidence(watts=12.0), standby.KIND)

    def test_and_so_is_a_different_switch(self):
        """A different thing being switched off is a different change, however
        similar the number beside it."""
        assert review.evidence_fingerprint(_evidence(switch="switch.other"), standby.KIND) != \
            review.evidence_fingerprint(_evidence(), standby.KIND)

    def test_unchanged_evidence_is_handed_back_stored_not_fresh(self):
        """Which is what holds the replay: upsert_candidate compares the blob
        it is given against the one on the row, and they are the same object."""
        existing = _row(state="validated", evidence=_evidence())
        fresh = _evidence(window={"from": "2026-06-01", "to": "2026-07-01", "days": 30},
                          covered_days=29)

        evidence, why = review.evidence_to_store(existing, fresh, standby.KIND, NOW)

        assert why == "held"
        assert evidence is existing["evidence"]

    def test_a_claim_that_moved_takes_the_fresh_blob(self):
        existing = _row(state="validated", evidence=_evidence(watts=12.0))

        evidence, why = review.evidence_to_store(existing, _evidence(watts=20.0), standby.KIND, NOW)

        assert (why, evidence["watts"]) == ("moved", 20.0)

    def test_evidence_is_not_held_forever(self):
        """A window that closed a fortnight ago is not a statement about now,
        even when the claim it makes has not moved."""
        stale = _evidence(window={"from": "2026-05-01", "to": "2026-05-31", "days": 30})
        existing = _row(state="validated", evidence=stale)

        evidence, why = review.evidence_to_store(existing, _evidence(), standby.KIND, NOW)

        assert why == "aged"
        assert evidence["window"]["to"] == "2026-06-30"

    def test_only_a_validated_row_has_anything_to_hold(self):
        """Holding exists to keep a replay, and only a validated row has one.
        On a candidate it would store a measurement taken over one window
        beside a grading about to be taken over another."""
        for state in ("candidate", "dismissed", "proposed"):
            evidence, why = review.evidence_to_store(
                _row(state=state), _evidence(watts=9.0), standby.KIND, NOW)

            assert (why, evidence["watts"]) == ("fresh", 9.0)

    def test_a_candidate_nobody_has_seen_before_takes_what_was_measured(self):
        evidence, why = review.evidence_to_store(None, _evidence(), standby.KIND, NOW)

        assert why == "new"

    def test_a_blob_with_no_window_is_not_aged_out_on_a_guess(self):
        assert review.evidence_age_days({"watts": 12.0}, NOW) is None


class TestTheFingerprintOverTwoShapes:
    """Decision 1 read through decision 7: the two fields that move between
    shapes are read through the RAISING DETECTOR's names for them."""

    def test_a_schedule_blob_is_read_in_its_own_vocabulary(self):
        """`occurrences_per_month` and `schedule_window`, which are what a
        schedule blob carries. Read by the night's names both come back absent
        and the last two places of the tuple are None and ""."""
        fingerprint = review.evidence_fingerprint(_schedule_evidence(), schedule.KIND)

        assert fingerprint[-2:] == (round(21.7 / 5.0), "07:00-17:00 local Mon-Fri")

    def test_a_window_that_slid_a_day_is_the_same_claim_here_too(self):
        yesterday = _schedule_evidence(
            window={"from": "2026-03-31", "to": "2026-06-29", "days": 90})

        assert review.evidence_fingerprint(yesterday, schedule.KIND) == \
            review.evidence_fingerprint(_schedule_evidence(), schedule.KIND)

    def test_but_a_device_that_idles_a_third_as_often_is_not(self):
        assert review.evidence_fingerprint(
            _schedule_evidence(occurrences_per_month=8.0), schedule.KIND) != \
            review.evidence_fingerprint(_schedule_evidence(), schedule.KIND)

    def test_and_neither_is_a_window_that_moved(self):
        """The half that vanished. Read by `night_window` alone this comes back
        "" on both sides, and a schedule whose hours changed fingerprints as
        the claim it used to be."""
        moved = _schedule_evidence(schedule_window="09:00-17:00 local Mon-Fri")

        assert review.evidence_fingerprint(moved, schedule.KIND) != \
            review.evidence_fingerprint(_schedule_evidence(), schedule.KIND)

    def test_the_night_and_the_daytime_claim_about_one_device_differ(self):
        """The collapse this work package fixed. Same television, same switch,
        same twelve watts, same frequency — and with the last two fields
        unreadable the two tuples were equal, so two claims about one device
        fingerprinted alike and the window that DID move was not seen moving."""
        night = review.evidence_fingerprint(_evidence(nights_per_month=21.7), standby.KIND)
        daytime = review.evidence_fingerprint(_schedule_evidence(), schedule.KIND)

        assert night != daytime

    def test_a_kind_with_no_detector_has_no_fingerprint_at_all(self):
        """None, not a shorter tuple. Two tuples missing the same fields
        compare EQUAL, and "the evidence has not moved" is the one answer that
        must never be reached by failing to look."""
        assert review.evidence_fingerprint(_evidence(), "occupancy") is None

    def test_and_a_blob_it_cannot_read_is_never_held(self):
        """`None != None` is False, so comparing the two answers directly would
        call an unreadable blob unchanged and hold a replay nobody can confirm
        still describes the same claim."""
        existing = _row(state="validated", kind="occupancy")

        evidence, why = review.evidence_to_store(
            existing, _evidence(watts=99.0), "occupancy", NOW)

        assert (why, evidence["watts"]) == ("moved", 99.0)


# --------------------------------------------------------------------------
# 2. how many would-have-been-wrong nights disqualify
# --------------------------------------------------------------------------
class TestHowManyWrongNights:
    """replay.run reports nights_wrong as a fact and enforces nothing. The
    policy is here, where it can be found and argued with."""

    def test_a_clean_replay_is_proposable(self):
        assert review.disqualified({"nights_wrong": 0, "nights_examined": 30}, standby.KIND) is None

    def test_one_proven_interruption_is_enough_to_stop_it(self):
        """The count is a lower bound, not an estimate: a device used later is
        proof the rule was wrong, while a device not used is not proof it was
        right, and unreadable nights prove nothing either way. Tolerating one
        seen is tolerating an unknown number unseen."""
        refusal = review.disqualified({"nights_wrong": 1, "nights_examined": 30}, standby.KIND)

        assert refusal and "1 of 30" in refusal

    def test_the_refusal_says_what_was_seen(self):
        refusal = review.disqualified({"nights_wrong": 4, "nights_examined": 28}, standby.KIND)

        assert "cut power to a device in use" in refusal


class TestHowManyWrongOccurrences:
    """The same policy over the other shape's grading, and the two ways the
    count can be unreadable."""

    def test_a_schedule_grading_is_read_by_its_own_name(self):
        """`occurrences_wrong`, which is what a schedule blob carries. Read by
        `nights_wrong` it would be absent, taken as zero behind an `or 0`, and
        a rule the record proves would have cut power to a device in use on two
        Tuesday afternoons would be proposed."""
        refusal = review.disqualified(
            {"occurrences_wrong": 2, "occurrences_examined": 64,
             "would_have_interrupted": True}, schedule.KIND)

        assert refusal and "2 of 64 occurrences" in refusal

    def test_a_clean_schedule_grading_is_proposable(self):
        assert review.disqualified(
            {"occurrences_wrong": 0, "occurrences_examined": 64}, schedule.KIND) is None

    def test_a_kind_the_grading_has_no_vocabulary_for_is_refused_outright(self):
        """A blob written by a version of this agent that had a detector this
        one does not. There is no vocabulary to read it in, so it is refused
        rather than read as a night."""
        refusal = review.disqualified({"nights_wrong": 0}, "occupancy")

        assert refusal and "vocabulary this pass does not know" in refusal

    def test_and_a_known_kind_whose_count_is_missing_is_refused_as_well(self):
        """`would_have_interrupted` keeps ONE name for both shapes, which is
        what makes it the backstop: a grading saying it would have interrupted
        is refused even when how often cannot be read."""
        refusal = review.disqualified({"would_have_interrupted": True}, schedule.KIND)

        assert refusal and "cannot read how often" in refusal


# --------------------------------------------------------------------------
# 5. cadence, rank and volume
# --------------------------------------------------------------------------
class TestRanking:
    def test_the_biggest_claim_goes_first(self):
        small = _row(key="standby:sensor.a", estimate={"kwh_per_month": 0.4})
        big = _row(key="standby:sensor.b", estimate={"kwh_per_month": 4.0})

        assert [r["key"] for r in review.rank([small, big])] == \
            ["standby:sensor.b", "standby:sensor.a"]

    def test_it_ranks_on_the_estimate_not_the_replays_own_figure(self):
        """replay.py reports the DETECTOR's figures when the two disagree, and
        `estimate` is the half a user is shown. Ranking on the other half would
        order the digest by a number nobody sees."""
        shown = _row(key="standby:sensor.a", estimate={"kwh_per_month": 3.0, "basis": "detector"},
                     replay={"kwh_per_month": 9.0, "nights_saved": 30})
        other = _row(key="standby:sensor.b", estimate={"kwh_per_month": 5.0, "basis": "replay"},
                     replay={"kwh_per_month": 5.0, "nights_saved": 30})

        assert [r["key"] for r in review.rank([shown, other])][0] == "standby:sensor.b"

    def test_a_tie_is_broken_by_evidence_then_by_key(self):
        thin = _row(key="standby:sensor.a", replay={"nights_saved": 11})
        thick = _row(key="standby:sensor.b", replay={"nights_saved": 28})

        assert [r["key"] for r in review.rank([thin, thick])] == \
            ["standby:sensor.b", "standby:sensor.a"]

    def test_a_suggestion_with_no_estimate_sorts_last_rather_than_raising(self):
        broken = _row(key="standby:sensor.a", estimate=None, replay=None)
        fine = _row(key="standby:sensor.b")

        assert [r["key"] for r in review.rank([broken, fine])][0] == "standby:sensor.b"


class TestVolume:
    def test_one_new_question_a_day(self):
        assert review.proposals_allowed(open_now=0, proposed_today=0) == 1
        assert review.proposals_allowed(open_now=1, proposed_today=1) == 0

    def test_and_never_more_than_the_digest_can_show(self):
        """§9's digest is three lines. A fourth open proposal is a question the
        user has no surface to see."""
        assert review.MAX_OPEN_PROPOSALS == 3
        assert review.proposals_allowed(open_now=3, proposed_today=0) == 0

    def test_a_home_that_answers_keeps_getting_the_next_one(self):
        assert review.proposals_allowed(open_now=2, proposed_today=0) == 1


class TestRankingAcrossDetectors:
    """One ordering over two kinds, because §9's digest is one list."""

    def test_the_biggest_claim_goes_first_whichever_detector_raised_it(self):
        """`kwh_per_month` is the one figure both shapes write under one name,
        and a night's kilowatt hours and a schedule's are the same quantity."""
        night = _row(estimate={"kwh_per_month": 1.44})
        daytime = _schedule_row(estimate={"kwh_per_month": 2.6})

        assert [r["key"] for r in review.rank([night, daytime])] == [SCHEDULE_KEY, KEY]

    def test_the_tiebreak_reads_each_grading_in_its_own_vocabulary(self):
        """`nights_saved` on one blob, `occurrences_saved` on the other. Read
        by one name the other comes back absent, is taken as zero, and sixty
        credited occurrences sort behind eleven credited nights."""
        thin_night = _row(key="standby:sensor.a", estimate={"kwh_per_month": 3.0},
                          replay={"nights_saved": 11})
        thick_day = _schedule_row(key="schedule:sensor.b", estimate={"kwh_per_month": 3.0},
                                  replay={"occurrences_saved": 60})

        assert [r["key"] for r in review.rank([thin_night, thick_day])][0] == \
            "schedule:sensor.b"

    def test_a_grading_this_cannot_read_sorts_after_every_one_it_can(self):
        """Not read as zero, which would land it among the genuinely thin —
        where a suggestion with sixty credited nights it could not count would
        be indistinguishable from one with none."""
        unreadable = _row(key="standby:sensor.a", kind="occupancy",
                          estimate={"kwh_per_month": 3.0}, replay={"nights_saved": 60})
        readable = _row(key="standby:sensor.b", estimate={"kwh_per_month": 3.0},
                        replay={"nights_saved": 11})

        assert [r["key"] for r in review.rank([unreadable, readable])] == \
            ["standby:sensor.b", "standby:sensor.a"]


class TestOneQuestionPerDevice:
    """Decision 5's last clause, which used to be the store's unique index and
    is not any more: the key it is unique on is `kind:entity`, so
    standby:sensor.tv and schedule:sensor.tv are two live rows."""

    def test_an_open_question_claims_the_device(self):
        """§9 answers a suggestion in one word. Two questions about one
        television, a line apart in a three-line digest, are two things "yes"
        could mean."""
        assert review.claimed_entities([_row(state="proposed")]) == {SENSOR, SWITCH}

    def test_so_does_a_rule_whose_week_is_being_measured(self):
        """§7.2 subtracts two weeks of the SAME sensor. A second rule applied
        inside the first one's week moves the after, and the report would
        credit the saving to whichever rule happened to be measured."""
        assert review.claimed_entities([_row(state="applied")]) == {SENSOR, SWITCH}

    def test_and_a_measured_result_waiting_to_be_answered(self):
        assert review.claimed_entities([_row(state="verified")]) == {SENSOR, SWITCH}

    def test_the_agents_own_bookkeeping_claims_nothing(self):
        """A candidate or a validated row costs nothing and is left to
        accumulate; tomorrow's pass ranks it against everything else."""
        assert review.claimed_entities(
            [_row(state="candidate"), _schedule_row(state="validated")]) == set()

    def test_a_dismissal_is_a_cooldown_on_one_key_not_on_the_device(self):
        """A "no" to the night question is not a "no" to the daytime one."""
        assert review.claimed_entities([_row(state="dismissed")]) == set()

    def test_an_answered_result_frees_the_device_again(self):
        """kept and rolled_back are ends: the verification is over, and the
        next claim about the device is measured over a record that already
        includes whatever was kept."""
        assert review.claimed_entities(
            [_row(state="kept"), _row(state="rolled_back")]) == set()

    def test_the_switch_is_claimed_as_well_as_the_sensor(self):
        """Not only the series the subtraction is measured on. A device with
        TWO watched power sensors pairs both of them with its one switch
        (`power.controllable_switch` asks for one controllable sibling, and
        each sensor sees the same one), so two suggestions naming two
        different sensors are still two automations cutting one socket."""
        assert review.claimed_entities([_schedule_row(state="proposed")]) == \
            {SENSOR, SWITCH}

    def test_which_is_what_stops_two_sensors_sharing_a_switch(self):
        """The case claiming the sensor alone let through: different keys,
        different sensors, one socket."""
        asked = _row(key="standby:sensor.a_power", entities=["sensor.a_power", SWITCH],
                     evidence=_evidence(entity="sensor.a_power"), state="proposed")
        other = _row(key="standby:sensor.b_power", entities=["sensor.b_power", SWITCH],
                     evidence=_evidence(entity="sensor.b_power"))

        claimed = review.claimed_entities([asked, other])

        assert "sensor.b_power" not in claimed
        assert review._claims(other) & claimed == {SWITCH}

    def test_the_watchlist_check_still_asks_only_about_the_sensor(self):
        """`_claims` is not `_observed`, and the difference is load-bearing:
        the watchlist is opt-in and a household may well have put the power
        sensor on it and not the plug."""
        assert review.withdrawal_reason(_row(state="validated"), NOW, watched={SENSOR}) is None


class TestTheEvidenceTimeoutIsTheDetectorsOwn:
    """One constant for two lookbacks was the fourth night-shaped thing."""

    def test_a_night_ages_out_at_thirty_days(self):
        assert review.evidence_max_age_days(standby.KIND) == standby.LOOKBACK_DAYS == 30

    def test_and_a_schedule_at_ninety(self):
        """One number for both would either withdraw a schedule proposal two
        months before its evidence stopped describing now, or leave a night one
        standing for three."""
        assert review.evidence_max_age_days(schedule.KIND) == schedule.LOOKBACK_DAYS == 90

    def test_a_kind_with_no_detector_is_aged_against_the_longest(self):
        """Not zero — which is what reading a missing answer as a number comes
        to, and would withdraw every such question the morning it was asked."""
        assert review.evidence_max_age_days("occupancy") == \
            max(d.LOOKBACK_DAYS for d in review.DETECTORS) == 90

    def test_a_schedule_proposal_survives_the_nights_calendar(self):
        """Forty days old: past standby's window entirely, and less than half
        way through the one this evidence was actually measured over."""
        row = _schedule_row(state="proposed", evidence=_schedule_evidence(
            window={"from": "2026-02-20", "to": "2026-05-21", "days": 90}))

        assert review.withdrawal_reason(row, NOW) is None

    def test_but_not_its_own(self):
        row = _schedule_row(state="proposed", evidence=_schedule_evidence(
            window={"from": "2026-01-01", "to": "2026-04-01", "days": 90}))

        reason = review.withdrawal_reason(row, NOW)

        assert reason and "no longer describes now" in reason


class TestTheDetectorRegistry:
    """Decision 7: one answer per kind, on the detector, and no table here."""

    def test_both_detectors_are_registered(self):
        assert [d.KIND for d in review.DETECTORS] == [standby.KIND, schedule.KIND]

    def test_each_one_answers_the_four_things_the_pass_asks_it(self):
        """And declares the whole vocabulary of the blob it writes, which is
        what core/artefacts.py reads to name a count in a sentence — the pass
        itself still only takes `per_month` and `window` off it."""
        for detector in review.DETECTORS:
            assert isinstance(detector.KIND, str) and detector.KIND
            assert int(detector.LOOKBACK_DAYS) > 0
            assert callable(detector.scan)
            assert set(detector.EVIDENCE_FIELDS) == {
                "qualified", "observed", "examined", "per_month", "hours", "window"}

    def test_and_the_names_it_declares_are_the_ones_its_blob_carries(self):
        """Asserted against a blob rather than against the declaration, so that
        renaming a field without moving the declaration fails here rather than
        in a home."""
        assert set(_evidence()) >= set(standby.EVIDENCE_FIELDS.values())
        assert set(_schedule_evidence()) >= set(schedule.EVIDENCE_FIELDS.values())

    def test_every_kind_the_pass_raises_can_be_graded_and_measured(self):
        """The three tables downstream each refuse a kind they do not know
        rather than guessing, so a detector registered here and missing from
        one of them would propose nothing and say nothing about why."""
        for detector in review.DETECTORS:
            assert replay.fields_for(detector.KIND) is not None
            assert builder.build({"kind": detector.KIND})["error"] != \
                f"no automation shape is defined for a {detector.KIND!r} candidate"
            _, why = verify.shape_of({"kind": detector.KIND})
            assert "no way to measure" not in why

    def test_and_a_kind_none_of_them_knows_is_refused_by_all_three(self):
        assert replay.fields_for("occupancy") is None
        assert builder.build({"kind": "occupancy"})["error"] == \
            "no automation shape is defined for a 'occupancy' candidate"
        _, why = verify.shape_of({"kind": "occupancy"})
        assert "no way to measure" in why


class TestCadence:
    def test_it_does_not_run_before_the_morning(self):
        early = int(datetime(2026, 6, 30, 4, 0, tzinfo=timezone.utc).timestamp())

        assert review.due(early, None) is False

    def test_it_runs_once_a_day(self):
        assert review.due(NOW, None) is True
        assert review.due(NOW, NOW - 60) is False
        assert review.due(NOW + DAY, NOW) is True

    def test_the_hour_is_after_the_night_it_reasons_about(self):
        assert review.REVIEW_HOUR > standby.NIGHT.end_hour


class TestWithdrawal:
    """The rule gate.propose is counting on, since the session TTL
    deliberately will not expire a proposal the agent raised."""

    def test_a_fresh_proposal_is_left_alone(self):
        assert review.withdrawal_reason(_row(state="proposed"), NOW) is None

    def test_a_proposal_is_not_withdrawn_for_going_unanswered(self):
        """Nobody promised to read it today. What ages out is the evidence."""
        old_question = _row(state="proposed", updated_at=NOW - 60 * DAY)

        assert review.withdrawal_reason(old_question, NOW) is None

    def test_but_it_is_withdrawn_when_its_evidence_stops_describing_now(self):
        answered_never = _row(state="proposed", evidence=_evidence(
            window={"from": "2026-04-01", "to": "2026-05-01", "days": 30}))

        reason = review.withdrawal_reason(answered_never, NOW)

        assert reason and "no longer describes now" in reason

    def test_a_candidate_the_detector_stopped_finding_is_retired(self):
        """upsert_candidate stamps updated_at every time the same waste is
        found again, so time since it is time since it was last seen."""
        gone = _row(state="candidate", updated_at=NOW - 8 * DAY)

        assert review.withdrawal_reason(gone, NOW)

    def test_but_not_after_one_missed_night(self):
        assert review.withdrawal_reason(_row(state="validated", updated_at=NOW - DAY), NOW) is None

    def test_a_dismissal_is_never_retired_by_this(self):
        """Retiring it would put it outside the store's partial unique index
        and let tomorrow raise the same waste again — turning the user's "not
        now" into "asked again in the morning"."""
        dismissed = _row(state="dismissed", updated_at=NOW - 90 * DAY)

        assert review.withdrawal_reason(dismissed, NOW) is None

    def test_an_applied_suggestion_belongs_to_the_verification_not_here(self):
        assert review.withdrawal_reason(_row(state="applied", updated_at=NOW - 90 * DAY), NOW) is None

    def test_an_entity_the_agent_no_longer_watches_is_withdrawn_at_once(self):
        """§11: a suggestion about something the agent cannot observe is one it
        cannot verify a change to — whether the device disappeared from Home
        Assistant or the household stopped watching it."""
        reason = review.withdrawal_reason(_row(state="proposed"), NOW,
                                          watched={"sensor.something_else"})

        assert reason and SENSOR in reason

    def test_the_switch_is_not_required_to_be_watched(self):
        """The watchlist is opt-in and a household may well have put the power
        sensor on it and not the plug. The detector supports that; retiring it
        would throw away most real suggestions."""
        assert review.withdrawal_reason(_row(state="validated"), NOW, watched={SENSOR}) is None

    def test_an_empty_watchlist_retires_nothing(self):
        """It is what core/watchlist.py answers for a home that watches nothing
        AND what it answers when it cannot read its own file. Acting on it
        would retire every open suggestion over a failed read."""
        assert review.withdrawal_reason(_row(state="proposed"), NOW, watched=set()) is None


# --------------------------------------------------------------------------
# the pipeline, against real stores
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


@pytest.fixture
def home(monkeypatch):
    """A one-device home: a power sensor and the switch that feeds it."""
    snapshot = {
        "areas": [],
        "devices": [{"id": "dev1", "name": "TV plug"}],
        "entities": [{"entity_id": SENSOR, "device_id": "dev1"},
                     {"entity_id": SWITCH, "device_id": "dev1"}],
        "states": {
            SENSOR: {"state": "12.0", "attributes": {
                "unit_of_measurement": "W", "device_class": "power"}},
            SWITCH: {"state": "on", "attributes": {"friendly_name": "TV socket"}},
        },
    }
    monkeypatch.setattr(watchlist, "watched_entities", lambda: [SENSOR, SWITCH])
    monkeypatch.setattr(registry, "fetch_registry", lambda: (True, snapshot))
    return snapshot


@pytest.fixture
def offline_ha(monkeypatch):
    """The only thing stubbed in the pipeline tests: the test-create both
    preflights make. Everything else — the detector, the builder, the replay,
    the stores and the gate — runs for real."""
    monkeypatch.setattr(automations, "_validate_by_test_create", lambda cfg: (True, "temp-id"))
    monkeypatch.setattr(gate, "_ha_get", lambda path: (False, "not found"))


def _plant(state="12.0", start=WINDOW_START, end=NOW, entity_id=SENSOR, active="120.0"):
    """A heartbeat row every ten minutes, the way NUMERIC_MAX_GAP_SECONDS
    leaves one on a value that never moves. `state` may be a callable.

    `active` is what the device draws between eight and ten in the evening, and
    every series here has one: the detector rejects a device with a single
    level, because a flat draw with no in-use state above it is a router rather
    than a standby load. It is clear of the night window, so nothing any night
    assertion below turns on is touched by it."""
    for ts in range(int(start), int(end), HEARTBEAT):
        in_use = 20 <= datetime.fromtimestamp(ts).hour < 22
        value = str(active if (active is not None and in_use)
                    else (state(ts) if callable(state) else state))
        record.record_transition(entity_id, ts, value, value, HEARTBEAT)


def _used_at(hour, day, otherwise="12.0", during="100.0"):
    """A state callable: the device drawing `during` for an hour on one local
    date, and idling the rest of the time."""
    def state(ts):
        moment = datetime.fromtimestamp(ts)
        if moment.date() == day and moment.hour == hour:
            return during
        return otherwise
    return state


class TestThePipeline:
    """Record to question, with nothing between the five modules stubbed."""

    def test_a_standby_load_becomes_one_proposed_change(self, stores, home, offline_ha):
        _plant()

        summary = review.run_once(now=NOW)

        assert summary["candidates"] == 1
        assert summary["validated"] == 1
        assert summary["proposed"] == 1

        stored = suggestions.open_for(KEY)
        assert stored["state"] == "proposed"
        # All three together, because they are one fact: this config, replayed,
        # gave this estimate.
        assert stored["config"]["alias"] == "TV socket off overnight"
        assert stored["replay"]["nights_saved"] == 30
        assert stored["estimate"]["kwh_per_month"] == 1.44

        change = activity.change(stored["change_id"])
        assert change["source"] == activity.AGENT_SOURCE
        assert change["decision"] == "proposed"

    def test_the_question_reaches_the_user_through_the_outbox(self, stores, home, offline_ha):
        """§4: the producer says what happened, and core/delivery.py decides
        where it goes. What matters here is that something was queued at all."""
        _plant()

        review.run_once(now=NOW)

        waiting = [m for m in activity.pending("chat") if m["kind"] == "approval_request"]
        assert len(waiting) == 1
        assert "TV socket off overnight" in waiting[0]["body"]

    def test_the_replay_grades_the_series_the_detector_was_judged_on(self, stores, home,
                                                                     offline_ha, monkeypatch):
        """Decision 4. The record is read once per CANDIDATE, by the detector's
        own scan, and the rows travel with the candidate — a second read would
        be a different window, and the config would be graded against nights
        the printed measurement never saw.

        Two reads, not one, and that is decision 6 rather than a breach of this
        one: there are two detectors and each derives its own window, thirty
        days for the night and ninety for the schedule. Stated as literals
        because the numbers are the point — a shared read would have to be the
        longer window sliced by the pass, which is the pass deciding how far
        back a detector looks."""
        _plant()
        reads = []
        real_between = record.between

        def counted(entity_id, start, end):
            reads.append((entity_id, start, end))
            return real_between(entity_id, start, end)

        monkeypatch.setattr(record, "between", counted)

        review.run_once(now=NOW)

        assert reads == [(SENSOR, WINDOW_START, NOW),
                         (SENSOR, SCHEDULE_WINDOW_START, NOW)]

    def test_a_rule_the_record_proves_would_have_been_wrong_is_not_proposed(
            self, stores, home, offline_ha):
        """Decision 2, end to end: the device is idle at 01:00 — so the guard
        passes and the rule would have fired — and in use at 03:00, which is
        proof it would have cut power to something being used."""
        _plant(state=_used_at(3, date(2026, 6, 15)))

        summary = review.run_once(now=NOW)

        assert summary["candidates"] == 1
        assert (summary["validated"], summary["proposed"]) == (0, 0)
        # Recorded, and left where tomorrow's pass will grade it again.
        assert suggestions.open_for(KEY)["state"] == "candidate"

    def test_nothing_is_proposed_from_a_home_with_no_waste(self, stores, home, offline_ha):
        _plant(state="0.0")

        summary = review.run_once(now=NOW)

        assert summary == {"candidates": 0, "validated": 0, "settled": 0,
                           "verified": 0, "retired": 0, "digest": 0, "proposed": 0}
        assert suggestions.open_for(KEY) is None

    def test_a_second_run_the_same_day_asks_nothing_more(self, stores, home, offline_ha):
        _plant()

        review.run_once(now=NOW)
        again = review.run_once(now=NOW)

        assert again["proposed"] == 0
        assert len([c for c in activity.changes() if c["source"] == activity.AGENT_SOURCE]) == 1

    def test_tomorrow_keeps_the_replay_it_already_has(self, stores, home, offline_ha, monkeypatch):
        """Decision 1, end to end. The window slides, the evidence blob moves,
        and the suggestion stays exactly where it was — with the grade it was
        given, not a fresh one taken over a config nobody re-preflighted."""
        _plant()
        review.run_once(now=NOW)
        proposed = suggestions.open_for(KEY)

        builds = []
        monkeypatch.setattr(builder, "build",
                            lambda *a, **kw: builds.append(a) or {"ok": False, "error": "no"})
        review.run_once(now=NOW + DAY)

        after = suggestions.open_for(KEY)
        assert builds == []
        assert after["state"] == "proposed"
        assert after["replay"] == proposed["replay"]
        assert after["evidence"]["window"] == proposed["evidence"]["window"]

    def test_a_candidate_the_store_could_not_record_goes_no_further(
            self, stores, home, offline_ha, monkeypatch):
        """A locked or unwritable database. There is nothing to hang a replay,
        a proposal or a measurement on, so nothing is proposed — and tomorrow
        finds the same waste under the same key."""
        _plant()
        monkeypatch.setattr(suggestions, "upsert_candidate", lambda *a, **kw: None)

        summary = review.run_once(now=NOW)

        assert (summary["validated"], summary["proposed"]) == (0, 0)

    def test_a_config_home_assistant_rejects_is_never_stored_as_validated(
            self, stores, home, offline_ha, monkeypatch):
        """builder.build's preflight, doing its job: a stored config HA would
        reject is a suggestion the user can accept and the agent can never
        apply."""
        _plant()
        monkeypatch.setattr(automations, "_validate_by_test_create",
                            lambda cfg: (False, "entity switch.tv does not exist"))

        summary = review.run_once(now=NOW)

        assert (summary["validated"], summary["proposed"]) == (0, 0)
        stored = suggestions.open_for(KEY)
        assert (stored["state"], stored["config"]) == ("candidate", None)


# The household docs/PROPOSAL-SCHEDULE-DETECTOR.md opens with: out 07:00-17:00
# on weekdays, in front of the television until eleven, at home all weekend.
# BOTH detectors find something in it — a flat 12 W across standby's
# 01:00-05:00 night, and the daytime stretch the proposal is about — which
# makes it the case decision 5's last clause exists for, and a real one rather
# than a contrived one.
def _office(ts) -> str:
    moment = datetime.fromtimestamp(int(ts))
    if moment.date().weekday() < 5:
        return "120.0" if (6 <= moment.hour < 7 or 17 <= moment.hour < 23) else "12.0"
    return "120.0" if 8 <= moment.hour < 23 else "12.0"


def _plant_office(end=NOW):
    """Ninety days of it, because that is the window the schedule detector
    reads and eight observations of a weekday is what its floor asks for."""
    _plant(state=_office, start=SCHEDULE_WINDOW_START, end=end, active=None)


class TestTwoDetectorsOnOneDevice:
    """A television with a night plateau and a daytime one. Both claims are
    true, both are found, and only one of them is a question."""

    def test_both_are_found_and_graded(self, stores, home, offline_ha):
        _plant_office()

        summary = review.run_once(now=NOW)

        assert (summary["candidates"], summary["validated"]) == (2, 2)
        assert suggestions.open_for(KEY)["kind"] == standby.KIND
        assert suggestions.open_for(SCHEDULE_KEY)["kind"] == schedule.KIND

    def test_and_only_the_bigger_one_is_asked(self, stores, home, offline_ha):
        """Ranked across both detectors on the estimate: ten hours on twenty-odd
        weekdays beats four hours on thirty nights."""
        _plant_office()

        summary = review.run_once(now=NOW)

        assert summary["proposed"] == 1
        assert suggestions.open_for(SCHEDULE_KEY)["state"] == "proposed"
        assert suggestions.open_for(KEY)["state"] == "validated"

    def test_tomorrow_does_not_ask_the_other_one(self, stores, home, offline_ha):
        """With a slot free and none raised today, `claimed_entities` is the
        only thing holding it back."""
        _plant_office(end=NOW + 2 * DAY)
        review.run_once(now=NOW)
        assert review.proposals_allowed(open_now=1, proposed_today=0) == 1

        summary = review.run_once(now=NOW + DAY)

        assert summary["proposed"] == 0
        assert suggestions.open_for(KEY)["state"] == "validated"

    def test_answering_the_first_frees_the_device_for_the_second(
            self, stores, home, offline_ha):
        """A "no" to the daytime question is a cooldown on THAT key, not on the
        television — so the night claim is asked next, on its own merits."""
        _plant_office(end=NOW + 2 * DAY)
        review.run_once(now=NOW)
        asked = suggestions.open_for(SCHEDULE_KEY)
        activity.record_decision(asked["change_id"], "rejected", status="not_applied")
        review.settle(asked["change_id"], now=NOW)

        summary = review.run_once(now=NOW + DAY)

        assert summary["proposed"] == 1
        assert suggestions.open_for(KEY)["state"] == "proposed"
        assert suggestions.open_for(SCHEDULE_KEY)["state"] == "dismissed"

    def test_the_two_questions_are_about_two_different_windows(
            self, stores, home, offline_ha):
        """Which is why both are worth holding: 01:00-05:00 every night and
        07:00-17:00 on weekdays do not overlap, and the estimates do not double
        count. The labels are core/window.py's, rendered once."""
        _plant_office()

        review.run_once(now=NOW)

        assert suggestions.open_for(KEY)["evidence"]["night_window"] == "01:00-05:00 local"
        assert suggestions.open_for(SCHEDULE_KEY)["evidence"]["schedule_window"] == \
            "07:00-17:00 local Mon-Fri"


class TestProposingThroughCreate:
    """Decision 3: an automation that does not exist yet is created, never
    updated."""

    def test_the_tool_is_one_the_gate_can_propose(self):
        assert review.PROPOSAL_TOOL in gate.PROPOSABLE_TOOLS

    def test_a_proposed_creation_is_journalled_as_reversible(self, stores, home, offline_ha):
        _plant()

        review.run_once(now=NOW)

        change = activity.change(suggestions.open_for(KEY)["change_id"])
        assert change["tool"] == "Create Home Assistant automation"
        assert change["reversibility"] == "reversible"
        assert json.loads(change["args"]["config_json"])["alias"] == "TV socket off overnight"

    def test_which_an_update_against_nothing_would_not_have_been(self, stores, offline_ha):
        """The trap builder.py names. Home Assistant's config endpoint is the
        same POST whether the id exists or not, so proposing an update would
        have SUCCEEDED — and been journalled one_way, a reversible change
        advertised as irreversible with no inverse anything could run."""
        raised = gate.propose("Update Home Assistant automation",
                              {"automation_id": "ceelabs_0123456789abcdef",
                               "config_json": '{"alias": "x", "trigger": [], "action": []}'})

        assert activity.change(raised["change_id"])["reversibility"] == "one_way"


class TestWithdrawingAProposal:
    def test_evidence_that_no_longer_overlaps_today_withdraws_the_question(
            self, stores, home, offline_ha):
        """And retires the suggestion rather than dismissing it: a retired key
        sits outside the store's unique index, so the same waste is raised
        again, replayed again, and asked with a number that is true."""
        _plant()
        review.run_once(now=NOW)
        change_id = suggestions.open_for(KEY)["change_id"]

        later = NOW + (review.evidence_max_age_days(standby.KIND) + 1) * DAY
        summary = review.run_once(now=later)

        assert summary["retired"] == 1
        assert suggestions.suggestion(1)["state"] == "retired"
        withdrawn = activity.change(change_id)
        assert withdrawn["decision"] == "expired"
        assert withdrawn["status"] == "not_applied"
        assert not [m for m in activity.pending("chat") if m["id"] == withdrawn["notice_id"]]

    def test_a_device_that_disappeared_takes_its_question_with_it(
            self, stores, home, offline_ha, monkeypatch):
        """End to end: §11's case, within one review rather than within a
        month of evidence ageing out."""
        _plant()
        review.run_once(now=NOW)
        change_id = suggestions.open_for(KEY)["change_id"]
        monkeypatch.setattr(watchlist, "watched_entities", lambda: ["sensor.something_else"])

        summary = review.run_once(now=NOW + DAY)

        assert summary["retired"] == 1
        assert activity.change(change_id)["decision"] == "expired"

    def test_a_proposal_answered_while_the_pass_ran_is_left_alone(
            self, stores, home, offline_ha, monkeypatch):
        """activity.claim_decision is the boundary: exactly one of the two can
        win, and losing means the user's answer stands."""
        _plant()
        review.run_once(now=NOW)
        row = suggestions.open_for(KEY)
        activity.claim_decision(row["change_id"], "approved")

        later = NOW + (review.evidence_max_age_days(standby.KIND) + 1) * DAY
        summary = review.run_once(now=later)

        assert summary["retired"] == 0
        assert suggestions.suggestion(row["id"])["state"] == "proposed"


# --------------------------------------------------------------------------
# the seam core/gate.py leaves open
# --------------------------------------------------------------------------
class TestTheApplySeam:
    """On approval the suggestion learns what became of it, or WP9 has nothing
    to read."""

    def _proposed(self):
        review.run_once(now=NOW)
        return suggestions.open_for(KEY)

    def _answer(self, row, decision="approved", status="applied", result="{}", at=NOW):
        """Settles the journal row as the gate would have, at a controlled
        instant. The clock has to be pinned for that one call: `decided_at` is
        what the baseline window ends at, and against the real one it would end
        months after the record this test planted — measuring a week with
        nothing in it. Restored by hand rather than through monkeypatch, whose
        undo() is global to the test and would take the fixtures with it."""
        real_now = activity._now
        activity._now = lambda: int(at)
        try:
            activity.record_decision(row["change_id"], decision, status=status, result=result)
        finally:
            activity._now = real_now

    def test_an_applied_change_captures_the_baseline_and_when_to_look_again(
            self, stores, home, offline_ha):
        _plant()
        row = self._proposed()
        self._answer(row, result='{"created": true}')

        assert review.settle(row["change_id"], now=NOW) is True

        applied = suggestions.suggestion(row["id"])
        assert applied["state"] == "applied"
        # A week from when the automation started running, counted off the
        # journal's own instant rather than off whenever this noticed.
        assert applied["verify_after"] == NOW + review.VERIFY_AFTER_DAYS * DAY
        # The measurement itself, not just its timestamps: the same statistic
        # the detector used, over the week the record actually holds.
        baseline = applied["baseline"]
        assert (baseline["applied_at"], baseline["measured_at"]) == (NOW, NOW)
        assert baseline["watts"] == 12.0
        assert baseline["nights"] == baseline["nights_examined"] > 0
        assert baseline["entity"] == SENSOR

    def test_the_baseline_is_a_week_of_nights_not_the_detectors_month(
            self, stores, home, offline_ha):
        """§7.2: compare like periods. A thirty-day before against a seven-day
        after is a difference in the measurement rather than in the home."""
        _plant()
        row = self._proposed()
        self._answer(row)

        review.settle(row["change_id"], now=NOW)

        baseline = suggestions.suggestion(row["id"])["baseline"]
        assert baseline["window"]["days"] == review.VERIFY_AFTER_DAYS
        assert 0 < baseline["nights_examined"] <= review.BASELINE_DAYS
        assert baseline["unit_scale_w"] == 1.0

    def test_a_week_the_agent_could_not_read_is_empty_rather_than_zero(
            self, stores, home, offline_ha):
        """A device applied before the record reaches back a week. Reporting
        0.0 kWh there is indistinguishable from a device that drew nothing, and
        §7.2's verification would subtract a measurement that never happened."""
        _plant()
        row = self._proposed()
        self._answer(row, at=NOW + 30 * DAY)

        review.settle(row["change_id"], now=NOW + 30 * DAY)

        baseline = suggestions.suggestion(row["id"])["baseline"]
        assert baseline["nights"] == 0
        assert baseline["watts"] is None
        assert baseline["kwh_per_month"] is None

    def test_a_baseline_that_could_not_be_measured_still_records_the_attempt(
            self, stores, home, offline_ha, monkeypatch):
        """The change is already live in someone's home. Refusing to record a
        baseline because Home Assistant was slow would leave the suggestion at
        `proposed` with an automation running — a worse lie than saying what
        could not be measured."""
        _plant()
        row = self._proposed()
        activity.record_decision(row["change_id"], "approved", status="applied", result="{}")
        monkeypatch.setattr(registry, "fetch_registry", lambda: (False, "HA is down"))

        assert review.settle(row["change_id"], now=NOW) is True

        applied = suggestions.suggestion(row["id"])
        assert applied["state"] == "applied"
        assert "HA is down" in applied["baseline"]["error"]

    def test_a_rejection_is_a_cooldown_rather_than_a_verdict(self, stores, home, offline_ha):
        _plant()
        row = self._proposed()
        activity.record_decision(row["change_id"], "rejected", status="not_applied")

        review.settle(row["change_id"], now=NOW)

        dismissed = suggestions.suggestion(row["id"])
        assert dismissed["state"] == "dismissed"
        assert dismissed["dismissed_until"] > int(time.time())

    def test_a_dismissed_suggestion_is_not_raised_again_while_it_holds(
            self, stores, home, offline_ha):
        _plant()
        row = self._proposed()
        activity.record_decision(row["change_id"], "rejected", status="not_applied")
        review.settle(row["change_id"], now=NOW)

        summary = review.run_once(now=NOW + DAY)

        assert summary["proposed"] == 0
        assert suggestions.suggestion(row["id"])["state"] == "dismissed"

    def test_a_write_that_never_landed_frees_the_key_to_be_asked_again(
            self, stores, home, offline_ha):
        """Refused at apply time, or the write failed. The change is closed and
        cannot be answered again, so a suggestion left `proposed` would sit
        against a dead question forever."""
        _plant()
        row = self._proposed()
        activity.record_decision(row["change_id"], "approved", status="failed",
                                 result='{"error": "HA said no"}')

        review.settle(row["change_id"], now=NOW)

        assert suggestions.suggestion(row["id"])["state"] == "retired"

    def test_it_reads_the_journal_rather_than_the_tool_result(self, stores, home, offline_ha):
        """gate.apply has already decided whether the write landed and written
        that down; a second reading here could disagree with the audit trail."""
        _plant()
        row = self._proposed()
        activity.record_decision(row["change_id"], "approved", status="applied", result="")

        assert review.settle(row["change_id"], now=NOW) is True
        assert suggestions.suggestion(row["id"])["state"] == "applied"

    def test_an_answer_nobody_heard_is_settled_by_the_next_pass(
            self, stores, home, offline_ha):
        """A restart between gate.apply and settle would otherwise leave the
        suggestion at `proposed` for ever, against a question the journal says
        is settled — holding one of the three slots and its key with it."""
        _plant()
        row = self._proposed()
        activity.record_decision(row["change_id"], "approved", status="applied", result="{}")

        summary = review.run_once(now=NOW + DAY)

        assert summary["settled"] == 1
        assert suggestions.suggestion(row["id"])["state"] == "applied"

    def test_and_measures_the_before_up_to_when_the_change_landed(
            self, stores, home, offline_ha):
        """Not up to when it noticed. A "before" that included a night the
        automation was already running would be measuring the after, which is
        the one thing a baseline exists to avoid — and the record still holds
        those days, so reading the earlier window costs nothing."""
        _plant()
        row = self._proposed()
        activity.record_decision(row["change_id"], "approved", status="applied", result="{}")
        applied_at = activity.change(row["change_id"])["decided_at"]

        review.run_once(now=NOW + 3 * DAY)

        baseline = suggestions.suggestion(row["id"])["baseline"]
        assert baseline["applied_at"] == applied_at
        assert baseline["window"]["to"] == date.fromtimestamp(applied_at).isoformat()
        assert baseline["measured_at"] == NOW + 3 * DAY

    def test_a_write_still_in_flight_is_left_where_it_is(self, stores, home, offline_ha):
        """Claimed but not settled is "answered, outcome unknown" — the one
        state nobody may read as an outcome. gate.apply is executing it, or the
        startup sweep that owns that pair has not run yet."""
        _plant()
        row = self._proposed()
        activity.claim_decision(row["change_id"], "approved")

        summary = review.run_once(now=NOW + DAY)

        assert summary["settled"] == 0
        assert suggestions.suggestion(row["id"])["state"] == "proposed"

    def test_a_write_still_in_flight_is_never_read_as_a_failure(
            self, stores, home, offline_ha):
        """A double-tapped Apply. One caller wins activity.claim_decision and
        is inside the Home Assistant write — decision off 'proposed', status
        still NULL — while the loser's gate.apply executes nothing, returns,
        and this runs anyway. Reading that pair as an outcome would retire a
        suggestion whose automation is about to be live in the home, leaving it
        with no baseline, no verify_after, and its key free for tomorrow's pass
        to propose the same automation a second time."""
        _plant()
        row = self._proposed()
        assert activity.claim_decision(row["change_id"], "approved") is True

        assert review.settle(row["change_id"], now=NOW) is False
        assert suggestions.suggestion(row["id"])["state"] == "proposed"

        # ...and when the winner's write lands, the suggestion is still there
        # to receive it.
        self._answer(row)
        assert review.settle(row["change_id"], now=NOW) is True
        assert suggestions.suggestion(row["id"])["state"] == "applied"

    def test_a_change_no_suggestion_raised_moves_nothing(self, stores, offline_ha):
        """Most of the journal is conversation."""
        change_id = activity.record_proposal(tool="Delete Home Assistant automation",
                                             args={"automation_id": "1748"}, session_id="s")

        assert review.settle(change_id, now=NOW) is False

    def test_answering_twice_moves_nothing_the_second_time(self, stores, home, offline_ha):
        _plant()
        row = self._proposed()
        activity.record_decision(row["change_id"], "approved", status="applied", result="{}")
        review.settle(row["change_id"], now=NOW)

        assert review.settle(row["change_id"], now=NOW) is False


# --------------------------------------------------------------------------
# 6. cost and locking
# --------------------------------------------------------------------------
class TestCostAndLocking:
    """Thirty days times every watched power sensor, daily, against a store the
    observation thread is writing to constantly."""

    def test_the_whole_pass_runs_while_another_connection_holds_the_write_lock(
            self, stores, home, offline_ha):
        """record.py's hardest rule, applied to its heaviest reader. SQLite
        takes one writer at a time even in WAL and the observation thread
        writes constantly, so a pass that needed the record's write lock would
        block behind every observation — or, holding it, block them. It reads
        and never writes; everything it does write goes to activity.db, which
        is a different file."""
        _plant()
        writer = sqlite3.connect(record.DB_PATH, timeout=1)
        try:
            writer.execute("BEGIN IMMEDIATE")

            summary = review.run_once(now=NOW)

            assert summary["proposed"] == 1
            assert record._connect().in_transaction is False
        finally:
            writer.rollback()
            writer.close()

    def test_only_a_candidates_rows_are_held(self, stores, home, offline_ha):
        """An entity that produced no candidate has its series dropped where it
        was read, which is what keeps thirty days times every watched sensor
        out of memory at once."""
        home["devices"].append({"id": "dev2", "name": "Fridge plug"})
        home["entities"] += [{"entity_id": "sensor.fridge_power", "device_id": "dev2"},
                             {"entity_id": "switch.fridge", "device_id": "dev2"}]
        home["states"]["sensor.fridge_power"] = {"state": "0.0", "attributes": {
            "unit_of_measurement": "W", "device_class": "power"}}
        _plant()
        _plant(state="0.0", entity_id="sensor.fridge_power")

        found = standby.scan(now=NOW)

        assert [c["key"] for c in found] == [KEY]
        assert found[0]["rows"]


@pytest.mark.skipif(
    not conftest.STUBBED,
    reason="_interval_handlers is bookkeeping conftest's Agent stub adds for "
           "introspection — the real uagents.Agent exposes no such attribute.",
)
class TestTheAgentIsWiredToReview:
    """Structural safety net, stub-only. Nothing else calls the review pass, so
    a registration that quietly stopped happening would leave a home that
    observes, records and never suggests — with every other test still green."""

    def test_the_daily_pass_is_registered_as_an_interval(self):
        from core.uagent import agent_1
        from tasks import review as review_task

        assert review_task.review_pass in agent_1._interval_handlers

    def test_the_interval_is_finer_than_the_cadence_it_serves(self):
        """The period is not the cadence: review.due decides that. A period of
        a day would run the pass at whatever time the add-on last restarted,
        which is the one thing the fixed hour exists to avoid."""
        from tasks import review as review_task

        assert review_task.REVIEW_INTERVAL_SECONDS < 3600


# --------------------------------------------------------------------------
# analyst rows in the pass — proposal §5B, WP-D1 (D-5, D-6, D-7)
# --------------------------------------------------------------------------
MOTION = "sensor.hall_motion"
HALL = "light.hall"
FAN = "fan.hall"  # config-only, and sorts FIRST in the row's entities


def _matched(end=NOW - DAY, a=MOTION, b=HALL):
    return {"text": "The hall light followed motion 14 times", "type": "follows",
            "params": {"a": a, "b": b, "start": end - 20 * DAY, "end": end},
            "stated": 14, "recounted": 14, "result": "matched"}


def _analyst_row(**overrides):
    """An analyst row as the retired claim audit left it (`mark_audited`,
    free-hand F-9): no evidence, no estimate, no replay, sorted entities.
    `_shown_row` below is the explorer's; rows like this one are still in
    stores written before 2.23.0, and the review withdraws them."""
    row = {
        "id": 7,
        "key": "analyst:0123456789abcdef",
        "kind": "analyst",
        "source": "analyst",
        "state": "validated",
        "updated_at": NOW,
        "evidence_level": "checked",
        "entities": sorted([FAN, HALL, MOTION]),
        "evidence": None,
        "estimate": None,
        "replay": None,
        "claims": [_matched()],
        "config": {"alias": "Hall light and fan on with motion",
                   "trigger": [{"platform": "state", "entity_id": MOTION, "to": "on"}],
                   "action": [{"service": "light.turn_on", "target": {"entity_id": HALL}},
                              {"service": "fan.turn_on", "target": {"entity_id": FAN}}]},
    }
    row.update(overrides)
    return row


class TestAnalystWithdrawal:
    """D-5 after free-hand F-9 (2.23.0): a row the retired claim audit stored
    is withdrawn outright; a `shown` row keeps the watch and age rules
    (TestShownRows)."""

    @pytest.mark.parametrize("state", ["candidate", "validated", "proposed"])
    @pytest.mark.parametrize("level", ["checked", "untested"])
    def test_a_row_the_claim_audit_stored_is_withdrawn(self, state, level):
        row = _analyst_row(state=state, evidence_level=level)

        assert review.withdrawal_reason(row, NOW, watched={MOTION, HALL, FAN}) == \
            review.CLAIM_AUDIT_RETIRED

    def test_even_on_an_empty_watchlist(self):
        """The watch rule skips an empty watchlist; this one is not about
        watching at all."""
        assert review.withdrawal_reason(_analyst_row(), NOW, watched=set()) == \
            review.CLAIM_AUDIT_RETIRED

    def test_the_reason_says_what_happened_and_what_comes_next(self):
        assert "retired" in review.CLAIM_AUDIT_RETIRED
        assert "chart" in review.CLAIM_AUDIT_RETIRED

    def test_the_limit_is_the_no_detector_one(self):
        assert review.evidence_max_age_days("analyst") == review._LONGEST_LOOKBACK_DAYS

    def test_a_dismissed_or_applied_analyst_row_is_not_this_rules(self):
        for level in ("checked", "shown"):
            for state in ("dismissed", "applied"):
                assert review.withdrawal_reason(
                    _analyst_row(state=state, evidence_level=level,
                                 updated_at=NOW - 300 * DAY),
                    NOW, watched={"sensor.elsewhere"}) is None


class TestRankingAcrossSources:
    """D-6: saving, then label, then credited firings, then key."""

    def test_a_literal_mix_of_all_three_labels_and_detector_rows(self):
        saving = _row(key="standby:sensor.a", evidence_level="replayed",
                      estimate={"kwh_per_month": 0.5})
        no_saving = _row(key="standby:sensor.z", evidence_level="replayed",
                         estimate=None, replay={"nights_saved": 3})
        # Keys chosen against the label: the untested one would win on key.
        untested = _analyst_row(key="analyst:0000", evidence_level="untested")
        checked_b = _analyst_row(key="analyst:bbbb", evidence_level="checked")
        checked_a = _analyst_row(key="analyst:aaaa", evidence_level="checked")
        unreadable = _analyst_row(key="analyst:----", evidence_level="guessed")

        ranked = review.rank([unreadable, untested, checked_b, no_saving, checked_a, saving])

        assert [r["key"] for r in ranked] == [
            "standby:sensor.a",   # a saving beats any label: M-1's accepted consequence
            "standby:sensor.z",   # zero kWh, replayed
            "analyst:aaaa",       # checked, key decides between equals
            "analyst:bbbb",
            "analyst:0000",       # untested
            "analyst:----",       # a label it cannot read, after all three
        ]


def _shown_row(**overrides):
    """An explorer-run row as mark_shown leaves it (free-hand F-10): no
    claims, a chart request whose entities are its evidence."""
    return _analyst_row(**{
        "evidence_level": "shown", "claims": None,
        "finding": "The hall light follows motion almost every evening.",
        "rule_text": "When motion is seen in the hall, turn the hall light on.",
        "chart": {"spec": {"type": "overlay", "entities": [MOTION, HALL], "state": None,
                           "start": NOW - 5 * DAY, "end": NOW},
                  "series": None, "markers": None, "error": None},
        **overrides})


class TestShownRows:
    """F-10 beside D-5 and D-6: a `shown` row's evidence entities are its
    chart's, its age runs from `updated_at`, and it ranks where `untested`
    does."""

    def test_the_fixture_puts_a_config_only_entity_first(self):
        """The rule _observed would have applied reads entities[0]. Here that
        is the fan, which nothing charted — so the tests below prove it is not
        the rule being applied."""
        assert _shown_row()["entities"][0] == FAN

    def test_an_empty_watchlist_retires_nothing_on_watch_grounds(self):
        assert review.withdrawal_reason(_shown_row(state="proposed"), NOW, watched=set()) is None

    def test_an_idea_the_analyst_stopped_raising_is_retired_on_the_unseen_rule(self):
        assert review.withdrawal_reason(
            _shown_row(state="candidate", updated_at=NOW - review.UNSEEN_RETIRE_DAYS * DAY), NOW)
        assert review.withdrawal_reason(_shown_row(updated_at=NOW - DAY), NOW) is None

    def test_an_unwatched_chart_entity_retires_it(self):
        reason = review.withdrawal_reason(_shown_row(), NOW, watched={HALL, FAN})

        assert reason and MOTION in reason and "chart" in reason

    def test_all_chart_entities_watched_keeps_it(self):
        assert review.withdrawal_reason(_shown_row(), NOW, watched={MOTION, HALL}) is None

    def test_a_config_only_entity_still_does_not(self):
        """FAN is only in the config, never in the chart: D-5's rule holds."""
        assert review.withdrawal_reason(_shown_row(), NOW, watched={MOTION, HALL}) is None
        assert FAN not in _shown_row()["chart"]["spec"]["entities"]

    def test_a_chart_the_record_could_not_draw_still_names_its_entities(self):
        row = _shown_row(chart={"spec": {"type": "overlay", "entities": [MOTION, "light.gone"]},
                                "series": None, "markers": None, "error": "not watched"})

        reason = review.withdrawal_reason(row, NOW, watched={MOTION, HALL})

        assert reason and "light.gone" in reason

    def test_a_proposed_shown_row_ages_from_updated_at(self):
        limit = review.evidence_max_age_days("analyst")

        assert review.withdrawal_reason(
            _shown_row(state="proposed", updated_at=NOW - (limit - 1) * DAY), NOW) is None
        assert review.withdrawal_reason(
            _shown_row(state="proposed", updated_at=NOW - limit * DAY), NOW)

    def test_it_ranks_where_untested_does_with_all_four_labels(self):
        saving = _row(key="standby:sensor.a", evidence_level="replayed",
                      estimate={"kwh_per_month": 0.5})
        replayed = _row(key="standby:sensor.z", evidence_level="replayed",
                        estimate=None, replay={"nights_saved": 3})
        checked = _analyst_row(key="analyst:cccc", evidence_level="checked")
        # Keys interleaved: between `shown` and `untested` the key alone decides,
        # which is what "sorts where untested does" means.
        shown_a = _shown_row(key="analyst:aaaa")
        untested_b = _analyst_row(key="analyst:bbbb", evidence_level="untested")
        shown_d = _shown_row(key="analyst:dddd")

        ranked = review.rank([shown_d, untested_b, checked, shown_a, replayed, saving])

        assert [r["key"] for r in ranked] == [
            "standby:sensor.a", "standby:sensor.z", "analyst:cccc",
            "analyst:aaaa", "analyst:bbbb", "analyst:dddd",
        ]


@pytest.fixture
def refusing_gate(monkeypatch):
    error = "Message malformed: extra keys not allowed @ data['action'][0]['servce']"
    monkeypatch.setattr(gate, "propose", lambda tool, args: {"ok": False, "error": error})
    return error


class TestARefusedPreflight:
    """D-7: an analyst row's config is fixed text and will fail again; a
    detector row's is rebuilt from fresh evidence every morning."""

    def test_retires_an_analyst_row_with_the_gates_error(self, stores, refusing_gate):
        row = _analyst_row()
        suggestion_id = suggestions.upsert_analyst_candidate(
            row["key"], entities=row["entities"], title=row["config"]["alias"],
            rationale="Motion is always followed by the light.", model="asi1-mini")
        shown = _shown_row()
        assert suggestions.mark_shown(suggestion_id, config=row["config"],
                                      finding=shown["finding"], rule_text=shown["rule_text"],
                                      chart=shown["chart"])

        assert review._propose_one(suggestions.suggestion(suggestion_id)) is False

        stored = suggestions.suggestion(suggestion_id)
        assert stored["state"] == "retired"
        assert stored["retired_reason"] == refusing_gate
        assert suggestions.open_for(row["key"]) is None, "the analyst may raise it afresh"

    def test_leaves_a_detector_row_validated(self, stores, refusing_gate):
        row = _row()
        suggestion_id = suggestions.upsert_candidate(KEY, standby.KIND,
                                                     entities=row["entities"],
                                                     evidence=row["evidence"])
        assert suggestions.mark_validated(suggestion_id, replay=row["replay"],
                                          estimate=row["estimate"], config=row["config"])

        assert review._propose_one(suggestions.suggestion(suggestion_id)) is False

        assert suggestions.suggestion(suggestion_id)["state"] == "validated"


class TestThePassStillReachesNoModel:
    def test_review_imports_neither_the_analyst_nor_a_model(self):
        """D-1: analyst rows reach the pass as rows in the store. The module
        docstring's "No model, anywhere" stays literally true."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(review))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported |= {f"{node.module}.{a.name}" for a in node.names}
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}

        assert not imported & {"core.analyst", "core.agent_runtime", "core.models"}


# --------------------------------------------------------------------------
# free-hand F-12 (F-WP9): the analyst's weekly digest
# --------------------------------------------------------------------------
# A Saturday at the review hour, and the one before it. UTC by the autouse
# fixture, so the arithmetic below is wall-clock arithmetic.
SATURDAY = review.DIGEST_WEEKDAYS.index("saturday")
DIGEST_AT = int(datetime(2026, 7, 4, review.REVIEW_HOUR, 0, tzinfo=timezone.utc).timestamp())
LAST_WEEK = DIGEST_AT - 7 * DAY


class TestTheDigestDay:
    @pytest.mark.parametrize("name", review.DIGEST_WEEKDAYS)
    def test_every_option_value_is_its_own_day(self, name):
        assert review.digest_weekday(name) == review.DIGEST_WEEKDAYS.index(name)

    def test_case_and_whitespace_are_forgiven(self):
        assert review.digest_weekday("  Sunday ") == 6

    @pytest.mark.parametrize("bad", [None, "", "funday", "sat", 5, "saturday,sunday"])
    def test_anything_else_is_the_default_never_no_digest(self, bad):
        assert review.digest_weekday(bad) == SATURDAY
        assert review.DEFAULT_DIGEST_DAY == "saturday"

    def test_the_default_matches_the_setting(self):
        from core import settings
        assert review.digest_weekday(settings.DIGEST_DAY) == SATURDAY

    def test_the_moment_is_the_days_review_hour(self):
        assert time.gmtime(DIGEST_AT).tm_wday == SATURDAY
        assert review.digest_instant(DIGEST_AT, SATURDAY) == DIGEST_AT
        assert review.digest_instant(DIGEST_AT + 5 * 3600, SATURDAY) == DIGEST_AT
        assert review.digest_instant(DIGEST_AT + 3 * DAY, SATURDAY) == DIGEST_AT

    def test_before_the_hour_on_the_day_it_is_last_weeks(self):
        assert review.digest_instant(DIGEST_AT - 60, SATURDAY) == LAST_WEEK

    def test_the_week_the_clocks_change_keeps_the_hour(self, monkeypatch):
        """2026-03-29 is the Sunday Europe/Berlin loses an hour at 02:00."""
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        sunday_noon = int(datetime(2026, 3, 29, 10, 0, tzinfo=timezone.utc).timestamp())

        instant = review.digest_instant(sunday_noon, 6)

        assert time.localtime(instant).tm_hour == review.REVIEW_HOUR
        assert time.localtime(instant).tm_mday == 29
        assert time.localtime(review.digest_instant(sunday_noon + 7 * DAY, 6)).tm_hour == \
            review.REVIEW_HOUR


class TestWhenTheDigestIsDue:
    def test_on_the_day_after_the_hour(self):
        assert review.digest_due(DIGEST_AT + 60, SATURDAY, LAST_WEEK) is True

    def test_not_before_the_hour_nor_on_another_day(self):
        assert review.digest_due(DIGEST_AT - 60, SATURDAY, LAST_WEEK) is False
        assert review.digest_due(DIGEST_AT - 2 * DAY, SATURDAY, LAST_WEEK) is False

    def test_a_restart_on_the_day_holds_nothing_twice(self):
        held = DIGEST_AT + 600
        assert review.digest_due(DIGEST_AT + 3600, SATURDAY, held) is False
        assert review.digest_due(DIGEST_AT + 12 * 3600, SATURDAY, held) is False

    def test_a_missed_day_is_caught_up_the_next_morning_once(self):
        sunday = DIGEST_AT + DAY
        assert review.digest_due(sunday, SATURDAY, LAST_WEEK) is True
        assert review.digest_due(sunday + DAY, SATURDAY, sunday) is False
        assert review.digest_due(DIGEST_AT + 7 * DAY, SATURDAY, sunday) is True

    @pytest.mark.parametrize("junk", [None, "", "yesterday"])
    def test_no_readable_stamp_is_never_due(self, junk):
        assert review.digest_due(DIGEST_AT + 60, SATURDAY, junk) is False


class TestHowManyTheDigestRaises:
    def test_it_tops_up_to_three(self):
        assert review.DIGEST_SIZE == 3
        assert [review.digest_allowed(n) for n in range(5)] == [3, 2, 1, 0, 0]

    @pytest.mark.parametrize("junk", [None, "three"])
    def test_an_unreadable_count_raises_nothing(self, junk):
        assert review.digest_allowed(junk) == 0

    def test_the_detectors_limits_are_unchanged(self):
        assert review.MAX_PROPOSALS_PER_RUN == 1
        assert review.MAX_OPEN_PROPOSALS == 3
        assert review.proposals_allowed(open_now=0, proposed_today=0) == 1

    def test_longest_waiting_first_then_rank(self):
        rows = [_shown_row(key="analyst:cccc", created_at=300),
                _shown_row(key="analyst:bbbb", created_at=100),
                _shown_row(key="analyst:dddd", created_at=100),
                _shown_row(key="analyst:aaaa", created_at=200)]

        assert [r["key"] for r in review.digest_order(rows)] == [
            "analyst:bbbb", "analyst:dddd", "analyst:aaaa", "analyst:cccc"]


class TestAWaitingIdeaAgesOnTheDigest:
    def test_a_validated_idea_survives_one_missed_digest(self):
        assert review.DIGEST_WAIT_DAYS == 14
        assert review.withdrawal_reason(
            _shown_row(updated_at=NOW - 13 * DAY), NOW, watched={MOTION, HALL}) is None

    def test_and_goes_after_two(self):
        reason = review.withdrawal_reason(
            _shown_row(updated_at=NOW - 14 * DAY), NOW, watched={MOTION, HALL})

        assert reason and "digest" in reason

    def test_a_candidate_still_ages_on_the_unseen_rule(self):
        assert review.withdrawal_reason(
            _shown_row(state="candidate", updated_at=NOW - review.UNSEEN_RETIRE_DAYS * DAY),
            NOW, watched={MOTION, HALL})


def _plant_idea(n, entities=None, created_at=None, state="validated"):
    """One explorer idea in the store, as mark_shown leaves it. Each gets its
    own lamp unless told otherwise, so claims only collide where a test
    means them to."""
    lamp = f"light.lamp_{n}"
    entities = entities or [f"binary_sensor.motion_{n}", lamp]
    config = {"alias": f"Lamp {n} with motion",
              "trigger": [{"platform": "state", "entity_id": entities[0], "to": "on"}],
              "action": [{"service": "light.turn_on", "target": {"entity_id": entities[-1]}}]}
    suggestion_id = suggestions.upsert_analyst_candidate(
        f"analyst:{n:04d}", entities=entities, title=f"Lamp {n} with motion",
        rationale=f"Lamp {n} follows motion.", model="asi1-mini")
    assert suggestions.mark_shown(
        suggestion_id, config=config, finding=f"Lamp {n} follows motion most evenings.",
        rule_text=f"When motion is seen, turn lamp {n} on.",
        chart={"spec": {"type": "overlay", "entities": entities}, "series": None,
               "markers": None, "error": None})
    if created_at is not None:
        conn = suggestions._conn()
        conn.execute("UPDATE suggestions SET created_at = ? WHERE id = ?",
                     (created_at, suggestion_id))
        conn.commit()
    if state == "proposed":
        raised = gate.propose(review.PROPOSAL_TOOL, {"config_json": json.dumps(config)})
        assert suggestions.mark_proposed(suggestion_id, raised["change_id"])
    return suggestion_id


def _plant_detector_row(entity=SENSOR, switch=SWITCH, state="validated"):
    row = _row(entities=[entity, switch], config={
        "alias": f"{switch} off overnight",
        "trigger": [{"platform": "time", "at": "01:00:00"}],
        "action": [{"service": "switch.turn_off", "target": {"entity_id": switch}}]})
    suggestion_id = suggestions.upsert_candidate(f"{standby.KIND}:{entity}", standby.KIND,
                                                 entities=row["entities"],
                                                 evidence={**row["evidence"], "entity": entity})
    assert suggestions.mark_validated(suggestion_id, replay=row["replay"],
                                      estimate=row["estimate"], config=row["config"])
    if state == "proposed":
        assert review._propose_one(suggestions.suggestion(suggestion_id))
    return suggestion_id


def _stamp(at):
    assert review.stamp_digest(at)


def _outbox(kind):
    return [m for m in activity.pending("chat", limit=100) if m["kind"] == kind]


def _states(ids):
    return [suggestions.suggestion(i)["state"] for i in ids]


class TestTheDigestPass:
    """`_digest` over a real store and a real gate; only HA's test-create is
    stubbed (offline_ha)."""

    def test_on_the_day_it_raises_three_longest_waiting_first(self, stores, offline_ha):
        ids = [_plant_idea(n, created_at=1000 - n) for n in range(1, 6)]
        _stamp(LAST_WEEK)

        assert review._digest(DIGEST_AT + 60) == 3

        # created_at runs backwards with n: ideas 5, 4 and 3 waited longest.
        assert _states(ids) == ["validated", "validated", "proposed", "proposed", "proposed"]
        for i in ids[2:]:
            assert suggestions.suggestion(i)["change_id"]
        [message] = _outbox("analyst_digest")
        assert message["title"] == artefacts.ANALYST_DIGEST_TITLE
        assert message["urgency"] == "normal", "a digest is never a push"
        assert "Lamp 5 follows motion" in message["body"]
        assert "Lamp 1" not in message["body"]
        assert message["payload"]["suggestion_ids"] == [ids[4], ids[3], ids[2]]

    def test_each_idea_is_its_own_question_through_the_gate(self, stores, offline_ha):
        ids = [_plant_idea(n) for n in range(1, 3)]
        _stamp(LAST_WEEK)

        review._digest(DIGEST_AT + 60)

        changes = {suggestions.suggestion(i)["change_id"] for i in ids}
        assert len(changes) == 2
        for change_id in changes:
            assert activity.change(change_id)["decision"] == "proposed"
        assert len(_outbox("approval_request")) == 2

    def test_not_on_another_day(self, stores, offline_ha):
        ids = [_plant_idea(1)]
        _stamp(LAST_WEEK)

        assert review._digest(DIGEST_AT - DAY) == 0
        assert _states(ids) == ["validated"]
        assert not _outbox("analyst_digest")

    def test_a_restart_on_the_day_sends_nothing_twice(self, stores, offline_ha):
        ids = [_plant_idea(n) for n in range(1, 6)]
        _stamp(LAST_WEEK)
        assert review._digest(DIGEST_AT + 60) == 3

        # The same morning again, after two of the three were answered: the
        # top-up would have room, the stamp says the week is done.
        for i in [i for i in ids if suggestions.suggestion(i)["state"] == "proposed"][:2]:
            suggestions.retire(i, "answered elsewhere")
        assert review._digest(DIGEST_AT + 3600) == 0
        assert len(_outbox("analyst_digest")) == 1

    def test_a_missed_day_is_held_the_next_morning_and_only_then(self, stores, offline_ha):
        _plant_idea(1)
        _plant_idea(2)
        _stamp(LAST_WEEK)

        assert review._digest(DIGEST_AT + DAY) == 2
        _plant_idea(3)
        assert review._digest(DIGEST_AT + 2 * DAY) == 0
        assert review._digest(DIGEST_AT + 7 * DAY + 60) == 1

    def test_last_weeks_unanswered_ideas_are_topped_up_not_stacked(self, stores, offline_ha):
        old = _plant_idea(1, state="proposed")
        ids = [_plant_idea(n) for n in range(2, 6)]
        _stamp(LAST_WEEK)

        assert review._digest(DIGEST_AT + 60) == 2
        assert suggestions.suggestion(old)["state"] == "proposed", "never replaced"
        assert sum(1 for s in _states(ids) if s == "proposed") == 2

    def test_a_fresh_install_waits_for_the_next_digest_day(self, stores, offline_ha):
        _plant_idea(1)

        assert review._digest(DIGEST_AT + 60) == 0
        assert review.last_digest_at(DIGEST_AT + 120) == DIGEST_AT + 60
        assert review._digest(DIGEST_AT + DAY) == 0
        assert review._digest(DIGEST_AT + 7 * DAY + 60) == 1

    @pytest.mark.parametrize("torn", ['{"last_at": ', "[]", '{"last_at": "x"}',
                                      '{"last_at": true}', "\xff\xfe"])
    def test_an_unreadable_stamp_fails_closed(self, stores, offline_ha, torn):
        _plant_idea(1)
        with open(review.DIGEST_PATH, "w", encoding="latin-1") as f:
            f.write(torn)

        assert review._digest(DIGEST_AT + 60) == 0
        assert review.last_digest_at(DIGEST_AT + 120) == DIGEST_AT + 60, "rewritten"

    def test_a_digest_that_cannot_be_stamped_is_not_held(self, stores, offline_ha, monkeypatch):
        ids = [_plant_idea(1)]
        _stamp(LAST_WEEK)
        monkeypatch.setattr(review, "stamp_digest", lambda now: False)

        assert review._digest(DIGEST_AT + 60) == 0
        assert _states(ids) == ["validated"]

    def test_an_empty_digest_day_still_counts_as_the_weeks(self, stores, offline_ha):
        _stamp(LAST_WEEK)
        assert review._digest(DIGEST_AT + 60) == 0
        assert not _outbox("analyst_digest")

        _plant_idea(1)
        assert review._digest(DIGEST_AT + 3 * DAY) == 0, "not Tuesday: nobody picked Tuesday"

    def test_a_refused_idea_gives_its_slot_to_the_next(self, stores, offline_ha, monkeypatch):
        ids = [_plant_idea(n, created_at=100 + n) for n in range(1, 5)]
        _stamp(LAST_WEEK)
        real = gate.propose

        def refuse_the_first(tool, args):
            if "Lamp 1" in args["config_json"]:
                return {"ok": False, "error": "entity gone"}
            return real(tool, args)
        monkeypatch.setattr(gate, "propose", refuse_the_first)

        assert review._digest(DIGEST_AT + 60) == 3
        assert _states(ids) == ["retired", "proposed", "proposed", "proposed"]


class TestTheTwoSourcesKeepApart:
    def test_an_analyst_idea_never_takes_the_daily_slot(self, stores, offline_ha):
        idea = _plant_idea(1)

        assert review._propose(NOW) == 0
        assert suggestions.suggestion(idea)["state"] == "validated"

    def test_open_analyst_questions_do_not_count_against_the_detectors(
            self, stores, offline_ha):
        for n in range(1, 4):
            _plant_idea(n, state="proposed")
        row = _plant_detector_row()

        assert review._propose(NOW) == 1
        assert suggestions.suggestion(row)["state"] == "proposed"

    def test_nor_does_an_analyst_idea_proposed_today(self, stores, offline_ha):
        _plant_idea(1, state="proposed")
        row = _plant_detector_row()

        assert review._propose(NOW) == 1
        assert suggestions.suggestion(row)["state"] == "proposed"

    def test_open_detector_questions_do_not_count_against_the_digest(self, stores, offline_ha):
        for n in range(3):
            _plant_detector_row(entity=f"sensor.p{n}", switch=f"switch.p{n}", state="proposed")
        ids = [_plant_idea(n) for n in range(1, 4)]
        _stamp(LAST_WEEK)

        assert review._digest(DIGEST_AT + 60) == 3
        assert _states(ids) == ["proposed"] * 3

    def test_a_detector_question_blocks_an_idea_about_its_device(self, stores, offline_ha):
        _plant_detector_row(state="proposed")
        blocked = _plant_idea(1, entities=[SENSOR, SWITCH], created_at=1)
        free = _plant_idea(2, created_at=2)
        _stamp(LAST_WEEK)

        assert review._digest(DIGEST_AT + 60) == 1
        assert _states([blocked, free]) == ["validated", "proposed"]

    def test_an_analyst_question_blocks_a_detector_row_on_its_device(self, stores, offline_ha):
        _plant_idea(1, entities=["binary_sensor.motion", SWITCH], state="proposed")
        row = _plant_detector_row()

        assert review._propose(NOW) == 0
        assert suggestions.suggestion(row)["state"] == "validated"

    def test_two_ideas_about_one_lamp_are_not_raised_together(self, stores, offline_ha):
        first = _plant_idea(1, entities=["binary_sensor.a", "light.study"], created_at=1)
        second = _plant_idea(2, entities=["binary_sensor.b", "light.study"], created_at=2)
        third = _plant_idea(3, created_at=3)
        _stamp(LAST_WEEK)

        assert review._digest(DIGEST_AT + 60) == 2
        assert _states([first, second, third]) == ["proposed", "validated", "proposed"]

    def test_the_pass_holds_the_digest_and_logs_what_waits(self, stores, home, offline_ha,
                                                            caplog, monkeypatch):
        """Through run_once: the summary carries the digest, and a day that is
        not the digest's says how many ideas are held for it."""
        monkeypatch.setattr(watchlist, "watched_entities", lambda: [])
        _plant_idea(1)
        _stamp(NOW)

        with caplog.at_level("INFO"):
            summary = review.run_once(now=NOW + 60)

        assert summary["digest"] == 0 and summary["proposed"] == 0
        assert "1 analyst idea(s) held for the digest on Saturday" in caplog.text


    def test_on_digest_day_the_idea_goes_first_and_the_detector_waits_a_day(
            self, stores, home, offline_ha, monkeypatch):
        """Both want one socket. The idea would wait a week, the detector row
        waits until tomorrow."""
        monkeypatch.setattr(watchlist, "watched_entities", lambda: [])
        row = _plant_detector_row()
        idea = _plant_idea(1, entities=["binary_sensor.motion", SWITCH])
        _stamp(LAST_WEEK)

        summary = review.run_once(now=DIGEST_AT + 60)

        assert (summary["digest"], summary["proposed"]) == (1, 0)
        assert _states([idea, row]) == ["proposed", "validated"]


class TestTheDigestMessage:
    def test_it_carries_each_cards_text_and_points_at_the_panel(self):
        rows = [_shown_row(title="Study lamp off when empty"),
                _shown_row(title="Porch light at dusk", finding="The porch light goes on at 20:00",
                           rule_text="At 20:00 turn the porch light on.")]

        text = artefacts.analyst_digest_text(rows)

        assert text.startswith("1. Study lamp off when empty")
        assert "   The hall light follows motion almost every evening." in text
        assert "2. Porch light at dusk" in text
        assert "   The porch light goes on at 20:00." in text
        assert "It would: At 20:00 turn the porch light on." in text
        assert text.endswith(artefacts.ANALYST_DIGEST_POINTER)
        assert "panel" in artefacts.ANALYST_DIGEST_POINTER

    def test_it_asks_nothing_a_single_yes_could_answer(self):
        """§9: never one yes covering several changes."""
        text = artefacts.analyst_digest_text([_shown_row(), _shown_row(key="analyst:x")])

        assert "YES" not in text and "reply" not in text.lower()
