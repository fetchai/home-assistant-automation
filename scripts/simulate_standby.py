#!/usr/bin/env python3
"""Keep the standby test devices reporting power, as if they were plugged in.

The companion to plant_standby.py: that one writes the thirty nights the
detector judges, this one keeps the entities alive from now on. Both read the
same profiles from standby_devices.py, so the history the agent was judged on
and the states it watches live describe one household — a device whose planted
nights sit at 12 W and whose live feed sits at 20 W is a device the detector
and the panel will disagree about.

Publishes to the entities' STATE topics through Home Assistant's own
mqtt.publish service, exactly as simulate_test_devices.py does and for the same
reason: no broker credentials, no MQTT client library, only the
SUPERVISOR_TOKEN any add-on shell already has.

WHY THIS IS NEEDED AT ALL, GIVEN THE HISTORY IS PLANTED
--------------------------------------------------------
Three things read the LIVE state rather than the record, and all three are on
the path between a planted candidate and a proposal reaching anybody:

* standby.power_scale reads the sensor's unit from its state attributes (and,
  since a fix in WP4, from the registry row too, because Home Assistant drops
  the unit while an entity is unavailable). A sensor that has never reported
  has no unit, and an entity with no unit is skipped before its thirty days are
  ever read;
* standby.controllable_switch pairs the sensor to a switch on the same device
  through the entity registry, which needs both entities to exist;
* core/observation.py only records what it is watching, and the watchlist is
  resolved against the registry every fifteen minutes.

So the planted rows are the evidence, and this script is what makes the
entities real enough for that evidence to be read.

THE CLOCK IS THE REAL ONE
-------------------------
Values follow the local wall clock: standby overnight, in use in the evening.
That means the interesting hours happen at the interesting hours, and a run
started at three in the afternoon publishes an idle device for hours before
anything resembling a night. --profile overrides that when the point is to see
a particular value arrive rather than to wait for it — useful for checking that
a topic works, and useless for producing evidence, since a night is judged on
the record and the record is what plant_standby.py wrote.

Usage:
    python3 scripts/simulate_standby.py --check        # one round, then exit
    python3 scripts/simulate_standby.py                # every 60s until stopped
    python3 scripts/simulate_standby.py --interval 300
    python3 scripts/simulate_standby.py --profile standby --once
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
import standby_devices as devices

HA_API = os.environ.get("HA_API_URL", "http://supervisor/core/api")

# Set by the signal handler, read by the loop. A simulator that ignores SIGTERM
# is one that has to be killed, and a killed one leaves its last publish
# half-done rather than at a value that means something.
_stopping = False


def _api(path: str, body=None):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        sys.exit("SUPERVISOR_TOKEN is not set — run this from a Home Assistant add-on shell")
    request = urllib.request.Request(
        f"{HA_API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def _publish(topic: str, payload: str):
    """One retained publish.

    Retained for the reason simulate_test_devices.py gives: a real device's
    last-will leaves its state behind, and an entity that goes `unknown` on
    every Home Assistant restart is one whose unit disappears with it — which
    is exactly the case that stopped a plug being read at all before WP4 taught
    power_scale to fall back to the registry.
    """
    _api("/services/mqtt/publish", {"topic": topic, "payload": payload, "retain": True})


def _values(device: dict, when: datetime, profile: str, jitter: random.Random):
    """What to publish for one device right now: (watts, reported, switch)."""
    if profile == "standby":
        watts = device["standby_w"]
    elif profile == "active":
        low, high = device["active_w"]
        watts = round(jitter.uniform(low, high), 1)
    elif profile == "late":
        # The 02:00 use that makes `desk` a rule replay counts as wrong. On a
        # device with no late_w this is just its active level — the profile is
        # about the hour, and only one device has anything to say about it.
        watts = device.get("late_w") or device["active_w"][1]
    else:
        watts = devices.watts_at(device, when.hour, when.minute, jitter=jitter)
    # The switch is what the suggestion would act on, and it is ON whenever the
    # device is drawing anything at all: a standby load is a device that is
    # "off" and still fed, which is the whole premise.
    return watts, devices.reported(device, watts), "ON"


def _round(chosen: list, profile: str, jitter: random.Random, verbose: bool = True) -> int:
    now = datetime.now()
    published = 0
    for name in chosen:
        device = devices.DEVICES[name]
        watts, payload, switch_state = _values(device, now, profile, jitter)
        try:
            _publish(device["power_topic"], payload)
            _publish(device["switch_topic"], switch_state)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            # Logged and carried on. Home Assistant restarting is not this
            # script's problem to solve, and a simulator that exits on the
            # first blip is one nobody can leave running overnight — which is
            # the only way to watch a night happen.
            print(f"  {now:%H:%M:%S} {name:5} publish failed: {e}")
            continue
        published += 1
        if verbose:
            print(f"  {now:%H:%M:%S} {name:5} {payload:>8} {device['unit']}"
                  f"  ({watts:g} W)")
    return published


def _stop(signum, frame):
    global _stopping
    _stopping = True
    print("\nstopping after this round")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=int, default=60,
                        help="seconds between rounds (default 60)")
    parser.add_argument("--device", choices=sorted(devices.DEVICES) + ["all"],
                        default="all")
    parser.add_argument("--profile", choices=("clock", "standby", "active", "late"),
                        default="clock",
                        help="clock follows the local time (default); the others force a value")
    parser.add_argument("--once", action="store_true", help="one round, then exit")
    parser.add_argument("--check", action="store_true",
                        help="one round, then read the states back out of Home Assistant")
    parser.add_argument("--seed", type=int, default=None,
                        help="jitter seed; omit for a different evening every run")
    args = parser.parse_args()

    chosen = sorted(devices.DEVICES) if args.device == "all" else [args.device]
    jitter = random.Random(args.seed)

    if args.check:
        _round(chosen, args.profile, jitter)
        # Read back rather than trust the publish: a retained message on a
        # topic no entity is configured for looks exactly like a successful
        # round from here, and is the failure this flag exists to catch.
        print("\nas Home Assistant sees them:")
        for name in chosen:
            device = devices.DEVICES[name]
            for entity_id in (device["sensor"], device["switch"]):
                try:
                    state = _api(f"/states/{entity_id}")
                    unit = (state.get("attributes") or {}).get("unit_of_measurement", "")
                    print(f"  {entity_id:38} {state.get('state'):>8} {unit}")
                except urllib.error.HTTPError:
                    print(f"  {entity_id:38} NOT FOUND — is its discovery config"
                          f" published and retained?")
        return 0

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print(f"publishing {len(chosen)} device(s) every {args.interval}s, "
          f"profile '{args.profile}' — Ctrl-C to stop")
    rounds = 0
    while not _stopping:
        _round(chosen, args.profile, jitter)
        rounds += 1
        if args.once:
            break
        # Slept in short steps so a Ctrl-C during a five-minute interval is
        # noticed now rather than five minutes from now.
        slept = 0
        while slept < args.interval and not _stopping:
            time.sleep(min(1, args.interval - slept))
            slept += 1
    print(f"{rounds} round(s) published")
    return 0


if __name__ == "__main__":
    sys.exit(main())
