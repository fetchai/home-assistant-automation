#!/usr/bin/env python3
"""The four MQTT analyst test devices, and the days they live through.

Shared by plant_analyst.py and simulate_analyst.py the way standby_devices.py
and schedule_devices.py are shared by theirs: the history the analyst reads and
the states the agent watches live have to describe one household.

Not run directly.


WHY A FOURTH SET
----------------
Every other test device in this directory is a POWER sensor with a switch,
because both detectors read watts. The analyst (core/analyst.py, proposal
§5B) exists for exactly what they cannot see: habits between entities, and
habits of entities that report no number at all. Nothing here reports power,
so neither detector raises a candidate on any of it, and whatever reaches the
Suggestions tab from these devices came from the model — which is the thing
this set is for.

The first three were shaped for the audited run (retired, free-hand F-9) so
that ONE claim type of its vocabulary (C-1) was the natural way to evidence
each; the names below are kept because they still say what the habit IS. The
fourth, `study`, was shaped so that none could:

  hallway  motion, then the hallway light by hand within a minute — on
           evenings only. A `follows` claim (Get observed patterns hands the
           model the pair directly), and the obvious automation is motion →
           light with a time or sun condition, which the daytime visits make
           the model's to notice.
  bathroom an extractor fan switched on every morning and, most days,
           forgotten for hours. `transitions` to "on" and `on_hours` evidence
           it; the automation is "turn it off after N minutes".
  porch    a porch light switched on by hand around 19:00 on weekdays and
           later at weekends. `transitions` with `hours` and `weekdays`; the
           automation is a time (or sunset) trigger on weekdays.
  study    an occupancy sensor and a desk lamp (free-hand F-WP8). The lamp
           comes on as someone sits down on a weekday morning, stays on over
           lunch, and on about half the evenings is forgotten when they leave
           at 17:00 — off only at 21:00-23:00, when someone passes. "On while
           the room is empty" is an ABSENCE of co-change: no claim type could
           state it, and no aggregate shows it. Only
           the explorer's own SQL overlap query (tool_record_query example (b))
           finds it, which is what this device is for. The automation is
           "occupancy off for N minutes → lamp off".

The states are Home Assistant's own ("on"/"off"), as scenario.py's are; the
MQTT payload mapping is simulate_analyst.py's.


WHAT THE TIMES HAVE TO SATISFY
------------------------------
record.scan_relations pairs ANY two entities changing within
RELATION_WINDOW_SECONDS (120 s), and the power test devices already in the
record write a heartbeat on every quarter hour. An event planted a minute
after :15 pairs with every one of them, and "when the lounge TV reports power,
the hallway light comes on" is a pattern the model would be shown with a real
count behind it. So every event here is placed between minute 3 and minute 12
of its quarter (`_quiet`): more than 120 s after the last heartbeat and more
than 120 s before the next. That keeps the heartbeats out of THESE pairs; it
cannot keep the live MQTT scenario's own random events out, and does not try.
"""
import random
import time
from datetime import date

DEVICES = {
    "hallway": {
        "name": "Test hallway",
        "unique": "ceelabs_test_hallway",
        "entities": {
            "motion": {"entity_id": "binary_sensor.test_hallway_motion",
                       "component": "binary_sensor", "name": "Motion",
                       "topic": "test/hallway/motion/state"},
            "light": {"entity_id": "light.test_hallway_light",
                      "component": "light", "name": "Light",
                      "topic": "test/hallway/light/state"},
        },
        "expect": "follows: motion → light, evenings only — a motion-light automation",
    },
    "bathroom": {
        "name": "Test bathroom",
        "unique": "ceelabs_test_bathroom",
        "entities": {
            "fan": {"entity_id": "switch.test_bathroom_extractor_fan",
                    "component": "switch", "name": "Extractor fan",
                    "topic": "test/bathroom/fan/state"},
        },
        "expect": "transitions + on_hours: left running for hours — an auto-off timer",
    },
    "porch": {
        "name": "Test porch",
        "unique": "ceelabs_test_porch",
        "entities": {
            "light": {"entity_id": "light.test_porch_light",
                      "component": "light", "name": "Light",
                      "topic": "test/porch/light/state"},
        },
        "expect": "transitions (hours, weekdays): on by hand ~19:00 Mon-Fri — a time trigger",
    },
    "study": {
        "name": "Test study",
        "unique": "ceelabs_test_study",
        "entities": {
            "occupancy": {"entity_id": "binary_sensor.test_study_occupancy",
                          "component": "binary_sensor", "name": "Occupancy",
                          "device_class": "occupancy",
                          "topic": "test/study/occupancy/state"},
            "lamp": {"entity_id": "light.test_study_desk_lamp",
                     "component": "light", "name": "Desk lamp",
                     "topic": "test/study/lamp/state"},
        },
        "expect": "explorer only: lamp on while the study is empty, weekday evenings — "
                  "occupancy off for N minutes → lamp off",
    },
}

