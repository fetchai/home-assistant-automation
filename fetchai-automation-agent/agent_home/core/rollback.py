"""Executing a recorded inverse: the half of "reversibly" that was missing.

core/gate.py works out the inverse of every destructive change while the old
config is still readable, and core/activity.py stores it. This module is what
runs one. Together they are design §11's "applied automation misbehaves →
recorded inverse operation; rollback offered without waiting for the weekly
report".

FOUR decisions are made here rather than left to the call sites.

1. The OPERATION is not gated. It is pre-authorised by the approval that let the
   original change through.

   The argument is not convenience. gate.py exists because a write the user did
   not ask for must be impossible; a rollback is the opposite — it is the user
   asking for the state they already had, and the operation is not chosen by
   anybody at rollback time. It was computed at approval time, from the config
   Home Assistant held, and written down. There is no argument to fill in, no
   entity to pick, no model in the loop. Asking "may I?" about returning the
   home to a state the user has already approved of would be asking permission
   to withdraw permission, and would put an LLM turn in front of the one path
   that must work when the LLM does not — which is precisely what gate.py's
   docstring forbids: "undoing a change must never depend on the model being
   available, or willing."

   What a rollback does NOT get out of is being recorded. It is a write into
   someone's home, so it is journalled with its own inverse before the user is
   told it happened, exactly like a gated one. That is the convention the gate
   protects; the approval step is not.

   Note the scope of that argument, because decision 4 turns on it: it is about
   the OPERATION. It was written for a surface where the user clicked a specific
   journal row, and it says nothing about how a change gets picked.

2. A rollback is a NEW ROW, not a status change on the original.

   Design §4 asks the panel to be "a local record of everything proposed,
   applied and rolled back" — three verbs, three things that happened, each with
   its own moment and its own outcome. Flipping the original's status from
   'applied' to 'rolled_back' would spend one field on two facts and lose the
   second: when the undo happened, which surface asked for it, and whether it
   worked. It has nowhere at all to put a rollback that FAILED, and §11 requires
   that case to be visible — with the original still honestly reading
   'applied', because it still is.

   So the original is never rewritten. It gains one pointer, `undone_by`, set
   only once the undo actually succeeded. Everything else about the rollback
   lives on the rollback's own row, `undo_of` naming what it reversed. An audit
   trail is append-mostly: you do not edit what happened, you record what
   happened next.

3. An unrecognised op is refused, never improvised.

   The dispatch table below is the whole vocabulary. An inverse naming anything
   else is a change written by a version of the gate this one does not
   understand, and guessing at it would be a write into a home on the strength
   of a string nobody validated.

4. In a conversation, the SELECTION is confirmed — even though the operation is
   not. These are two different decisions and this module keeps them apart.

   In the panel the user picks the row. Nothing is inferred: the change id comes
   from the thing they clicked, so decision 1 covers the whole act. In chat
   nobody clicks anything. The user says "undo that" or "put the kitchen
   automation back", and a model reads the journal and picks an id by matching
   fuzzy text against it. The operation is still pre-authorised — but WHICH
   change to undo is a fresh decision, made by an LLM, and getting it wrong
   undoes something the user never meant.

   Design §9: "Every approval is echoed back with what was actually done,
   because a one-word reply carries no record of what it agreed to", and "the
   question must be unambiguous". So `prepare()` names the specific change and
   what undoing it will do, in plain text, and the surface holds that question
   until the user answers it in their own words. `answer()` is the only chat
   path that reaches `roll_back()`.

   What this is NOT is a second approval, and it must never become one. It does
   not go through core/gate.py, the tool is not in DESTRUCTIVE_TOOLS, and no
   part of it calls gate.apply — routing it there would put the model back in
   front of the escape hatch, which is what decision 1 exists to prevent. The
   confirmation reads the USER's own reply, with
   core.agent_runtime.parse_approval_decision, which deliberately does not use
   the LLM and matches the whole normalised message. The model asks; it does not
   answer.

Failure is loud (§11: "surfaced as an explicit, actionable error — never
silently swallowed"). A failed rollback records itself as failed, leaves the
original reading applied, keeps the change eligible to be tried again, and
queues an URGENT message — urgent because the user is being told that the
escape hatch did not open, and that is the one thing they must not find out by
going to look.

This module imports the Home Assistant integration functions and nothing else
from the tools package. It must never import core/agent_runtime or the tool
registry: the moment rolling back needs the model to be reachable, it stops
being a guarantee. core/suggestions is a store, not the model: it is here for
one write AFTER a successful undo (`_answer_the_idea`, free-hand F-WP6), which
cannot fail the undo it follows.
"""
from datetime import datetime, timezone
import json
import logging
import threading

