"""Wiring tests for /rest/post and /rest/approve — the code that CALLS the
approval gate, not the gate itself.

test_approval_gate.py already proves _is_destructive is correct in isolation.
That is necessary but not sufficient: nothing there proves handle_post actually
consults it at the right moment, or that handle_approve actually executes the
tool it was told to. A regression that deleted the "if parked, return the
approval payload instead of running the tool" branch would leave every other
test in this suite green — these tests are what would catch it.

This only became possible once conftest's uagents.Agent stub let rest_chat.py
be imported at all (core/uagent.py builds a real Agent at module scope). The
LLM itself is still out of reach — _run_agent / _run_agent_from_history /
_execute_tool_async are monkeypatched to canned results, exactly the boundary
the rest of this suite draws everywhere else.
"""
import asyncio
import json

import pytest

import conftest
from core import activity
from core import agent_runtime as rt
from core import gate
from core import protocols
from core import settings
from rest import rest_chat
from tools.tool_homeassistant import automations
from rest.rest_chat import ApprovalRequest

REAL_DEPS = not conftest.STUBBED


def _approval_payload(**overrides):
    payload = {
        "__approval_required__": True,
        "tool_name": "Delete Home Assistant automation",
        "tool_args": {"automation_id": "1712345678901"},
        "tool_call_id": "call_abc",
        "old_yaml": "alias: Porch at sunset\n",
        "new_yaml": "",
    }
    payload.update(overrides)
    return payload


class _AI:
    """Stands in for a langchain AIMessage; only ever stored, never inspected
    for its type by the code under test."""

    def __init__(self, content=""):
        self.content = content


def _run(coro):
    return asyncio.run(coro)


async def _run_returns(value):
    """Wraps a canned value as a coroutine, for monkeypatching the async
    functions rest_chat awaits (_run_agent, _run_agent_from_history)."""
    return value


def _never_called(*args, **kwargs):
    raise AssertionError("this must not be called on this path")


class TestHandlePostParksDestructiveCalls:
    """The scenario from the CLAUDE.md warning made concrete: an approval
    payload from the LLM must park, not execute."""

    def test_a_destructive_tool_call_parks_instead_of_executing(self, monkeypatch):
        monkeypatch.setattr(
            rest_chat, "_run_agent",
            lambda messages, session_id="": _run_returns(
                (json.dumps(_approval_payload()), _AI("calling delete"))
            ),
        )
        # If handle_post ever executed the tool directly instead of parking
        # it, this would fire and fail the test loudly.
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)

        response = _run(rest_chat.handle_post(
            ctx=None, req=protocols.Request(text="delete my porch automation", session_id="s1")
        ))

        assert rt.APPROVAL_MARKER in response.text
        pending = rt.get_memory("s1")["pending_approval"]
        assert pending["tool_name"] == "Delete Home Assistant automation"
        assert pending["tool_args"] == {"automation_id": "1712345678901"}

    def test_a_plain_reply_does_not_park_anything(self, monkeypatch):
        monkeypatch.setattr(
            rest_chat, "_run_agent",
            lambda messages, session_id="": _run_returns(
                ("Turned off the kitchen light.", _AI("Turned off the kitchen light."))
            ),
        )

        response = _run(rest_chat.handle_post(
            ctx=None, req=protocols.Request(text="turn off the kitchen light", session_id="s2")
        ))

        assert response.text == "Turned off the kitchen light."
        assert rt.get_memory("s2")["pending_approval"] is None
    def test_a_timeout_is_reported_not_raised(self, monkeypatch):
        async def _hangs(messages, session_id=""):
            await asyncio.sleep(10)
            return "never", _AI()

        monkeypatch.setattr(rest_chat, "_run_agent", _hangs)
        monkeypatch.setattr(settings, "LLM_TIMEOUT", 0.01)

        response = _run(rest_chat.handle_post(
            ctx=None, req=protocols.Request(text="anything", session_id="s3")
        ))

        assert "took too long" in response.text
        assert rt.get_memory("s3")["pending_approval"] is None

    def test_an_agent_loop_exception_is_reported_not_raised(self, monkeypatch):
        async def _boom(messages, session_id=""):
            raise RuntimeError("asi1 is down")

        monkeypatch.setattr(rest_chat, "_run_agent", _boom)

        response = _run(rest_chat.handle_post(
            ctx=None, req=protocols.Request(text="anything", session_id="s4")
        ))

        assert response.text.startswith("Error processing request:")
        assert "asi1 is down" in response.text


