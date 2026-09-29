"""uAgent chat protocol: protocol instance and message/ack handlers.
Relays chat text to the /rest/post handler in rest_chat.

A ChatMessage from an agent this home ASKS (core/peers.py `askable`, or one a
question is waiting on) is an answer, not a conversation: it goes to
core/outbound.py and stops there — no pairing reply, no model, no tools.

Any other agent reaches this home over plain chat too (2.28.0), in this order:
an APPROVED agent's message goes to core/peer_chat.py — its own conversation,
its level's tools only, never rest_chat — before the askable check, so an agent
this home asks and has also approved may write; an agent this home asks that
writes unprompted is recorded as a request to connect, SILENTLY (2.28.2) — two
agents that answer each other must not start a loop; a blocked sender gets nothing;
an unknown sender's non-code message is a request to connect, recorded for the
household to decide, with the pairing instructions for a person alongside,
because a person on ASI1 and an agent look the same from here.
"""
import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

from core import settings
from core import protocols
from core import outbound
from core import pairing
from core import peer_chat
from core import peers
from core.uagent import agent_1

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatMessage, ChatAcknowledgement, TextContent, chat_protocol_spec, StartSessionContent,
)

from rest.rest_chat import handle_post, handle_approve, ApprovalRequest
from core.agent_runtime import (
    format_approval_for_chat,
    parse_approval_decision,
    approval_expired,
    get_memory,
    _format_approval_message,
)
from core.text import parse_pairing_code
from handlers.peer_handlers import announce_request

logger = logging.getLogger(__name__)

chat_proto = Protocol(spec=chat_protocol_spec)
#chat_proto = Protocol(name="AgentChatProtocol", version="0.3.0")

_PAIRING_PROMPT = (
    "This agent is not paired with you yet, so it will not act on your messages.\n\n"
    "Open the **Fetch.ai Automation Agent** panel in Home Assistant, press **Pair a device**, "
    "and send me the six-digit code it shows."
)

# An unknown sender may be a person on ASI1 or another agent; from here the two
# look the same, so both are told their way in.
_AGENT_REQUEST_NOTE = (
    "\n\nIf you are an agent rather than a person: your request to connect has been passed to "
    "the household, who approve agents in their panel. Until they do, this home answers nothing."
)

# What the sender is told for each redeem_code outcome. Deliberately the same
# text for a wrong code and a code that was never issued: someone probing the
# address should not learn whether a pairing is currently open.
_REDEEM_REPLIES = {
    "paired": "Paired. This home is now bound to your address — tell me what you would like it to do.",
    "already_paired": "You are already paired with this home.",
    "wrong": "That code is not valid. Check the panel for the current code and try again.",
    "no_code": "That code is not valid. Check the panel for the current code and try again.",
    "expired": "That code has expired. Press **Pair a device** in the panel for a new one.",
    "locked_out": (
        "Too many incorrect codes — that code has been cancelled. "
        "Press **Pair a device** in the panel for a new one."
    ),
}


@chat_proto.on_message(ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage):
    """Handles an incoming chat message: greets on session start, ignores
    non-text content, checks the sender is paired with this home, acknowledges
    receipt, and forwards text to the LLM agent."""
    content = msg.content[0]

    if outbound.expecting(sender):
        await _handle_answer(ctx, sender, msg)
        return
    if peers.may_read(peers.level_of(sender)):
        await _handle_peer_chat(ctx, sender, msg)
        return
    if peers.may_be_asked(sender):
        await _handle_unprompted(ctx, sender, msg)
        return
    if peers.state_of(sender) == peers.BLOCKED and not pairing.is_paired(sender):
        ctx.logger.info(f"PEERS: ignored a chat message from blocked {sender}")
        return

    try:
        # Safely handle content type
        if isinstance(content, StartSessionContent):
            greeting = "Welcome! How can I help you today?" if pairing.is_paired(sender) else _PAIRING_PROMPT
            await ctx.send(sender, ChatMessage(timestamp=datetime.now(tz=timezone.utc),msg_id=msg.msg_id,content=[TextContent(text=greeting, type="text")]))
            return

        if isinstance(content, TextContent):
            # session is logged alongside sender to establish whether ASI1 gives
            # each user a distinct address or routes everyone through one gateway.
            ctx.logger.info(f"Got a message from {sender} (session {ctx.session}): {content.text}")
        else:
            ctx.logger.info(f"Got a non-text message from {sender}: {type(content).__name__}")
            await ctx.send(sender, ChatMessage(timestamp=datetime.now(tz=timezone.utc),msg_id=msg.msg_id,content=[TextContent(text="I am not able to process this kind of message (only text is supported).", type="text")]))
            return

        # PAIRING GATE. Everything past this point reads or changes the user's
        # home, so a sender this home has not bound gets no further: no stored
        # session, no LLM call, no tools, no ASI1 tokens spent. The approval
        # gate is no help here — a stranger would be confirming their own
        # request. See core/pairing.py.
        if not pairing.is_paired(sender):
            await _handle_unpaired(ctx, sender, content.text)
            return

        ctx.storage.set(str(ctx.session), sender)

        await ctx.send(
            sender,
            ChatAcknowledgement(timestamp=datetime.now(tz=timezone.utc), acknowledged_msg_id=msg.msg_id),
        )

        await send_message(ctx, sender, msg)
    except Exception as e:
        ctx.logger.error("Error: "+str(e))

