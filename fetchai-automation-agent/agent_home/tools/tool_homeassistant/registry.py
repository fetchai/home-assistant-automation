"""The device/entity tree the observation picker is built from.

The agent already walks Home Assistant's registries for the LLM's discovery
tools — areas.build_area_index for rooms, devices.py for hardware. What the
picker needs is the same three registries joined the other way round: areas
holding devices holding entities, so a user can tick a whole device or reach
inside it for one entity.

The join is a pure function taking registry rows, because the awkward cases
are all data shapes — an entity with no device, a device with no area, an
entity pointing at an area that no longer exists — and those are far easier to
be sure about with canned rows than against a live home.
"""
from ._client import _ha_get, _ha_ws_calls

# Only entities that could serve one of the suggestion types in scope are
# offered. The design is explicit that the record is scoped rather than
# exhaustive (§6.2) — and every domain listed here is a domain whose history
# the agent would actually reason over. The rest (automations, scripts, scenes,
# updates, the weather forecast) are either the agent's own output or things it
# cannot act on, and would only be noise in the picker and bulk in the log.
WATCHABLE_DOMAINS = frozenset({
    "alarm_control_panel", "binary_sensor", "climate", "cover", "device_tracker",
    "fan", "humidifier", "input_boolean", "light", "lock", "media_player",
    "number", "person", "sensor", "switch", "vacuum", "valve", "water_heater",
})


def entities_by_device(entity_rows) -> dict:
    """Maps {device_id: [entity_id, ...]} from entity registry rows.

    Shared with core.watchlist's resolution step, which is what makes "watch
    this device" mean the same set of entities in the picker and in the
    observation loop. Disabled entities are excluded here too, matching the
    filter build_registry_tree applies for display — otherwise ticking a
    device would silently watch entities that were never shown as rows."""
    out = {}
    for e in (entity_rows or []):
        if (
            isinstance(e, dict)
            and e.get("device_id")
            and e.get("entity_id")
            and not e.get("disabled_by")
        ):
            out.setdefault(e["device_id"], []).append(e["entity_id"])
    for entities in out.values():
        entities.sort()
    return out


def fetch_registry():
    """Reads the three registries and the current states in one round trip
    each, returning (True, {...}) or (False, error).

    States are fetched too because the picker shows what each entity is doing
    right now — an entity reading 'unavailable' is one a user probably does not
    want to watch, and that is only visible here."""
    ok, results = _ha_ws_calls([
        {"type": "config/area_registry/list"},
        {"type": "config/device_registry/list"},
        {"type": "config/entity_registry/list"},
    ])
    if not ok:
        return False, results
    area_rows, device_rows, entity_rows = results

    ok_states, states = _ha_get("/states")
    if not ok_states:
        return False, states
    if not isinstance(states, list):
        return False, "Unexpected response shape from HA /states"

    return True, {
        "areas": area_rows or [],
        "devices": device_rows or [],
        "entities": entity_rows or [],
        "states": {s.get("entity_id", ""): s for s in states if isinstance(s, dict)},
    }


def names_from(snapshot: dict) -> dict:
    """Pulls {entity_id: friendly name} out of a registry snapshot.

    The record stores entity ids and nothing else — names belong to Home
    Assistant and change under it — so the panel's labels come from a cache
    that registry fetches refill on the way past."""
    names = {}
    for entity_id, state in (snapshot.get("states") or {}).items():
        friendly = (state.get("attributes") or {}).get("friendly_name")
        if friendly:
            names[entity_id] = friendly
    for row in (snapshot.get("entities") or []):
        # A name the user set on the entity beats the integration's own.
        if isinstance(row, dict) and row.get("entity_id") and row.get("name"):
            names[row["entity_id"]] = row["name"]
    return names


def _entity_node(entity_id, registry_row, states, watched):
    state = states.get(entity_id) or {}
    attrs = state.get("attributes") or {}
    name = (
        (registry_row or {}).get("name")
        or attrs.get("friendly_name")
        or (registry_row or {}).get("original_name")
        or entity_id
    )
    return {
        "entity_id": entity_id,
        "name": name,
        "domain": entity_id.split(".")[0],
        "state": state.get("state", ""),
        "watched": watched,
    }


