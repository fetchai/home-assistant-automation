"""REST handlers for the chat / approval flow: /rest/post and /rest/approve.

Three kinds of question can be parked on a session here, and they are answered
by three different things.

A ROLLBACK confirmation settles an operation that was pre-authorised when the
original change was approved, so core/rollback.py runs it directly.

A SUGGESTION the agent raised itself is the opposite — the write nobody has
approved yet — so its answer goes through `_approve_change`, the same gate.apply
and the same review.settle the panel's Accept and Reject buttons reach. One
exit, two surfaces.

A MEASURED RESULT (§7.2, a week after a suggestion was applied) is the first
kind again wearing the second kind's clothes: the question is new, but the
operation behind a "no" is the inverse the gate computed when the change was
approved. So it is not re-gated either — the answer goes to `verify.answer`,
which records the verdict and hands the undo to core/rollback.py, the one
module that executes one.

Only one question may be outstanding at a time (`_only_question`). A one-word
reply has to be an answer to something the user can still see (§9), and two
parked questions make "yes" ambiguous in a way no amount of wording fixes.
"""
import json
import asyncio
import logging

from core import activity
from core import artefacts
from core import gate
from core import review
from core import rollback
from core import verify
from core import settings
from core import protocols
from tools import tool_peers

from uagents import Context
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

from core.uagent import agent_1
from core.agent_runtime import (
    get_memory,
    now_ts,
    park_approval,
    parse_approval_decision,
    approval_expired,
    _run_agent,
    _run_agent_from_history,
    _execute_tool_async,
    drop_session,
)

logger = logging.getLogger(__name__)


# The slots a question can be parked in. Kept in one tuple because the rule
# below is about the set of them rather than about any one.
_QUESTION_SLOTS = ("pending_rollback", "pending_suggestion", "pending_verdict", "pending_ask")


def _only_question(memory: dict, slot: str):
    """Makes `slot` the only parked question on this session.

    §9 gives the user a one-word reply, which only works while the question is
    the one in front of them. Two parked questions would make YES mean whichever
    slot the handler happened to check first — so asking a new one withdraws the
    old. Nothing is lost: a rollback question was never journalled, and a
    suggestion's proposal is a row in the journal that stays open and answerable
    from the panel or by asking to see it again.
    """
    for other in _QUESTION_SLOTS:
        if other != slot:
            memory[other] = None


def _remember(memory: dict, user_text: str, reply: str):
    """Records an exchange the model was never part of.

    All three parked questions are answered outside the agent loop, so nothing
    else would put them in the history — and a conversation missing the turn
    where the user approved something reads as if it never happened.
    """
    memory["messages"].append(HumanMessage(content=user_text))
    memory["messages"].append(AIMessage(content=reply))
    memory["timestamp"] = now_ts()


def _answer_pending_rollback(memory: dict, text: str, session_id: str):
    """Reads this message as the answer to a parked rollback question.

    Returns the reply text when it settled the question, a dict carrying a
    notice when the question had expired and the message should be handled
    normally, or None when nothing was parked.

    The decision comes from parse_approval_decision, which matches the whole
    normalised message and never uses the LLM — so "no, the other one" is a
    correction, not a rejection, and the question is asked again rather than
    guessed at. Both chat surfaces reach this, because both reach handle_post:
    the panel's chat box posts here directly, and chat_handlers relays ASI1
    messages through it.
    """
    pending = memory.get("pending_rollback")
    if not pending:
        return None

    if rollback.confirmation_expired(pending):
        memory["pending_rollback"] = None
        logger.info(f"[{session_id}] ROLLBACK CONFIRMATION EXPIRED")
        return {"notice": "_The earlier rollback question expired, and nothing was undone._\n\n"}

    decision = parse_approval_decision(text)
    if decision is None:
        # Held, not guessed at. The question is repeated whole: a one-word reply
        # has to be an answer to something the user can still see (§9).
        return rollback.reask(pending)

    memory["pending_rollback"] = None
    return rollback.answer(pending, decision, session_id)


