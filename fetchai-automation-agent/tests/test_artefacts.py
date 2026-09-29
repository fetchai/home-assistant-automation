"""What a suggestion is allowed to claim when it is rendered.

Design §9 gives three rules that are properties of the RENDERING rather than of
the data behind it, which is why they get a test file of their own:

    "Every suggestion shows its evidence. No number without the observation
     behind it"
    "The plain-text rendering is the design constraint, not the fallback"
    "One suggestion per approval ... never a single yes covering several"

And one that comes from core/replay.py rather than from §9: a suggestion whose
`estimate.basis` is 'detector' is quoting the figure the replay's own was
DISCARDED in favour of, and must not be printed as something the replay found.

Everything here is a pure function over a row literal. There is no store, no
Home Assistant and no clock — which is the point: these are the sentences a
person reads before they type yes.
"""
from datetime import datetime, timezone
import json
import time

import pytest

from core import artefacts
from core import builder
from core import detectors
from core import replay
from core import review
from core import rollback
from core import verify
from core import window
from core.detectors import schedule
from core.detectors import standby


EVIDENCE = {
    "entity": "sensor.tv_power",
    "switch": "switch.tv",
    "watts": 12.0,
    "watts_range": [11.0, 13.4],
    "hours_per_day": 4,
    "kwh_per_month": 1.01,
    "nights_per_month": 21.0,
    "nights": 21,
    "nights_observed": 26,
    "nights_examined": 30,
    "excluded": {"no_coverage": 4, "in_use": 5},
    "covered_days": 26,
    "night_window": "01:00-05:00 local",
    "window": {"from": "2026-07-31", "to": "2026-08-30", "days": 30},
    "thresholds": {"ceiling_w": 30.0, "floor_w": 1.0, "min_nights": 10,
                   "min_days": 14, "max_gap_s": 1800},
}

REPLAY = {
    "guard": {"entity": "sensor.tv_power", "below": 30.0, "below_w": 30.0},
    "switch": "switch.tv",
    "nights_examined": 30,
    "nights_fired": 24,
    "nights_saved": 21,
    "nights_wrong": 0,
    # The nights the counts above do not claim, which is what the accounting
    # line is built from: 21 saved + 3 uncredited = 24 fired, + 5 not fired
    # + 1 unreadable = 30 examined.
    "outcomes": {"saved": 21, "no_credit": 3, "not_fired": 5, "unreadable": 1},
    "watts": 12.0,
    "hours_per_night": 4.0,
    "nights_per_month": 21.0,
    "kwh_per_month": 1.01,
    "bounded_by_detector": False,
}

# Every figure here multiplies out — 12 W x 4 h x 21 nights / 1000 = 1.01 kWh —
# because that is the invariant core/replay.py's `_estimate` exists to keep by
# taking the estimate WHOLE from one side, and a fixture that broke it would let
# a rendering assembled from two blobs pass this file.
REPLAY_ESTIMATE = {"basis": "replay", "kwh_per_month": 1.01, "watts": 12.0,
                   "hours_per_night": 4.0, "nights_per_month": 21.0}

# The other side of core/replay.py's clamp: the same four names, the detector's
# figures, and a `basis` that says so. Given DIFFERENT counts from the replay's
# on purpose — that is what makes "which counts got printed" a real question.
# It multiplies out too (11 x 4 x 18 / 1000 = 0.79), and comes out LOWER than
# the replay's, which is the only direction this branch is ever reached from.
DETECTOR_ESTIMATE = {"basis": "detector", "kwh_per_month": 0.79, "watts": 11.0,
                     "hours_per_night": 4.0, "nights_per_month": 18.0}


def _built():
    """The config core/builder.py would actually write for this evidence."""
    result, error = builder.build_config(
        {"key": "standby:sensor.tv_power", "kind": standby.KIND,
         "entities": ["sensor.tv_power", "switch.tv"], "evidence": EVIDENCE},
        name="Living room TV")
    assert not error, error
    return result


def _row(**overrides):
    row = {
        "id": 3,
        "change_id": 41,
        "state": "proposed",
        "key": "standby:sensor.tv_power",
        "kind": standby.KIND,
        "entities": ["sensor.tv_power", "switch.tv"],
        "evidence": EVIDENCE,
        "estimate": REPLAY_ESTIMATE,
        "replay": REPLAY,
        "config": _built()["config"],
    }
    row.update(overrides)
    return row


class TestNoNumberWithoutItsObservation:
    def test_the_digest_line_carries_the_nights_behind_its_kwh(self):
        line = artefacts.headline(_row())

        assert "1.01 kWh a month" in line
        assert "21 of 30 nights" in line

    def test_the_window_the_measurement_covers_is_stated(self):
        lines = artefacts.evidence_lines(_row())

        assert any("2026-07-31 to 2026-08-30" in line for line in lines)

    def test_the_nights_that_were_left_out_are_accounted_for(self):
        """§9's rule read strictly: "21 of 30" with nothing said about the other
        nine is a number without its observation."""
        lines = artefacts.evidence_lines(_row())

        excluded = next(line for line in lines if line.startswith("Nights left out"))
        assert "4 × the agent was not looking" in excluded
        assert "5 × the device was in use" in excluded

    def test_the_two_levels_behind_calling_this_standby_are_stated(self):
        """The detector's answer to "is this a television or a router" is an
        observation like any other, and §9 wants it beside the number it
        justifies rather than left in a log line."""
        row = _row(evidence={**EVIDENCE, "idle_watts": 12.0, "active_floor": 36.0})

        lines = artefacts.evidence_lines(row)

        line = next(line for line in lines if "in-use level" in line)
        assert "around 12 W" in line
        assert "above 36 W" in line

    def test_a_blob_measured_before_the_two_levels_were_claims_neither(self):
        """Every evidence blob written before the detector learned to measure
        them lacks both, and half a sentence would claim a level nobody
        measured."""
        lines = artefacts.evidence_lines(_row())

        assert not any("in-use level" in line for line in lines)

    def test_one_level_without_the_other_is_still_no_sentence(self):
        row = _row(evidence={**EVIDENCE, "idle_watts": 12.0})

        assert not any("in-use level" in line
                       for line in artefacts.evidence_lines(row))

    def test_a_reason_this_version_has_no_words_for_is_still_reported(self):
        row = _row(evidence={**EVIDENCE, "excluded": {"something_new": 2}})

        lines = artefacts.evidence_lines(row)

        assert any("2 × something_new" in line for line in lines)

    def test_the_estimate_prints_the_breakdown_that_multiplies_out(self):
        """core/replay.py takes the estimate WHOLE from one side precisely so
        that these four figures agree with each other; splitting them across
        two sentences would let a reader do the arithmetic and find it wrong."""
        sentence = artefacts.estimate_sentence(_row())

        assert "12 W" in sentence
        assert "4 hours" in sentence
        assert "21 nights a month" in sentence
        assert "1.01 kWh a month" in sentence


