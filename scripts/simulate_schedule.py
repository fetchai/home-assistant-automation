#!/usr/bin/env python3
"""Create the schedule test devices in Home Assistant, and keep them reporting.

The companion to plant_schedule.py: that one writes the ninety days the
detector judges, this one makes the entities real enough for that evidence to
be read. Both take the same profiles from schedule_devices.py, so the history
the agent was judged on and the states it watches live describe one household.

Two jobs, and --announce is the one its neighbours do not have:

  --announce   publishes the MQTT DISCOVERY configs, once, which is what
               creates `sensor.*_power_draw` and `switch.*_power` and — the
               part that matters — puts them on ONE device in the entity
               registry. `power.controllable_switch` pairs a sensor to a
               switch by shared device_id and pairs nothing when a device
               offers more or fewer than one controllable sibling, so a
               discovery config without a shared `device` block produces two
               entities the detector will never connect. The other test
               devices in this directory were announced by hand and their
               configs live only on the broker; this one is in the repo so the
               set can be rebuilt.
  (no flag)    publishes state, every --interval seconds, until stopped.

Both go through Home Assistant's own mqtt.publish service, exactly as
simulate_standby.py does and for the same reason: no broker credentials, no
MQTT client library, only the SUPERVISOR_TOKEN any add-on shell already has.

RETAINED, BOTH OF THEM
----------------------
A discovery config has to be retained or the entities vanish on the next Home
Assistant restart. A state publish is retained for the reason
simulate_test_devices.py gives: a real device's last will leaves its state
behind, and an entity that goes `unknown` on every restart is one whose UNIT
disappears with it — which is exactly the case that stopped a plug being read
at all until `power_scale` learned to fall back to the registry.

THE CLOCK IS THE REAL ONE
-------------------------
Values follow the local wall clock through `schedule_devices.watts_at`, so the
device is idle when its household is out and in use the rest of the time. That
means a run started on a Sunday afternoon publishes an in-use device for hours,
which is correct and is not evidence of anything: the window is judged on the
record, and the record is what plant_schedule.py wrote.

Usage:
    python3 scripts/simulate_schedule.py --announce      # once, creates the entities
    python3 scripts/simulate_schedule.py --check         # one round, then exit
    python3 scripts/simulate_schedule.py                 # every 60s until stopped
    python3 scripts/simulate_schedule.py --retire        # remove the entities again
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
import schedule_devices as devices

HA_API = os.environ.get("HA_API_URL", "http://supervisor/core/api")

# Home Assistant's default MQTT discovery prefix. Overridable because a home
# that changed it in the MQTT integration's options would otherwise get
# entities nothing ever picks up, with no error anywhere to say why.
DISCOVERY_PREFIX = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")

# What the registry will show as the maker. Only so these three are obvious in
# a device list next to a household's real plugs.
MANUFACTURER = "CEELABS"
MODEL = "Schedule test device"

# Set by the signal handler, read by the loop. A simulator that ignores SIGTERM
# has to be killed, and a killed one leaves its last publish half-done rather
# than at a value that means something.
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
    """One retained publish."""
    _api("/services/mqtt/publish",
         {"topic": topic, "payload": payload, "retain": True})


def _device_block(device: dict) -> dict:
    """The block that makes the sensor and the switch ONE device.

    The whole reason --announce exists. `power.controllable_switch` looks for
    exactly one controllable entity sharing this sensor's device_id, so these
    identifiers are what turns two unrelated entities into a pairing the
    detector can act on — and getting them wrong produces a sensor that is
    read, judged, and then discarded for having no switch, with nothing in the
    log about identifiers.
    """
    return {
        "identifiers": [device["unique"]],
        "name": device["name"],
        "manufacturer": MANUFACTURER,
        "model": MODEL,
    }


def _configs(device: dict) -> list:
    """The two discovery configs for one device, as (topic, payload).

    The entity ids fall out of the device name and the entity name — "Study
    workstation" plus "Power draw" is `sensor.study_workstation_power_draw` —
    which is why the names here are not free text: schedule_devices.py states
    those ids, plant_schedule.py writes the record under them, and the panel's
    watchlist is set against them.
    """
    block = _device_block(device)
    sensor = {
        "name": "Power draw",
        "unique_id": f"{device['unique']}_power",
        "state_topic": device["power_topic"],
        "device_class": "power",
        "state_class": "measurement",
        "unit_of_measurement": device["unit"],
        "device": block,
    }
    switch = {
        "name": "Power",
        "unique_id": f"{device['unique']}_switch",
        "state_topic": device["switch_topic"],
        "command_topic": device["switch_topic"].replace("/state", "/set"),
        "payload_on": "ON",
        "payload_off": "OFF",
        # Not optimistic: the switch reports its own state on the state topic,
        # so what Home Assistant shows is what this script last published
        # rather than what it last commanded. An optimistic switch would show
        # OFF the moment an automation acted on it whether or not anything
        # happened, which is the one thing a replay is supposed to be able to
        # contradict.
        "optimistic": False,
        "device": block,
    }
    return [
        (f"{DISCOVERY_PREFIX}/sensor/{device['unique']}/power/config", sensor),
        (f"{DISCOVERY_PREFIX}/switch/{device['unique']}/power/config", switch),
    ]


def announce(chosen: list, retire: bool = False) -> int:
    """Publishes (or clears) the discovery configs. Idempotent either way —
    the broker keeps one retained message per topic, so re-announcing replaces
    rather than duplicates, and an empty payload is MQTT's own way of saying
    the device is gone."""
    count = 0
    for name in chosen:
        device = devices.DEVICES[name]
        for topic, config in _configs(device):
            _publish(topic, "" if retire else json.dumps(config))
            count += 1
            print(f"   {'retired' if retire else 'announced'} {topic}")
    if not retire:
        print("\nGive Home Assistant a few seconds, then check the entities exist:")
        for name in chosen:
            device = devices.DEVICES[name]
            print(f"   {device['sensor']}  +  {device['switch']}")
        print("\nThey are NOT watched yet — add each sensor AND its switch to the "
              "watchlist in the panel.")
    return count


def _round(chosen: list, jitter: random.Random, verbose: bool = True) -> int:
    now = datetime.now()
    sent = 0
    for name in chosen:
        device = devices.DEVICES[name]
        watts = devices.watts_at(device, now.weekday(), now.hour, now.minute,
                                 jitter=jitter)
        payload = devices.reported(device, watts)
        try:
            _publish(device["power_topic"], payload)
            # The switch is what a suggestion would act on, and it is ON
            # whenever the device is fed at all: a device idling at 9 W is one
            # that is "off" and still drawing, which is the whole premise.
            _publish(device["switch_topic"], "ON")
        except (urllib.error.URLError, urllib.error.HTTPError) as error:
            print(f"   {now:%H:%M:%S}  {name}: publish failed — {error}")
            continue
        sent += 1
        if verbose:
            away = devices.is_away(device, now.weekday(), now.hour, now.minute)
            print(f"   {now:%H:%M:%S}  {name:8} {payload:>9} {device['unit']:2} "
                  f"({'away' if away else 'in use'})")
    return sent


def _check(chosen: list) -> bool:
    """One round, then read each entity back. Verifies the payload arrived
    rather than assuming it: a discovery config is retained on the broker and
    cannot be read from here, so the only proof the two agree is a state that
    came back."""
    _round(chosen, random.Random(0))
    time.sleep(2)
    ok = True
    for name in chosen:
        device = devices.DEVICES[name]
        for entity_id in (device["sensor"], device["switch"]):
            try:
                state = _api(f"/states/{entity_id}").get("state")
            except urllib.error.HTTPError:
                state = None
            good = state not in (None, "unknown", "unavailable")
            ok = ok and good
            print(f"   {'OK  ' if good else 'HUH '} {entity_id:42} → {state!r}")
    if not ok:
        print("\nAn entity that is missing has not been announced — run --announce first.")
    return ok


def _stop(_signum, _frame):
    global _stopping
    _stopping = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", choices=sorted(devices.DEVICES) + ["all"],
                        default="all")
    parser.add_argument("--announce", action="store_true",
                        help="publish the MQTT discovery configs and exit")
    parser.add_argument("--retire", action="store_true",
                        help="clear those configs, removing the entities, and exit")
    parser.add_argument("--check", action="store_true",
                        help="one round, read every entity back, then exit")
    parser.add_argument("--interval", type=int, default=60,
                        help="seconds between rounds (default 60)")
    parser.add_argument("--seed", type=int, default=20260917)
    args = parser.parse_args()

    chosen = sorted(devices.DEVICES) if args.device == "all" else [args.device]

    if args.announce or args.retire:
        announce(chosen, retire=args.retire)
        return 0
    if args.check:
        return 0 if _check(chosen) else 1

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    jitter = random.Random(args.seed)
    print(f"publishing {len(chosen)} device(s) every {args.interval}s — Ctrl-C to stop\n")
    while not _stopping:
        _round(chosen, jitter)
        for _tick in range(args.interval):
            if _stopping:
                break
            time.sleep(1)
    print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
