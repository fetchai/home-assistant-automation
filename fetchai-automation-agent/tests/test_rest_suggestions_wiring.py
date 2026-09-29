"""Wiring tests for /rest/suggestions — the endpoint behind the panel's
Suggestions tab.

What the rendering may claim is held in test_artefacts.py and the chat path in
test_suggestion_chat.py. What is proved here is what the PANEL receives, and
one negative that matters more than anything the endpoint returns: there is no
APPROVE endpoint in this file. The panel's Accept and Reject post the
suggestion's change_id to the existing /rest/approve, so both surfaces land on
gate.apply and both reach review.settle.

There IS a verdict endpoint, and the tests below say why that is not the same
thing: it answers a change approved a week ago, and the only write behind it is
an undo the gate already authorised. The handler listing is asserted exactly, so
a third one cannot appear without someone reading that argument.
"""
import asyncio
import json

import pytest

import legacy_rows
import conftest
from core import activity
from core import gate
from core import protocols
from core import suggestions
from rest import rest_chat
from rest import rest_suggestions
from rest.rest_chat import ApprovalRequest
from tools.tool_homeassistant import automations, registry

REAL_DEPS = not conftest.STUBBED

CREATE = "Create Home Assistant automation"
CONFIG = {"alias": "Living room TV off overnight", "trigger": [], "condition": [], "action": []}


def _run(coro):
    return asyncio.run(coro)


def _request(**kwargs):
    return protocols.SuggestionsRequest(**kwargs)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    activity.close()
    suggestions.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    suggestions.close()
    activity.close()


@pytest.fixture(autouse=True)
def offline_ha(monkeypatch):
    monkeypatch.setattr(gate, "_ha_get", lambda path: (True, {}))
    monkeypatch.setattr(automations, "_validate_by_test_create", lambda cfg: (True, "1"))
    monkeypatch.setattr(registry, "fetch_registry", lambda: (False, "no HA in tests"))


def _a_proposal(key="standby:sensor.tv_power"):
    suggestion_id = suggestions.upsert_candidate(
        key, "standby",
        entities=["sensor.tv_power", "switch.tv"],
        evidence={"entity": "sensor.tv_power", "switch": "switch.tv", "watts": 12.0,
                  "kwh_per_month": 1.01, "nights": 21, "nights_examined": 30,
                  "night_window": "01:00-05:00 local",
                  "window": {"from": "2026-07-31", "to": "2026-08-30", "days": 30}})
    suggestions.mark_validated(
        suggestion_id,
        replay={"nights_examined": 30, "nights_fired": 24, "nights_saved": 21,
                "nights_wrong": 0},
        estimate={"basis": "replay", "kwh_per_month": 1.01, "watts": 12.0,
                  "hours_per_night": 4.0, "nights_per_month": 21.0},
        config=CONFIG)
    raised = gate.propose(CREATE, {"config_json": json.dumps(CONFIG)})
    assert raised["ok"] is True
    assert suggestions.mark_proposed(suggestion_id, raised["change_id"])
    return suggestion_id, raised["change_id"]


async def _returns(value):
    return value


class TestReadingTheSuggestions:
    def test_an_empty_store_is_not_an_error(self):
        response = _run(rest_suggestions.handle_suggestions(None, _request()))

        assert response.open == []
        assert response.digest == []
        assert response.error == ""

    def test_a_proposal_arrives_with_its_sentences_already_written(self):
        """Rendered in Python, not in JavaScript. §9's rules are about what a
        rendering may claim, and one implemented twice is one that drifts."""
        _a_proposal()

        response = _run(rest_suggestions.handle_suggestions(None, _request()))

        row = response.open[0]
        assert row["label"] == "Living room TV"
        assert "never dropped below" in row["why"]
        assert "1.01 kWh a month" in row["estimate"]
        assert any("2026-07-31 to 2026-08-30" in line for line in row["evidence"])
        assert row["basis"] == "replay"

    def test_the_digest_line_is_the_same_one_the_chat_gets(self):
        from core import artefacts

        suggestion_id, _ = _a_proposal()

        response = _run(rest_suggestions.handle_suggestions(None, _request()))

        assert response.digest[0]["line"] == artefacts.headline(
            suggestions.suggestion(suggestion_id))

    def test_the_diff_comes_from_the_journal_rather_than_being_re_rendered(self):
        """The suggestion row holds the config as a structure; the journal
        holds the YAML a human is shown. Rendering the structure again here
        would be a second YAML of one config, and the one the user reads should
        be the one the approval was recorded against."""
        _, change_id = _a_proposal()

        response = _run(rest_suggestions.handle_suggestions(None, _request()))

        assert response.open[0]["new_yaml"] == activity.change(change_id)["new_config"]
        assert "Living room TV off overnight" in response.open[0]["new_yaml"]

    def test_only_what_is_actually_waiting_is_listed(self):
        """A candidate nobody has been shown is not a suggestion the user can
        answer, and neither is one already applied."""
        suggestions.upsert_candidate("standby:sensor.hifi_power", "standby",
                                     entities=["sensor.hifi_power"], evidence={"watts": 3})
        _a_proposal()

        response = _run(rest_suggestions.handle_suggestions(None, _request()))

        assert len(response.open) == 1

    def test_it_reads_only_local_storage(self, monkeypatch):
        """It sits beside the Activity tab and has the same job: the surface
        that keeps working when the connection being audited does not."""
        _a_proposal()

        def _boom(*args, **kwargs):
            raise AssertionError("the suggestions listing reached Home Assistant")

        monkeypatch.setattr(gate, "_ha_get", _boom)

        response = _run(rest_suggestions.handle_suggestions(None, _request()))

        assert response.open


