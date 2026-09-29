"""REST handlers backing the panel's Watching and Record tabs: what the agent
is allowed to observe, and what it has observed so far.

These are POST even where GET would read more naturally, for the same reason
the pairing endpoints are: proxy.py answers GET from the built panel directory
and only forwards POST to the agent.

Every Home Assistant call here is blocking — the registry is a websocket round
trip, the recorder is HTTP — so each one goes through the executor rather than
stalling the uAgent's event loop while a panel refresh runs.
"""
import asyncio
import logging

from core import backfill
from core import observation
from core import protocols
from core import record
from core import watchlist
from core.uagent import agent_1
from tools.tool_homeassistant import registry

from uagents import Context

logger = logging.getLogger(__name__)


async def _in_executor(fn, *args):
    """Runs a blocking Home Assistant call off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _tree_response(data: dict) -> protocols.ObservationRegistryResponse:
    """Builds the picker payload from a registry snapshot and the current
    selection."""
    observation.remember_names(registry.names_from(data))
    selection = watchlist.selection()
    tree = registry.build_registry_tree(
        data["areas"], data["devices"], data["entities"], data["states"], selection
    )
    return protocols.ObservationRegistryResponse(
        areas=tree["areas"],
        selection=selection,
        watched_count=tree["watched_count"],
        hidden_count=tree["hidden_count"],
    )


def _named_relations(rows: list) -> list:
    """Widens raw relations rows with the friendly names the panel renders —
    the same join tool_record_relations does for the LLM, kept separate
    because REST and the tool shape their payloads differently."""
    return [
        {**r, "entity_a_name": observation.name_for(r["entity_a"]),
              "entity_b_name": observation.name_for(r["entity_b"])}
        for r in rows
    ]


def _spawn_backfill(entity_ids: list):
    """Warms newly watched entities from Home Assistant's recorder, without
    making the panel wait for it.

    Deliberately not awaited: fetching ten days for a freshly ticked room can
    take a while, and the user has already got the answer they asked for. The
    result shows up in the Record tab when it lands."""
    if not entity_ids:
        return

    def _run():
        try:
            result = backfill.backfill(entity_ids)
            observation.note_backfilled(result.get("since"))
        except Exception as e:
            # A bonus that fails to arrive must not take the request with it.
            logger.warning(f"OBSERVATION: backfill of newly watched entities failed: {e}")

    asyncio.get_running_loop().run_in_executor(None, _run)


@agent_1.on_rest_post("/rest/observation/registry", protocols.ObservationRequest,
                      protocols.ObservationRegistryResponse)
async def handle_registry(ctx: Context, req: protocols.ObservationRequest) -> protocols.ObservationRegistryResponse:
    """Returns the device/entity tree with the current selection marked on it."""
    ok, data = await _in_executor(registry.fetch_registry)
    if not ok:
        logger.warning(f"OBSERVATION: could not read the registry: {data}")
        return protocols.ObservationRegistryResponse(error=str(data))
    return _tree_response(data)


@agent_1.on_rest_post("/rest/observation/selection", protocols.ObservationRequest,
                      protocols.ObservationRegistryResponse)
async def handle_selection(ctx: Context, req: protocols.ObservationRequest) -> protocols.ObservationRegistryResponse:
    """Replaces what the agent watches, and warms whatever is newly watched.

    The registry is re-read first and the selection resolved against that same
    snapshot: saving against a registry we could not fetch would silently drop
    every entity reached through a device, which is a selection the user never
    made. So a failed fetch saves nothing and says so.
    """
    ok, data = await _in_executor(registry.fetch_registry)
    if not ok:
        logger.warning(f"OBSERVATION: refusing to save a selection without the registry: {data}")
        return protocols.ObservationRegistryResponse(error=str(data))

    before = set(watchlist.watched_entities())
    watchlist.set_selection(req.devices, req.entities, registry.entities_by_device(data["entities"]))
    newly_watched = sorted(set(watchlist.watched_entities()) - before)
    if newly_watched:
        _spawn_backfill(newly_watched)

    return _tree_response(data)


@agent_1.on_rest_post("/rest/observation/record", protocols.ObservationRequest,
                      protocols.ObservationRecordResponse)
async def handle_record(ctx: Context, req: protocols.ObservationRequest) -> protocols.ObservationRecordResponse:
    """Returns what the record holds, per watched entity, plus the health of
    the link that fills it.

    The per-entity merge is observation.coverage(), shared with the record tool
    the LLM calls — the tab and the agent must not be able to disagree about
    what has been observed.
    """
    return protocols.ObservationRecordResponse(
        status=observation.status(),
        entities=observation.coverage(),
        totals=record.stats(),
        relations=_named_relations(record.relations(limit=20)),
    )


@agent_1.on_rest_post("/rest/observation/entity", protocols.ObservationRequest,
                      protocols.ObservationEntityResponse)
async def handle_entity(ctx: Context, req: protocols.ObservationRequest) -> protocols.ObservationEntityResponse:
    """Returns one entity's recent transitions and its aggregates."""
    entity_id = (req.entity_id or "").strip()
    if not entity_id:
        return protocols.ObservationEntityResponse(error="entity_id is required")
    return protocols.ObservationEntityResponse(
        entity_id=entity_id,
        name=observation.name_for(entity_id),
        recent=record.recent(entity_id, limit=req.limit),
        profile=record.profile(entity_id, days=req.days),
        relations=_named_relations(record.relations(entity_id, limit=10)),
    )
