"""Core agentic runtime: tool registry, session memory store, chat-reply
parsing and the LLM agent loop. This is a leaf module — it must NOT import any
of the handler modules (chat_handlers, rest_chat, scheduled_tasks).

Deciding whether a tool call needs approval, and executing one that got it, is
NOT here: that is core/gate.py, so it can be one component rather than a rule
this loop remembers to apply. The names are re-exported below because the
handlers have always reached them through this module.
"""
import json
import asyncio
import logging

from core import artefacts
from core import disclosure
from core import models
from core import rollback
from core import settings
# Re-exported: the approval gate and the pairing gate normalise text the same
# way, and core.text stays free of the LLM imports below.
from core.text import strip_leading_mentions  # noqa: F401
# Re-exported for the same reason: the gate moved out to core/gate.py so that
# deciding, executing and recording a destructive change live in one component
# (design §5), but the handlers and this module's own loop still reach it here.
from core.gate import (  # noqa: F401
    APPROVAL_MARKER, DESTRUCTIVE_TOOLS, SERVICE_CALL_TOOL, now_ts,
    _is_destructive, _build_destructive_approval, _format_approval_message,
    park_approval,
)

from langchain.tools import BaseTool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

# Import TOOLS
from tools.tool_time import tool_time
from tools.tool_changes import tool_list_changes, tool_roll_back_change
from tools.tool_peers import tool_ask_agent
from tools import tool_peers
from tools.tool_suggestions import (
    tool_list_suggestions, tool_show_suggestion, tool_show_verification,
)
from tools.tool_record import (
    tool_record_overview, tool_record_profile, tool_record_relations, tool_pending_messages,
)
from tools.tool_homeassistant import (
    tool_ha_list_entities, tool_ha_get_entity, tool_ha_list_devices, tool_ha_list_services,
    tool_ha_list_areas, tool_ha_list_area_entities, tool_ha_call_service,
    tool_ha_entity_history,
    tool_ha_create_automation, tool_ha_list_automations, tool_ha_get_automation,
    tool_ha_update_automation, tool_ha_delete_automation,
)

logger = logging.getLogger(__name__)

# Create tools
tools: list[BaseTool] = [
    tool_time(),
    tool_ha_list_entities(),
    tool_ha_get_entity(),
    tool_ha_list_devices(),
    tool_ha_list_services(),
    tool_ha_list_areas(),
    tool_ha_list_area_entities(),
    tool_ha_call_service(),
    tool_ha_entity_history(),
    tool_record_overview(),
    tool_record_profile(),
    tool_record_relations(),
    tool_pending_messages(),
    tool_list_changes(),
    tool_roll_back_change(),
    tool_ask_agent(),
    tool_list_suggestions(),
    tool_show_suggestion(),
    tool_show_verification(),
    tool_ha_create_automation(),
    tool_ha_list_automations(),
    tool_ha_get_automation(),
    tool_ha_update_automation(),
    tool_ha_delete_automation(),
]


session_memory_store = {}
SESSION_TIMEOUT_SECONDS = 3600

# How long a parked approval stays answerable in a text chat client. The only
# other expiry is the one-hour session eviction in scheduled_tasks.manage_memory,
# far too long a window for a stray "yes" to fire a delete.
APPROVAL_TTL_SECONDS = 600


_YES_REPLIES = frozenset({
    "yes", "y", "yeah", "yep", "ok", "okay", "sure", "confirm", "approve",
    "proceed", "go ahead", "do it", "yes please",
})
_NO_REPLIES = frozenset({
    "no", "n", "nope", "cancel", "reject", "deny", "stop", "abort",
    "no thanks", "do not", "don't",
})

def _execute_tool_sync(tool_name: str, tool_args: dict) -> str:
    """Finds the registered tool by name and runs it synchronously,
    returning its result or an error string."""
    for t in tools:
        if t.name == tool_name:
            try:
                return t._run(**tool_args)
            except Exception as e:
                return disclosure.text(f"Tool error: {str(e)}")
    return f"Tool '{tool_name}' not found."

async def _execute_tool_async(tool_name: str, tool_args: dict) -> str:
    """Finds the registered tool by name and runs it asynchronously,
    returning its result or an error string."""
    for t in tools:
        if t.name == tool_name:
            try:
                return await t._arun(**tool_args)
            except Exception as e:
                return disclosure.text(f"Tool error: {str(e)}")
    return f"Tool '{tool_name}' not found."

def format_approval_for_chat(text: str) -> str:
    """Rewrites a handle_post response for plain-text chat clients (ASI1).

    The [APPROVAL_REQUIRED] marker and its JSON payload are meant for the Vue
    panel, which strips them and renders the diff modal. Clients that only show
    markdown would print the raw blob, so replace it with the automation config
    in fenced yaml blocks. Text without the marker is returned unchanged."""
    if not text or APPROVAL_MARKER not in text:
        return text

    friendly, _, payload_json = text.partition(APPROVAL_MARKER)
    friendly = friendly.rstrip()
    try:
        payload = json.loads(payload_json)
    except Exception:
        # Never leak a half-parsed payload to the user.
        return friendly

    parts = [friendly]
    for label, key in (("Current configuration", "old_yaml"), ("New configuration", "new_yaml")):
        # Both are empty for buy/cancel approvals — emit no fence at all there.
        block = (payload.get(key) or "").strip()
        if block:
            parts.append(f"**{label}:**\n```yaml\n{block}\n```")
    # Also covers the chained approval handle_approve can return, so the prompt
    # never has to be re-appended at the call site.
    parts.append("_Reply **YES** to confirm or **NO** to cancel._")
    return "\n\n".join(parts)

