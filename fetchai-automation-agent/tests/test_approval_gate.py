"""The approval gate's decision functions.

_is_destructive is the single predicate standing between the LLM and an
irreversible change to someone's home. Everything here pins current behaviour,
including the branch where an unparseable config deliberately does NOT gate.

The second half of the file is the gate's other entry — proposing a change with
no session behind it, and answering one from its journal row rather than from a
conversation.
"""
from datetime import datetime, timezone

import pytest

from core import activity
from core import agent_runtime as rt
from core import gate
from tools.tool_homeassistant import automations


VALID_CONFIG = '{"alias":"Test","trigger":[],"action":[]}'


class TestIsDestructive:
    @pytest.mark.parametrize("tool_name,args", [
        ("Delete Home Assistant automation", {"automation_id": "123"}),
        ("Update Home Assistant automation", {"automation_id": "1", "config_json": VALID_CONFIG}),
        ("Create Home Assistant automation", {"config_json": VALID_CONFIG}),
        ("Create Home Assistant automation", {}),            # no config_json key: try/except skipped
        ("Create Home Assistant automation", {"config_json": "null"}),  # valid JSON, not a dict
        ("Call Home Assistant service", {"service": "lock.unlock"}),
        ("Call Home Assistant service", {"service": "cover.open_cover"}),
    ])
    def test_these_require_approval(self, tool_name, args):
        assert rt._is_destructive(tool_name, args) is True

    @pytest.mark.parametrize("tool_name,args", [
        ("Call Home Assistant service", {"service": "light.turn_on"}),
        ("Call Home Assistant service", {"service": "switch.toggle"}),
        ("Call Home Assistant service", {}),                 # empty service -> empty domain
        ("List Home Assistant entities", {}),
        ("Datetime get tool", {}),
        ("A tool that does not exist", {}),
    ])
    def test_these_run_immediately(self, tool_name, args):
        assert rt._is_destructive(tool_name, args) is False

    @pytest.mark.parametrize("tool_name", sorted(rt.DESTRUCTIVE_TOOLS))
    def test_unparseable_config_does_not_gate(self, tool_name):
        """Deliberate: an invalid config is let through the gate so the model
        sees the tool's own parse error and self-corrects silently, instead of
        asking the user to approve something malformed. The tool then refuses
        it anyway. Applies to every destructive tool, not just create."""
        assert rt._is_destructive(tool_name, {"config_json": "{not json"}) is False


class TestFormatApprovalMessage:
    def test_delete_names_the_automation_and_says_it_is_irreversible(self):
        out = rt._format_approval_message("Delete Home Assistant automation", {"automation_id": "42"})
        assert "Confirm deletion" in out and "42" in out and "irreversible" in out

    def test_update_names_the_automation(self):
        out = rt._format_approval_message("Update Home Assistant automation", {"automation_id": "42"})
        assert "Confirm update" in out and "42" in out

    def test_create_names_the_alias(self):
        out = rt._format_approval_message(
            "Create Home Assistant automation", {"config_json": '{"alias":"Porch at sunset"}'}
        )
        assert "Confirm creation" in out and "Porch at sunset" in out

    @pytest.mark.parametrize("args", [{}, {"config_json": "{broken"}])
    def test_create_falls_back_to_unknown(self, args):
        assert "unknown" in rt._format_approval_message("Create Home Assistant automation", args)

    @pytest.mark.parametrize("tool_name", [
        "Delete Home Assistant automation", "Update Home Assistant automation",
    ])
    def test_missing_id_falls_back_to_unknown(self, tool_name):
        assert "unknown" in rt._format_approval_message(tool_name, {})

    def test_service_call_names_the_service_and_target(self):
        out = rt._format_approval_message(
            rt.SERVICE_CALL_TOOL, {"service": "lock.unlock", "entity_id": "lock.front_door"}
        )
        assert "lock.unlock" in out and "lock.front_door" in out

    def test_service_call_without_a_target_says_so(self):
        out = rt._format_approval_message(rt.SERVICE_CALL_TOOL, {"service": "homeassistant.restart"})
        assert "no specific entity" in out

    def test_unknown_tool_gets_the_generic_message(self):
        assert rt._format_approval_message("Some Future Tool", {}) == \
            "⚠️ **Confirm action:** Some Future Tool"