async def _answer_pending_ask(memory: dict, text: str, session_id: str):
    """Reads this message as the answer to a parked question for another
    agent (tools/tool_peers.py). Same contract as the rollback answerer: the
    reply text, a notice dict when it had expired, or None when nothing was
    parked. The decision is parse_approval_decision's, never the model's; the
    send waits for the other agent, so it runs off the loop — which is also
    where core/outbound.py needs it, to schedule the send back onto the loop."""
    pending = memory.get("pending_ask")
    if not pending:
        return None
    if tool_peers.expired(pending):
        memory["pending_ask"] = None
        logger.info(f"[{session_id}] PEER QUESTION EXPIRED")
        return {"notice": "_The earlier question for another agent expired, and was not sent._\n\n"}
    decision = parse_approval_decision(text)
    if decision is None:
        return tool_peers.reask(pending)
    memory["pending_ask"] = None
    logger.info(f"[{session_id}] PEER QUESTION DECISION: send={decision} to {pending.get('address')}")
    return await asyncio.get_running_loop().run_in_executor(
        None, tool_peers.send_confirmed, pending, decision)


async def _answer_pending_suggestion(memory: dict, text: str, session_id: str):
    """Reads this message as the answer to a parked suggestion.

    Same shape as the rollback answerer above and the same decision function —
    `parse_approval_decision` matches the whole normalised message and never
    uses the LLM, so "no, not that one" is a correction and the question is
    asked again rather than guessed at.

    What differs is where the answer goes. A rollback is pre-authorised and is
    executed by core/rollback.py; a proposal is the write nobody has approved,
    so the decision is handed to `_approve_change` — gate.pending_for rebuilds
    the pending from the journal row, gate.apply executes or refuses it, and
    review.settle tells the suggestion what became of it. That last step is why
    a "no" typed in chat becomes a thirty-day cooldown exactly as a "no"
    clicked in the panel does: both are the same call.

    Expiry withdraws the QUESTION and not the suggestion. gate.pending_for
    deliberately gives an agent-raised change no TTL — it is meant to wait —
    so the proposal is still open, still in the digest, and still answerable.
    """
    pending = memory.get("pending_suggestion")
    if not pending:
        return None

    if artefacts.question_expired(pending):
        memory["pending_suggestion"] = None
        logger.info(f"[{session_id}] SUGGESTION QUESTION EXPIRED")
        return {"notice": "_That suggestion question expired, and nothing was changed. "
                          "The suggestion itself is still open — ask to see it again._\n\n"}

    decision = parse_approval_decision(text)
    if decision is None:
        return artefacts.reask(pending)

    memory["pending_suggestion"] = None
    change_id = pending.get("change_id")
    logger.info(f"[{session_id}] SUGGESTION ANSWER: change {change_id} approved={decision}")
    try:
        answered = await _approve_change(change_id, decision)
        return answered.text
    except Exception as e:
        # Caught HERE rather than left to handle_post's own handler, because
        # this call is made before that try block and reaches Home Assistant.
        # The question has already been cleared, so an escaping exception would
        # lose the user's answer AND the only message that could say so — while
        # every other failure on this path becomes a sentence. The journal row
        # is untouched by anything that raises before gate.apply claims it, so
        # the proposal is still open and still answerable.
        logger.error(f"[{session_id}] SUGGESTION ANSWER FAILED: change {change_id}: {e}")
        return (f"Something went wrong while answering that suggestion, and it is not known "
                f"whether anything was written: {e}. Check the Activity tab, and the "
                f"suggestion if it is still open.")


