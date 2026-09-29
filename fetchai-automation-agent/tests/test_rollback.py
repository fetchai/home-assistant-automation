"""Executing a recorded inverse — the half of "reversibly" that used to be
missing.

What matters here is not that the HTTP call happens; it is what the journal
says afterwards. A rollback is a write into someone's home, so it has to leave
the same kind of trace an approved change does: its own row, its own inverse,
its own outcome — and, when it fails, an original that still honestly reads
applied and a message loud enough that nobody has to go looking for the bad
news (design §11).

The Home Assistant side is stubbed at the integration functions, which is the
whole point of extracting them: rolling back reaches those and never the tool
registry, so a test can drive it without a model, a session or a socket.
"""
import ast
import pathlib

import pytest

from core import activity
from core import rollback


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    activity.close()


@pytest.fixture
def ha(monkeypatch):
    """Records what the integration layer was asked to do, and succeeds."""
    calls = []

    def _ok(name, arity):
        def _fn(*args):
            calls.append((name, *args))
            return True, ""
        return _fn

    monkeypatch.setattr(rollback, "create_automation", _ok("create", 2))
    monkeypatch.setattr(rollback, "update_automation", _ok("update", 2))
    monkeypatch.setattr(rollback, "delete_automation", _ok("delete", 1))
    monkeypatch.setattr(rollback, "read_automation",
                        lambda aid: (True, {"alias": "as it stands", "trigger": [], "action": []}))
    return calls


OLD = {"alias": "Evening lights", "trigger": [], "action": []}


def _applied(inverse, tool="Update Home Assistant automation", target="1748"):
    """A change that was approved, applied, and left an inverse behind."""
    change_id = activity.record_proposal(tool=tool, args={"automation_id": target},
                                         session_id="session_abc", target=target,
                                         inverse=inverse, reversibility="reversible")
    activity.record_decision(change_id, "approved", status="applied", result="{}")
    return change_id


class TestDispatch:
    def test_restoring_a_deleted_automation_writes_the_old_config_back(self, ha):
        cid = _applied({"op": "restore_automation", "automation_id": "1748", "config": OLD})

        assert rollback.roll_back(cid)["ok"] is True
        assert ("create", "1748", OLD) in ha

    def test_undoing_an_update_writes_the_previous_config(self, ha):
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})

        assert rollback.roll_back(cid)["ok"] is True
        assert ("update", "1748", OLD) in ha

    def test_undoing_a_creation_deletes_what_it_created(self, ha):
        cid = _applied({"op": "delete_automation", "automation_id": "1748"},
                       tool="Create Home Assistant automation")

        assert rollback.roll_back(cid)["ok"] is True
        assert ("delete", "1748") in ha

    def test_an_unknown_op_is_refused_rather_than_improvised(self, ha):
        """A change written by a gate this version does not understand. Guessing
        would be a write into a home on the strength of an unvalidated string."""
        cid = _applied({"op": "reticulate_splines", "automation_id": "1748"})

        outcome = rollback.roll_back(cid)

        assert outcome["ok"] is False
        assert "does not know how to perform" in outcome["message"]
        assert ha == []

    def test_an_inverse_with_no_config_to_write_is_refused(self, ha):
        """Restoring with nothing to restore would leave the automation empty."""
        cid = _applied({"op": "restore_automation", "automation_id": "1748"})

        assert rollback.roll_back(cid)["ok"] is False
        assert ha == []


class TestWhatMayBeRolledBack:
    def test_a_rejected_change_never_touched_the_house(self, ha):
        cid = activity.record_proposal(tool="Delete Home Assistant automation", args={},
                                       inverse={"op": "restore_automation",
                                                "automation_id": "1748", "config": OLD},
                                       reversibility="reversible")
        activity.record_decision(cid, "rejected", status="not_applied")

        assert rollback.roll_back(cid)["ok"] is False
        assert ha == []

    def test_a_one_way_change_says_why_rather_than_that_it_cannot(self, ha):
        """Sensitive service calls are recorded one_way on purpose, and the
        refusal has to carry that reason — it is a decision, not a gap."""
        cid = activity.record_proposal(tool="Call Home Assistant service",
                                       args={"service": "lock.unlock"})
        activity.record_decision(cid, "approved", status="applied")

        message = rollback.roll_back(cid)["message"]

        assert "re-locking a door" in message
        assert ha == []

    def test_a_change_that_does_not_exist(self, ha):
        assert rollback.roll_back(9999)["ok"] is False

    def test_rolling_back_twice_is_refused_the_second_time(self, ha):
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})
        rollback.roll_back(cid)

        second = rollback.roll_back(cid)

        assert second["ok"] is False
        assert "already rolled back" in second["message"]