# Every entity id this set owns, and the only ones any of the three scripts
# will write, clear or add to the watchlist.
ENTITY_IDS = tuple(entity["entity_id"]
                   for device in DEVICES.values()
                   for entity in device["entities"].values())

HALLWAY_MOTION = DEVICES["hallway"]["entities"]["motion"]["entity_id"]
HALLWAY_LIGHT = DEVICES["hallway"]["entities"]["light"]["entity_id"]
BATHROOM_FAN = DEVICES["bathroom"]["entities"]["fan"]["entity_id"]
PORCH_LIGHT = DEVICES["porch"]["entities"]["light"]["entity_id"]
STUDY_OCCUPANCY = DEVICES["study"]["entities"]["occupancy"]["entity_id"]
STUDY_LAMP = DEVICES["study"]["entities"]["lamp"]["entity_id"]

# How often the hallway light answers evening motion. Not 1.0: a habit a
# person has is one they sometimes skip, and a pattern that holds every single
# time reads as an automation that already exists.
LIGHT_ANSWERS = 0.9

# Share of mornings the fan is forgotten. The rest it is switched off after a
# normal shower's worth of minutes, so the model has both to tell apart.
FAN_FORGOTTEN = 0.6

# Share of weekday evenings the study lamp is left on after the room empties.
# The rest it is switched off within a few minutes of leaving, so the
# overlap query has both to tell apart — and the lunch hour, when the lamp
# stays on in an empty room every day, is an overlap no automation should
# act on for an hour's absence; the forgotten evenings are the 4-6 hours.
LAMP_FORGOTTEN = 0.5

# Motion is a pulse: the sensor clears this long after it tripped. Under
# RELATION_WINDOW_SECONDS on purpose — motion clearing is itself an event, and
# a light switched off within 120 s of it would pair "motion → light" a second
# time per visit. The light stays on well past that (see _hallway).
MOTION_PULSE_SECONDS = 90


def _epoch(day: date, hour: int, minute: int, second: int = 0) -> int:
    """A local wall time as an epoch second, through mktime with tm_isdst=-1
    so the C library resolves a DST boundary — plant_standby.py's `_epoch`."""
    return int(time.mktime((day.year, day.month, day.day, hour, minute, second, 0, 0, -1)))


def _quiet(ts: int) -> int:
    """`ts`, moved forward to the next moment clear of every quarter-hour
    heartbeat by more than RELATION_WINDOW_SECONDS — minute 3..12 of its
    quarter, as the module docstring sets out. Forward only, so a duration
    measured from an earlier event never shrinks."""
    local = time.localtime(ts)
    into = (local.tm_min % 15) * 60 + local.tm_sec
    if into < 3 * 60 + 1:
        return ts + (3 * 60 + 1 - into)
    if into > 12 * 60:
        return ts + (15 * 60 - into) + 3 * 60 + 1
    return ts


def _slot(day: date, hour: int, quarter: int, rng: random.Random, latest: int = 8) -> int:
    """A quiet moment inside the given quarter hour, no later than minute
    `latest` of it — lower for an event whose follow-ups must still end
    before minute 12."""
    return _epoch(day, hour, quarter * 15 + rng.randint(3, latest), rng.randint(0, 59))


def _hallway(day: date, rng: random.Random, evening: bool, busy=()) -> list:
    """One day's visits: 3-5 in daylight, which nothing answers, and 4-7 in
    the evening, which the light answers LIGHT_ANSWERS of the time. Visits
    take distinct quarter hours so one never overlaps the next, and none takes
    a quarter in `busy` — the porch light's, which would otherwise pair with
    the hallway a handful of times a month for no reason a person has.

    Tripped by minute 6 of the quarter: the light goes off up to 300 s later,
    and that has to land by minute 12 as well (`_quiet`)."""
    events = []
    hours = range(18, 23) if evening else range(8, 17)
    visits = rng.randint(4, 7) if evening else rng.randint(3, 5)
    free = [(h, q) for h in hours for q in range(4) if (h, q) not in busy]
    for hour, quarter in sorted(rng.sample(free, visits)):
        tripped = _slot(day, hour, quarter, rng, latest=6)
        events.append((tripped, HALLWAY_MOTION, "on"))
        events.append((tripped + MOTION_PULSE_SECONDS, HALLWAY_MOTION, "off"))
        if evening and rng.random() < LIGHT_ANSWERS:
            events.append((tripped + rng.randint(10, 45), HALLWAY_LIGHT, "on"))
            # Off 4-5 minutes after the trip: past the motion clearing by far
            # more than 120 s, and still inside the same quiet stretch.
            events.append((tripped + rng.randint(240, 300), HALLWAY_LIGHT, "off"))
    return events


