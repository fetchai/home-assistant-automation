"""LLM tools over the change journal: what the agent wrote, and undoing it.

Design §4 makes ASI1 the PRIMARY interface, and §9 states the rule that made
these necessary: "The plain-text rendering is the design constraint, not the
fallback. If it cannot be read and decided in a chat message, it does not ship."
Rollback shipped reachable only from a button in the panel, which is exactly
that failure.

Not in tools/tool_homeassistant/: neither tool calls a Home Assistant API. They
read the add-on's own journal (core/activity.py) and hand the decision to
core/rollback.py, which owns every rule about what may be undone.

The two-step contract, and why it is shaped this way
----------------------------------------------------

core/rollback.py decision 4 is the argument; this is its mechanics. The
operation is pre-authorised, so nothing here re-approves it. The SELECTION is
not, because in chat the model picks a change id by matching "undo that"
against a journal — so 'Roll back a change' NEVER rolls anything back. It plans
one and returns a question. The agent loop recognises that question in the
tool's RESULT and hands it to the REST layer, which holds it on the session and
reads the user's own next message with parse_approval_decision.

That is why the confirmation is not an argument on this tool. A `confirmed`
flag would be the model asserting that a question was answered, which is the
failure §9 names: "the user said yes to a question they could not see." A tool
result cannot be forged by the model; a tool argument can.

The listing exists for the same reason. Without it the model has no way to turn
"put the kitchen automation back" into a change id except by guessing, and the
eligibility it reports comes from rollback.why_not — the same code that will
refuse the rollback — so the model is never offered an undo that is then
refused.

What leaves the house
---------------------

The listing carries ids, one-line descriptions, times, outcomes and the reason
a change cannot be undone. It deliberately does NOT carry the journal's stored
configs or Home Assistant's result blobs: a change row holds a whole automation
config in `args`, `old_config` and `new_config`, and twenty of those would be
the model's entire context spent on something it did not ask for. It can fetch
one config with 'Get Home Assistant automation' if it needs it. The actor
column — a uAgent address for an ASI1 session — is left out too; `source` says
which surface asked, which is the part that means anything to a person.
"""
from datetime import datetime
from typing import Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import activity
from core import disclosure
from core import gate
from core import rollback

# How many journal rows one answer carries. The journal is never pruned — it is
# the audit trail — so a home a year in has thousands of rows and "recent" has
# to mean something.
DEFAULT_CHANGES = 20
MAX_CHANGES = 50


def _local_iso(ts) -> str:
    """A unix second as local time. Same reasoning as the copy in
    tools/tool_record.py: the stores work in UTC epoch seconds and the model
    reads a bare integer as nonsense."""
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _outcome(row: dict) -> str:
    """What became of this change, in the words the panel uses for it."""
    decision = row.get("decision")
    if decision == "proposed":
        return "awaiting the user's answer"
    if decision == "rejected":
        return "rejected by the user"
    if decision == "expired":
        return "expired unanswered"
    if decision == "refused":
        # FIX 3: the gate itself refused at apply time — drift or a stale
        # config, not the user saying no. `result` on the row carries why.
        return "refused before it was written"
    status = row.get("status")
    if not status:
        return "in flight"
    return "applied" if status == "applied" else "failed"


def _row(row: dict) -> dict:
    """One journal row as the model should see it."""
    reason = rollback.why_not(row)
    return {
        "change_id": row.get("id"),
        "what": gate.describe_change(row),
        "at": _local_iso(row.get("decided_at") or row.get("proposed_at")),
        "asked_from": activity.source_label(row.get("source")),
        "outcome": _outcome(row),
        "automation_id": row.get("target") or "",
        "can_be_undone": not reason,
        # Empty when it can be. Carried so the model can tell the user WHY
        # rather than "no" — a sensitive service call has no undo on purpose,
        # and that is worth saying.
        "why_not": reason,
        # Present only on a rollback row, naming what it reversed.
        "undo_of": row.get("undo_of"),
        "undone_by_change": row.get("undone_by"),
    }


class _ListChangesInput(BaseModel):
    """Input schema for the change listing."""
    limit: int = Field(
        description=f"How many recent changes to return, newest first. Defaults to "
                    f"{DEFAULT_CHANGES}, maximum {MAX_CHANGES}.",
        default=DEFAULT_CHANGES,
    )


class tool_list_changes(BaseTool):
    """LLM tool listing what the agent has written into Home Assistant."""
    name: str = "List agent changes"
    description: str = (
        "List the automation changes this agent has proposed and applied, newest first, with "
        "whether each one can still be undone. Each entry has a change_id, a one-line description, "
        "when it happened, which surface asked for it, its outcome, and — when it cannot be undone "
        "— the reason. Call this FIRST whenever the user wants something undone or reverted, or "
        "asks what you have changed or created recently. This is the agent's own journal of its own "
        "writes; it is not a list of everything that happened in Home Assistant."
    )
    args_schema: Type[BaseModel] = _ListChangesInput

    def _run(self, limit: int = DEFAULT_CHANGES) -> str:
        """Reads the journal and marks each row with rollback's own verdict."""
        try:
            limit = max(1, min(int(limit), MAX_CHANGES))
        except (TypeError, ValueError):
            limit = DEFAULT_CHANGES

        rows = activity.changes(limit=limit)
        payload = {
            "source": "the add-on's own change journal",
            "changes": [_row(r) for r in rows],
            "count": len(rows),
        }
        if not rows:
            payload["note"] = (
                "The agent has not written anything into Home Assistant yet. Say exactly that — "
                "there is nothing to undo, and nothing to look for elsewhere."
            )
        return disclosure.emit(payload)


class _RollBackInput(BaseModel):
    """Input schema for undoing one recorded change."""
    change_id: int = Field(
        description="The change_id to undo, from 'List agent changes'. Never guess it — list the "
                    "changes first and use the id of the one the user meant.",
        default=0,
    )


class tool_roll_back_change(BaseTool):
    """LLM tool that puts one recorded change to the user for confirmation."""
    name: str = "Roll back a change"
    description: str = (
        "Undo one automation change this agent made, by running the inverse operation recorded "
        "when the user approved it — putting a deleted automation back, restoring the previous "
        "configuration of an updated one, or removing one that was created. Pass the change_id "
        "from 'List agent changes'. The user is shown exactly which change and what undoing it "
        "will do, and must confirm in their own words before anything happens, so calling this "
        "never changes the home on its own. It only works on changes THIS agent made and recorded; "
        "it cannot undo anything else in Home Assistant, and sensitive service calls such as "
        "unlocking a door have no undo by design."
    )
    args_schema: Type[BaseModel] = _RollBackInput

    def _run(self, change_id: int = 0) -> str:
        """Plans the rollback and phrases the question. Runs nothing.

        Every refusal comes back from core/rollback.py in its own words, so the
        model has something to tell the user rather than a boolean.
        """
        return disclosure.emit(rollback.prepare(change_id, summary=_summary_of(change_id)))


def _summary_of(change_id) -> str:
    """What the change itself was, in the words every other surface uses.

    Passed into rollback.prepare rather than built there: the phrasing comes
    from core/gate.py, which core/rollback.py may not import — a module the undo
    path depends on must not drag in langchain.
    """
    row = activity.change(change_id)
    return gate.describe_change(row) if row else ""
