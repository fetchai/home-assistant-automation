"""Analyst rows through §9's artefacts and the week-later lifecycle —
proposal §5, WP-B2.

The first half renders planted rows through every public renderer and holds
them to B-3: the only numbers printed are ones the core recounted, a claim
nobody could check is not shown as evidence, and the model's text is printed
and never read. The second half takes an analyst row from approval to a
report a week later against real stores, with Home Assistant the only thing
stubbed: `settle` records that there is nothing to measure rather than
measuring, `run_due` reports that at the first due date without retrying, and
decision 2's deleted / disabled / edited checks still hold.
"""
import json

import pytest

from core import activity
from core import artefacts
from core import review
from core import suggestions
from core import verify
from tools.tool_homeassistant import registry

DAY = 86400
NOW = 1_780_000_000
AUTOMATION_ID = "1790000000000"

CONFIG = {"alias": "Hall light off at midnight",
          "triggers": [{"trigger": "time", "at": "00:00:00"}],
          "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.hall"}}]}
MATCHED = {"text": "The hall light was on\npast midnight on most nights", "type": "on_hours",
           "params": {"entity_id": "light.hall"}, "stated": 23, "recounted": 19,
           "result": "matched"}
UNCHECKABLE = {"text": "Nobody walks through the hall after midnight", "type": "free",
               "params": {}, "stated": 0, "recounted": None, "result": "not_checkable"}
RATIONALE = "The light is left on most nights.\nA midnight switch-off catches it."


def _row(level="untested", claims=(), **overrides):
    row = {"id": 7, "key": "analyst:3f9a1c", "kind": "analyst", "source": "analyst",
           "state": "proposed", "change_id": 41, "entities": ["light.hall"],
           "evidence_level": level, "model": "asi1-mini",
           "title": "Turn the hall light off at midnight", "rationale": RATIONALE,
           "claims": list(claims), "config": CONFIG, "evidence": None,
           "estimate": None, "replay": None}
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# artefacts 1-3
# --------------------------------------------------------------------------
class TestTheHeadline:
    @pytest.mark.parametrize("level, words", [
        ("untested", "not tested"),
        ("checked", "checked against your history"),
        ("replayed", "replayed"),
    ])
    def test_it_is_the_title_and_the_label(self, level, words):
        assert artefacts.headline(_row(level)) == \
            f"Turn the hall light off at midnight ({words})"

    def test_a_level_outside_the_vocabulary_reads_as_the_weakest(self):
        assert artefacts.headline(_row("proven")).endswith("(not tested)")

    def test_the_digest_is_one_line_per_row_with_the_label_on_it(self):
        text = artefacts.digest_text([_row("checked", [MATCHED])])

        assert "1. Turn the hall light off at midnight (checked against your history)" in text
        assert artefacts.digest([_row()])["suggestions"][0]["line"] == \
            "Turn the hall light off at midnight (not tested)"


