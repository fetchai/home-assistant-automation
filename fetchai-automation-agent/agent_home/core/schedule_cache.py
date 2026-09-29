"""D-4's cache: so that a weekly grid whose classified shape has not moved
costs no model call.

The store, in `core/watchlist.py`'s own shape — stdlib `json`, one small file
in `settings.DATA_DIR`, a tmp-file-plus-`os.replace` write, and every failure
logged and absorbed rather than raised. It is keyed on `entity_id`, and on
three things per entity, which between them are the whole of what the question
was: `core/detectors/schedule.grid_fingerprint(grid)`, a fact about the grid
the detector already computes; `core/scheduler_llm.name_print(name)`, a digest
of the device name the prompt carries; and the caller-supplied prompt version
(`core/scheduler_llm.PROMPT_VERSION`), because a cached answer is only true of
the prompt that produced it — a revised prompt makes every stored answer an
answer to a different question until the version passed here moves too.

THE NAME IS IN THE KEY BECAUSE THE NAME MOVES THE ANSWER. D-4 named only the
grid, and that was wrong in a way only a live run could show:
`sensor.lounge_tv_power_draw` and `sensor.server_rack_power_draw` hold grids
identical character for character, and at temperature 0 the same prompt built
from "Lounge TV Power" comes back with all seven days while the one built from
"Server rack Power" comes back with Mon-Fri, reproducibly. Keyed on the grid
alone, a household renaming a device would go on getting the answer to its old
name for as long as its weekly shape held — silently, since the fingerprint
never moves for a rename. `core/scheduler_llm.name_print` carries the argument
and the measurement.

A CACHED REFUSAL IS A CACHED ANSWER. `lookup` returns three different things
and callers must tell them apart: `MISS` (no valid entry — ask the model),
`None` (this grid, at this prompt version, was asked and the model produced
nothing usable), or a `window.Window` (the model's accepted answer). Storing
the refusal is the point, not an edge case — D-2 consults the model on every
run-producing entity, so a refusal that was not cached would be re-asked,
identically, every single morning the grid stays the same shape, which is
exactly the cost D-4 exists to remove.

WHY NO EVICTION BY AGE. An entry whose fingerprint still matches is still a
true answer about a grid that has not moved; expiring it on a clock would
re-ask a question whose inputs did not change, which is the cost this whole
package removes. The only two ways an entry stops applying are the ones
already modelled — the fingerprint moves (`store` overwrites it with the new
answer) or the entity stops being watched (`prune` drops it). If steady-state
staleness is ever observed in practice, that is an argument for a bound
stated and justified here; it is not added speculatively now.

WHAT THIS MODULE DOES NOT KNOW. No suggestions, no replays, no proposals, no
gate. It does not import `core/review.py`, `core/suggestions.py`,
`core/gate.py`, `core/agent_runtime.py` or any handler — a grid fingerprint is
a fact about the RECORD, which the detector owns outright, and "what did I
propose yesterday" lives on a suggestion row this module must not read
(`core/review.py`'s own reason: that decision belongs to the pass). `prune`
takes the set of entities to keep as an argument rather than reaching into
`core/watchlist.py` itself, for the same reason — the caller already has that
set, and a store that fetches it itself is a store that knows more than a
store should.

Lands uncalled. Wiring this into `schedule.scan` is WP4, held behind D-6's
soak; nothing here is imported by anything that runs today.
"""
from datetime import datetime, timezone
import json
import logging
import os

from core import settings
from core import window

logger = logging.getLogger(__name__)

STORE_PATH = os.path.join(settings.DATA_DIR, "schedule_cache.json")

# A distinct sentinel from None, because None is itself a valid cached
# answer — "the model was asked and refused". A bare None return from
# `lookup` would be indistinguishable from a cache miss, and the whole point
# of caching a refusal is that it is NOT the same thing as not having asked.
MISS = object()

# Loaded once and written through on every change, exactly as
# core/watchlist.py's `_state` is: this process is the only writer.
_state = None


def _now() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def _empty_state():
    return {"version": 1, "entities": {}}


def _valid_window_field(raw):
    """Rehydrates a stored window through `window.spec`, which is where a
    window is validated — never built as a bare namedtuple, which would skip
    that. Returns the `Window`, or None for a stored `null` (a cached
    refusal). Raises on anything else, for `_load` to catch per-entity: a
    malformed field must drop the one entry it is on, not the whole file."""
    if raw is None:
        return None
    return window.spec(raw["start"], raw["end"], raw["days"])


