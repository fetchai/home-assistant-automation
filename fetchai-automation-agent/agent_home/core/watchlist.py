"""Which entities this home has asked the agent to watch.

This is the gate on the behavioural record: the observation loop stores a
transition only for an entity named here, so an entity that was never selected
is never watched and never written down. An empty watchlist therefore means
nothing is recorded at all, which is what a fresh install starts as — the
record has to be opted into, not opted out of.

A selection is kept as the user expressed it, not as a flattened list of
entity_ids. Choosing a whole DEVICE means "whatever this device turns out to
have", so entities added to it later are picked up on the next re-resolve;
choosing individual entities means exactly those. `resolved` caches the
concrete set the loop filters on, so it filters correctly from the first event
after a restart, before any registry call has had a chance to run.

State lives in /data alongside pairing.json, and for the same reason: /app is
part of the image and is wiped by every add-on rebuild.
"""
from datetime import datetime, timezone
import json
import logging
import os

from core import settings

logger = logging.getLogger(__name__)

STORE_PATH = os.path.join(settings.DATA_DIR, "watchlist.json")

# Loaded once and written through on every change; this process is the only
# writer, so the file never has to be re-read.
_state = None

# is_watched() is called for every state_changed event Home Assistant emits —
# every entity in the house, not just the watched ones — so the membership test
# is against a set, rebuilt whenever the selection changes.
_resolved_set = frozenset()


def _now():
    return int(datetime.now(tz=timezone.utc).timestamp())


def _empty_state():
    return {"version": 1, "devices": [], "entities": [], "resolved": [], "updated_at": 0}


def _load():
    """Returns the cached state, reading it from disk on first use.

    A missing, unreadable or corrupt file yields an empty watchlist. That fails
    closed in the privacy sense as well as the safety one: the agent falls back
    to watching nothing and the user re-selects, rather than the file's loss
    quietly widening what gets recorded."""
    global _state, _resolved_set
    if _state is not None:
        return _state
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("resolved"), list):
            raise ValueError("unexpected structure")
        for key in ("devices", "entities"):
            if not isinstance(data.get(key), list):
                data[key] = []
        data.setdefault("version", 1)
        data.setdefault("updated_at", 0)
        _state = data
    except FileNotFoundError:
        _state = _empty_state()
    except Exception as e:
        logger.warning(f"WATCHLIST: could not read {STORE_PATH} ({e}) — watching nothing")
        _state = _empty_state()
    _resolved_set = frozenset(_state["resolved"])
    return _state


def _save():
    """Writes the cached state to disk atomically, so an interrupted write
    cannot leave a half-written file that the next start would discard."""
    state = _load()
    tmp = f"{STORE_PATH}.tmp"
    try:
        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STORE_PATH)
    except Exception as e:
        logger.error(f"WATCHLIST: failed to persist {STORE_PATH}: {e}")


def resolve_selection(devices, entities, entities_by_device) -> list:
    """Turns a selection into the concrete entity set to watch.

    Pure — no I/O, no module state. `entities_by_device` is {device_id: [entity_id]}
    as the entity registry gives it. An entity picked directly is watched even
    if its device was not, and a device's entities are watched even though they
    were never named individually; that is the whole point of the two lists.
    """
    resolved = set(e for e in (entities or []) if e)
    by_device = entities_by_device or {}
    for device_id in (devices or []):
        resolved.update(by_device.get(device_id, []))
    return sorted(resolved)


def _apply(devices, entities, resolved):
    global _resolved_set
    state = _load()
    state["devices"] = sorted(set(d for d in devices if d))
    state["entities"] = sorted(set(e for e in entities if e))
    state["resolved"] = resolved
    state["updated_at"] = _now()
    _resolved_set = frozenset(resolved)
    _save()
    return state


def set_selection(devices, entities, entities_by_device) -> dict:
    """Replaces the selection and resolves it in one step.

    The registry map is a required argument rather than something fetched here:
    the caller needs the registry anyway to render the picker, and resolving
    from the same snapshot the user was looking at is what makes the saved
    result match what they ticked. Deselecting takes effect immediately —
    entities of a device that is no longer selected stop being watched here,
    not at the next re-resolve.
    """
    resolved = resolve_selection(devices, entities, entities_by_device)
    state = _apply(devices, entities, resolved)
    logger.info(
        f"WATCHLIST: watching {len(resolved)} entity(ies) "
        f"from {len(state['devices'])} device(s) and {len(state['entities'])} direct pick(s)"
    )
    return selection()


def resolve(entities_by_device) -> list:
    """Re-resolves the stored selection against a fresh registry snapshot.

    Run periodically so an entity added to an already-watched device starts
    being recorded without the user revisiting the picker. Returns the entity
    ids that are newly watched, which is what the caller backfills."""
    state = _load()
    before = set(state["resolved"])
    resolved = resolve_selection(state["devices"], state["entities"], entities_by_device)
    if set(resolved) == before:
        return []
    _apply(state["devices"], state["entities"], resolved)
    added = sorted(set(resolved) - before)
    if added:
        logger.info(f"WATCHLIST: {len(added)} entity(ies) newly watched after re-resolve")
    return added


def selection() -> dict:
    """The selection as the user expressed it, plus what it resolves to."""
    state = _load()
    return {
        "devices": list(state["devices"]),
        "entities": list(state["entities"]),
        "resolved": list(state["resolved"]),
        "updated_at": state["updated_at"],
    }


def watched_entities() -> list:
    """The concrete entity ids being watched."""
    return list(_load()["resolved"])


def is_watched(entity_id: str) -> bool:
    """True if this entity's transitions should be recorded. Called once per
    state change in the whole house, so it must stay a set lookup."""
    _load()
    return entity_id in _resolved_set
