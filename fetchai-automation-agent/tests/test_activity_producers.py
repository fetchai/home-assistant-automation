"""What actually puts messages in the outbox, and the tool that takes them out.

Three small producers, each with something worth getting wrong:

  * a watched entity disappearing — the ONLY urgent producer in Phase 0, and
    the thing that keeps the push channel from shipping as dead code
  * a session going idle with an approval parked on it — the silent void this
    work exists to close
  * the ASI1 pull, where handing a message over and forgetting to mark it, or
    marking it and failing to hand it over, are both losses

Home Assistant is monkeypatched at registry.fetch_registry, the same seam the
rest of the suite draws.
"""
import asyncio
import json

import pytest

from core import activity
from core import agent_runtime as rt
from core import observation
from core import watchlist
from tasks import observation as observation_task
from tasks import scheduled_tasks
from tools import tool_record
from tools.tool_homeassistant import automations, registry


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setattr(watchlist, "STORE_PATH", str(tmp_path / "watchlist.json"))
    monkeypatch.setattr(watchlist, "_state", None)
    monkeypatch.setattr(watchlist, "_resolved_set", frozenset())
    monkeypatch.setattr(observation, "_names", {})
    rt.session_memory_store.clear()
    yield
    activity.close()
    watchlist._state = None
    watchlist._resolved_set = frozenset()
    rt.session_memory_store.clear()


def _snapshot(entity_ids):
    return {
        "areas": [{"area_id": "hall", "name": "Hall"}],
        "devices": [{"id": "dev_lamp", "name": "Hall lamp", "area_id": "hall"}],
        "entities": [{"entity_id": e, "device_id": "dev_lamp"} for e in entity_ids],
        "states": {e: {"state": "on", "attributes": {}} for e in entity_ids},
    }


def _registry_holding(monkeypatch, entity_ids):
    monkeypatch.setattr(registry, "fetch_registry", lambda: (True, _snapshot(entity_ids)))


class TestAWatchedEntityDisappearing:
    def test_it_is_reported_urgently(self, monkeypatch):
        """Nothing else tells the user. The entity simply stops producing
        events, and anything depending on it quietly stops working."""
        watchlist.set_selection([], ["light.hall", "switch.fan"], {})
        _registry_holding(monkeypatch, ["light.hall"])

        _run(observation_task.resolve_watchlist(None))

        pushable = activity.pending("push")
        assert [m["kind"] for m in pushable] == ["entity_gone"]
        assert "switch.fan" in pushable[0]["body"]

    def test_it_says_which_one_in_words_a_person_recognises(self, monkeypatch):
        watchlist.set_selection([], ["switch.fan"], {})
        observation.remember_names({"switch.fan": "Bedroom fan"})
        _registry_holding(monkeypatch, [])

        _run(observation_task.resolve_watchlist(None))

        assert "Bedroom fan" in activity.pending("push")[0]["body"]

    def test_an_unchanged_registry_says_nothing(self, monkeypatch):
        watchlist.set_selection([], ["light.hall"], {})
        _registry_holding(monkeypatch, ["light.hall"])

        _run(observation_task.resolve_watchlist(None))

        assert activity.messages() == []

    def test_a_registry_that_could_not_be_read_reports_nothing(self, monkeypatch):
        """Home Assistant restarting is the ordinary cause of a failed fetch.
        Announcing every watched entity as gone would be the worst possible
        reading of it."""
        watchlist.set_selection([], ["light.hall"], {})
        monkeypatch.setattr(registry, "fetch_registry", lambda: (False, "connection refused"))

        _run(observation_task.resolve_watchlist(None))

        assert activity.messages() == []

    def test_an_entity_that_was_never_watched_is_not_missed(self, monkeypatch):
        _registry_holding(monkeypatch, [])

        _run(observation_task.resolve_watchlist(None))

        assert activity.messages() == []