async def _answer_pending_verdict(memory: dict, text: str, session_id: str):
    """Reads this message as the answer to a parked verification report.

    The third of the three, and the same shape again: `parse_approval_decision`
    over the user's own words, no LLM, the whole question re-asked when the
    reply was not a decision.

    Where it goes is `verify.answer`, and what makes that the right exit is
    core/rollback.py decision 1 rather than anything decided here: a "no" runs
    the inverse core/gate.py computed when the change was approved, so nothing
    is re-gated and no second rollback path is built. YES is `kept` and NO is
    `rolled_back` — stated by `artefacts.VERDICT_PROMPT`, which is the sentence
    the user actually read, so the two cannot come to disagree.

    Blocking — it writes to the store and, for a "no", reaches Home Assistant —
    so it goes to the executor.

    Expiry withdraws the QUESTION and not the report. The suggestion stays at
    `verified`, in the panel and in the listing, and can be asked for again.
    """
    pending = memory.get("pending_verdict")
    if not pending:
        return None

    if artefacts.verdict_expired(pending):
        memory["pending_verdict"] = None
        logger.info(f"[{session_id}] VERDICT QUESTION EXPIRED")
        return {"notice": "_That question about a measured result expired, and nothing was "
                          "changed. The report itself is still there — ask to see it again._"
                          "\n\n"}

    decision = parse_approval_decision(text)
    if decision is None:
        return artefacts.reask_verdict(pending)

    memory["pending_verdict"] = None
    suggestion_id = pending.get("suggestion_id")
    logger.info(f"[{session_id}] VERDICT ANSWER: suggestion {suggestion_id} keep={decision}")
    try:
        answered = await asyncio.get_running_loop().run_in_executor(
            None, verify.answer, suggestion_id, decision, session_id)
        return answered.get("message", "")
    except Exception as e:
        # Caught here for the reason the suggestion answerer above is: this runs
        # before handle_post's own try block and it reaches Home Assistant, and
        # the question has already been cleared — so an escaping exception would
        # lose the user's answer and the only message that could say so.
        logger.error(f"[{session_id}] VERDICT ANSWER FAILED: suggestion {suggestion_id}: {e}")
        return (f"Something went wrong while answering that: {e}. It is not known whether the "
                f"automation was removed — check the Activity tab.")


