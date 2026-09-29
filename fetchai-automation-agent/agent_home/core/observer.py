"""What counts as a meaningful state transition, and how to read one out of a
Home Assistant event.

Home Assistant emits a state_changed event for every attribute touch on every
entity in the house — a light reporting the same brightness, a sensor
reporting the same reading a second later. Storing all of that would be a
second copy of the recorder, which is the thing the behavioural record exists
not to be. The rules here are what turn that stream into "one packed row per
meaningful transition": attribute-only changes are dropped, flapping is
debounced, and numeric sensors are recorded as threshold crossings rather than
as every reading — a real step at the second it happened, everything smaller
rate-limited, because an integration reporting every second is otherwise
unbounded and the record has to fit on the host's disk.

Deliberately pure — no I/O, no module state, no Home Assistant client. The
observation thread cannot be exercised by the test suite; every decision it
makes can be, and lives here for that reason.
"""
from datetime import datetime, timezone
import logging
import os

logger = logging.getLogger(__name__)

# Two transitions of the same entity closer together than this are one event as
# far as the record is concerned. Contact and motion sensors bounce, and a pair
# of rows a few hundred milliseconds apart describes the hardware, not the home.
MIN_INTERVAL_SECONDS = 2

# Numeric sensors: record a reading only once it has moved by this fraction of
# the previous value, or by the absolute floor below — whichever is larger. The
# floor is what stops a value hovering near zero from crossing the relative
# threshold on noise alone.
NUMERIC_MIN_CHANGE_FRACTION = 0.05
NUMERIC_MIN_ABS_CHANGE = 0.5

# ...but record one anyway if this long has passed, so a slow drift (a freezer
# warming over six hours) still leaves a trail. Bounded at ~96 rows/day/sensor,
# which the retention window comfortably absorbs.
NUMERIC_MAX_GAP_SECONDS = 900

# A change this large is an event rather than noise — a kettle switching on, a
# meter dropping to standby — and is recorded at the second it happened however
# recently the last row was written. Keeping the timestamp matters as much as
# keeping the row: record.scan_relations pairs a change with what follows it
# inside two minutes, so a crossing shifted even a minute late stops being
# evidence of what it caused.
NUMERIC_STEP_FRACTION = 0.25

# The jitter floor. Everything under NUMERIC_STEP_FRACTION is the sensor
# describing itself rather than the home, and two such rows are never closer
# together than this.
#
# This is the single number that bounds what a fast-reporting entity costs on
# disk, and without it there is no bound at all: an integration pushing a
# reading every second crosses the 5% threshold below on noise alone, which is
# 86,400 rows a day rather than the ~96 the paragraph above budgets for. At 60
# seconds twenty such entities fit in roughly 156 MB over the retention window;
# at 30 it is about double that, at 120 about half.
NUMERIC_MIN_INTERVAL_SECONDS = int(os.environ.get("RECORD_NUMERIC_MIN_INTERVAL", "60"))

# States that are not measurements but availability. Kept — an entity going
# unavailable is a fact worth having when a suggestion later depends on it —
# but never treated as a numeric reading.
_NON_NUMERIC_STATES = {"unavailable", "unknown", "none", ""}


def as_float(state):
    """The state as a number, or None if it is not one."""
    if state is None:
        return None
    text = str(state).strip()
    if text.lower() in _NON_NUMERIC_STATES:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def median(values):
    """The middle value, averaging the two middle ones on an even count.

    Lives here rather than in standby.py, replay.py or detectors/levels.py
    because all three want it and this is the one module every one of them
    already imports — a shared stdlib-only leaf rather than a third (levels.py)
    or fourth copy of the same six lines.
    """
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def parse_timestamp(value):
    """Reads one of Home Assistant's ISO timestamps as a unix second.

    Returns None rather than guessing when the value is absent or malformed:
    the caller substitutes its own clock, and a wrong timestamp is worse than
    an approximate one — it lands the row in the wrong hour-of-day bucket."""
    if not value:
        return None
    try:
        text = str(value)
        # Python's fromisoformat handles "Z" from 3.11, but HA also emits
        # microsecond precision with an explicit offset; both parse cleanly.
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return None


