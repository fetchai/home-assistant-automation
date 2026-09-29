"""Undoing something by saying so: the path from "undo that" to a rolled-back
automation, and everything it must refuse to do on the way.

Design §4 makes ASI1 the primary interface and §9 makes the plain-text
rendering the constraint rather than the fallback, so this is the surface the
rollback feature is actually judged on. What is held here is the seam
test_tool_changes.py cannot reach: the tool asks a question, and something has
to hold it, read the user's own answer, and run exactly one rollback.

The line that matters most runs through all of it — core/rollback.py decision 4.
The operation is pre-authorised, so nothing on this path may touch the approval
gate; the SELECTION was made by a model reading fuzzy text, so nothing on this
path may act on it until the user has answered in their own words.
"""
import asyncio
import json
import pathlib

import pytest

import conftest
from core import activity
from core import agent_runtime as rt
from core import gate
from core import protocols
from core import rollback
from rest import rest_chat
from rest.rest_chat import ApprovalRequest

REAL_DEPS = not conftest.STUBBED


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    rt.session_memory_store.clear()
    yield
    activity.close()
    rt.session_memory_store.clear()


OLD = {"alias": "Evening lights", "trigger": [], "action": []}


class _AI:
    """Stands in for a langchain AIMessage; only ever stored."""

    def __init__(self, content=""):
        self.content = content


def _run(coro):
    return asyncio.run(coro)


def _post(text, session_id="session_panel"):
    return _run(rest_chat.handle_post(
        None, protocols.Request(text=text, session_id=session_id)))


def _applied(target="1748"):
    change_id = activity.record_proposal(
        tool="Update Home Assistant automation", args={"automation_id": target},
        session_id="session_panel", target=target,
        inverse={"op": "update_automation", "automation_id": target, "config": OLD},
        reversibility="reversible")
    activity.record_decision(change_id, "approved", status="applied", result="{}")
    return change_id


def _agent_asks_to_roll_back(monkeypatch, change_id):
    """Makes the agent loop answer with the confirmation payload the rollback
    tool produces — which is what the real loop returns for it."""
    payload = json.dumps(rollback.prepare(change_id, summary="update automation 1748"))

    async def _fake(*args, **kwargs):
        return payload, _AI("asked")

    monkeypatch.setattr(rest_chat, "_run_agent", _fake)


@pytest.fixture
def ha(monkeypatch):
    """A Home Assistant that accepts every write, and counts them."""
    calls = []
    monkeypatch.setattr(rollback, "update_automation",
                        lambda aid, cfg: (calls.append((aid, cfg)), (True, ""))[1])
    monkeypatch.setattr(rollback, "read_automation", lambda aid: (True, {"alias": "now"}))
    return calls


@pytest.fixture
def no_ha(monkeypatch):
    """A Home Assistant nothing is allowed to reach."""
    def _boom(*args, **kwargs):
        raise AssertionError("something rolled back without the user answering")

    for name in ("create_automation", "update_automation", "delete_automation"):
        monkeypatch.setattr(rollback, name, _boom)


class TestTheQuestionIsHeld:
    def test_asking_parks_it_and_shows_the_question(self, monkeypatch, no_ha):
        change_id = _applied()
        _agent_asks_to_roll_back(monkeypatch, change_id)

        response = _post("undo that")

        assert f"#{change_id}" in response.text
        assert "Reply YES to confirm or NO to cancel" in response.text
        assert rt.get_memory("session_panel")["pending_rollback"]["change_id"] == change_id

    def test_it_is_parked_where_the_approval_gate_cannot_reach_it(self, monkeypatch, no_ha):
        """Decision 4 is only true if the two slots stay separate: whatever is
        parked here must never be answerable by gate.apply."""
        _agent_asks_to_roll_back(monkeypatch, _applied())

        _post("undo that")

        assert rt.get_memory("session_panel")["pending_approval"] is None

    def test_approving_nothing_is_still_nothing(self, monkeypatch, no_ha):
        """/rest/approve is the panel's Accept button for a DESTRUCTIVE tool. A
        parked rollback question must not be answerable by it."""
        _agent_asks_to_roll_back(monkeypatch, _applied())
        _post("undo that")

        response = _run(rest_chat.handle_approve(
            None, ApprovalRequest(session_id="session_panel", approved=True)))

        assert "No pending approval" in response.text
        assert rt.get_memory("session_panel")["pending_rollback"] is not None

    def test_asking_writes_nothing_to_the_journal(self, monkeypatch, no_ha):
        change_id = _applied()
        _agent_asks_to_roll_back(monkeypatch, change_id)

        _post("undo that")

        assert [c["id"] for c in activity.changes()] == [change_id]


