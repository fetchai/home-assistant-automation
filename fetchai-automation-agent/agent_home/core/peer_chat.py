"""Chat with an APPROVED agent (2.28.0): any Agentverse agent, over plain chat.

Chat is what every agent speaks, so it is how any agent can work with this
home — but chat text goes to a model, and another agent's text is an outside
party's. So an approved agent's messages never reach rest_chat or the person's
tools. They come here, where the conversation is:

- its own: memory per agent address, never a person's session;
- told who it is with (settings.peer_system_message: an outside agent, whose
  text is a request from outside, never the household's instruction);
- bounded by level, in the TOOL LIST itself, not by asking the model: `read`
  gets the watched devices and the record's watched-only tools, nothing that
  lists the whole house, the outbox, the journal or other agents; `propose`
  adds 'Propose a change to the household', bound to this agent's address,
  which goes through peer_handlers.raise_proposal — vet, then the gate, then a
  person. A tool the model names that is not on the list is not run;
- rate-limited (MAX_PER_HOUR, before any model call) and logged on the entry
  (peers.log_chat), which the panel shows.

The reply passes core/disclosure like every answer that leaves the house.
"""
import asyncio
import logging
import time

from core import disclosure
from core import models
from core import peers
from core import settings
from tools.tool_peers import tool_propose_change, tool_watched_devices
from tools.tool_record import tool_record_overview, tool_record_profile, tool_record_relations
from tools.tool_time import tool_time

logger = logging.getLogger(__name__)

MAX_PER_HOUR = 20
MAX_STEPS = 6
MESSAGES_KEPT = 20
FORGET_AFTER_SECONDS = 3600
MESSAGE_MAX = 2000
REPLY_MAX = 4000

# The chat loop's lesson (agent_runtime._NO_TOOL_NUDGE), for this loop too:
# asi1-mini answers from nothing when it can, and the first live test of 2.28.0
# told an approved agent "none are being watched" with 28 watched. Once, only
# while no tool has run; a HumanMessage because ASI1 rejects a second system one.
_NUDGE = (
    "Reminder, not a correction: answer only from a tool result. Call 'Get watched devices' or "
    "another of your tools now and answer from what it returns. If the message needs no lookup "
    "at all, repeat your previous reply unchanged."
)

_memory = {}
_recent = {}


def tools_for(level, address) -> list:
    """The whole of what the model may do in this conversation."""
    if not peers.may_read(level):
        return []
    tools = [tool_watched_devices(), tool_record_overview(), tool_record_profile(),
             tool_record_relations(), tool_time()]
    if peers.may_propose(level):
        tools.append(tool_propose_change(address=address))
    return tools


def _llm():
    return models.chat_model(settings.ASI1_MODEL or "asi1-mini")


def _over_budget(address, now) -> bool:
    stamps = [t for t in _recent.get(address, []) if now - t < 3600]
    over = len(stamps) >= MAX_PER_HOUR
    if not over:
        stamps.append(now)
    _recent[address] = stamps
    return over


def _session(address, now) -> list:
    entry = _memory.get(address)
    if entry is None or now - entry["at"] > FORGET_AFTER_SECONDS:
        entry = {"messages": [], "at": now}
        _memory[address] = entry
    entry["at"] = now
    return entry["messages"]


def _system(address, level) -> str:
    peer = next((p for p in peers.list_peers() if p["address"] == address), {})
    name = (peer.get("ask") or {}).get("name") or peer.get("name") or "an unnamed agent"
    return (settings.peer_system_message
            + f" You are talking to {name!r} ({address}), approved at level '{level}'.")


def _text(content) -> str:
    return content if isinstance(content, str) else " ".join(str(p) for p in (content or []))


async def reply(address, text, now=None) -> str:
    """This home's answer to one message from an approved agent. Returns the
    text to send, or "" when the agent is not (or no longer) approved."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    now = int(now if now is not None else time.time())
    level = peers.level_of(address)
    tools = tools_for(level, address)
    if not tools:
        return ""
    if _over_budget(address, now):
        return f"This home answers at most {MAX_PER_HOUR} messages an hour. Try again later."

    messages = _session(address, now)
    messages.append(HumanMessage(content=str(text)[:MESSAGE_MAX]))
    history = [SystemMessage(content=_system(address, level))] + messages
    by_name = {t.name: t for t in tools}
    llm = _llm().bind_tools(tools)

    final = "I could not work that out within the steps I have. Please ask more simply."
    nudged = False
    any_tool = False
    for _ in range(MAX_STEPS):
        response = await llm.ainvoke(history)
        history.append(response)
        if not getattr(response, "tool_calls", None):
            if not nudged and not any_tool:
                nudged = True
                history.append(HumanMessage(content=_NUDGE))
                continue
            final = _text(response.content)
            break
        any_tool = True
        for call in response.tool_calls:
            tool = by_name.get(call["name"])
            if tool is None:
                result = disclosure.emit({"error": f"{call['name']!r} is not available to you."})
            else:
                try:
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: tool._run(**(call.get("args") or {})))
                except Exception as e:
                    result = disclosure.text(f"Tool error: {e}")
            logger.info(f"PEERS: chat tool {call['name']} for {address}: {str(result)[:160]}")
            history.append(ToolMessage(content=str(result), tool_call_id=call["id"]))

    final = disclosure.text(final)[:REPLY_MAX]
    messages.append(AIMessage(content=final))
    del messages[:-MESSAGES_KEPT]
    peers.log_chat(address, str(text)[:MESSAGE_MAX], final, now=now)
    return final