# uagent REST api handler
@agent_1.on_rest_post("/rest/post", protocols.Request, protocols.Response)
async def handle_post(ctx: Context, req: protocols.Request) -> protocols.Response:
    """Runs the LLM agent on the user's message for this session, returning
    its reply text or, if a destructive tool needs confirmation, an
    [APPROVAL_REQUIRED] marker for the client to act on."""
    sid = req.session_id
    if sid == activity.AGENT_SESSION:
        # FIX 2, defence in depth: activity.source_for no longer derives the
        # agent source from any session id, but a client posting this exact
        # sentinel is still worth refusing outright rather than quietly
        # treating it as an ordinary (if odd) panel session.
        logger.warning("REJECTED a request using the reserved session id 'agent:self'")
        return protocols.Response(text="Invalid session id.")
    memory = get_memory(sid)
    if getattr(req, "model", None):
        memory["model"] = req.model

    # A rollback question this session is holding is answered by the user's own
    # words, before the model sees them. NOT the approval gate: the operation
    # was pre-authorised when the original change was approved, and what is
    # being confirmed here is only WHICH change the model picked out of "undo
    # that" (core/rollback.py decision 4). Nothing on this path calls
    # gate.apply, and the parked question lives in its own slot so it cannot be.
    notice = ""
    answered = _answer_pending_rollback(memory, req.text, sid)
    if answered is not None:
        if isinstance(answered, str):
            _remember(memory, req.text, answered)
            logger.info(f"[{sid}] ROLLBACK ANSWER: {answered[:200]}")
            return protocols.Response(text=answered)
        # An expired question. Say so — the user asked for an undo and did not
        # get one — and let their message be handled normally.
        notice = answered.pop("notice", "")

    # A question for another agent this session is holding: sent on YES, by
    # the person's own words, and its answer given to them straight.
    asked = await _answer_pending_ask(memory, req.text, sid)
    if asked is not None:
        if isinstance(asked, str):
            _remember(memory, req.text, asked)
            logger.info(f"[{sid}] PEER QUESTION ANSWER: {asked[:200]}")
            return protocols.Response(text=asked)
        notice += asked.pop("notice", "")

    # And a suggestion this session is holding, the same way and for the same
    # reason — except that this one is not pre-authorised, so the answer leaves
    # here for /rest/approve's change_id path rather than being executed.
    settled = await _answer_pending_suggestion(memory, req.text, sid)
    if settled is not None:
        if isinstance(settled, str):
            _remember(memory, req.text, settled)
            logger.info(f"[{sid}] SUGGESTION READBACK: {settled[:200]}")
            return protocols.Response(text=settled)
        notice += settled.pop("notice", "")

    # And a measured result. Not pre-authorised in the way a rollback is and
    # not un-authorised in the way a proposal is: the operation behind a "no"
    # here is the one the gate already approved, so this answers it without
    # going near gate.apply.
    answered_verdict = await _answer_pending_verdict(memory, req.text, sid)
    if answered_verdict is not None:
        if isinstance(answered_verdict, str):
            _remember(memory, req.text, answered_verdict)
            logger.info(f"[{sid}] VERDICT READBACK: {answered_verdict[:200]}")
            return protocols.Response(text=answered_verdict)
        notice += answered_verdict.pop("notice", "")

    memory["messages"].append(HumanMessage(content=req.text))
    logger.info(f"[{sid}] USER: {req.text} (model: {memory.get('model', 'asi1-mini')})")
    try:
        txt, ai_msg = await asyncio.wait_for(
            _run_agent(memory["messages"], session_id=sid),
            timeout=settings.LLM_TIMEOUT
        )
        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = None

        if parsed and parsed.get("__approval_required__"):
            _, full_response = park_approval(memory, parsed, ai_msg, memory["messages"], sid)
            # A destructive tool call is a question too, and it is answered by
            # a YES that must not be able to mean anything else.
            _only_question(memory, "pending_approval")
            return protocols.Response(text=notice + full_response)

        if parsed and parsed.get(rollback.CONFIRMATION_MARKER):
            question = rollback.park_confirmation(memory, parsed, sid)
            _only_question(memory, "pending_rollback")
            memory["messages"].append(AIMessage(content=question))
            memory["timestamp"] = now_ts()
            logger.info(f"[{sid}] ROLLBACK PARKED: change {parsed.get('change_id')}")
            return protocols.Response(text=notice + question)

        if parsed and parsed.get(tool_peers.ASK_MARKER):
            question = tool_peers.park_question(memory, parsed)
            _only_question(memory, "pending_ask")
            memory["messages"].append(AIMessage(content=question))
            memory["timestamp"] = now_ts()
            logger.info(f"[{sid}] PEER QUESTION PARKED: to {parsed.get('address')}")
            return protocols.Response(text=notice + question)

        if parsed and parsed.get(artefacts.QUESTION_MARKER):
            # §9 artefacts 2 and 3, put to the user as one question about one
            # change. Parked in its own slot, never `pending_approval`: that
            # slot is answered by resuming a conversation the model was having,
            # and there is none — the pending is rebuilt from the journal row
            # when the answer arrives.
            question = artefacts.park_question(memory, parsed, sid)
            _only_question(memory, "pending_suggestion")
            memory["messages"].append(AIMessage(content=question))
            memory["timestamp"] = now_ts()
            logger.info(f"[{sid}] SUGGESTION PARKED: suggestion "
                        f"{parsed.get('suggestion_id')}, change {parsed.get('change_id')}")
            return protocols.Response(text=notice + question)

        if parsed and parsed.get(artefacts.VERDICT_MARKER):
            # §9 artefact 4, put to the user as one question about one change
            # that is already in their home. Its own slot again — a YES here
            # means "keep it", and a YES in either of the two slots above means
            # something else entirely.
            question = artefacts.park_verdict(memory, parsed, sid)
            _only_question(memory, "pending_verdict")
            memory["messages"].append(AIMessage(content=question))
            memory["timestamp"] = now_ts()
            logger.info(f"[{sid}] VERDICT PARKED: suggestion "
                        f"{parsed.get('suggestion_id')}, change {parsed.get('change_id')}")
            return protocols.Response(text=notice + question)

        memory["messages"].append(AIMessage(content=txt))
        memory["timestamp"] = now_ts()
        logger.info(f"[{sid}] AGENT: {txt}")
        return protocols.Response(text=notice + txt)
    except asyncio.TimeoutError:
        error_msg = "Request took too long to process. Please try a simpler request."
        memory["messages"].append(AIMessage(content=error_msg))
        logger.warning(f"[{sid}] TIMEOUT")
        return protocols.Response(text=error_msg)
    except Exception as e:
        error_msg = f"Error processing request: {str(e)}"
        memory["messages"].append(AIMessage(content=error_msg))
        logger.error(f"[{sid}] ERROR: {str(e)}")
        return protocols.Response(text=error_msg)


