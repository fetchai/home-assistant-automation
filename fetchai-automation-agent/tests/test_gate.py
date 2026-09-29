"""The approval gate as a component: what it decides, and what it writes down.

The predicates moved here from core/agent_runtime.py, so the first thing this
file does is prove the move changed no behaviour — test_approval_gate.py still
exercises them through the re-export, and these run against the new home.

The rest is what is new. Design §5 asks that nothing reach Home Assistant
"without a recorded approval and a recorded inverse operation", and gate.apply
is the only path that runs an approved tool. So: approving records, rejecting
records and executes nothing, a failed write is not recorded as a success, and
the inverse is right for each kind of change — including the awkward one, a
creation whose inverse names an id that does not exist until it has run.
"""
import asyncio
import json

import pytest

from core import activity
from core import gate
from tools.tool_homeassistant import automations


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    activity.close()


@pytest.fixture(autouse=True)
def no_ha_calls(monkeypatch):
    """FIX 3 makes gate.apply re-run preflight (CREATE/UPDATE) and the drift
    check (UPDATE/DELETE) at approval time, so every path through apply() now
    reaches Home Assistant even in this file's unit tests. None of them are
    testing that half — test_approval_gate.py and the drift-specific tests in
    this file are — so it is stubbed the same way it is stubbed there: the
    test-create always validates, and there is nothing to have drifted from
    unless a test says otherwise."""
    monkeypatch.setattr(automations, "_validate_by_test_create", lambda cfg: (True, "temp-id"))
    monkeypatch.setattr(gate, "_ha_get", lambda path: (False, "not found"))


CREATE = "Create Home Assistant automation"
UPDATE = "Update Home Assistant automation"
DELETE = "Delete Home Assistant automation"
SERVICE = "Call Home Assistant service"

OLD_CONFIG = {"alias": "Evening lights", "trigger": [], "action": []}


def _run(coro):
    return asyncio.run(coro)


def _executor(result):
    """A tool runner that answers with `result` and remembers being called."""
    calls = []

    async def _execute(tool_name, tool_args):
        calls.append((tool_name, tool_args))
        return result

    _execute.calls = calls
    return _execute


_VALID_CONFIG_JSON = json.dumps({"alias": "Evening lights", "trigger": [], "action": []})


def _pending(tool=CREATE, args=None, **extra):
    args = args or {"config_json": _VALID_CONFIG_JSON}
    change_id = activity.record_proposal(
        tool=tool, args=args,
        session_id="session_abc",
        reversibility=extra.pop("reversibility", "reversible"),
        inverse=extra.pop("inverse", None),
    )
    return {"tool_name": tool, "tool_args": args,
            "change_id": change_id, "tool_call_id": "call_1", **extra}


class TestWhatNeedsApproval:
    """Unchanged behaviour, re-tested at its new address."""

    @pytest.mark.parametrize("tool", sorted(gate.DESTRUCTIVE_TOOLS))
    def test_automation_writes_are_gated(self, tool):
        assert gate._is_destructive(tool, {}) is True

    def test_a_sensitive_service_call_is_gated(self):
        assert gate._is_destructive(SERVICE, {"service": "lock.unlock"}) is True

    def test_an_everyday_service_call_is_not(self):
        """Asking twice for "turn off the kitchen light" is worse than the
        mistake it would prevent."""
        assert gate._is_destructive(SERVICE, {"service": "light.turn_off"}) is False

    def test_an_unparseable_config_is_left_to_the_model(self):
        assert gate._is_destructive(CREATE, {"config_json": "{not json"}) is False


class TestTheInverse:
    def test_an_update_is_undone_by_restoring_what_was_there(self):
        inverse, reversibility = gate._inverse_for(UPDATE, {"automation_id": "1748"}, OLD_CONFIG)

        assert inverse == {"op": "update_automation", "automation_id": "1748", "config": OLD_CONFIG}
        assert reversibility == "reversible"

    def test_a_delete_is_undone_by_putting_it_back_at_the_same_id(self):
        """The id is the entity_id everything else references. Restoring the
        config under a new one would leave every reference broken."""
        inverse, _ = gate._inverse_for(DELETE, {"automation_id": "automation.1748"}, OLD_CONFIG)

        assert inverse["op"] == "restore_automation"
        assert inverse["automation_id"] == "1748"

    def test_a_create_has_no_inverse_yet_but_is_still_reversible(self):
        inverse, reversibility = gate._inverse_for(CREATE, {"config_json": "{}"}, None)

        assert inverse is None
        assert reversibility == "reversible"

    def test_a_sensitive_service_call_gets_no_inverse_at_all(self):
        """Locking a door you just unlocked is a new action with its own
        consequences, not an undo."""
        inverse, reversibility = gate._inverse_for(SERVICE, {"service": "lock.unlock"}, None)

        assert inverse is None
        assert reversibility == "one_way"

    def test_a_config_that_could_not_be_read_is_not_pretended_to_be_reversible(self):
        inverse, reversibility = gate._inverse_for(UPDATE, {"automation_id": "1748"}, None)

        assert inverse is None
        assert reversibility == "one_way"


