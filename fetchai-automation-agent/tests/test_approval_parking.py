"""Parking a destructive tool call on a session.

park_approval is the single assembly both REST paths use to stash a pending
confirmation and build the reply that carries it to the client. It exists
because /rest/post and /rest/approve each had a copy, and the copies drifted —
so most of what is pinned here is that the two call shapes still agree.

The second half is about the pending that is NOT parked: a change the agent
raised itself keeps nothing in session memory, and is rebuilt from its journal
row instead.
"""
import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from core import activity
from core import agent_runtime as rt
from core import gate
from tools.tool_homeassistant import automations


PARSED = {
    "__approval_required__": True,
    "tool_name": "Delete Home Assistant automation",
    "tool_args": {"automation_id": "1712345678901"},
    "tool_call_id": "call_abc",
    "old_yaml": "alias: Porch at sunset\n",
    "new_yaml": "",
}


class _Msg:
    """Stands in for a langchain message; park_approval only ever stores it."""

    def __init__(self, content=""):
        self.content = content


class TestNowTs:
    def test_it_is_epoch_seconds(self):
        assert abs(rt.now_ts() - time.time()) < 2

    @pytest.mark.parametrize("tz", ["UTC", "Asia/Tokyo", "America/Los_Angeles"])
    def test_the_host_timezone_does_not_move_it(self, tz, monkeypatch):
        """The eviction window in tasks.scheduled_tasks is a difference of two
        of these ints, so the clock has to mean the same instant on every host.
        Home Assistant sets TZ in the add-on container, so a non-UTC host is
        the normal case rather than an edge one.
        """
        monkeypatch.setenv("TZ", tz)
        time.tzset()
        try:
            assert abs(rt.now_ts() - time.time()) < 2
        finally:
            monkeypatch.undo()
            time.tzset()

    def test_it_agrees_with_an_explicit_utc_reading(self):
        assert abs(rt.now_ts() - datetime.now(tz=timezone.utc).timestamp()) < 2


class TestParkApproval:
    def test_it_stores_everything_needed_to_resume(self):
        memory = rt.get_memory("s")
        ai_msg = _Msg("the tool call")
        rt.park_approval(memory, PARSED, ai_msg, [_Msg("hello")])

        pending = memory["pending_approval"]
        assert pending["tool_name"] == PARSED["tool_name"]
        assert pending["tool_args"] == PARSED["tool_args"]
        assert pending["tool_call_id"] == "call_abc"
        assert pending["ai_message"] is ai_msg
        assert pending["old_yaml"] == PARSED["old_yaml"]
        assert pending["new_yaml"] == ""

    def test_the_reply_carries_the_marker_and_the_payload(self):
        memory = rt.get_memory("s")
        friendly, full = rt.park_approval(memory, PARSED, _Msg(), [])

        assert full.startswith(friendly)
        assert rt.APPROVAL_MARKER in full
        # The panel parses what follows the marker; chat clients re-render it.
        assert "Porch at sunset" in rt.format_approval_for_chat(full)

    def test_the_snapshot_excludes_the_confirmation_prompt(self):
        """The snapshot is replayed to the LLM once the user answers. The
        prompt asking them to confirm is not part of that conversation — it
        goes to the live history only."""
        memory = rt.get_memory("s")
        earlier = _Msg("turn it off")
        friendly, _ = rt.park_approval(memory, PARSED, _Msg(), [earlier])

        assert memory["pending_approval"]["messages_snapshot"] == [earlier]
        assert [m.content for m in memory["messages"]] == ["turn it off", friendly]

    def test_the_snapshot_is_not_aliased_to_the_live_history(self):
        """They are stored on the same dict and mutated independently."""
        memory = rt.get_memory("s")
        rt.park_approval(memory, PARSED, _Msg(), [_Msg("hello")])

        memory["messages"].append(_Msg("later"))
        assert len(memory["pending_approval"]["messages_snapshot"]) == 1

    def test_both_timestamps_come_off_the_same_clock(self):
        """created_at gates the ten-minute text-reply TTL and timestamp gates
        the one-hour session eviction. They are written together, so a reader
        of either is looking at the same instant."""
        memory = rt.get_memory("s")
        rt.park_approval(memory, PARSED, _Msg(), [])

        assert memory["timestamp"] == pytest.approx(
            memory["pending_approval"]["created_at"], abs=1
        )
        assert not rt.approval_expired(memory["pending_approval"])

    def test_a_freshly_parked_approval_is_not_evictable(self):
        """The exact comparison tasks.scheduled_tasks.manage_memory makes."""
        memory = rt.get_memory("s")
        rt.park_approval(memory, PARSED, _Msg(), [])
        assert rt.now_ts() - memory["timestamp"] <= 1