def parse_state_changed(message):
    """Reads a state_changed websocket message as
    {entity_id, state, prev_state, ts}, or None if it is not one.

    Accepts either the full websocket envelope ({"type": "event", "event": ...})
    or the event itself, because the subscription delivers the former and the
    tests are clearer with the latter.
    """
    if not isinstance(message, dict):
        return None
    event = message.get("event") if "event" in message else message
    if not isinstance(event, dict) or event.get("event_type") != "state_changed":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None

    entity_id = data.get("entity_id")
    new_state = data.get("new_state")
    if not entity_id or not isinstance(new_state, dict):
        # new_state is null when an entity is REMOVED from Home Assistant.
        # There is no transition to record; the entity simply stops producing.
        return None
    old_state = data.get("old_state") if isinstance(data.get("old_state"), dict) else None

    ts = parse_timestamp(new_state.get("last_changed")) or parse_timestamp(event.get("time_fired"))
    return {
        "entity_id": entity_id,
        "state": new_state.get("state"),
        "prev_state": old_state.get("state") if old_state else None,
        "ts": ts,
    }


def should_record(state, ts, last) -> bool:
    """True if this transition belongs in the record.

    `last` is the previously recorded {state, ts} for the same entity, or None
    if it has never been recorded. Every rule below is a reason NOT to store a
    row; anything that survives them is a transition the agent will later have
    to reason about.
    """
    if state is None or str(state) == "":
        return False
    if not last:
        return True

    previous = last.get("state")
    elapsed = int(ts) - int(last.get("ts") or 0)

    # Time running backwards means the caller handed us history out of order,
    # or two sources disagree about the clock. Storing it would corrupt the
    # duration of the previous state, so leave it out.
    if elapsed < 0:
        return False

    new_value = as_float(state)
    old_value = as_float(previous)
    if new_value is not None and old_value is not None:
        if elapsed >= NUMERIC_MAX_GAP_SECONDS:
            return True
        change = abs(new_value - old_value)
        # A real step keeps its own timestamp and jumps the queue. The absolute
        # floor is carried into this test too, because 25% of a value sitting
        # at zero is zero, and every flicker would qualify.
        if change >= abs(old_value) * NUMERIC_STEP_FRACTION and change >= NUMERIC_MIN_ABS_CHANGE:
            return True
        # Anything smaller is jitter, and jitter waits.
        if elapsed < NUMERIC_MIN_INTERVAL_SECONDS:
            return False
        threshold = max(abs(old_value) * NUMERIC_MIN_CHANGE_FRACTION, NUMERIC_MIN_ABS_CHANGE)
        return change >= threshold

    # An attribute-only change — brightness, a refreshed timestamp — arrives as
    # a state_changed event with the state unchanged. That is not a transition.
    if str(state) == str(previous):
        return False
    return elapsed >= MIN_INTERVAL_SECONDS


def transition_row(entry, last):
    """Builds the (ts, state, prev_state, prev_duration) row the record stores.

    prev_duration is how long the state being replaced had lasted, which is
    only knowable against the previous row — hence it is computed here rather
    than at the call site, where the live loop and backfill would each have
    needed their own copy of it.
    """
    ts = int(entry["ts"])
    prev_state = entry.get("prev_state")
    prev_duration = None
    if last and last.get("ts") is not None:
        prev_duration = max(0, ts - int(last["ts"]))
        # The record's own previous row is a better source for what the state
        # actually was than the event's old_state, which may describe a change
        # that was itself filtered out.
        prev_state = last.get("state")
    return (ts, entry["state"], prev_state, prev_duration)
