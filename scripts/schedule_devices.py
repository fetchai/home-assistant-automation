#!/usr/bin/env python3
"""The three MQTT schedule test devices, and the week they live through.

Shared by plant_schedule.py and simulate_schedule.py the way standby_devices.py
is shared by its own pair, and for the same reason: the history the detector
judges and the states the agent watches live have to describe one household.

Not run directly.


WHY THESE EXIST WHEN THREE TEST DEVICES ALREADY DO
---------------------------------------------------
The standby devices are idle across the SAME half-hours on every weekday, and
identical weekday runs are the one shape `schedule.schedules` merges outright.
So D1 is already optimal on all of them, the best the model proposer (D2) can
score is a draw, and a comparison run over them cannot tell a better prompt
from a lucky one — which is exactly what the first live run of
`compare_proposers.py` ran into.

`schedules()`' own docstring names the gap these fill:

    Runs are merged across weekdays only when they are IDENTICAL [...] A
    Monday run of 07:00-17:00 and a Tuesday run of 07:30-17:00 are left as
    two, even though their intersection would be a true claim about both days.

Every device below has an intersection worth more than anything D1 can name.
`scripts/bench_scheduler_prompt.py` holds the same three shapes as synthetic
grids, which is the fast way to iterate on the prompt; these are the slow way,
and the only one that also runs the window through `builder.build` and
`replay.run` over real recorded rows.


WHAT EACH DEVICE IS FOR
-----------------------
  study    ragged weekday edges — a different leaving and returning time every
           day of the week. D1 can merge only Mon with Thu, the one identical
           pair, and proposes a two-day window; the five-day intersection is
           worth about twice that. The gap at full size.
  landing  the near miss. Four identical weekdays and a fifth that ends an hour
           early. D1 drops Friday rather than shorten the other four, which is
           the narrower trigger §7.2's decision 2 was weighed against.
  spare    the judgement case, and the only one where the highest-scoring
           window is the WRONG answer. Five office weekdays plus a Saturday the
           household is out even longer. Including Saturday scores higher and
           is what §3 stage D2 exists to decline: the reason the window exists
           is that everybody is at work, and next Saturday they may be home.
           Reported in KILOWATTS, so the factor of a thousand every displayed
           watt figure crosses is exercised here too.


WHAT THE NUMBERS HAVE TO SATISFY
--------------------------------
Every value is chosen against a rule in core/detectors/schedule.py:

* the idle and active levels are far enough apart for `levels.two_levels` to
  separate them, and the idle level sits above `levels.IDLE_FLOOR_WATTS` — a
  device that really switched off has no standing draw to cut;
* rows are HEARTBEAT_SECONDS apart, under `COVERAGE_MAX_GAP_SECONDS` (30 min).
  A wider spacing is a half-hour nobody watched, which is unobserved rather
  than idle, and unobserved cells are the failure this whole detector is most
  exposed to;
* a device is IN USE outside its away window, all the way to local midnight on
  both sides. That is what keeps the away window the only stretch in the grid
  and stops a night-time run — which is standby.py's territory, not this
  detector's — outranking it;
* the away windows land on the half hour, because the grid is cut there and a
  window off the lattice is one none of the validator's checks mean anything
  about.
"""

# 15 minutes, matching observer.NUMERIC_MAX_GAP_SECONDS and standby_devices'
# own heartbeat — half of COVERAGE_MAX_GAP_SECONDS, so one dropped publish
# still leaves the half-hour observed.
HEARTBEAT_SECONDS = 900