@agent_1.on_rest_post("/rest/session/delete", protocols.SessionRequest, protocols.Response)
async def handle_delete_session(ctx: Context, req: protocols.SessionRequest) -> protocols.Response:
    """Discards panel session memory, including any unconfirmed action."""
    drop_session(req.session_id)
    return protocols.Response(text="Session deleted.")

# uagent REST approval handler
class ApprovalRequest(protocols.Model):
    """User's decision on a pending destructive change.

    Two ways of naming which one, because there are two ways a change gets
    raised. `session_id` answers whatever that conversation has parked — the
    panel's buttons and the ASI1 relay have always sent this and still do,
    unchanged. `change_id` answers a change the agent raised itself, which has
    no session to be parked on and is named by its row in the journal instead.

    One endpoint rather than two: whichever surface the answer comes from, it
    lands on core/gate.py's apply, and a second endpoint would be a second place
    for that to stop being true.
    """
    session_id: str = ""
    approved: bool = False
    change_id: int = 0


async def _approve_change(change_id, approved: bool) -> protocols.Response:
    """Answers a change that has no session behind it.

    The pending is rebuilt from the journal row (gate.pending_for) and handed to
    the same gate.apply the conversational path uses — identical dict, identical
    execution, identical record. What cannot happen here is resuming the LLM: an
    agent-raised change has no conversation to resume, and inventing one would
    put words in the user's mouth to justify a write. So the reply is the gate's
    factual readback of what was actually done (§9), with no model in the loop.

    The one thing this path adds after the gate is `review.settle`. The gate
    hands back a change_id and does not know what a suggestion is, so the
    suggestion that raised the change learns here what became of it — applied,
    with the baseline §7.2's verification is measured against, dismissed on a
    "no", retired if the write never landed. It reads the journal row the gate
    has just written rather than the tool result, and it is best-effort: the
    change is recorded and the user is told what happened either way.
    """
    pending, refusal = await asyncio.get_running_loop().run_in_executor(
        None, gate.pending_for, change_id
    )
    if pending is None:
        logger.info(f"[change {change_id}] APPROVAL REFUSED: {refusal}")
        return protocols.Response(text=refusal)

    tool_result = await gate.apply(
        pending, approved, activity.AGENT_SESSION, _execute_tool_async
    )
    logger.info(f"[change {change_id}] TOOL RESULT (agent-raised): "
                f"{pending['tool_name']} | {str(tool_result)[:300]}")

    # Blocking — it reads the record and asks Home Assistant for the sensor's
    # unit — so it goes to the executor the way pending_for did.
    try:
        await asyncio.get_running_loop().run_in_executor(None, review.settle, change_id)
    except Exception as e:
        logger.error(f"[change {change_id}] could not settle the suggestion behind it: {e}")

    return protocols.Response(text=gate.readback(pending, approved, tool_result))


