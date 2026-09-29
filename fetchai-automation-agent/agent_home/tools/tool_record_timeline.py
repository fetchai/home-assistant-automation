"""The free-hand analyst's timeline tool: entities' history as RUNS, not rows.

Proposal F-2 (docs/PROPOSAL-FREE-HAND-ANALYST.md, F-WP2). `Query the
behavioural record` (tool_record_query.py, F-1) answers any question that can
be written as SQL, but "show me the week" is cheaper to both ask and read as
compact stretches — "on 06:52-10:41" — than as however many rows crossed that
window. Built on `record.between()` and `core/timeline.py`'s `runs()`, the one
place a series of rows becomes runs, so what this tool tells the model and what
F-WP4's charts later draw for the user are never two implementations of the
same merge.

Analyst-only, like tool_record_query.py: not added to `agent_runtime.tools` (chat
does not get it) and not added to `analyst.ANALYST_READ_TOOLS` (F-WP3's own
wiring) — F-WP1's rule, restated here rather than assumed.
"""
from datetime import datetime
from typing import List, Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import disclosure
from core import observation
from core import record
from core import timeline
from core import watchlist

# Entities one call may cover. Five is already several charts' worth of rows
# to hold in one answer; a caller wanting more is asking a different question.
MAX_ENTITIES = 5

# Runs one entity may report. Past this the answer is a log, not a look —
# 300 runs is many hundreds of toggles, well past what a window this tool is
# for ("show me the week") should ever produce for a light or a switch.
TIMELINE_MAX_RUNS_PER_ENTITY = 300

DEFAULT_DAYS = 7
MAX_DAYS = 21


def _local_iso(ts) -> str:
    """A unix second as a local ISO string. Same three lines as
    tools/tool_record.py's copy (its own docstring explains why this is
    duplicated rather than shared): tool_record's `_local_iso` already fits a
    run's `from`/`to` exactly as it is, so core/timeline.py stays free of a
    formatting helper it would otherwise need only for this."""
    if ts is None:
        return ""
    return datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _parse_local(value: str):
    """A local 'YYYY-MM-DD HH:MM[:SS]' string as an epoch second, or None if
    it does not parse. Seconds are optional because a person asking for a
    window types "2026-09-20 07:00", not "...:00"."""
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(value, fmt).timestamp())
        except (ValueError, OverflowError):
            continue
    return None


def _resolve_window(start: str, end: str, days: int):
    """(start_ts, end_ts, error). `error` is set, and the other two None, when
    the request cannot be turned into a window at all."""
    now = int(datetime.now().timestamp())
    start = (start or "").strip()
    end = (end or "").strip()
    if not start and not end:
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = DEFAULT_DAYS
        days = max(1, min(days, MAX_DAYS))
        return now - days * 86400, now, None
    if not start or not end:
        return None, None, ("start and end must both be given, or both left empty to use "
                            "'days' back from now")
    end_ts = now if end.lower() == "now" else _parse_local(end)
    start_ts = _parse_local(start)
    if start_ts is None or end_ts is None:
        return None, None, ("start and end must be local 'YYYY-MM-DD HH:MM[:SS]', or "
                            "end: \"now\"")
    if start_ts >= end_ts:
        return None, None, "start must be before end"
    return start_ts, end_ts, None


def _entity_timeline(entity_id: str, start_ts: int, end_ts: int, watched_set: frozenset) -> dict:
    """One entity's runs over [start_ts, end_ts), or the note explaining why
    there are none — the same two silences tool_record.py's profile tells
    apart: not watched, versus watched but nothing to show."""
    watched = entity_id in watched_set
    payload = {
        "entity_id": entity_id,
        "name": observation.name_for(entity_id),
        "watched": watched,
        "runs": [],
        "run_count": 0,
        "truncated": False,
        "note": "",
    }
    if not watched:
        payload["note"] = (
            "This entity is not being watched, so it is not shown (the record may still hold "
            "its past from before it was unwatched). The user can add it in the panel's "
            "Watching tab."
        )
        return payload

    first_seen = record.first_ts(entity_id)
    if first_seen is None:
        payload["note"] = (
            "This entity is being watched but nothing has been recorded for it yet — it has not "
            "changed state since watching started."
        )
        return payload

    rows = record.between(entity_id, start_ts, end_ts)
    state_before = record.state_at(entity_id, start_ts)
    sample = rows if rows else ([{"state": state_before}] if state_before is not None else [])
    if timeline.is_numeric(sample):
        payload["note"] = (
            "This entity's states are numbers, so runs would mean one per reading rather than a "
            "meaningful stretch. Use 'Query the behavioural record' to bucket or average it."
        )
        return payload

    computed = timeline.runs(rows, start_ts, end_ts, state_before=state_before)
    if not computed:
        payload["note"] = (
            f"Nothing was recorded for this entity in the requested window, though the record "
            f"reaches back to {_local_iso(first_seen)}. Try a different window."
        )
        return payload

    truncated = len(computed) > TIMELINE_MAX_RUNS_PER_ENTITY
    computed = computed[:TIMELINE_MAX_RUNS_PER_ENTITY]
    payload["runs"] = [
        {"state": state, "from": _local_iso(frm), "to": _local_iso(to),
         "lasted_seconds": to - frm}
        for frm, to, state in computed
    ]
    payload["run_count"] = len(payload["runs"])
    payload["truncated"] = truncated
    return payload


