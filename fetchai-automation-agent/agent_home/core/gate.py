"""The approval gate: the one place a destructive change is described, put to
the user, executed, and written down.

Design §5 asks for this to be a component rather than a habit — "every write
passes through it; nothing reaches Home Assistant without a recorded approval
and a recorded inverse operation. One component, not a convention repeated at
ten call sites — that is the difference between reversible as a property and
reversible as a promise."

It used to be a predicate called from three places in the agent loop, with the
approved tool executed in a fourth, and nothing recorded anywhere. The predicate
still lives here, but so does `apply` — and `apply` is the only path that runs an
approved tool. Both surfaces reach it through rest_chat.handle_approve, so
neither can grow its own copy.

`apply` also claims the row before touching Home Assistant, atomically, and
refuses to run a second time if it already has. The check upstream —
`decision == 'proposed'` — is read well before this ever runs, so two
concurrent answers to the same change (a double-tapped Apply, or the approval
card and the panel answering the same agent-raised proposal) can both pass it.
The claim is the actual boundary: a conditional UPDATE against
`decision = 'proposed'`, exactly one caller can win it, and the loser executes
nothing. See `activity.claim_decision` and `activity.abandon_orphaned_approvals`
for the shape this borrows — it is the same "claimed but not yet settled" pair
a rollback caught mid-flight already leaves, reaped the same way at startup.

Nothing here rolls anything back — core/rollback.py does, and that is the whole
division of labour. What happens here is that the inverse of every write is
worked out while the information is still to hand (the prior config is fetched
anyway, to render the diff) and stored with the change. An inverse worked out
afterwards would be a guess.

The inverse is written as an integration-level operation — restore_automation,
not "Create Home Assistant automation" — deliberately. Undoing a change must
never depend on the model being available, or willing, so what is recorded here
names something the integration can do, not something a tool can be asked to do.

Rolling one back does NOT come back through this gate. core/rollback.py argues
why: the operation was decided here, at approval time, and returning the home to
a state the user already approved of is not a new decision to put to them.

TWO WAYS IN, ONE GATE. `park_approval` is the conversational one: a model asked
for something, a user is waiting, and the pending change is held in that
session's memory because the answer is seconds away. `propose` is the other, and
it is what a change the agent raised itself comes through — no user message
behind it, no conversation to resume, and possibly days before anyone looks.

The difference between them is where the pending lives, and it is not a detail.
Session memory is in-process, evicted after an hour and gone on restart; a
proposal that may wait a week cannot be kept there, so it is not kept anywhere.
`pending_for` rebuilds it from the journal row instead — the row already carries
the tool, the arguments, the target, both configs, the inverse and the
reversibility, which is the pending dict minus exactly the three fields that
describe a conversation. `apply` therefore cannot tell the two apart, and that is
the point: one path executes an approved change, and it is the same one whether
the change was asked for by a person or found by the agent.

What the sessionless path does NOT get out of is validation. `propose` preflights
an automation config exactly as the conversational path does, before the user is
shown anything — the proposer validates, it does not trust its caller.

This module does not know what a suggestion is, and must not learn. It proposes
a change and hands back an id; whoever raised it links its own record to that id.
Same boundary as rollback: the gate knows how to name an inverse, not who wanted
one.
"""
from datetime import datetime, timezone
import json
import logging

import yaml

from core import activity
# Imported for describe_change below, and the direction is safe: rollback.py
# imports activity and the Home Assistant integration functions, never this
# module. Nothing about "rolling back must not need the model" is weakened by
# the gate knowing how to name one.
from core import rollback

from langchain_core.messages import AIMessage

from tools.tool_homeassistant._client import _ha_get
from tools.tool_homeassistant.actions import is_sensitive_service

logger = logging.getLogger(__name__)

DESTRUCTIVE_TOOLS = {
    "Delete Home Assistant automation",
    "Update Home Assistant automation",
    "Create Home Assistant automation",
}

SERVICE_CALL_TOOL = "Call Home Assistant service"

# Marker the REST chat response uses to hand the approval payload to the panel.
APPROVAL_MARKER = "[APPROVAL_REQUIRED]"

