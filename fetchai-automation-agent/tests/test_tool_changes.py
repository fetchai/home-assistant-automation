"""The conversational rollback path: what the model can see, and what it cannot do.

Design §9 — "if it cannot be read and decided in a chat message, it does not
ship" — is why these tools exist; rollback shipped reachable only from a button.

The property under test throughout is core/rollback.py decision 4: the OPERATION
is pre-authorised and needs no approval, but the SELECTION is made by an LLM
matching "undo that" against a journal, and a wrong selection undoes something
the user never meant. So the tool that says "roll back" must never roll anything
back — it may only ask — and every refusal must arrive in rollback.py's own
words rather than being restated here and left to drift.
"""
import inspect
import json

import pytest

from core import activity
from core import disclosure
from core import gate
from core import rollback
from tools import tool_changes


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    activity.close()


OLD = {"alias": "Evening lights", "trigger": [], "action": []}


def _listing(**kwargs):
    return json.loads(tool_changes.tool_list_changes()._run(**kwargs))


def _ask(change_id):
    return json.loads(tool_changes.tool_roll_back_change()._run(change_id))


def _applied(tool="Update Home Assistant automation", target="1748",
             inverse=None, reversibility="reversible", args=None):
    """A change that was approved, applied, and left an inverse behind."""
    if inverse is None:
        inverse = {"op": "update_automation", "automation_id": target, "config": OLD}
    change_id = activity.record_proposal(
        tool=tool, args=args if args is not None else {"automation_id": target},
        session_id="session_abc", target=target,
        inverse=inverse, reversibility=reversibility)
    activity.record_decision(change_id, "approved", status="applied", result="{}")
    return change_id


class TestSeeingWhatCanBeUndone:
    def test_an_empty_journal_says_so_rather_than_returning_nothing(self):
        """"No changes" and "the tool failed" must not look the same to a model
        that has to tell the user which it was."""
        answer = _listing()

        assert answer["changes"] == []
        assert "has not written anything" in answer["note"]

    def test_an_applied_change_is_offered(self):
        change_id = _applied()

        row = _listing()["changes"][0]

        assert row["change_id"] == change_id
        assert row["can_be_undone"] is True
        assert row["why_not"] == ""

    def test_it_is_described_the_way_every_other_surface_describes_it(self):
        """One describer, in core/gate.py. The panel row, the notification and
        this listing are naming the same event."""
        _applied(tool="Create Home Assistant automation",
                 args={"config_json": '{"alias": "Porch light"}'})

        assert _listing()["changes"][0]["what"] == "create the automation 'Porch light'"

    @pytest.mark.parametrize("setup,fragment", [
        ("rejected", "never applied"),
        ("proposed", "never applied"),
        ("undone", "already rolled back"),
    ])
    def test_what_cannot_be_undone_says_why(self, setup, fragment):
        if setup == "proposed":
            activity.record_proposal(tool="Delete Home Assistant automation", args={})
        elif setup == "rejected":
            cid = activity.record_proposal(tool="Delete Home Assistant automation", args={})
            activity.record_decision(cid, "rejected", status="not_applied")
        else:
            cid = _applied()
            activity.mark_undone(cid, activity.record_rollback(undo_of=cid, tool="x", args={}))

        # The last row the setup made a statement about, not simply the newest:
        # recording a rollback adds a row of its own on top.
        row = [r for r in _listing()["changes"] if not r["undo_of"]][0]

        assert row["can_be_undone"] is False
        assert fragment in row["why_not"]

    def test_a_sensitive_service_call_says_it_has_no_undo_by_design(self):
        """Not a gap to be worked around — the model has to be able to tell the
        user to ask for the opposite action instead."""
        cid = activity.record_proposal(tool="Call Home Assistant service",
                                       args={"service": "lock.unlock"})
        activity.record_decision(cid, "approved", status="applied")

        row = _listing()["changes"][0]

        assert row["can_be_undone"] is False
        assert "re-locking a door" in row["why_not"]