class TestApplying:
    def test_approving_runs_the_tool_and_records_it(self):
        execute = _executor('{"created": true, "automation_id": "1748"}')
        pending = _pending()

        _run(gate.apply(pending, True, "session_abc", execute))

        assert execute.calls == [(CREATE, pending["tool_args"])]
        row = activity.change(pending["change_id"])
        assert (row["decision"], row["status"]) == ("approved", "applied")

    def test_a_creation_is_given_its_inverse_once_the_id_exists(self):
        execute = _executor('{"created": true, "automation_id": "1748"}')
        pending = _pending()

        _run(gate.apply(pending, True, "session_abc", execute))

        assert activity.change(pending["change_id"])["inverse"] == {
            "op": "delete_automation", "automation_id": "1748",
        }

    def test_rejecting_records_the_decision_and_runs_nothing(self):
        execute = _executor("never called")
        pending = _pending()

        result = _run(gate.apply(pending, False, "session_abc", execute))

        assert execute.calls == []
        assert json.loads(result)["error"] == "User rejected this action."
        assert activity.change(pending["change_id"])["decision"] == "rejected"

    def test_a_failed_write_is_not_recorded_as_a_success(self):
        execute = _executor('{"error": "Entity not found"}')
        pending = _pending()

        result = _run(gate.apply(pending, True, "session_abc", execute))

        assert result.startswith("TOOL_FAILED:"), "the model must not read a failure as a success"
        row = activity.change(pending["change_id"])
        assert row["status"] == "failed"
        assert row["inverse"] is None, "nothing was written, so there is nothing to undo"

    def test_a_tool_that_raised_counts_as_failed(self):
        """Tool errors come back as bare prose, not JSON."""
        execute = _executor("Tool error: connection refused")
        pending = _pending()

        _run(gate.apply(pending, True, "session_abc", execute))

        assert activity.change(pending["change_id"])["status"] == "failed"

    def test_the_result_is_kept_on_the_row(self):
        execute = _executor('{"created": true, "automation_id": "1748"}')
        pending = _pending()

        _run(gate.apply(pending, True, "session_abc", execute))

        assert "1748" in activity.change(pending["change_id"])["result"]


class TestRefusingOnDrift:
    """FIX 3: the captured old_config is a snapshot, not a lock. If the
    automation changed in Home Assistant between propose and apply, the diff
    the user approved no longer describes what would happen — so `apply`
    refuses rather than re-diffing and silently overwriting the edit."""

    def _update_pending(self, captured_yaml, **extra):
        args = {"automation_id": "1748", "config_json": _VALID_CONFIG_JSON}
        change_id = activity.record_proposal(
            tool=UPDATE, args=args, session_id="session_abc", old_config=captured_yaml,
        )
        return {"tool_name": UPDATE, "tool_args": args, "change_id": change_id,
                "tool_call_id": "call_1", "old_yaml": captured_yaml, **extra}

    def test_an_unchanged_automation_applies_normally(self, monkeypatch):
        live = OLD_CONFIG
        monkeypatch.setattr(gate, "_ha_get", lambda path: (True, live))
        pending = self._update_pending(gate.yaml.dump(live, allow_unicode=True, sort_keys=False))
        execute = _executor('{"updated": true}')

        result = _run(gate.apply(pending, True, "s", execute))

        assert execute.calls == [(UPDATE, pending["tool_args"])]
        assert activity.change(pending["change_id"])["status"] == "applied"

    def test_a_hand_edited_automation_is_refused_not_overwritten(self, monkeypatch):
        captured = gate.yaml.dump(OLD_CONFIG, allow_unicode=True, sort_keys=False)
        edited_live = {**OLD_CONFIG, "alias": "Someone renamed this by hand"}
        monkeypatch.setattr(gate, "_ha_get", lambda path: (True, edited_live))
        pending = self._update_pending(captured)
        execute = _executor('{"updated": true}')

        result = _run(gate.apply(pending, True, "s", execute))

        assert execute.calls == [], "nothing may be written over an edit nobody was asked about"
        assert json.loads(result)["error"]
        row = activity.change(pending["change_id"])
        assert row["decision"] == "refused"
        assert row["status"] == "not_applied"

    def test_a_refused_row_withdraws_its_own_notice(self, monkeypatch):
        """Settled honestly rather than left 'proposed' to be answered again
        against the same stale capture."""
        captured = gate.yaml.dump(OLD_CONFIG, allow_unicode=True, sort_keys=False)
        edited_live = {**OLD_CONFIG, "alias": "Changed"}
        monkeypatch.setattr(gate, "_ha_get", lambda path: (True, edited_live))
        notice_id = activity.queue("approval_request", "Approval needed", "…")
        pending = self._update_pending(captured, notice_id=notice_id)

        _run(gate.apply(pending, True, "s", _executor('{"updated": true}')))

        assert notice_id not in [m["id"] for m in activity.pending("chat")]
        assert activity.change(pending["change_id"])["decision"] == "refused"

    def test_a_create_has_nothing_to_drift_from_but_is_still_preflighted(self, monkeypatch):
        """A create has no old_config, so drift does not apply to it — but a
        config validated days ago against entities that may since have
        disappeared is the same staleness wearing a different coat."""
        from tools.tool_homeassistant import automations
        monkeypatch.setattr(automations, "_validate_by_test_create",
                            lambda cfg: (False, "entity light.gone does not exist"))
        pending = _pending()  # CREATE, no old_yaml at all

        result = _run(gate.apply(pending, True, "s", _executor("never called")))

        assert json.loads(result)["error"]
        assert activity.change(pending["change_id"])["decision"] == "refused"

    def test_ha_being_unreachable_does_not_manufacture_a_drift_refusal(self, monkeypatch):
        """Cannot confirm either way — the write itself should surface a real
        problem rather than this check inventing one."""
        captured = gate.yaml.dump(OLD_CONFIG, allow_unicode=True, sort_keys=False)
        monkeypatch.setattr(gate, "_ha_get", lambda path: (False, "unreachable"))
        pending = self._update_pending(captured)
        execute = _executor('{"updated": true}')

        _run(gate.apply(pending, True, "s", execute))

        assert execute.calls == [(UPDATE, pending["tool_args"])]


