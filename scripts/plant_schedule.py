#!/usr/bin/env python3
"""Plant ninety days of power history for the schedule test devices, directly
into the agent's own behavioural record.

WHY THIS WRITES TO THE AGENT'S STORE
-------------------------------------
plant_standby.py's docstring makes the whole argument and it applies here
harder. `core/detectors/schedule.py` reads LOOKBACK_DAYS (90) and needs
MIN_OCCURRENCES (8) sightings of each WEEKDAY, so it wants at least eight weeks
before it can say anything at all. Home Assistant's recorder purges at
purge_keep_days — 10 by default — and core/backfill.py will not reach past
MAX_BACKFILL_DAYS for the same reason, so the real path physically cannot
deliver that. Waiting three months for the record to fill on its own is the
honest alternative and is not a test.

What this therefore does NOT prove, stated rather than glossed: that the
listener records these devices correctly, that backfill converts a real
history series correctly, or that the watchlist gate lets them through.
simulate_schedule.py and the panel are what prove those.

It writes source='history' rows, the same source backfill writes, because that
is what they are standing in for.

SAFETY
------
* --dry-run generates everything, runs the agent's OWN detector over it, and
  reports the window it found per device without writing a row.
* --clear removes every event for exactly these three sensors and nothing else.
* Inserts go through record.record_many, so they take the same locks, the same
  retries and the same INSERT OR IGNORE as every other write. Re-running is a
  no-op rather than a double count, and the observation thread keeps working
  while it happens.

WHERE TO RUN IT
---------------
Inside the add-on's own container, where /data is mounted:

    docker cp scripts <container>:/tmp/devscripts
    docker exec <container> sh -c 'cd /tmp/devscripts && python3 plant_schedule.py --dry-run'

Usage:
    python3 scripts/plant_schedule.py --dry-run
    python3 scripts/plant_schedule.py
    python3 scripts/plant_schedule.py --days 90 --device study
    python3 scripts/plant_schedule.py --clear
"""
import argparse
import os
import random
import sqlite3
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule_devices as devices

# plant_standby.py's own resolution, repeated: two paths, because the container
# copies agent_home/ to /app and a checkout has it beside this script.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (os.environ.get("AGENT_HOME"), "/app",
                   os.path.join(os.path.dirname(_HERE),
                                "fetchai-automation-agent", "agent_home")):
    if _candidate and os.path.isdir(os.path.join(_candidate, "core")):
        sys.path.insert(0, _candidate)
        break
else:
    sys.exit("could not find agent_home — run this inside the add-on container, "
             "or set AGENT_HOME to a checkout's agent_home directory")

from core import record                      # noqa: E402
from core import window                      # noqa: E402
from core.detectors import schedule          # noqa: E402

# Slots per day at the heartbeat cadence. 96 at fifteen minutes.
_SLOTS_PER_DAY = 86400 // devices.HEARTBEAT_SECONDS


def _epoch(day: date, hour: int, minute: int) -> int:
    """A local wall time as an epoch second.

    Local because an away window is a statement about the household's clock —
    the same reason `window.bounds` is local — and through mktime with
    tm_isdst=-1 so the C library resolves a DST boundary rather than this
    script pretending to know the rules.
    """
    return int(time.mktime((day.year, day.month, day.day, hour, minute, 0, 0, 0, -1)))


def _series(device: dict, days: list, jitter: random.Random) -> list:
    """One device's whole planted series as record_many rows, oldest first.

    Returns (ts, state, prev_state, prev_duration) tuples — core/backfill.py's
    shape, because these rows are standing in for the ones it would have
    written.
    """
    rows = []
    prev_state, prev_ts = None, None
    for day in days:
        for slot in range(_SLOTS_PER_DAY):
            seconds = slot * devices.HEARTBEAT_SECONDS
            hour, minute = seconds // 3600, (seconds % 3600) // 60
            watts = devices.watts_at(device, day.weekday(), hour, minute, jitter=jitter)
            ts = _epoch(day, hour, minute)
            state = devices.reported(device, watts)
            rows.append((ts, state, prev_state,
                         None if prev_ts is None else ts - prev_ts))
            prev_state, prev_ts = state, ts
    return rows


def _as_record_rows(rows: list) -> list:
    """The same series as core/record.py hands it back on a read, so the
    detector can be run over it before anything is stored."""
    return [{"ts": ts, "state": state, "prev_state": prev, "prev_duration": duration,
             "source": "history"}
            for ts, state, prev, duration in rows]