class TestHandleDeleteSession:
    def test_it_forgets_the_history_and_parked_approval(self):
        memory = rt.get_memory("delete-me")
        memory["messages"].append(_AI("a prior turn"))
        memory["pending_approval"] = {"tool_name": "Delete Home Assistant automation"}

        response = _run(rest_chat.handle_delete_session(
            ctx=None, req=protocols.SessionRequest(session_id="delete-me")
        ))

        assert response.text == "Session deleted."
        assert "delete-me" not in rt.session_memory_store


class TestHandleApproveExecutesOrRejects:
    """The other half: an approval must lead to real execution, and a
    rejection must not."""

    def _park(self, session_id, **payload_overrides):
        """Puts a pending approval on a session via the real park_approval,
        the same call handle_post makes — so these tests exercise the actual
        shape handle_approve is handed, not a hand-rolled one."""
        memory = rt.get_memory(session_id)
        rt.park_approval(memory, _approval_payload(**payload_overrides), _AI("calling delete"), [])
        return memory

    def test_approving_executes_the_parked_tool_with_its_exact_args(self, monkeypatch):
        self._park("s5")
        calls = []

        async def _fake_execute(tool_name, tool_args):
            calls.append((tool_name, tool_args))
            return json.dumps({"deleted": True, "automation_id": "1712345678901"})

        monkeypatch.setattr(rest_chat, "_execute_tool_async", _fake_execute)
        monkeypatch.setattr(
            rest_chat, "_run_agent_from_history",
            lambda history, session_id: _run_returns(("Deleted it.", _AI("Deleted it."))),
        )

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(session_id="s5", approved=True)
        ))

        assert calls == [("Delete Home Assistant automation", {"automation_id": "1712345678901"})]
        assert response.text == "Deleted it."
        assert rt.get_memory("s5")["pending_approval"] is None

    def test_rejecting_never_calls_the_tool(self, monkeypatch):
        self._park("s6")
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)

        captured_history = {}

        async def _capture(history, session_id):
            captured_history["value"] = history
            return "Cancelled.", _AI("Cancelled.")

        monkeypatch.setattr(rest_chat, "_run_agent_from_history", _capture)

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(session_id="s6", approved=False)
        ))

        assert response.text == "Cancelled."
        # The model is told the user said no, not left to infer it.
        rejection_tool_msg = captured_history["value"][-1]
        assert "User rejected this action" in rejection_tool_msg.content

    def test_approving_with_nothing_pending_neither_executes_nor_errors(self, monkeypatch):
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)
        monkeypatch.setattr(rest_chat, "_run_agent_from_history", _never_called)

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(session_id="never-had-one", approved=True)
        ))

        assert response.text == "No pending approval found for this session."

    def test_an_approval_is_consumed_exactly_once(self, monkeypatch):
        """A second YES against the same session must find nothing parked —
        otherwise a stray repeated confirmation could re-run a destructive
        call the user only meant to approve once."""
        self._park("s7")
        monkeypatch.setattr(
            rest_chat, "_execute_tool_async",
            lambda name, args: _run_returns(json.dumps({"deleted": True})),
        )
        monkeypatch.setattr(
            rest_chat, "_run_agent_from_history",
            lambda history, session_id: _run_returns(("Deleted it.", _AI("Deleted it."))),
        )

        first = _run(rest_chat.handle_approve(ctx=None, req=ApprovalRequest(session_id="s7", approved=True)))
        second = _run(rest_chat.handle_approve(ctx=None, req=ApprovalRequest(session_id="s7", approved=True)))

        assert first.text == "Deleted it."
        assert second.text == "No pending approval found for this session."

    def test_an_expired_approval_is_not_executed(self, monkeypatch):
        """The panel session can sit open for the full one-hour eviction
        window, far longer than the chat-text TTL — so a stale click must be
        refused here exactly as it already is on the chat path."""
        memory = self._park("s9")
        change_id = memory["pending_approval"]["change_id"]
        memory["pending_approval"]["created_at"] = rt.now_ts() - rt.APPROVAL_TTL_SECONDS - 1
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)
        monkeypatch.setattr(rest_chat, "_run_agent_from_history", _never_called)

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(session_id="s9", approved=True)
        ))

        assert "expired" in response.text.lower()
        assert rt.APPROVAL_MARKER not in response.text
        assert rt.get_memory("s9")["pending_approval"] is None
        assert activity.change(change_id)["decision"] == "expired"

    def test_a_chained_approval_parks_again_instead_of_executing(self, monkeypatch):
        """Approving one destructive call can surface another (e.g. the model
        immediately proposes a follow-up delete). That second call must also
        pause for confirmation, not run on the back of the first YES."""
        self._park("s8")
        monkeypatch.setattr(
            rest_chat, "_execute_tool_async",
            lambda name, args: _run_returns(json.dumps({"deleted": True})),
        )
        monkeypatch.setattr(
            rest_chat, "_run_agent_from_history",
            lambda history, session_id: _run_returns((
                json.dumps(_approval_payload(tool_args={"automation_id": "second-one"})),
                _AI("calling delete again"),
            )),
        )

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(session_id="s8", approved=True)
        ))

        assert rt.APPROVAL_MARKER in response.text
        pending = rt.get_memory("s8")["pending_approval"]
        assert pending["tool_args"] == {"automation_id": "second-one"}


