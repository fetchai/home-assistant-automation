"""What a chart IS: the evidence an explorer idea is judged by, drawn by code
from the record at the moment the idea is stored.

Free-hand proposal F-7 and F-8 (docs/PROPOSAL-FREE-HAND-ANALYST.md, F-WP4).
The model REQUESTS a chart — a type, entities, a window — and this module
draws it. The row stores the drawn SERIES, not the request alone: §9's rule
that evidence travels with the suggestion and is never recomputed later "from
a record that has moved on". A person reads the chart, not the model's
sentence, so the chart has to be the record's own account, drawn by the one
builder everything else uses: rows come from `record.between` (live events,
the same rows the detectors and the timeline tool read) and become runs
through `core/timeline.runs`, never a second run-builder here.

PURE WHERE IT CAN BE
--------------------
`request`, `clamp`, `series` and `markers` take literals and return literals;
`draw` is the one I/O entry (the watchlist, `record.first_ts`,
`record.between`, `record.state_at`) and `redraw` the one store-side one. So
every rule below is tested against a list literal, like core/window.py.

WINDOWS ARE CLAMPED, NOT REFUSED (field run 2)
----------------------------------------------
F-WP3 refused a window longer than the type allows; field run 2's one idea
asked for an overlay of 7.6 days against a cap of 7 and was stored with no
chart at all — the one idea whose evidence most needed looking at. So a
window longer than the type allows is drawn as the most recent span it
allows, one shorter as the shortest span ending where it asked, one ending in
the future ends now, and one starting before the record reaches is started
where the record begins. Each is written into `window_clamped: {asked, drawn,
why}` so the card can say so. Only a spec that cannot be drawn AT ALL — an
unknown type, a malformed entity list, an unwatched or never-recorded entity,
a window with no rows in it — sets `error`, and even then the idea is stored
(F-7: the absence of evidence is itself evidence).

"unknown" AND "unavailable" ARE GAPS, NOT STATES (field run 2)
--------------------------------------------------------------
Field run 2's finding — the kitchen light "on for 466 hours with no motion" —
was an artefact: the scenario light's simulator was not running, the light
sat `on` until a Home Assistant restart turned it `unknown`/`unavailable`, and
the SQL counted everything up to the next row as on. A chart would have shown
flat multi-day bands, which is what F-7 is for — but only if those bands are
not drawn as "on". So in a SERIES both states are one class, `None`, "no data":
drawn as a gap, never merged into the state before them, and neither the
heatmap nor the daily count counts a change into or out of them (a light that
"comes on" when Home Assistant restores it after a restart is not someone
switching it on). The same class covers any stretch before the entity's first
live row. MARKERS are different: they read the raw states, because Home
Assistant's own triggers and conditions see `unavailable` as a state like any
other (and an entity service call skips an unavailable target).

TIME IS LOCAL MINUTES PER LOCAL DAY
-----------------------------------
A timeline or overlay day is `{date, minutes, span, entities}` where
`minutes` is how long that local day actually is — 1380 on the day the clocks
go forward, 1500 on the day they go back — and every segment and marker is
`[start, end]` in minutes ELAPSED since that day's local midnight
(`window.day_start`). Elapsed, not wall-clock: on a 25-hour day the repeated
hour would otherwise draw twice over the same pixels. The epochs are only
ever subtracted, never round-tripped through a wall clock (core/timeline.py's
rule), so a DST day is simply a shorter or longer row.

MARKERS: WHAT THE RULE WOULD HAVE DONE, FOR A SUPPORTED SUBSET ONLY (F-8)
--------------------------------------------------------------------------
Over a timeline or overlay window, `markers` evaluates the proposed config
against the record and places a marker wherever it would have acted on a
charted entity. It follows Home Assistant's own semantics for the shapes it
reads, and ONLY those shapes:

- triggers: `state` (entity_id str or list; `to` / `from`, str or list; `for`
  as seconds, "HH:MM[:SS]" or a {days, hours, minutes, seconds, milliseconds}
  dict — fires at transition + for, only if the new state HELD for the whole
  duration, exactly `async_track_same_state` with `to` given); `time` (`at`
  "HH:MM[:SS]", str or list, every local day; a time the clock skips does not
  fire that day and an ambiguous one fires once, in the first fold, as
  `dt_util.find_next_time_expression_time` does). Both `platform:` and HA's
  newer `trigger:` key are read.
- conditions: `state` (every entity AT fire time, from the runs, in one of
  the states), `time` (`after` / `before`, crossing midnight as HA's
  `condition.time` does it, `weekday`), and `and` of those; the top-level list
  is an `and`.
- actions: `service` / `action` `light|switch|fan|input_boolean.turn_on|
  turn_off|toggle` with `target.entity_id` (str or list) and optional
  non-template `data`. A toggle resolves from the target's state at that
  instant (`on` → turn_off, anything else → turn_on: ToggleEntity.async_toggle).
  A target that is `unavailable` then gets no marker: HA's entity service call
  skips it.
- `mode: single` (or no mode). With no delay in the subset, a run is
  instantaneous and cannot overlap another; the only overlap is two triggers
  firing in the same second, drawn once — a second start of a running
  `single` automation is dropped. Other modes are refused rather than assumed
  equivalent, because they come with `max` / `max_exceeded` and describe
  behaviour this does not model.

ANYTHING ELSE, ANYWHERE — a template, `sun`, `numeric_state`, `from` with
`for` and no `to`, a `delay`, `choose`, another domain, a device trigger, an
area target, an unknown key — draws NO markers, and `markers_note` says "the
agent can't draw what this rule would have done" naming the first part it
does not read. Never a partial drawing of a rule it half understood: a person
trusts a marker, and a wrong one is worse than none. Extending the subset
means a test against Home Assistant's own semantics for that shape first
(docs/CONVENTIONS.md).

The markers read the record AS IT HAPPENED. They do not simulate the rule's
own effect: a light the rule would have turned off is still `on` in the
record, so a later fire the rule itself would have prevented is still drawn.
That is the honest reading of "what would it have done, given what actually
happened", and the proposal's open question (§6) — check the first field run
against the automation's real traces — is where it gets tested.
"""
from datetime import timedelta
import json
import logging
import time

