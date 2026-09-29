"""Answering a suggestion by saying so: the path from "what have you found?"
to an automation written into a home, and everything it must refuse on the way.

Design §4 makes ASI1 the primary interface and §9 makes the plain-text
rendering the constraint rather than the fallback, so this is the surface the
whole suggestion pipeline is finally judged on. Before WP8 core/review.py could
raise a proposal through `gate.propose` and NOTHING could answer one: an ASI1
user had nothing parked in session for a "yes" to match.

The line that runs through all of it is core/rollback.py decision 4, applied to
a change that is NOT pre-authorised. The model picks WHICH suggestion to put
up; it must never be able to answer one. So the tool returns a question, the
loop recognises it in the RESULT, the session holds it in its own slot, and the
user's own words decide — read by parse_approval_decision, which does not use
the LLM.

The other half is where the answer goes. It leaves this file's path entirely
and lands on /rest/approve's change_id branch, which is what makes a chat "no"
the same thirty-day cooldown a panel "Dismiss" is: both reach review.settle.

WP9 puts the same shape a second time, for the question that comes a week
later. §7.2's measured result is answered in one word too, and the same rules
hold — a question in the RESULT, its own session slot, no LLM in the decision.
What differs is where the answer goes and why: keeping a change writes nothing
at all, and rolling one back runs the inverse core/gate.py computed when it was
approved, through core/rollback.py. Neither is re-gated, because the operation
was pre-authorised and only the selection was ever in question — and here the
suggestion row names the change, so nobody is selecting.
"""
import asyncio
import json

import pytest

import conftest
from core import activity
from core import agent_runtime as rt
from core import artefacts
from core import gate
from core import protocols
from core import review
from core import rollback
from core import suggestions
from rest import rest_chat
from rest.rest_chat import ApprovalRequest
from tools.tool_homeassistant import automations, registry

REAL_DEPS = not conftest.STUBBED

CREATE = "Create Home Assistant automation"
CONFIG = {"alias": "Living room TV off overnight", "trigger": [], "condition": [], "action": []}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    activity.close()
    suggestions.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    rt.session_memory_store.clear()
    yield
    suggestions.close()
    activity.close()
    rt.session_memory_store.clear()


@pytest.fixture(autouse=True)
def offline_ha(monkeypatch):
    """A Home Assistant that validates configs and answers nothing else.

    `fetch_registry` is refused on purpose: review.settle measures a baseline
    at apply time and reads the sensor's unit from it, and a baseline that
    says it could not be measured is still a baseline. That is the case worth
    holding here — the write has already landed in someone's home.
    """
    monkeypatch.setattr(gate, "_ha_get", lambda path: (True, {}))
    monkeypatch.setattr(automations, "_validate_by_test_create", lambda cfg: (True, "1"))
    monkeypatch.setattr(registry, "fetch_registry", lambda: (False, "no HA in tests"))


class _AI:
    """Stands in for a langchain AIMessage; only ever stored."""

    def __init__(self, content=""):
        self.content = content


def _run(coro):
    return asyncio.run(coro)


async def _returns(value):
    """A canned value as a coroutine, for the async functions rest_chat
    awaits."""
    return value


def _post(text, session_id="session_panel"):
    return _run(rest_chat.handle_post(
        None, protocols.Request(text=text, session_id=session_id)))


