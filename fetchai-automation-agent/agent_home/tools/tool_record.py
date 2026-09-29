"""LLM tools over the add-on's own behavioural record.

The record is the agent's memory of the home (core/record.py), and until now
only the panel's Record tab could read it. These two tools let the agent read it
as well, which is what lets it answer "what have you noticed about my home" and
"when do I usually switch this on" instead of only "what is it doing right now".

Not in tools/tool_homeassistant/: neither tool touches a Home Assistant API.
They read SQLite, the watchlist and the listener's own status, and they read
only — a tool must never widen what is being recorded, because the watchlist is
the user's opt-in and the panel is where it is given.

The third tool here is not about the record at all — it collects whatever the
agent has been waiting to tell the user. It lives with these because it answers
the same kind of question ("what do you know that I don't?") and reads the same
family of local stores, and because ASI1 is pull-only: the agent cannot start a
conversation, so the only way a queued message reaches a chat client is the user
asking for it.

Nothing the record holds about an entity that is NOT watched now reaches the
model (2.21.4). Unwatching deletes nothing — only retention and the size cap
do — so the record keeps an entity's past after the household withdraws it,
and the panel's Record tab (observation.coverage over REST, which never leaves
the house) still lists it. These tools are the path to ASI-1, in chat and in
both analyst modes, so each filters on the watchlist at the call: the overview
lists and totals watched entities only, the profile shows nothing of an
unwatched one, and a pattern needs both of its entities watched. Same bound as
tools/tool_record_query.py's views.

The overview deliberately reports the health of the listener alongside the
counts. "The record is empty" has two very different causes — nothing has
happened, or nothing is being watched, or the socket never connected — and an
agent that cannot tell them apart tells the user the wrong one.
"""
from datetime import datetime
from typing import Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import delivery
from core import disclosure
from core import observation
from core import record
from core import watchlist

# As many entities as one answer carries. A home with hundreds of watched
# entities would otherwise spend the model's whole context on a table it was not
# asked about.
MAX_ENTITIES = 100

# Defaults and ceilings for the profile. 30 days is what the panel shows; the
# ceiling is a year because the record is kept for RECORD_RETENTION_DAYS and
# asking beyond it simply returns less.
DEFAULT_PROFILE_DAYS = 30
MAX_PROFILE_DAYS = 365
DEFAULT_RECENT = 30
MAX_RECENT = 200
DEFAULT_RELATIONS = 20
MAX_RELATIONS = 50

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _local_iso(ts) -> str:
    """A unix second as a local ISO string, or "" for nothing recorded.

    Same reasoning as the copy in tools/tool_homeassistant/history.py: the
    stores work in UTC epoch seconds and the model reads a bare integer as
    nonsense. Two three-line helpers beat giving core/text.py — which is the
    stdlib chat-text parsing module — a date formatter it has no other use for.
    """
    if ts is None:
        return ""
    return datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _clamp(value, default: int, low: int, high: int) -> int:
    """A model-supplied number, made safe. Anything unparseable falls back to
    the default rather than erroring: the question is still answerable."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


class tool_record_overview(BaseTool):
    """LLM tool reporting what the add-on's behavioural record holds."""
    name: str = "Get behavioural record overview"
    description: str = (
        "Report what the add-on itself has observed and stored about this home: which entities are "
        "being watched, how many transitions have been recorded for each, how far back the record "
        "reaches, and whether the listener is currently connected to Home Assistant. There is no "
        "input parameter. Use it when the user asks what you know about their home, what you have "
        "been recording, since when, or why the record is empty. This is the agent's own store — "
        "it only ever shows entities the user has selected in the panel's Watching tab."
    )

    def _run(self) -> str:
        """Composes the record's totals, its per-entity coverage and the health
        of the link that fills it."""
        return disclosure.emit(overview_payload())