from core import record
from core import suggestions
from core import timeline
from core import watchlist
from core import window

logger = logging.getLogger(__name__)

# F-7's four chart types (proposal §4.2): (min entities, max entities, whether
# a state is required, min days, max days). Moved here from core/analyst.py
# with F-WP4; settings.analyst_explorer_message states them and
# tests/test_analyst_explorer.py pins the two against each other.
CHART_RULES = {
    "timeline": (1, 3, False, 3, 21),
    "heatmap": (1, 1, True, 14, 90),
    "overlay": (2, 2, False, 1, 7),
    "daily_count": (1, 1, True, 7, 90),
}

# A window "of 7 days" that crosses a DST change is 7 days ± an hour of unix
# seconds, and the model counts days on a wall clock — not a clamp.
CHART_SLACK_SECONDS = 3600

# The two chart types that carry "what the rule would have done" (§4.2).
MARKER_TYPES = frozenset({"timeline", "overlay"})

# Home Assistant's "no data" states. See the module docstring: field run 2.
GAP_STATES = frozenset({"unknown", "unavailable"})

# Size caps, like the tools'. A timeline of three chatty entities over 21 days
# can reach tens of thousands of segments; the row is read by the panel on
# every open. Oldest days go first, and the series says `truncated`.
SERIES_MAX_SEGMENTS = 3000      # per entity, over the whole window
CHART_MAX_BYTES = 96 * 1024     # the series as JSON
MARKERS_MAX = 1000              # most recent kept

CANNOT_DRAW_RULE = "the agent can't draw what this rule would have done"

_SPEC_KEYS = ("type", "entities", "state", "start", "end")

# runs() treats a None state_before as "unknown", so a gap cannot be None on
# the way IN; it is this marker, turned into None on the way out.
_GAP_MARK = "\x00no data"


# --------------------------------------------------------------------------
# the request: shape, then the window
# --------------------------------------------------------------------------
def _epoch(value, now):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value        # a spec already normalised (a redraw)
    return window.local_epoch(value, now)


def request(spec, now):
    """The model's chart request → (normalised spec, None) or (known keys, why).

    Shape only — nothing here reads the record. Normalised is `type`,
    `entities`, `state` (or None), `start` / `end` as epochs with "now"
    frozen to `now`. Refused keeps the request's five known keys as sent."""
    known = ({k: spec.get(k) for k in _SPEC_KEYS} if isinstance(spec, dict) else {})
    kind = known.get("type")
    if kind not in CHART_RULES:
        return known, f"{kind!r} is not a chart type"
    min_e, max_e, needs_state, _, _ = CHART_RULES[kind]
    entities = known.get("entities")
    if (not isinstance(entities, list)
            or not all(isinstance(e, str) and e.strip() for e in entities)):
        return known, "entities is not a list of entity ids"
    entities = [e.strip() for e in entities]
    if len(set(entities)) != len(entities):
        return known, "an entity is named twice"
    if not min_e <= len(entities) <= max_e:
        return known, f"{_a(kind)} draws {min_e}-{max_e} entities, not {len(entities)}"
    state = known.get("state")
    state = state.strip() if isinstance(state, str) and state.strip() else None
    if needs_state and state is None:
        return known, f"{_a(kind)} needs the state to count"
    start, end = _epoch(known.get("start"), now), _epoch(known.get("end"), now)
    if start is None or end is None:
        return known, "start and end must be local 'YYYY-MM-DD HH:MM:SS' times, end may be 'now'"
    if start >= end:
        return known, "the window ends before it starts"
    return {"type": kind, "entities": entities, "state": state,
            "start": int(start), "end": int(end)}, None