# Every away window below is stated as local clock times on the half hour, in
# the grid's own units. Weekdays are Python's: Monday 0 through Sunday 6, the
# numbering core/window.py and the grid both use.
DEVICES = {
    "study": {
        "name": "Study workstation",
        "sensor": "sensor.study_workstation_power_draw",
        "switch": "switch.study_workstation_power",
        "unique": "ceelabs_study_workstation",
        "power_topic": "test/study_workstation/power/state",
        "switch_topic": "test/study_workstation/switch/state",
        "unit": "W",
        # Watts per reported unit — power.power_scale's answer for this
        # sensor, and the number every figure here is stated in WATTS despite.
        "scale": 1.0,
        "decimals": 1,
        "idle_w": 9.0,
        "active_w": (70.0, 130.0),
        "away": {0: ("07:30", "17:00"), 1: ("07:00", "17:30"), 2: ("08:00", "16:30"),
                 3: ("07:30", "17:00"), 4: ("07:00", "15:30")},
        "expect": "D1 finds 07:30-17:00 Mon, Thu; the answer is 08:00-15:30 Mon-Fri",
    },
    "landing": {
        "name": "Landing media hub",
        "sensor": "sensor.landing_media_hub_power_draw",
        "switch": "switch.landing_media_hub_power",
        "unique": "ceelabs_landing_media_hub",
        "power_topic": "test/landing_media_hub/power/state",
        "switch_topic": "test/landing_media_hub/switch/state",
        "unit": "W",
        "scale": 1.0,
        "decimals": 1,
        "idle_w": 11.0,
        "active_w": (60.0, 105.0),
        "away": {0: ("08:00", "17:00"), 1: ("08:00", "17:00"), 2: ("08:00", "17:00"),
                 3: ("08:00", "17:00"), 4: ("08:00", "16:00")},
        "expect": "D1 finds 08:00-17:00 Mon-Thu; the answer is 08:00-16:00 Mon-Fri",
    },
    "spare": {
        "name": "Spare room desk",
        "sensor": "sensor.spare_room_desk_power_draw",
        "switch": "switch.spare_room_desk_power",
        "unique": "ceelabs_spare_room_desk",
        "power_topic": "test/spare_room_desk/power/state",
        "switch_topic": "test/spare_room_desk/switch/state",
        "unit": "kW",
        "scale": 1000.0,
        "decimals": 4,
        "idle_w": 10.0,
        "active_w": (65.0, 115.0),
        "away": {0: ("08:00", "16:30"), 1: ("08:00", "16:30"), 2: ("08:00", "16:30"),
                 3: ("08:00", "16:30"), 4: ("08:00", "16:30"), 5: ("08:00", "20:30")},
        "expect": "D1 finds 08:00-16:30 Mon-Fri; adding Saturday scores higher and is wrong",
    },
}


def reported(device: dict, watts: float) -> str:
    """Watts as this sensor would report them, in ITS units.

    The one conversion in this file, and standby_devices.py's own line: every
    figure above is stated in watts because a person reads watts, and Home
    Assistant only ever sees this.
    """
    return f"{watts / device['scale']:.{device['decimals']}f}"


def _minutes(clock: str) -> int:
    hour, minute = clock.split(":")
    return int(hour) * 60 + int(minute)


def is_away(device: dict, weekday: int, hour: int, minute: int) -> bool:
    """Whether the house is empty at this local wall time on this weekday.

    Half-open on both ends the way `core/window.py` is: a window running to
    17:00 is over AT 17:00, so the 17:00 cell is the first one the device is
    back in use for. Getting that wrong by one cell would move every planted
    edge half an hour and quietly make the expectations above false.
    """
    span = (device.get("away") or {}).get(weekday)
    if not span:
        return False
    return _minutes(span[0]) <= hour * 60 + minute < _minutes(span[1])


def watts_at(device: dict, weekday: int, hour: int, minute: int,
             jitter=None) -> float:
    """What this device is drawing at a local wall time, in watts.

    One function for both scripts: the planter walks it over ninety days of
    heartbeats, the simulator asks it what now looks like.
    """
    if is_away(device, weekday, hour, minute):
        # Idle, and FLAT. Not jittered even when a generator is available: the
        # plateau is the claim, and noise here would be noise in the one
        # measurement every number downstream is taken from.
        return device["idle_w"]
    low, high = device["active_w"]
    if jitter is None:
        return round((low + high) / 2, 1)
    return round(jitter.uniform(low, high), 1)