def _verdict(device: dict, rows: list, now: int) -> list:
    """What the agent's own detector makes of this seed, as report lines.

    The whole point of --dry-run. Asserting here that these ninety days ought
    to produce a particular window would be this script's opinion, and
    `schedule.find_candidate`'s is the only one that decides anything — so
    this prints D1's pick AND the per-weekday stretches the model proposer
    would be shown, which is where a mis-planted edge actually shows up.
    """
    stored = _as_record_rows(rows)
    levels = schedule.device_levels(stored, scale=device["scale"])
    if levels is None:
        return ["NO LEVELS — the record never showed two separable levels"]

    start = now - schedule.LOOKBACK_DAYS * 86400
    series = schedule.intervals(stored, start, now, levels["active_floor"],
                               scale=device["scale"])
    grid = schedule.weekly_grid(series)
    lines = [line for line in schedule.runs_text(grid).splitlines()
             if line.startswith("  ")]

    candidate = schedule.find_candidate(device["sensor"], stored, device["switch"],
                                        now=now, scale=device["scale"])
    if not candidate:
        lines.insert(0, "D1: no candidate")
        return lines
    blob = candidate["evidence"]["schedule"]
    spec = window.spec(blob["start"], blob["end"], blob["days"])
    lines.insert(0, f"D1: {window.label(spec)}")
    return lines


def _clear() -> int:
    """Removes every event for exactly these three sensors.

    Scoped by entity rather than by a marker file, plant_standby.py's own
    reasoning: these entity ids belong to test devices that exist for this
    script, so there is nothing of anyone else's to lose, and a marker would be
    one more thing that can disagree with the store.
    """
    removed = 0
    with sqlite3.connect(record.DB_PATH, timeout=30) as conn:
        for device in devices.DEVICES.values():
            cursor = conn.execute(
                "DELETE FROM events WHERE entity ="
                " (SELECT id FROM entities WHERE entity_id = ?)",
                (device["sensor"],))
            removed += cursor.rowcount or 0
        conn.commit()
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=schedule.LOOKBACK_DAYS,
                        help=f"how many days to plant (default {schedule.LOOKBACK_DAYS}; "
                             f"MIN_OCCURRENCES is {schedule.MIN_OCCURRENCES} sightings "
                             "of each WEEKDAY, so under about 60 nothing qualifies)")
    parser.add_argument("--device", choices=sorted(devices.DEVICES) + ["all"],
                        default="all")
    parser.add_argument("--dry-run", action="store_true",
                        help="generate and judge, write nothing")
    parser.add_argument("--clear", action="store_true",
                        help="remove every planted row for these sensors")
    parser.add_argument("--seed", type=int, default=20260917,
                        help="jitter seed, so two runs plant the same house")
    args = parser.parse_args()

    if args.clear:
        print(f"removed {_clear()} event(s) for {len(devices.DEVICES)} sensor(s)")
        return 0

    # Ending YESTERDAY. Today is still being lived through — the live simulator
    # is writing into it — and a part-day at the end of the window is a set of
    # half-read cells rather than observations.
    last = date.today() - timedelta(days=1)
    days = [last - timedelta(days=offset) for offset in range(args.days - 1, -1, -1)]
    now = _epoch(last + timedelta(days=1), 0, 0)

    chosen = sorted(devices.DEVICES) if args.device == "all" else [args.device]
    print(f"{'planning' if args.dry_run else 'planting'} {args.days} day(s), "
          f"{days[0]} to {days[-1]}\n")

    for name in chosen:
        device = devices.DEVICES[name]
        rows = _series(device, days, random.Random(args.seed + len(name)))
        print(f"  {name:8} {device['sensor']}")
        print(f"           {len(rows)} rows, {device['idle_w']} W while away, "
              f"reported in {device['unit']}")
        print(f"           expected: {device['expect']}")
        for line in _verdict(device, rows, now):
            print(f"           {line}")
        if not args.dry_run:
            stored = record.record_many(device["sensor"], rows, source="history")
            print(f"           stored:   {stored} new row(s)")
        print()

    if args.dry_run:
        print("nothing was written — drop --dry-run to plant it")
        return 0

    print("planted. Now:\n"
          "  1. python3 scripts/simulate_schedule.py --announce   (once, to create the entities)\n"
          "  2. add these sensors AND their switches to the watchlist in the panel\n"
          "  3. python3 scripts/compare_proposers.py, or bench_scheduler_prompt.py for the\n"
          "     same three shapes offline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