class TestAnsweringIt:
    def test_yes_rolls_it_back(self, monkeypatch, ha):
        change_id = _applied()
        _agent_asks_to_roll_back(monkeypatch, change_id)
        _post("undo that")

        response = _post("yes")

        assert ha == [("1748", OLD)]
        assert activity.change(change_id)["undone_by"] is not None
        assert f"Change #{change_id} has been rolled back" in response.text

    def test_no_leaves_it_alone_and_says_so(self, monkeypatch, no_ha):
        change_id = _applied()
        _agent_asks_to_roll_back(monkeypatch, change_id)
        _post("undo that")

        response = _post("no")

        assert activity.change(change_id)["undone_by"] is None
        assert "was not rolled back" in response.text

    def test_a_non_answer_re_asks_rather_than_deciding(self, monkeypatch, no_ha):
        """"no, the other one" is a correction. parse_approval_decision matches
        the whole message for exactly this reason, and the question is repeated
        whole because a one-word reply has to answer something still visible."""
        change_id = _applied()
        _agent_asks_to_roll_back(monkeypatch, change_id)
        _post("undo that")

        response = _post("no, the other one")

        assert "Nothing has been changed" in response.text
        assert f"#{change_id}" in response.text
        assert rt.get_memory("session_panel")["pending_rollback"] is not None

    def test_a_non_answer_never_reaches_the_model_either(self, monkeypatch, no_ha):
        """Holding it means holding it: the message is not passed on as a fresh
        request while the question is still open."""
        _agent_asks_to_roll_back(monkeypatch, _applied())
        _post("undo that")

        def _never(*args, **kwargs):
            raise AssertionError("the held message was sent to the agent loop")

        monkeypatch.setattr(rest_chat, "_run_agent", _never)
        _post("what time is it")

    def test_the_question_is_answered_exactly_once(self, monkeypatch, ha):
        change_id = _applied()
        _agent_asks_to_roll_back(monkeypatch, change_id)
        _post("undo that")
        _post("yes")

        async def _fake(*args, **kwargs):
            return "nothing to do", _AI("")

        monkeypatch.setattr(rest_chat, "_run_agent", _fake)
        _post("yes")

        assert len(ha) == 1

    def test_an_expired_question_is_not_answerable_and_is_said_out_loud(
            self, monkeypatch, no_ha):
        change_id = _applied()
        _agent_asks_to_roll_back(monkeypatch, change_id)
        _post("undo that")

        memory = rt.get_memory("session_panel")
        memory["pending_rollback"]["created_at"] -= rollback.CONFIRMATION_TTL_SECONDS + 1

        async def _fake(*args, **kwargs):
            return "It is half past four.", _AI("")

        monkeypatch.setattr(rest_chat, "_run_agent", _fake)
        response = _post("yes")

        assert activity.change(change_id)["undone_by"] is None
        assert "expired" in response.text
        # The message is still answered — the user asked something, and the
        # expiry is a fact about the old question, not a reason to ignore them.
        assert "half past four" in response.text