from core import activity
from core import suggestions
from tools.tool_homeassistant.automations import (
    create_automation,
    delete_automation,
    read_automation,
    update_automation,
)

logger = logging.getLogger(__name__)

# The tool column is NOT NULL and every row says what it is. The panel does not
# read this — it recognises a rollback by `undo_of` — but the log and the raw
# table do.
ROLLBACK_TOOL = "Roll back change"

# What a parked confirmation is recognised by, on its way from the tool through
# the agent loop to the REST handler. Same shape as gate's APPROVAL_MARKER and
# for the same reason — the loop cannot return two kinds of thing without one
# way to tell them apart — but a DIFFERENT key, because the two must never be
# answered by the same code path. See decision 4.
CONFIRMATION_MARKER = "__rollback_confirmation__"

# How long a parked confirmation stays answerable. Same window the gate gives a
# chat approval: long enough for someone to read the question and reply, short
# enough that a stray "yes" hours later cannot undo anything.
CONFIRMATION_TTL_SECONDS = 600


def _restore(automation_id: str, config) -> tuple:
    """Puts a deleted automation back at the id it had."""
    return create_automation(automation_id, config)


def _update(automation_id: str, config) -> tuple:
    """Puts an automation's previous config back."""
    return update_automation(automation_id, config)


def _delete(automation_id: str, config) -> tuple:
    """Removes an automation the agent created. `config` is unused and accepted
    only so all three handlers share one signature."""
    return delete_automation(automation_id)


# op -> (handler, does it need a config, one plain phrase for the message)
#
# The keys are exactly the ops gate._inverse_for and gate._created_inverse
# write. Adding one there means adding it here; anything not in this table is
# refused by `_plan` rather than attempted.
_OPS = {
    "restore_automation": (_restore, True, "put automation {aid} back"),
    "update_automation": (_update, True, "restore the previous configuration of automation {aid}"),
    "delete_automation": (_delete, False, "remove automation {aid}"),
}

# One rollback at a time. Two clicks on the same button, or a panel and an ASI1
# client racing, would otherwise both read `undone_by` as empty and both write.
# The work is one or two HTTP calls, so serialising it costs nothing worth
# measuring.
_lock = threading.Lock()


def describe(inverse) -> str:
    """One plain-text phrase for what running this inverse would do.

    Plain, not markdown, for the same reason gate._describe is: these strings
    end up in a Home Assistant notification as often as in a panel row.
    """
    if not isinstance(inverse, dict):
        return "undo this change"
    entry = _OPS.get(inverse.get("op"))
    if not entry:
        return "undo this change"
    return entry[2].format(aid=inverse.get("automation_id") or "?")


def _check(change):
    """The eligibility rules, over a journal row already read.

    Split out from `_plan` so that listing what CAN be undone and actually
    undoing it ask the same question of the same code. A listing that restated
    these rules would drift from them, and the first the user would know is an
    "undo" the agent offered and then refused.
    """
    change_id = change["id"]
    if change.get("undone_by"):
        return None, (f"Change #{change_id} was already rolled back "
                      f"(see change #{change['undone_by']}).")
    if change.get("status") != "applied":
        return None, (f"Change #{change_id} was never applied to Home Assistant, "
                      "so there is nothing to undo.")

    inverse = change.get("inverse")
    if not isinstance(inverse, dict) or change.get("reversibility") == "one_way":
        # Which of the two this is decides what the user should do about it, so
        # the refusal says which. It is relayed verbatim in chat now, not only
        # shown beside a row they can see the rest of.
        if change.get("tool") == "Call Home Assistant service":
            return None, (f"Change #{change_id} was a service call in a sensitive domain, and "
                          "those are recorded with no inverse on purpose — re-locking a door is "
                          "a new action with its own consequences, not an undo. Ask for the "
                          "action you want instead.")
        return None, (f"Change #{change_id} has no recorded inverse: the agent could not read "
                      "the previous configuration when the change was approved, so there is "
                      "nothing to put back.")

    entry = _OPS.get(inverse.get("op"))
    if not entry:
        # Refused rather than guessed at. See decision 3 above.
        return None, (f"Change #{change_id} records an inverse this version does not know "
                      f"how to perform ({inverse.get('op')!r}). It has not been touched.")
    if not inverse.get("automation_id"):
        return None, f"Change #{change_id} records an inverse with no automation to act on."
    if entry[1] and not isinstance(inverse.get("config"), dict):
        return None, (f"Change #{change_id} records an inverse with no configuration to "
                      "write back, so undoing it would leave the automation empty.")
    return (change, inverse, entry), ""


