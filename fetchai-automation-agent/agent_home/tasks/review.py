"""uAgent wiring for the review pass: run it once a day, off the event loop.

Only the registration lives here, the same way tasks/observation.py is only the
registration for the listener and tasks/delivery.py only the drain. Everything
the pass decides — including whether it is due — is in core/review.py, so that
it stays importable, and testable, without uagents and without a clock this
process happens to be running under.

The interval is not the cadence. It ticks often enough to notice that the local
day has rolled over and that REVIEW_HOUR has arrived, and `review.due` decides
whether that means anything; a `period` of a day would run the pass at whatever
time the add-on last restarted, which is the one thing the fixed hour exists to
avoid.

Blocking throughout — SQLite reads over thirty days per watched sensor, one
registry round trip, a preflight per candidate — so it goes to an executor. The
event loop is what serves /rest/post while this runs, and a review that made
the panel wait would be a background task the user can feel.
"""
import asyncio
import logging
import time

from uagents import Context

from core import review
from core.uagent import agent_1

logger = logging.getLogger(__name__)

# Four times an hour. Cheap — `review.due` is two comparisons — and fine enough
# that the pass starts within fifteen minutes of REVIEW_HOUR rather than at
# whatever offset the process happens to have been started at.
REVIEW_INTERVAL_SECONDS = 900

# When the pass last STARTED, for review.due. In process, and deliberately not
# persisted: what a restart could cause is one extra run on a day that already
# had one, and the volume rule that actually matters — one new question a day —
# is counted from the suggestion store instead, where a restart cannot reach
# it. See review.proposals_allowed.
_last_run_at = None


@agent_1.on_interval(period=REVIEW_INTERVAL_SECONDS)
async def review_pass(ctx: Context):
    """Design §7 stages 3 to 5, once a day: observe what the record holds,
    grade the change it implies, and put at most one question to the user."""
    global _last_run_at
    now = int(time.time())
    if not review.due(now, _last_run_at):
        return

    # Stamped before the run rather than after it, so that a pass which throws
    # or takes a long time is not retried on every tick for the rest of the day.
    _last_run_at = now
    try:
        summary = await asyncio.get_running_loop().run_in_executor(None, review.run_once)
    except Exception as e:
        # The home stays controllable and the record keeps filling whatever
        # happens here (§11: "the agent going quiet is acceptable"). Tomorrow's
        # pass reads the same record and reaches the same candidates.
        ctx.logger.error(f"REVIEW: the daily pass failed: {e}")
        return

    if summary.get("proposed"):
        ctx.logger.info(f"REVIEW: {summary['proposed']} suggestion(s) put to the user")