def clamp(spec, now, first_ts):
    """A normalised spec's window → (drawn spec, window_clamped | None, why).

    In order: an end in the future (past the slack) ends at `now`; a span
    longer than the type allows keeps its most recent allowed span, one
    shorter is widened back to the shortest allowed span ending where it
    asked; a start before `first_ts` (the EARLIEST of the charted entities'
    `record.first_ts` — a later-recorded one shows "no data" before its
    record begins) starts there. Only a window left empty is refused."""
    _, _, _, min_days, max_days = CHART_RULES[spec["type"]]
    start, end = spec["start"], spec["end"]
    why = []
    if end > int(now) + CHART_SLACK_SECONDS:
        end = int(now)
        why.append("the window ended in the future")
    span = end - start
    if span > max_days * 86400 + CHART_SLACK_SECONDS:
        start = end - max_days * 86400
        why.append(f"{_a(spec['type'])} covers at most {max_days} days")
    elif span < min_days * 86400 - CHART_SLACK_SECONDS:
        start = end - min_days * 86400
        why.append(f"{_a(spec['type'])} covers at least {min_days} days")
    if start < int(first_ts):
        start = int(first_ts)
        why.append("the record does not reach back further")
    if start >= end:
        return None, None, "the record holds nothing in that window"
    drawn = {**spec, "start": start, "end": end}
    if not why:
        return drawn, None, None
    return drawn, {"asked": {"start": spec["start"], "end": spec["end"]},
                   "drawn": {"start": start, "end": end}, "why": why}, None


# --------------------------------------------------------------------------
# series (pure)
# --------------------------------------------------------------------------
def gapped_runs(rows, start, end, state_before=None) -> list:
    """One entity's runs over [start, end) with the gap states as None.

    Through `timeline.runs` — the gap states are mapped on the way in, so an
    `unknown` followed by `unavailable` merges into one gap by the same rule
    that merges any two same-state rows, and never into the state before it.
    A stretch before the first row (no `state_before`) is a gap too, so the
    runs always tile the whole window."""
    def mark(state):
        return _GAP_MARK if state in GAP_STATES else state

    marked = [{**row, "state": mark(row["state"])} for row in rows]
    before = mark(state_before) if state_before is not None else None
    out = [(a, b, None if s == _GAP_MARK else s)
           for a, b, s in timeline.runs(marked, start, end, before)]
    if not out:
        return [(int(start), int(end), None)]
    if out[0][0] > int(start):
        out.insert(0, (int(start), out[0][0], None))
    return out


def _minute(seconds):
    """Elapsed seconds → minutes, to a tenth: a 30-second motion blip is a
    visible 0.5, not a zero-width 0."""
    m = round(seconds / 60, 1)
    return int(m) if m == int(m) else m


def _days(start, end) -> list:
    """Every local date [start, end) touches, as (date, day_start, day_end,
    clip_start, clip_end). A day the zone skipped (length 0) is left out."""
    out = []
    day = window._local_date(start)
    last = window._local_date(end - 1)
    while day <= last:
        lo, hi = window.day_start(day), window.day_start(day + timedelta(days=1))
        if hi > lo:
            out.append((day, lo, hi, max(lo, start), min(hi, end)))
        day += timedelta(days=1)
    return out


def _changes_into(runs, state) -> list:
    """The instants a run of `state` began right after a run of another
    REAL state. The first run is not a change (it opened with the window),
    and a return from a gap is not one either — see the module docstring."""
    return [cur[0] for prev, cur in zip(runs, runs[1:])
            if cur[2] == state and prev[2] is not None and prev[2] != state]


def _size(value) -> int:
    return len(json.dumps(value, separators=(",", ":")))