def _a_proposal():
    """A suggestion that has been through the whole pipeline and is waiting.

    Built with the store's own API rather than by inserting rows, so the state
    machine has to allow every step — a suggestion that could not legally reach
    `proposed` is not a fixture, it is a bug being tested around.
    """
    suggestion_id = suggestions.upsert_candidate(
        "standby:sensor.tv_power", "standby",
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


def _agent_shows_it(monkeypatch, suggestion_id):
    """Makes the agent loop answer with what the show tool actually returns."""
    from tools import tool_suggestions

    payload = tool_suggestions.tool_show_suggestion()._run(suggestion_id=suggestion_id)

    async def _fake(*args, **kwargs):
        return payload, _AI("asked")

    monkeypatch.setattr(rest_chat, "_run_agent", _fake)


def _no_writes(monkeypatch):
    """A tool executor nothing on this path is allowed to reach."""
    async def _boom(*args, **kwargs):
        raise AssertionError("an automation was written without the user answering")

    monkeypatch.setattr(rest_chat, "_execute_tool_async", _boom)
    return _boom


def _writes(monkeypatch):
    """A tool executor that records what the gate asked it to run."""
    calls = []

    async def _fake(tool_name, tool_args):
        calls.append((tool_name, tool_args))
        return json.dumps({"created": True, "automation_id": "1748000000000"})

    monkeypatch.setattr(rest_chat, "_execute_tool_async", _fake)
    return calls


class TestTheQuestionIsHeld:
    def test_showing_one_parks_it_and_prints_the_whole_artefact(self, monkeypatch):
        suggestion_id, change_id = _a_proposal()
        _no_writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)

        response = _post("what have you found?")

        # §9 artefacts 2 and 3, in one message, answerable in one word.
        assert "never dropped below" in response.text
        assert "Estimated saving" in response.text
        assert "Reply YES to apply this, or NO to dismiss it." in response.text
        parked = rt.get_memory("session_panel")["pending_suggestion"]
        assert (parked["change_id"], parked["suggestion_id"]) == (change_id, suggestion_id)

    def test_it_is_parked_where_the_approval_gate_cannot_reach_it(self, monkeypatch):
        """`pending_approval` is answered by resuming a conversation through
        gate.apply. There is no conversation here, and inventing one would put
        words in the user's mouth to justify a write."""
        suggestion_id, _ = _a_proposal()
        _no_writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)

        _post("what have you found?")

        assert rt.get_memory("session_panel")["pending_approval"] is None

    def test_the_session_approval_endpoint_cannot_answer_it(self, monkeypatch):
        suggestion_id, change_id = _a_proposal()
        _no_writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)
        _post("what have you found?")

        response = _run(rest_chat.handle_approve(
            None, ApprovalRequest(session_id="session_panel", approved=True)))

        assert "No pending approval" in response.text
        assert activity.change(change_id)["decision"] == "proposed"

    def test_showing_it_writes_nothing_new_to_the_journal(self, monkeypatch):
        """The row already exists — gate.propose wrote the whole question to it
        when the review raised it. Showing it again is not a second proposal."""
        suggestion_id, change_id = _a_proposal()
        _no_writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)

        _post("what have you found?")
        _post("tell me again")

        assert [c["id"] for c in activity.changes()] == [change_id]

    def test_the_tool_is_not_gated_and_must_not_be(self, monkeypatch):
        """It writes nothing, so gating it would put an approval in front of a
        question — and DESTRUCTIVE_TOOLS is what routes a call to gate.apply."""
        assert "Show an agent suggestion" not in rt.DESTRUCTIVE_TOOLS
        assert "List agent suggestions" not in rt.DESTRUCTIVE_TOOLS

    def test_the_model_has_no_argument_that_could_approve_one(self):
        """The confirmation is never an argument. A tool result is built by
        this process and cannot be forged; an argument is written by the model,
        so a `confirmed` flag would be the model asserting that a question it
        never asked was answered."""
        import inspect

        from tools import tool_suggestions

        taken = set(inspect.signature(tool_suggestions.tool_show_suggestion()._run).parameters)
        assert taken == {"suggestion_id"}