def overview_payload() -> dict:
    """What the overview tool answers, before the boundary. Its own function
    because the analyst's explorer run hands the same answer to the model up
    front (2.19.1) — one builder, so the two can never disagree about what the
    record holds. The caller still emits it.

    Watched entities only (module docstring), and the totals are summed over
    those rows rather than taken from record.stats(), whose counts and oldest
    / newest include every entity the record still holds."""
    rows = [r for r in observation.coverage() if r["watched"]]
    total = len(rows)
    selection = watchlist.selection()
    status = observation.status()
    totals = record.stats()
    firsts = [r["first_seen"] for r in rows if r["first_seen"] is not None]
    lasts = [r["last_seen"] for r in rows if r["last_seen"] is not None]

    return {
        "source": "the add-on's behavioural record",
        "watching": {
            "entities": len(selection["resolved"]),
            "devices_selected": len(selection["devices"]),
            "entities_selected": len(selection["entities"]),
            # An empty watchlist is the normal state of a fresh install, not
            # a fault — but it is the answer to "why do you know nothing".
            "note": ("Nothing is being watched. The record stays empty until the user selects "
                     "entities in the panel's Watching tab."
                     if not selection["resolved"] else ""),
        },
        "record": {
            "events": sum(r["events"] or 0 for r in rows),
            "entities": sum(1 for r in rows if r["events"]),
            "backfilled_from_home_assistant": sum(r["backfilled"] or 0 for r in rows),
            "oldest": _local_iso(min(firsts) if firsts else None),
            "newest": _local_iso(max(lasts) if lasts else None),
            "retention_days": totals["retention_days"],
            # The configured window is not always the one in force: as the
            # record approaches its size cap it collapses older events into
            # profiles early (record.enforce_cap). Reporting the setting
            # alone would tell the user the record remembers more detail
            # than it still holds.
            "note": ("The record is near its size cap, so events older than the retention "
                     "window above are already collapsed into per-entity profiles — their "
                     "hour-of-day and day-of-week shape is kept, the individual "
                     "transitions are not." if totals["capped"] else ""),
        },
        "listener": {
            "running": status["running"],
            "connected": status["connected"],
            "recording_since": _local_iso(status["live_since"]),
            "last_event_at": _local_iso(status["last_event_at"]),
            "last_error": status["last_error"],
        },
        "entities": [
            {
                "entity_id": r["entity_id"],
                "name": r["name"],
                "watched": r["watched"],
                "events": r["events"],
                "last_state": r["last_state"],
                "first_seen": _local_iso(r["first_seen"]),
                "last_seen": _local_iso(r["last_seen"]),
            }
            for r in rows[:MAX_ENTITIES]
        ],
        "total_entities": total,
        "_truncated": total > MAX_ENTITIES,
    }


class _RecordProfileInput(BaseModel):
    """Input schema for one entity's behavioural profile."""
    entity_id: str = Field(
        description="The full entity_id, e.g. 'light.kitchen', 'binary_sensor.hall_motion'. "
                    "Use 'Get behavioural record overview' or 'List Home Assistant entities' "
                    "first if you do not know it exactly.",
        default="",
    )
    days: int = Field(
        description=f"How many days of the record to aggregate over. Defaults to "
                    f"{DEFAULT_PROFILE_DAYS}, maximum {MAX_PROFILE_DAYS}.",
        default=DEFAULT_PROFILE_DAYS,
    )
    limit: int = Field(
        description=f"How many of the most recent transitions to list. Defaults to "
                    f"{DEFAULT_RECENT}, maximum {MAX_RECENT}.",
        default=DEFAULT_RECENT,
    )