_CREATE = "Create Home Assistant automation"
_UPDATE = "Update Home Assistant automation"
_DELETE = "Delete Home Assistant automation"


def now_ts() -> int:
    """The one clock every session and approval timestamp is written and read
    against.

    Deliberately UTC, and deliberately shared. These ints are written by the
    REST handlers in rest.rest_chat and read back by tasks.scheduled_tasks to
    evict idle sessions, so a naive datetime.now() in any one of those places
    shifts that eviction window by the host's UTC offset — a session evicted
    hours early or hours late depending on the sign. Home Assistant sets TZ in
    the add-on container, so that offset is normally not zero.
    """
    return int(datetime.now(tz=timezone.utc).timestamp())


def _is_destructive(tool_name: str, tool_args: dict) -> bool:
    """Returns True if this tool call needs user approval before executing:
    an HA automation create/update/delete, or a service call in a domain where
    a wrong call is not trivially undone (see SENSITIVE_SERVICE_DOMAINS).

    Everyday service calls — lights, switches, media, climate — run without a
    confirmation step: asking twice for "turn off the kitchen light" is worse
    than the mistake it would prevent."""
    if tool_name in DESTRUCTIVE_TOOLS:
        # Don't treat as destructive if config_json is invalid — let the LLM self-correct silently
        if "config_json" in tool_args:
            try:
                json.loads(tool_args["config_json"])
            except Exception:
                return False
        return True
    if tool_name == SERVICE_CALL_TOOL:
        return is_sensitive_service(tool_args.get("service", ""))
    return False


def _automation_id(tool_args: dict) -> str:
    return str(tool_args.get("automation_id", "")).removeprefix("automation.")


def _target(tool_name: str, tool_args: dict) -> str:
    """What this change is about, so a target's history reads back as one story."""
    if tool_name in (_UPDATE, _DELETE):
        return _automation_id(tool_args)
    if tool_name == SERVICE_CALL_TOOL:
        return str(tool_args.get("entity_id", "") or tool_args.get("service", ""))
    return ""


def _describe(tool_name: str, tool_args: dict) -> str:
    """One plain-text phrase naming the change, used in every message about it.

    Plain, not markdown: §4 makes the chat rendering the design constraint, and
    these strings end up in a Home Assistant notification as often as in a
    panel row.
    """
    if tool_name == _CREATE:
        try:
            alias = json.loads(tool_args.get("config_json", "{}")).get("alias", "")
        except Exception:
            alias = ""
        return f"create the automation '{alias}'" if alias else "create an automation"
    if tool_name == _UPDATE:
        return f"update automation {_automation_id(tool_args)}"
    if tool_name == _DELETE:
        return f"delete automation {_automation_id(tool_args)}"
    if tool_name == SERVICE_CALL_TOOL:
        service = tool_args.get("service", "unknown")
        target = tool_args.get("entity_id", "")
        return f"call {service}" + (f" on {target}" if target else "")
    return tool_name


def describe_change(row: dict) -> str:
    """One plain-text line for a journal row, whatever kind of row it is.

    The single describer, for the same reason `_describe` is: the panel row, the
    chat listing, the notification and the audit table are all naming the same
    event, and four renderings that can drift is four chances to name it
    differently. A rollback row is not a tool call and has no tool name to
    recognise, so it describes itself from `undo_of` and its recorded inverse.
    """
    if row.get("undo_of"):
        return (f"roll back change #{row['undo_of']} — "
                f"{rollback.describe(row.get('args') or {})}")
    return _describe(row.get("tool", ""), row.get("args") or {})


def _inverse_for(tool_name: str, tool_args: dict, old_config):
    """The operation that would undo this one, as (inverse, reversibility).

    A creation is reversible but its inverse cannot be written yet — the
    automation id is minted by Home Assistant when the create runs — so it comes
    back as None here and is completed in `apply`.

    A sensitive service call gets no inverse at all, and that is the honest
    answer rather than a missing feature: locking a door you just unlocked is a
    new action with its own consequences, not an undo. Recording it as one would
    make the rollback rate the design measures meaningless.
    """
    if tool_name == _CREATE:
        return None, "reversible"
    if tool_name in (_UPDATE, _DELETE) and old_config:
        op = "update_automation" if tool_name == _UPDATE else "restore_automation"
        return {"op": op, "automation_id": _automation_id(tool_args), "config": old_config}, "reversible"
    return None, "one_way"