class TestAnsweringIt:
    def test_yes_writes_it_through_the_gate(self, monkeypatch):
        suggestion_id, change_id = _a_proposal()
        calls = _writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)
        _post("what have you found?")

        response = _post("yes")

        assert calls == [(CREATE, {"config_json": json.dumps(CONFIG)})]
        row = activity.change(change_id)
        assert (row["decision"], row["status"]) == ("approved", "applied")
        # §9: echoed back with what was actually done, with no model in it.
        assert "Done" in response.text

    def test_yes_settles_the_suggestion_with_a_baseline_to_measure_against(self, monkeypatch):
        """The seam review.settle closes. Without it WP9 has nothing to compare
        a week later and the row sits at `proposed` against a settled question."""
        suggestion_id, _ = _a_proposal()
        _writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)
        _post("what have you found?")

        _post("yes")

        applied = suggestions.suggestion(suggestion_id)
        assert applied["state"] == "applied"
        assert applied["verify_after"] > activity._now()
        assert applied["baseline"]["measured_at"]

    def test_no_writes_nothing_and_becomes_a_cooldown_rather_than_a_verdict(self, monkeypatch):
        """The rule the brief is most explicit about: a chat "no" must reach
        review.settle, or it is a rejection that silences a real problem
        forever instead of holding it for thirty days."""
        suggestion_id, change_id = _a_proposal()
        _no_writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)
        _post("what have you found?")

        response = _post("no")

        assert activity.change(change_id)["decision"] == "rejected"
        dismissed = suggestions.suggestion(suggestion_id)
        assert dismissed["state"] == "dismissed"
        assert dismissed["dismissed_until"] > activity._now()
        assert "Nothing was changed" in response.text

    def test_a_chat_no_and_a_panel_no_settle_identically(self, monkeypatch):
        """Same endpoint, same call, so this is the assertion that the two
        surfaces have not been allowed to diverge."""
        chat_id, chat_change = _a_proposal()
        _no_writes(monkeypatch)
        _agent_shows_it(monkeypatch, chat_id)
        _post("what have you found?")
        _post("no")
        chat_row = suggestions.suggestion(chat_id)

        suggestions.retire(chat_id, "making room for the second fixture")
        panel_id, panel_change = _a_proposal()
        _run(rest_chat.handle_approve(
            None, ApprovalRequest(change_id=panel_change, approved=False)))
        panel_row = suggestions.suggestion(panel_id)

        assert chat_row["state"] == panel_row["state"] == "dismissed"
        assert (activity.change(chat_change)["decision"]
                == activity.change(panel_change)["decision"] == "rejected")

    def test_a_non_answer_re_asks_rather_than_deciding(self, monkeypatch):
        """"no, not that one" is a correction. parse_approval_decision matches
        the whole normalised message for exactly this reason."""
        suggestion_id, change_id = _a_proposal()
        _no_writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)
        _post("what have you found?")

        response = _post("no, not that one")

        assert "Nothing has been changed" in response.text
        assert "Estimated saving" in response.text
        assert activity.change(change_id)["decision"] == "proposed"
        assert rt.get_memory("session_panel")["pending_suggestion"] is not None

    def test_a_non_answer_never_reaches_the_model_either(self, monkeypatch):
        suggestion_id, _ = _a_proposal()
        _no_writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)
        _post("what have you found?")

        def _never(*args, **kwargs):
            raise AssertionError("the held message was sent to the agent loop")

        monkeypatch.setattr(rest_chat, "_run_agent", _never)
        _post("how much would that save again")

    def test_it_is_answered_exactly_once(self, monkeypatch):
        suggestion_id, change_id = _a_proposal()
        _writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)
        _post("what have you found?")
        _post("yes")

        async def _quiet(*args, **kwargs):
            return "nothing to do", _AI("")

        monkeypatch.setattr(rest_chat, "_run_agent", _quiet)
        _no_writes(monkeypatch)

        _post("yes")

        assert suggestions.suggestion(suggestion_id)["state"] == "applied"