class TestApprovalExpired:
    @staticmethod
    def _at(monkeypatch, seconds_ago):
        """Freezes the clock so the TTL boundary is exact rather than racing a
        real second tick.

        now_ts lives in core.gate — approval_expired is re-exported through
        agent_runtime, but the clock it reads is the gate's."""
        fixed = 1_700_000_000

        class _Frozen:
            @staticmethod
            def now(tz=None):
                return datetime.fromtimestamp(fixed, tz=tz)

        monkeypatch.setattr(gate, "datetime", _Frozen)
        return {"created_at": fixed - seconds_ago}

    @pytest.mark.parametrize("pending", [None, {}, {"created_at": None}, {"created_at": 0}])
    def test_without_a_timestamp_it_never_expires(self, pending):
        assert rt.approval_expired(pending) is False

    @pytest.mark.parametrize("seconds_ago", [0, 1, 599, 600])
    def test_within_the_ttl(self, monkeypatch, seconds_ago):
        """600 exactly is NOT expired — the comparison is >, not >=."""
        assert rt.approval_expired(self._at(monkeypatch, seconds_ago)) is False

    @pytest.mark.parametrize("seconds_ago", [601, 3600])
    def test_beyond_the_ttl(self, monkeypatch, seconds_ago):
        assert rt.approval_expired(self._at(monkeypatch, seconds_ago)) is True

    def test_a_timestamp_in_the_future_is_not_expired(self, monkeypatch):
        assert rt.approval_expired(self._at(monkeypatch, -60)) is False

    def test_the_ttl_is_ten_minutes(self):
        """Short on purpose: the only other expiry is the one-hour session
        eviction, far too long a window for a stray 'yes' to fire a delete."""
        assert rt.APPROVAL_TTL_SECONDS == 600