def parse_approval_decision(text: str):
    """Reads a chat reply as a decision on a pending approval: True to confirm,
    False to cancel, None if it is not an answer at all.

    Matches the whole normalised message, never a substring — "no, actually use
    light.kitchen" is a correction, not a rejection, and must come back None so
    the caller re-asks instead of acting. The LLM is deliberately not involved:
    it must not be the thing deciding what counts as consent to delete."""
    if not text:
        return None
    stripped = strip_leading_mentions(text)
    normalized = " ".join(stripped.strip().strip(".!?,").lower().split())
    if normalized in _YES_REPLIES:
        return True
    if normalized in _NO_REPLIES:
        return False
    return None

def approval_expired(pending: dict) -> bool:
    """True if a parked approval is too old to still be answerable by text."""
    created_at = (pending or {}).get("created_at")
    if not created_at:
        return False
    return now_ts() - created_at > APPROVAL_TTL_SECONDS

# asi1-mini sometimes answers straight from the model, ignoring the tool-first
# rules in the system prompt, and fabricates plausible facts about the user's
# home (entity ids that don't exist, automations it never created). The agent
# loop treats any tool-call-free response as the final answer, so nothing
# catches it. Give the model exactly one chance to correct itself before its
# unverified text reaches the user.
#
# Sent as a HumanMessage, not a SystemMessage: the ASI1 endpoint rejects any
# system message that is not the first message in the request
# ("System message must be at the beginning", HTTP 400).
#
# Only fires when NO tool has run yet this turn. Once a tool has been called,
# a response without further tool calls is the expected final answer.
_NO_TOOL_NUDGE = (
    "Reminder, not a correction — you have not made an error. "
    "Facts about the user's home — entity ids, device and service names, existing automations and their "
    "configs, current states — MUST come from a tool result, never from memory, guesswork, or the "
    "conversation history. Turning something on or off, and creating, updating or deleting an automation, "
    "MUST go through the corresponding tool. If the user named a room, resolve it with the area tools "
    "instead of asking which entity they meant. "
    "If you have enough information to call the appropriate tool now, call it and answer only from its result. "
    "If you need the user to supply something first (such as which room or which light), or the question is "
    "general and involves no lookup or change at all, simply repeat your previous reply unchanged and do NOT apologise."
)


def _create_llm_with_tools(model_name: str = ""):
    """Builds the ASI1 chat model bound to the full tool registry. The
    session's chosen model, not a tier (core/models.py, design §15.4) — chat
    keeps the user's own choice."""
    chosen_model = model_name or settings.ASI1_MODEL or "asi1-mini"
    llm = models.chat_model(chosen_model)
    return llm.bind_tools(tools)

async def _run_agent(messages: list, max_iterations: int = 10, session_id: str = "", model_name: str = "") -> tuple:
    """Runs the LLM agent loop over a fresh message list, executing tool
    calls until a final text answer or a pending destructive-tool approval
    is produced. Returns (text_or_approval_payload, response_or_None)."""
    if not model_name and session_id:
        model_name = get_memory(session_id).get("model")
    llm_with_tools = _create_llm_with_tools(model_name=model_name)
    system_msg = SystemMessage(content=settings.system_message + settings.get_liveSystemMesaageData())
    history = [system_msg] + messages
    sid = f"[{session_id}] " if session_id else ""
    nudged = False
    any_tool_called = False

    for _ in range(max_iterations):
        response = await llm_with_tools.ainvoke(history)
        history.append(response)

        if not response.tool_calls:
            if not nudged and not any_tool_called:
                nudged = True
                logger.info(f"{sid}NO TOOL CALL — nudging once before accepting the answer")
                history.append(HumanMessage(content=_NO_TOOL_NUDGE))
                continue
            final_text = response.content if isinstance(response.content, str) else " ".join(str(p) for p in response.content)
            return final_text, None

        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            logger.info(f"{sid}TOOL CALL: {tool_name} | args: {tool_args}")
            any_tool_called = True

            if _is_destructive(tool_name, tool_args):
                approval_payload, preflight_error = _build_destructive_approval(tool_name, tool_args, tool_call)
                if preflight_error:
                    logger.info(f"{sid}PREFLIGHT FAILED: {tool_name} | {preflight_error[:200]}")
                    history.append(ToolMessage(content=preflight_error, tool_call_id=tool_call["id"]))
                    continue
                return approval_payload, response

            tool_result = await asyncio.get_event_loop().run_in_executor(
                None, _execute_tool_sync, tool_name, tool_args
            )
            logger.info(f"{sid}TOOL RESULT: {tool_name} | {str(tool_result)[:200]}")

            # A rollback the user has to confirm. Recognised in the RESULT, not
            # by tool name and not by an argument: a result is built by this
            # process, so the model cannot forge one — and it stops here rather
            # than going back into the history, so the model never gets to
            # answer its own question. core/rollback.py decision 4. This is NOT
            # the approval gate and must never be routed into it.
            if rollback.confirmation_in(tool_result):
                logger.info(f"{sid}ROLLBACK CONFIRMATION: {str(tool_result)[:200]}")
                return tool_result, response

            # A suggestion the agent raised itself, put to the user. Recognised
            # the same way and stopping here for the same reason: the model
            # asked the question, and letting the result back into the history
            # would let it answer its own. Its own marker, because this one is
            # answered by /rest/approve and a rollback must never reach that.
            if artefacts.question_in(tool_result):
                logger.info(f"{sid}SUGGESTION QUESTION: {str(tool_result)[:200]}")
                return tool_result, response

            # A question to another agent the person confirms first. Same
            # interception, same reason: the model must not answer it itself.
            if tool_peers.question_in(tool_result):
                logger.info(f"{sid}PEER QUESTION: {str(tool_result)[:200]}")
                return tool_result, response

            history.append(ToolMessage(content=str(tool_result), tool_call_id=tool_call["id"]))

    return "Agent reached maximum iterations without a final answer.", None


