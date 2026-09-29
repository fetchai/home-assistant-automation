"""The journal and the outbox, against real SQLite.

core/activity.py is stdlib sqlite3, so nothing here is stubbed and nothing needs
to be — these are the actual queries the gate and the delivery layer issue.

Two properties carry the weight and both are about not losing things. A change
put to the user must leave a row whatever happens next, including nothing
happening at all; and a message must not be counted as delivered until a channel
has actually taken it, per channel, because the panel and ASI1 are not
necessarily the same person.
"""
import pytest

from core import activity


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """A private database per test. activity keeps a thread-local connection,
    so pointing DB_PATH somewhere new is not enough on its own."""
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    activity.close()


CREATE = "Create Home Assistant automation"
DELETE = "Delete Home Assistant automation"


def _proposal(**kwargs):
    defaults = {"tool": CREATE, "args": {"config_json": "{}"}, "session_id": "session_abc"}
    return activity.record_proposal(**{**defaults, **kwargs})


class TestTheChangeJournal:
    def test_a_proposal_exists_before_anyone_answers(self):
        """The row is what makes an unanswered request visible in the panel,
        including one raised over ASI1 that nobody is looking at."""
        change_id = _proposal()

        row = activity.change(change_id)

        assert row["decision"] == "proposed"
        assert row["decided_at"] is None
        assert row["proposed_at"] > 0

    def test_the_args_come_back_as_data_not_a_string(self):
        change_id = _proposal(args={"automation_id": "1748", "why": "test"})

        assert activity.change(change_id)["args"]["automation_id"] == "1748"

    def test_approval_closes_the_row_it_opened(self):
        change_id = _proposal()

        activity.record_decision(change_id, "approved", status="applied", result='{"created": true}')

        row = activity.change(change_id)
        assert (row["decision"], row["status"]) == ("approved", "applied")
        assert row["decided_at"] > 0

    def test_a_rejection_is_recorded_as_firmly_as_an_approval(self):
        """What the user declined is part of the audit trail, not an absence."""
        change_id = _proposal()

        activity.record_decision(change_id, "rejected", status="not_applied")

        assert activity.change(change_id)["decision"] == "rejected"

    def test_an_inverse_given_at_decision_time_marks_the_row_reversible(self):
        """A creation's inverse names an id Home Assistant only mints when the
        create runs, so it cannot be known when the change is proposed."""
        change_id = _proposal(reversibility="reversible")

        activity.record_decision(change_id, "approved", status="applied",
                                 inverse={"op": "delete_automation", "automation_id": "1748"})

        row = activity.change(change_id)
        assert row["inverse"] == {"op": "delete_automation", "automation_id": "1748"}
        assert row["reversibility"] == "reversible"

    def test_an_inverse_recorded_at_proposal_time_survives_the_decision(self):
        change_id = _proposal(tool=DELETE, inverse={"op": "restore_automation"},
                              reversibility="reversible")

        activity.record_decision(change_id, "approved", status="applied")

        assert activity.change(change_id)["inverse"] == {"op": "restore_automation"}

    def test_a_one_way_change_says_so(self):
        """Recording an inverse that does not exist would make the rollback
        rate the design measures meaningless."""
        change_id = _proposal(tool="Call Home Assistant service", reversibility="one_way")

        row = activity.change(change_id)
        assert row["reversibility"] == "one_way"
        assert row["inverse"] is None

    def test_the_surface_is_derived_from_the_session_id(self):
        """ASI1 sessions ARE the sender's uAgent address; the panel makes its
        own. Nothing has to pass the surface down through the call stack."""
        assert activity.change(_proposal(session_id="agent1qw8s0mefake"))["source"] == "asi1"
        assert activity.change(_proposal(session_id="session_k3j"))["source"] == "panel"
        assert activity.change(_proposal(session_id=""))["source"] == "panel"

    def test_the_actor_is_kept_verbatim(self):
        assert activity.change(_proposal(session_id="agent1qw8s0mefake"))["actor"] == "agent1qw8s0mefake"

    def test_changes_come_back_newest_first(self):
        first = _proposal(target="a")
        second = _proposal(target="b")

        assert [r["id"] for r in activity.changes()] == [second, first]

    def test_a_decision_on_nothing_is_not_an_error(self):
        """The gate calls this with whatever the session carried. A missing id
        must not take the approval down with it."""
        assert activity.record_decision(None, "approved") is False