class TestTheJournal:
    def test_the_rollback_gets_its_own_row(self, ha):
        """Not a status change on the original. Three verbs in §4, three things
        that happened, each with its own moment and its own outcome."""
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})

        rollback_id = rollback.roll_back(cid)["rollback_id"]

        row = activity.change(rollback_id)
        assert row["undo_of"] == cid
        assert row["status"] == "applied"
        assert row["target"] == "1748"

    def test_the_original_still_reads_applied(self, ha):
        """It WAS applied, and no later event makes that untrue. What the undo
        adds is a pointer, not a correction."""
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})

        rollback_id = rollback.roll_back(cid)["rollback_id"]

        original = activity.change(cid)
        assert original["status"] == "applied"
        assert original["undone_by"] == rollback_id

    def test_the_rollback_is_recorded_with_its_own_inverse(self, ha):
        """Every write is journalled with the way back before the user is told
        it happened, and a rollback is a write."""
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})

        rollback_id = rollback.roll_back(cid)["rollback_id"]

        row = activity.change(rollback_id)
        assert row["reversibility"] == "reversible"
        assert row["inverse"]["op"] == "update_automation"
        # Read from HA before the overwrite — afterwards it would be gone.
        assert row["inverse"]["config"]["alias"] == "as it stands"

    def test_undoing_a_restore_needs_nothing_read_first(self, monkeypatch, ha):
        """Putting a deleted automation back is undone by deleting it again, and
        that inverse is known without asking Home Assistant anything."""
        monkeypatch.setattr(rollback, "read_automation",
                            lambda aid: (_ for _ in ()).throw(AssertionError("read HA needlessly")))
        cid = _applied({"op": "restore_automation", "automation_id": "1748", "config": OLD})

        rollback_id = rollback.roll_back(cid)["rollback_id"]

        assert activity.change(rollback_id)["inverse"] == {
            "op": "delete_automation", "automation_id": "1748"}

    def test_an_unreadable_current_config_does_not_block_the_undo(self, monkeypatch, ha):
        """The undo is the escape hatch. An escape hatch with a precondition is
        not one — so it runs, and records itself as one_way."""
        monkeypatch.setattr(rollback, "read_automation", lambda aid: (False, "HA is restarting"))
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})

        outcome = rollback.roll_back(cid)

        assert outcome["ok"] is True
        assert activity.change(outcome["rollback_id"])["reversibility"] == "one_way"

    def test_a_successful_rollback_is_announced(self, ha):
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})

        rollback.roll_back(cid)

        queued = [m for m in activity.messages() if m["kind"] == "rolled_back"]
        assert len(queued) == 1
        assert queued[0]["urgency"] == "normal"


class TestFailureIsLoud:
    @pytest.fixture
    def refusing_ha(self, monkeypatch, ha):
        monkeypatch.setattr(rollback, "update_automation",
                            lambda aid, cfg: (False, "HTTP 500: entity is gone"))
        return ha

    def test_the_original_is_not_marked_undone(self, refusing_ha):
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})

        rollback.roll_back(cid)

        assert activity.change(cid)["undone_by"] is None

    def test_the_rollback_row_records_the_failure_and_what_ha_said(self, refusing_ha):
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})

        rollback_id = rollback.roll_back(cid)["rollback_id"]

        row = activity.change(rollback_id)
        assert row["status"] == "failed"
        assert "entity is gone" in row["result"]

    def test_it_pushes(self, refusing_ha):
        """§11: never silently swallowed. Urgent is the only agent-to-user path
        there is, so a failed rollback has to take it."""
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})

        rollback.roll_back(cid)

        failed = [m for m in activity.pending("push") if m["kind"] == "rollback_failed"]
        assert len(failed) == 1
        assert "entity is gone" in failed[0]["body"]

    def test_the_change_can_be_tried_again(self, monkeypatch, refusing_ha):
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})
        rollback.roll_back(cid)

        monkeypatch.setattr(rollback, "update_automation", lambda aid, cfg: (True, ""))
        assert rollback.roll_back(cid)["ok"] is True

    def test_a_write_that_did_not_reload_is_a_success_that_says_so(self, monkeypatch, ha):
        monkeypatch.setattr(rollback, "update_automation",
                            lambda aid, cfg: (True, "reload failed — HA is busy"))
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})

        outcome = rollback.roll_back(cid)

        assert outcome["ok"] is True
        assert "reload failed" in outcome["message"]


class TestARestartMidRollback:
    def test_an_unsettled_rollback_is_closed_as_failed_and_pushed(self):
        """Nobody can say whether it landed, and the safe reading of that is the
        loud one."""
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})
        rollback_id = activity.record_rollback(
            undo_of=cid, tool=rollback.ROLLBACK_TOOL,
            args={"change_id": cid, "op": "update_automation", "automation_id": "1748"},
        )

        assert rollback.close_interrupted() == 1

        assert activity.change(rollback_id)["status"] == "failed"
        assert [m for m in activity.pending("push") if m["kind"] == "rollback_failed"]

    def test_a_settled_rollback_is_left_alone(self, ha):
        cid = _applied({"op": "update_automation", "automation_id": "1748", "config": OLD})
        rollback.roll_back(cid)

        assert rollback.close_interrupted() == 0

    def test_an_unanswered_proposal_is_not_mistaken_for_one(self):
        """expire_stale_proposals owns those; this must not reach across."""
        activity.record_proposal(tool="Create Home Assistant automation", args={})

        assert rollback.close_interrupted() == 0


class TestItDoesNotNeedTheModel:
    """The whole reason the writes were extracted. gate.py records inverses as
    integration operations "deliberately — undoing a change must never depend on
    the model being available, or willing", and an import is how that stops
    being true."""

    def test_it_imports_neither_the_agent_loop_nor_the_tool_registry(self):
        tree = ast.parse(pathlib.Path(rollback.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert "core.agent_runtime" not in imported
        assert not any(name.startswith("tools.tool_homeassistant.tool_") for name in imported)
        assert imported & {"tools.tool_homeassistant.automations"}

    def test_no_tool_class_is_reachable_from_it(self):
        """Importing the package would pull the whole registry in behind it."""
        assert not [name for name in vars(rollback) if name.startswith("tool_ha_")]