async def _run_agent_from_history(history: list, session_id: str = "", model_name: str = "") -> tuple:
    """Resumes the LLM agent loop from an existing message history (e.g.
    after a user approves/denies a destructive tool call), executing tool
    calls until a final text answer or a new approval is produced."""
    if not model_name and session_id:
        model_name = get_memory(session_id).get("model")
    llm_with_tools = _create_llm_with_tools(model_name=model_name)
    sid = f"[{session_id}] " if session_id else ""

    for _ in range(10):
        response = await llm_with_tools.ainvoke(history)
        history.append(response)

        # No _NO_TOOL_NUDGE here: the caller has already executed (or rejected)
        # the approved tool and appended its ToolMessage, so a reply without
        # further tool calls IS the expected final answer. Nudging it would make
        # the model answer the reminder instead, and that reply — "I understand,
        # I'll ensure all measurement values come from tool results…" — is what
        # the user would see after confirming, in place of the actual result.
        if not response.tool_calls:
            final_text = response.content if isinstance(response.content, str) else " ".join(str(p) for p in response.content)
            return final_text, None

        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            logger.info(f"{sid}TOOL CALL (resume): {tool_name} | args: {tool_args}")

            if _is_destructive(tool_name, tool_args):
                approval_payload, preflight_error = _build_destructive_approval(tool_name, tool_args, tool_call)
                if preflight_error:
                    logger.info(f"{sid}PREFLIGHT FAILED (resume): {tool_name} | {preflight_error[:200]}")
                    history.append(ToolMessage(content=preflight_error, tool_call_id=tool_call["id"]))
                    continue
                return approval_payload, response

            tool_result = await _execute_tool_async(tool_name, tool_args)
            logger.info(f"{sid}TOOL RESULT (resume): {tool_name} | {str(tool_result)[:200]}")

            # The same interception on the resume path: answering one approval
            # can lead the model to propose undoing something else.
            if rollback.confirmation_in(tool_result):
                logger.info(f"{sid}ROLLBACK CONFIRMATION (resume): {str(tool_result)[:200]}")
                return tool_result, response

            if artefacts.question_in(tool_result):
                logger.info(f"{sid}SUGGESTION QUESTION (resume): {str(tool_result)[:200]}")
                return tool_result, response

            if tool_peers.question_in(tool_result):
                logger.info(f"{sid}PEER QUESTION (resume): {str(tool_result)[:200]}")
                return tool_result, response

            history.append(ToolMessage(content=str(tool_result), tool_call_id=tool_call["id"]))

    return "Agent reached maximum iterations without a final answer.", None

def drop_session(session_id: str) -> bool:
    """Forgets a session entirely, including any parked approval.

    Called when an identity is unpaired: a confirmation the user is no longer
    willing to grant must not stay answerable, and a re-paired address must not
    resume the old conversation."""
    return session_memory_store.pop(session_id, None) is not None


def get_memory(session_id: str):
    """Returns the session's memory entry (message history, pending
    approval), creating a fresh one if the session id is unknown."""
    global session_memory_store
    if session_id not in session_memory_store:
        session_memory_store[session_id] = {
            "timestamp": now_ts(),
            "messages": [],
            "pending_approval": None,
            # The add-on's asi1_model option, if the operator set one. A panel
            # session overwrites this on its very next /rest/post (it always
            # sends a model), so this default is only ever load-bearing for a
            # session that never goes through the panel — i.e. ASI1 chat.
            "model": settings.ASI1_MODEL,
        }
    return session_memory_store[session_id]