class TestWhoseNumberItIs:
    def test_a_detector_basis_figure_is_not_attributed_to_the_replay(self):
        sentence = artefacts.estimate_sentence(_row(estimate=DETECTOR_ESTIMATE))

        assert "0.79 kWh a month" in sentence
        assert "replaying the rule" not in sentence
        assert "the measurement itself" in sentence

    def test_a_detector_basis_headline_quotes_the_detector_s_own_counts(self):
        """The replay credited 21 of 30 and the detector 21 of 30 as well —
        so the fixture moves one, and the headline has to follow the estimate
        rather than reaching for whichever count is nearest."""
        row = _row(estimate=DETECTOR_ESTIMATE,
                   evidence={**EVIDENCE, "nights": 18, "nights_examined": 28},
                   replay={**REPLAY, "nights_saved": 21, "nights_examined": 30})

        line = artefacts.headline(row)

        assert "18 of 28 nights" in line
        assert "21 of 30" not in line

    def test_the_replay_reports_its_own_counts_under_its_own_heading(self):
        """Kept apart, not merged: these are the replay's whatever the estimate
        ended up quoting."""
        row = _row(estimate=DETECTOR_ESTIMATE)

        lines = artefacts.replay_lines(row)

        assert any("30 nights" in line and "24" in line for line in lines)

    def test_the_basis_travels_as_data_too(self):
        assert artefacts.detail(_row())["basis"] == "replay"
        assert artefacts.detail(_row(estimate=DETECTOR_ESTIMATE))["basis"] == "detector"


class TestTheReplayAccountsForEveryNightToo:
    """§9's rule applies to the grading as much as to the measurement. "Ran on
    24 of 30, saved on 21" leaves nine nights unexplained, and the blob has
    already said what happened on every one of them."""

    def test_the_nights_it_did_not_credit_are_named(self):
        lines = artefacts.replay_lines(_row())

        rest = next(line for line in lines if line.startswith("The remaining nights"))
        assert "5 × the device was still drawing power" in rest
        assert "3 × the night was too patchy to credit" in rest
        assert "1 × the check itself could not be read" in rest

    def test_the_biggest_omission_is_read_first(self):
        rest = next(line for line in artefacts.replay_lines(_row())
                    if line.startswith("The remaining nights"))

        assert rest.index("5 ×") < rest.index("3 ×") < rest.index("1 ×")

    def test_saved_and_wrong_are_not_repeated_there(self):
        """They already have sentences of their own; a third mention would read
        as a fourth measurement."""
        rest = next(line for line in artefacts.replay_lines(_row())
                    if line.startswith("The remaining nights"))

        assert "21" not in rest

    def test_a_blob_with_no_outcomes_says_nothing_rather_than_guessing(self):
        row = _row(replay={k: v for k, v in REPLAY.items() if k != "outcomes"})

        assert not any(line.startswith("The remaining nights")
                       for line in artefacts.replay_lines(row))

    def test_the_excluded_nights_are_ordered_the_same_way(self):
        lines = artefacts.evidence_lines(_row())

        excluded = next(line for line in lines if line.startswith("Nights left out"))
        assert excluded.index("5 ×") < excluded.index("4 ×")


class TestTheZeroThatMatters:
    def test_no_wrong_nights_is_stated_as_a_fact_about_the_record(self):
        """review.MAX_WRONG_NIGHTS is zero, so every proposal that reaches a
        user has this. Printing "0 wrong nights" would read as a score; what it
        actually means is that the record contains no such night."""
        lines = artefacts.replay_lines(_row())

        assert any("no night in the record" in line for line in lines)

    def test_a_night_it_would_have_got_wrong_is_never_hidden(self):
        row = _row(replay={**REPLAY, "nights_wrong": 2})

        lines = artefacts.replay_lines(row)

        assert any("On 2 of them it would have cut power" in line for line in lines)


