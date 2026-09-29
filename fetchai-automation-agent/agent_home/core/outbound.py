"""Asking another agent a question and waiting for its answer (phase 3).

Most agents on Agentverse speak the chat protocol, so a question goes out as a
ChatMessage and the answer comes back as one. The chat handler
(handlers/chat_handlers.py) sees every incoming ChatMessage first; for an agent
this home is waiting on, `deliver` hands the text here instead of letting it
reach the pairing gate or the model's chat path. An agent this home may ask
never gets a chat conversation of its own that way either — a late or
unsolicited message from one is acknowledged and dropped.

The tools run in an executor thread (agent_runtime._execute_tool_sync), and a
uagents message can only be sent from the agent's event loop. So `bind`
records the loop at startup, and `ask` schedules the send-and-wait there and
blocks its own thread on the result. One question per agent at a time: a
second while the first waits is refused rather than raced for the answer.

Only core/peers.py's askable agents are asked — tools/tool_peers.py checks — and
the question passes core/disclosure first like every other answer that leaves.
"""
import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

logger = logging.getLogger(__name__)

#: How long to wait for an answer. Mailbox delivery and a model on the other
#: side both take seconds; a chat reply held longer than this is not worth it.
TIMEOUT_SECONDS = 60
QUESTION_MAX = 1000
ANSWER_MAX = 4000

_loop = None
_context = None
_waiting = {}


def bind(loop, context_factory):
    """Called once at startup, on the agent's loop: where to send from, and
    how to get a context that can send (uagents Agent._build_context — what
    interval handlers are given)."""
    global _loop, _context
    _loop, _context = loop, context_factory


def _message(text):
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
    return ChatMessage(timestamp=datetime.now(tz=timezone.utc), msg_id=uuid4(),
                       content=[TextContent(type="text", text=text)])


def _failed(status) -> bool:
    value = getattr(getattr(status, "status", None), "value", getattr(status, "status", None))
    return str(value) == "failed"


async def _ask(address, text, timeout):
    if address in _waiting:
        return None, "a question to this agent is already waiting for its answer"
    answer = asyncio.get_running_loop().create_future()
    _waiting[address] = answer
    try:
        status = await _context().send(address, _message(text))
        if _failed(status):
            return None, f"the question could not be delivered ({getattr(status, 'detail', '')})"
        return (await asyncio.wait_for(answer, timeout))[:ANSWER_MAX], ""
    except asyncio.TimeoutError:
        return None, f"no answer within {timeout} s"
    finally:
        _waiting.pop(address, None)


def ask(address, text, timeout=TIMEOUT_SECONDS) -> tuple:
    """(answer, "") or (None, why). Blocking — call it from a tool's thread,
    never from the event loop."""
    if _loop is None or _context is None:
        return None, "the agent has not finished starting"
    future = asyncio.run_coroutine_threadsafe(_ask(address, text[:QUESTION_MAX], timeout), _loop)
    try:
        return future.result(timeout + 15)
    except Exception as e:
        future.cancel()
        return None, f"asking failed: {e}"


def expecting(address) -> bool:
    return address in _waiting


def deliver(address, text) -> bool:
    """The first text an awaited agent sends is its answer. True if it was
    awaited; False means nobody is waiting (late, or never asked)."""
    answer = _waiting.get(address)
    if answer is None or answer.done():
        return False
    answer.set_result(str(text))
    return True
