"""uAgent wiring for the analyst: run it once a day, off the event loop.

Only the registration lives here, as tasks/review.py is only the registration
for the review pass. Whether a run is due, what it reads, what it asks the model
and what it stores are all core/analyst.py's, importable and testable without
uagents.

The one thing that differs from tasks/review.py is where the last run is kept.
The review stamps an in-process variable, because a restart costs it at most one
extra free pass. An analyst run costs money and sends the house's data to ASI1
unattended (design §10.1), and the strong-tier cap is a daily ceiling, not a
guard against running twice — so the stamp is `analyst.stamp_run`'s file, written BEFORE the run starts, and a
stamp that cannot be written means no run (D-10: fails closed). A run that died
halfway is not retried the same day; tomorrow's reads the same house.

ANALYST_HOUR (07:00) is two hours before REVIEW_HOUR on purpose: the analyst
only stores rows, it never asks anything (M-1). The morning's review ranks what
it stored beside the detectors' rows, and the day's one question still goes out
through review._propose, the only route to a person.

Blocking throughout — HA reads, SQLite, and the model calls — so it goes to an
executor, for the same reason the review does: the event loop is what serves
/rest/post while this runs.
"""
import asyncio
import logging
import time

from uagents import Context

from core import analyst
from core.uagent import agent_1

logger = logging.getLogger(__name__)

# Four times an hour, as the review. The period is not the cadence —
# `analyst.due` is — it only has to notice ANALYST_HOUR arriving.
ANALYST_INTERVAL_SECONDS = 900


@agent_1.on_interval(period=ANALYST_INTERVAL_SECONDS)
async def analyst_pass(ctx: Context):
    """Design §15.2 once a day: the model explores the record and submits at
    most a few ideas; the house checks decide which are stored, each with a
    chart drawn from the record (core/analyst.run_explorer)."""
    now = int(time.time())
    if not analyst.due(now, analyst.last_run_date(now)):
        return

    # Stamped before the run, and persisted: a restart during or after it must
    # not start a second one today. No stamp, no run.
    if not analyst.stamp_run(now):
        ctx.logger.error("ANALYST: could not stamp today's run — not running it")
        return
    try:
        await asyncio.get_running_loop().run_in_executor(None, analyst.run_explorer, now)
    except Exception as e:
        # Nothing is half-stored: each idea is checked and stored on its own
        # (D-9). What did not happen today is tomorrow's run's.
        ctx.logger.error(f"ANALYST: the daily run failed: {e}")