def _plan(change_id):
    """Works out whether this change can be undone, and how.

    Returns (plan, "") where plan is (change, inverse, entry), or (None, refusal).
    Every refusal is phrased for a person: the panel shows it, the chat relays it
    and the outbox may carry it, so "not eligible" is not an answer.
    """
    change = activity.change(change_id)
    if not change:
        return None, f"There is no change #{change_id} in the journal."
    return _check(change)


def why_not(change) -> str:
    """"" if this row can be rolled back, otherwise the reason it cannot.

    The listing surfaces call this so the model is never told a change is
    undoable by one set of rules and then refused by another.
    """
    if not isinstance(change, dict) or not change.get("id"):
        return "That is not a change in the journal."
    _plan_or_none, refusal = _check(change)
    return refusal


def _redo_for(op: str, automation_id: str):
    """The inverse of the rollback itself, worked out BEFORE it runs.

    Every write is journalled with its inverse; a rollback is a write. For the
    two ops that overwrite or remove something, that means reading what is there
    now — the same trick the gate uses, and for the same reason: afterwards it
    is gone.

    Best-effort on purpose. A rollback must not be blocked because Home
    Assistant would not answer a question about redoing it; the undo is the
    escape hatch, and an escape hatch with a precondition is not one.
    """
    if op == "restore_automation":
        # Putting a deleted automation back; undoing that is deleting it again,
        # and needs nothing read.
        return {"op": "delete_automation", "automation_id": automation_id}, "reversible"
    ok, current = read_automation(automation_id)
    if not ok or not isinstance(current, dict):
        logger.warning(f"ROLLBACK: could not read automation {automation_id} first — "
                       f"the rollback will be recorded without a way back: {current}")
        return None, "one_way"
    redo_op = "update_automation" if op == "update_automation" else "restore_automation"
    return {"op": redo_op, "automation_id": automation_id, "config": current}, "reversible"


def roll_back(change_id, session_id: str = "") -> dict:
    """Undoes one recorded change. Returns {ok, message, rollback_id}.

    `message` is plain text and is the whole answer either way: on refusal it
    says why, on failure it says what Home Assistant said, on success it says
    what was put back. The caller renders it; it does not have to interpret it.
    """
    with _lock:
        plan, refusal = _plan(change_id)
        if plan is None:
            logger.info(f"ROLLBACK: refused change {change_id} — {refusal}")
            return {"ok": False, "message": refusal, "rollback_id": None}

        change, inverse, (handler, _needs_config, _phrase) = plan
        op = inverse["op"]
        automation_id = str(inverse["automation_id"])
        what = describe(inverse)

        redo, reversibility = _redo_for(op, automation_id)
        # A journal that will not accept the row does not stop the undo. It is
        # the other way round from the gate, deliberately: the gate is holding
        # something back and can afford to hesitate, this is letting something
        # go and the home is currently in the state the user wants out of.
        rollback_id = activity.record_rollback(
            undo_of=change["id"],
            tool=ROLLBACK_TOOL,
            args={"change_id": change["id"], "op": op, "automation_id": automation_id},
            session_id=session_id,
            target=automation_id,
            inverse=redo,
            reversibility=reversibility,
        )

        ok, detail = handler(automation_id, inverse.get("config"))

        if not ok:
            activity.record_decision(rollback_id, "pre_authorised", status="failed", result=detail)
            message = (f"Could not roll back change #{change['id']}. Home Assistant refused to "
                       f"{what}: {detail}. The change is still in place — try again, or edit "
                       f"automation {automation_id} in Home Assistant directly.")
            activity.queue("rollback_failed", title="Rollback failed", body=message,
                           urgency="urgent",
                           payload={"change_id": change["id"], "rollback_id": rollback_id,
                                    "automation_id": automation_id, "error": str(detail)[:500]})
            logger.error(f"ROLLBACK: failed to {what} | {str(detail)[:200]}")
            return {"ok": False, "message": message, "rollback_id": rollback_id}

        activity.record_decision(rollback_id, "pre_authorised", status="applied",
                                 result=detail or "rolled back")
        activity.mark_undone(change["id"], rollback_id)
        _answer_the_idea(change["id"], rollback_id)
        message = (f"Change #{change['id']} has been rolled back: the agent ran the "
                   f"recorded inverse to {what}.")
        if detail:
            # A write that landed but did not reload. Said out loud rather than
            # reported as a clean success — the user would otherwise go looking
            # for a change Home Assistant has not picked up yet.
            message += f" One caveat: {detail}"
        activity.queue("rolled_back", title="Change rolled back", body=message,
                       payload={"change_id": change["id"], "rollback_id": rollback_id,
                                "automation_id": automation_id})
        logger.info(f"ROLLBACK: {what} (change {change['id']})")
        return {"ok": True, "message": message, "rollback_id": rollback_id}