class _TimelineInput(BaseModel):
    """Input schema for the timeline tool."""
    entity_ids: List[str] = Field(
        description=f"1-{MAX_ENTITIES} full entity_ids, e.g. ['light.kitchen', "
                    f"'binary_sensor.hall_motion'].",
        default_factory=list,
    )
    start: str = Field(
        description="Local time 'YYYY-MM-DD HH:MM[:SS]' the window starts at. Leave empty, "
                    "together with end, to use 'days' back from now instead.",
        default="",
    )
    end: str = Field(
        description="Local time 'YYYY-MM-DD HH:MM[:SS]' the window ends at, or \"now\". Leave "
                    "empty, together with start, to use 'days' back from now instead.",
        default="",
    )
    days: int = Field(
        description=f"Used only when start/end are both empty: how many days back from now. "
                    f"Defaults to {DEFAULT_DAYS}, maximum {MAX_DAYS}.",
        default=DEFAULT_DAYS,
    )


class tool_record_timeline(BaseTool):
    """LLM tool reporting one or more entities' history as runs, not rows."""
    name: str = "Get entity timeline"
    description: str = (
        "Report one or more watched entities' history as RUNS: stretches of time spent in one "
        "state — 'on 06:52-10:41', not four separate rows of raw transitions. Cheaper to read than "
        "'Query the behavioural record' for 'show me the week', and the same shape a chart draws. "
        f"Up to {MAX_ENTITIES} entity_ids. Give start and end as local 'YYYY-MM-DD HH:MM[:SS]' "
        "(end may be \"now\"), or leave both empty and set days instead "
        f"(default {DEFAULT_DAYS}, maximum {MAX_DAYS}) for the last N days up to now. At most "
        f"{TIMELINE_MAX_RUNS_PER_ENTITY} runs per entity — `truncated` true means more happened in "
        "the window than are shown, so narrow it. A numeric entity (a temperature, a power draw in "
        "watts) is reported with no runs and a note to use 'Query the behavioural record' instead, "
        "because its readings rarely repeat exactly and 'runs' would mean nothing. An entity that "
        "is not watched, or watched but never recorded, is reported with no runs and a note saying "
        "which.\n"
        "Example: entity_ids=['light.kitchen', 'binary_sensor.hall_motion'], days=3 returns each "
        "entity's on/off stretches over the last three days."
    )
    args_schema: Type[BaseModel] = _TimelineInput

    def _run(self, entity_ids: list = None, start: str = "", end: str = "",
             days: int = DEFAULT_DAYS) -> str:
        """Returns each entity's runs over the resolved window, or an error."""
        entity_ids = [e.strip() for e in (entity_ids or []) if e and e.strip()]
        if not entity_ids:
            return disclosure.emit({"error": "entity_ids is required (1-{} entities)"
                                    .format(MAX_ENTITIES)})
        entity_ids = entity_ids[:MAX_ENTITIES]

        start_ts, end_ts, error = _resolve_window(start, end, days)
        if error:
            return disclosure.emit({"error": error})

        watched_set = frozenset(watchlist.watched_entities())
        return disclosure.emit({
            "source": "the add-on's behavioural record",
            "start": _local_iso(start_ts),
            "end": _local_iso(end_ts),
            "entities": [_entity_timeline(e, start_ts, end_ts, watched_set) for e in entity_ids],
        })