@pytest.mark.skipif(
    REAL_DEPS,
    reason="_rest_routes is bookkeeping conftest's Agent stub adds for introspection — "
           "the real uagents.Agent has no such attribute, and its actual internal route "
           "registry isn't something this suite should reach into.",
)
class TestAgentIsWiredToTheRightRoutes:
    """Structural safety net, stub-only: even before behaviour, the handlers
    have to be registered under the paths the panel actually calls."""

    def test_post_and_approve_are_registered_on_the_real_agent(self):
        from core.uagent import agent_1

        assert agent_1._rest_routes["/rest/post"] is rest_chat.handle_post
        assert agent_1._rest_routes["/rest/approve"] is rest_chat.handle_approve
        assert agent_1._rest_routes["/rest/session/delete"] is rest_chat.handle_delete_session


# --------------------------------------------------------------------------
# Answering by change_id: the same endpoint, for a change with no session.
#
# /rest/approve has always keyed off session_id, and the panel's buttons still
# send exactly that. A change the agent raised itself has no session to key off,
# so it is named by its journal row instead — one entry point, two ways of
# saying which change, and the same gate.apply behind both.
# --------------------------------------------------------------------------
DELETE = "Delete Home Assistant automation"


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    activity.close()


@pytest.fixture
def offline_ha(monkeypatch):
    monkeypatch.setattr(gate, "_ha_get", lambda path: (True, {"alias": "Porch at sunset"}))
    monkeypatch.setattr(automations, "_validate_by_test_create", lambda cfg: (True, "1"))


def _raise_a_change():
    """A proposal with no session anywhere — the thing under test."""
    out = gate.propose(DELETE, {"automation_id": "1712345678901"})
    assert out["ok"] is True
    return out["change_id"]


def _restart(monkeypatch):
    """As close to an add-on restart as this suite gets: the startup sweep
    runs, the session store is empty, and the SQLite handle is dropped so
    nothing is served out of a connection this process already had."""
    rt.session_memory_store.clear()
    activity.expire_stale_proposals()
    activity.close()