def _answer_the_idea(change_id, rollback_id) -> None:
    """Free-hand F-11 (F-WP6): an explorer idea whose change was just undone
    is answered `rolled_back`, so the analyst's next run reads that the
    household took it back.

    Here, and not at the three surfaces, because every one of them — the
    measured result's "roll back" (verify.answer), the Activity tab's undo
    and chat's confirmed rollback (`answer`) — ends in `roll_back`, and a
    fourth surface will too. Called only after the inverse succeeded and the
    original gained `undone_by`: a failed undo is not an answer that took
    effect. It writes an answer and nothing else — no gate, no transition,
    nothing re-asked — and it can never undo or fail the rollback: the home
    is already back, so a store that refuses or raises is logged and the
    rollback reports success as it would have.

    Only a `shown` analyst row takes feedback (suggestions.set_feedback);
    any other change, most of the journal, is left alone without a word."""
    try:
        row = suggestions.for_change(change_id)
        if (not row or row.get("source") != suggestions.SOURCE_ANALYST
                or row.get("evidence_level") != suggestions.EVIDENCE_SHOWN):
            return
        if suggestions.set_feedback(row["id"], suggestions.FEEDBACK_ROLLED_BACK,
                                    rollback_id=rollback_id) is None:
            logger.warning(f"ROLLBACK: change {change_id} is undone, but suggestion "
                           f"{row['id']} could not be answered rolled_back")
    except Exception as e:  # noqa: BLE001 — the undo has landed; nothing here may unsay it
        logger.error(f"ROLLBACK: change {change_id} is undone, but recording it on its "
                     f"suggestion failed: {e}")


# --------------------------------------------------------------------------
# The conversational path: confirming the SELECTION.
#
# Decision 4. Everything above is about the operation and never asks anybody
# anything; everything here is about which change was meant, and asks exactly
# once. `roll_back` above is reachable from the panel with no confirmation at
# all — that is not an inconsistency, it is the split: there, the user picked
# the row themselves.
#
# Nothing in this section imports the model, the tool registry or the gate. It
# builds a question, holds it on the session, and reads a decision it is handed.
# --------------------------------------------------------------------------

def _now() -> int:
    """UTC seconds. Its own, rather than gate.now_ts, because importing the gate
    would drag langchain into the one path that must not need it."""
    return int(datetime.now(tz=timezone.utc).timestamp())


def _local_time(ts) -> str:
    """A unix second in the home's local time, for a question a person reads."""
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%d %H:%M")


def prepare(change_id, summary: str = "") -> dict:
    """Plans a rollback and phrases the question, WITHOUT running anything.

    This is what the chat tool returns. Either a refusal the model relays as-is,
    or a confirmation payload the agent loop hands to the REST layer to park.
    Nothing here writes to Home Assistant and nothing here writes to the
    journal: a question the user has not answered yet is not a change.

    `summary` is what the change itself was, in the same words the panel and the
    notification use — supplied by the caller because it comes from core/gate.py,
    which this module may not import. Optional: the question is unambiguous
    without it, because the effect below names the automation.
    """
    plan, refusal = _plan(change_id)
    if plan is None:
        return {
            "rolled_back": False,
            "reason": refusal,
            "note": "Nothing was changed. Relay this reason to the user in full — "
                    "it says what can be done about it.",
        }

    change, inverse, _entry = plan
    effect = describe(inverse)
    applied = _local_time(change.get("decided_at") or change.get("proposed_at"))
    question = (
        f"Roll back change #{change['id']}"
        + (f" ({summary})" if summary else "")
        + (f", applied {applied}" if applied else "")
        + f"? The agent will {effect}."
    )
    return {
        CONFIRMATION_MARKER: True,
        "change_id": change["id"],
        "question": question,
        "effect": effect,
    }