class tool_record_profile(BaseTool):
    """LLM tool returning one entity's habits as the record has them."""
    name: str = "Get entity behaviour profile"
    description: str = (
        "Read what the add-on's behavioural record holds about ONE entity: its most recent "
        "transitions, which hours of the day and days of the week it changes on, what fraction of "
        "the time it is on, how long it typically stays on, how many times it changed INTO each "
        "state and how many hours it was on over the printed window. Covers weeks or months, but only "
        "for entities the user selected in the panel's Watching tab, and only from when watching "
        "started. Use it for habits and patterns — 'when do I usually turn this on', 'how often is "
        "this used', 'is this normally on at night'. For the exact recent past, or for an entity "
        "that is not watched, use 'Get Home Assistant entity history' instead."
    )
    args_schema: Type[BaseModel] = _RecordProfileInput

    def _run(self, entity_id: str = "", days: int = DEFAULT_PROFILE_DAYS,
             limit: int = DEFAULT_RECENT) -> str:
        """Returns the entity's aggregates and recent transitions from the record."""
        entity_id = (entity_id or "").strip()
        if not entity_id:
            return disclosure.emit({"error": "entity_id is required"})
        days = _clamp(days, DEFAULT_PROFILE_DAYS, 1, MAX_PROFILE_DAYS)
        limit = _clamp(limit, DEFAULT_RECENT, 1, MAX_RECENT)

        watched = entity_id in set(watchlist.watched_entities())
        payload = {
            "entity_id": entity_id,
            "name": observation.name_for(entity_id),
            "source": "the add-on's behavioural record",
            "watched": watched,
        }

        # Which of the two silences this is decides what the user should be
        # told to do about it, so the tool says which rather than returning
        # a zeroed profile that reads like a measurement. Not "the record
        # holds nothing": it may hold the entity's past from before it was
        # unwatched, and that is exactly what is withheld (module docstring).
        if not watched:
            payload["note"] = (
                "This entity is not being watched, so it is not shown. The user can add it in the "
                "panel's Watching tab. For its recent past regardless, use 'Get Home Assistant "
                "entity history' where it is available."
            )
            return disclosure.emit(payload)

        first_seen = record.first_ts(entity_id)
        profile = record.profile(entity_id, days=days)
        payload["recorded_since"] = _local_iso(first_seen)
        payload["days"] = days

        if first_seen is None:
            payload["note"] = (
                "This entity is being watched but nothing has been recorded for it yet — it has "
                "not changed state since watching started. Use 'Get Home Assistant entity "
                "history' for its past where it is available."
            )
            return disclosure.emit(payload)

        payload["profile"] = {
            "events": profile["events"],
            "on_fraction": profile["on_fraction"],
            "mean_on_duration_seconds": profile["mean_on_duration"],
            "changes_by_hour": {f"{h:02d}": n for h, n in enumerate(profile["by_hour"]) if n},
            "changes_by_weekday": {_DAYS[d]: n for d, n in enumerate(profile["by_day"]) if n},
            # Counts and hours over the window printed beside them, live
            # events only. Added for the audited run's claims (§5A C-1), whose
            # first field runs quoted `events` as a switch-on count and the
            # mean on-duration as hours on; kept after F-9 retired that run
            # (2.23.0), because a figure the model quotes must come with the
            # window it covers and be the kind of number it reads as.
            "changes_to_state": profile["changes_to_state"],
            "on_hours": profile["on_hours"],
            "window_start": _local_iso(profile["window_start"]),
            "window_end": "now",
        }
        payload["fields"] = {
            "events": "every change into any state — not a count of switch-ons",
            "on_fraction": "a share of the time, not a count or hours",
            "mean_on_duration_seconds": "an average in seconds, not a total",
            "changes_to_state": "changes INTO each state from window_start to window_end, "
                                "live events only",
            "on_hours": "hours spent on from window_start to window_end, live events only; "
                        "null when the entity has no on/off state",
        }
        payload["recent"] = [
            {"at": _local_iso(r["ts"]), "state": r["state"],
             "previous_state": r["prev_state"], "previous_lasted_seconds": r["prev_duration"],
             "source": r["source"]}
            for r in record.recent(entity_id, limit=limit)
        ]
        if not profile["events"]:
            payload["note"] = (
                f"Nothing was recorded for this entity in the last {days} days, though the record "
                f"reaches back to {payload['recorded_since']}. Try a longer window."
            )
        elif profile.get("includes_collapsed_history"):
            # Old events are collapsed into a single aggregate with no day-by-day
            # resolution, so beyond retention this is no longer an exact N-day
            # slice — the tool has to say that rather than let the model imply
            # more precision than the record actually kept.
            payload["note"] = (
                f"This covers everything the record holds, not just the last {days} days — "
                "history older than the retention window is kept as a single running total, "
                "not day-by-day, so it cannot be sliced to an exact window."
            )
        return disclosure.emit(payload)


class _RecordRelationsInput(BaseModel):
    """Input schema for the observed-patterns query."""
    entity_id: str = Field(
        description="Optional. The full entity_id to filter to, e.g. 'binary_sensor.hall_motion' — "
                    "returns only patterns TRIGGERED by it (what tends to follow it). Leave empty for "
                    "the strongest patterns across the whole record.",
        default="",
    )
    limit: int = Field(
        description=f"How many patterns to return, strongest first. Defaults to "
                    f"{DEFAULT_RELATIONS}, maximum {MAX_RELATIONS}.",
        default=DEFAULT_RELATIONS,
    )


