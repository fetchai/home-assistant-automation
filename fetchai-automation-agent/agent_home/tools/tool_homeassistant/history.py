"""Reading Home Assistant's own recorder, so the behavioural record does not
have to start empty.

The agent's record is authoritative and the recorder is a bonus: where HA
happens to still hold history for an entity the agent has just been asked to
watch — or for the hours the add-on was switched off — that history warms the
record retrospectively (design doc §6.3). It is best-effort by nature. The
recorder's purge window is whatever the user configured, entities in its
exclude filters were never stored at all, and neither case is an error here.

The same recorder answers a second question, and this module holds both: the
agent is asked "how long was the door open yesterday" in chat, and the LLM tool
at the bottom of this file reads the recorder to answer it. Fetching is shared;
what differs is the destination — backfill writes what it finds into the
behavioural record, the tool writes nothing and only reports.
"""
from datetime import datetime, timezone
from typing import Type
from urllib.parse import quote
import logging

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import disclosure
from core import observer
from ._client import _ha_get

logger = logging.getLogger(__name__)

# How many entities one request asks about. The recorder answers per entity,
# so this trades round trips against response size.
BATCH_SIZE = 20

# ...and the widest window one request may cover. A day was picked back when
# every watched entity was assumed to change a few hundred times a day; it is
# the ceiling now rather than the rule, because the width actually used is
# derived from how often the entity really reports. See chunk_for().
CHUNK_SECONDS = 86400

# The real bound, and the one the two constants above quietly assumed all
# along: roughly how many history entries a single request may be asked to
# carry. Twenty ordinary entities over a day land just under it.
#
# An entity reporting every second holds 86,400 entries a day on its own, and
# the old fixed shape asked for twenty of those at once — some 120 MB, which
# Home Assistant cannot serialise inside any sane timeout. The request was
# retried on every reconnect, failed every time, and the entity was never
# observed at all. Sizing the request to the entity is what fixes that.
MAX_ENTRIES_PER_REQUEST = 15000

# ...but never narrower than this. Below an hour the request COUNT becomes the
# problem rather than the response size, and an entity that dense is one the
# statistics path should be covering instead.
MIN_CHUNK_SECONDS = 3600

# How long a density probe looks at. Long enough that an entity reporting every
# few seconds is unmistakable, short enough that twenty of them still answer in
# one small response.
PROBE_SECONDS = 900

# Recorder reads get their own timeout. The client default is sized for a state
# read; a database query behind even a well-sized window takes longer than that
# on a Pi with MariaDB.
TIMEOUT_SECONDS = 60


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def chunk_for(per_second: float) -> int:
    """How wide a window one request may cover, for an entity reporting at this
    rate. Seconds."""
    if not per_second or per_second <= 0:
        return CHUNK_SECONDS
    return int(max(MIN_CHUNK_SECONDS,
                   min(CHUNK_SECONDS, MAX_ENTRIES_PER_REQUEST / per_second)))


def is_batchable(per_second: float) -> bool:
    """True if this entity can share a request with nineteen others.

    Its fair share of a full batch is what decides it. One entity reporting a
    hundred times more often than the rest makes the whole batch's response too
    large to answer, and nineteen innocent entities lose their history because
    of it — so that one is fetched alone instead.
    """
    return (per_second or 0) * CHUNK_SECONDS <= MAX_ENTRIES_PER_REQUEST / BATCH_SIZE


def _chunks(entity_ids, since, until, batch_size, chunk_seconds, newest_first=False):
    """Yields (entity_batch, start, end) windows covering the whole request."""
    entity_ids = list(entity_ids)
    for i in range(0, len(entity_ids), batch_size):
        batch = entity_ids[i:i + batch_size]
        windows = []
        start = int(since)
        while start < int(until):
            end = min(start + int(chunk_seconds), int(until))
            windows.append((batch, start, end))
            start = end
        yield from (reversed(windows) if newest_first else windows)