async def _handle_peer_chat(ctx: Context, sender: str, msg: ChatMessage):
    """An approved agent wrote: acknowledged, and answered by core/peer_chat.py
    — its own conversation and its level's tools, never the person's."""
    try:
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(tz=timezone.utc),
                                                   acknowledged_msg_id=msg.msg_id))
    except Exception as e:
        ctx.logger.warning(f"PEERS: could not acknowledge {sender}: {e}")
    text = next((c.text for c in msg.content if isinstance(c, TextContent)), None)
    if not text:
        return
    ctx.logger.info(f"PEERS: chat from approved {sender} ({len(text)} chars)")
    try:
        answer = await peer_chat.reply(sender, text)
    except Exception as e:
        ctx.logger.error(f"PEERS: chat with {sender} failed: {e}")
        answer = "Something went wrong on this side; please try again later."
    if answer:
        await _send_text(ctx, sender, answer)


async def _handle_unprompted(ctx: Context, sender: str, msg: ChatMessage):
    """An agent this home asks wrote without being asked (or answered too late)
    and is not approved to write. Until 2.28.2 that was dropped, so an agent the
    household asks could never ask for access itself. Now its text becomes a
    request to connect, like any unknown sender's — acknowledged, announced
    once, and NOT answered: a reply to an agent that replies is a loop. A
    denied one's text is dropped."""
    try:
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(tz=timezone.utc),
                                                   acknowledged_msg_id=msg.msg_id))
    except Exception as e:
        ctx.logger.warning(f"PEERS: could not acknowledge {sender}: {e}")
    text = next((c.text for c in msg.content if isinstance(c, TextContent)), None)
    if not text or peers.state_of(sender) not in (None, peers.PENDING):
        ctx.logger.info(f"PEERS: dropped an unprompted message from {sender}")
        return
    state = peers.request(sender, purpose=text, level="read", via="chat")
    if state == peers.PENDING and peers.was_new(sender):
        announce_request(sender)
        peers.mark_notified(sender)
    ctx.logger.info(f"PEERS: unprompted message from asked agent {sender} -> request {state}")


async def _handle_answer(ctx: Context, sender: str, msg: ChatMessage):
    """An agent this home asks has written back. Acknowledged, as the chat
    protocol expects; its first text is the awaited answer; anything else it
    sends — late, unsolicited, or meant as instructions — is logged and dropped."""
    try:
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(tz=timezone.utc),
                                                   acknowledged_msg_id=msg.msg_id))
    except Exception as e:
        ctx.logger.warning(f"PEERS: could not acknowledge {sender}: {e}")
    text = next((c.text for c in msg.content if isinstance(c, TextContent)), None)
    if text is None:
        return
    if outbound.deliver(sender, text):
        ctx.logger.info(f"PEERS: answer from {sender} ({len(text)} chars)")
    else:
        ctx.logger.info(f"PEERS: dropped an unawaited message from {sender} ({len(text)} chars)")


@agent_1.on_event("startup")
async def _bind_outbound(ctx: Context):
    """Where core/outbound.py sends questions from: this loop, with a fresh
    context per question as interval handlers get."""
    outbound.bind(asyncio.get_running_loop(), agent_1._build_context)