class TestWhenTheAnswerCannotBeCarriedOut:
    def test_a_failure_reaches_the_user_as_a_sentence(self, monkeypatch):
        """The answer is settled before the gate is reached, so an exception
        escaping here would lose the question AND the only message that could
        say so. Everything else in handle_post becomes a sentence; so does
        this."""
        suggestion_id, change_id = _a_proposal()
        _agent_shows_it(monkeypatch, suggestion_id)
        _no_writes(monkeypatch)
        _post("what have you found?")

        async def _explode(*args, **kwargs):
            raise RuntimeError("Home Assistant fell over")

        monkeypatch.setattr(rest_chat, "_approve_change", _explode)

        response = _post("yes")

        assert "not known whether anything was written" in response.text
        assert "Home Assistant fell over" in response.text

    def test_the_proposal_is_left_open_when_that_happens(self, monkeypatch):
        suggestion_id, change_id = _a_proposal()
        _agent_shows_it(monkeypatch, suggestion_id)
        _no_writes(monkeypatch)
        _post("what have you found?")

        async def _explode(*args, **kwargs):
            raise RuntimeError("Home Assistant fell over")

        monkeypatch.setattr(rest_chat, "_approve_change", _explode)
        _post("yes")

        assert activity.change(change_id)["decision"] == "proposed"
        assert suggestions.suggestion(suggestion_id)["state"] == "proposed"


class TestWhenNobodyAnswers:
    def test_an_expired_question_changes_nothing_and_says_so(self, monkeypatch):
        suggestion_id, change_id = _a_proposal()
        _no_writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)
        _post("what have you found?")
        memory = rt.get_memory("session_panel")
        memory["pending_suggestion"]["created_at"] -= artefacts.QUESTION_TTL_SECONDS + 1

        async def _quiet(*args, **kwargs):
            return "the time is 3pm", _AI("")

        monkeypatch.setattr(rest_chat, "_run_agent", _quiet)
        response = _post("yes")

        assert "expired" in response.text
        assert activity.change(change_id)["decision"] == "proposed"

    def test_the_suggestion_itself_stays_open_and_answerable(self, monkeypatch):
        """The proposal is MEANT to wait — gate.pending_for gives it no TTL.
        What expires is one conversation's copy of the question."""
        suggestion_id, change_id = _a_proposal()
        _agent_shows_it(monkeypatch, suggestion_id)
        _no_writes(monkeypatch)
        _post("what have you found?")
        memory = rt.get_memory("session_panel")
        memory["pending_suggestion"]["created_at"] -= artefacts.QUESTION_TTL_SECONDS + 1

        async def _quiet(*args, **kwargs):
            return "the time is 3pm", _AI("")

        monkeypatch.setattr(rest_chat, "_run_agent", _quiet)
        _post("yes")

        assert suggestions.suggestion(suggestion_id)["state"] == "proposed"
        assert suggestions.proposed()[0]["id"] == suggestion_id
        calls = _writes(monkeypatch)
        _run(rest_chat.handle_approve(
            None, ApprovalRequest(change_id=change_id, approved=True)))
        assert len(calls) == 1


# --------------------------------------------------------------------------
# a week later: the same pattern, for a change that IS pre-authorised
# --------------------------------------------------------------------------
def _a_report(monkeypatch):
    """A suggestion taken all the way to a delivered verification report.

    Through the store's own API again, so every step has to be one the state
    machine allows — `verified` is the claim that a measurement was taken, and
    a fixture that could reach it illegally would be testing around the thing
    that keeps it meaningful.
    """
    suggestion_id, change_id = _a_proposal()
    calls = _writes(monkeypatch)
    _run(rest_chat.handle_approve(None, ApprovalRequest(change_id=change_id, approved=True)))
    assert calls
    assert suggestions.suggestion(suggestion_id)["state"] == "applied"
    assert suggestions.record_verdict(suggestion_id, {
        "outcome": "saved", "reason": "", "attempts": 1, "verified_at": activity._now(),
        "before": {"window": {"from": "2026-09-01", "to": "2026-09-08", "days": 7},
                   "night_window": "01:00-05:00 local", "hours_per_day": 4,
                   "watts": 12.0, "watts_range": [11.0, 13.0], "nights": 7,
                   "nights_observed": 7, "nights_examined": 7, "excluded": {}},
        "after": {"window": {"from": "2026-09-08", "to": "2026-09-15", "days": 7},
                  "night_window": "01:00-05:00 local", "hours_per_day": 4,
                  "watts": None, "watts_range": None, "nights": 0,
                  "nights_observed": 7, "nights_examined": 7,
                  "excluded": {"below_floor": 7}},
        "floor_kwh_per_month": 0.12, "saved_kwh_per_month": 1.44,
        "saved_range_kwh_per_month": [1.2, 1.68], "saved_watts": 12.0,
        "estimate_kwh_per_month": 1.01, "estimate_basis": "replay",
    }, state="verified")
    return suggestion_id, change_id


