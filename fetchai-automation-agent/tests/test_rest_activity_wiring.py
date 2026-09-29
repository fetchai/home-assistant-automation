"""Wiring tests for /rest/activity — the endpoints behind the panel's audit tab.

The store beneath is covered in test_activity_store.py. What is proved here is
what the panel actually receives: that a change arrives with the same one-line
description the chat message and the notification use, and that opening the tab
marks messages seen for the PANEL only. The second is the one worth a test —
the person at the panel and the person in ASI1 are not necessarily the same
person, and a message silenced for both because one of them glanced at a screen
is a message lost.
"""
import asyncio

import pytest

import conftest
from core import activity
from core import protocols
from core import rollback
from rest import rest_activity

REAL_DEPS = not conftest.STUBBED


def _run(coro):
    return asyncio.run(coro)


def _request(**kwargs):
    return protocols.ActivityRequest(**kwargs)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    activity.close()


class TestReadingTheActivity:
    def test_an_empty_store_is_not_an_error(self):
        response = _run(rest_activity.handle_activity(None, _request()))

        assert response.changes == []
        assert response.messages == []
        assert response.error == ""

    def test_a_change_arrives_described_the_same_way_it_was_announced(self):
        """One describer, in core/gate.py. Three renderings that can drift is
        three chances to describe the same change differently."""
        activity.record_proposal(tool="Create Home Assistant automation",
                                 args={"config_json": '{"alias": "Porch light"}'},
                                 session_id="session_a")

        response = _run(rest_activity.handle_activity(None, _request()))

        assert response.changes[0]["summary"] == "create the automation 'Porch light'"

    def test_the_totals_the_badge_is_drawn_from_come_back(self):
        activity.record_proposal(tool="Delete Home Assistant automation", args={}, session_id="s")
        activity.queue("entity_gone", "Gone", "…", urgency="urgent")

        response = _run(rest_activity.handle_activity(None, _request()))

        assert response.totals["awaiting"] == 1
        assert response.totals["unseen"] == 1

    def test_reading_does_not_mark_anything_seen(self):
        """Only opening the tab does. A background refresh must not clear the
        badge for a user who never looked."""
        activity.queue("change_applied", "Done", "…")

        _run(rest_activity.handle_activity(None, _request()))

        assert _run(rest_activity.handle_activity(None, _request())).totals["unseen"] == 1


class TestOpeningTheTab:
    def test_it_clears_the_unseen_count(self):
        activity.queue("change_applied", "Done", "…")

        response = _run(rest_activity.handle_seen(None, _request()))

        assert response.totals["unseen"] == 0

    def test_it_leaves_the_message_waiting_for_chat(self):
        activity.queue("change_applied", "Done", "…")

        _run(rest_activity.handle_seen(None, _request()))

        assert len(activity.pending("chat")) == 1

    def test_it_answers_with_the_refreshed_state(self):
        """The panel renders what comes back rather than asking again."""
        activity.record_proposal(tool="Create Home Assistant automation",
                                 args={"config_json": "{}"}, session_id="s")

        response = _run(rest_activity.handle_seen(None, _request()))

        assert len(response.changes) == 1


class TestRollingBackFromThePanel:
    """The third verb. §4 asks the panel to record everything proposed, applied
    AND rolled back, and this is the only endpoint here that writes."""

    @pytest.fixture
    def ha(self, monkeypatch):
        monkeypatch.setattr(rollback, "update_automation", lambda aid, cfg: (True, ""))
        monkeypatch.setattr(rollback, "read_automation", lambda aid: (True, {"alias": "now"}))

    def _applied_update(self):
        change_id = activity.record_proposal(
            tool="Update Home Assistant automation", args={"automation_id": "1748"},
            session_id="session_a", target="1748",
            inverse={"op": "update_automation", "automation_id": "1748",
                     "config": {"alias": "before"}},
            reversibility="reversible")
        activity.record_decision(change_id, "approved", status="applied", result="{}")
        return change_id

    def test_it_undoes_the_change_and_answers_with_the_refreshed_journal(self, ha):
        """One round trip. The panel can never show an outcome that disagrees
        with the record it was read from."""
        change_id = self._applied_update()

        response = _run(rest_activity.handle_rollback(None, _request(change_id=change_id)))

        assert response.error == ""
        assert len(response.changes) == 2
        assert response.totals["rolled_back"] == 1

    def test_the_rollback_row_describes_itself(self, ha):
        """It is not a tool call, so gate._describe has no name to recognise —
        the row says what it undid and what it did instead."""
        change_id = self._applied_update()

        response = _run(rest_activity.handle_rollback(None, _request(change_id=change_id)))

        summary = response.changes[0]["summary"]
        assert summary.startswith(f"roll back change #{change_id}")
        assert "automation 1748" in summary

    def test_a_refusal_comes_back_as_an_explicit_error(self):
        """§11: never silently swallowed. A 200 with an empty error field would
        be exactly that."""
        response = _run(rest_activity.handle_rollback(None, _request(change_id=4242)))

        assert "no change #4242" in response.error


@pytest.mark.skipif(
    REAL_DEPS,
    reason="_rest_routes is bookkeeping conftest's Agent stub adds for introspection — "
           "the real uagents.Agent has no such attribute.",
)
class TestTheRoutesThePanelCalls:
    def test_all_three_endpoints_are_registered(self):
        from core.uagent import agent_1

        assert agent_1._rest_routes["/rest/activity"] is rest_activity.handle_activity
        assert agent_1._rest_routes["/rest/activity/seen"] is rest_activity.handle_seen
        assert agent_1._rest_routes["/rest/activity/rollback"] is rest_activity.handle_rollback