# --------------------------------------------------------------------------
# The sessionless entry: a change the agent raised itself.
#
# Everything above is about a change a model asked for while a user waited.
# These are about the other way in — proposed with nobody asking, answered from
# the journal row rather than from a session, possibly days later. The property
# that carries all of it is that gate.apply cannot tell the difference.
# --------------------------------------------------------------------------
CREATE = "Create Home Assistant automation"
DELETE = "Delete Home Assistant automation"
OLD_CONFIG = {"alias": "Porch at sunset", "trigger": [], "action": []}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A private journal per test — activity holds a thread-local connection,
    so repointing DB_PATH is not enough on its own."""
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    activity.close()


@pytest.fixture
def offline_ha(monkeypatch):
    """Home Assistant answering the two questions proposing asks it: what the
    automation looks like now, and whether the new config is acceptable.

    Only the network half of preflight is stubbed; the real
    _preflight_automation still parses and normalises, so the refusals below
    are the ones the add-on would actually produce.
    """
    monkeypatch.setattr(gate, "_ha_get", lambda path: (True, dict(OLD_CONFIG)))
    monkeypatch.setattr(automations, "_validate_by_test_create", lambda cfg: (True, "1"))


class TestProposing:
    def test_a_proposal_is_journalled_as_the_agent_not_as_a_person(self, fresh_db, offline_ha):
        """source_for returns 'panel' for any session id it does not recognise,
        which for a change nobody asked for would be a false audit trail."""
        out = gate.propose(CREATE, {"config_json": VALID_CONFIG})

        row = activity.change(out["change_id"])
        assert out["ok"] is True
        assert row["source"] == activity.AGENT_SOURCE
        assert row["source"] not in ("panel", "asi1")
        assert row["decision"] == "proposed"

    def test_the_row_carries_the_whole_change(self, fresh_db, offline_ha):
        """Nothing is held in memory, so anything missing here is lost."""
        out = gate.propose(DELETE, {"automation_id": "1748"})

        row = activity.change(out["change_id"])
        assert row["tool"] == DELETE
        assert row["args"] == {"automation_id": "1748"}
        assert row["target"] == "1748"
        assert row["inverse"] == {"op": "restore_automation", "automation_id": "1748",
                                  "config": OLD_CONFIG}
        assert row["reversibility"] == "reversible"
        assert "Porch at sunset" in row["old_config"]

    def test_an_invalid_config_is_refused_before_anyone_sees_it(self, fresh_db, monkeypatch):
        """The proposing path validates; it does not trust its caller. A
        proposal the user approves must not then fail because it was never
        valid."""
        def _never(cfg):
            raise AssertionError("an invalid config reached Home Assistant")

        monkeypatch.setattr(automations, "_validate_by_test_create", _never)

        out = gate.propose(CREATE, {"config_json": '{"alias": "no triggers"}'})

        assert out["ok"] is False
        assert "alias, trigger, and action" in out["error"]
        assert activity.changes() == []

    def test_home_assistants_own_refusal_is_relayed(self, fresh_db, monkeypatch):
        monkeypatch.setattr(automations, "_validate_by_test_create",
                            lambda cfg: (False, "bad trigger"))

        out = gate.propose(CREATE, {"config_json": VALID_CONFIG})

        assert out["ok"] is False and "bad trigger" in out["error"]
        assert activity.changes() == []

    def test_a_tool_the_gate_cannot_describe_is_refused(self, fresh_db, offline_ha):
        """Not the same question as what needs approving: everything the agent
        raises itself is put to the user whatever domain it touches (§3), so
        this refuses what the gate could not record or invert, not what it
        would not gate."""
        out = gate.propose("List Home Assistant entities", {})

        assert out["ok"] is False
        assert activity.changes() == []

    def test_an_everyday_service_call_is_still_proposed(self, fresh_db, offline_ha):
        """_is_destructive lets 'turn off the kitchen light' run immediately
        because a user asked for it. Nobody asked for this one."""
        out = gate.propose(gate.SERVICE_CALL_TOOL,
                           {"service": "light.turn_off", "entity_id": "light.porch"})

        assert out["ok"] is True
        assert activity.change(out["change_id"])["decision"] == "proposed"

    def test_the_question_reaches_the_outbox_and_stays_linked_to_the_row(self, fresh_db, offline_ha):
        """§4: the core emits a message and never learns which channel carried
        it. The link back is what lets answering withdraw the question."""
        out = gate.propose(DELETE, {"automation_id": "1748"})

        message = activity.messages()[0]
        assert message["kind"] == "approval_request"
        assert message["payload"]["change_id"] == out["change_id"]
        assert activity.change(out["change_id"])["notice_id"] == message["id"]

    def test_a_journal_that_would_not_take_the_row_proposes_nothing(self, fresh_db, offline_ha,
                                                                    monkeypatch):
        """On the chat path a failed journal write still leaves a user holding
        a question. Here the row IS the question."""
        monkeypatch.setattr(activity, "record_proposal", lambda **kwargs: None)

        out = gate.propose(DELETE, {"automation_id": "1748"})

        assert out["ok"] is False
        assert activity.messages() == []

    def test_it_says_what_it_proposed_in_the_same_words_everything_else_uses(self, fresh_db,
                                                                             offline_ha):
        out = gate.propose(DELETE, {"automation_id": "1748"})

        assert out["summary"] == "delete automation 1748"
        assert gate.describe_change(activity.change(out["change_id"])) == out["summary"]


class TestPendingFor:
    """Rebuilding the pending from the row — the reason gate.apply needed no
    change at all."""

    def test_it_is_the_parked_shape_minus_the_conversation(self, fresh_db, offline_ha):
        out = gate.propose(DELETE, {"automation_id": "1748"})

        pending, refusal = gate.pending_for(out["change_id"])

        assert refusal == ""
        assert pending["tool_name"] == DELETE
        assert pending["tool_args"] == {"automation_id": "1748"}
        assert pending["change_id"] == out["change_id"]
        assert pending["reversibility"] == "reversible"
        assert "Porch at sunset" in pending["old_yaml"]
        # The three fields that describe a conversation this change never had.
        for absent in ("messages_snapshot", "ai_message", "tool_call_id"):
            assert absent not in pending

    def test_it_carries_the_notice_so_answering_can_withdraw_it(self, fresh_db, offline_ha):
        out = gate.propose(DELETE, {"automation_id": "1748"})

        pending, _ = gate.pending_for(out["change_id"])

        assert pending["notice_id"] == activity.messages()[0]["id"]

    def test_it_does_not_expire_on_the_session_clock(self, fresh_db, offline_ha):
        """The TTL exists because a panel session sits open for an hour and a
        stray click should not fire a delete. This one is meant to wait."""
        out = gate.propose(DELETE, {"automation_id": "1748"})

        pending, _ = gate.pending_for(out["change_id"])

        assert "created_at" not in pending
        assert rt.approval_expired(pending) is False

    def test_a_change_raised_in_a_conversation_is_not_answerable_by_id(self, fresh_db):
        """It has a session holding the conversation to resume, and answering
        it twice — once by id, once by session — would run the tool twice."""
        change_id = activity.record_proposal(tool=DELETE, args={"automation_id": "1748"},
                                             session_id="session_abc")

        pending, refusal = gate.pending_for(change_id)

        assert pending is None
        assert "conversation" in refusal

    def test_an_answered_change_is_not_answerable_again(self, fresh_db, offline_ha):
        out = gate.propose(DELETE, {"automation_id": "1748"})
        activity.record_decision(out["change_id"], "approved", status="applied")

        pending, refusal = gate.pending_for(out["change_id"])

        assert pending is None
        assert "already been answered" in refusal

    def test_an_id_that_is_not_in_the_journal_is_refused_in_words(self, fresh_db):
        pending, refusal = gate.pending_for(4242)

        assert pending is None
        assert "no change #4242" in refusal


class TestTheStartupSweep:
    """expire_stale_proposals closes what a restart made unanswerable. Having
    nothing to lose in session memory is the point of the other kind."""

    def test_it_expires_a_chat_raised_proposal(self, fresh_db):
        change_id = activity.record_proposal(tool=DELETE, args={"automation_id": "1"},
                                             session_id="session_abc")

        assert activity.expire_stale_proposals() == 1
        assert activity.change(change_id)["decision"] == "expired"

    def test_it_leaves_an_agent_raised_one_alone(self, fresh_db, offline_ha):
        out = gate.propose(DELETE, {"automation_id": "1748"})

        activity.expire_stale_proposals()

        assert activity.change(out["change_id"])["decision"] == "proposed"

    def test_the_two_kinds_are_swept_apart_in_one_pass(self, fresh_db, offline_ha):
        chat = activity.record_proposal(tool=DELETE, args={"automation_id": "1"},
                                        session_id="agent1qsomeone")
        raised = gate.propose(DELETE, {"automation_id": "1748"})["change_id"]

        assert activity.expire_stale_proposals() == 1
        assert activity.change(chat)["decision"] == "expired"
        assert activity.change(raised)["decision"] == "proposed"


class TestTheReadback:
    """§9: every approval is echoed back with what actually happened. No model
    is in the loop here, so this is the whole answer."""

    PENDING = {"tool_name": DELETE, "tool_args": {"automation_id": "1748"},
               "change_id": 7, "reversibility": "reversible"}

    def test_a_rejection_says_nothing_was_changed(self):
        out = gate.readback(self.PENDING, False, '{"error": "User rejected this action."}')

        assert "Nothing was changed" in out
        assert "delete automation 1748" in out

    def test_a_failure_is_not_reported_as_a_success(self):
        out = gate.readback(self.PENDING, True, "TOOL_FAILED: {\"error\": \"not found\"}")

        assert "Nothing was changed" in out
        assert "not found" in out
        assert "TOOL_FAILED" not in out

    def test_a_success_names_the_change_and_says_it_can_be_undone(self):
        out = gate.readback(self.PENDING, True, '{"deleted": true}')

        assert "delete automation 1748" in out
        assert "#7" in out
        assert "undone" in out

    def test_a_one_way_change_is_not_advertised_as_undoable(self):
        pending = {**self.PENDING, "tool_name": gate.SERVICE_CALL_TOOL,
                   "tool_args": {"service": "lock.unlock"}, "reversibility": "one_way"}

        out = gate.readback(pending, True, '{"ok": true}')

        assert "undone" not in out