class TestApprovingAChangeTheAgentRaised:
    def test_it_executes_through_the_gate_with_the_exact_recorded_args(self, fresh_db, offline_ha,
                                                                       monkeypatch):
        change_id = _raise_a_change()
        calls = []

        async def _fake_execute(tool_name, tool_args):
            calls.append((tool_name, tool_args))
            return json.dumps({"deleted": True})

        monkeypatch.setattr(rest_chat, "_execute_tool_async", _fake_execute)
        # There is no conversation to resume, and none may be invented.
        monkeypatch.setattr(rest_chat, "_run_agent_from_history", _never_called)
        monkeypatch.setattr(rest_chat, "_run_agent", _never_called)

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(change_id=change_id, approved=True)
        ))

        assert calls == [(DELETE, {"automation_id": "1712345678901"})]
        row = activity.change(change_id)
        assert (row["decision"], row["status"]) == ("approved", "applied")
        # §9: echoed back with what was actually done, with no model in the loop.
        assert "delete automation 1712345678901" in response.text
        assert rt.APPROVAL_MARKER not in response.text

    def test_rejecting_runs_nothing_and_says_so(self, fresh_db, offline_ha, monkeypatch):
        change_id = _raise_a_change()
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)
        monkeypatch.setattr(rest_chat, "_run_agent_from_history", _never_called)

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(change_id=change_id, approved=False)
        ))

        assert activity.change(change_id)["decision"] == "rejected"
        assert "Nothing was changed" in response.text

    def test_answering_withdraws_the_question_it_answered(self, fresh_db, offline_ha, monkeypatch):
        """The notice_id lives only on the row here — there is no session
        holding a copy — so this is what proves the row carries it."""
        change_id = _raise_a_change()
        monkeypatch.setattr(rest_chat, "_execute_tool_async",
                            lambda name, args: _run_returns(json.dumps({"deleted": True})))

        asked = activity.messages()[0]
        assert asked["kind"] == "approval_request"

        _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(change_id=change_id, approved=True)
        ))

        # Withdrawn on every channel: collecting "waiting for permission to
        # delete automation 1748" in ASI1 an hour later would ask a question
        # the user has already settled.
        for channel in ("push", "chat", "panel"):
            assert asked["id"] not in [m["id"] for m in activity.pending(channel)]

    def test_it_is_answerable_exactly_once(self, fresh_db, offline_ha, monkeypatch):
        change_id = _raise_a_change()
        monkeypatch.setattr(rest_chat, "_execute_tool_async",
                            lambda name, args: _run_returns(json.dumps({"deleted": True})))

        first = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(change_id=change_id, approved=True)))
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)
        second = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(change_id=change_id, approved=True)))

        assert "delete automation" in first.text
        assert "already been answered" in second.text

    def test_the_suggestion_behind_it_learns_what_happened(self, fresh_db, offline_ha,
                                                           monkeypatch):
        """The seam core/gate.py deliberately leaves open (WP7). The gate hands
        back a change_id and does not know what a suggestion is, so this path
        tells the suggestion — and without it WP9 has no baseline to measure a
        week later, and the row sits at `proposed` against a settled question
        forever."""
        from core import review
        from core import suggestions
        from tools.tool_homeassistant import registry

        suggestions.close()
        # The baseline is measured at apply time and reads the sensor's unit
        # from Home Assistant; there is none here, and a baseline that says so
        # is still a baseline.
        monkeypatch.setattr(registry, "fetch_registry", lambda: (False, "no HA in tests"))
        suggestion_id = suggestions.upsert_candidate(
            "standby:sensor.tv_power", "standby",
            entities=["sensor.tv_power", "switch.tv"], evidence={"entity": "sensor.tv_power"})
        suggestions.mark_validated(suggestion_id, replay={"nights_saved": 30},
                                   estimate={"kwh_per_month": 1.4}, config={"alias": "x"})
        change_id = _raise_a_change()
        suggestions.mark_proposed(suggestion_id, change_id)
        monkeypatch.setattr(rest_chat, "_execute_tool_async",
                            lambda name, args: _run_returns(json.dumps({"deleted": True})))

        _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(change_id=change_id, approved=True)
        ))

        applied = suggestions.suggestion(suggestion_id)
        assert applied["state"] == "applied"
        assert applied["verify_after"] > activity._now()
        assert applied["baseline"]["measured_at"]

    def test_a_change_that_is_not_in_the_journal_is_refused_in_words(self, fresh_db, monkeypatch):
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(change_id=999, approved=True)
        ))

        assert "no change #999" in response.text

    def test_a_chat_raised_change_is_not_answerable_by_id(self, fresh_db, monkeypatch):
        """Its session holds the conversation to resume; answering it here as
        well would run the tool twice."""
        change_id = activity.record_proposal(tool=DELETE, args={"automation_id": "1"},
                                             session_id="session_abc")
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(change_id=change_id, approved=True)
        ))

        assert "conversation" in response.text
        assert activity.change(change_id)["decision"] == "proposed"