class TestTheDetail:
    def test_an_untested_row_carries_the_fixed_warning_as_its_own_field(self):
        detail = artefacts.detail(_row("untested"))

        assert detail["warning"] == ("I couldn't test this against your history — "
                                     "check it yourself before approving.")
        assert detail["source"] == "analyst"
        assert detail["evidence_level"] == "untested"
        assert detail["evidence_label"] == "not tested"

    def test_and_it_is_the_first_line_of_the_text(self):
        text = artefacts.detail_text(_row("untested"))

        assert text.splitlines()[0] == artefacts.UNTESTED_WARNING

    def test_a_checked_row_has_no_warning(self):
        detail = artefacts.detail(_row("checked", [MATCHED]))

        assert detail["warning"] == ""
        assert artefacts.UNTESTED_WARNING not in artefacts.detail_text(
            _row("checked", [MATCHED]))

    def test_evidence_prints_the_recounted_figure_and_never_the_stated_one(self):
        lines = artefacts.detail(_row("checked", [MATCHED]))["evidence"]

        assert lines == ["The hall light was on past midnight on most nights — "
                         "recounted from your history: 19"]
        assert "23" not in artefacts.detail_text(_row("checked", [MATCHED]))

    def test_a_claim_nobody_could_check_is_not_evidence(self):
        row = _row("untested", [MATCHED, UNCHECKABLE])

        assert len(artefacts.evidence_lines(row)) == 1
        assert "Nobody walks through the hall" not in artefacts.detail_text(row)

    def test_a_matched_claim_with_no_printable_recount_is_omitted(self):
        row = _row("checked", [{**MATCHED, "recounted": {"n": 19}}])

        assert artefacts.evidence_lines(row) == []

    def test_the_reasoning_is_under_its_own_heading(self):
        detail = artefacts.detail(_row())
        text = artefacts.detail_text(_row())

        assert detail["reasoning"] == RATIONALE
        assert detail["reasoning_heading"] == \
            "Why the agent suggests this (its own reasoning, not checked)"
        assert f"{artefacts.REASONING_HEADING}:\n{RATIONALE}" in text

    def test_the_why_is_the_title_as_a_sentence(self):
        assert artefacts.why(_row()) == "Turn the hall light off at midnight."
        assert artefacts.why(_row(title="Is it on?")) == "Is it on?"

    def test_the_readback_is_the_alias_then_the_config_as_yaml(self):
        detail = artefacts.detail(_row())

        assert detail["readback"].startswith("Hall light off at midnight\n```yaml\n")
        assert detail["readback"].endswith("```")
        assert "light.turn_off" in detail["readback_yaml"]
        assert "```" not in detail["readback_yaml"]
        assert "What would be written:\nHall light off at midnight" in \
            artefacts.detail_text(_row())

    def test_there_is_no_estimate_no_replay_and_no_basis(self):
        detail = artefacts.detail(_row())

        assert (detail["estimate"], detail["replay"], detail["basis"]) == ("", [], None)
        assert "kWh" not in artefacts.detail_text(_row())
        assert "saving" not in artefacts.detail_text(_row()).lower()

    def test_the_label_is_the_alias_the_config_writes(self):
        assert artefacts.label_for(_row()) == "Hall light off at midnight"
        assert artefacts.label_for(_row(config=None, state="candidate")) == \
            "Turn the hall light off at midnight"

    def test_model_text_is_printed_not_read(self):
        """B-3: a number inside the model's text is printed as text, and never
        becomes a figure anywhere else in the artefact."""
        row = _row(title="Save 400 kWh a month", rationale="Trust me: 400 kWh.")

        detail = artefacts.detail(row)

        assert detail["headline"] == "Save 400 kWh a month (not tested)"
        assert detail["estimate"] == "" and detail["evidence"] == []


class TestTheQuestion:
    def test_a_proposed_analyst_row_is_a_question_in_the_detail_text(self):
        asked = artefacts.question(_row())

        assert asked[artefacts.QUESTION_MARKER] is True
        assert asked["change_id"] == 41
        assert asked["question"] == artefacts.detail_text(_row())
        assert asked["label"] == "Hall light off at midnight"


class TestADetectorRowIsUnaffected:
    def test_a_row_with_no_source_is_a_detector_row(self):
        row = _row(source=None, kind="standby")

        assert artefacts.detail(row)["source"] == "detector"
        assert artefacts.detail(row)["warning"] == ""
        assert "(not tested)" not in artefacts.headline(row)


# --------------------------------------------------------------------------
# artefact 4
# --------------------------------------------------------------------------
class TestTheReport:
    def _verified(self, outcome="not_measured", reason=""):
        return _row(state="verified", baseline={"outcome": "not_measured"},
                    verdict={"outcome": outcome, "reason": reason, "attempts": 1,
                             "before": {"outcome": "not_measured"}, "after": {},
                             "floor_kwh_per_month": None,
                             "saved_range_kwh_per_month": None})

    def test_nothing_to_measure_is_said_in_the_fixed_sentence(self):
        report = artefacts.verification(self._verified())

        assert report["headline"] == \
            "Hall light off at midnight: nothing the agent could measure"
        assert report["result"] == \
            "The agent had nothing it could measure for this change."
        assert report["evidence"] == [] and report["windows"] == ""
        assert report["estimate"] == "" and report["basis"] is None
        assert report["undoable"] is True

    def test_the_text_offers_keep_or_roll_back(self):
        text = artefacts.verification_text(self._verified())

        assert "Keep it, or roll it back" in text
        assert "kWh" not in text

    def test_not_running_on_an_analyst_row_claims_no_saving_either_way(self):
        report = artefacts.verification(self._verified(
            "not_running", "automation 1 has been switched off in Home Assistant"))

        assert report["result"] == ("No measured result: automation 1 has been switched "
                                    "off in Home Assistant.")


