#!/usr/bin/env python3
"""The three MQTT standby test devices, and the day they live through.

Shared by plant_standby.py and simulate_standby.py the way scenario.py is
shared by the other two, and for the same reason: the history the agent
backfills and the states it watches live have to describe one household. A
device whose planted nights sit at 12 W and whose live feed sits at 20 W is a
device the detector will judge on one series and the panel will show from
another.

Not run directly.


WHAT EACH DEVICE IS FOR
-----------------------
Three cases, chosen so that a run exercises the parts of the pipeline that can
be wrong in different ways rather than three shades of the same pass:

  tv    a clean standby load. Every night qualifies, replay credits every
        night, and this is the one that should reach a person.
  desk  a standby load the household USES after the cut would have happened.
        The detector still likes it — four bad nights out of thirty clear
        MIN_QUALIFYING_FRACTION — and it never reaches a person: replay counts
        four nights on which the rule would have cut power to a device drawing
        60 W, and review.MAX_WRONG_NIGHTS is 0.

        Which module says no matters, and it is not replay. Replay REPORTS
        `nights_wrong` as a fact and enforces nothing, deliberately — how many
        such nights disqualify a candidate is the review pass's policy, and
        putting a threshold inside the grading would hide it. So a run over
        this device should show a graded suggestion in the log and no question
        put to anyone. This is the only case that tests that seam.
  rack  the same shape as `tv`, reported in KILOWATTS. Every watt figure the
        user is shown crosses a factor of a thousand on its way, and the
        readback saying "less than 0.03 W" is a bug this repo has already
        shipped once (WP6, caught in review). If `rack` and `tv` do not come
        out with the same estimate, that line is broken again.


WHAT THE NUMBERS HAVE TO SATISFY
--------------------------------
Every value below is chosen against a rule in core/detectors/standby.py, and
changing one without reading that module is how a seeded night stops
qualifying for a reason nobody can see:

* the overnight level sits between STANDBY_MIN_WATTS and STANDBY_MAX_WATTS.
  Under the floor is a device that really switched off; over the ceiling is a
  device in use;
* rows are HEARTBEAT_SECONDS apart, which is under COVERAGE_MAX_GAP_SECONDS
  (30 min). A wider spacing is missing coverage and disqualifies the night —
  the agent not looking, not a flat reading;
* the device drops to standby BEFORE midnight and keeps reporting. assess_night
  reads the last row before 01:00 as the value in force at 01:00, so an evening
  at 120 W followed by silence makes the night read as IN USE at 120 W however
  flat the rest of it is;
* the overnight spread stays inside PLATEAU_SPREAD_FRACTION. The jitter here is
  deliberately smaller than that and is applied to the ACTIVE hours only, so a
  night is exactly as flat as a real heartbeat would leave it.
"""
import random

# 15 minutes, matching observer.NUMERIC_MAX_GAP_SECONDS — the heartbeat that
# makes a plateau visible in a record that otherwise only writes on change.
# Half of COVERAGE_MAX_GAP_SECONDS, so a single dropped publish is survivable.
HEARTBEAT_SECONDS = 900

# When the simulated household uses these devices. Outside this the device is
# idling, which includes the whole of the detector's 01:00-05:00 window.
ACTIVE_FROM_HOUR = 18
ACTIVE_TO_HOUR = 23

# The late-evening use that makes `desk` a candidate replay must refuse. Inside
# the night window and AFTER the cutting trigger, so the guard at 01:00 passes
# on a device that is idle at that instant and is then used anyway.
LATE_FROM_HOUR = 2
LATE_TO_HOUR = 3

DEVICES = {
    "tv": {
        "name": "Lounge TV",
        "sensor": "sensor.lounge_tv_power_draw",
        "switch": "switch.lounge_tv_power",
        "power_topic": "test/tv_lounge/power/state",
        "switch_topic": "test/tv_lounge/switch/state",
        "unit": "W",
        # Watts per reported unit — standby.power_scale's answer for this
        # sensor, and the number every figure below is stated in WATTS despite.
        "scale": 1.0,
        "decimals": 1,
        "standby_w": 12.0,
        "active_w": (85.0, 140.0),
        "late_nights": 0,
        "expect": "proposed — a clean standby load",
    },
    "desk": {
        "name": "Desk setup",
        "sensor": "sensor.desk_setup_power_draw",
        "switch": "switch.desk_setup_power",
        "power_topic": "test/desk/power/state",
        "switch_topic": "test/desk/switch/state",
        "unit": "W",
        "scale": 1.0,
        "decimals": 1,
        "standby_w": 7.5,
        "active_w": (55.0, 95.0),
        # Four of thirty. Under MIN_QUALIFYING_FRACTION's 20% budget, so the
        # detector still raises it; MAX_WRONG_NIGHTS is 0, so replay does not.
        "late_nights": 4,
        "late_w": 60.0,
        "expect": "graded, then withheld — replay finds 4 nights it would have interrupted",
    },
    "rack": {
        "name": "Server rack",
        "sensor": "sensor.server_rack_power_draw",
        "switch": "switch.server_rack_power",
        "power_topic": "test/rack/power/state",
        "switch_topic": "test/rack/switch/state",
        "unit": "kW",
        "scale": 1000.0,
        "decimals": 4,
        "standby_w": 12.0,
        "active_w": (60.0, 110.0),
        "late_nights": 0,
        "expect": "proposed — same estimate as `tv`, or the unit line is broken",
    },
}


def reported(device: dict, watts: float) -> str:
    """Watts as this sensor would report them, in ITS units.

    The one conversion in this file, and it is the line WP5's docstring calls a
    factor of a thousand: everything above is stated in watts because a person
    reads watts, and Home Assistant only ever sees this.
    """
    return f"{watts / device['scale']:.{device['decimals']}f}"


def watts_at(device: dict, hour: int, minute: int, late_night: bool = False,
             jitter: random.Random = None) -> float:
    """What this device is drawing at a local wall time, in watts.

    One function for both scripts: the planter walks it over thirty days of
    fifteen-minute slots, the simulator asks it what now looks like.
    """
    if late_night and LATE_FROM_HOUR <= hour < LATE_TO_HOUR:
        return device["late_w"]
    if ACTIVE_FROM_HOUR <= hour < ACTIVE_TO_HOUR:
        low, high = device["active_w"]
        if jitter is None:
            return round((low + high) / 2, 1)
        return round(jitter.uniform(low, high), 1)
    # Idle, and FLAT. Not jittered even when a generator is available: the
    # plateau is the claim, and noise here would be noise in the one
    # measurement every number downstream is taken from.
    return device["standby_w"]


def late_dates(device: dict, days: list) -> set:
    """Which of the planted days get the 02:00 use, spread through the window
    rather than clustered.

    Spread deliberately: four bad nights in a row at the old end of the record
    would be four nights the agent might never have seen, and the case this
    device exists for is a rule that would have interrupted somebody LAST week
    as much as last month.
    """
    count = device.get("late_nights") or 0
    if not count or not days:
        return set()
    step = max(1, len(days) // count)
    return {days[i] for i in range(0, len(days), step)[:count]}