class TestTheAnswerGoesThroughTheExistingEndpoint:
    def test_this_module_offers_no_way_to_APPROVE_one(self):
        """A second approval entry would be a second place for "every write
        lands on gate.apply" to stop being true, and a second place for a
        rejection to forget to become a cooldown.

        The verdict handler beside it is not that: it answers a change that was
        approved a week ago, "keep" writes nothing at all, and "roll back" runs
        the inverse the gate computed at approval time through the one module
        that executes one. Nor is the feedback handler (F-WP5): it stores what
        the household said about an explorer idea and nothing else — the panel
        then posts the same answer to /rest/approve. Named here so that a
        fourth handler cannot be added without someone reading this sentence.
        """
        assert sorted(name for name in dir(rest_suggestions)
                      if name.startswith("handle_")) == ["handle_feedback", "handle_suggestions",
                                                         "handle_verdict"]

    def test_and_the_verdict_handler_never_reaches_the_gate(self, monkeypatch):
        """It is on the other side of core/gate.py from Accept and Dismiss:
        the write it may perform is an undo, not an approval."""
        def _boom(*args, **kwargs):
            raise AssertionError("the verdict endpoint reached the approval gate")

        monkeypatch.setattr(gate, "apply", _boom)
        monkeypatch.setattr(gate, "pending_for", _boom)

        response = _run(rest_suggestions.handle_verdict(
            None, protocols.SuggestionsRequest(suggestion_id=999, keep=True)))

        assert "no suggestion" in response.error

    def test_the_change_id_the_panel_is_given_is_answerable_there(self, monkeypatch):
        suggestion_id, change_id = _a_proposal()
        calls = []

        async def _fake(tool_name, tool_args):
            calls.append((tool_name, tool_args))
            return json.dumps({"created": True, "automation_id": "1748000000000"})

        monkeypatch.setattr(rest_chat, "_execute_tool_async", _fake)

        listed = _run(rest_suggestions.handle_suggestions(None, _request()))
        _run(rest_chat.handle_approve(
            None, ApprovalRequest(change_id=listed.open[0]["change_id"], approved=True)))

        assert calls == [(CREATE, {"config_json": json.dumps(CONFIG)})]
        assert suggestions.suggestion(suggestion_id)["state"] == "applied"

    def test_dismissing_from_the_panel_is_a_cooldown_not_a_verdict(self, monkeypatch):
        suggestion_id, change_id = _a_proposal()
        monkeypatch.setattr(rest_chat, "_execute_tool_async",
                            lambda name, args: _returns("nothing should run"))

        _run(rest_chat.handle_approve(
            None, ApprovalRequest(change_id=change_id, approved=False)))

        dismissed = suggestions.suggestion(suggestion_id)
        assert dismissed["state"] == "dismissed"
        assert dismissed["dismissed_until"] > activity._now()

    def test_an_answered_suggestion_leaves_the_listing(self, monkeypatch):
        _, change_id = _a_proposal()
        monkeypatch.setattr(rest_chat, "_execute_tool_async",
                            lambda name, args: _returns(json.dumps({"created": True})))

        _run(rest_chat.handle_approve(
            None, ApprovalRequest(change_id=change_id, approved=True)))

        assert _run(rest_suggestions.handle_suggestions(None, _request())).open == []

    def test_one_answered_elsewhere_is_shown_as_unanswerable(self, monkeypatch):
        """Read off the JOURNAL, not off the suggestion's state: /rest/approve
        consults the journal, and a change answered in ASI1 thirty seconds ago
        is already decided there. Offering Accept for it would produce a
        refusal the user did not deserve."""
        _, change_id = _a_proposal()
        activity.claim_decision(change_id, "approved")

        response = _run(rest_suggestions.handle_suggestions(None, _request()))

        assert response.open[0]["answerable"] is False