def _preflight_validate(tool_name: str, tool_args: dict):
    """Test-create a temp automation to validate schema before showing the approval dialog.
    Returns an error string if invalid (to be injected as ToolMessage), or None if ok."""
    from tools.tool_homeassistant.automations import _preflight_automation
    if tool_name in (_CREATE, _UPDATE):
        return _preflight_automation(tool_args.get("config_json", ""))
    return None


def _format_approval_message(tool_name: str, tool_args: dict) -> str:
    """Builds the human-readable confirmation message shown to the user
    before a destructive tool call is executed."""
    if tool_name == _DELETE:
        aid = tool_args.get("automation_id", "unknown")
        return f"⚠️ **Confirm deletion:** Remove automation **{aid}**. This is irreversible."
    elif tool_name == _UPDATE:
        aid = tool_args.get("automation_id", "unknown")
        return f"⚠️ **Confirm update:** Modify automation **{aid}** with new configuration."
    elif tool_name == _CREATE:
        try:
            cfg = json.loads(tool_args.get("config_json", "{}"))
            alias = cfg.get("alias", "unknown")
        except:
            alias = "unknown"
        return f"⚠️ **Confirm creation:** Create new automation **{alias}**."
    elif tool_name == SERVICE_CALL_TOOL:
        service = tool_args.get("service", "unknown")
        target = tool_args.get("entity_id", "") or "no specific entity"
        return f"⚠️ **Confirm action:** Call **{service}** on **{target}**."
    return f"⚠️ **Confirm action:** {tool_name}"


def _change_payload(tool_name: str, tool_args: dict):
    """Everything about a proposed change that is not about the conversation:
    (payload, None), or (None, preflight_error) if the config would not validate.

    Split out from `_build_destructive_approval` so that both entry points build
    the same thing. What a chat approval adds on top is a tool_call_id; what an
    agent-raised proposal adds is nothing at all. Neither may end up with its own
    idea of what the inverse of a delete is.

    The prior configuration is fetched once and used twice: rendered as YAML for
    the diff the user reads, and kept as the structure the inverse operation is
    built from. Fetching it again at rollback time would read a config that has
    already been overwritten.

    This capture is a snapshot, not a lock — and `apply` may run seconds after
    it (the conversational path's window is bounded by
    agent_runtime.APPROVAL_TTL_SECONDS, 600s) or days after it (a change
    `propose` raised has no such bound). If the automation is edited in Home
    Assistant in that window, the diff the user approved no longer describes
    what `apply` would do, and the inverse captured here would "undo" to the
    pre-edit config — overwriting the user's edit twice. `apply` re-reads the
    live config and refuses on drift rather than re-diffing and proceeding; see
    its docstring for why refusing is also what keeps this snapshot honest.
    """
    preflight_error = _preflight_validate(tool_name, tool_args)
    if preflight_error:
        return None, preflight_error

    old_yaml = ""
    new_yaml = ""
    old_config = None
    if tool_name in (_UPDATE, _DELETE):
        ok, data = _ha_get(f"/config/automation/config/{_automation_id(tool_args)}")
        if ok:
            old_config = data
            try:
                old_yaml = yaml.dump(data, allow_unicode=True, sort_keys=False)
            except Exception:
                old_yaml = str(data)
    if tool_name in (_CREATE, _UPDATE):
        try:
            cfg = json.loads(tool_args.get("config_json", "{}"))
            new_yaml = yaml.dump(cfg, allow_unicode=True, sort_keys=False)
        except Exception:
            new_yaml = tool_args.get("config_json", "")

    inverse, reversibility = _inverse_for(tool_name, tool_args, old_config)
    return {
        "tool_name": tool_name,
        "tool_args": tool_args,
        "old_yaml": old_yaml,
        "new_yaml": new_yaml,
        # Journalling detail, not shown to anyone: the proposal record reads
        # these, and the payload the panel receives is assembled separately.
        "target": _target(tool_name, tool_args),
        "inverse": inverse,
        "reversibility": reversibility,
    }, None


