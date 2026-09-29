"""uAgent wiring for the delivery layer: drain the outbox, and close the work a
restart orphaned — approvals nobody can answer any more, and rollbacks that
never reached an outcome.

Only the registration lives here, the same way tasks/observation.py is only the
registration for the listener. core/delivery.py holds the channels so they stay
importable — and testable — without uagents.

The drain is on a timer rather than at the point a message is queued, because
the producer is not allowed to know a channel exists (design §4). Thirty seconds
is well inside what "urgent" means for a freezer that has been warming for six
hours, and it costs one Home Assistant call only when there is something to say.
"""
import asyncio
import logging

from uagents import Context

from core import activity
from core import delivery
from core import gate
from core import rollback
from core.uagent import agent_1

logger = logging.getLogger(__name__)

PUSH_INTERVAL_SECONDS = 30


@agent_1.on_event("startup")
async def close_orphaned_approvals(ctx: Context):
    """Expires every SESSION-BACKED change still waiting for an answer.

    A parked approval lives in the session memory of a process that no longer
    exists, so the question cannot be answered any more. Saying so is the point:
    a row that reads "awaiting your answer" forever is a lie the panel would
    tell indefinitely, and the user is entitled to know their yes never landed.

    That reason does not reach a change the agent raised itself, and neither
    does this sweep. Nothing was parked for it: the journal row carries the whole
    question, core/gate.py rebuilds the pending from it, and the answer was never
    going to arrive in this process's lifetime anyway — the proposal is meant to
    wait for someone to look. Expiring it would be the restart cancelling a
    question nobody has read yet.
    """
    expired = await asyncio.get_running_loop().run_in_executor(
        None, activity.expire_stale_proposals
    )
    if expired:
        ctx.logger.info(f"ACTIVITY: {expired} approval(s) did not survive the restart")

    # A rollback caught mid-flight is the worse half of the same problem: an
    # unanswered approval changed nothing, whereas nobody can say whether an
    # interrupted undo landed. It is settled here rather than left looking live,
    # and §11 makes saying so urgent.
    interrupted = await asyncio.get_running_loop().run_in_executor(
        None, rollback.close_interrupted
    )
    if interrupted:
        ctx.logger.warning(f"ROLLBACK: {interrupted} rollback(s) did not survive the restart")

    # FIX 1's sibling reaper: an approval claimed but not yet settled is the
    # same "nobody can tell whether it landed" shape a rollback caught
    # mid-flight leaves, one row up in the same journal — gate.apply now claims
    # before it executes, so this is the other half of that invariant.
    stuck_approvals = await asyncio.get_running_loop().run_in_executor(
        None, gate.close_interrupted
    )
    if stuck_approvals:
        ctx.logger.warning(f"GATE: {stuck_approvals} approval(s) did not survive the restart mid-write")


@agent_1.on_interval(period=PUSH_INTERVAL_SECONDS)
async def deliver(ctx: Context):
    """Pushes whatever urgent messages are waiting."""
    await asyncio.get_running_loop().run_in_executor(None, delivery.push_urgent)