# --------------------------------------------------------------------------
# the lifecycle, against real stores
# --------------------------------------------------------------------------
@pytest.fixture
def stores(tmp_path, monkeypatch):
    suggestions.close()
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setattr(review, "DIGEST_PATH", str(tmp_path / "digest.json"))
    yield
    suggestions.close()
    activity.close()


def _snapshot(automation_state="on"):
    states = {"light.hall": {"state": "on", "attributes": {}}}
    if automation_state:
        states["automation.hall_light_off_at_midnight"] = {
            "state": automation_state, "attributes": {"id": AUTOMATION_ID}}
    return {"areas": [], "devices": [], "entities": [], "states": states}


def _live(config=None):
    """What Home Assistant hands back for the config written: HA's own id and
    the three defaults `_write_automation` fills, in the singular keys."""
    config = config or CONFIG
    return {"id": AUTOMATION_ID, "alias": config["alias"], "description": "",
            "mode": "single", "trigger": config["triggers"], "condition": [],
            "action": config["actions"]}


@pytest.fixture
def live(monkeypatch):
    held = {"config": _live()}
    monkeypatch.setattr(verify, "read_automation",
                        lambda automation_id: (True, held["config"]) if held["config"]
                        else (False, "not found"))
    return held


def _applied(applied_at=NOW):
    """An analyst row walked from candidate to applied the way the run, the
    pass and `/rest/approve` walk one: shown with its chart, proposed through
    a journal row, approved, and settled."""
    suggestion_id = suggestions.upsert_analyst_candidate(
        "analyst:3f9a1c", entities=["light.hall"],
        title="Turn the hall light off at midnight", rationale=RATIONALE,
        model="asi1-mini")
    assert suggestions.mark_shown(suggestion_id, config=CONFIG, finding=RATIONALE,
                                  rule_text="At midnight, turn the hall light off.",
                                  chart=CHART)
    change_id = activity.record_proposal(
        tool="Create Home Assistant automation",
        args={"config_json": json.dumps(CONFIG)}, source="agent")
    assert suggestions.mark_proposed(suggestion_id, change_id)
    real_now = activity._now
    activity._now = lambda: int(applied_at)
    try:
        activity.record_decision(
            change_id, "approved", status="applied",
            result=json.dumps({"created": True, "automation_id": AUTOMATION_ID}),
            inverse={"op": "delete_automation", "automation_id": AUTOMATION_ID})
    finally:
        activity._now = real_now
    assert review.settle(change_id, now=applied_at) is True
    return suggestions.suggestion(suggestion_id)


class TestSettle:
    def test_settle_records_that_there_is_nothing_to_measure(self, stores, monkeypatch):
        def _never(*args, **kwargs):
            raise AssertionError("an analyst row must not be measured")
        monkeypatch.setattr(verify, "measure_week", _never)

        row = _applied()

        assert row["state"] == "applied"
        assert row["baseline"] == {"outcome": "not_measured"}
        assert row["verify_after"] == NOW + verify.AFTER_DAYS * DAY

    def test_the_week_is_after_days(self):
        assert verify.days_for(_row()) == verify.AFTER_DAYS
        assert verify.measurable(_row()) is False
        assert verify.measurable(_row(source=None, kind="standby")) is True