def _path(batch, start, end) -> str:
    """The recorder request for one batch and window.

    minimal_response and no_attributes strip everything the record does not
    store; significant_changes_only drops the attribute-only churn that
    core.observer would filter out anyway, but drops it server-side where it
    costs nothing to transfer. It does NOT thin a numeric sensor: every new
    reading is a state change, which is the whole reason the sizing above has
    to exist.

    Percent-encoded, both timestamps. An ISO timestamp ends in "+00:00", and a
    raw "+" in a query string decodes to a SPACE — which Home Assistant rejects
    as "Invalid end_time". The one in the path survives unencoded, which is
    exactly why this was easy to miss.
    """
    return (
        f"/history/period/{quote(_iso(start))}"
        f"?filter_entity_id={quote(','.join(batch))}"
        f"&end_time={quote(_iso(end))}"
        f"&minimal_response&no_attributes&significant_changes_only"
    )


def _series_in(data):
    """Yields (entity_id, entries) for each series in one response."""
    for series in data:
        if not isinstance(series, list) or not series:
            continue
        # With minimal_response only the first and last entries carry the
        # entity_id, so it is read once and applied to the whole series.
        entity_id = None
        for entry in series:
            if isinstance(entry, dict) and entry.get("entity_id"):
                entity_id = entry["entity_id"]
                break
        if not entity_id:
            continue
        yield entity_id, [e for e in series if isinstance(e, dict)]


def stream_history(entity_ids, since: int, until: int, *, batch_size: int = BATCH_SIZE,
                   chunk_seconds: int = CHUNK_SECONDS, newest_first: bool = False,
                   max_entries: int = None):
    """Yields (entity_id, entries) one chunk at a time, in window order.

    A generator rather than a dict, because the dict is what made this
    unusable at the size it most needs to work: twenty entities over ten days
    at a reading a second is some seventeen million entries — several
    gigabytes of Python objects — and the add-on is killed long before one row
    reaches the record. The caller converts each chunk, stores it, and keeps
    nothing.

    Entities the recorder knows nothing about simply never appear. A failed
    request is logged and skipped rather than raised: a gap that cannot be
    filled leaves the record smaller, which is a bonus not arriving, not a
    failure to handle.

    `max_entries` stops the walk once that many have been yielded — a ceiling
    for callers that need a bounded answer rather than a complete one. Pair it
    with `newest_first` so what gets dropped is the far end.
    """
    if not entity_ids or int(until) <= int(since):
        return

    yielded = 0
    for batch, start, end in _chunks(entity_ids, since, until, batch_size,
                                     chunk_seconds, newest_first):
        ok, data = _ha_get(_path(batch, start, end), timeout=TIMEOUT_SECONDS)
        if not ok:
            logger.warning(f"BACKFILL: history request failed ({data}) — leaving that window empty")
            continue
        if not isinstance(data, list):
            logger.warning("BACKFILL: unexpected history response shape — leaving that window empty")
            continue

        for entity_id, entries in _series_in(data):
            yielded += len(entries)
            yield entity_id, entries

        if max_entries is not None and yielded >= max_entries:
            logger.info(
                f"BACKFILL: stopped reading history after {yielded} entries — the window "
                f"asked for is larger than one answer may carry"
            )
            return


def fetch_history(entity_ids, since: int, until: int, **kwargs) -> dict:
    """Returns {entity_id: [{state, ts}, ...]} for the window, oldest first.

    The whole answer in memory at once, which is right for a bounded window —
    the density probe below, the LLM tool at the bottom of this file — and is
    exactly what core.backfill must not do. That is what stream_history
    underneath is for.
    """
    out = {}
    for entity_id, entries in stream_history(entity_ids, since, until, **kwargs):
        out.setdefault(entity_id, []).extend(entries)
    return out


def probe_density(entity_ids, until: int) -> dict:
    """Returns {entity_id: entries per second}, measured over the last
    PROBE_SECONDS.

    Measured rather than guessed, and measured with exactly the request the
    real fetch makes — same filters, same server-side thinning — so the number
    means what that fetch will actually have to carry. A guess is wrong in both
    directions: a calm entity pushed onto a narrower path pays round trips it
    never needed, and a firehose left on the default path hangs the backfill
    for good.

    An entity the recorder answers nothing for is absent, not zero. "Quiet" and
    "unknown" are different claims, and the callers read an absence as ordinary
    — the optimistic reading, which the caller's own request budget bounds
    anyway.
    """
    until = int(until)
    series = fetch_history(entity_ids, until - PROBE_SECONDS, until)
    return {entity_id: len(entries) / PROBE_SECONDS
            for entity_id, entries in series.items() if entries}