@agent_1.on_rest_post("/rest/approve", ApprovalRequest, protocols.Response)
async def handle_approve(ctx: Context, req: ApprovalRequest) -> protocols.Response:
    """Executes (or rejects) a pending destructive change on the user's
    decision, then resumes the LLM agent loop with the tool result and returns
    its follow-up reply.

    A `change_id` takes precedence over a session: it names one specific row,
    whereas the session names whatever that conversation happens to be holding,
    and a client answering a proposal it was shown by id means that one.
    """
    if req.change_id:
        return await _approve_change(req.change_id, req.approved)

    sid = req.session_id
    if sid == activity.AGENT_SESSION:
        # Same defence as handle_post: the sentinel is not a session a client
        # gets to name, on this path either.
        logger.warning("REJECTED an approval using the reserved session id 'agent:self'")
        return protocols.Response(text="Invalid session id.")
    memory = get_memory(sid)
    pending = memory.get("pending_approval")
    if not pending:
        return protocols.Response(text="No pending approval found for this session.")

    memory["pending_approval"] = None

    # The panel session can sit open for the full one-hour eviction window,
    # far longer than a stray click should still be able to fire a delete —
    # so this path needs the same TTL guard the chat text path already has.
    if approval_expired(pending):
        activity.withdraw(pending.get("notice_id"))
        activity.record_decision(
            pending.get("change_id"), "expired", status="not_applied",
            result="This confirmation request expired before it was answered.",
        )
        logger.info(f"[{sid}] APPROVAL EXPIRED (panel)")
        return protocols.Response(
            text="This confirmation request expired and was cancelled. Please try again."
        )

    # The gate executes it, not this handler. Every write is recorded with its
    # inverse before the user is told it happened, and there is exactly one
    # place that can be true of.
    tool_result = await gate.apply(pending, req.approved, sid, _execute_tool_async)
    logger.info(f"[{sid}] TOOL RESULT (approval): {pending['tool_name']} | {str(tool_result)[:300]}")

    system_msg = SystemMessage(content=settings.system_message + settings.get_liveSystemMesaageData())
    history = [system_msg] + pending["messages_snapshot"] + [
        pending["ai_message"],
        ToolMessage(content=str(tool_result), tool_call_id=pending["tool_call_id"]),
    ]

    try:
        txt, ai_msg = await asyncio.wait_for(_run_agent_from_history(history, sid), timeout=60.0)

        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = None

        if parsed and parsed.get("__approval_required__"):
            prior_tool_msg = ToolMessage(content=str(tool_result), tool_call_id=pending["tool_call_id"])
            updated_snapshot = pending["messages_snapshot"] + [pending["ai_message"], prior_tool_msg]
            friendly, full_response = park_approval(memory, parsed, ai_msg, updated_snapshot, sid)
            _only_question(memory, "pending_approval")
            logger.info(f"[{sid}] APPROVED (chained approval): {friendly[:200]}")
            return protocols.Response(text=full_response)

        if parsed and parsed.get(rollback.CONFIRMATION_MARKER):
            # Answering one approval can lead the model to propose undoing
            # something else. Parked the same way, and answered by the user's
            # next message rather than by this endpoint's approved flag — that
            # flag was a decision about a different thing.
            question = rollback.park_confirmation(memory, parsed, sid)
            _only_question(memory, "pending_rollback")
            logger.info(f"[{sid}] ROLLBACK PARKED (chained): change {parsed.get('change_id')}")
            return protocols.Response(text=question)

        if parsed and parsed.get(tool_peers.ASK_MARKER):
            # And a question for another agent, asked while answering something
            # else: the approved flag that got here was about a different thing.
            question = tool_peers.park_question(memory, parsed)
            _only_question(memory, "pending_ask")
            logger.info(f"[{sid}] PEER QUESTION PARKED (chained): to {parsed.get('address')}")
            return protocols.Response(text=question)

        if parsed and parsed.get(artefacts.QUESTION_MARKER):
            # Likewise for a suggestion the model raised while answering
            # something else. The `approved` flag that got here answered a
            # different question and must not carry over to this one.
            question = artefacts.park_question(memory, parsed, sid)
            _only_question(memory, "pending_suggestion")
            logger.info(f"[{sid}] SUGGESTION PARKED (chained): change {parsed.get('change_id')}")
            return protocols.Response(text=question)

        if parsed and parsed.get(artefacts.VERDICT_MARKER):
            # And likewise for a measured result. Same argument a third time:
            # the flag that reached this endpoint answered a different question.
            question = artefacts.park_verdict(memory, parsed, sid)
            _only_question(memory, "pending_verdict")
            logger.info(f"[{sid}] VERDICT PARKED (chained): suggestion "
                        f"{parsed.get('suggestion_id')}")
            return protocols.Response(text=question)

        tool_msg = ToolMessage(content=str(tool_result), tool_call_id=pending["tool_call_id"])
        memory["messages"] = pending["messages_snapshot"] + [pending["ai_message"], tool_msg, AIMessage(content=txt)]
        memory["timestamp"] = now_ts()
        logger.info(f"[{sid}] APPROVED: {txt[:200]}")
        return protocols.Response(text=txt)
    except asyncio.TimeoutError:
        error_msg = "Request took too long to process."
        logger.warning(f"[{sid}] APPROVAL TIMEOUT")
        return protocols.Response(text=error_msg)
    except Exception as e:
        error_msg = f"Error processing approval: {str(e)}"
        logger.error(f"[{sid}] APPROVAL ERROR: {str(e)}")
        return protocols.Response(text=error_msg)