class TestTheReadback:
    def test_it_is_the_sentence_the_builder_itself_emits(self):
        """Rebuilt from the stored config rather than restated here. Two
        descriptions of one automation is what artefact 3 exists to prevent."""
        built = _built()

        assert artefacts.readback(_row(config=built["config"])) == built["readback"]

    def test_it_describes_the_stored_config_and_not_today_s_constants(self, monkeypatch):
        """A proposal raised last week is answered against the rule it was
        raised with. If the detector's night window moved in between, the
        readback must still describe the automation that will be written."""
        row = _row()
        monkeypatch.setattr(standby, "NIGHT", window.spec("23:00", "06:00"))

        assert "At 01:00" in artefacts.readback(row)
        assert "back on at 05:00" in artefacts.readback(row)

    def test_the_ceiling_comes_from_what_was_graded_not_from_a_constant(self, monkeypatch):
        """The threshold `replay_lines` prints a paragraph above comes from the
        replay's guard. If this reached for the live constant instead, a
        constant that moved between the proposal and the answer would put two
        different thresholds for one automation in one message."""
        row = _row(replay={**REPLAY, "guard": {"below": 25.0, "below_w": 25.0}},
                   evidence={**EVIDENCE, "thresholds": {"ceiling_w": 22.0}})
        monkeypatch.setattr(standby, "STANDBY_MAX_WATTS", 99.0)

        text = artefacts.readback(row)

        assert "less than 25.0 W" in text
        assert "22.0 W" not in text and "99.0 W" not in text

    def test_a_row_with_no_replay_falls_back_to_what_was_measured(self):
        row = _row(replay=None, evidence={**EVIDENCE, "thresholds": {"ceiling_w": 22.0}})

        assert "less than 22.0 W" in artefacts.readback(row)

    def test_one_message_never_states_two_thresholds(self):
        """The failure the precedence above exists to prevent, asserted on the
        whole artefact rather than on either half of it."""
        row = _row(replay={**REPLAY, "guard": {"below": 25.0, "below_w": 25.0}})

        text = artefacts.detail_text(row)

        assert "switches off below 25 W" in text
        assert "less than 25.0 W" in text

    def test_it_says_the_device_is_genuinely_unreachable_meanwhile(self):
        """The half of the readback the household actually meets."""
        assert "not respond to its remote" in artefacts.readback(_row())

    def test_a_config_this_version_does_not_recognise_renders_nothing(self):
        """Rather than a confident sentence about a rule nobody parsed."""
        assert artefacts.readback(_row(config={"alias": "hand-written"})) == ""


class TestItCanBeReadAndDecidedInOneMessage:
    def test_the_message_carries_all_three_artefacts(self):
        text = artefacts.detail_text(_row())

        assert "never dropped below" in text            # what was found
        assert "2026-07-31 to 2026-08-30" in text       # over what window
        assert "Estimated saving" in text               # what it is worth
        assert "What would be written" in text          # what would happen

    def test_it_needs_no_markup_to_be_read(self):
        """It goes to ASI1 as plain text. A rendering that only works with a
        diff view or a button belongs on the card, never as the only path."""
        text = artefacts.detail_text(_row())

        assert "<" not in text
        assert "```" not in text

    def test_a_missing_estimate_says_so_rather_than_printing_nothing(self):
        row = _row(estimate={"basis": "replay"})

        assert "No saving can be estimated" in artefacts.estimate_sentence(row)


class TestOneSuggestionPerApproval:
    def test_the_question_names_exactly_one_change(self):
        asked = artefacts.question(_row())

        assert asked[artefacts.QUESTION_MARKER] is True
        assert asked["change_id"] == 41
        assert asked["suggestion_id"] == 3

    def test_a_suggestion_nobody_has_been_asked_about_cannot_be_answered(self):
        asked = artefacts.question(_row(state="validated", change_id=None))

        assert artefacts.QUESTION_MARKER not in asked
        assert "waiting for an answer" in asked["reason"]

    def test_a_suggestion_that_does_not_exist_is_refused_in_words(self):
        asked = artefacts.question(None)

        assert artefacts.QUESTION_MARKER not in asked
        assert "no such suggestion" in asked["reason"]

    def test_the_digest_never_grows_past_three_lines_per_source(self):
        """§9's digest is three lines per source, and review.MAX_OPEN_PROPOSALS
        and review.DIGEST_SIZE are written to make a seventh impossible. If
        one ever arrives, the digest is not the place it gets shown."""
        rows = [_row(id=i, change_id=i) for i in range(1, 9)]

        assert artefacts.DIGEST_LINES == review.MAX_OPEN_PROPOSALS + review.DIGEST_SIZE
        assert artefacts.digest(rows)["count"] == 6
        assert artefacts.digest_text(rows).count("\n") == 6

    def test_an_empty_digest_says_so_rather_than_going_blank(self):
        assert "not raised any suggestions" in artefacts.digest_text([])


class TestTheParkedQuestion:
    def test_it_is_parked_where_the_approval_gate_cannot_reach_it(self):
        """Its own slot. `pending_approval` is answered by resuming a
        conversation through gate.apply, and this one has no conversation."""
        memory = {}

        artefacts.park_question(memory, artefacts.question(_row()))

        assert memory["pending_suggestion"]["change_id"] == 41
        assert "pending_approval" not in memory

    def test_the_text_says_how_to_answer_it(self):
        text = artefacts.park_question({}, artefacts.question(_row()))

        assert "Reply YES to apply this, or NO to dismiss it." in text

    def test_re_asking_repeats_the_whole_question(self):
        """A one-word reply has to answer something the user can still see, and
        by this point the original may be several messages up."""
        memory = {}
        artefacts.park_question(memory, artefacts.question(_row()))

        again = artefacts.reask(memory["pending_suggestion"])

        assert "Estimated saving" in again
        assert "Nothing has been changed" in again

    def test_a_question_older_than_its_ttl_is_not_answerable(self):
        stale = {"change_id": 41, "created_at": artefacts._now()
                 - artefacts.QUESTION_TTL_SECONDS - 1}

        assert artefacts.question_expired(stale)
        assert not artefacts.question_expired({"change_id": 41, "created_at": artefacts._now()})

    def test_a_pending_with_no_clock_never_expires(self):
        assert not artefacts.question_expired({"change_id": 41})


class TestRecognisingTheQuestion:
    def test_it_is_found_in_a_tool_result(self):
        result = json.dumps(artefacts.question(_row()))

        assert artefacts.question_in(result)["change_id"] == 41

    def test_an_ordinary_answer_is_not_one(self):
        assert artefacts.question_in('{"suggestions": []}') is None
        assert artefacts.question_in("") is None
        assert artefacts.question_in("not json at all") is None

    def test_a_rollback_confirmation_is_not_one_either(self):
        """Three markers, three answering paths. One recognised as another is
        the failure both modules' docstrings are written against."""
        result = json.dumps({rollback.CONFIRMATION_MARKER: True, "change_id": 41})
        assert artefacts.question_in(result) is None


