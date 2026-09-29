#!/usr/bin/env python3
"""Drive the MQTT test entities as if someone were living in the house.

Runs until stopped. Publishes to the entities' STATE topics through Home
Assistant's own mqtt.publish service, so it needs no broker credentials and no
MQTT client library — only the SUPERVISOR_TOKEN this environment already has.

State topics rather than command topics, because that is what a real device
reports on, and because the motion and temperature entities have no command
topic at all. The four controllable entities were configured optimistic, so
publishing state is what actually moves them.

The same scenario generator backs this and plant_test_history.py: the history
the agent backfills and the events it watches live describe one household.
Everything published here is timestamped now, by definition — which is why the
planting script exists for anything that needs to look like last Tuesday.

--speed skips the boring parts rather than speeding up the clock, which is not
the same thing: see the comment on PRESERVE_SECONDS.

Usage:
    python3 scripts/simulate_test_devices.py                 # idle gaps compressed 20x
    python3 scripts/simulate_test_devices.py --speed 1       # real time, nothing compressed
    python3 scripts/simulate_test_devices.py --check         # verify payloads and exit
"""
import argparse
import json
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario

HA_API = os.environ.get("HA_API_URL", "http://supervisor/core/api")

# Only these topics, only these entities. The script refuses to publish
# anywhere else — a simulator with a typo in a topic is indistinguishable from
# a real device misbehaving, and this one runs unattended.
STATE_TOPICS = {
    scenario.LIGHT: "test/kitchen_light/state",
    scenario.SWITCH: "test/lr_switch/state",
    scenario.BLIND: "test/bedroom_blind/state",
    scenario.THERMOSTAT: "test/thermostat/mode",
    scenario.MOTION: "test/motion/state",
    scenario.TEMPERATURE: "test/temp/state",
}

# Home Assistant's state vocabulary is not the wire format. on/off go out as
# ON/OFF (the MQTT integration's defaults), while covers, climate modes and
# sensor readings travel as themselves.
_ON_OFF = {"on": "ON", "off": "OFF"}

# Intervals shorter than this are played at their real length, however high
# --speed is; only the idle stretches between them are compressed.
#
# Scaling the whole clock would be much simpler and would produce garbage. The
# record stores how long each state LASTED, and those durations are what
# on-fraction and mean-duration are computed from — so a light "on" for two
# seconds instead of five minutes is not a fast simulation, it is a wrong one.
# Worse, core/observer.py debounces anything under MIN_INTERVAL_SECONDS as
# flapping, so a compressed motion pulse is not merely distorted, it is
# discarded: at 60x a 45-second pulse arrives 0.75 seconds long and never
# reaches the record at all.
#
# So a motion burst still takes its real 20-90 seconds, a light still stays on
# for minutes, and the two hours of nothing in between become a few seconds.
PRESERVE_SECONDS = 120

# How often the temperature sensor reports, in REAL seconds. It is deliberately
# not on the scenario's timeline: a continuous stream has no idle gaps, so
# compressing it is impossible without either shrinking the intervals (which
# falsifies every duration and trips the debounce) or letting its cadence set
# the pace of everything else (which means a simulated day takes a real day).
# So the sensor ticks on the wall clock and the CURVE is sampled at whatever
# simulated moment the run has reached — real intervals, real values, no
# contradiction.
REPORT_SECONDS = 120

_stop = False


def _payload(entity_id, state):
    if entity_id in (scenario.LIGHT, scenario.SWITCH, scenario.MOTION):
        return _ON_OFF.get(state, state)
    return state