def _day_series(runs_by_entity, start, end) -> dict:
    days = []
    for day, lo, hi, a, b in _days(start, end):
        entities = {}
        for entity, runs in runs_by_entity.items():
            entities[entity] = [[_minute(max(f, a) - lo), _minute(min(t, b) - lo), s]
                                for f, t, s in runs if f < b and t > a]
        days.append({"date": day.isoformat(), "minutes": _minute(hi - lo),
                     "span": [_minute(a - lo), _minute(b - lo)], "entities": entities})
    out = {"days": days, "truncated": False, "days_dropped": 0}

    def over():
        counts = {}
        for d in days:
            for entity, segments in d["entities"].items():
                counts[entity] = counts.get(entity, 0) + len(segments)
        return (any(n > SERIES_MAX_SEGMENTS for n in counts.values())
                or _size(out) > CHART_MAX_BYTES)

    while len(days) > 1 and over():
        days.pop(0)
        out["truncated"] = True
        out["days_dropped"] += 1
    return out


def _heatmap(runs, start, end, state) -> dict:
    counts = [[0] * 24 for _ in range(7)]
    for ts in _changes_into(runs, state):
        local = time.localtime(ts)
        counts[local.tm_wday][local.tm_hour] += 1
    per_weekday = [0] * 7
    for day, *_ in _days(start, end):
        per_weekday[day.weekday()] += 1
    return {"state": state, "counts": counts, "days_per_weekday": per_weekday,
            "changes": sum(map(sum, counts)), "truncated": False}


def _daily_count(runs, start, end, state) -> dict:
    changes = _changes_into(runs, state)
    days = []
    for day, lo, hi, a, b in _days(start, end):
        gap = sum(min(t, b) - max(f, a) for f, t, s in runs if s is None and f < b and t > a)
        days.append({"date": day.isoformat(),
                     "count": sum(1 for ts in changes if a <= ts < b),
                     "no_data_minutes": _minute(gap)})
    return {"state": state, "days": days, "truncated": False}


def series(kind, runs_by_entity, start, end, state=None) -> dict:
    """The drawn series for one chart, from `gapped_runs` per entity.

    timeline / overlay: per local day, per entity, `[start, end, state]` in
    elapsed local minutes, `None` for no data. heatmap: 7×24 counts (Monday
    first) of changes into `state` by local weekday and hour, and how many of
    each weekday the window touched. daily_count: per local day, changes into
    `state` and how many minutes had no data — a zero on a day the device was
    unavailable is not a zero."""
    if kind in MARKER_TYPES:
        return _day_series(runs_by_entity, start, end)
    (runs,) = runs_by_entity.values()
    if kind == "heatmap":
        return _heatmap(runs, start, end, state)
    return _daily_count(runs, start, end, state)


# --------------------------------------------------------------------------
# markers: reading the config (pure, reject by default)
# --------------------------------------------------------------------------
class _Unsupported(Exception):
    """The first part of a config the marker subset does not read."""


_TOP_LEVEL = {"id", "alias", "description", "trigger", "condition", "action", "mode"}
_STATE_TRIGGER = {"platform", "trigger", "entity_id", "to", "from", "for", "id", "alias"}
_TIME_TRIGGER = {"platform", "trigger", "at", "id", "alias"}
_STATE_CONDITION = {"condition", "entity_id", "state", "alias"}
_TIME_CONDITION = {"condition", "after", "before", "weekday", "alias"}
_AND_CONDITION = {"condition", "conditions", "alias"}
_ACTION = {"service", "action", "target", "data", "alias"}
_DOMAINS = {"light", "switch", "fan", "input_boolean"}
_SERVICES = {"turn_on", "turn_off", "toggle"}
# Home Assistant's weekday spelling, date.weekday() order (as core/replay.py
# quotes it: both read HA's schema, neither the other).
_HA_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DURATION_KEYS = {"days": 86400, "hours": 3600, "minutes": 60, "seconds": 1,
                  "milliseconds": 0.001}


def _a(word) -> str:
    return f"an {word}" if word[:1] in "aeiou" else f"a {word}"


def _listed(value):
    return value if isinstance(value, list) else [value]


def _strings(value, what) -> list:
    """A str or a non-empty list of str, as HA accepts for an entity or state."""
    items = _listed(value)
    if not items or not all(isinstance(v, str) and v.strip() for v in items):
        raise _Unsupported(f"{what} that is not a name or a list of names")
    return [v.strip() for v in items]


def _check_keys(mapping, allowed, what):
    extra = sorted(k for k in mapping if k not in allowed)
    if extra:
        raise _Unsupported(f"`{extra[0]}` in {what}")