# --------------------------------------------------------------------------
# artefact 4 — the verification report
# --------------------------------------------------------------------------
BEFORE = {
    "entity": "sensor.tv_power", "switch": "switch.tv",
    "night_window": "01:00-05:00 local", "hours_per_day": 4,
    "window": {"from": "2026-09-01", "to": "2026-09-08", "days": 7},
    "watts": 12.0, "watts_range": [11.0, 13.0], "kwh_per_month": 1.44,
    "nights_per_month": 30.0, "nights": 7, "nights_observed": 7,
    "nights_examined": 7, "excluded": {},
}

# What a working automation leaves behind: no night qualifies as a plateau,
# because the device was switched off — and every one of them was READ.
AFTER_GONE = {
    **BEFORE,
    "window": {"from": "2026-09-08", "to": "2026-09-15", "days": 7},
    "watts": None, "watts_range": None, "kwh_per_month": None,
    "nights_per_month": None, "nights": 0, "nights_observed": 7,
    "excluded": {"below_floor": 7},
}


def _verdict(**overrides):
    verdict = {
        "outcome": "saved",
        "reason": "",
        "attempts": 1,
        "verified_at": 1789000000,
        "before": BEFORE,
        "after": AFTER_GONE,
        "floor_kwh_per_month": 0.12,
        "saved_kwh_per_month": 1.44,
        "saved_range_kwh_per_month": [1.2, 1.68],
        "saved_watts": 12.0,
        "estimate_kwh_per_month": 1.01,
        "estimate_basis": "replay",
    }
    verdict.update(overrides)
    return verdict


def _reported(verdict=None, **overrides):
    return _row(state="verified", verdict=verdict or _verdict(), **overrides)


class TestTheReportStatesItsWindows:
    """§7.2: "compare like periods, and state the comparison window"."""

    def test_both_windows_are_named_in_the_order_they_happened(self):
        line = artefacts.verification_windows(_reported())

        assert "2026-09-15" in line and "2026-09-08" in line
        assert line.index("2026-09-15") < line.index("2026-09-08")
        assert "the week before the change was applied" in line

    def test_and_so_are_the_hours_of_the_night_they_cover(self):
        assert "01:00-05:00 local" in artefacts.verification_windows(_reported())


class TestTheClaimIsARangeOrNothing:
    """§7.2: give a range, or say the effect is too small to separate from
    noise. There is no third form."""

    def test_a_saving_is_stated_as_a_range(self):
        result = artefacts.verification_result(_reported())

        assert "between 1.2 and 1.68 kWh a month" in result

    def test_the_midpoint_is_never_the_whole_claim(self):
        """It is recorded for a reader who wants one number, and printing it
        alone is the confident figure §7.2 says would damage trust faster than
        reporting nothing."""
        result = artefacts.verification_result(_reported())

        assert "1.44 kWh" not in result
        assert "between" in result

    def test_a_difference_inside_the_floor_prints_no_number_at_all(self):
        result = artefacts.verification_result(
            _reported(_verdict(outcome="no_change", saved_range_kwh_per_month=[-0.05, 0.06],
                               saved_kwh_per_month=0.0, saved_watts=0.1)))

        assert "nothing that can be separated from noise" in result
        assert "0.12 kWh a month" in result
        assert "0.06" not in result

    def test_a_device_drawing_more_than_before_is_said_so_out_loud(self):
        """Not rounded away into "no change": this is where the user most wants
        the rollback."""
        result = artefacts.verification_result(
            _reported(_verdict(outcome="worse", saved_range_kwh_per_month=[-1.2, -0.4],
                               saved_watts=-6.0)))

        assert "MORE" in result
        assert "rolling the change back" in result

    def test_and_the_size_of_it_is_stated_without_the_sign(self):
        """The direction is already in the words. "drew MORE than before, by
        between -1.2 and -0.4 kWh a month" states the opposite of the clause it
        sits in, and the two ends swap places when their size is taken."""
        result = artefacts.verification_result(
            _reported(_verdict(outcome="worse", saved_range_kwh_per_month=[-1.2, -0.4],
                               saved_watts=-6.0)))

        assert "by between 0.4 and 1.2 kWh a month" in result
        assert "-" not in result

    def test_an_unmeasurable_before_reads_like_an_unmeasurable_after(self):
        """One condition described in two registers is how a report starts
        looking machine-made. Both halves say "none", not "0"."""
        empty = {**BEFORE, "watts": None, "nights_observed": None, "nights": 0}
        line = artefacts.verification_evidence(
            _reported(_verdict(outcome="no_baseline", before=empty, after={})))[0]

        assert "none of its 7 nights could be read" in line
        assert "0 of its" not in line

    def test_a_week_that_could_not_be_read_never_reads_as_saved_nothing(self):
        """core/verify.py refuses to zero an empty measurement for this
        sentence's sake, and the sentence has to honour it."""
        result = artefacts.verification_result(
            _reported(_verdict(outcome="unmeasured", attempts=3,
                               reason="only 1 of the 7 nights in the week could be read",
                               after={}, saved_range_kwh_per_month=None,
                               saved_kwh_per_month=None, saved_watts=None)))

        assert "No measured result" in result
        assert "only 1 of the 7 nights" in result
        assert "tried 3 times" in result
        assert "not a week in which nothing was saved" in result
        assert "0 kWh" not in result


class TestTheReportShowsBothObservations:
    """§9's "no number without the observation behind it", applied to a before
    and an after — on both sides."""

    def test_the_before_carries_the_nights_it_was_measured_on(self):
        lines = artefacts.verification_evidence(_reported())

        assert any("never dropped below about 12 W on 7 of the 7 nights" in line
                   for line in lines)

    def test_a_successful_after_says_the_device_switched_off_rather_than_saying_nothing(self):
        """The shape that is easiest to render wrongly: no night qualified
        BECAUSE the automation worked."""
        lines = artefacts.verification_evidence(_reported())

        assert any("really did switch off on all 7 of the 7 nights" in line for line in lines)

    def test_an_after_that_still_shows_a_plateau_says_so_with_its_level(self):
        after = {**BEFORE, "watts": 9.0, "watts_range": [8.0, 10.0],
                 "window": {"from": "2026-09-08", "to": "2026-09-15", "days": 7}}
        lines = artefacts.verification_evidence(_reported(_verdict(after=after)))

        assert any("still never dropped below about 9 W" in line for line in lines)

    def test_an_after_nobody_could_read_says_how_much_of_it(self):
        """And is never rendered as the success it looks identical to. One
        readable night and seven differ in no field this can see — so the
        rendering follows core/verify.py's own outcome rather than
        re-deriving the judgement from the counts."""
        after = {**AFTER_GONE, "nights_observed": 1}
        lines = artefacts.verification_evidence(
            _reported(_verdict(outcome="unmeasured", after=after)))

        assert any("only 1 of the 7 nights that week could be read" in line for line in lines)
        assert not any("really did switch off" in line for line in lines)


