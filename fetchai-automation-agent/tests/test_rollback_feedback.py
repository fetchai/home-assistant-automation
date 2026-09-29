"""Free-hand F-11, F-WP6: an explorer idea that was applied and then rolled
back is answered `rolled_back`, whichever surface undid it.

Every surface ends in core/rollback.py's `roll_back` — the measured result's
"roll back" (verify.answer), the Activity tab's undo (/rest/activity/rollback)
and chat's confirmed rollback (rest_chat → rollback.answer) — so each is
driven here end to end and each must leave the same answer on the row. What
is held besides: only a SUCCESSFUL inverse writes it; a feedback write that
fails or raises neither fails nor undoes the rollback; nothing on the way
reaches the gate; a change behind no explorer idea is left alone; and the
answer is refused to anyone without the journal's rollback row.
"""
import asyncio
import json

import pytest

import legacy_rows
from core import activity
from core import agent_runtime as rt
from core import gate
from core import protocols
from core import rollback
from core import suggestions
from core import verify
from rest import rest_activity
from rest import rest_chat


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    suggestions.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    rt.session_memory_store.clear()
    yield
    suggestions.close()
    rt.session_memory_store.clear()


@pytest.fixture(autouse=True)
def no_gate(monkeypatch):
    """A rollback is pre-authorised (rollback.py decision 1): nothing on any
    of these paths may reach the gate."""
    def _never(*args, **kwargs):
        raise AssertionError("a rollback path reached the approval gate")

    for name in ("apply", "propose", "park_approval"):
        monkeypatch.setattr(gate, name, _never)


@pytest.fixture
def ha(monkeypatch):
    calls = []
    monkeypatch.setattr(rollback, "delete_automation",
                        lambda aid: (calls.append(aid), (True, ""))[1])
    monkeypatch.setattr(rollback, "read_automation", lambda aid: (True, {"alias": "now"}))
    return calls


@pytest.fixture
def refusing_ha(monkeypatch):
    monkeypatch.setattr(rollback, "delete_automation", lambda aid: (False, "HTTP 500: gone"))
    monkeypatch.setattr(rollback, "read_automation", lambda aid: (True, {"alias": "now"}))


CONFIG = {"alias": "Hall light on motion",
          "trigger": [{"platform": "state", "entity_id": "binary_sensor.motion", "to": "on"}],
          "action": [{"service": "light.turn_on", "target": {"entity_id": "light.hall"}}]}
CHART = {"spec": {"type": "timeline", "entities": ["light.hall"], "state": None,
                  "start": 1_700_000_000, "end": 1_701_000_000},
         "series": None, "markers": None, "error": None}


def _applied_change(session_id="agent:self"):
    change_id = activity.record_proposal(
        tool="Create Home Assistant automation", args={"config": CONFIG},
        session_id=session_id, target="hall_motion", reversibility="reversible")
    activity.record_decision(change_id, "approved", status="applied", result="{}",
                             inverse={"op": "delete_automation", "automation_id": "hall_motion"})
    return change_id


def _idea(key="analyst:hall", verified=False, shown=True):
    """A shown explorer idea, proposed through an applied change — and, with
    `verified`, carrying a measured result that waits for keep / roll back."""
    suggestion_id = suggestions.upsert_analyst_candidate(
        key, ["light.hall"], "Hall light on with motion", "It follows motion.", "asi1-mini")
    if shown:
        assert suggestions.mark_shown(suggestion_id, config=CONFIG, finding="It follows motion.",
                                      rule_text="Motion turns the hall light on.", chart=CHART)
    else:
        assert legacy_rows.plant_audited(suggestion_id, CONFIG)
    change_id = _applied_change()
    assert suggestions.mark_proposed(suggestion_id, change_id=change_id)
    assert suggestions.mark_applied(suggestion_id)
    if verified:
        assert suggestions.record_verdict(suggestion_id, {"measured": True}, state="verified")
    return suggestion_id, change_id


def _feedback(suggestion_id):
    return suggestions.suggestion(suggestion_id)["feedback"]


def _rolled_back(suggestion_id, change_id):
    feedback = _feedback(suggestion_id)
    assert feedback["answer"] == "rolled_back"
    assert feedback["reason"] == ""
    assert feedback["change_id"] == change_id
    assert isinstance(feedback["at"], int)
    return feedback


# ---------------------------------------------------------------------------
# Each surface
# ---------------------------------------------------------------------------