class tool_record_relations(BaseTool):
    """LLM tool reporting observed co-occurrences between watched entities."""
    name: str = "Get observed patterns"
    description: str = (
        "Report which watched entities tend to change shortly after another one does — e.g. the "
        "kitchen light usually turning on within two minutes of hall motion, 43 times. These patterns "
        "are noticed automatically from the behavioural record, not configured by the user. With no "
        "entity_id, returns the strongest patterns across the whole record; with one, only patterns "
        "TRIGGERED by that entity. A pattern needs to have recurred a handful of times to be reported "
        "at all — a one-off coincidence never appears. Use this for 'what usually happens when X', "
        "'does X trigger Y', or before proposing an automation based on the user's own habits."
    )
    args_schema: Type[BaseModel] = _RecordRelationsInput

    def _run(self, entity_id: str = "", limit: int = DEFAULT_RELATIONS) -> str:
        """Reads the relations table, resolves entity names, and explains an
        empty result before the model has to guess why."""
        entity_id = (entity_id or "").strip()
        limit = _clamp(limit, DEFAULT_RELATIONS, 1, MAX_RELATIONS)
        watched_set = set(watchlist.watched_entities())
        watched = (not entity_id) or entity_id in watched_set

        # Both ends watched, filtered before the limit is applied — so every
        # pair is read (LIMIT -1) and cut here. The table holds pairs of
        # entities, never events, so reading all of it is cheap.
        rows = [] if not watched else [
            r for r in record.relations(entity_id or None, limit=-1)
            if r["entity_a"] in watched_set and r["entity_b"] in watched_set
        ][:limit]
        payload = {
            "source": "the add-on's behavioural record",
            "window_seconds": record.RELATION_WINDOW_SECONDS,
            "minimum_occurrences_to_report": record.RELATION_MIN_SUPPORT,
            "patterns": [
                {
                    "trigger": r["entity_a"], "trigger_name": observation.name_for(r["entity_a"]),
                    "then": r["entity_b"], "then_name": observation.name_for(r["entity_b"]),
                    "count": r["count"],
                    "first_observed": _local_iso(r["since"]), "last_observed": _local_iso(r["until"]),
                }
                for r in rows
            ],
        }
        if entity_id and not watched:
            payload["note"] = (
                "This entity is not being watched, so patterns involving it are not shown. The user "
                "can add it in the panel's Watching tab."
            )
        elif not rows:
            payload["note"] = (
                "No recurring pattern has been observed yet"
                + (f" for {entity_id}" if entity_id else "")
                + f" — a pattern needs at least {record.RELATION_MIN_SUPPORT} occurrences within "
                  f"{record.RELATION_WINDOW_SECONDS} seconds of each other to be reported."
            )
        return disclosure.emit(payload)


class tool_pending_messages(BaseTool):
    """LLM tool handing over whatever the agent has queued for the user."""
    name: str = "Get pending messages"
    description: str = (
        "Collect anything the agent has been waiting to tell the user: approvals it is still "
        "holding, changes that were applied or failed, and warnings such as a watched device "
        "disappearing. There is no input parameter. Call it when the user asks what is new, what "
        "you have found, whether anything needs their attention, or at the start of a conversation "
        "they open with a general greeting. Each message is already written for a person — relay "
        "them as they are rather than summarising them away. Collecting a message hands it over, "
        "so anything returned here will NOT come back on the next call."
    )

    def _run(self) -> str:
        """Takes the pending messages and reports them in the home's local time."""
        messages = delivery.collect_for_chat()
        if not messages:
            return disclosure.emit({
                "messages": [],
                "note": "Nothing is waiting. Say so plainly — do not invent an update.",
            })
        return disclosure.emit({
            "count": len(messages),
            "messages": [
                {"at": _local_iso(m["created_at"]), "kind": m["kind"],
                 "urgency": m["urgency"], "title": m["title"], "body": m["body"]}
                for m in messages
            ],
            "note": "These have now been handed over and will not be returned again. "
                    "Pass them on to the user in full.",
        })