class TestWhoseEstimateIsBeingComparedAgainst:
    """The reason `estimate.basis` was carried this far. A report naming the
    wrong side gives back everything §7.1 spent a module protecting."""

    def test_a_replay_sourced_estimate_is_named_as_the_replays(self):
        line = artefacts.verification_estimate(_reported())

        assert "1.01 kWh a month" in line
        assert "replaying the rule against the record" in line

    def test_a_detector_sourced_one_is_never_printed_as_the_replays(self):
        line = artefacts.verification_estimate(
            _reported(estimate=DETECTOR_ESTIMATE))

        assert "replaying the rule" not in line
        assert "the replay's own figure came out higher" in line

    def test_basis_is_still_read_in_exactly_one_place(self):
        """core/verify.py stores its own copy on the verdict as the audit
        record. The SENTENCE reads it through `_measured_on`, or there would be
        two places for it to be read wrongly."""
        row = _reported(estimate=DETECTOR_ESTIMATE)
        row["verdict"]["estimate_basis"] = "replay"

        assert artefacts.verification(row)["basis"] == "detector"
        assert "the replay's own figure came out higher" in \
            artefacts.verification_estimate(row)


class TestTheRollbackIsAlwaysOffered:
    """§7.2's fourth rule, and the one with no exception in it."""

    def test_it_is_offered_when_the_news_is_good(self):
        assert "roll it back" in artefacts.verification_choice(_reported())

    def test_and_when_there_is_no_news_at_all(self):
        unmeasured = _reported(_verdict(outcome="unmeasured", reason="nothing was readable"))

        assert "roll it back" in artefacts.verification_choice(unmeasured)

    def test_and_when_it_genuinely_cannot_be_the_reason_is_rollbacks_own(self):
        """Never a second set of rules invented here: the panel, the chat tool
        and this all ask core/rollback.py the same question."""
        why_not = rollback.why_not({"id": 41, "status": "not_applied"})

        offer = artefacts.verification_choice(_reported(), why_not)

        assert why_not in offer
        assert "no rollback to offer" in offer
        assert artefacts.verification(_reported(), why_not)["undoable"] is False


class TestArtefactFourReadsInOneMessage:
    """§9: "the plain-text rendering is the design constraint, not the
    fallback"."""

    def test_the_whole_report_is_one_message(self):
        text = artefacts.verification_text(_reported())

        assert "01:00-05:00" in text                      # the window
        assert "never dropped below about 12 W" in text   # the before
        assert "really did switch off" in text            # the after
        assert "between 1.2 and 1.68 kWh a month" in text  # the claim
        assert "the estimate was about 1.01 kWh" in text  # whose claim
        assert "roll it back" in text                     # the choice

    def test_the_panel_gets_the_same_sentences_in_fields(self):
        """One dict for both surfaces, so they cannot come to differ in what
        they claim — only in how it is arranged."""
        row = _reported()
        report = artefacts.verification(row)
        text = artefacts.verification_text(row)

        assert report["result"] in text
        assert report["estimate"] in text
        assert report["choice"] in text
        for line in report["evidence"]:
            assert line in text

    def test_the_headline_carries_one_number_and_its_window(self):
        line = artefacts.verification_headline(_reported())

        assert "Living room TV" in line
        assert "between 1.2 and 1.68 kWh a month" in line
        assert "the 7 nights to 2026-09-15" in line

    def test_and_says_plainly_when_there_is_no_number(self):
        line = artefacts.verification_headline(
            _reported(_verdict(outcome="unmeasured", saved_range_kwh_per_month=None)))

        assert "could not be measured" in line
        assert "kWh" not in line


class TestTheParkedVerdictQuestion:
    """WP8's pattern a second time, for the answer that runs an undo."""

    def test_a_report_is_asked_as_one_question_about_one_change(self):
        parsed = artefacts.verdict_question(_reported())

        assert parsed[artefacts.VERDICT_MARKER] is True
        assert parsed["change_id"] == 41
        assert parsed["suggestion_id"] == 3
        assert "between 1.2 and 1.68" in parsed["question"]

    def test_the_two_answers_are_named_so_one_word_is_unambiguous(self):
        """§9 gives the user one word, and parse_approval_decision knows YES
        and NO and nothing else."""
        memory = {}

        asked = artefacts.park_verdict(memory, artefacts.verdict_question(_reported()))

        assert artefacts.VERDICT_PROMPT in asked
        assert "YES to keep it" in asked and "NO to roll it back" in asked

    def test_it_is_parked_in_its_own_slot(self):
        """A fourth question is a fourth answering path, and a YES meant for
        one of them must never be read by another."""
        memory = {}

        artefacts.park_verdict(memory, artefacts.verdict_question(_reported()), "s1")

        assert memory["pending_verdict"]["suggestion_id"] == 3
        assert "pending_approval" not in memory
        assert "pending_suggestion" not in memory
        assert "pending_rollback" not in memory

    def test_a_suggestion_still_waiting_to_be_applied_is_refused_in_words(self):
        parsed = artefacts.verdict_question(_row(state="proposed"))

        assert artefacts.VERDICT_MARKER not in parsed
        assert "nothing to keep or roll back" in parsed["reason"]

    def test_and_so_is_one_that_does_not_exist(self):
        parsed = artefacts.verdict_question(None)

        assert artefacts.VERDICT_MARKER not in parsed
        assert "no such suggestion" in parsed["reason"]

    def test_the_marker_is_recognised_in_the_result_not_in_an_argument(self):
        """A result is built by this process; an argument is written by the
        model. What is being recognised is a request to put an UNDO to the
        user."""
        answer = json.dumps(artefacts.verdict_question(_reported()))

        assert artefacts.verdict_in(answer)["change_id"] == 41
        assert artefacts.verdict_in(json.dumps({"keep": True})) is None
        assert artefacts.verdict_in("") is None

    def test_the_four_markers_are_all_different(self):
        """Four code paths answer them, and telling them apart is what keeps
        that true."""
        from core import gate as gate_module

        markers = {gate_module.APPROVAL_MARKER, rollback.CONFIRMATION_MARKER,
                   artefacts.QUESTION_MARKER, artefacts.VERDICT_MARKER}

        assert len(markers) == 4

    def test_a_reply_that_was_not_a_decision_gets_the_whole_report_again(self):
        memory = {}
        artefacts.park_verdict(memory, artefacts.verdict_question(_reported()))

        again = artefacts.reask_verdict(memory["pending_verdict"])

        assert "between 1.2 and 1.68" in again
        assert "Nothing has been changed." in again