ANALYST_CONFIG = {"alias": "Hall light off at midnight",
                  "trigger": [{"trigger": "time", "at": "00:00:00"}],
                  "condition": [], "action": [{"action": "light.turn_off",
                                               "target": {"entity_id": "light.hall"}}]}


def _an_analyst_proposal(level="untested", claims=()):
    """Proposal §5: a row the retired audited run raised (`checked` /
    `untested`, free-hand F-9), planted as a pre-2.23.0 store holds it, and
    proposed through the gate like any other. Its rendering still matters:
    one applied under it is verified and shown with its label."""
    suggestion_id = suggestions.upsert_analyst_candidate(
        "analyst:3f9a1c", entities=["light.hall"],
        title="Turn the hall light off at midnight",
        rationale="It is left on most nights.", model="asi1-mini")
    assert legacy_rows.plant_audited(suggestion_id, ANALYST_CONFIG, level, claims)
    raised = gate.propose(CREATE, {"config_json": json.dumps(ANALYST_CONFIG)})
    assert raised["ok"] is True
    assert suggestions.mark_proposed(suggestion_id, raised["change_id"])
    return suggestion_id, raised["change_id"]


class TestAnAnalystSuggestionReachesThePanel:
    """WP-B3: the fields the panel lays out for a suggestion no detector
    raised, rendered in core/artefacts.py and passed through unchanged."""

    def test_the_new_fields_arrive_on_an_analyst_card(self):
        _an_analyst_proposal()

        row = _run(rest_suggestions.handle_suggestions(None, _request())).open[0]

        assert row["source"] == "analyst"
        assert row["evidence_level"] == "untested"
        assert row["evidence_label"] == "not tested"
        assert row["warning"].startswith("I couldn't test this against your history")
        assert row["reasoning"] == "It is left on most nights."
        assert row["reasoning_heading"].startswith("Why the agent suggests this")
        assert "light.turn_off" in row["readback_yaml"]
        assert row["basis"] is None
        assert row["headline"] == "Turn the hall light off at midnight (not tested)"

    def test_the_digest_line_carries_the_label(self):
        _an_analyst_proposal(level="checked", claims=[{
            "text": "On past midnight", "type": "on_hours", "params": {},
            "stated": 20, "recounted": 19, "result": "matched"}])

        response = _run(rest_suggestions.handle_suggestions(None, _request()))

        assert response.digest[0]["line"] == \
            "Turn the hall light off at midnight (checked against your history)"
        assert response.open[0]["evidence"] == [
            "On past midnight — recounted from your history: 19"]
        assert response.open[0]["warning"] == ""

    def test_a_detector_card_carries_the_same_keys_with_the_detector_s_values(self):
        _a_proposal()

        row = _run(rest_suggestions.handle_suggestions(None, _request())).open[0]

        assert (row["source"], row["evidence_level"], row["warning"],
                row["reasoning"], row["readback_yaml"]) == \
            ("detector", "replayed", "", "", "")
        assert row["basis"] == "replay"

    def test_the_journal_s_yaml_is_the_one_the_readback_prints(self):
        """Two YAMLs of one config on one card would be two claims about what
        is written. The gate dumps its `new_yaml` the way the readback does."""
        _, change_id = _an_analyst_proposal()

        row = _run(rest_suggestions.handle_suggestions(None, _request())).open[0]

        assert row["readback_yaml"] == activity.change(change_id)["new_config"].strip()

    def test_a_week_later_the_report_says_it_is_the_analyst_s(self):
        suggestion_id, change_id = _an_analyst_proposal()
        activity.record_decision(
            change_id, "approved", status="applied",
            result=json.dumps({"created": True, "automation_id": "1790000000000"}),
            inverse={"op": "delete_automation", "automation_id": "1790000000000"})
        from core import review
        assert review.settle(change_id)
        assert suggestions.record_verdict(suggestion_id, {"outcome": "not_measured"})

        report = _run(rest_suggestions.handle_suggestions(None, _request())).verified[0]

        assert report["source"] == "analyst"
        assert report["basis"] is None
        assert report["result"] == "The agent had nothing it could measure for this change."