def _agent_reports_it(monkeypatch, suggestion_id):
    """Makes the agent loop answer with what the report tool actually
    returns."""
    from tools import tool_suggestions

    payload = tool_suggestions.tool_show_verification()._run(suggestion_id=suggestion_id)

    async def _fake(*args, **kwargs):
        return payload, _AI("asked")

    monkeypatch.setattr(rest_chat, "_run_agent", _fake)


class TestTheMeasuredResultIsPutTheSameWay:
    """§7.2's report is answered in one word too, and by a fourth code path."""

    def test_showing_one_parks_it_and_prints_the_whole_report(self, monkeypatch):
        suggestion_id, change_id = _a_report(monkeypatch)
        _agent_reports_it(monkeypatch, suggestion_id)

        response = _post("did that TV change actually work?")

        assert "between 1.2 and 1.68 kWh a month" in response.text
        assert "2026-09-15" in response.text and "2026-09-08" in response.text
        assert artefacts.VERDICT_PROMPT in response.text
        assert rt.get_memory("session_panel")["pending_verdict"]["change_id"] == change_id

    def test_it_is_parked_where_none_of_the_other_three_can_reach_it(self, monkeypatch):
        suggestion_id, _ = _a_report(monkeypatch)
        _agent_reports_it(monkeypatch, suggestion_id)

        _post("did that work?")

        memory = rt.get_memory("session_panel")
        assert memory["pending_verdict"] is not None
        assert memory["pending_approval"] is None
        assert memory["pending_suggestion"] is None
        assert memory["pending_rollback"] is None

    def test_the_model_has_no_argument_that_could_answer_one(self):
        """A result is built by this process; an argument is written by the
        model. "The user said keep it" is a claim it must not be able to
        make."""
        import inspect

        from tools import tool_suggestions

        taken = set(inspect.signature(
            tool_suggestions.tool_show_verification()._run).parameters)

        assert taken == {"suggestion_id"}
        assert "Show a measured result" not in gate.DESTRUCTIVE_TOOLS

    def test_showing_it_undoes_nothing(self, monkeypatch):
        suggestion_id, _ = _a_report(monkeypatch)

        def _boom(*args, **kwargs):
            raise AssertionError("showing a report rolled something back")

        monkeypatch.setattr(rollback, "roll_back", _boom)
        _agent_reports_it(monkeypatch, suggestion_id)
        _post("did that work?")

        assert suggestions.suggestion(suggestion_id)["state"] == "verified"