def _no_templates(value, where="the config"):
    if isinstance(value, str):
        if "{{" in value or "{%" in value:
            raise _Unsupported(f"a template in {where}")
    elif isinstance(value, dict):
        for key, inner in value.items():
            _no_templates(inner, f"`{key}`")
    elif isinstance(value, list):
        for inner in value:
            _no_templates(inner, where)


def _hms(value, what) -> int:
    """"HH:MM" or "HH:MM:SS" → seconds of the day (HA's cv.time)."""
    parts = value.split(":") if isinstance(value, str) else []
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise _Unsupported(f"{what} {value!r}, which is not HH:MM[:SS]")
    hour, minute, second = (int(p) for p in parts + ["0"] * (3 - len(parts)))
    if not (hour < 24 and minute < 60 and second < 60):
        raise _Unsupported(f"{what} {value!r}, which is not a time of day")
    return hour * 3600 + minute * 60 + second


def _duration(value) -> int:
    """A `for:` → seconds: a number, "HH:MM[:SS]" or a dict (HA's
    cv.positive_time_period)."""
    if isinstance(value, bool):
        raise _Unsupported("a `for` that is not a duration")
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    if isinstance(value, str):
        parts = value.split(":")
        if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
            hours, minutes, seconds = (int(p) for p in parts + ["0"] * (3 - len(parts)))
            return hours * 3600 + minutes * 60 + seconds
        raise _Unsupported(f"a `for` of {value!r}")
    if isinstance(value, dict) and value:
        _check_keys(value, _DURATION_KEYS, "a `for`")
        total = 0
        for key, amount in value.items():
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
                raise _Unsupported(f"a `for` whose {key} is not a number")
            total += amount * _DURATION_KEYS[key]
        return int(total)
    raise _Unsupported("a `for` that is not a duration")


def _trigger(trigger) -> dict:
    if not isinstance(trigger, dict):
        raise _Unsupported("a trigger that is not a mapping")
    platform = trigger.get("platform", trigger.get("trigger"))
    if platform == "state":
        _check_keys(trigger, _STATE_TRIGGER, "a state trigger")
        entities = _strings(trigger.get("entity_id"), "a state trigger on something")
        if "to" not in trigger and "from" not in trigger:
            # HA then fires on attribute changes too, which the record does
            # not hold: every marker drawn would be a lower bound.
            raise _Unsupported("a state trigger with neither `to` nor `from`")
        to = _strings(trigger["to"], "a `to`") if "to" in trigger else None
        frm = _strings(trigger["from"], "a `from`") if "from" in trigger else None
        hold = _duration(trigger["for"]) if "for" in trigger else 0
        if hold and to is None:
            # With `from` alone, HA holds the trigger while the state is not
            # the old one, and whether a second matching change replaces a
            # pending one is not pinned down here — so it is not drawn.
            raise _Unsupported("a state trigger with `for` and `from` but no `to`")
        return {"kind": "state", "entities": entities, "to": to, "from": frm, "for": hold}
    if platform == "time":
        _check_keys(trigger, _TIME_TRIGGER, "a time trigger")
        ats = _listed(trigger.get("at"))
        if not ats:
            raise _Unsupported("a time trigger with no `at`")
        for at in ats:
            if isinstance(at, str) and "." in at:
                raise _Unsupported(f"a time trigger at an entity's time ({at})")
        return {"kind": "time", "at": [_hms(at, "a time trigger at") for at in ats]}
    raise _Unsupported(f"{_a(platform)} trigger" if isinstance(platform, str)
                       else "a trigger with no platform")


def _conditions(conditions) -> list:
    out = []
    for condition in _listed(conditions):
        if not isinstance(condition, dict):
            raise _Unsupported("a condition that is not a mapping")
        kind = condition.get("condition")
        if kind == "state":
            _check_keys(condition, _STATE_CONDITION, "a state condition")
            if "state" not in condition:
                raise _Unsupported("a state condition with no `state`")
            out.append({"kind": "state",
                        "entities": _strings(condition.get("entity_id"), "a state condition on something"),
                        "states": _strings(condition["state"], "a condition `state`")})
        elif kind == "time":
            _check_keys(condition, _TIME_CONDITION, "a time condition")
            if not any(k in condition for k in ("after", "before", "weekday")):
                raise _Unsupported("a time condition with no `after`, `before` or `weekday`")
            for edge in ("after", "before"):
                if isinstance(condition.get(edge), str) and "." in condition[edge]:
                    raise _Unsupported(f"a time condition {edge} an entity's time")
            days = None
            if "weekday" in condition:
                names = _strings(condition["weekday"], "a `weekday`")
                if not all(n in _HA_WEEKDAYS for n in names):
                    raise _Unsupported(f"a `weekday` of {names!r}")
                days = frozenset(_HA_WEEKDAYS.index(n) for n in names)
            out.append({"kind": "time",
                        "after": _hms(condition["after"], "a time condition after")
                        if "after" in condition else None,
                        "before": _hms(condition["before"], "a time condition before")
                        if "before" in condition else None,
                        "days": days})
        elif kind == "and":
            _check_keys(condition, _AND_CONDITION, "an and condition")
            out.extend(_conditions(condition.get("conditions") or []))
        else:
            raise _Unsupported(f"{_a(kind)} condition" if isinstance(kind, str)
                               else "a condition with no `condition`")
    return out