def _build_destructive_approval(tool_name: str, tool_args: dict, tool_call: dict):
    """For an already-confirmed-destructive tool call, returns (approval_payload, None)
    if approval is required, or (None, preflight_error) if preflight validation failed.
    """
    payload, preflight_error = _change_payload(tool_name, tool_args)
    if preflight_error:
        return None, preflight_error
    return json.dumps({
        "__approval_required__": True,
        "tool_call_id": tool_call["id"],
        **payload,
    }), None


def park_approval(memory: dict, parsed: dict, ai_msg, messages_snapshot: list,
                  session_id: str = "") -> tuple:
    """Parks a destructive tool call on the session, records it, and builds the
    reply that carries it to the client, returning (friendly, full_response).

    Both REST entry points need this: /rest/post when the agent first asks for
    confirmation, and /rest/approve when answering one approval turns up
    another. They had a copy each, which is how the session timestamp below
    came to be written on two different clocks — so the assembly lives here
    now, once, and neither path can drift from the other again.

    `messages_snapshot` is the history to resume from once the user answers; it
    deliberately excludes the confirmation prompt itself, which is appended to
    the live history only.

    The change is written to the journal HERE, before the user has answered, so
    that a request raised in ASI1 is visible in the panel while it is still open
    — and so that an unanswered one leaves a trace rather than evaporating with
    the session.
    """
    snapshot = list(messages_snapshot)
    tool_name = parsed["tool_name"]
    tool_args = parsed["tool_args"]

    change_id = activity.record_proposal(
        tool=tool_name,
        args=tool_args,
        session_id=session_id,
        target=parsed.get("target", ""),
        old_config=parsed.get("old_yaml", ""),
        new_config=parsed.get("new_yaml", ""),
        inverse=parsed.get("inverse"),
        reversibility=parsed.get("reversibility", "one_way"),
    )
    notice_id = activity.queue(
        "approval_request",
        title="Approval needed",
        body=f"The agent is waiting for permission to {_describe(tool_name, tool_args)}.",
        payload={"change_id": change_id, "tool": tool_name},
    )

    # Also written to the row, not only to the session. The column means the
    # same thing on both paths — which message asked this question — and a
    # question that can only be withdrawn by the process that asked it is one
    # more thing lost with the session.
    activity.attach_notice(change_id, notice_id)

    memory["pending_approval"] = {
        "tool_name": tool_name,
        "tool_args": tool_args,
        "tool_call_id": parsed["tool_call_id"],
        "messages_snapshot": snapshot,
        "ai_message": ai_msg,
        "old_yaml": parsed.get("old_yaml", ""),
        "new_yaml": parsed.get("new_yaml", ""),
        # The journal row this session is answering. Carried on the session so
        # the decision lands on the row the proposal opened, rather than on a
        # new one.
        "change_id": change_id,
        # The "waiting for permission" message, so answering can withdraw it
        # rather than leaving a settled question to be collected later.
        "notice_id": notice_id,
        "reversibility": parsed.get("reversibility", "one_way"),
        # Chat clients answer this by text, so it needs its own expiry.
        "created_at": now_ts(),
    }
    friendly = _format_approval_message(tool_name, tool_args)
    approval_json = json.dumps({
        "tool_name": tool_name,
        "tool_args": tool_args,
        "old_yaml": parsed.get("old_yaml", ""),
        "new_yaml": parsed.get("new_yaml", ""),
    })
    memory["messages"] = snapshot + [AIMessage(content=friendly)]
    memory["timestamp"] = now_ts()
    return friendly, f"{friendly}\n\n{APPROVAL_MARKER}{approval_json}"


# --------------------------------------------------------------------------
# The sessionless entry.
#
# Everything above assumes a conversation: a model asked for something and a
# user is waiting for the answer. Below is the other way in, for a change the
# agent raised itself — nobody asked, nobody is waiting, and the answer may come
# from a different surface days later. The journal row is the only thing that
# carries it, so it has to carry all of it.
# --------------------------------------------------------------------------