class TestRunDue:
    def test_it_is_reported_at_the_first_due_date_as_not_measured(self, stores, live):
        row = _applied()

        done = verify.run_due(now=NOW + verify.AFTER_DAYS * DAY, snapshot=_snapshot())

        reported = suggestions.suggestion(row["id"])
        assert done == 1
        assert reported["state"] == "verified"
        assert reported["verdict"]["outcome"] == verify.OUTCOME_NOT_MEASURED
        assert reported["verdict"]["attempts"] == 1
        assert reported["verdict"]["floor_kwh_per_month"] is None
        assert reported["verdict"]["saved_range_kwh_per_month"] is None

    def test_it_is_never_retried(self, stores, live):
        verdict = verify._not_measured_verdict(_row(), 1, NOW)

        assert verify.should_retry(verdict) is False

    def test_the_user_gets_the_keep_or_roll_back_question(self, stores, live):
        _applied()
        verify.run_due(now=NOW + verify.AFTER_DAYS * DAY, snapshot=_snapshot())

        queued = [m for m in activity.messages() if m["kind"] == "verification"]
        assert len(queued) == 1
        assert artefacts.NOT_MEASURED_SENTENCE in queued[0]["body"]
        assert "Keep it, or roll it back" in queued[0]["body"]
        assert queued[0]["payload"]["outcome"] == "not_measured"

    def test_and_keeping_it_is_an_ordinary_answer(self, stores, live):
        row = _applied()
        verify.run_due(now=NOW + verify.AFTER_DAYS * DAY, snapshot=_snapshot())

        answered = verify.answer(row["id"], keep=True)

        assert answered["ok"] and answered["state"] == "kept"

    def test_a_deleted_automation_still_withdraws_it(self, stores, live):
        row = _applied()
        live["config"] = None

        verify.run_due(now=NOW + verify.AFTER_DAYS * DAY,
                       snapshot=_snapshot(automation_state=None))

        retired = suggestions.suggestion(row["id"])
        assert retired["state"] == "retired"
        assert "no longer in Home Assistant" in retired["retired_reason"]

    def test_a_disabled_automation_is_still_not_running(self, stores, live):
        row = _applied()

        verify.run_due(now=NOW + verify.AFTER_DAYS * DAY,
                       snapshot=_snapshot(automation_state="off"))

        verdict = suggestions.suggestion(row["id"])["verdict"]
        assert verdict["outcome"] == verify.OUTCOME_NOT_RUNNING
        assert "switched off" in verdict["reason"]

    def test_an_edited_automation_is_still_not_running(self, stores, live):
        row = _applied()
        live["config"] = _live({**CONFIG, "triggers": [{"trigger": "time", "at": "01:00:00"}]})

        verify.run_due(now=NOW + verify.AFTER_DAYS * DAY, snapshot=_snapshot())

        verdict = suggestions.suggestion(row["id"])["verdict"]
        assert verdict["outcome"] == verify.OUTCOME_NOT_RUNNING
        assert "edited" in verdict["reason"]

    def test_the_defaults_home_assistant_fills_in_are_not_an_edit(self):
        assert verify._same_config(CONFIG, _live())
        assert verify._same_config(CONFIG, {**_live(), "alias": "Renamed",
                                            "description": "mine now"})
        assert not verify._same_config(CONFIG, {**_live(), "mode": "restart"})
        assert not verify._same_config(CONFIG, {})

    def test_the_pass_reaches_it_too(self, stores, live, monkeypatch):
        """Through review.run_once, the only caller in production."""
        row = _applied()
        monkeypatch.setattr(registry, "fetch_registry", lambda: (True, _snapshot()))

        review.run_once(now=NOW + verify.AFTER_DAYS * DAY)

        assert suggestions.suggestion(row["id"])["verdict"]["outcome"] == "not_measured"


# --------------------------------------------------------------------------
# a `shown` row — the explorer run (free-hand F-6, F-10, F-WP3)
# --------------------------------------------------------------------------
FINDING = "The hall light stays on\nafter midnight on most nights"
RULE = "At midnight, turn the hall light off."
CHART = {"spec": {"type": "timeline", "entities": ["light.hall"], "state": None,
                  "start": NOW - 14 * DAY, "end": NOW},
         "series": None, "markers": None, "error": None}


def _shown(**overrides):
    row = _row("shown", **{"finding": FINDING, "rule_text": RULE, "chart": CHART,
                           "rationale": FINDING, **overrides})
    row["claims"] = None  # as mark_shown stores it
    return row