def _action(action) -> dict:
    if not isinstance(action, dict):
        raise _Unsupported("an action that is not a mapping")
    if "service" not in action and "action" not in action:
        first = next(iter(action), "empty")
        raise _Unsupported(f"{_a(str(first))} action")
    _check_keys(action, _ACTION, "an action")
    if "service" in action and "action" in action:
        raise _Unsupported("an action naming both `service` and `action`")
    name = action.get("service", action.get("action"))
    domain, _, service = name.partition(".") if isinstance(name, str) else ("", "", "")
    if domain not in _DOMAINS or service not in _SERVICES:
        raise _Unsupported(f"the service {name}")
    target = action.get("target")
    if not isinstance(target, dict) or "entity_id" not in target:
        raise _Unsupported(f"{name} without target.entity_id")
    _check_keys(target, {"entity_id"}, "a target")
    if "data" in action and not isinstance(action["data"], dict):
        raise _Unsupported("action data that is not a mapping")
    return {"service": service, "entities": _strings(target["entity_id"], "a target")}


def rule(config):
    """A config read into the marker subset → (plan, None) or (None, note).

    The whole gate: every key at every level is either read or refused, a
    template anywhere is refused, and the note names the first refusal."""
    try:
        if not isinstance(config, dict) or not config:
            raise _Unsupported("no config")
        cfg = dict(config)
        for plural, singular in (("triggers", "trigger"), ("conditions", "condition"),
                                 ("actions", "action")):
            if plural in cfg and singular not in cfg:
                cfg[singular] = cfg.pop(plural)
        _check_keys(cfg, _TOP_LEVEL, "the automation")
        _no_templates(cfg)
        if cfg.get("mode", "single") != "single":
            raise _Unsupported(f"mode {cfg['mode']}")
        triggers = [_trigger(t) for t in _listed(cfg.get("trigger") or [])]
        actions = [_action(a) for a in _listed(cfg.get("action") or [])]
        if not triggers or not actions:
            raise _Unsupported("no trigger or no action")
        return {"triggers": triggers, "conditions": _conditions(cfg.get("condition") or []),
                "actions": actions}, None
    except _Unsupported as e:
        return None, f"{CANNOT_DRAW_RULE}: it uses {e}"


def read_entities(plan) -> set:
    """Every entity whose state the plan reads (state triggers, conditions).
    Action targets are read only where charted, which the caller knows."""
    out = set()
    for trigger in plan["triggers"]:
        out.update(trigger.get("entities") or [])
    for condition in plan["conditions"]:
        out.update(condition.get("entities") or [])
    return out


def lookback(plan) -> int:
    """How far before the window a transition can still fire inside it."""
    return max([t.get("for", 0) for t in plan["triggers"]] + [0])


# --------------------------------------------------------------------------
# markers: evaluating the plan over the runs (pure)
# --------------------------------------------------------------------------
def _state_at(runs, ts):
    for f, t, s in runs:
        if f <= ts < t:
            return s
    return None


def _state_fires(trigger, runs_by_entity, start, end) -> list:
    fires = []
    for entity in trigger["entities"]:
        runs = runs_by_entity.get(entity) or []
        for prev, cur in zip(runs, runs[1:]):
            ts, old, new = cur[0], prev[2], cur[2]
            if trigger["from"] is not None and old not in trigger["from"]:
                continue
            if trigger["to"] is not None and new not in trigger["to"]:
                continue
            at = ts + trigger["for"]
            # Held for the whole duration: this run outlasts the deadline.
            # A run clipped at the window's end has not been seen to.
            if trigger["for"] and not (cur[1] > at and at < end):
                continue
            if start <= at < end:
                fires.append(at)
    return fires