# What may be proposed this way: the vocabulary of a change, which is not the
# same question as what needs approving.
#
# `_is_destructive` deliberately lets everyday service calls run immediately,
# because asking twice for "turn off the kitchen light" is worse than the
# mistake it prevents. That exemption is about a request the USER made. Nothing
# the agent raises by itself is exempt from anything — design §3 makes acting
# autonomously a non-goal, "the agent proposes; the user decides" — so a
# proposal is put to the user whatever domain it touches. The check here is
# therefore narrower in a different direction: it refuses a tool the gate could
# not describe, invert or record, rather than one it would not gate.
PROPOSABLE_TOOLS = DESTRUCTIVE_TOOLS | {SERVICE_CALL_TOOL}


def propose(tool_name: str, tool_args: dict, *, source: str = None, actor: str = "",
            notice: str = "") -> dict:
    """Raises a change with no session behind it. Returns
    {"ok": True, "change_id", "summary"} or {"ok": False, "error"}.

    `source` is AGENT_SOURCE (the agent's own suggestion) unless a caller states
    PEER_SOURCE — another uAgent's proposal (handlers/peer_handlers.py), with its
    address as `actor`. Only a sessionless source is accepted: both live in the
    journal row alone, and one that could name a chat surface would be answerable
    twice. `notice` replaces the outbox text; the peer path passes one built
    only from checked facts, so nothing another agent wrote reaches the outbox
    — which ASI1's "what's new" hands to the model.

    The whole question is written to the journal and nowhere else: no session
    memory, no in-process dict, nothing that a restart or an hour's inactivity
    takes with it. `pending_for` reads it back whenever the answer arrives.

    Validated here, before anybody is shown anything. An automation config goes
    through the same `_preflight_automation` test-create a model-raised one does
    — the proposing path does not trust its caller, and a proposal the user
    approves must not then fail because it was never valid. A config that will
    not validate comes back as an error to whoever raised it, which is the same
    place a model-raised one goes: back to the thing that wrote it.

    The outbox message is queued the same way `park_approval` queues one, for
    the same reason and with the same withdrawal: §4's core "emits a message and
    never learns which channel carried it". What this function does NOT do is
    know why the change was worth proposing. It returns the change_id and stops
    there; whatever raised it links its own record to that id.
    """
    if tool_name not in PROPOSABLE_TOOLS:
        return {"ok": False, "error": f"{tool_name!r} is not a change this gate can propose."}
    source = activity.AGENT_SOURCE if source is None else source
    if source not in activity.SESSIONLESS_SOURCES:
        return {"ok": False, "error": f"{source!r} is not a source a proposal can have."}

    payload, preflight_error = _change_payload(tool_name, tool_args)
    if preflight_error:
        logger.info(f"GATE: refused to propose {tool_name} — {str(preflight_error)[:200]}")
        return {"ok": False, "error": preflight_error}

    # `source` stated explicitly rather than encoded in a fake session id
    # (FIX 2): AGENT_SESSION used to be passed as `session_id` here purely so
    # `source_for` would recognise it and derive AGENT_SOURCE — but
    # `source_for` reads real, client-influenced session ids everywhere else,
    # and a value that grants the agent source has no business being
    # derivable from a string a client could also send. There genuinely is no
    # session behind this, so `actor` is left at its default "" rather than
    # carrying a sentinel that named nothing real.
    change_id = activity.record_proposal(
        tool=tool_name,
        args=tool_args,
        source=source,
        actor=actor,
        target=payload["target"],
        old_config=payload["old_yaml"],
        new_config=payload["new_yaml"],
        inverse=payload["inverse"],
        reversibility=payload["reversibility"],
    )
    if not change_id:
        # On the conversational path a journal that will not accept the row
        # still leaves a user holding a question they can answer. Here the row
        # IS the question, so there is nothing left to put to anyone.
        return {"ok": False, "error": "The change could not be recorded, so it was not proposed."}

    what = _describe(tool_name, tool_args)
    notice_id = activity.queue(
        "approval_request",
        title="Approval needed",
        body=notice or f"The agent is asking for permission to {what}.",
        payload={"change_id": change_id, "tool": tool_name},
    )
    activity.attach_notice(change_id, notice_id)
    logger.info(f"GATE: proposed — {what} (change {change_id})")
    return {"ok": True, "change_id": change_id, "summary": what}