class TestAShownRow:
    def test_the_headline_points_at_the_chart(self):
        assert artefacts.headline(_shown()) == \
            "Turn the hall light off at midnight (suggested by the analyst — see the chart)"

    def test_why_is_the_finding_as_one_sentence(self):
        assert artefacts.why(_shown()) == "The hall light stays on after midnight on most nights."

    def test_no_warning_and_no_evidence_lines(self):
        detail = artefacts.detail(_shown())
        assert detail["warning"] == ""
        assert detail["evidence"] == []
        assert artefacts.UNTESTED_WARNING not in artefacts.detail_text(_shown())

    def test_the_rule_is_under_its_heading_and_the_finding_is_not_repeated(self):
        detail = artefacts.detail(_shown())
        assert detail["reasoning_heading"] == artefacts.RULE_HEADING
        assert detail["reasoning"] == RULE
        text = artefacts.detail_text(_shown())
        assert f"{artefacts.RULE_HEADING}:\n{RULE}" in text
        assert artefacts.REASONING_HEADING not in text
        assert text.count("most nights") == 1

    def test_the_readback_is_the_yaml(self):
        detail = artefacts.detail(_shown())
        assert detail["readback_yaml"].startswith("alias: Hall light off at midnight")
        assert "What would be written:\n" in artefacts.detail_text(_shown())

    def test_the_label_words_are_in_detail(self):
        detail = artefacts.detail(_shown())
        assert detail["evidence_level"] == "shown"
        assert detail["evidence_label"] == "suggested by the analyst — see the chart"

    def test_the_digest_reads_it(self):
        assert "see the chart" in artefacts.digest_text([_shown()])

    def test_without_a_finding_why_falls_back_to_the_title(self):
        assert artefacts.why(_shown(finding=None)) == "Turn the hall light off at midnight."


# --------------------------------------------------------------------------
# the sentences around a shown row's chart (free-hand F-7, F-8, F-11, F-WP5)
# --------------------------------------------------------------------------
START = 1_789_000_000 - 1_789_000_000 % DAY     # a UTC midnight
DRAWN = {"spec": {"type": "timeline", "entities": ["light.hall", "binary_sensor.hall_motion"],
                  "state": None, "start": START, "end": START + 7 * DAY},
         "series": {"days": [], "truncated": False, "days_dropped": 0},
         "markers": [{"day": "2026-09-10", "minute": 0, "action": "turn_off",
                      "entity": "light.hall"},
                     {"day": "2026-09-11", "minute": 0, "action": "turn_off",
                      "entity": "light.hall"}],
         "markers_note": None, "error": None, "drawn_at": START + 7 * DAY,
         "window_clamped": None}