DRAWN_CHART = {"spec": {"type": "timeline", "entities": ["light.hall"], "state": None,
                        "start": 1_789_000_000, "end": 1_789_000_000 + 7 * 86400},
               "series": {"days": [{"date": "2026-09-10", "minutes": 1440, "span": [0, 1440],
                                    "entities": {"light.hall": [[0, 400, "off"],
                                                                [400, 1440, "on"]]}}],
                          "truncated": False, "days_dropped": 0},
               "markers": [{"day": "2026-09-10", "minute": 0, "action": "turn_off",
                            "entity": "light.hall"}],
               "markers_note": None, "error": None, "drawn_at": 1_789_700_000,
               "window_clamped": None}
SENT = [{"tool": "Query the behavioural record",
         "query": {"sql": "SELECT count(*) FROM ev WHERE entity_id = 'light.hall'"},
         "answer_excerpt": '{"rows": [[42]]}', "rows": 1, "truncated": False}]


def _an_explorer_idea(chart=None):
    """Free-hand F-WP3/F-WP4: an explorer row as `run_explorer` stores it —
    mark_shown with a drawn chart and what was sent — proposed like any
    other."""
    suggestion_id = suggestions.upsert_analyst_candidate(
        "analyst:9c1e22", entities=["light.hall"], title="Turn the hall light off at midnight",
        rationale="The hall light stays on after midnight.", model="asi1-strong")
    assert suggestions.mark_shown(suggestion_id, config=ANALYST_CONFIG,
                                  finding="The hall light stays on after midnight.",
                                  rule_text="At midnight, turn the hall light off.",
                                  chart=chart or DRAWN_CHART, sent=SENT)
    raised = gate.propose(CREATE, {"config_json": json.dumps(ANALYST_CONFIG)})
    assert raised["ok"] is True
    assert suggestions.mark_proposed(suggestion_id, raised["change_id"])
    return suggestion_id, raised["change_id"]


def _feedback(suggestion_id, answer, reason=""):
    return _run(rest_suggestions.handle_feedback(None, protocols.SuggestionsRequest(
        suggestion_id=suggestion_id, answer=answer, reason=reason)))


def _journal():
    import sqlite3
    conn = sqlite3.connect(activity.DB_PATH)
    try:
        return conn.execute("SELECT * FROM changes ORDER BY id").fetchall()
    finally:
        conn.close()


class TestAnExplorerIdeaReachesThePanel:
    """F-WP5: an explorer idea's entry carries its stored chart and `sent`
    as they are, and the sentences about them from core/artefacts.py."""

    def test_the_chart_and_what_was_sent_arrive_as_stored(self):
        _an_explorer_idea()

        row = _run(rest_suggestions.handle_suggestions(None, _request())).open[0]

        assert row["evidence_level"] == "shown"
        assert row["chart"] == DRAWN_CHART
        assert row["sent"] == SENT
        assert row["feedback"] is None
        assert row["why"] == "The hall light stays on after midnight."
        assert row["reasoning"] == "At midnight, turn the hall light off."
        assert row["chart_error"] == ""
        assert row["chart_caption"].startswith("Timeline of light.hall, ")
        assert row["chart_markers"].startswith("The markers show the 1 time ")
        assert row["sent_heading"] == "What was sent to the model for this"
        # No registry read yet in this process: each id is its own name.
        assert row["chart_names"] == {e: e for e in DRAWN_CHART["spec"]["entities"]}

    def test_the_charted_entities_are_named_from_the_record_tab_s_cache(self, monkeypatch):
        _an_explorer_idea()
        monkeypatch.setattr(rest_suggestions.observation, "name_for",
                            lambda e: {"light.hall": "Hall light"}.get(e, e))

        row = _run(rest_suggestions.handle_suggestions(None, _request())).open[0]

        assert row["chart_names"]["light.hall"] == "Hall light"
        assert row["chart_caption"].startswith("Timeline of Hall light, ")

    def test_a_chart_the_record_could_not_draw_is_the_sentence(self):
        _an_explorer_idea(chart={"spec": {"type": "timeline", "entities": ["light.porch"]},
                                 "series": None, "markers": None, "markers_note": None,
                                 "error": "light.porch is not watched, so the record "
                                          "holds nothing to draw",
                                 "drawn_at": 1_789_700_000, "window_clamped": None})

        row = _run(rest_suggestions.handle_suggestions(None, _request())).open[0]

        assert row["chart_error"] == (
            "The agent asked for a chart the record could not draw: light.porch is not "
            "watched, so the record holds nothing to draw.")
        assert row["chart_caption"] == row["chart_markers"] == ""
        assert row["chart"]["error"]

    def test_detector_and_audited_entries_are_unchanged(self):
        _a_proposal()
        _an_analyst_proposal()

        rows = _run(rest_suggestions.handle_suggestions(None, _request())).open

        added = {"chart", "sent", "feedback", "chart_error", "chart_caption",
                 "chart_clamped", "chart_markers", "chart_truncated", "sent_heading",
                 "sent_note", "feedback_line", "chart_names"}
        assert len(rows) == 2
        for row in rows:
            assert not added & set(row), row["source"]