def build_registry_tree(area_rows, device_rows, entity_rows, states, selection) -> dict:
    """Joins the registries into areas → devices → entities for the picker.

    `selection` is core.watchlist.selection(): its "devices" and "entities"
    lists are what each node is marked against, so the picker redraws exactly
    what the user ticked rather than a flattened resolution of it. A device is
    "watched" when it was selected as a whole and "partial" when only some of
    its entities were picked individually.
    """
    states = states or {}
    selected_devices = set((selection or {}).get("devices") or [])
    selected_entities = set((selection or {}).get("entities") or [])

    areas = {}
    for a in (area_rows or []):
        if isinstance(a, dict) and a.get("area_id"):
            areas[a["area_id"]] = {"area_id": a["area_id"], "name": a.get("name") or a["area_id"]}

    device_rows = [d for d in (device_rows or []) if isinstance(d, dict) and d.get("id")]
    devices = {d["id"]: d for d in device_rows}

    # Entities that Home Assistant itself has switched off produce no states and
    # never will, so offering them would only be a way to watch nothing.
    registry = {
        e["entity_id"]: e
        for e in (entity_rows or [])
        if isinstance(e, dict) and e.get("entity_id") and not e.get("disabled_by")
    }

    # An entity can exist in /states without a registry entry — YAML-configured
    # template sensors are the common case, and they are perfectly watchable.
    all_entity_ids = set(registry) | set(states)

    buckets = {}   # area_id (or None) -> {"devices": {device_id: [entity]}, "entities": [entity]}
    hidden = 0
    for entity_id in sorted(all_entity_ids):
        if entity_id.split(".")[0] not in WATCHABLE_DOMAINS:
            hidden += 1
            continue
        row = registry.get(entity_id)
        device_id = (row or {}).get("device_id")
        device = devices.get(device_id) if device_id else None

        # An area set on the entity itself overrides the one it inherits from
        # its device — exactly what the HA UI does, and what areas.py already
        # assumes everywhere else.
        area_id = (row or {}).get("area_id") or (device or {}).get("area_id")
        if area_id not in areas:
            area_id = None

        watched = entity_id in selected_entities or (device_id in selected_devices if device_id else False)
        node = _entity_node(entity_id, row, states, watched)

        bucket = buckets.setdefault(area_id, {"devices": {}, "entities": []})
        if device is not None:
            bucket["devices"].setdefault(device_id, []).append(node)
        else:
            bucket["entities"].append(node)

    out_areas = []
    watched_total = 0
    for area_id, bucket in buckets.items():
        area_devices = []
        for device_id, entities in bucket["devices"].items():
            d = devices[device_id]
            picked = sum(1 for e in entities if e["watched"])
            watched_total += picked
            area_devices.append({
                "device_id": device_id,
                "name": d.get("name_by_user") or d.get("name") or device_id,
                "manufacturer": d.get("manufacturer", ""),
                "model": d.get("model", ""),
                "entities": entities,
                "watched_count": picked,
                # "watched" is the user's tick on the device itself; "partial"
                # is some-but-not-all of its entities, however they were
                # reached. The picker renders those as the two checkbox states,
                # and all-picked-individually reads as fully checked — which is
                # what it looks like, and what unticking it should undo.
                "watched": device_id in selected_devices,
                "partial": 0 < picked < len(entities),
            })
        watched_total += sum(1 for e in bucket["entities"] if e["watched"])
        area_devices.sort(key=lambda d: str(d["name"]).lower())
        bucket["entities"].sort(key=lambda e: str(e["name"]).lower())
        out_areas.append({
            "area_id": area_id or "",
            # Entities and devices with no area are real and often the ones a
            # user most wants to watch, so they get a bucket rather than being
            # dropped from the picker.
            "name": areas[area_id]["name"] if area_id else "No area",
            "devices": area_devices,
            "entities": bucket["entities"],
        })

    # Unassigned last; everything else alphabetically.
    out_areas.sort(key=lambda a: (a["area_id"] == "", str(a["name"]).lower()))
    return {
        "areas": out_areas,
        "watched_count": watched_total,
        "hidden_count": hidden,
    }