class TestExpiry:
    def test_a_restart_closes_what_it_orphaned(self):
        """The session memory holding the question is gone, so the question can
        no longer be answered — and a row reading "awaiting you" forever would
        be a lie the panel tells indefinitely."""
        open_change = _proposal()
        answered = _proposal()
        activity.record_decision(answered, "approved", status="applied")

        assert activity.expire_stale_proposals() == 1

        assert activity.change(open_change)["decision"] == "expired"
        assert activity.change(answered)["decision"] == "approved"

    def test_expiry_says_why(self):
        change_id = _proposal()

        activity.expire_stale_proposals()

        assert "restarted" in activity.change(change_id)["result"]


class TestTheOutbox:
    def test_a_message_waits_for_every_channel_separately(self):
        activity.queue("change_applied", "Done", "The automation was written.")

        assert len(activity.pending("chat")) == 1
        assert len(activity.pending("panel")) == 1

    def test_taking_a_message_on_one_channel_leaves_the_others(self):
        """The person at the panel and the person in ASI1 are not necessarily
        the same person, and neither should silence the other."""
        message_id = activity.queue("change_applied", "Done", "…")

        activity.mark(message_id, "panel")

        assert activity.pending("panel") == []
        assert len(activity.pending("chat")) == 1

    def test_only_urgent_messages_are_pushed(self):
        """§4 has exactly one agent-to-user path. Spending it on a digest would
        teach the user to ignore the channel that matters."""
        activity.queue("digest", "Three suggestions", "…")
        activity.queue("entity_gone", "A device disappeared", "…", urgency="urgent")

        pushable = activity.pending("push")

        assert [m["kind"] for m in pushable] == ["entity_gone"]

    def test_pending_is_oldest_first(self):
        """A queue delivers in the order things happened, not in reverse."""
        first = activity.queue("a", "1", "…")
        second = activity.queue("b", "2", "…")

        assert [m["id"] for m in activity.pending("chat")] == [first, second]

    def test_the_payload_comes_back_as_data(self):
        activity.queue("change_applied", "Done", "…", payload={"change_id": 7})

        assert activity.pending("chat")[0]["payload"] == {"change_id": 7}

    def test_a_withdrawn_message_stops_being_delivered_everywhere(self):
        """The question has been answered. Collecting "waiting for permission"
        in ASI1 an hour later would ask it again."""
        message_id = activity.queue("approval_request", "Approval needed", "…")

        activity.withdraw(message_id)

        assert activity.pending("chat") == []
        assert activity.pending("panel") == []

    def test_withdrawing_does_not_overwrite_a_real_delivery(self):
        """It was genuinely collected; when is part of the record."""
        message_id = activity.queue("approval_request", "Approval needed", "…")
        activity.mark(message_id, "chat")
        collected_at = activity.messages()[0]["collected_at"]

        activity.withdraw(message_id)

        assert activity.messages()[0]["collected_at"] == collected_at

    def test_a_withdrawn_message_is_still_in_the_record(self):
        message_id = activity.queue("approval_request", "Approval needed", "…")

        activity.withdraw(message_id)

        assert len(activity.messages()) == 1

    def test_a_delivered_message_is_kept_not_deleted(self):
        """The panel shows what was sent as well as what is waiting."""
        message_id = activity.queue("change_applied", "Done", "…")
        activity.mark(message_id, "panel")
        activity.mark(message_id, "chat")

        assert len(activity.messages()) == 1

    def test_an_unknown_channel_returns_nothing_rather_than_reaching_sql(self):
        """The channel name ends up in a column position. It is not going to do
        so unchecked."""
        activity.queue("change_applied", "Done", "…")

        assert activity.pending("seen_at; DROP TABLE outbox") == []
        assert activity.mark(1, "nonsense") is False
        assert len(activity.messages()) == 1


