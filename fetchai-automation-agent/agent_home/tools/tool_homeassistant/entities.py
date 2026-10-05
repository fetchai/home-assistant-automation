"""LLM tools for listing Home Assistant entities (optionally filtered by
domain and area) and fetching a single entity's full state/attributes."""
from typing import Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import disclosure
from ._client import _ha_get
from .areas import build_area_index, resolve_area


class _HAListEntitiesInput(BaseModel):
    """Input schema for listing entities: optional domain and area filters."""
    domain: str = Field(
        description="Optional domain filter, e.g. 'light', 'switch', 'sensor', 'climate'. "
                    "Pass it whenever the user named a kind of device, so the answer is not "
                    "buried in hundreds of unrelated entities.",
        default="",
    )
    area: str = Field(
        description="Optional area (room) filter — the area name ('Kitchen'), an alias, or the "
                    "area_id. Pass it whenever the user named a room. Combine with domain to get "
                    "exactly the candidates that matter, e.g. domain='light', area='kitchen'.",
        default="",
    )


class tool_ha_list_entities(BaseTool):
    """LLM tool that lists HA entities with their state, domain and area."""
    name: str = "List Home Assistant entities"
    description: str = (
        "List the entities available in Home Assistant, optionally filtered by domain and/or area "
        "(room). Returns a compact JSON array of {entity_id, friendly_name, state, domain, area}. "
        "Use this to discover what devices, sensors, switches, lights, automations, etc. exist in "
        "the user's smart home. Match user-spoken names (e.g. 'living room lamp') against the "
        "friendly_name field. When the user named a room, pass area (and domain) — if exactly one "
        "entity comes back, use it instead of asking the user which entity they meant."
    )
    args_schema: Type[BaseModel] = _HAListEntitiesInput

    def _run(self, domain: str = "", area: str = "") -> str:
        """Fetches entity states, annotates them with their area, applies the
        domain/area filters and returns a simplified list (capped at 500)."""
        ok, data = _ha_get("/states")
        if not ok:
            return disclosure.emit({"error": data})
        if not isinstance(data, list):
            return disclosure.emit({"error": "Unexpected response shape from HA /states"})

        # The area registry lives behind the websocket API; if it is not
        # reachable the entity list is still useful, just without areas —
        # unless the caller asked to filter by area, which then cannot be honoured.
        ok_index, index = build_area_index()
        if not ok_index:
            if area:
                return disclosure.emit({"error": f"Cannot resolve area '{area}': {index}"})
            index = {"areas": {}, "entity_area": {}}

        wanted_areas = None
        if area:
            matches = resolve_area(index, area)
            if not matches:
                return disclosure.emit({
                    "error": f"No area matching '{area}' was found",
                    "available_areas": sorted(a["name"] for a in index["areas"].values()),
                })
            wanted_areas = set(matches)

        simplified = []
        for e in data:
            entity_id = e.get("entity_id", "")
            entity_domain = entity_id.split(".")[0]
            if domain and entity_domain != domain:
                continue
            area_id = index["entity_area"].get(entity_id)
            if wanted_areas is not None and area_id not in wanted_areas:
                continue
            simplified.append({
                "entity_id": entity_id,
                "friendly_name": (e.get("attributes") or {}).get("friendly_name", ""),
                "state": e.get("state", ""),
                "domain": entity_domain,
                "area": index["areas"].get(area_id, {}).get("name", "") if area_id else "",
            })

        total = len(simplified)
        truncated = False
        if len(simplified) > 500:
            simplified = simplified[:500]
            truncated = True
        return disclosure.emit({
            "entities": simplified,
            "domain": domain,
            "area": area,
            "_truncated": truncated,
            "total": total,
            "unambiguous": total == 1,
        })


class _HAEntityInput(BaseModel):
    """Input schema for fetching a single entity's details by id."""
    entity_id: str = Field(
        description="The full entity_id (e.g. 'sensor.kitchen_temperature', 'light.living_room'). "
                    "Get it from the 'List Home Assistant entities' tool first if you don't know it.",
        default="",
    )


class tool_ha_get_entity(BaseTool):
    """LLM tool that fetches a single entity's full state and attributes."""
    name: str = "Get Home Assistant entity details"
    description: str = (
        "Get the state, attributes, last_changed and last_updated timestamps for a single "
        "Home Assistant entity. Pass entity_id, e.g. 'sensor.fetchai_grid_power'. ALWAYS call "
        "'List Home Assistant entities' first if you don't know the exact entity_id. Attributes "
        "that would disclose a credential, a network address or the home's coordinates are "
        "withheld and marked as such — do not ask the user to supply them instead."
    )
    args_schema: Type[BaseModel] = _HAEntityInput

    def _run(self, entity_id: str = "") -> str:
        """Fetches the state and attributes for the given entity_id.

        The only tool that emits an entity's attributes at all, which is why it
        names the fields it keeps rather than handing over Home Assistant's
        state object whole. `context` is dropped outright — it carries the HA
        user_id and the context ids of whatever caused the change, none of which
        the model can use for anything. The attributes themselves go out through
        core.disclosure like everything else: an attributes blob is where a
        home's coordinates, camera tokens and network addresses actually live.
        """
        if not entity_id:
            return disclosure.emit({"error": "entity_id is required"})
        ok, data = _ha_get(f"/states/{entity_id}")
        if not ok:
            return disclosure.emit({"error": data})
        if not isinstance(data, dict):
            return disclosure.emit({"error": "Unexpected response shape from HA /states"})
        return disclosure.emit({
            "entity_id": data.get("entity_id", entity_id),
            "state": data.get("state", ""),
            "domain": str(data.get("entity_id", entity_id)).split(".")[0],
            "last_changed": data.get("last_changed", ""),
            "last_updated": data.get("last_updated", ""),
            "attributes": data.get("attributes") or {},
        })