class TestNamingTheDevice:
    def test_it_uses_the_name_the_household_will_see_in_home_assistant(self):
        assert artefacts.label_for(_row()) == "Living room TV"

    def test_a_row_with_no_config_still_names_something(self):
        assert artefacts.label_for(_row(config=None)) == "switch.tv"


# --------------------------------------------------------------------------
# the other shape
#
# Everything above is a night. A schedule suggestion is the same claim about a
# stretch of the daytime that names its weekdays, and §9's rules are the same
# rules — so the fixtures below are not a second set of literals. They are the
# real pipeline run once over a household's ninety days: the detector finds the
# window, core/builder.py writes the automation for it, core/replay.py grades
# that config against the same rows, and core/verify.py measures a fortnight of
# it afterwards. A blob written by hand here would be a blob agreeing with
# whatever this file believed on the day, and the thing being tested is that
# the renderer reads what the pipeline actually wrote.
# --------------------------------------------------------------------------
DAY = 86400
HEARTBEAT = 900  # observer.NUMERIC_MAX_GAP_SECONDS — the record's own floor
NOW = int(datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc).timestamp())

PRINTER = "sensor.printer_power"
PRINTER_SWITCH = "switch.printer"

_PIPELINE = {}


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Pins the process timezone.

    A window is a statement about the household's clock, so the fixtures below
    are placed by local wall time and would otherwise drift with whoever ran
    the suite. tzset() is what time.localtime reads and monkeypatch does not
    restore it. Nothing above this line depends on it — those rows are literals
    — which is why it can be autouse without moving any of them.
    """
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _office(ts, cut=False) -> str:
    """An office printer: idle all through the working day, in use morning and
    evening on weekdays and all day at the weekend. `cut` is the same household
    with the automation running — the socket is off, so the sensor reads zero.
    """
    moment = datetime.fromtimestamp(int(ts))
    hour = moment.hour
    if moment.date().weekday() >= 5:
        return "120.0" if 8 <= hour < 23 else "12.0"
    if 6 <= hour < 7 or 17 <= hour < 23:
        return "120.0"
    return "0.0" if (cut and 7 <= hour < 17) else "12.0"


def _series(start, end, cut=False) -> list:
    return [{"ts": ts, "state": _office(ts, cut), "prev_state": None,
             "prev_duration": HEARTBEAT, "source": "live"}
            for ts in range(int(start), int(end), HEARTBEAT)]


def _pipeline() -> dict:
    """The whole of it, run once: {"row", "reported"}.

    Cached because it reads ninety days of heartbeats three times over and the
    answer does not depend on the test asking for it.
    """
    if _PIPELINE:
        return _PIPELINE
    rows = _series(NOW - schedule.LOOKBACK_DAYS * DAY, NOW)
    candidate = schedule.find_candidate(PRINTER, rows, PRINTER_SWITCH, now=NOW)
    assert candidate, "the detector found no daytime window in the fixture"
    built, error = builder.build_config(candidate, name="Office printer")
    assert not error, error
    graded, error = replay.run(built["config"], PRINTER, rows, schedule.KIND,
                               evidence=candidate["evidence"], now=NOW)
    assert not error, error
    row = {
        "id": 9, "change_id": 77, "state": "proposed", "key": candidate["key"],
        "kind": schedule.KIND, "entities": candidate["entities"],
        "evidence": candidate["evidence"], "estimate": graded["estimate"],
        "replay": graded["replay"], "config": built["config"],
    }

    # ...and §7.2 a fortnight later, which is what `verify.days_for` asks for a
    # Mon-Fri rule: ten occurrences, not seven. Both weeks through
    # `verify.measure`, so the report is rendered off what that module wrote.
    shape, why_not = verify.shape_of(row)
    assert shape, why_not
    length = verify.days_for(row)
    baseline = verify.measure(_series(NOW - length * DAY, NOW), NOW,
                              days=length, shape=shape)
    after_end = NOW + length * DAY
    verdict = verify.compare({**row, "baseline": baseline},
                             _series(NOW, after_end, cut=True), after_end)
    _PIPELINE.update(row=row, reported={**row, "state": "verified",
                                        "baseline": baseline, "verdict": verdict})
    return _PIPELINE


def _schedule_row():
    return _pipeline()["row"]


def _schedule_reported():
    return _pipeline()["reported"]


class TestTheReasonOnAScheduleCard:
    """The sentence 2.14.5 shipped wrong on purpose and gated the release on.
    A daytime window described as a night is not a smaller claim than the true
    one, it is a different one."""

    def test_it_names_the_window_the_evidence_was_actually_measured_over(self):
        said = artefacts.why(_schedule_row())

        assert "07:00-17:00 local Mon-Fri" in said
        assert "overnight" not in said

    def test_and_the_counts_are_the_ones_that_window_came_round(self):
        """§9's rule: the number carries its observation, in the unit the
        detector counted. `nights` is not a field of this blob and reading it
        as absent would have dropped the clause entirely."""
        said = artefacts.why(_schedule_row())
        evidence = _schedule_row()["evidence"]

        assert f"on {evidence['occurrences']} of the " \
               f"{evidence['occurrences_examined']} occurrences examined" in said

    def test_a_kind_this_build_has_no_detector_for_says_less_rather_than_more(self):
        """2.14.5's refusal in a renderer's terms: a field that cannot be named
        means the clause is omitted, never printed with the other shape's word
        over it."""
        said = artefacts.why({**_schedule_row(), "kind": "occupancy"})

        assert "07:00-17:00" not in said
        assert "occurrences" not in said and "nights" not in said
        assert "drawing power while it is not being used" in said


class TestTheScheduleDigestLine:
    def test_it_carries_the_kwh_the_occurrences_and_the_window(self):
        line = artefacts.headline(_schedule_row())
        row = _schedule_row()

        assert f"about {artefacts._num(row['estimate']['kwh_per_month'])} kWh a month" in line
        assert f"{row['evidence']['occurrences']} of " \
               f"{row['evidence']['occurrences_examined']} occurrences measured" in line
        assert "(07:00-17:00 local Mon-Fri)" in line

    def test_two_claims_about_one_device_are_told_apart_by_that_window(self):
        """Which is the whole reason the window is on the end of every digest
        line: §9 answers one of these in one word, and a night claim and a
        daytime claim about one television are two things a yes could mean."""
        night = artefacts.headline(_row())
        day = artefacts.headline(_schedule_row())

        assert "(01:00-05:00 local)" in night
        assert "(07:00-17:00 local Mon-Fri)" in day


class TestTheScheduleEvidenceAndGrading:
    def test_the_measurement_is_described_in_the_shape_s_own_noun(self):
        lines = artefacts.evidence_lines(_schedule_row())

        assert any("The lowest reading on each occurrence ranged from" in line
                   for line in lines)
        assert not any("night" in line for line in lines)

    def test_the_coverage_of_the_ninety_days_is_stated(self):
        """The line that used to drop out entirely, because this blob states
        the figure and the night's name for it was the only one being read."""
        lines = artefacts.evidence_lines(_schedule_row())

        assert any("of those 90 days had readable data" in line for line in lines)

    def test_the_occurrences_left_out_are_accounted_for_under_their_own_word(self):
        row = _schedule_row()
        row = {**row, "evidence": {**row["evidence"],
                                   "excluded": {"no_coverage": 4, "statistics": 2}}}

        left_out = next(line for line in artefacts.evidence_lines(row)
                        if line.startswith("Occurrences left out"))

        assert "4 × the agent was not looking" in left_out
        assert "2 × only averaged data was available" in left_out

    def test_the_estimate_still_prints_the_breakdown_that_multiplies_out(self):
        """Its hours and its frequency are named `hours_per_occurrence` and
        `occurrences_per_month`, so read by the night's names the whole
        breakdown vanished and the sentence became the bare kWh §9 is written
        against."""
        estimate = _schedule_row()["estimate"]

        sentence = artefacts.estimate_sentence(_schedule_row())

        assert f"about {artefacts._num(estimate['watts'])} W" in sentence
        assert f"for {artefacts._num(estimate['hours_per_occurrence'])} hours" in sentence
        assert f"{artefacts._num(estimate['occurrences_per_month'])} occurrences a month" \
            in sentence
        assert round(estimate["watts"] * estimate["hours_per_occurrence"]
                     * estimate["occurrences_per_month"] / 1000.0, 2) == \
            estimate["kwh_per_month"]

    def test_the_grading_reports_its_counts_and_not_only_its_guard(self):
        graded = _schedule_row()["replay"]

        lines = artefacts.replay_lines(_schedule_row())

        assert any(f"Replayed against the same {graded['occurrences_examined']} "
                   "occurrences" in line for line in lines)
        assert any(f"saved on {graded['occurrences_saved']}." in line for line in lines)
        assert any("switches off below" in line for line in lines)

    def test_and_the_zero_that_matters_is_stated_in_the_same_unit(self):
        """review.MAX_WRONG_NIGHTS is zero for both shapes, so every schedule
        proposal that reaches a user has this sentence too."""
        assert any("no occurrence in the record" in line
                   for line in artefacts.replay_lines(_schedule_row()))

    def test_a_grading_it_would_have_got_wrong_names_no_morning(self):
        """"before morning" is a night's word for it, and this rule's power
        comes back at five in the afternoon."""
        row = _schedule_row()
        row = {**row, "replay": {**row["replay"], "occurrences_wrong": 2}}

        lines = artefacts.replay_lines(row)

        assert any("On 2 of them it would have cut power" in line for line in lines)
        assert not any("morning" in line for line in lines)

    def test_the_guard_line_survives_a_kind_nothing_can_name(self):
        """Each clause is omitted on its own field rather than the block on the
        vocabulary: the threshold is one number under one name in both shapes,
        and an unreadable kind has not made it untrue."""
        lines = artefacts.replay_lines({**_schedule_row(), "kind": "occupancy"})

        assert lines == [line for line in lines if "switches off below" in line]
        assert lines