class TestTheFeedbackOnlyRecords:
    """F-11 from the panel: POST /rest/suggestions/feedback stores {answer,
    reason, at} and nothing else. The change is decided by /rest/approve,
    which the panel calls next with the row's change_id."""

    def test_the_round_trip(self):
        suggestion_id, _ = _an_explorer_idea()

        response = _feedback(suggestion_id, "no", "the dog sleeps in the hall")

        stored = suggestions.suggestion(suggestion_id)["feedback"]
        assert (stored["answer"], stored["reason"]) == ("no", "the dog sleeps in the hall")
        assert response.error == ""
        listed = response.open[0]
        assert listed["feedback"] == stored
        assert listed["feedback_line"].endswith(", because: the dog sleeps in the hall")
        again = _run(rest_suggestions.handle_suggestions(None, _request())).open[0]
        assert again["feedback"] == stored

    @pytest.mark.parametrize("answer", ["yes", "no"])
    def test_it_never_touches_the_journal_or_the_suggestion_s_state(self, answer, monkeypatch):
        suggestion_id, change_id = _an_explorer_idea()
        journal = _journal()
        before = suggestions.suggestion(suggestion_id)

        def _boom(*args, **kwargs):
            raise AssertionError("the feedback endpoint reached a write path")

        from core import review, rollback, verify
        for module, name in ((gate, "apply"), (gate, "pending_for"), (gate, "propose"),
                             (rollback, "roll_back"), (verify, "answer"),
                             (review, "settle"), (activity, "record_decision"),
                             (activity, "claim_decision")):
            monkeypatch.setattr(module, name, _boom)

        response = _feedback(suggestion_id, answer, "because")

        after = suggestions.suggestion(suggestion_id)
        assert response.error == ""
        assert _journal() == journal
        assert activity.change(change_id)["decision"] == "proposed"
        assert (after["state"], after["updated_at"], after["change_id"]) == \
            (before["state"], before["updated_at"], before["change_id"]) == \
            ("proposed", before["updated_at"], change_id)
        assert response.open[0]["answerable"] is True

    def test_a_refused_answer_is_said_and_stores_nothing(self):
        suggestion_id, _ = _an_explorer_idea()

        response = _feedback(suggestion_id, "rolled_back")

        assert "could not record" in response.error
        assert suggestions.suggestion(suggestion_id)["feedback"] is None

    def test_a_detector_row_takes_no_feedback(self):
        suggestion_id, _ = _a_proposal()

        assert _feedback(suggestion_id, "no").error
        assert suggestions.suggestion(suggestion_id).get("feedback") is None

    def test_yes_then_approve_is_the_existing_path(self, monkeypatch):
        """What the panel's Yes does: record, then /rest/approve with the
        change_id — the gate applies, the suggestion settles."""
        suggestion_id, change_id = _an_explorer_idea()
        calls = []

        async def _fake(tool_name, tool_args):
            calls.append(tool_name)
            return json.dumps({"created": True, "automation_id": "1790000000001"})

        monkeypatch.setattr(rest_chat, "_execute_tool_async", _fake)

        listed = _feedback(suggestion_id, "yes")
        _run(rest_chat.handle_approve(
            None, ApprovalRequest(change_id=listed.open[0]["change_id"], approved=True)))

        assert calls == [CREATE]
        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "applied"
        assert row["feedback"]["answer"] == "yes"


@pytest.mark.skipif(
    REAL_DEPS,
    reason="_rest_routes is bookkeeping conftest's Agent stub adds for introspection — "
           "the real uagents Agent does not expose it")
class TestTheRouteIsRegistered:
    def test_the_panel_s_endpoint_exists(self):
        from core.uagent import agent_1

        assert agent_1._rest_routes["/rest/suggestions"] is rest_suggestions.handle_suggestions

    def test_the_feedback_endpoint_exists(self):
        from core.uagent import agent_1

        assert agent_1._rest_routes["/rest/suggestions/feedback"] is \
            rest_suggestions.handle_feedback