def close_interrupted() -> int:
    """Settles approvals a restart caught mid-write, and says so. Returns how
    many.

    FIX 1's reaper, a sibling of rollback.close_interrupted() and read against
    the same reasoning: `apply` now claims a row (moves `decision` off
    'proposed') before it calls `execute`, so a crash between the claim and the
    final `record_decision` leaves a row nobody can tell "the write landed"
    from "it didn't" — activity.abandon_orphaned_approvals() is the query for
    exactly that pair. Loud rather than guessed at, same as a rollback caught
    the same way: §11 makes "we do not know" the one thing that must not stay
    quiet. Called at startup, alongside expire_stale_proposals() and
    rollback.close_interrupted() — distinct from both: this is neither "nobody
    answered" (that is still expire_stale_proposals()'s row to close) nor a
    rollback (that is undo_of IS NOT NULL, the other half of the same journal).
    """
    abandoned = activity.abandon_orphaned_approvals()
    for row in abandoned:
        what = _describe(row.get("tool", ""), row.get("args") or {})
        activity.queue(
            "change_failed", title="Change interrupted",
            body=(f"The add-on restarted while trying to {what}. It is not known whether the "
                  "write landed — check Home Assistant directly before assuming either way."),
            urgency="urgent",
            payload={"change_id": row.get("id")},
        )
    return len(abandoned)


def withdraw(change_id, reason) -> bool:
    """Settles an open sessionless proposal so nobody can answer it, and takes
    its question out of the outbox. Returns whether it was still open.

    Claimed with activity.claim_decision rather than written straight to
    `expired`, for the reason that function exists: the user may be answering
    it at this instant, and exactly one of the two may win. Losing means the
    proposal was answered under us, and there is nothing to withdraw. The
    review withdraws a suggestion's question this way, and the panel an agent's
    proposals when that agent loses the right to make them.
    """
    if not change_id:
        return False
    if not activity.claim_decision(change_id, "expired"):
        logger.info(f"GATE: change {change_id} was answered before it could be withdrawn")
        return False
    row = activity.change(change_id) or {}
    activity.withdraw(row.get("notice_id"))
    activity.record_decision(change_id, "expired", status="not_applied", result=str(reason)[:500])
    return True


def pending_for(change_id):
    """Rebuilds the pending change from its journal row, for an answer arriving
    with no session behind it. Returns (pending, "") or (None, reason).

    This is the whole trick, and it is why `apply` needed no change: the row
    already holds the tool, its arguments, the target, both configs, the inverse
    and the reversibility — the pending dict minus exactly the three fields that
    describe a conversation (messages_snapshot, ai_message, tool_call_id), which
    an agent-raised change does not have and must not be given invented ones.

    Note what is deliberately absent: `created_at`. `approval_expired` reads that
    field and answers False without it, which is the intended behaviour rather
    than a hole. The TTL exists because a panel session can sit open for an hour
    and a stray click should not fire a delete; a proposal the agent raised is
    MEANT to wait, and expiring it on a clock nobody is watching would silently
    throw away the question. It should not live forever either — one open
    proposal per entity, withdrawn when it stops being worth asking, is the
    reasoning schedule's job, and there is deliberately no second expiry here.

    Only an agent-raised row answers this way. A session-backed proposal is
    answered through its session, where the conversation to resume still is —
    and letting it be answered twice, once by id and once by session, would run
    the tool twice.
    """
    row = activity.change(change_id)
    if not row:
        return None, f"There is no change #{change_id} in the journal."
    if row.get("source") not in activity.SESSIONLESS_SOURCES:
        return None, (f"Change #{change_id} was raised in a conversation, and is answered "
                      "there rather than by id.")
    if row.get("decision") != "proposed":
        return None, (f"Change #{change_id} has already been answered "
                      f"({row.get('decision')}). Nothing was done.")
    return {
        "tool_name": row.get("tool", ""),
        "tool_args": row.get("args") or {},
        "old_yaml": row.get("old_config") or "",
        "new_yaml": row.get("new_config") or "",
        "change_id": row.get("id"),
        "notice_id": row.get("notice_id"),
        "reversibility": row.get("reversibility") or "one_way",
    }, ""