class TestAScheduleCardReadsInOneMessage:
    def test_the_whole_message_says_nothing_about_a_night(self):
        """The check this package is measured by from the other end: §9's
        plain-text rendering is the design constraint, and a card that states
        the wrong reason is what it forbids."""
        text = artefacts.detail_text(_schedule_row())

        assert "night" not in text.lower()
        assert "overnight" not in text.lower()

    def test_and_still_carries_all_three_artefacts(self):
        text = artefacts.detail_text(_schedule_row())

        assert "never dropped below" in text                # what was found
        assert "Measured over 90 days" in text              # over what window
        assert "Estimated saving" in text                   # what it is worth
        assert "At 07:00 on Mon-Fri" in text                # what would happen

    def test_the_readback_is_the_builder_s_own_sentence_for_this_shape(self):
        row = _schedule_row()

        assert artefacts.readback(row) == builder.readback(
            row["config"], row["evidence"], ceiling_w=row["replay"]["guard"]["below_w"])

    def test_the_device_is_named_without_the_automation_s_suffix(self):
        """A second alias suffix this module never knew about: stripping the
        night's by hand called the device "Office printer off on a schedule"."""
        assert artefacts.label_for(_schedule_row()) == "Office printer"


class TestArtefactFourForAScheduleRule:
    """§7.2 over a window that is not a week of anything. `verify.days_for`
    gives a Mon-Fri rule a fortnight, and ten occurrences is what it offers."""

    def test_both_windows_are_stated_in_occurrences_not_in_days(self):
        after = _schedule_reported()["verdict"]["after"]

        line = artefacts.verification_windows(_schedule_reported())

        assert f"the {after['occurrences_examined']} occurrences to " \
               f"{after['window']['to']}" in line
        assert "07:00-17:00 local Mon-Fri" in line

    def test_and_the_length_of_the_before_is_never_called_a_week(self):
        """2.14.4 made the after-length per suggestion. A fortnight reported as
        "the week before the change" is a comparison window that was not
        measured, which is the one thing this sentence exists to state."""
        line = artefacts.verification_windows(_schedule_reported())

        assert "the 14 days before the change was applied" in line
        assert "the week before" not in line

    def test_both_weeks_show_their_own_observation(self):
        before = _schedule_reported()["verdict"]["before"]

        lines = artefacts.verification_evidence(_schedule_reported())

        assert any(f"never dropped below about {artefacts._num(before['watts'])} W on "
                   f"{before['occurrences']} of the {before['occurrences_examined']} "
                   "occurrences in those 14 days" in line for line in lines)
        assert any("really did switch off on all" in line and "occurrences" in line
                   for line in lines)

    def test_the_headline_carries_its_number_and_the_window_it_was_taken_over(self):
        line = artefacts.verification_headline(_schedule_reported())

        assert "Office printer" in line
        assert "occurrences to" in line
        assert "nights" not in line

    def test_the_measured_result_is_a_range_and_names_no_night(self):
        verdict = _schedule_reported()["verdict"]
        assert verdict["outcome"] == "saved", verdict.get("reason")

        result = artefacts.verification_result(_schedule_reported())

        assert "between" in result and "kWh a month" in result
        assert "overnight" not in result

    def test_and_the_whole_report_reads_in_one_message(self):
        text = artefacts.verification_text(_schedule_reported())

        assert "night" not in text.lower()
        assert "roll it back" in text