class TestAskedFrom:
    """FIX 4: the model must be told the truth about who asked, not always
    told "the panel" — this is the one place `asked_from` is rendered for
    chat, and core/activity.py's SOURCE_LABELS is the one place it is decided,
    so all three sources are exercised here rather than trusting the mapping
    by inspection."""

    def test_a_panel_change_says_the_panel(self):
        activity.record_proposal(tool="Delete Home Assistant automation", args={},
                                 session_id="session_abc")

        assert _listing()["changes"][0]["asked_from"] == "the panel"

    def test_an_asi1_change_says_asi1_chat(self):
        activity.record_proposal(tool="Delete Home Assistant automation", args={},
                                 session_id="agent1qsomeuseraddress")

        assert _listing()["changes"][0]["asked_from"] == "ASI1 chat"

    def test_an_agent_raised_change_says_the_agent_itself_not_the_panel(self, monkeypatch):
        """The bug this fix removes: an agent-raised change rendered as "the
        panel" is the false audit trail WP2 exists to eliminate."""
        monkeypatch.setattr(gate, "_ha_get", lambda path: (True, {"alias": "Porch at sunset"}))
        gate.propose("Delete Home Assistant automation", {"automation_id": "1748"})

        row = _listing()["changes"][0]
        assert row["asked_from"] == "the agent itself"
        assert row["asked_from"] != "the panel"

    def test_eligibility_is_not_restated_here(self):
        """The listing and the rollback must answer to the same code. If this
        ever drifts, the agent offers an undo and then refuses it."""
        cid = _applied()
        row = _listing()["changes"][0]

        assert row["can_be_undone"] is (rollback.why_not(activity.change(cid)) == "")

    def test_the_listing_carries_no_stored_configs(self):
        """A change row holds a whole automation config in three columns. Twenty
        of those is the model's context spent on something it did not ask for."""
        activity.record_proposal(tool="Update Home Assistant automation",
                                 args={"automation_id": "1748", "config_json": json.dumps(OLD)},
                                 old_config="alias: Evening lights\n",
                                 new_config="alias: Later\n")

        text = tool_changes.tool_list_changes()._run()

        assert "Evening lights" not in text
        assert "config_json" not in text

    def test_the_limit_is_bounded(self):
        for _ in range(3):
            _applied()

        assert len(_listing(limit=2)["changes"]) == 2
        assert len(_listing(limit=9999)["changes"]) == 3

    def test_a_rollback_row_names_what_it_reversed(self):
        cid = _applied()
        rollback_id = activity.record_rollback(
            undo_of=cid, tool=rollback.ROLLBACK_TOOL,
            args={"change_id": cid, "op": "update_automation", "automation_id": "1748"})

        row = [r for r in _listing()["changes"] if r["change_id"] == rollback_id][0]

        assert row["undo_of"] == cid
        assert f"roll back change #{cid}" in row["what"]


class TestAskingIsNotDoing:
    """The whole contract. 'Roll back a change' plans one and asks; it is
    structurally incapable of performing one."""

    @pytest.fixture
    def ha(self, monkeypatch):
        """Fails the test if anything reaches Home Assistant."""
        def _boom(*args, **kwargs):
            raise AssertionError("the tool wrote to Home Assistant without a confirmation")

        for name in ("create_automation", "update_automation", "delete_automation",
                     "read_automation"):
            monkeypatch.setattr(rollback, name, _boom)

    def test_calling_it_changes_nothing_and_returns_a_question(self, ha):
        change_id = _applied()

        answer = _ask(change_id)

        assert answer[rollback.CONFIRMATION_MARKER] is True
        assert answer["change_id"] == change_id
        assert activity.change(change_id)["undone_by"] is None

    def test_the_question_names_the_change_and_what_undoing_it_does(self, ha):
        """§9: a one-word reply carries no record of what it agreed to, so the
        question has to carry it instead."""
        change_id = _applied()

        question = _ask(change_id)["question"]

        assert f"#{change_id}" in question
        assert "restore the previous configuration of automation 1748" in question
        assert "update automation 1748" in question

    def test_nothing_is_journalled_by_asking(self, ha):
        """An unanswered question is not a proposal — nothing would have
        happened, so there is nothing to leave a trace of."""
        change_id = _applied()
        before = len(activity.changes())

        _ask(change_id)

        assert len(activity.changes()) == before

    def test_there_is_no_way_for_the_model_to_say_the_user_confirmed(self, ha):
        """A `confirmed` argument would be the model asserting that a question
        it never asked was answered — §9's exact failure mode. Asserted on the
        signature the registry actually calls, so it holds whatever the schema
        class looks like."""
        parameters = set(inspect.signature(
            tool_changes.tool_roll_back_change()._run).parameters)

        assert parameters == {"change_id"}

    @pytest.mark.parametrize("change_id,fragment", [
        (0, "no change #0"),
        (4242, "no change #4242"),
    ])
    def test_a_change_that_is_not_there(self, ha, change_id, fragment):
        answer = _ask(change_id)

        assert answer["rolled_back"] is False
        assert fragment in answer["reason"]

    def test_a_refusal_arrives_in_rollbacks_own_words(self, ha):
        """Relayed, not restated. The reason is the actionable half."""
        cid = _applied()
        activity.mark_undone(cid, activity.record_rollback(undo_of=cid, tool="x", args={}))

        answer = _ask(cid)

        assert answer["reason"] == rollback.why_not(activity.change(cid))
        assert "in full" in answer["note"]

    def test_an_unrecognised_op_is_refused_rather_than_asked_about(self, ha):
        """Refusing at the question stage means the user is never shown a
        confirmation for something that could not be carried out."""
        cid = _applied(inverse={"op": "reticulate_splines", "automation_id": "1748"})

        answer = _ask(cid)

        assert rollback.CONFIRMATION_MARKER not in answer
        assert "does not know how to perform" in answer["reason"]