class TestAnsweringTheReport:
    def test_yes_keeps_it_and_changes_nothing_in_the_home(self, monkeypatch):
        suggestion_id, _ = _a_report(monkeypatch)
        _agent_reports_it(monkeypatch, suggestion_id)
        _post("how did that go?")

        def _boom(*args, **kwargs):
            raise AssertionError("keeping a change rolled something back")

        monkeypatch.setattr(rollback, "roll_back", _boom)
        response = _post("yes")

        assert suggestions.suggestion(suggestion_id)["state"] == "kept"
        assert "Kept" in response.text
        assert rt.get_memory("session_panel")["pending_verdict"] is None

    def test_no_runs_the_inverse_the_gate_recorded_and_nothing_else(self, monkeypatch):
        """Not re-gated and not a second rollback path: the operation was
        pre-authorised when the change was approved, and only the selection was
        ever in question — and here nobody is selecting."""
        suggestion_id, change_id = _a_report(monkeypatch)
        deleted = []
        monkeypatch.setattr(
            rollback, "delete_automation",
            lambda automation_id: (deleted.append(automation_id), (True, ""))[1])

        def _boom(*args, **kwargs):
            raise AssertionError("a verification answer reached the approval gate")

        monkeypatch.setattr(gate, "apply", _boom)
        _agent_reports_it(monkeypatch, suggestion_id)
        _post("how did that go?")

        response = _post("no")

        assert deleted == ["1748000000000"]
        assert suggestions.suggestion(suggestion_id)["state"] == "rolled_back"
        assert f"#{change_id}" in response.text

    def test_a_chat_answer_and_a_panel_click_settle_identically(self, monkeypatch):
        """One exit for both surfaces, the way /rest/approve is one exit for
        Accept and for "yes"."""
        from rest import rest_suggestions
        from core import verify

        suggestion_id, _ = _a_report(monkeypatch)
        monkeypatch.setattr(rollback, "delete_automation", lambda aid: (True, ""))

        _run(rest_suggestions.handle_verdict(
            None, protocols.SuggestionsRequest(suggestion_id=suggestion_id, keep=False)))

        assert suggestions.suggestion(suggestion_id)["state"] == "rolled_back"
        assert verify.answer.__module__ == "core.verify"

    def test_a_non_answer_re_asks_rather_than_deciding(self, monkeypatch):
        """"no, the other one" is a correction. parse_approval_decision matches
        the whole normalised message and never uses the LLM."""
        suggestion_id, _ = _a_report(monkeypatch)
        _agent_reports_it(monkeypatch, suggestion_id)
        _post("how did that go?")

        def _never(*args, **kwargs):
            raise AssertionError("a non-answer reached the model")

        monkeypatch.setattr(rest_chat, "_run_agent", _never)
        response = _post("no, I meant the kitchen one")

        assert "Nothing has been changed" in response.text
        assert suggestions.suggestion(suggestion_id)["state"] == "verified"
        assert rt.get_memory("session_panel")["pending_verdict"] is not None

    def test_a_failed_rollback_reaches_the_user_verbatim(self, monkeypatch):
        """§11: explicit and actionable, never summarised into "that did not
        work"."""
        suggestion_id, _ = _a_report(monkeypatch)
        monkeypatch.setattr(rollback, "delete_automation",
                            lambda aid: (False, "HA refused: 404"))
        _agent_reports_it(monkeypatch, suggestion_id)
        _post("how did that go?")

        response = _post("no")

        assert "HA refused: 404" in response.text
        assert suggestions.suggestion(suggestion_id)["state"] == "verified"

    def test_an_expired_question_changes_nothing_and_leaves_the_report_open(
            self, monkeypatch):
        suggestion_id, _ = _a_report(monkeypatch)
        _agent_reports_it(monkeypatch, suggestion_id)
        _post("how did that go?")
        rt.get_memory("session_panel")["pending_verdict"]["created_at"] -= (
            artefacts.QUESTION_TTL_SECONDS + 1)

        async def _plain(*args, **kwargs):
            return "Nothing else to say.", _AI("ok")

        monkeypatch.setattr(rest_chat, "_run_agent", _plain)
        response = _post("yes")

        assert "expired" in response.text
        assert suggestions.suggestion(suggestion_id)["state"] == "verified"