class TestNamingWhatDependsOnIt:
    """§11 asks for more than a guess: "Dependent automations flagged... user
    told which and why." The disappearance is only ever checked against
    automations that could actually reference the entity that just went —
    resolve_watchlist must not fetch every automation's config on every tick,
    only on the entity that vanished."""

    def test_the_automations_that_reference_it_are_named(self, monkeypatch):
        watchlist.set_selection([], ["switch.fan"], {})
        _registry_holding(monkeypatch, [])
        monkeypatch.setattr(
            automations, "find_dependent_automations",
            lambda entity_id: (True, ["Bedtime routine", "Fan on heat"]),
        )

        _run(observation_task.resolve_watchlist(None))

        body = activity.pending("push")[0]["body"]
        assert "Bedtime routine" in body
        assert "Fan on heat" in body

    def test_no_dependents_is_said_plainly_not_omitted(self, monkeypatch):
        watchlist.set_selection([], ["switch.fan"], {})
        _registry_holding(monkeypatch, [])
        monkeypatch.setattr(
            automations, "find_dependent_automations",
            lambda entity_id: (True, []),
        )

        _run(observation_task.resolve_watchlist(None))

        body = activity.pending("push")[0]["body"]
        assert "No automation references it directly" in body

    def test_a_failed_lookup_says_so_rather_than_guessing(self, monkeypatch):
        """This is the whole point of the task: the old message asserted
        "anything that depends on it has stopped working" without checking.
        When the check itself cannot run, the message must say that — not
        assert a dependency it never verified, and not stay silent about it."""
        watchlist.set_selection([], ["switch.fan"], {})
        _registry_holding(monkeypatch, [])
        monkeypatch.setattr(
            automations, "find_dependent_automations",
            lambda entity_id: (False, "connection refused"),
        )

        _run(observation_task.resolve_watchlist(None))

        body = activity.pending("push")[0]["body"]
        assert "could not be checked" in body
        assert "connection refused" in body


class TestAnApprovalThatWasNeverAnswered:
    def test_an_evicted_session_closes_its_journal_row(self):
        """The silent void: the session is thrown away, the user types "yes" an
        hour later, and nothing anywhere records that they were ever asked."""
        change_id = activity.record_proposal(tool="Delete Home Assistant automation",
                                             args={"automation_id": "1748"}, session_id="s")
        memory = rt.get_memory("s")
        memory["pending_approval"] = {"change_id": change_id}
        memory["timestamp"] = rt.now_ts() - 7200

        _run(scheduled_tasks.manage_memory(None))

        assert "s" not in rt.session_memory_store
        assert activity.change(change_id)["decision"] == "expired"

    def test_a_live_session_is_left_alone(self):
        change_id = activity.record_proposal(tool="Delete Home Assistant automation",
                                             args={}, session_id="s")
        memory = rt.get_memory("s")
        memory["pending_approval"] = {"change_id": change_id}

        _run(scheduled_tasks.manage_memory(None))

        assert activity.change(change_id)["decision"] == "proposed"


class TestTheChatPull:
    def test_it_hands_over_what_is_waiting(self):
        activity.queue("entity_gone", "A device disappeared", "switch.fan is gone.",
                       urgency="urgent")

        result = json.loads(tool_record.tool_pending_messages()._run())

        assert result["count"] == 1
        assert result["messages"][0]["title"] == "A device disappeared"
        assert result["messages"][0]["body"] == "switch.fan is gone."

    def test_what_was_handed_over_does_not_come_back(self):
        activity.queue("change_applied", "Done", "…")
        tool_record.tool_pending_messages()._run()

        assert json.loads(tool_record.tool_pending_messages()._run())["messages"] == []

    def test_an_empty_queue_is_said_out_loud(self):
        """Silence is where the model invents an update."""
        result = json.loads(tool_record.tool_pending_messages()._run())

        assert result["messages"] == []
        assert "do not invent" in result["note"].lower()

    def test_times_are_local_and_readable(self):
        from datetime import datetime

        activity.queue("change_applied", "Done", "…")

        at = json.loads(tool_record.tool_pending_messages()._run())["messages"][0]["at"]
        datetime.strptime(at, "%Y-%m-%d %H:%M:%S")