def _time_fires(trigger, start, end) -> list:
    fires = []
    for day, *_ in _days(start, end):
        for seconds in trigger["at"]:
            ts = window.first_instant(day, seconds // 3600, seconds // 60 % 60, seconds % 60)
            if ts is not None and start <= ts < end:
                fires.append(ts)
    return fires


def _condition(condition, ts, runs_by_entity):
    """True / False, or None when the record holds no state to judge by."""
    if condition["kind"] == "state":
        for entity in condition["entities"]:
            state = _state_at(runs_by_entity.get(entity) or [], ts)
            if state is None:
                return None
            if state not in condition["states"]:
                return False
        return True
    local = time.localtime(ts)
    now = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
    # homeassistant.helpers.condition.time: after defaults to 00:00, before
    # to 23:59:59.999999; after < before is a same-day window, otherwise it
    # crosses midnight and only [before, after) is outside it.
    after = condition["after"] if condition["after"] is not None else 0
    before = condition["before"] if condition["before"] is not None else 86400
    if after < before:
        if not after <= now < before:
            return False
    elif before <= now < after:
        return False
    if condition["days"] is not None and local.tm_wday not in condition["days"]:
        return False
    return True


def markers(config, runs_by_entity, window_, charted=None):
    """What `config` would have done over `window_` = (start, end) →
    (markers | None, note).

    `runs_by_entity` holds RAW-state runs (`timeline.runs`, no gap mapping)
    for every entity the rule reads and every charted target, reaching back
    `lookback(plan)` before the window so a `for` begun earlier can fire
    inside it. `charted` is the entities the chart draws (default: every key).
    Markers are `{day, minute, action, entity}`, oldest first; None with a
    note when the rule is outside the subset. A fire whose condition or
    toggle the record cannot judge draws nothing and is counted in the note."""
    plan, note = rule(config)
    if plan is None:
        return None, note
    start, end = int(window_[0]), int(window_[1])
    charted = set(runs_by_entity if charted is None else charted)
    targets = {e for a in plan["actions"] for e in a["entities"]}
    if not targets & charted:
        return [], f"the rule acts on {', '.join(sorted(targets))}, which this chart does not draw"

    fires = set()
    for trigger in plan["triggers"]:
        fires.update(_state_fires(trigger, runs_by_entity, start, end)
                     if trigger["kind"] == "state" else _time_fires(trigger, start, end))

    days = {d: lo for d, lo, *_ in _days(start, end)}
    out, unjudged = [], 0
    for ts in sorted(fires):
        verdicts = [_condition(c, ts, runs_by_entity) for c in plan["conditions"]]
        if False in verdicts:
            continue
        if None in verdicts:
            unjudged += 1
            continue
        day = window._local_date(ts)
        for action in plan["actions"]:
            for entity in action["entities"]:
                if entity not in charted:
                    continue
                state = _state_at(runs_by_entity.get(entity) or [], ts)
                if state is None:
                    unjudged += 1
                    continue
                if state == "unavailable":
                    continue
                service = action["service"]
                if service == "toggle":
                    service = "turn_off" if state == "on" else "turn_on"
                marker = {"day": day.isoformat(), "minute": _minute(ts - days[day]),
                          "action": service, "entity": entity}
                if not out or out[-1] != marker:
                    out.append(marker)

    notes = []
    if unjudged:
        notes.append(f"{unjudged} time(s) the rule would have run could not be judged: "
                     f"the record holds no state for what it reads at that moment")
    if len(out) > MARKERS_MAX:
        notes.append(f"only the most recent {MARKERS_MAX} of {len(out)} markers are drawn")
        out = out[-MARKERS_MAX:]
    return out, "; ".join(notes) or None


# --------------------------------------------------------------------------
# I/O: drawing from the record, and redrawing a stored row
# --------------------------------------------------------------------------
def _chart(spec, error=None, series_=None, markers_=None, note=None, clamped=None) -> dict:
    return {"spec": spec, "series": series_, "markers": markers_, "markers_note": note,
            "error": error, "drawn_at": int(time.time()), "window_clamped": clamped}


def _state_before(entity, ts):
    # The state in force AT `ts`, a row exactly at `ts` included: state_at
    # answers strictly before, and runs() merges a row at `start` into the
    # run `state_before` opened — so asking at ts + 1 is what makes that row
    # the first run's state rather than swallowed by the one before it. The
    # cost: a change in the window's very first second is the state the
    # window opens in, not a change a count or a trigger sees.
    return record.state_at(entity, int(ts) + 1)


def _marker_runs(plan, entities, start, end, watched):
    """RAW runs for the marker evaluator, or (None, note) when it reads an
    entity the record does not hold."""
    back = start - lookback(plan) - 1
    out = {}
    for entity in sorted(entities):
        if entity not in watched or record.first_ts(entity) is None:
            return None, (f"{CANNOT_DRAW_RULE}: the record does not hold {entity}, "
                          f"which it reads")
        rows = record.between(entity, back, end)
        out[entity] = timeline.runs(rows, back, end, _state_before(entity, back))
    return out, None


def draw(spec, config, now) -> dict:
    """F-7 and F-8: one idea's chart column, drawn from the record now.

    `{spec, series, markers, markers_note, error, drawn_at, window_clamped}`.
    `spec` is the drawn (normalised, clamped) request, or the request's known
    keys when it could not be drawn at all. Never raises: a drawing that
    fails is an `error`, and the idea is stored either way (F-7)."""
    known, why = request(spec, now)
    if why:
        return _chart(known, why)
    try:
        watched = frozenset(watchlist.watched_entities())
        unwatched = [e for e in known["entities"] if e not in watched]
        if unwatched:
            return _chart(known, f"{unwatched[0]} is not watched, so it is not drawn")
        firsts = {}
        for entity in known["entities"]:
            firsts[entity] = record.first_ts(entity)
            if firsts[entity] is None:
                return _chart(known, f"the record holds nothing for {entity}")
        drawn, clamped, why = clamp(known, now, min(firsts.values()))
        if why:
            return _chart(known, why)
        start, end = drawn["start"], drawn["end"]

        runs, seen = {}, False
        for entity in drawn["entities"]:
            rows = record.between(entity, start, end)
            before = _state_before(entity, start)
            seen = seen or bool(rows) or before is not None
            runs[entity] = gapped_runs(rows, start, end, before)
        if not seen:
            return _chart(drawn, "the record holds no rows in that window", clamped=clamped)
        drawn_series = series(drawn["type"], runs, start, end, drawn["state"])

        marks, note = None, None
        if drawn["type"] in MARKER_TYPES:
            plan, note = rule(config)
            if plan is not None:
                targets = {e for a in plan["actions"] for e in a["entities"]}
                needed = read_entities(plan) | (targets & set(drawn["entities"]))
                raw, note = _marker_runs(plan, needed, start, end, watched)
                if raw is not None:
                    marks, note = markers(config, raw, (start, end), charted=drawn["entities"])
        return _chart(drawn, None, drawn_series, marks, note, clamped)
    except Exception as e:  # noqa: BLE001 — the idea must survive its chart
        logger.error(f"CHARTS: could not draw {known}: {e}", exc_info=True)
        return _chart(known, f"the chart could not be drawn: {e}")


def redraw(suggestion_id):
    """Draws a stored explorer row's chart again from its own spec and config
    and writes it back (`suggestions.set_chart`). For rows stored before
    F-WP4 (`series: null`) or refused a window F-WP4 now clamps.

    "now" is the row's `created_at` — the run that stored it — so a spec
    kept as the model sent it ("end": "now") draws the window the model
    looked at, not today's. A normalised spec carries epochs and needs no
    "now" at all. A clamped chart is redrawn from the window it was ASKED
    for (`window_clamped.asked`): its `spec` holds only the window drawn, so
    drawing that again clamps nothing and the card's "the agent asked for …"
    sentence would vanish on a second redraw. Returns the chart drawn, or
    None when there is no such explorer row."""
    row = suggestions.suggestion(suggestion_id)
    chart = (row or {}).get("chart")
    if (not row or row.get("source") != suggestions.SOURCE_ANALYST
            or row.get("evidence_level") != suggestions.EVIDENCE_SHOWN
            or not isinstance(chart, dict) or "spec" not in chart):
        logger.error(f"CHARTS: suggestion {suggestion_id} is not an explorer row with a chart")
        return None
    spec = chart["spec"]
    asked = (chart.get("window_clamped") or {}).get("asked") \
        if isinstance(chart.get("window_clamped"), dict) else None
    if isinstance(spec, dict) and isinstance(asked, dict) and "start" in asked and "end" in asked:
        spec = {**spec, "start": asked["start"], "end": asked["end"]}
    drawn = draw(spec, row.get("config"), int(row.get("created_at") or time.time()))
    if not suggestions.set_chart(suggestion_id, drawn):
        return None
    logger.info(f"CHARTS: redrew suggestion {suggestion_id}"
                + (f" — {drawn['error']}" if drawn["error"] else ""))
    return drawn