def _failed(tool_result: str) -> bool:
    """True if a tool reported an error rather than doing the thing.

    The tools answer with JSON and put failures in an 'error' key; anything that
    is not JSON at all is a raised exception's message, which is also a failure.
    """
    try:
        parsed = json.loads(tool_result)
    except Exception:
        return True
    return isinstance(parsed, dict) and "error" in parsed


def _drift_refusal(tool_name: str, tool_args: dict, captured_old_yaml: str):
    """None if Home Assistant's live config still matches what was captured at
    propose time, or the refusal to hand back otherwise.

    FIX 3. Scoped to UPDATE and DELETE, and only when there IS a captured
    old_config to compare — a CREATE has none, there is nothing to have drifted
    from. Re-fetches rather than trusting the capture in `pending`, because the
    capture is a snapshot taken once in `_change_payload` and `apply` runs
    anywhere from seconds later (conversational approvals, bounded by
    agent_runtime.APPROVAL_TTL_SECONDS) to days later (a `propose`d change,
    unbounded) — and this function does not get to know which, by design (see
    `apply`'s docstring: it cannot tell which entry a change came through, and
    must not start needing to). So it always re-checks; the two windows differ
    only in how likely drift actually is; the check is not.

    Matches core/rollback.py's `_check` in shape and register: a plain-text
    refusal phrased for the person reading it, not a raised exception. Refusing
    is the cheap fix, not a consolation prize — it is also what keeps the
    recorded inverse honest, since that inverse was worked out from the same
    snapshot this refuses to trust silently.
    """
    if tool_name not in (_UPDATE, _DELETE) or not captured_old_yaml:
        return None
    ok, current = _ha_get(f"/config/automation/config/{_automation_id(tool_args)}")
    if not ok:
        # Cannot confirm either way — let the write itself surface whatever is
        # actually wrong (deleted automation, unreachable HA) rather than
        # inventing a drift refusal for a question this could not answer.
        return None
    try:
        captured = yaml.safe_load(captured_old_yaml) or {}
    except Exception:
        return None
    if current != captured:
        aid = _automation_id(tool_args)
        return (f"Automation {aid} has changed in Home Assistant since this was asked about — "
                "it looks like someone edited it by hand. Nothing was written, to avoid silently "
                "overwriting that edit. Ask again to see where it stands now.")
    return None


def _created_inverse(tool_result: str):
    """The inverse of a creation, once Home Assistant has minted the id."""
    try:
        created = json.loads(tool_result)
    except Exception:
        return None
    automation_id = (created or {}).get("automation_id") if isinstance(created, dict) else None
    if not automation_id:
        return None
    return {"op": "delete_automation", "automation_id": str(automation_id)}