@pytest.fixture
def utc(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _chart(**overrides):
    return {**DRAWN, **overrides}


class TestTheShownCard:
    """artefacts.shown_detail: what the panel prints around the chart it
    draws. The sentences are Python's for §9's reason — they are claims about
    the evidence — and the panel only lays them out."""

    def test_a_drawn_chart_has_a_caption_and_its_markers_counted(self, utc):
        card = artefacts.shown_detail(_shown(chart=_chart()))
        assert card["chart_error"] == ""
        assert card["chart_caption"] == (
            "Timeline of light.hall, binary_sensor.hall_motion, "
            "2026-09-10 00:00 to 2026-09-17 00:00")
        assert card["chart_markers"] == ("The markers show the 2 times the rule would have "
                                         "acted on a charted device.")
        assert card["chart_clamped"] == card["chart_truncated"] == ""

    def test_the_caption_names_entities_by_the_household_s_name_where_it_has_one(self, utc):
        card = artefacts.shown_detail(_shown(chart=_chart()),
                                      {"light.hall": "Hall light", "binary_sensor.hall_motion": ""})
        assert card["chart_caption"] == (
            "Timeline of Hall light, binary_sensor.hall_motion, "
            "2026-09-10 00:00 to 2026-09-17 00:00")

    def test_a_chart_the_record_could_not_draw_is_the_sentence_and_nothing_else(self):
        card = artefacts.shown_detail(_shown(chart={
            "spec": {"type": "timeline", "entities": ["light.porch"]}, "series": None,
            "markers": None, "error": "light.porch is not watched, so the record holds "
                                      "nothing to draw"}))
        assert card["chart_error"] == ("The agent asked for a chart the record could not draw: "
                                       "light.porch is not watched, so the record holds "
                                       "nothing to draw.")
        assert (card["chart_caption"], card["chart_markers"], card["chart_clamped"]) == \
            ("", "", "")

    def test_a_row_stored_before_charts_were_drawn_says_so(self):
        """#15's shape before `run_analyst.py --redraw`: a request, no error,
        `series: null`. Nothing was refused — it is not F-7's sentence."""
        card = artefacts.shown_detail(_shown())
        assert card["chart_error"] == artefacts.CHART_NOT_DRAWN

    def test_a_rule_outside_the_subset_says_which_part(self):
        card = artefacts.shown_detail(_shown(chart=_chart(
            markers=None, markers_note="the agent can't draw what this rule would have done: "
                                       "it uses mode queued")))
        assert card["chart_markers"] == ("The agent can't draw what this rule would have done: "
                                         "it uses mode queued.")

    def test_a_chart_type_without_markers_says_that(self):
        card = artefacts.shown_detail(_shown(chart=_chart(
            spec={**DRAWN["spec"], "type": "heatmap", "entities": ["light.hall"],
                  "state": "on"},
            series={"state": "on", "counts": [], "truncated": False}, markers=None)))
        assert card["chart_markers"] == artefacts.NO_MARKERS_FOR_TYPE
        assert card["chart_caption"].startswith("Changes by weekday and hour of light.hall")
        assert card["chart_caption"].endswith("— changes to “on”")

    def test_a_rule_that_would_not_have_acted_is_a_finding(self):
        assert artefacts.shown_detail(_shown(chart=_chart(markers=[])))["chart_markers"] == \
            artefacts.NO_MARKERS_IN_WINDOW

    def test_a_rule_acting_on_nothing_charted_prints_the_note_not_the_finding(self):
        card = artefacts.shown_detail(_shown(chart=_chart(
            markers=[], markers_note="the rule acts on light.porch, which this chart does "
                                     "not draw")))
        assert card["chart_markers"] == ("The rule acts on light.porch, which this chart does "
                                         "not draw.")

    def test_a_clamped_window_is_stated(self, utc):
        card = artefacts.shown_detail(_shown(chart=_chart(window_clamped={
            "asked": {"start": START - DAY // 2, "end": START + 7 * DAY},
            "drawn": {"start": START, "end": START + 7 * DAY},
            "why": ["an overlay covers at most 7 days"]})))
        assert card["chart_clamped"] == (
            "The agent asked for 2026-09-09 12:00 to 2026-09-17 00:00; the chart shows "
            "2026-09-10 00:00 to 2026-09-17 00:00, because an overlay covers at most 7 days.")

    def test_days_left_out_are_stated(self):
        card = artefacts.shown_detail(_shown(chart=_chart(
            series={"days": [], "truncated": True, "days_dropped": 3})))
        assert card["chart_truncated"] == \
            "The oldest 3 days were left out to keep the chart small."

    def test_the_household_s_answer_is_printed_as_given(self, utc):
        card = artefacts.shown_detail(_shown(chart=_chart(), feedback={
            "answer": "no", "reason": "the dog sleeps there", "at": START}))
        assert card["feedback_line"] == "You answered no on 2026-09-10, because: the dog sleeps there"
        assert artefacts.shown_detail(_shown(chart=_chart()))["feedback_line"] == ""

    def test_a_rollback_is_said_as_what_happened(self, utc):
        """F-WP6: written by the rollback path, with no reason typed."""
        card = artefacts.shown_detail(_shown(chart=_chart(), feedback={
            "answer": "rolled_back", "reason": "", "at": START, "change_id": 4}))
        assert card["feedback_line"] == "You applied this and then rolled it back on 2026-09-10."

    def test_what_was_sent_is_headed_and_says_what_it_leaves_out(self):
        card = artefacts.shown_detail(_shown(chart=_chart()))
        assert card["sent_heading"] == artefacts.SENT_HEADING
        assert "overview" in card["sent_note"]

    @pytest.mark.parametrize("row", [_row("untested"), _row("checked")])
    def test_an_audited_row_has_none_of_it(self, row):
        assert artefacts.shown_detail(row) == {}

    def test_a_detector_row_has_none_of_it(self):
        assert artefacts.shown_detail({"source": "detector", "kind": "standby",
                                       "evidence_level": "replayed"}) == {}