def _bathroom(day: date, rng: random.Random) -> list:
    """On once each morning — 06:45-07:15 on weekdays, 08:30-09:30 at the
    weekend — and off after 20-35 minutes, or after 3-6 hours on a forgotten
    morning."""
    weekend = day.weekday() >= 5
    hour, quarter = (rng.choice([(8, 2), (8, 3), (9, 0), (9, 1)]) if weekend
                     else rng.choice([(6, 3), (7, 0)]))
    on = _slot(day, hour, quarter, rng)
    minutes = (rng.randint(180, 360) if rng.random() < FAN_FORGOTTEN
               else rng.randint(20, 35))
    return [(on, BATHROOM_FAN, "on"), (_quiet(on + minutes * 60), BATHROOM_FAN, "off")]


def _porch(day: date, rng: random.Random) -> list:
    """On by hand 18:45-19:15 on weekdays and 19:45-20:30 at the weekend; off
    23:00-23:45. The weekday hour sits inside [18, 20) and the weekend one does
    not, so a `transitions` claim with hours [18, 20) and weekdays 0-4 counts
    the weekday habit and nothing else. Also returns the quarters it used, for
    the hallway to stay out of."""
    weekend = day.weekday() >= 5
    hour, quarter = (rng.choice([(19, 3), (20, 0), (20, 1)]) if weekend
                     else rng.choice([(18, 3), (19, 0)]))
    off = rng.randint(0, 2)
    return [(_slot(day, hour, quarter, rng), PORCH_LIGHT, "on"),
            (_slot(day, 23, off, rng), PORCH_LIGHT, "off")], {(hour, quarter), (23, off)}


def _study(day: date, rng: random.Random, busy=()) -> tuple:
    """Weekdays only. Occupied from ~08:30 to ~12:00 and ~13:00 to ~17:00;
    the lamp on 20-60 s after the first arrival and left on over lunch. At
    17:00 it goes off 60-180 s after the room empties, or, on LAMP_FORGOTTEN
    of the evenings, at a quiet quarter between 21:00 and 23:00 that the
    porch light does not use. Every event sits in its own quarter (`_slot`),
    and the quarters are returned for the hallway to stay out of — its
    daytime visits would otherwise pair with the study's arrivals."""
    if day.weekday() >= 5:
        return [], set()
    arrive, lunch, back, leave = (8, rng.choice([1, 2])), (12, 0), (13, 0), (17, rng.choice([0, 1]))
    occupied = [(_slot(day, h, q, rng, latest=6), state)
                for (h, q), state in ((arrive, "on"), (lunch, "off"), (back, "on"), (leave, "off"))]
    events = [(ts, STUDY_OCCUPANCY, state) for ts, state in occupied]
    events.append((occupied[0][0] + rng.randint(20, 60), STUDY_LAMP, "on"))
    quarters = {arrive, lunch, back, leave}
    if rng.random() < LAMP_FORGOTTEN:
        free = [(h, q) for h in (21, 22) for q in range(4) if (h, q) not in busy]
        late = rng.choice(free)
        quarters.add(late)
        events.append((_slot(day, *late, rng), STUDY_LAMP, "off"))
    else:
        events.append((occupied[3][0] + rng.randint(60, 180), STUDY_LAMP, "off"))
    return events, quarters


def day_events(day: date, rng: random.Random) -> list:
    """Everything the four devices do on one local day, as (ts, entity_id,
    state), oldest first. One function for both scripts: the planter walks it
    over thirty days, the simulator plays today's remainder at the wall
    clock."""
    porch, porch_quarters = _porch(day, rng)
    study, study_quarters = _study(day, rng, busy=porch_quarters)
    events = (porch + study + _bathroom(day, rng)
              + _hallway(day, rng, evening=False, busy=study_quarters)
              + _hallway(day, rng, evening=True, busy=porch_quarters | study_quarters))
    return sorted(events)


def day_rng(seed: int, day: date) -> random.Random:
    """One generator per day, seeded by the date, so the simulator playing
    today produces exactly the day the planter would have written for it."""
    return random.Random(seed * 100000 + day.toordinal())