async def _handle_unpaired(ctx: Context, sender: str, text: str):
    """Replies to a sender that is not bound to this home: redeems a pairing
    code if the message is one, otherwise explains how to pair."""
    code = parse_pairing_code(text)
    # An address the household has decided about as an AGENT (approved,
    # denied, blocked) or asks never becomes a person: a paired person may
    # answer YES to a change, and an agent approving its own request is exactly
    # what core/peers.py keeps apart. A merely PENDING request does not count —
    # a person's first "hello" makes one.
    if code is not None and peers.blocks_pairing(sender):
        ctx.logger.info(f"PAIRING: refused a code from {sender}, known as an agent")
        await _send_text(ctx, sender, "This address is connected as an agent, not a person, "
                                      "so it cannot pair for chat.")
        return
    if code is None:
        await _send_text(ctx, sender, _PAIRING_PROMPT + _agent_request(sender, text))
        return

    ok, reason = pairing.redeem_code(code, sender)
    ctx.logger.info(f"PAIRING: redemption by {sender} -> {reason}")
    if reason == "paired" and peers.state_of(sender) == peers.PENDING:
        # A person after all: the request their first message made goes.
        peers.remove(sender)
    await _send_text(ctx, sender, _REDEEM_REPLIES.get(reason, _PAIRING_PROMPT))


def _agent_request(sender, text) -> str:
    """Records an unknown sender's message as a request to connect as an agent,
    announced to the household once. Returns the note to append to the pairing
    instructions ("" when there is nothing to say — a denied agent)."""
    state = peers.request(sender, purpose=text, level="read", via="chat")
    note = _AGENT_REQUEST_NOTE if state in (peers.PENDING, "full") else ""
    if state == peers.PENDING and peers.was_new(sender):
        announce_request(sender)
        peers.mark_notified(sender)
    logger.info(f"PAIRING: message from unpaired sender {sender} -> agent request {state}")
    return note


async def _send_text(ctx: Context, sender, text: str):
    """Sends a plain text chat message back to the sender."""
    try:
        await ctx.send(sender, ChatMessage(
            timestamp=datetime.now(tz=timezone.utc),
            msg_id=uuid4(),
            content=[TextContent(type="text", text=text)],
        ), timeout=settings.MESSAGE_TIMEOUT)
    except Exception as e:
        ctx.logger.error("Error: "+str(e))

async def send_message(ctx: Context, sender, msg: ChatMessage):
    """Routes the chat text through the /rest/post handler and sends the
    LLM's completion back to the sender.

    While an approval is parked for this session the message is read as the
    YES/NO decision on it instead, since chat clients cannot reach the
    /rest/approve endpoint the panel's buttons use."""
    # Defence in depth. handle_message already gates on the binding and is the
    # only caller, but this is the function that reaches the LLM and the tools,
    # so it does not take the caller's word for it.
    if not pairing.is_paired(sender):
        ctx.logger.warning(f"PAIRING: blocked agent run for unpaired sender {sender}")
        return

    user_text = msg.content[0].text
    memory = get_memory(sender)
    pending = memory.get("pending_approval")
    notice = ""

    if pending and approval_expired(pending):
        memory["pending_approval"] = None
        pending = None
        notice = "_The earlier confirmation request expired and was cancelled._\n\n"
        ctx.logger.info(f"APPROVAL EXPIRED: sender={sender}")

    if pending:
        decision = parse_approval_decision(user_text)
        if decision is None:
            # Not an answer — hold the approval rather than guess at intent.
            friendly = _format_approval_message(pending["tool_name"], pending["tool_args"])
            await _send_text(
                ctx, sender,
                f"Please reply **YES** to confirm or **NO** to cancel.\n\n{friendly}",
            )
            return
        ctx.logger.info(f"APPROVAL DECISION: sender={sender} approved={decision}")
        completion = await handle_approve(
            ctx=ctx, req=ApprovalRequest(session_id=sender, approved=decision)
        )
    else:
        completion = await handle_post(
            ctx=ctx, req=protocols.Request(text=user_text, session_id=sender)
        )

    # The panel strips the [APPROVAL_REQUIRED] payload itself; chat clients only
    # render markdown, so hand them the config as fenced yaml instead.
    await _send_text(ctx, sender, notice + format_approval_for_chat(completion.text))

@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    """Logs receipt of a chat acknowledgement from the sender."""
    ctx.logger.info(f"Got an acknowledgement from {sender} for {msg.acknowledged_msg_id}")
