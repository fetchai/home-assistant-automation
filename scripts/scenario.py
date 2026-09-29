"""A plausible week in a small home, as state transitions.

Shared by both dev scripts: plant_test_history.py writes these into Home
Assistant's recorder with past timestamps, simulate_test_devices.py publishes
the same shapes over MQTT as they happen. One generator, so the history the
agent backfills and the events it watches live describe the same household
rather than two unrelated ones.

The patterns are not decoration. Each maps to a suggestion type the design
puts in scope for v1 (§8), so that the replay and suggestion work has
something real to find:

  motion → light        the "manual habit": a light switched on by hand within
                        a couple of minutes of motion, over and over
  switch left on        the "standby load": left running overnight on some
                        days and not others, which is what makes it a habit
                        worth telling someone about rather than a constant
  blind + thermostat    the daily rhythm that gives hour-of-day and
                        day-of-week profiles a shape to have
  temperature           a sinusoidal drift sampled every few minutes, which is
                        mostly noise — deliberately, since thinning it is the
                        numeric threshold rule's whole job

States are Home Assistant's own vocabulary ("on", "open", "heat"), not MQTT
payloads; each script maps them to its own wire format.

Stdlib only, and pure: given the same seed it produces the same week.
"""
from datetime import datetime, time as dtime, timedelta
import math
import random

# The six MQTT test entities. Nothing here touches anything else.
LIGHT = "light.test_kitchen_light"
SWITCH = "switch.test_living_room_switch"
BLIND = "cover.test_bedroom_blind"
THERMOSTAT = "climate.test_thermostat"
MOTION = "binary_sensor.test_motion_sensor"
TEMPERATURE = "sensor.test_temperature_sensor"

ENTITIES = (LIGHT, SWITCH, BLIND, THERMOSTAT, MOTION, TEMPERATURE)

# How often the temperature sensor reports. A real one reports far more often
# than it meaningfully changes, which is the point.
TEMPERATURE_INTERVAL_SECONDS = 300


def _local_midnight(day) -> float:
    """Epoch seconds of local midnight on `day`.

    Local, not UTC: "the light comes on around seven" is a statement about the
    household's clock, and the hour-of-day profile reads these back in local
    time too."""
    return datetime.combine(day, dtime(0, 0)).timestamp()


def _at(midnight: float, hour: float) -> float:
    return midnight + hour * 3600.0


def _jitter(rng, hour: float, spread_minutes: float) -> float:
    return hour + rng.uniform(-spread_minutes, spread_minutes) / 60.0


def _motion_burst(rng, midnight, hour, events, count=None):
    """Someone moving through the kitchen: a few detections, then quiet.

    Emitted as on/off pairs rather than single pulses, because that is what the
    record stores durations from."""
    for _ in range(count if count is not None else rng.randint(1, 3)):
        start = _at(midnight, _jitter(rng, hour, 12))
        events.append((MOTION, start, "on"))
        events.append((MOTION, start + rng.uniform(20, 90), "off"))
        hour += rng.uniform(0.05, 0.2)


def _day(rng, day, events):
    """One day of the household, weekday or weekend."""
    midnight = _local_midnight(day)
    weekend = day.weekday() >= 5

    # --- morning ---------------------------------------------------------
    wake = _jitter(rng, 8.5 if weekend else 6.75, 25)
    events.append((BLIND, _at(midnight, wake), "open"))
    events.append((THERMOSTAT, _at(midnight, wake - 0.25), "heat"))
    _motion_burst(rng, midnight, wake + 0.1, events, count=rng.randint(2, 4))

    # --- daytime ---------------------------------------------------------
    # Someone is in and out during a weekend, mostly out on a weekday.
    for hour in ([10, 12.5, 14, 16] if weekend else [12.5]):
        if rng.random() < (0.8 if weekend else 0.4):
            _motion_burst(rng, midnight, hour, events)

    events.append((THERMOSTAT, _at(midnight, 9.5), "off"))

    # --- evening ---------------------------------------------------------
    home = _jitter(rng, 17.5 if not weekend else 16.5, 40)
    events.append((THERMOSTAT, _at(midnight, home), "heat"))
    events.append((SWITCH, _at(midnight, home + rng.uniform(0.1, 0.6)), "on"))
    for hour in (home + 0.2, home + 1.5, home + 3.0, home + 4.2):
        if rng.random() < 0.85:
            _motion_burst(rng, midnight, hour, events)

    # --- night -----------------------------------------------------------
    bed = _jitter(rng, 23.2 if not weekend else 24.0, 45)
    events.append((BLIND, _at(midnight, bed), "closed"))
    events.append((THERMOSTAT, _at(midnight, bed), "off"))

    # The standby load: most nights the switch goes off, and on a third of them
    # it is forgotten until morning. A constant would be a device that is
    # simply always on; it is the inconsistency that makes it a habit.
    if rng.random() < 0.66:
        events.append((SWITCH, _at(midnight, bed + rng.uniform(0.02, 0.3)), "off"))
    else:
        events.append((SWITCH, _at(midnight, 24 + 7.0 + rng.uniform(0, 1.5)), "off"))

    # Someone up in the night, occasionally.
    if rng.random() < 0.3:
        _motion_burst(rng, midnight, rng.uniform(1.5, 4.5), events, count=1)