# The window the tool will look back over, in hours. Home Assistant's recorder
# keeps ten days by default; asking for more is harmless — the answer is simply
# shorter — but there is no point asking for much more.
MAX_HISTORY_HOURS = 240

# How many transitions one answer carries. The observer rules below already
# collapse sensor churn, but a busy motion sensor over ten days still runs to
# thousands of rows, and every one of them costs the model context.
MAX_TRANSITIONS = 200

# ...and how much history is read to produce them. The cap above bounds what
# the MODEL sees; this one bounds what the add-on holds while deciding, which
# an entity reporting every second would otherwise blow through — 240 hours of
# it is 864,000 entries to keep 200. Read newest-first so the far end is what
# gets dropped, which is the end the cap above discards anyway.
MAX_ENTRIES_READ = 50000


def _local_iso(ts) -> str:
    """A unix second as a local ISO string.

    The record and the recorder both work in UTC epoch seconds; the model reads
    them as unlabelled integers and reports them back as nonsense. Home
    Assistant sets the container's timezone to the home's, so local is both
    readable and the clock the user means.
    """
    if ts is None:
        return ""
    return datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _entries(series) -> list:
    """One entity's recorder series as (ts, state) pairs, oldest first."""
    rows = []
    for entry in series or []:
        if not isinstance(entry, dict):
            continue
        ts = observer.parse_timestamp(entry.get("last_changed") or entry.get("last_updated"))
        state = entry.get("state")
        if ts is None or state is None:
            continue
        rows.append((ts, str(state)))
    rows.sort(key=lambda r: r[0])
    return rows


def summarise_series(series, since: int, until: int) -> dict:
    """What the window amounts to, in one object.

    Computed from the RAW series rather than from the filtered transitions on
    purpose: observer.should_record drops numeric readings that do not cross its
    threshold, which is right for the record and wrong for "what was the highest
    it got". The extremes have to come from every reading there was.

    Pure — no I/O, no clock — so it can be exercised without Home Assistant.
    """
    rows = _entries(series)
    if not rows:
        return {"readings": 0, "changes": 0}

    changes = sum(1 for i in range(1, len(rows)) if rows[i][1] != rows[i - 1][1])
    out = {"readings": len(rows), "changes": changes,
           "first": rows[0][1], "latest": rows[-1][1],
           "latest_at": _local_iso(rows[-1][0])}

    numbers = [n for n in (observer.as_float(state) for _, state in rows) if n is not None]
    if len(numbers) >= 2 and len(numbers) * 2 >= len(rows):
        out["numeric"] = {
            "min": min(numbers),
            "max": max(numbers),
            "mean_of_readings": round(sum(numbers) / len(numbers), 3),
            "readings": len(numbers),
        }
        return out

    # Categorical: how long each state held. The recorder's first entry is the
    # state as it stood when the window opened, and its last_changed is normally
    # older than the window, so the first span is clamped to `since` — otherwise
    # "on for 3 hours" silently includes time before the period asked about.
    seconds = {}
    for i, (ts, state) in enumerate(rows):
        start = max(int(ts), int(since))
        end = int(rows[i + 1][0]) if i + 1 < len(rows) else int(until)
        if end > start:
            seconds[state] = seconds.get(state, 0) + (end - start)
    out["seconds_per_state"] = {k: seconds[k] for k in sorted(seconds, key=seconds.get, reverse=True)}
    return out


class _HAHistoryInput(BaseModel):
    """Input schema for reading one entity's recorder history."""
    entity_id: str = Field(
        description="The full entity_id to look up, e.g. 'binary_sensor.front_door', "
                    "'sensor.kitchen_temperature'. Use 'List Home Assistant entities' first "
                    "if you do not know it exactly.",
        default="",
    )
    hours: int = Field(
        description=f"How far back to look, in hours. Defaults to 24, maximum {MAX_HISTORY_HOURS} "
                    "(ten days). Home Assistant's recorder usually keeps about ten days, so a "
                    "longer window simply returns whatever is left.",
        default=24,
    )


