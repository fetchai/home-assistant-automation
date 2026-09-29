"""REST handlers backing the panel's Activity tab: every change the agent
proposed, and every message it has queued.

Design §4 gives the panel one job the other surfaces cannot do — "a local record
of everything proposed, applied and rolled back" — and it exists precisely so
the audit does not depend on the connection being audited. That is why this
reads local SQLite and makes no Home Assistant call at all: it is the screen
that has to work when nothing else does.

The third verb — rolled back — is here too, and it is the only endpoint in this
file that writes anything. It does not go through the approval gate; core/rollback.py
argues why, and the panel button and this handler have to agree with it, so the
decision is made once over there rather than twice here.

POST like every other endpoint here, because proxy.py answers GET from the
built panel directory and only forwards POST to the agent.
"""
import asyncio
import logging

from core import activity
from core import gate
from core import protocols
from core import rollback
from core.uagent import agent_1

from uagents import Context

logger = logging.getLogger(__name__)


async def _in_executor(fn, *args):
    """Runs a blocking SQLite read off the uAgent's event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _snapshot(limit: int, error: str = "") -> protocols.ActivityResponse:
    # The one-line description comes from the gate rather than being rebuilt in
    # JavaScript, and the chat listing tool reads it from the same place: the
    # panel row, the chat message and the Home Assistant notification are all
    # describing the same change.
    changes = [
        {**row, "summary": gate.describe_change(row)}
        for row in activity.changes(limit=limit)
    ]
    return protocols.ActivityResponse(
        error=error,
        changes=changes,
        messages=activity.messages(limit=limit),
        totals=activity.stats(),
    )


@agent_1.on_rest_post("/rest/activity", protocols.ActivityRequest, protocols.ActivityResponse)
async def handle_activity(ctx: Context, req: protocols.ActivityRequest) -> protocols.ActivityResponse:
    """Returns the change journal and the outbox, newest first."""
    return await _in_executor(_snapshot, req.limit)


@agent_1.on_rest_post("/rest/activity/seen", protocols.ActivityRequest, protocols.ActivityResponse)
async def handle_seen(ctx: Context, req: protocols.ActivityRequest) -> protocols.ActivityResponse:
    """Marks the waiting messages as seen in the panel, and answers with the
    refreshed state.

    Seen is per channel, not global: a message read here is still pending for
    ASI1, because the person at the panel and the person in the chat are not
    necessarily the same person, and neither should silence the other.
    """
    def _mark_and_read():
        for message in activity.pending("panel", limit=200):
            activity.mark(message["id"], "panel")
        return _snapshot(req.limit)

    return await _in_executor(_mark_and_read)


@agent_1.on_rest_post("/rest/activity/rollback", protocols.ActivityRequest, protocols.ActivityResponse)
async def handle_rollback(ctx: Context, req: protocols.ActivityRequest) -> protocols.ActivityResponse:
    """Undoes one recorded change and answers with the refreshed journal.

    The refresh is not a convenience: whether the undo worked or not, the row
    that says so exists by the time this returns, and sending the listing back
    with it means the panel can never show an outcome that disagrees with the
    record it was read from.

    Unlike /rest/post this reaches Home Assistant, so it runs in the executor
    for the HTTP calls, not just for SQLite.
    """
    def _roll_and_read():
        outcome = rollback.roll_back(req.change_id, session_id="panel")
        # A refusal or a failure comes back in `error` — §11 wants it explicit
        # and actionable, and rollback.roll_back has already written the same
        # words into the outbox for whoever is not looking at this screen.
        return _snapshot(req.limit, error="" if outcome["ok"] else outcome["message"])

    response = await _in_executor(_roll_and_read)
    logger.info(f"ACTIVITY: rollback of change {req.change_id} requested from the panel")
    return response