class TestTheDisclosureBoundary:
    """The journal is full of things the boundary exists for: automation configs
    with webhook ids in them, Home Assistant's own error strings with URLs, and
    session ids for actors. Driven against a poisoned journal and asserted on
    scan(), so it keeps holding when the payloads are rewritten."""

    @pytest.fixture
    def poisoned_journal(self):
        change_id = activity.record_proposal(
            tool="Update Home Assistant automation",
            args={"automation_id": "1748",
                  "config_json": json.dumps({
                      "alias": "Martin's office lamp at sunset",
                      "trigger": [{"platform": "webhook", "webhook_id": "s3cr3t-hook-abcdef"}],
                      "action": [{"service": "rest_command.ping",
                                  "url": "http://192.168.1.44:8123/api/webhook/x"}]})},
            session_id="agent1qw8n5k2j4h6g9f3d7s1a0z5x2c4v6b8n0m1q3w5e7r9t2y4u6i8o",
            target="1748",
            old_config="alias: Martin's office lamp at sunset\nwebhook_id: s3cr3t-hook-abcdef\n",
            new_config="alias: Later\n",
            inverse={"op": "update_automation", "automation_id": "1748",
                     "config": {"alias": "Before", "webhook_id": "s3cr3t-hook-abcdef"}},
            reversibility="reversible")
        activity.record_decision(
            change_id, "approved", status="failed",
            result='{"error": "HTTP 500 from http://192.168.1.44:8123/api/config: token abc123"}')
        return change_id

    def test_the_listing_carries_nothing_the_boundary_forbids(self, poisoned_journal):
        assert disclosure.scan(json.loads(tool_changes.tool_list_changes()._run())) == []

    def test_the_question_carries_nothing_the_boundary_forbids(self, poisoned_journal):
        answer = tool_changes.tool_roll_back_change()._run(poisoned_journal)

        assert disclosure.scan(json.loads(answer)) == []

    def test_the_secrets_in_the_journal_do_not_come_out_at_all(self, poisoned_journal):
        """Not merely withheld — the listing never reaches for the columns that
        hold them, so there is no marker to explain either."""
        listing = tool_changes.tool_list_changes()._run()

        for secret in ("s3cr3t-hook-abcdef", "192.168.1.44", "abc123", "agent1qw8n5k2j"):
            assert secret not in listing, secret

    def test_a_name_still_arrives_intact(self, poisoned_journal):
        """The product constraint. A change described by its alias has to read
        back as the alias the user chose."""
        change_id = activity.record_proposal(
            tool="Create Home Assistant automation",
            args={"config_json": json.dumps({"alias": "Martin's office lamp"})})

        assert "Martin's office lamp" in tool_changes.tool_list_changes()._run()