class tool_ha_entity_history(BaseTool):
    """LLM tool returning one entity's past states from Home Assistant's recorder."""
    name: str = "Get Home Assistant entity history"
    description: str = (
        "Read what a Home Assistant entity DID over a past window, from Home Assistant's own "
        "recorder. Returns the state changes with their local timestamps, how long the previous "
        "state lasted, and a summary: how many changes there were, how long each state was held, "
        "and for numeric sensors the minimum, maximum and mean reading. Use it for questions about "
        "the recent past — 'when did the door last open', 'how long was the light on yesterday', "
        "'how many times did the boiler fire today', 'what was the highest temperature'. Covers "
        "ANY entity, not only the ones the add-on watches, but only as far back as the recorder "
        "still holds (about ten days by default). For habits over weeks and months use "
        "'Get entity behaviour profile' instead."
    )
    args_schema: Type[BaseModel] = _HAHistoryInput

    def _run(self, entity_id: str = "", hours: int = 24) -> str:
        """Fetches the window from the recorder and reports it. Reads only —
        nothing here is written to the behavioural record."""
        # Imported here, not at module scope: core.backfill imports THIS module
        # at import time, so a top-level import would be a cycle. The conversion
        # is shared deliberately — a stretch of history the tool describes and a
        # stretch the record stores must be the same stretch.
        from core.backfill import history_to_transitions

        entity_id = (entity_id or "").strip()
        if not entity_id:
            return disclosure.emit({"error": "entity_id is required"})
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            hours = 24
        hours = max(1, min(hours, MAX_HISTORY_HOURS))

        until = int(datetime.now(tz=timezone.utc).timestamp())
        since = until - hours * 3600
        series = fetch_history([entity_id], since, until, newest_first=True,
                               max_entries=MAX_ENTRIES_READ).get(entity_id)

        # What was actually read, which for a fast-reporting entity is less
        # than what was asked for. Reported as the window rather than quietly
        # summarised as if it covered the whole request: "on for 3 hours" over
        # a window that silently stopped early is a wrong answer, not a partial
        # one.
        covered = since
        if series and len(series) >= MAX_ENTRIES_READ:
            covered = min((observer.parse_timestamp(
                e.get("last_changed") or e.get("last_updated")) or until) for e in series)

        window = {
            "hours": round((until - covered) / 3600, 2),
            "from": _local_iso(covered),
            "to": _local_iso(until),
            "timezone": str(datetime.now().astimezone().tzinfo),
        }
        if covered > since:
            window["asked_for_hours"] = hours
            window["note"] = ("This entity reports too often for the whole window to be read; "
                             "the answer covers the most recent part of it.")
        if not series:
            # Said out loud rather than returned as an empty list: an absence the
            # model is left to interpret is an absence it fills in.
            return disclosure.emit({
                "entity_id": entity_id,
                "source": "Home Assistant recorder",
                "window": window,
                "transitions": [],
                "note": "Home Assistant's recorder holds no history for this entity in this "
                        "window. It may have been purged (the recorder keeps about ten days by "
                        "default), the entity may be excluded from the recorder, or it may not "
                        "have existed yet. Do not guess what it was doing.",
            })

        rows = history_to_transitions(series)
        total = len(rows)
        truncated = total > MAX_TRANSITIONS
        # The newest end is the one questions are usually about, so that is the
        # end that survives the cap.
        rows = rows[-MAX_TRANSITIONS:]

        return disclosure.emit({
            "entity_id": entity_id,
            "source": "Home Assistant recorder",
            "window": window,
            "summary": summarise_series(series, covered, until),
            "transitions": [
                {"at": _local_iso(ts), "state": state,
                 "previous_state": prev_state, "previous_lasted_seconds": prev_duration}
                for ts, state, prev_state, prev_duration in rows
            ],
            "total": total,
            "_truncated": truncated,
        })