class TestItSurvivesARestart:
    """The central fact: a parked approval dies with the process, and an
    agent-raised proposal must not — it may be sitting for days before anyone
    looks at it."""

    def test_the_proposal_is_still_open_and_still_answerable(self, fresh_db, offline_ha,
                                                             monkeypatch):
        change_id = _raise_a_change()

        _restart(monkeypatch)

        assert activity.change(change_id)["decision"] == "proposed"
        monkeypatch.setattr(rest_chat, "_execute_tool_async",
                            lambda name, args: _run_returns(json.dumps({"deleted": True})))
        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(change_id=change_id, approved=True)
        ))

        assert activity.change(change_id)["status"] == "applied"
        assert "delete automation 1712345678901" in response.text

    def test_a_chat_raised_proposal_does_not_survive_it(self, fresh_db, offline_ha, monkeypatch):
        """The other half of the same sweep, in one test so the two cannot
        drift apart: the reason a parked approval is expired is that its
        session is gone, and that reason does not reach the other kind."""
        memory = rt.get_memory("session_abc")
        rt.park_approval(memory, _approval_payload(), _AI("ai"), [], "session_abc")
        chat_id = memory["pending_approval"]["change_id"]
        raised_id = _raise_a_change()

        _restart(monkeypatch)

        assert activity.change(chat_id)["decision"] == "expired"
        assert activity.change(raised_id)["decision"] == "proposed"


class TestThePanelContractIsUnchanged:
    def test_a_request_without_a_change_id_is_the_session_path(self, fresh_db, monkeypatch):
        """The panel posts {session_id, approved} and nothing else, exactly as
        it always has — the new field defaults to 0 and must not divert it."""
        memory = rt.get_memory("panel-session")
        rt.park_approval(memory, _approval_payload(), _AI("calling delete"), [], "panel-session")
        monkeypatch.setattr(rest_chat, "_execute_tool_async",
                            lambda name, args: _run_returns(json.dumps({"deleted": True})))
        monkeypatch.setattr(
            rest_chat, "_run_agent_from_history",
            lambda history, session_id: _run_returns(("Deleted it.", _AI("Deleted it."))),
        )

        request = ApprovalRequest(session_id="panel-session", approved=True)

        assert request.change_id == 0
        response = _run(rest_chat.handle_approve(ctx=None, req=request))
        # The LLM answered, which is what "resumed the conversation" looks like.
        assert response.text == "Deleted it."

    def test_a_session_with_nothing_parked_still_says_so(self, monkeypatch):
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(session_id="never-had-one", approved=True)
        ))

        assert response.text == "No pending approval found for this session."


# --------------------------------------------------------------------------
# FIX 1 — nothing claims the change between the check and the write.
#
# gate.pending_for's decision=='proposed' read and gate.apply's execution are
# two separate steps with an await between them. Two concurrent answers to the
# same agent-raised change can both pass the read; the claim inside apply is
# what stops both from also passing the write.
# --------------------------------------------------------------------------
class TestConcurrentAnswersToOneChange:
    def test_exactly_one_of_two_concurrent_answers_executes(self, fresh_db, offline_ha, monkeypatch):
        change_id = _raise_a_change()
        calls = []

        async def _fake_execute(tool_name, tool_args):
            calls.append((tool_name, tool_args))
            return json.dumps({"deleted": True})

        monkeypatch.setattr(rest_chat, "_execute_tool_async", _fake_execute)
        monkeypatch.setattr(rest_chat, "_run_agent_from_history", _never_called)
        monkeypatch.setattr(rest_chat, "_run_agent", _never_called)

        # Both readers see the row before either writer has claimed it — the
        # actual race handle_approve cannot reproduce sequentially, because
        # gate.pending_for and gate.apply are separate awaits with the read
        # already done for both by the time either applies.
        pending_a, refusal_a = gate.pending_for(change_id)
        pending_b, refusal_b = gate.pending_for(change_id)
        assert refusal_a == refusal_b == ""

        async def _both():
            return await asyncio.gather(
                gate.apply(pending_a, True, activity.AGENT_SESSION, rest_chat._execute_tool_async),
                gate.apply(pending_b, True, activity.AGENT_SESSION, rest_chat._execute_tool_async),
            )

        result_a, result_b = _run(_both())

        outcomes = sorted([result_a, result_b])
        assert len(calls) == 1, "the tool must run exactly once, however many answers arrived"
        assert any("already been answered" in json.loads(o).get("error", "")
                   for o in outcomes if o.startswith("{"))
        row = activity.change(change_id)
        assert (row["decision"], row["status"]) == ("approved", "applied")

    def test_a_double_tapped_reject_still_records_one_outcome(self, fresh_db, offline_ha, monkeypatch):
        change_id = _raise_a_change()
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)

        pending_a, _ = gate.pending_for(change_id)
        pending_b, _ = gate.pending_for(change_id)

        async def _both():
            return await asyncio.gather(
                gate.apply(pending_a, False, activity.AGENT_SESSION, rest_chat._execute_tool_async),
                gate.apply(pending_b, False, activity.AGENT_SESSION, rest_chat._execute_tool_async),
            )

        results = _run(_both())

        rejections = [r for r in results if "User rejected" in r]
        losers = [r for r in results if "already been answered" in r]
        assert len(rejections) == 1
        assert len(losers) == 1
        assert activity.change(change_id)["decision"] == "rejected"


