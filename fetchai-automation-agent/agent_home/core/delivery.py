"""The delivery layer: how a message in the outbox reaches a person.

The core writes to core/activity.py and never learns which channel carried what
(design §4). This module is the other half of that boundary — it knows about
channels and nothing about why a message exists. When ASI1 gains an
agent-to-user path, or the dashboard card ships, each becomes another consumer
of the same rows rather than a change to every producer.

Two channels today, because that is what exists:

  push   — a Home Assistant notification. The ONLY way the agent can reach
           someone who is not currently talking to it, which is why urgent
           messages and nothing else go this way: a digest pushed here would
           teach the user to ignore the channel that matters.
  chat   — ASI1, pulled. The user asks what is new; the agent answers with
           whatever is waiting.

A message that could not be delivered stays pending. Nothing is deleted on
failure, and nothing is marked delivered hopefully — §11's "outbox holds;
nothing is lost" is only true if a failed push leaves the row exactly as it
found it.
"""
import logging

from core import activity
from core import settings
from tools.tool_homeassistant._client import _ha_post

logger = logging.getLogger(__name__)

# How many messages one drain sends. A backlog after a long outage should not
# arrive as fifty notifications at once.
PUSH_BATCH = 5


def _push_target() -> tuple:
    """The (domain, service) an urgent message is sent through.

    Defaults to a persistent notification: it needs no configuration, it exists
    on every install, and it shows in the Home Assistant sidebar. A configured
    notify_service takes over when the user wants it on their phone, and is
    accepted with or without its domain — "notify.mobile_app_x" and
    "mobile_app_x" are the same thing to everyone except an API call.
    """
    configured = (settings.NOTIFY_SERVICE or "").strip()
    if not configured:
        return "persistent_notification", "create"
    if "." in configured:
        domain, _, service = configured.partition(".")
        return domain, service
    return "notify", configured


def _send(message: dict) -> bool:
    """Delivers one message through the push channel. True if it went."""
    domain, service = _push_target()
    body = {"title": message.get("title") or "Home Assistant Agent",
            "message": message.get("body") or ""}
    if domain == "persistent_notification":
        # Addressable, so a later version can dismiss or replace one rather
        # than leaving a pile of them in the sidebar.
        body["notification_id"] = f"fetchai_agent_{message.get('id')}"
    ok, data = _ha_post(f"/services/{domain}/{service}", body)
    if not ok:
        logger.warning(f"DELIVERY: could not push message {message.get('id')} via {domain}.{service}: {data}")
    return ok


def push_urgent() -> int:
    """Sends the urgent messages that have not been pushed yet, returning how
    many went. Safe to call on a timer: an empty outbox is a no-op."""
    sent = 0
    for message in activity.pending("push", limit=PUSH_BATCH):
        if not _send(message):
            # Left pending deliberately. Home Assistant restarting, or a notify
            # target that no longer exists, must delay a message rather than
            # consume it.
            break
        activity.mark(message["id"], "push")
        sent += 1
    if sent:
        logger.info(f"DELIVERY: pushed {sent} urgent message(s)")
    return sent


def collect_for_chat(limit: int = 10) -> list:
    """Takes what is waiting for the conversational channel and marks exactly
    that as collected.

    Marked here rather than by the caller: whatever this returns has been handed
    over, and a message counted as delivered but never shown is the one failure
    mode a pull channel has.
    """
    messages = activity.pending("chat", limit=limit)
    for message in messages:
        activity.mark(message["id"], "chat")
    if messages:
        logger.info(f"DELIVERY: {len(messages)} message(s) collected over chat")
    return messages