class TestStats:
    def test_it_counts_what_the_tab_header_and_badge_show(self):
        awaiting = _proposal()
        applied = _proposal()
        activity.record_decision(applied, "approved", status="applied")
        activity.queue("entity_gone", "Gone", "…", urgency="urgent")

        stats = activity.stats()

        assert stats["changes"] == 2
        assert stats["awaiting"] == 1
        assert stats["applied"] == 1
        assert stats["unseen"] == 1
        assert stats["undelivered"] == 1
        assert awaiting  # the row that makes "awaiting" non-zero

    def test_an_empty_store_reports_zeroes_rather_than_failing(self):
        assert activity.stats()["changes"] == 0


class TestUpgradingAnOlderStore:
    """activity.db may never be dropped and rebuilt — it is the audit trail, and
    one that starts again at every upgrade is not one."""

    def test_the_rollback_columns_are_added_to_a_table_that_predates_them(self, tmp_path, monkeypatch):
        import sqlite3

        activity.close()
        path = str(tmp_path / "old.db")
        old = sqlite3.connect(path)
        old.execute(
            "CREATE TABLE changes (id INTEGER PRIMARY KEY, proposed_at INTEGER NOT NULL,"
            " tool TEXT NOT NULL, decision TEXT NOT NULL DEFAULT 'proposed', status TEXT,"
            " reversibility TEXT NOT NULL DEFAULT 'one_way')"
        )
        old.execute("INSERT INTO changes (proposed_at, tool) VALUES (1, ?)", (CREATE,))
        old.commit()
        old.close()

        monkeypatch.setattr(activity, "DB_PATH", path)
        row = activity.change(1)

        assert row["undo_of"] is None
        assert row["undone_by"] is None
        activity.close()


class TestRollbackRows:
    def _applied(self):
        change_id = _proposal(inverse={"op": "delete_automation", "automation_id": "1748"},
                              reversibility="reversible")
        activity.record_decision(change_id, "approved", status="applied")
        return change_id

    def test_a_rollback_row_starts_settled_but_with_no_outcome(self):
        """There is nothing to ask — the original approval authorises its own
        undo — so only the result is still open."""
        original = self._applied()

        rollback_id = activity.record_rollback(undo_of=original, tool="Roll back change",
                                               args={"change_id": original})

        row = activity.change(rollback_id)
        assert row["decision"] == "pre_authorised"
        assert row["status"] is None
        assert row["undo_of"] == original

    def test_marking_a_change_undone_leaves_its_outcome_alone(self):
        original = self._applied()
        rollback_id = activity.record_rollback(undo_of=original, tool="Roll back change", args={})

        activity.mark_undone(original, rollback_id)

        row = activity.change(original)
        assert row["undone_by"] == rollback_id
        assert row["status"] == "applied"

    def test_an_unfinished_rollback_is_abandoned_at_startup(self):
        original = self._applied()
        rollback_id = activity.record_rollback(undo_of=original, tool="Roll back change", args={})

        abandoned = activity.abandon_running_rollbacks()

        assert [r["id"] for r in abandoned] == [rollback_id]
        assert activity.change(rollback_id)["status"] == "failed"

    def test_expiring_proposals_does_not_touch_rollbacks(self):
        """Two different kinds of unfinished business, closed by two different
        passes — an unanswered proposal changed nothing, an interrupted rollback
        may have changed everything."""
        original = self._applied()
        rollback_id = activity.record_rollback(undo_of=original, tool="Roll back change", args={})

        activity.expire_stale_proposals()

        assert activity.change(rollback_id)["decision"] == "pre_authorised"

    def test_the_rollback_rate_is_counted(self):
        """§12 calls it the number that matters, and it counts originals that
        were undone, not the undos."""
        original = self._applied()
        rollback_id = activity.record_rollback(undo_of=original, tool="Roll back change", args={})
        activity.record_decision(rollback_id, "pre_authorised", status="applied")
        activity.mark_undone(original, rollback_id)

        assert activity.stats()["rolled_back"] == 1