def _valid_entry(raw):
    """One entity's cache entry, validated, or None if it cannot be trusted.

    Every failure here is a data problem, never a bug to raise past — the
    file may have been hand-edited, truncated, or written by an older
    version of this module, and none of that may reach `schedule.scan`'s
    daily pass. `spec` raises `ValueError`; a `window` that is not a mapping,
    or a `days` list with the wrong element types, raises `TypeError` on the
    subscript or the `int(...)` conversion; a field missing outright raises
    `KeyError` — the three a hostile-by-accident file actually produces from
    this code path.

    The entry kept in `_state` (and round-tripped straight back out by
    `_save`) holds `window` in its RAW, JSON-storable form — not the
    rehydrated `Window` — so that the in-memory shape is always exactly what
    `json.dump` can write. `lookup` is what turns it into a `Window`, on the
    one path a caller actually needs one; validating it here only proves
    that `_valid_window_field` will not raise later.
    """
    try:
        fingerprint = raw["fingerprint"]
        name_print = raw["name_print"]
        prompt_version = raw["prompt_version"]
        decided_at = raw["decided_at"]
        window_field = raw["window"]
        _valid_window_field(window_field)  # raises on anything spec() would refuse
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    # An entry written before the name joined the key has no `name_print` and
    # is dropped by the KeyError above rather than defaulted to anything. That
    # is the fail-closed direction and the only correct one: a default would
    # be a claim about which name the entry was decided against, and the whole
    # reason the field exists is that we cannot know.
    if not isinstance(name_print, str) or not name_print:
        return None
    return {
        "fingerprint": fingerprint,
        "name_print": name_print,
        "prompt_version": prompt_version,
        "decided_at": decided_at,
        "window": window_field,
    }


def _load():
    """Returns the cached state, reading it from disk on first use.

    A missing file yields an empty cache. An unreadable or structurally
    broken file also yields an empty cache, logged — the daily pass loses
    today's cache hits, not its ability to run. A file that parses but
    carries one bad entity entry keeps every other entry and drops only that
    one, logged by entity_id, which is the finer-grained failure this
    function exists to make possible: one hand-edited or stale row must not
    cost the whole home its cache."""
    global _state
    if _state is not None:
        return _state
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("entities"), dict):
            raise ValueError("unexpected structure")
        entities = {}
        for entity_id, raw in data["entities"].items():
            entry = _valid_entry(raw)
            if entry is None:
                logger.warning(f"SCHEDULE_CACHE: dropping unreadable entry for {entity_id}")
                continue
            entities[entity_id] = entry
        _state = {"version": 1, "entities": entities}
    except FileNotFoundError:
        _state = _empty_state()
    except Exception as e:
        logger.warning(f"SCHEDULE_CACHE: could not read {STORE_PATH} ({e}) — starting empty")
        _state = _empty_state()
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
        logger.error(f"SCHEDULE_CACHE: failed to persist {STORE_PATH}: {e}")


def _serialise_window(chosen):
    """A `Window` as the file stores it: "HH:MM" strings plus a sorted list
    of weekday integers, via `window.starts_at`/`ends_at` — the pair `spec`
    reads back — rather than the namedtuple's own fields, which is what
    keeps a round trip going through validation both ways. None for a cached
    refusal, stored as JSON `null`."""
    if chosen is None:
        return None
    return {
        "start": window.starts_at(chosen),
        "end": window.ends_at(chosen),
        "days": sorted(chosen.days),
    }


def lookup(entity_id: str, fingerprint: str, name_print: str, prompt_version):
    """The cached answer for this entity, this grid shape, this device name and
    this prompt — `MISS` if there is none.

    A stored entry only answers for the EXACT three it was decided against;
    any one of them moving is "this is a different question" and the caller
    must ask the model again. Answers `None` (the model was asked and produced
    nothing usable) exactly as it was stored, so a caller that got `None` back
    must not re-ask today — see the module docstring.
    """
    entry = _load()["entities"].get(entity_id)
    if entry is None:
        return MISS
    if (entry["fingerprint"] != fingerprint
            or entry["name_print"] != name_print
            or entry["prompt_version"] != prompt_version):
        return MISS
    return _valid_window_field(entry["window"])


def store(entity_id: str, fingerprint: str, name_print: str, prompt_version,
          chosen_window):
    """Records this entity's answer for this grid shape, this device name and
    this prompt. `chosen_window` is a `window.Window` or None (a refusal,
    cached exactly as deliberately as an accepted window — see the module
    docstring). Overwrites whatever was there before, whichever of the three
    moved: there is only ever one current answer per entity.
    """
    state = _load()
    state["entities"][entity_id] = {
        "fingerprint": fingerprint,
        "name_print": name_print,
        "prompt_version": prompt_version,
        "decided_at": _now(),
        "window": _serialise_window(chosen_window),
    }
    _save()


def prune(keep) -> list:
    """Drops every cached entry for an entity not in `keep`, and returns the
    entity_ids removed.

    `keep` is the set of entities to retain — the caller's, always, never
    `core/watchlist.py`'s: a store that reached for the gate itself would be
    a store that knows more than it should. Called with the same set
    `schedule.scan` is already iterating, so a device un-watched or removed
    does not leave an answer behind for an entity_id that could later be
    reused.
    """
    state = _load()
    keep_set = set(keep or [])
    dropped = sorted(set(state["entities"]) - keep_set)
    if not dropped:
        return []
    for entity_id in dropped:
        del state["entities"][entity_id]
    _save()
    logger.info(f"SCHEDULE_CACHE: pruned {len(dropped)} entity(ies) no longer watched")
    return dropped