def confirmation_in(tool_result):
    """The confirmation payload inside a tool answer, or None.

    The agent loop calls this on every tool result. Recognising the request in
    the RESULT rather than trusting an argument is the point: a tool result is
    built by this process, so the model cannot forge one — whereas a `confirmed`
    flag in the tool's arguments would be the model asserting that a question it
    never asked was answered.
    """
    if not tool_result or CONFIRMATION_MARKER not in str(tool_result):
        return None
    try:
        parsed = json.loads(tool_result)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) and parsed.get(CONFIRMATION_MARKER) else None


def park_confirmation(memory: dict, parsed: dict, session_id: str = "") -> str:
    """Holds the question on the session and returns the text to send.

    Deliberately NOT memory["pending_approval"]: that slot is answered by
    gate.apply, and a rollback must never reach it (decision 4). Two slots, two
    answering paths, no chance of one being mistaken for the other.

    Nothing is journalled here. The gate writes its row when a change is parked
    because an unanswered request to WRITE is itself worth a trace; an
    unanswered question about undoing is not — nothing was proposed and nothing
    would have happened.
    """
    memory["pending_rollback"] = {
        "change_id": parsed.get("change_id"),
        "question": parsed.get("question", ""),
        "created_at": _now(),
    }
    return f"{parsed.get('question', '')}\n\nReply YES to confirm or NO to cancel."


def confirmation_expired(pending: dict) -> bool:
    """True if a parked question is too old to still be answerable."""
    created_at = (pending or {}).get("created_at")
    if not created_at:
        return False
    return _now() - created_at > CONFIRMATION_TTL_SECONDS


def reask(pending: dict) -> str:
    """What to say when the reply was not a decision.

    The question is repeated whole rather than shortened to "yes or no?": a
    one-word reply has to be an answer to something the user can still see
    (§9), and by this point the original question may be several messages up.
    """
    return (f"{(pending or {}).get('question', '')}\n\n"
            "Reply YES to confirm or NO to cancel. Nothing has been changed.")


def answer(pending: dict, decision: bool, session_id: str = "") -> str:
    """Resolves a parked question. Returns the plain text to send back.

    The ONLY chat path that reaches `roll_back`, and it passes the real session
    id through so activity.source_for records which surface asked. A
    chat-initiated rollback logged as 'panel' would be a false audit trail.

    The success and failure text is `roll_back`'s own `message`, verbatim. It is
    written to be the whole answer — on failure it names the automation and what
    Home Assistant said — and §11 requires that to reach the user actionable
    rather than summarised into "that did not work".
    """
    change_id = (pending or {}).get("change_id")
    if not decision:
        logger.info(f"ROLLBACK: the user declined to undo change {change_id}")
        return f"Left as it is. Change #{change_id} was not rolled back."
    return roll_back(change_id, session_id=session_id)["message"]


def close_interrupted() -> int:
    """Settles rollbacks a restart caught mid-flight, and says so. Returns how
    many.

    Called at startup, alongside the approvals that were orphaned the same way.
    A rollback with no outcome recorded is one nobody can tell succeeded from
    failed, and the safe reading of "we do not know whether the undo landed" is
    the loud one.
    """
    abandoned = activity.abandon_running_rollbacks()
    for row in abandoned:
        args = row.get("args") or {}
        activity.queue(
            "rollback_failed", title="Rollback interrupted",
            body=(f"The add-on restarted while rolling back change "
                  f"#{args.get('change_id', '?')}. It is not known whether the undo landed — "
                  f"check automation {args.get('automation_id', '?')} in Home Assistant, "
                  "and roll the change back again if it is still in place."),
            urgency="urgent",
            payload={"change_id": args.get("change_id"), "rollback_id": row.get("id")},
        )
    return len(abandoned)