def _api(path, body=None):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise SystemExit("SUPERVISOR_TOKEN is not set — run this inside a Home Assistant add-on.")
    request = urllib.request.Request(
        f"{HA_API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def publish(entity_id, state):
    topic = STATE_TOPICS.get(entity_id)
    if not topic:
        raise ValueError(f"{entity_id} is not one of the test entities")
    _api("/services/mqtt/publish", {
        "topic": topic,
        "payload": str(_payload(entity_id, state)),
        # Retained, so the entity keeps its state across a Home Assistant
        # restart the way a real device's last-will would leave it.
        "retain": True,
    })


def _state_of(entity_id):
    try:
        return _api(f"/states/{entity_id}").get("state")
    except urllib.error.HTTPError:
        return None


def check():
    """Publishes a known value to each entity and reads it back.

    The discovery configs are retained on the broker and cannot be read from
    here, so the payload format is verified rather than assumed."""
    print("Checking each entity responds to what this script publishes:\n")
    probes = {
        scenario.LIGHT: "off", scenario.SWITCH: "off", scenario.BLIND: "closed",
        scenario.THERMOSTAT: "off", scenario.MOTION: "off", scenario.TEMPERATURE: "21.0",
    }
    ok = True
    for entity_id, wanted in probes.items():
        publish(entity_id, wanted)
        time.sleep(1.5)
        got = _state_of(entity_id)
        good = (got == wanted)
        ok = ok and good
        print(f"   {'OK  ' if good else 'HUH '} {entity_id:42} published {_payload(entity_id, wanted)!r:8} → state {got!r}")
    if not ok:
        print("\nSomething did not take. The likely cause is a different payload_on/off in that")
        print("entity's MQTT discovery config than the ON/OFF this script sends.")
    return ok


def _emit(entity_id, state) -> int:
    """Publishes one change and logs it; returns 1 if it went out."""
    try:
        publish(entity_id, state)
        print(f"   {datetime.now():%H:%M:%S}  {entity_id.split('.')[1]:26} → {state}")
        return 1
    except Exception as e:
        print(f"   {datetime.now():%H:%M:%S}  publish failed: {e}")
        return 0


def run(args):
    if args.speed == 1:
        print("Simulating the six test entities in real time. Ctrl-C to stop.\n")
    else:
        print(f"Simulating the six test entities, idle gaps compressed {args.speed:g}x.")
        print(f"Anything shorter than {PRESERVE_SECONDS}s plays at its real length, so the durations")
        print(f"the agent records stay true. Temperature reports every {args.report:g}s. Ctrl-C to stop.\n")
    published = 0
    # A day at a time, replayed at speed. Regenerating per day rather than
    # once keeps a long run from holding a week of events in memory, and lets
    # each simulated day differ from the last.
    day = 0
    while not _stop:
        now = time.time()
        # No temperature on the timeline: it is reported below, on the wall
        # clock, at whatever the simulated moment happens to be.
        events = scenario.generate(now, now + 86400, seed=args.seed + day,
                                   temperature_interval=None)
        started = time.monotonic()
        previous_ts, due, next_report = now, 0.0, 0.0
        rng = random.Random(args.seed + day)
        if events:
            wait = (events[0][1] - now) / args.speed
            print(f"   next change in ~{wait / 60:.0f} min "
                  f"({events[0][0].split('.')[1]} → {events[0][2]})")

        for index, (entity_id, ts, state) in enumerate(events):
            if _stop:
                break
            gap = ts - previous_ts
            # Only the part of a gap beyond PRESERVE_SECONDS is compressed, so
            # short intervals — the ones carrying a duration worth recording —
            # survive intact. The wait before the FIRST event is exempt: it
            # precedes every recorded state, so it is not a duration of
            # anything and there is nothing to falsify by skipping it.
            step = gap / args.speed if index == 0 else (
                gap if gap <= PRESERVE_SECONDS else
                PRESERVE_SECONDS + (gap - PRESERVE_SECONDS) / args.speed
            )
            previous_ts, due = ts, due + step

            while not _stop and time.monotonic() - started < due:
                elapsed = time.monotonic() - started
                if elapsed >= next_report:
                    # Where the simulated clock has reached, interpolated across
                    # the gap currently being waited out.
                    fraction = 1.0 if step <= 0 else max(0.0, min(1.0, (elapsed - (due - step)) / step))
                    simulated = ts - gap * (1.0 - fraction)
                    published += _emit(scenario.TEMPERATURE, scenario.temperature_at(simulated, rng))
                    next_report = elapsed + args.report
                    continue
                time.sleep(max(0.05, min(0.5, due - (time.monotonic() - started),
                                         next_report - elapsed)))

            if _stop:
                break
            published += _emit(entity_id, state)
        day += 1
    print(f"\nStopped after publishing {published} state change(s).")


def main():
    # Line buffered, because stdout is fully buffered when it is not a terminal
    # and this script's entire purpose is being watched while it runs — piped
    # into a log or a pager, nothing would appear until it exited.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--speed", type=float, default=20.0,
                        help="how much to compress IDLE time (default 20; 1 for real time). "
                             f"Intervals under {PRESERVE_SECONDS}s always play at real length.")
    parser.add_argument("--report", type=float, default=REPORT_SECONDS,
                        help=f"how often the temperature sensor reports, in real seconds "
                             f"(default {REPORT_SECONDS})")
    parser.add_argument("--seed", type=int, default=20260824, help="scenario seed")
    parser.add_argument("--check", action="store_true",
                        help="verify each entity responds to this script's payloads, then exit")
    args = parser.parse_args()

    def _handle(signum, frame):
        global _stop
        _stop = True
        print("\nStopping…")

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    if args.check:
        raise SystemExit(0 if check() else 1)
    run(args)


if __name__ == "__main__":
    main()