class TestWhatTheUserIsTold:
    def test_every_outcome_queues_a_message(self):
        """§9: every approval is echoed back with what was actually done,
        because a one-word reply carries no record of what it agreed to."""
        _run(gate.apply(_pending(), True, "s", _executor('{"automation_id": "1"}')))
        _run(gate.apply(_pending(), False, "s", _executor("")))
        _run(gate.apply(_pending(), True, "s", _executor('{"error": "no"}')))

        kinds = [m["kind"] for m in activity.messages()]
        assert set(kinds) == {"change_applied", "change_rejected", "change_failed"}

    def test_answering_withdraws_the_request_that_asked(self):
        """Otherwise ASI1 collects "waiting for permission to…" long after the
        user settled it."""
        notice_id = activity.queue("approval_request", "Approval needed", "…")
        pending = {**_pending(), "notice_id": notice_id}

        _run(gate.apply(pending, True, "s", _executor('{"automation_id": "1"}')))

        assert [m["kind"] for m in activity.pending("chat")] == ["change_applied"]

    def test_the_message_names_the_change_in_plain_words(self):
        _run(gate.apply(_pending(), True, "s", _executor('{"automation_id": "1"}')))

        assert "Evening lights" in activity.messages()[0]["body"]

    def test_a_one_way_change_is_not_advertised_as_undoable(self):
        pending = _pending(tool=SERVICE, args={"service": "lock.unlock", "entity_id": "lock.front"},
                           reversibility="one_way")

        _run(gate.apply(pending, True, "s", _executor('{"ok": true}')))

        assert "undone" not in activity.messages()[0]["body"]

    def test_an_applied_automation_is(self):
        _run(gate.apply(_pending(), True, "s", _executor('{"automation_id": "1"}')))

        assert "can be undone" in activity.messages()[0]["body"]


class TestDescribing:
    """One describer, because the panel row, the chat message and the Home
    Assistant notification are all describing the same change."""

    def test_a_creation_is_named_by_its_alias(self):
        assert gate._describe(CREATE, {"config_json": '{"alias": "Porch light"}'}) == \
            "create the automation 'Porch light'"

    def test_an_unnamed_creation_still_reads_as_english(self):
        assert gate._describe(CREATE, {"config_json": "{}"}) == "create an automation"

    def test_a_service_call_names_its_target(self):
        assert gate._describe(SERVICE, {"service": "lock.unlock", "entity_id": "lock.front"}) == \
            "call lock.unlock on lock.front"

    def test_a_broken_config_does_not_take_the_description_down(self):
        assert gate._describe(CREATE, {"config_json": "{not json"}) == "create an automation"
