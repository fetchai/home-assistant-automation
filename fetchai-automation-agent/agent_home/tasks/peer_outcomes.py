"""Tells an agent how its proposals ended (agent-to-agent, phase 2).

A proposal is decided long after it arrives — by a person, in the panel, on
their own time — and the agent that raised it may be offline then. So the
outcome is not sent from the approval path (which knows nothing of agents, and
must not: core/gate.py's `apply` is the one exit for every change). Instead
this interval reads the journal for each proposal still unreported on its agent
(core/peers.py) and sends an `ActionStatus` once the change has settled. The
pending list lives in /data, so a restart forgets no outcome, and one that
cannot be delivered is tried again next minute — for a week after the proposal,
then given up on (an agent gone that long is not waiting for the answer).

A blocked agent is never told anything; a removed one is no longer in the list.
"""
import asyncio
import logging
import time

from uagents import Context

from core import activity
from core import disclosure
from core import peers
from core.peer_protocol import ActionStatus
from core.uagent import agent_1

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 60
GIVE_UP_SECONDS = 7 * 86400


def _failed(status) -> bool:
    """ctx.send reports a delivery it could not make by returning a status of
    `failed` (uagents_core DeliveryStatus), not by raising."""
    value = getattr(getattr(status, "status", None), "value", getattr(status, "status", None))
    return str(value) == "failed"


def outcome(row):
    """(state, detail) for a settled change, or None while it is still open —
    `proposed`, or approved and still being carried out (claimed, no status)."""
    if not row:
        return "withdrawn", "The change is no longer in the journal."
    decision = row.get("decision")
    status = row.get("status")
    if decision == "proposed" or (decision == "approved" and not status):
        return None
    detail = disclosure.text(str(row.get("result") or "")[:300])
    if decision == "approved":
        return ("applied" if status == "applied" else "failed"), detail
    if decision == "rejected":
        return "rejected", detail
    if decision == "expired":
        return "withdrawn", detail
    return str(decision), detail


async def report_outcomes(ctx: Context, now=None):
    now = int(now if now is not None else time.time())
    for address in peers.with_unreported():
        if peers.state_of(address) == peers.BLOCKED:
            continue
        for change_id in peers.unreported(address):
            row = await asyncio.to_thread(activity.change, change_id)
            settled = outcome(row)
            if settled is None:
                continue
            state, detail = settled
            try:
                sent = await ctx.send(address, ActionStatus(state=state, change_id=change_id,
                                                            detail=detail))
                failed = _failed(sent)
            except Exception as e:
                logger.warning(f"PEERS: could not tell {address} change {change_id} {state}: {e}")
                failed = True
            if failed:
                entry = peers.proposal(address, change_id) or {}
                if now - int(entry.get("proposed_at") or now) >= GIVE_UP_SECONDS:
                    peers.mark_reported(address, change_id, "undelivered")
                    logger.info(f"PEERS: gave up telling {address} about change {change_id}")
                continue
            peers.mark_reported(address, change_id, state)
            logger.info(f"PEERS: told {address} change {change_id} was {state}")


@agent_1.on_interval(period=INTERVAL_SECONDS)
async def _tick(ctx: Context):
    try:
        await report_outcomes(ctx)
    except Exception as e:
        logger.error(f"PEERS: reporting outcomes failed: {e}")