class TestEveryPathWritesIt:
    def test_the_measured_results_roll_back(self, ha):
        suggestion_id, change_id = _idea(verified=True)

        outcome = verify.answer(suggestion_id, keep=False, session_id="panel")

        assert outcome["ok"] and outcome["state"] == "rolled_back"
        _rolled_back(suggestion_id, change_id)

    def test_the_activity_tabs_undo(self, ha):
        suggestion_id, change_id = _idea()

        response = asyncio.run(rest_activity.handle_rollback(
            None, protocols.ActivityRequest(change_id=change_id)))

        assert response.error == ""
        _rolled_back(suggestion_id, change_id)
        # Not a transition: the row's state is the verify pass's business.
        assert suggestions.suggestion(suggestion_id)["state"] == "applied"

    def test_chats_confirmed_rollback(self, monkeypatch, ha):
        suggestion_id, change_id = _idea()
        payload = json.dumps(rollback.prepare(change_id, summary="delete automation hall_motion"))

        async def _asks(*args, **kwargs):
            class _AI:
                content = "asked"
            return payload, _AI()

        monkeypatch.setattr(rest_chat, "_run_agent", _asks)
        post = lambda text: asyncio.run(rest_chat.handle_post(
            None, protocols.Request(text=text, session_id="agent1qtest")))
        post("undo the hall light automation")
        assert _feedback(suggestion_id) is None  # asking decides nothing

        response = post("yes")

        assert f"Change #{change_id} has been rolled back" in response.text
        _rolled_back(suggestion_id, change_id)

    def test_it_replaces_the_yes_that_applied_it(self, ha):
        suggestion_id, change_id = _idea()
        assert suggestions.set_feedback(suggestion_id, "yes", "good idea")["answer"] == "yes"

        rollback.roll_back(change_id, session_id="panel")

        _rolled_back(suggestion_id, change_id)


# ---------------------------------------------------------------------------
# Only after the undo landed, and never at its expense
# ---------------------------------------------------------------------------

class TestOnlyASuccessfulUndo:
    def test_a_failed_inverse_writes_nothing(self, refusing_ha):
        suggestion_id, change_id = _idea()
        assert suggestions.set_feedback(suggestion_id, "yes")

        outcome = rollback.roll_back(change_id, session_id="panel")

        assert outcome["ok"] is False
        assert _feedback(suggestion_id)["answer"] == "yes"

    def test_a_failed_verdict_rollback_writes_nothing_either(self, refusing_ha):
        suggestion_id, _ = _idea(verified=True)

        outcome = verify.answer(suggestion_id, keep=False)

        assert outcome["ok"] is False and outcome["state"] == "verified"
        assert _feedback(suggestion_id) is None

    def test_a_refused_rollback_writes_nothing(self, ha):
        suggestion_id, change_id = _idea()
        assert rollback.roll_back(change_id)["ok"]
        suggestions.set_feedback(suggestion_id, "no", "changed my mind")  # a later panel answer

        assert rollback.roll_back(change_id)["ok"] is False  # already undone
        assert _feedback(suggestion_id)["answer"] == "no"

    def test_a_store_that_refuses_does_not_fail_the_rollback(self, monkeypatch, ha):
        suggestion_id, change_id = _idea()
        monkeypatch.setattr(suggestions, "set_feedback", lambda *a, **k: None)

        outcome = rollback.roll_back(change_id, session_id="panel")

        assert outcome["ok"] is True
        assert activity.change(change_id)["undone_by"] == outcome["rollback_id"]

    def test_a_store_that_raises_does_not_fail_or_undo_the_rollback(self, monkeypatch, ha):
        suggestion_id, change_id = _idea()

        def _boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(suggestions, "set_feedback", _boom)

        outcome = rollback.roll_back(change_id, session_id="panel")

        assert outcome["ok"] is True and "has been rolled back" in outcome["message"]
        assert ha == ["hall_motion"]
        assert activity.change(change_id)["undone_by"] == outcome["rollback_id"]
        assert activity.change(outcome["rollback_id"])["status"] == "applied"

    def test_a_change_behind_no_explorer_idea_is_left_alone(self, monkeypatch, ha):
        audited_id, audited_change = _idea(key="analyst:audited", shown=False)
        plain_change = _applied_change(session_id="session_panel")
        written = []
        real = suggestions.set_feedback
        monkeypatch.setattr(suggestions, "set_feedback",
                            lambda *a, **k: written.append(a) or real(*a, **k))

        assert rollback.roll_back(audited_change)["ok"]
        assert rollback.roll_back(plain_change)["ok"]

        assert written == []
        assert _feedback(audited_id) is None


# ---------------------------------------------------------------------------
# Nobody else may claim it
# ---------------------------------------------------------------------------

class TestOnlyTheRollbackPathMayClaimIt:
    def test_without_a_rollback_it_is_refused(self):
        suggestion_id, _ = _idea()
        assert suggestions.set_feedback(suggestion_id, "rolled_back") is None
        assert _feedback(suggestion_id) is None

    def test_a_failed_rollback_does_not_back_it(self, refusing_ha):
        suggestion_id, change_id = _idea()
        rollback_id = rollback.roll_back(change_id)["rollback_id"]
        assert suggestions.set_feedback(suggestion_id, "rolled_back",
                                        rollback_id=rollback_id) is None

    def test_a_rollback_of_another_change_does_not_back_it(self, ha):
        suggestion_id, _ = _idea()
        other = _applied_change()
        rollback_id = rollback.roll_back(other)["rollback_id"]
        assert suggestions.set_feedback(suggestion_id, "rolled_back",
                                        rollback_id=rollback_id) is None
        assert _feedback(suggestion_id) is None
