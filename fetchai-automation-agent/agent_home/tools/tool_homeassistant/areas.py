"""Area (room) support: the registry index that maps entities to the area
they live in, plus the LLM tools for listing areas and the entities in one.

Home Assistant stores areas in the area registry, which is only reachable over
the websocket API. An entity belongs to an area either directly (its entity
registry entry has an area_id) or, far more commonly, through the device it
belongs to. Both paths are resolved here so 'the kitchen light' can be answered
without asking the user which entity they meant.
"""
from typing import Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import disclosure
from ._client import _ha_get, _ha_ws_calls


def build_area_index():
    """Fetches the area/device/entity registries and returns
    (True, {"areas": {...}, "entity_area": {...}}) or (False, error).

    "areas"       maps area_id -> {area_id, name, aliases, floor_id}
    "entity_area" maps entity_id -> area_id (only entities that have one)
    """
    ok, results = _ha_ws_calls([
        {"type": "config/area_registry/list"},
        {"type": "config/device_registry/list"},
        {"type": "config/entity_registry/list"},
    ])
    if not ok:
        return False, results

    area_rows, device_rows, entity_rows = results
    areas = {
        a.get("area_id"): {
            "area_id": a.get("area_id"),
            "name": a.get("name", ""),
            "aliases": a.get("aliases", []) or [],
            "floor_id": a.get("floor_id"),
        }
        for a in (area_rows or [])
        if isinstance(a, dict) and a.get("area_id")
    }

    device_area = {
        d.get("id"): d.get("area_id")
        for d in (device_rows or [])
        if isinstance(d, dict) and d.get("id") and d.get("area_id")
    }

    entity_area = {}
    for e in (entity_rows or []):
        if not isinstance(e, dict):
            continue
        eid = e.get("entity_id")
        if not eid:
            continue
        # An area set on the entity itself overrides the one it inherits from
        # its device — that is exactly what the HA UI does.
        area_id = e.get("area_id") or device_area.get(e.get("device_id"))
        if area_id:
            entity_area[eid] = area_id

    return True, {"areas": areas, "entity_area": entity_area}


def resolve_area(index: dict, area: str):
    """Resolves a user-spoken room name to area_ids.

    Returns a list of matching area_ids: exact (case-insensitive) matches on
    the area name, an alias or the area_id itself win outright; otherwise
    substring matches are returned so the caller can report the ambiguity.
    """
    needle = (area or "").strip().lower()
    if not needle:
        return []
    exact, partial = [], []
    for area_id, a in index["areas"].items():
        names = [a["name"], area_id] + list(a["aliases"])
        lowered = [str(n).lower() for n in names if n]
        if needle in lowered:
            exact.append(area_id)
        elif any(needle in n or n in needle for n in lowered):
            partial.append(area_id)
    return exact or partial


def _states_by_entity():
    """Returns (True, {entity_id: state_dict}) from HA Core's /states."""
    ok, data = _ha_get("/states")
    if not ok:
        return False, data
    if not isinstance(data, list):
        return False, "Unexpected response shape from HA /states"
    return True, {s.get("entity_id", ""): s for s in data if isinstance(s, dict)}


def entities_in_area(index: dict, area_ids: list, states: dict, domain: str = ""):
    """Returns the entity rows belonging to any of the given areas, optionally
    filtered to a single domain."""
    wanted = set(area_ids)
    out = []
    for entity_id, area_id in index["entity_area"].items():
        if area_id not in wanted:
            continue
        entity_domain = entity_id.split(".")[0]
        if domain and entity_domain != domain:
            continue
        state = states.get(entity_id) or {}
        attrs = state.get("attributes") or {}
        out.append({
            "entity_id": entity_id,
            "friendly_name": attrs.get("friendly_name", ""),
            "domain": entity_domain,
            "state": state.get("state", ""),
            "area": index["areas"].get(area_id, {}).get("name", area_id),
        })
    return sorted(out, key=lambda e: e["entity_id"])


class tool_ha_list_areas(BaseTool):
    """LLM tool that lists the areas (rooms) configured in Home Assistant."""
    name: str = "List Home Assistant areas"
    description: str = (
        "List the areas (rooms) configured in Home Assistant, e.g. 'Kitchen', 'Living room', "
        "'Bedroom'. Returns a JSON array of {area_id, name, aliases, entity_count, domains}. "
        "Use this whenever the user refers to a room to find out which areas exist and what kinds "
        "of devices are in them, then use 'List entities in Home Assistant area' to get the "
        "entities of that room."
    )

    def _run(self) -> str:
        """Lists all areas with how many entities of which domains they contain."""
        ok, index = build_area_index()
        if not ok:
            return disclosure.emit({"error": index})

        counts, domains = {}, {}
        for entity_id, area_id in index["entity_area"].items():
            counts[area_id] = counts.get(area_id, 0) + 1
            domains.setdefault(area_id, set()).add(entity_id.split(".")[0])

        areas = [
            {
                "area_id": a["area_id"],
                "name": a["name"],
                "aliases": a["aliases"],
                "entity_count": counts.get(a["area_id"], 0),
                "domains": sorted(domains.get(a["area_id"], set())),
            }
            for a in index["areas"].values()
        ]
        areas.sort(key=lambda a: a["name"].lower())
        return disclosure.emit({"areas": areas, "total": len(areas)})


class _HAAreaEntitiesInput(BaseModel):
    """Input schema for listing the entities of one area."""
    area: str = Field(
        description="The room the user is talking about — the area name ('Kitchen'), one of its "
                    "aliases, or the area_id ('kitchen'). Case-insensitive.",
        default="",
    )
    domain: str = Field(
        description="Optional domain filter, e.g. 'light', 'switch', 'cover', 'climate', 'sensor'. "
                    "Pass it when the user named a kind of device ('the kitchen light') so the "
                    "result contains only the candidates that matter.",
        default="",
    )


class tool_ha_list_area_entities(BaseTool):
    """LLM tool that lists the entities assigned to one area, optionally by domain."""
    name: str = "List entities in Home Assistant area"
    description: str = (
        "List the entities that belong to one area (room) of the home, optionally filtered to a "
        "domain such as 'light' or 'switch'. Returns {entity_id, friendly_name, domain, state, area} "
        "for each match. This is THE tool to use when the user names a room — 'the kitchen light', "
        "'turn off the bedroom' — because it resolves the room to concrete entity_ids. If exactly "
        "one entity comes back, use it directly instead of asking the user which entity they meant. "
        "Entities inherit the area of their device, so this covers devices assigned to a room too."
    )
    args_schema: Type[BaseModel] = _HAAreaEntitiesInput

    def _run(self, area: str = "", domain: str = "") -> str:
        """Resolves the area name and returns its entities, filtered by domain."""
        if not area:
            return disclosure.emit({"error": "area is required"})

        ok, index = build_area_index()
        if not ok:
            return disclosure.emit({"error": index})

        matches = resolve_area(index, area)
        if not matches:
            return disclosure.emit({
                "error": f"No area matching '{area}' was found",
                "available_areas": sorted(a["name"] for a in index["areas"].values()),
            })

        ok_states, states = _states_by_entity()
        if not ok_states:
            return disclosure.emit({"error": states})

        entities = entities_in_area(index, matches, states, domain)
        return disclosure.emit({
            "area_query": area,
            "matched_areas": [index["areas"][a]["name"] for a in matches],
            "domain": domain,
            "entities": entities,
            "total": len(entities),
            # Spelled out so the model does not fall back to asking the user
            # when the answer is already unambiguous.
            "unambiguous": len(entities) == 1,
        })
