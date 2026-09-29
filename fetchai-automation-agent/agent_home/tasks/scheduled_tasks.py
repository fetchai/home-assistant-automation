"""Periodic uAgent interval tasks: chat session memory cleanup."""
import logging

from uagents import Context

from core import activity
from core.uagent import agent_1
from core.agent_runtime import now_ts, session_memory_store

logger = logging.getLogger(__name__)


# uagent periodical session history manager / cleaner
@agent_1.on_interval(period=60)
async def manage_memory(ctx: Context):
    """Every minute, trims overlong chat histories and evicts session
    memory entries that have been inactive too long."""
    max_messages = 20
    max_inactive_time_s = 3600 #one hour
    global session_memory_store
    for idx in list(session_memory_store.keys()):
        if len(session_memory_store[idx]["messages"]) > (max_messages * 2):
            session_memory_store[idx]["messages"] = session_memory_store[idx]["messages"][-max_messages * 2:]
        if now_ts() - session_memory_store[idx]["timestamp"] > max_inactive_time_s:
            # An approval parked on this session is being thrown away with it.
            # Closing the journal row first is what stops a user's "yes",
            # typed an hour later, disappearing into nothing with no record
            # that it was ever asked.
            pending = session_memory_store[idx].get("pending_approval")
            if pending and pending.get("change_id"):
                activity.record_decision(
                    pending["change_id"], "expired", status="not_applied",
                    result="The session went idle before this was answered.",
                )
                activity.withdraw(pending.get("notice_id"))
            del session_memory_store[idx]
    return