class TestTheEdgesOfTheVocabulary:
    """What a sentence does with a figure it cannot read, a count that is not a
    tally, and a row from before there were two shapes."""

    def test_a_projection_between_one_and_two_is_still_plural(self):
        """Not every count here is a tally. `nights_per_month` on a device
        present for a fortnight of the window comes out at 1.5, and truncating
        it to pick the plural reads "1.5 night a month"."""
        sparse = {**REPLAY_ESTIMATE, "nights_per_month": 1.5, "kwh_per_month": 0.07}

        sentence = artefacts.estimate_sentence(_row(estimate=sparse))

        assert "1.5 nights a month" in sentence

    def test_but_exactly_one_is_not(self):
        assert artefacts._many(1, "night") == "1 night"
        assert artefacts._many(1.0, "night") == "1 night"

    def test_a_coverage_figure_that_cannot_be_read_says_nothing(self):
        """The OMIT rule at the one place a figure is arithmetic before it is
        words. Read as zero it prints "0 of those 30 days had readable data" —
        a measurement saying the agent saw nothing, invented out of a field
        nobody could parse."""
        unreadable = {**EVIDENCE, "covered_days": "many"}

        lines = artefacts.evidence_lines(_row(evidence=unreadable))

        assert not any("had readable data" in line for line in lines)
        assert any("Measured over 30 days" in line for line in lines)

    def test_and_one_that_overruns_its_window_is_capped_rather_than_odd(self):
        """Both detectors count the calendar DATES the record holds a row on,
        and a window opening mid-afternoon touches one more than it spans. "31
        of those 30 days" reads as a mistake; capped, it says what is true —
        every day of it had data."""
        whole = {**EVIDENCE, "covered_days": 31}

        lines = artefacts.evidence_lines(_row(evidence=whole))

        assert any("30 of those 30 days had readable data" in line for line in lines)

    def test_a_row_from_before_there_were_two_shapes_reads_as_a_night(self):
        """`verify.shape_of` reads a row with no kind as a night — the only
        reading history allows — and will measure one. If this omitted instead,
        that week's report would carry a saving with its observation stripped
        off, which is the §9 rule this module exists to enforce."""
        old = {key: value for key, value in _row().items() if key != "kind"}

        assert "01:00-05:00 local" in artefacts.why(old)
        assert "21 of the 30 nights examined" in artefacts.why(old)

    def test_but_a_kind_this_build_has_no_detector_for_still_omits(self):
        """The two are different cases. A MISSING kind has a reading; an
        UNKNOWN one is a detector this build does not have, and guessing which
        of the shapes it resembles is what the refusal exists to stop."""
        said = artefacts.why(_row(kind="occupancy"))

        assert "01:00-05:00" not in said
        assert "nights" not in said

    def test_the_digest_omits_its_counts_rather_than_raising_on_them(self, monkeypatch):
        """`_measured_on`'s replay branch answers off core/replay.py's table
        alone, and the noun beside those counts comes from the DETECTOR
        registry. The two agree today, so the disagreement is staged: a kind
        core/replay.py can grade and this build has no detector for used to be
        a KeyError in the digest, which takes the whole listing rather than one
        line's clause."""
        monkeypatch.setattr(detectors, "for_kind", lambda kind: None)

        line = artefacts.headline(_row())

        assert "Living room TV" in line and "1.01 kWh a month" in line
        assert "of 30 nights" not in line
        assert "(01:00-05:00 local)" not in line