# --------------------------------------------------------------------------
# FIX 1's reaper — an approval claimed but never settled, because a restart
# caught it between the claim and the write's outcome.
# --------------------------------------------------------------------------
class TestTheApprovalReaper:
    def test_a_change_left_in_flight_is_reaped_and_says_so_urgently(self, fresh_db, offline_ha):
        change_id = _raise_a_change()
        # What a crash between claim_decision and record_decision leaves: the
        # decision settled, the outcome unknown.
        assert activity.claim_decision(change_id, "approved") is True

        reaped = gate.close_interrupted()

        assert reaped == 1
        row = activity.change(change_id)
        assert row["status"] == "failed"
        assert "restarted" in row["result"]
        urgent = [m for m in activity.messages() if m["urgency"] == "urgent"]
        assert urgent, "an unresolved outcome must be loud, per §11"
        assert str(change_id) in json.dumps(urgent[0]["payload"])

    def test_an_unanswered_proposal_is_left_alone_by_this_reaper(self, fresh_db, offline_ha):
        """expire_stale_proposals handles "nobody answered"; this reaper must
        not also touch it — the two states must not collapse into one."""
        change_id = _raise_a_change()

        reaped = gate.close_interrupted()

        assert reaped == 0
        assert activity.change(change_id)["decision"] == "proposed"


# --------------------------------------------------------------------------
# FIX 2 — a client cannot forge the agent source by naming the sentinel
# session id itself.
# --------------------------------------------------------------------------
class TestTheAgentSentinelCannotBeForged:
    def test_handle_post_rejects_the_sentinel_session_id(self, monkeypatch):
        monkeypatch.setattr(rest_chat, "_run_agent", _never_called)

        response = _run(rest_chat.handle_post(
            ctx=None, req=protocols.Request(text="delete it", session_id=activity.AGENT_SESSION)
        ))

        assert "Invalid session" in response.text

    def test_handle_approve_rejects_the_sentinel_session_id(self, monkeypatch):
        monkeypatch.setattr(rest_chat, "_execute_tool_async", _never_called)

        response = _run(rest_chat.handle_approve(
            ctx=None, req=ApprovalRequest(session_id=activity.AGENT_SESSION, approved=True)
        ))

        assert "Invalid session" in response.text

    def test_a_proposal_posted_under_the_sentinel_is_not_journaled_as_agent_raised(
        self, fresh_db, offline_ha, monkeypatch
    ):
        """Even if a caller reached park_approval with the sentinel as its real
        session id — bypassing the REST guard entirely — source_for must not
        turn that string into AGENT_SOURCE any more."""
        memory = rt.get_memory(activity.AGENT_SESSION)
        rt.park_approval(memory, _approval_payload(), _AI("ai"), [], activity.AGENT_SESSION)
        change_id = memory["pending_approval"]["change_id"]

        row = activity.change(change_id)
        assert row["source"] != activity.AGENT_SOURCE

        # And not answerable by id, because it never qualified as agent-raised.
        pending, refusal = gate.pending_for(change_id)
        assert pending is None
        assert "conversation" in refusal