async def apply(pending: dict, approved: bool, session_id: str, execute) -> str:
    """Carries out the user's decision on a parked change, and writes it down.

    THE only path that executes an approved destructive tool. `execute` is the
    tool runner, passed in rather than imported: the gate decides what may run
    and records what did, and it does not need to own the registry to do that —
    while injecting it keeps core.agent_runtime free to import this module.

    The row is claimed atomically before anything else happens (FIX 1): a
    conditional UPDATE that moves `decision` off 'proposed', with `status`
    left NULL. Both entries into this function reach the same journal row and
    the same claim — the session path clears memory["pending_approval"]
    synchronously before this is ever awaited, so it never actually races
    another caller here, but the invariant belongs in the one place that
    executes rather than being trusted to stay true at every call site that
    reaches it. A change with no `change_id` is not claimed at all — that
    should not happen on either entry, and skipping the claim leaves it exactly
    as unprotected as it always was rather than failing in a new way.

    Returns the tool result exactly as the resumed agent loop should see it,
    including the TOOL_FAILED prefix that stops the model reporting a failure as
    a success.
    """
    change_id = pending.get("change_id")
    tool_name = pending["tool_name"]
    tool_args = pending["tool_args"]
    what = _describe(tool_name, tool_args)

    if change_id and not activity.claim_decision(change_id, "approved" if approved else "rejected"):
        logger.info(f"GATE: change {change_id} was already answered — refusing a second execution")
        return json.dumps({"error": "This change has already been answered."})

    # The question has been answered; the message asking it must not go out.
    activity.withdraw(pending.get("notice_id"))

    if not approved:
        activity.record_decision(change_id, "rejected", status="not_applied")
        activity.queue("change_rejected", title="Change rejected",
                       body=f"You declined to {what}. Nothing was changed.",
                       payload={"change_id": change_id})
        logger.info(f"GATE: rejected — {what}")
        return json.dumps({"error": "User rejected this action."})

    # FIX 3: refuse rather than write over a hand-made edit, or a config that
    # has gone stale since it was preflighted. See _drift_refusal and
    # _change_payload's docstring for the argument.
    refusal = _drift_refusal(tool_name, tool_args, pending.get("old_yaml", ""))
    if refusal is None:
        refusal = _preflight_validate(tool_name, tool_args)
    if refusal:
        activity.record_decision(change_id, "refused", status="not_applied", result=refusal)
        activity.queue("change_refused", title="Change refused",
                       body=f"Could not {what}: {refusal}",
                       payload={"change_id": change_id})
        logger.warning(f"GATE: refused at apply time — {what} | {str(refusal)[:200]}")
        return json.dumps({"error": refusal})

    tool_result = await execute(tool_name, tool_args)

    if _failed(tool_result):
        activity.record_decision(change_id, "approved", status="failed", result=tool_result)
        activity.queue("change_failed", title="Change failed",
                       body=f"Tried to {what}, and Home Assistant refused. Nothing was changed.",
                       payload={"change_id": change_id, "result": str(tool_result)[:500]})
        logger.warning(f"GATE: failed — {what} | {str(tool_result)[:200]}")
        return f"TOOL_FAILED: {tool_result}"

    inverse = _created_inverse(tool_result) if tool_name == _CREATE else None
    activity.record_decision(change_id, "approved", status="applied",
                             result=tool_result, inverse=inverse)
    undoable = pending.get("reversibility") != "one_way" and tool_name in DESTRUCTIVE_TOOLS
    activity.queue(
        "change_applied", title="Change applied",
        body=f"You approved and the agent did {what}."
             + (" This can be undone." if undoable else ""),
        payload={"change_id": change_id},
    )
    logger.info(f"GATE: applied — {what}")
    return tool_result


def readback(pending: dict, approved: bool, tool_result: str) -> str:
    """What actually happened, in plain words, with no model in the loop.

    Design §9: "every approval is echoed back with what actually happened,
    because a one-word reply carries no record of what it agreed to". On the
    conversational path the model writes that echo, from the tool result, in the
    conversation it was already having. An agent-raised change has no such
    conversation — so this is a factual readback of the three outcomes `apply`
    can produce, and nothing else. It states what was done; it does not argue
    for it, and it is not the richer artefact the suggestion surfaces will want.

    Plain text, not markdown, for the reason `_describe` is: this is as likely
    to be read in a Home Assistant notification as on a screen.
    """
    what = _describe(pending.get("tool_name", ""), pending.get("tool_args") or {})
    change_id = pending.get("change_id")
    if not approved:
        return f"Nothing was changed. You declined to {what}."
    # `apply` returns the tool result on success and prefixes a failure, so the
    # same reading that decided which outcome to record decides what to say
    # about it — rather than a second opinion here that could disagree.
    if _failed(tool_result):
        return (f"Tried to {what}, and it did not go through. Nothing was changed. "
                f"Home Assistant said: {str(tool_result).removeprefix('TOOL_FAILED: ')[:300]}")
    undoable = pending.get("reversibility") != "one_way" and pending.get("tool_name") in DESTRUCTIVE_TOOLS
    done = f"Done — the agent did {what}."
    if change_id:
        done += f" It is change #{change_id} in the record"
        done += ", and can be undone." if undoable else "."
    return done