class TestAttribution:
    def test_a_chat_rollback_is_recorded_as_coming_from_chat(self, monkeypatch, ha):
        """activity.source_for reads the session id. A rollback asked for in
        ASI1 and logged as 'panel' is a false audit trail."""
        change_id = activity.record_proposal(
            tool="Update Home Assistant automation", args={"automation_id": "1748"},
            session_id="agent1qtest", target="1748",
            inverse={"op": "update_automation", "automation_id": "1748", "config": OLD},
            reversibility="reversible")
        activity.record_decision(change_id, "approved", status="applied")
        _agent_asks_to_roll_back(monkeypatch, change_id)

        _post("undo that", session_id="agent1qtest")
        _post("yes", session_id="agent1qtest")

        rollback_row = activity.change(activity.change(change_id)["undone_by"])
        assert rollback_row["source"] == "asi1"
        assert rollback_row["actor"] == "agent1qtest"

    def test_the_panel_button_still_records_itself_as_the_panel(self, ha):
        change_id = _applied()

        rollback.roll_back(change_id, session_id="panel")

        assert activity.change(activity.change(change_id)["undone_by"])["source"] == "panel"


class TestFailureIsRelayedNotSummarised:
    def test_the_actionable_half_reaches_the_user(self, monkeypatch):
        """§11: explicit and actionable, never silently swallowed. The reply is
        roll_back's own message, so what Home Assistant said and what to do
        about it both survive."""
        monkeypatch.setattr(rollback, "read_automation", lambda aid: (True, {"alias": "now"}))
        monkeypatch.setattr(rollback, "update_automation",
                            lambda aid, cfg: (False, "HTTP 500: entity is gone"))
        change_id = _applied()
        _agent_asks_to_roll_back(monkeypatch, change_id)
        _post("undo that")

        response = _post("yes")

        assert "entity is gone" in response.text
        assert "still in place" in response.text
        assert "automation 1748" in response.text

    def test_the_model_never_gets_to_rephrase_it(self, monkeypatch):
        """The reply is returned directly rather than fed back through the loop.
        A failure the model paraphrases into "that didn't work" has thrown away
        the half the user needs."""
        monkeypatch.setattr(rollback, "read_automation", lambda aid: (True, {}))
        monkeypatch.setattr(rollback, "update_automation", lambda aid, cfg: (False, "HTTP 500"))
        _agent_asks_to_roll_back(monkeypatch, _applied())
        _post("undo that")

        def _never(*args, **kwargs):
            raise AssertionError("the rollback outcome was sent back to the model")

        monkeypatch.setattr(rest_chat, "_run_agent", _never)
        monkeypatch.setattr(rest_chat, "_run_agent_from_history", _never)
        _post("yes")

    def test_a_refused_rollback_is_relayed_by_the_model_instead(self, monkeypatch, no_ha):
        """A refusal never becomes a question, so it goes back through the loop
        as an ordinary tool result for the model to pass on."""
        change_id = _applied()
        activity.mark_undone(change_id, activity.record_rollback(
            undo_of=change_id, tool="x", args={}))

        answer = rollback.prepare(change_id)

        assert rollback.CONFIRMATION_MARKER not in answer
        assert rollback.confirmation_in(json.dumps(answer)) is None


class TestTheLoopRecognisesTheQuestion:
    def test_a_confirmation_payload_is_recognised(self):
        payload = json.dumps(rollback.prepare(_applied()))

        assert rollback.confirmation_in(payload)["change_id"]

    @pytest.mark.parametrize("result", [
        "", None, "not json at all", '{"changes": []}', '{"__approval_required__": true}',
    ])
    def test_an_ordinary_tool_result_is_not(self, result):
        assert rollback.confirmation_in(result) is None


class TestBothChatSurfacesReachThisPath:
    """The panel's chat box posts to /rest/post; ASI1 messages are relayed into
    the same handler by handlers/chat_handlers.py. The parked question is read
    there, once, so neither surface has rollback code of its own to drift.

    Asserted on the source rather than by importing the module: chat_handlers
    reaches uagents_core.contrib, which conftest does not stub, and the point
    here is what the file says rather than what it does at runtime.
    """

    @pytest.fixture
    def source(self):
        return (pathlib.Path(__file__).resolve().parent.parent / "agent_home"
                / "handlers" / "chat_handlers.py").read_text(encoding="utf-8")

    def test_it_relays_chat_text_into_handle_post(self, source):
        assert "from rest.rest_chat import handle_post" in source

    def test_it_grew_no_rollback_handling_of_its_own(self, source):
        """A second copy here is how the two chat surfaces would start
        answering the same question differently."""
        assert "pending_rollback" not in source
        assert "roll_back" not in source
