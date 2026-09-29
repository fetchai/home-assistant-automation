#!/usr/bin/env python3
"""Create the analyst test devices in Home Assistant, and let them live out
today at the wall clock.

The companion to plant_analyst.py, as simulate_schedule.py is to
plant_schedule.py, and it announces its own devices for that script's reason:
the set can be rebuilt from the repo rather than from whatever is retained on
the broker.

The entities have to EXIST for more than the panel's sake. core/auditor.py's
C-4 refuses a config that names an entity id missing from the registry
snapshot, and the gate's test-create refuses it again at propose time — so an
analyst that correctly proposes "motion → light.test_hallway_light" against a
light nobody announced is refused, correctly, and the log says why.

  --announce   publishes the MQTT discovery configs, retained, once.
  --retire     clears them, which removes the entities.
  --check      publishes each entity's state as of now, reads it back, exits.
  (no flag)    plays today's remaining events at their real times, then the
               next day's, until stopped.

THE DAY IS THE PLANTER'S DAY
----------------------------
Events come from `analyst_devices.day_events` under `day_rng(seed, day)` — the
function and the per-day seed plant_analyst.py uses — so today, played live,
is the day the planter would have written for it had it planted today. Played
at the wall clock, not compressed: simulate_test_devices.py's docstring has
the argument, and the short one is that the record stores how long each state
lasted, so a light that was on for four seconds instead of four minutes is not
a faster simulation, it is a wrong one.

Everything goes through Home Assistant's own mqtt.publish service, as the
other simulators do: no broker credentials, only the SUPERVISOR_TOKEN any
add-on shell already has. States are retained, for simulate_schedule.py's
reason.

Usage:
    python3 scripts/simulate_analyst.py --announce
    python3 scripts/simulate_analyst.py --check
    python3 scripts/simulate_analyst.py
    python3 scripts/simulate_analyst.py --retire
"""
import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyst_devices as devices

HA_API = os.environ.get("HA_API_URL", "http://supervisor/core/api")
DISCOVERY_PREFIX = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")
MANUFACTURER = "CEELABS"
MODEL = "Analyst test device"

# Must match plant_analyst.py's default, or today is a different household
# from the thirty days before it.
DEFAULT_SEED = 20260921

_PAYLOAD = {"on": "ON", "off": "OFF"}

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
    _api("/services/mqtt/publish", {"topic": topic, "payload": payload, "retain": True})


def _topics():
    """entity_id → its state topic, across all four devices."""
    return {entity["entity_id"]: entity["topic"]
            for device in devices.DEVICES.values()
            for entity in device["entities"].values()}


def _config(device: dict, key: str, entity: dict) -> dict:
    """One discovery config. The entity id falls out of device name plus
    entity name — "Test hallway" + "Light" is `light.test_hallway_light` —
    which is why analyst_devices.py states both and the ids together."""
    config = {
        "name": entity["name"],
        "unique_id": f"{device['unique']}_{key}",
        "state_topic": entity["topic"],
        "payload_on": "ON",
        "payload_off": "OFF",
        "device": {"identifiers": [device["unique"]], "name": device["name"],
                   "manufacturer": MANUFACTURER, "model": MODEL},
    }
    if entity["component"] == "binary_sensor":
        config["device_class"] = entity.get("device_class", "motion")
    else:
        # Commandable, and not optimistic — simulate_schedule.py's reason: what
        # Home Assistant shows is what this script last reported, so an
        # automation the user approves visibly does NOT move these until the
        # simulator (standing in for the device) says so. Nothing here listens
        # on the command topic; an approved automation's effect is in the
        # automation trace, not in these states.
        config["command_topic"] = entity["topic"].replace("/state", "/set")
        config["optimistic"] = False
    return config


def announce(retire: bool = False) -> int:
    count = 0
    for device in devices.DEVICES.values():
        for key, entity in device["entities"].items():
            topic = f"{DISCOVERY_PREFIX}/{entity['component']}/{device['unique']}/{key}/config"
            _publish(topic, "" if retire else json.dumps(_config(device, key, entity)))
            count += 1
            print(f"   {'retired' if retire else 'announced'} {entity['entity_id']}")
    if not retire:
        print("\nGive Home Assistant a few seconds, then: simulate_analyst.py --check")
    return count


def _state_at(now_ts: int, seed: int) -> dict:
    """What each entity is doing at `now_ts`, by replaying today (and the tail
    of yesterday, for a light still on past midnight) up to it. Off when
    nothing has happened yet."""
    today = date.fromtimestamp(now_ts)
    state = {entity_id: "off" for entity_id in devices.ENTITY_IDS}
    for day in (today - timedelta(days=1), today):
        for ts, entity_id, value in devices.day_events(day, devices.day_rng(seed, day)):
            if ts <= now_ts:
                state[entity_id] = value
    return state


def _check(seed: int) -> bool:
    topics = _topics()
    for entity_id, value in _state_at(int(time.time()), seed).items():
        _publish(topics[entity_id], _PAYLOAD[value])
    time.sleep(2)
    ok = True
    for entity_id in devices.ENTITY_IDS:
        try:
            state = _api(f"/states/{entity_id}").get("state")
        except urllib.error.HTTPError:
            state = None
        good = state in ("on", "off")
        ok = ok and good
        print(f"   {'OK  ' if good else 'HUH '} {entity_id:40} → {state!r}")
    if not ok:
        print("\nAn entity that is missing has not been announced — run --announce first.")
    return ok


def _sleep_until(ts: int) -> bool:
    """Sleeps in one-second steps so SIGTERM is honoured. False if stopped."""
    while not _stopping:
        remaining = ts - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(1.0, remaining))
    return False


def _play(seed: int):
    topics = _topics()
    day = date.today()
    now = int(time.time())
    # Start from the right state, so the first live event is a real change.
    for entity_id, value in _state_at(now, seed).items():
        _publish(topics[entity_id], _PAYLOAD[value])
    while not _stopping:
        pending = [e for e in devices.day_events(day, devices.day_rng(seed, day))
                   if e[0] > now]
        print(f"{day}: {len(pending)} event(s) still to play")
        for ts, entity_id, value in pending:
            if not _sleep_until(ts):
                return
            try:
                _publish(topics[entity_id], _PAYLOAD[value])
                print(f"   {datetime.fromtimestamp(ts):%H:%M:%S}  {entity_id:40} {value}")
            except (urllib.error.URLError, urllib.error.HTTPError) as error:
                print(f"   {datetime.fromtimestamp(ts):%H:%M:%S}  {entity_id}: publish failed — {error}")
        day += timedelta(days=1)
        now = 0


def _stop(_signum, _frame):
    global _stopping
    _stopping = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--announce", action="store_true",
                        help="publish the MQTT discovery configs and exit")
    parser.add_argument("--retire", action="store_true",
                        help="clear those configs, removing the entities, and exit")
    parser.add_argument("--check", action="store_true",
                        help="publish the current states, read them back, exit")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    if args.announce or args.retire:
        announce(retire=args.retire)
        return 0
    if args.check:
        return 0 if _check(args.seed) else 1

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print("playing the analyst test devices at the wall clock — Ctrl-C to stop\n")
    _play(args.seed)
    print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