def _lights_follow_motion(rng, events):
    """The habit: the kitchen light switched on by hand shortly after motion.

    Not every time — a pattern that holds 100% of the time is a rule someone
    already automated, not a habit worth pointing out. Roughly three times in
    four, which is enough to be obvious in the record and imperfect enough to
    be believable.
    """
    motion_starts = sorted(ts for entity, ts, state in events
                           if entity == MOTION and state == "on")
    last_off = 0.0
    for start in motion_starts:
        if start < last_off:
            continue  # the light is already on from the previous burst
        if rng.random() > 0.75:
            continue
        on = start + rng.uniform(30, 120)
        off = on + rng.uniform(120, 480)
        events.append((LIGHT, on, "on"))
        events.append((LIGHT, off, "off"))
        last_off = off


def temperature_at(ts: float, rng=None) -> str:
    """The room's temperature at a moment: coldest around 05:00, warmest around
    17:00, plus a little noise.

    Exposed because the live simulator does not put this on the scenario's
    timeline at all — see the note in simulate_test_devices.py — and samples the
    curve on its own clock instead."""
    hour = (ts % 86400) / 3600.0
    base = 21.0 + 3.0 * math.sin((hour - 9.0) / 24.0 * 2 * math.pi)
    return f"{base + (rng.gauss(0, 0.25) if rng else 0.0):.1f}"


def _temperature(rng, start_ts, end_ts, events, interval):
    """A room warming and cooling over the day, reported far more often than it
    meaningfully changes."""
    ts = start_ts
    while ts < end_ts:
        events.append((TEMPERATURE, ts, temperature_at(ts, rng)))
        ts += interval


def generate(start_ts: float, end_ts: float, seed: int = 20260824,
             temperature_interval: float = TEMPERATURE_INTERVAL_SECONDS) -> list:
    """Returns [(entity_id, ts, state), ...] over the window, oldest first.

    Deterministic for a given seed, so a planted week and a re-planted one are
    the same week.

    `temperature_interval` is how often the sensor reports, or None to leave
    the sensor out entirely. It is an argument because a continuous stream has
    no idle gaps to skip, so the live simulator cannot place it on a compressed
    timeline at all and ticks it on the wall clock instead."""
    rng = random.Random(seed)
    events = []

    day = datetime.fromtimestamp(start_ts).date()
    last = datetime.fromtimestamp(end_ts).date()
    while day <= last:
        _day(rng, day, events)
        day += timedelta(days=1)

    _lights_follow_motion(rng, events)
    # None means the caller reports temperature on its own schedule — the live
    # simulator does, because a continuous stream cannot be time-compressed.
    if temperature_interval:
        _temperature(rng, start_ts, end_ts, events, temperature_interval)

    # Clip to the window and drop consecutive repeats per entity: the record
    # would filter these out anyway, and leaving them in would misrepresent how
    # much was planted.
    events = sorted((e for e in events if start_ts <= e[1] <= end_ts), key=lambda e: e[1])
    out, previous = [], {}
    for entity_id, ts, state in events:
        if previous.get(entity_id) == state:
            continue
        previous[entity_id] = state
        out.append((entity_id, int(ts), state))
    return out


def summarise(events: list) -> dict:
    """{entity_id: count}, for the dry run."""
    counts = {}
    for entity_id, _, _ in events:
        counts[entity_id] = counts.get(entity_id, 0) + 1
    return counts