class TestOnlyOneQuestionAtATime:
    """§9's one-word answer only works while the question is the one in front of
    the user, so two outstanding questions would make YES mean whichever slot
    the handler happened to check first.

    Two mechanisms hold it, and they are different: while a question is parked,
    handle_post answers it rather than passing the message on — so nothing new
    can be asked in a conversation. `_only_question` covers the one route that
    goes round that, /rest/approve, which the panel calls directly.
    """

    def test_a_held_question_stops_anything_else_being_asked(self, monkeypatch):
        suggestion_id, _ = _a_proposal()
        _no_writes(monkeypatch)
        _agent_shows_it(monkeypatch, suggestion_id)
        _post("what have you found?")

        def _never(*args, **kwargs):
            raise AssertionError("a second question was raised while one was held")

        monkeypatch.setattr(rest_chat, "_run_agent", _never)
        response = _post("actually, undo the last change")

        memory = rt.get_memory("session_panel")
        assert memory["pending_rollback"] is None
        assert memory["pending_suggestion"] is not None
        assert "Nothing has been changed" in response.text

    def test_a_report_raised_by_answering_an_approval_withdraws_the_others(self, monkeypatch):
        """Four slots now, and a YES meant for one of them must never be read
        by another. Through /rest/approve because that is the one route round
        the rule above — the panel calls it directly, and the resumed loop can
        raise a question of its own."""
        suggestion_id, _ = _a_report(monkeypatch)
        memory = rt.get_memory("session_panel")
        memory["pending_approval"] = {
            "tool_name": "Delete Home Assistant automation",
            "tool_args": {"automation_id": "1748"},
            "tool_call_id": "call_abc",
            "messages_snapshot": [],
            "ai_message": _AI("asked"),
            "created_at": activity._now(),
            "change_id": None,
            "reversibility": "one_way",
        }
        memory["pending_suggestion"] = {"change_id": 999, "question": "apply?",
                                        "created_at": activity._now()}
        memory["pending_rollback"] = {"change_id": 998, "question": "undo?",
                                      "created_at": activity._now()}

        from tools import tool_suggestions
        asked = tool_suggestions.tool_show_verification()._run(suggestion_id=suggestion_id)

        async def _resume(*args, **kwargs):
            return asked, _AI("asked")

        monkeypatch.setattr(rest_chat, "_run_agent_from_history", _resume)
        monkeypatch.setattr(rest_chat, "_execute_tool_async",
                            lambda name, args: _returns(json.dumps({"deleted": True})))

        _run(rest_chat.handle_approve(
            None, ApprovalRequest(session_id="session_panel", approved=True)))

        assert memory["pending_suggestion"] is None
        assert memory["pending_rollback"] is None
        assert memory["pending_verdict"]["suggestion_id"] == suggestion_id

    def test_a_question_raised_by_answering_an_approval_withdraws_the_other(self, monkeypatch):
        """The route that goes round the rule above: /rest/approve is called by
        the panel directly, does not consult the parked questions, and can end
        up raising one of its own from the resumed loop."""
        suggestion_id, change_id = _a_proposal()
        _no_writes(monkeypatch)
        memory = rt.get_memory("session_panel")
        memory["pending_approval"] = {
            "tool_name": "Delete Home Assistant automation",
            "tool_args": {"automation_id": "1748"},
            "tool_call_id": "call_abc",
            "messages_snapshot": [],
            "ai_message": _AI("asked"),
            "created_at": activity._now(),
            "change_id": None,
            "reversibility": "one_way",
        }
        memory["pending_rollback"] = {"change_id": 999, "question": "undo?",
                                      "created_at": activity._now()}

        from tools import tool_suggestions
        asked = tool_suggestions.tool_show_suggestion()._run(suggestion_id=suggestion_id)

        async def _resume(*args, **kwargs):
            return asked, _AI("asked")

        monkeypatch.setattr(rest_chat, "_run_agent_from_history", _resume)
        monkeypatch.setattr(rest_chat, "_execute_tool_async",
                            lambda name, args: _returns(json.dumps({"deleted": True})))

        _run(rest_chat.handle_approve(
            None, ApprovalRequest(session_id="session_panel", approved=True)))

        assert memory["pending_rollback"] is None
        assert memory["pending_suggestion"]["change_id"] == change_id
