"""uAgent wiring for the behavioural record: start the listener, keep the
watchlist resolved, and keep the record inside its retention window.

Only the registration lives here. The listener itself is core.observation, so
that the decisions it makes stay importable — and testable — without uagents
and without a socket. This module is imported by main.py for its side effects,
the same way scheduled_tasks and the REST modules are.
"""
import asyncio
import logging

from uagents import Context

from core import activity
from core import backfill
from core import observation
from core import record
from core import watchlist
from core.uagent import agent_1
from tools.tool_homeassistant import automations, registry

logger = logging.getLogger(__name__)

# How often the registry is re-read. Entities appear on a device long after it
# was first paired — a firmware update exposing a new sensor, an integration
# adding one — and "watch this device" has to keep meaning all of it.
RESOLVE_INTERVAL_SECONDS = 900

# Retention is a day-scale concern; hourly is frequent enough to keep the
# record inside its window and rare enough to be invisible.
PRUNE_INTERVAL_SECONDS = 3600

# The relations scan is one ordered pass over the live events table, bounded
# by the same retention window prune() enforces — hourly is cheap enough for
# both and keeps them on the same cadence.
RELATIONS_INTERVAL_SECONDS = 3600

# ...but not on the same tick. Both are whole-record passes and SQLite takes
# one writer at a time, so starting them together means one of them waits with
# the write lock in sight while the observation thread waits behind both.
# Sleeping once on the first run shifts this task's phase for good.
RELATIONS_OFFSET_SECONDS = 1800


async def _in_executor(fn, *args):
    """Runs a blocking Home Assistant or SQLite call off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


@agent_1.on_event("startup")
async def start_observing(ctx: Context):
    """Starts the listener thread.

    It costs nothing on a fresh install: with an empty watchlist the loop
    holds no connection at all, and picks up the first selection saved in the
    panel within seconds."""
    observation.start()
    watched = watchlist.watched_entities()
    ctx.logger.info(
        f"OBSERVATION: {len(watched)} entity(ies) selected for the behavioural record"
        + ("" if watched else " — choose some in the add-on panel to start recording")
    )


@agent_1.on_interval(period=RESOLVE_INTERVAL_SECONDS)
async def resolve_watchlist(ctx: Context):
    """Re-resolves watched devices against the registry, and warms anything
    that has newly appeared on one.

    Also refreshes the entity-name cache, so the Record tab stays correctly
    labelled even for a user who never opens the picker again."""
    ok, data = await _in_executor(registry.fetch_registry)
    if not ok:
        # Home Assistant restarting is the ordinary cause. The watchlist keeps
        # its last resolution and nothing stops being watched.
        logger.warning(f"OBSERVATION: could not re-read the registry ({data}) — keeping the last one")
        return

    observation.remember_names(registry.names_from(data))
    known = {e.get("entity_id") for e in data["entities"] if isinstance(e, dict)}
    before = set(watchlist.watched_entities())
    newly_watched = watchlist.resolve(registry.entities_by_device(data["entities"]))
    if newly_watched:
        logger.info(f"OBSERVATION: {len(newly_watched)} entity(ies) appeared on a watched device")
        await _in_executor(backfill.backfill, newly_watched)

    # ...and the other direction. §11 of the design: a referenced entity that
    # disappears must be surfaced, not silently stop producing events — an
    # automation may depend on it, and the record it was building just ended.
    # Urgent because it is not something the user finds out any other way.
    #
    # The automation lookup only happens here, on an entity that has actually
    # gone — never on the tick itself — because it costs one HA call per
    # automation and this loop runs every RESOLVE_INTERVAL_SECONDS.
    gone = sorted(e for e in before if e and e not in known)
    for entity_id in gone:
        name = observation.name_for(entity_id)
        logger.warning(f"OBSERVATION: watched entity {entity_id} is no longer in Home Assistant")
        ok, names_or_error = await _in_executor(automations.find_dependent_automations, entity_id)
        if not ok:
            dependents_clause = f"Dependent automations could not be checked ({names_or_error})."
        elif names_or_error:
            dependents_clause = f"Depended on by: {', '.join(names_or_error)}."
        else:
            dependents_clause = "No automation references it directly."
        activity.queue(
            "entity_gone", urgency="urgent",
            title="A watched device has disappeared",
            body=f"{name} ({entity_id}) is no longer in Home Assistant. {dependents_clause} "
                 f"The agent can no longer observe it.",
            payload={
                "entity_id": entity_id,
                "dependent_automations": names_or_error if ok else None,
            },
        )


@agent_1.on_interval(period=PRUNE_INTERVAL_SECONDS)
async def prune_record(ctx: Context):
    """Keeps the record inside both of its bounds: the retention window first,
    then the size cap.

    In that order, and both every hour. The window is the ordinary bound and
    costs one cheap DELETE when nothing is old enough; the cap only does
    anything on a home where time alone was never going to be enough, and it
    measures what the window pass left behind rather than what it found."""
    await _in_executor(record.prune)
    await _in_executor(record.enforce_cap)


_relations_first_run = True


@agent_1.on_interval(period=RELATIONS_INTERVAL_SECONDS)
async def scan_relations_task(ctx: Context):
    """Recomputes observed co-occurrences between watched entities."""
    global _relations_first_run
    if _relations_first_run:
        _relations_first_run = False
        await asyncio.sleep(RELATIONS_OFFSET_SECONDS)
    await _in_executor(record.scan_relations)