class TestTheTwoCallSitesAgree:
    """rest_chat calls this from /rest/post with the live history, and from
    /rest/approve with a snapshot extended by the answered call. The bug that
    prompted the extraction was the two sites disagreeing, so pin that the
    shape they produce is the same one."""

    @staticmethod
    def _park(session, snapshot):
        memory = rt.get_memory(session)
        friendly, full = rt.park_approval(memory, PARSED, _Msg("ai"), snapshot)
        return memory, friendly, full

    def test_the_parked_entry_has_the_same_keys_either_way(self):
        post, _, post_full = self._park("from-post", [_Msg("hello")])
        chained, _, chained_full = self._park(
            "from-approve", [_Msg("hello"), _Msg("ai"), _Msg("tool result")]
        )

        assert set(post["pending_approval"]) == set(chained["pending_approval"])
        assert post_full == chained_full

    def test_both_leave_the_prompt_as_the_last_live_message(self):
        post, post_friendly, _ = self._park("from-post", [_Msg("hello")])
        chained, chained_friendly, _ = self._park(
            "from-approve", [_Msg("hello"), _Msg("ai"), _Msg("tool result")]
        )

        assert post["messages"][-1].content == post_friendly
        assert chained["messages"][-1].content == chained_friendly
        assert len(chained["messages"]) == 4


# --------------------------------------------------------------------------
# The pending that is not parked anywhere.
#
# park_approval keeps its pending in session_memory_store: in-process, evicted
# after an hour, gone on restart. That is fine for a question answered in the
# next minute and useless for one that may wait days, so a change the agent
# raised itself is not parked at all — gate.pending_for rebuilds it from the
# journal row. These pin that the two are the same thing to everything
# downstream of them.
# --------------------------------------------------------------------------
CONVERSATION_FIELDS = {"messages_snapshot", "ai_message", "tool_call_id"}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    activity.close()


@pytest.fixture
def offline_ha(monkeypatch):
    """Only the network half of the proposing path is stubbed."""
    monkeypatch.setattr(gate, "_ha_get", lambda path: (True, {"alias": "Porch at sunset"}))
    monkeypatch.setattr(automations, "_validate_by_test_create", lambda cfg: (True, "1"))


def _park(session_id="session_abc"):
    """Parks the same change the conversational path would, from the payload
    the gate itself builds — not the hand-written PARSED above, which predates
    the journalling fields and would make the comparison flatter than it is."""
    parsed, error = gate._build_destructive_approval(
        PARSED["tool_name"], PARSED["tool_args"], {"id": "call_abc"}
    )
    assert error is None
    memory = rt.get_memory(session_id)
    rt.park_approval(memory, json.loads(parsed), _Msg("ai"), [_Msg("hello")], session_id)
    return memory["pending_approval"]


def _propose():
    out = gate.propose(PARSED["tool_name"], PARSED["tool_args"])
    pending, _ = gate.pending_for(out["change_id"])
    return pending


class TestTheParkedAndTheReconstructedAgree:
    def test_they_differ_by_exactly_the_conversation_and_the_clock(self, fresh_db, offline_ha):
        """The row holds everything the gate needs and nothing about a
        conversation, which is the whole reason gate.apply needed no change."""
        parked = set(_park())
        reconstructed = set(_propose())

        # created_at is deliberately absent: the TTL is a session's clock, and
        # an agent-raised proposal is meant to wait. See gate.pending_for.
        assert parked - reconstructed == CONVERSATION_FIELDS | {"created_at"}
        assert reconstructed - parked == set()

    def test_the_fields_the_gate_executes_from_are_identical(self, fresh_db, offline_ha):
        parked = _park()
        reconstructed = _propose()

        for field in ("tool_name", "tool_args", "old_yaml", "new_yaml", "reversibility"):
            assert parked[field] == reconstructed[field], field

    def test_both_point_at_their_own_row_and_their_own_question(self, fresh_db, offline_ha):
        """The decision has to land on the row the proposal opened, and the
        question has to be withdrawable by whoever answers it."""
        parked = _park()
        reconstructed = _propose()

        for pending in (parked, reconstructed):
            row = activity.change(pending["change_id"])
            assert row["decision"] == "proposed"
            assert row["notice_id"] == pending["notice_id"]

    def test_a_reconstructed_pending_applies_to_the_same_result(self, fresh_db, offline_ha):
        """The property the whole work package rests on: gate.apply cannot tell
        which of the two it was handed."""
        results = []
        for pending in (_park(), _propose()):
            calls = []

            async def _execute(tool_name, tool_args):
                calls.append((tool_name, tool_args))
                return json.dumps({"deleted": True})

            result = asyncio.run(gate.apply(pending, True, "session_abc", _execute))
            row = activity.change(pending["change_id"])
            results.append((calls, result, row["decision"], row["status"]))

        assert results[0] == results[1]
        assert results[0][0] == [(PARSED["tool_name"], PARSED["tool_args"])]

    def test_the_session_store_holds_nothing_for_an_agent_raised_change(self, fresh_db, offline_ha):
        """Nothing to lose in session memory is the point: a restart takes the
        store with it and the proposal is still there to answer."""
        gate.propose(PARSED["tool_name"], PARSED["tool_args"])

        assert rt.session_memory_store == {}
        assert activity.changes()[0]["decision"] == "proposed"
