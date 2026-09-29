#!/usr/bin/env python3
"""Plant thirty nights of overnight power history for the standby test devices,
directly into the agent's own behavioural record.

WHY THIS ONE WRITES TO THE AGENT'S STORE, WHEN plant_test_history.py REFUSES TO
-------------------------------------------------------------------------------
That script's docstring makes the case against exactly what this one does:
write through Home Assistant's recorder so the data arrives by the real path,
because seeding the agent's database directly proves nothing about the path.
The argument is right and it does not survive contact with this detector's
floors.

core/detectors/standby.py needs MIN_COVERAGE_DAYS (14) of coverage and
MIN_QUALIFYING_NIGHTS (10) inside a LOOKBACK_DAYS (30) window. Home Assistant's
recorder purges at purge_keep_days — 10 by default — and core/backfill.py will
not reach past MAX_BACKFILL_DAYS for the same reason. So the real path
physically cannot deliver a fortnight of nights, let alone a month: rows old
enough to matter would be written, purged, and never read. Waiting a month for
the record to fill on its own is the honest alternative and is not a test.

What this therefore does NOT prove, stated rather than glossed: that the
listener records these devices correctly, that backfill converts a real history
series correctly, or that the watchlist gate lets them through. Those are
plant_test_history.py's and simulate_standby.py's job. This proves what happens
AFTER a record exists — the detector, the builder, the replay, the review pass
and everything downstream of them.

It writes source='history' rows, the same source backfill writes, because that
is what they are standing in for. Never 'statistics': those are bucket means,
and standby.assess_night refuses a night that rests on one.

SAFETY
------
* --dry-run generates everything, runs the agent's OWN detector over it, and
  reports the verdict per device without writing a row. A seed that would not
  have qualified is worth knowing about before it is in the store rather than
  after the review pass silently finds nothing.
* --clear removes every event for exactly these three sensors and nothing else.
* Inserts go through record.record_many, so they take the same locks, the same
  retries and the same INSERT OR IGNORE as every other write. Re-running is a
  no-op rather than a double count, and the observation thread keeps working
  while it happens.

WHERE TO RUN IT
---------------
Inside the add-on's own container: the record lives at /data/record.db, and an
add-on's /data is private to it — a Terminal add-on cannot see it. From the
host:

    docker exec -it addon_<slug>_fetchai-automation-agent python3 - < scripts/plant_standby.py

or copy the scripts directory in and run it there.

Usage:
    python3 scripts/plant_standby.py --dry-run
    python3 scripts/plant_standby.py
    python3 scripts/plant_standby.py --nights 30 --device tv
    python3 scripts/plant_standby.py --clear
"""
import argparse
import os
import random
import sqlite3
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import standby_devices as devices

# The agent's own modules, so the rows are written by the code that owns the
# schema and judged by the code that will judge them for real. Two paths: the
# container copies agent_home/ to /app, a checkout has it beside this script.
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
from core.detectors import standby           # noqa: E402

# Slots per day at the heartbeat cadence. 96 at fifteen minutes.
_SLOTS_PER_DAY = 86400 // devices.HEARTBEAT_SECONDS


def _epoch(day: date, hour: int, minute: int) -> int:
    """A local wall time as an epoch second.

    Local because "night" is a statement about the household's clock — the same
    reason standby.night_bounds is local — and through mktime with tm_isdst=-1
    so the C library resolves a DST boundary rather than this script pretending
    to know the rules.
    """
    return int(time.mktime((day.year, day.month, day.day, hour, minute, 0, 0, 0, -1)))


def _series(device: dict, days: list, jitter: random.Random) -> list:
    """One device's whole planted series as record_many rows, oldest first.

    Returns (ts, state, prev_state, prev_duration) tuples — core/backfill.py's
    shape, because these rows are standing in for the ones it would have
    written.
    """
    late = devices.late_dates(device, days)
    rows = []
    prev_state, prev_ts = None, None
    for day in days:
        for slot in range(_SLOTS_PER_DAY):
            seconds = slot * devices.HEARTBEAT_SECONDS
            hour, minute = seconds // 3600, (seconds % 3600) // 60
            watts = devices.watts_at(device, hour, minute,
                                     late_night=day in late, jitter=jitter)
            ts = _epoch(day, hour, minute)
            state = devices.reported(device, watts)
            rows.append((ts, state, prev_state,
                         None if prev_ts is None else ts - prev_ts))
            prev_state, prev_ts = state, ts
    return rows


def _as_record_rows(rows: list) -> list:
    """The same series as core/record.py hands it back on a read, so the
    detector can be run over it before anything is stored."""
    return [{"ts": ts, "state": state, "prev_state": prev, "prev_duration": dur,
             "source": "history"}
            for ts, state, prev, dur in rows]


def _verdict(device: dict, rows: list, now: int) -> str:
    """What the agent's own detector makes of this seed.

    The whole point of --dry-run: asserting here that thirty flat nights ought
    to qualify would be this script's opinion, and standby.find_candidate's is
    the only one that decides anything.
    """
    candidate = standby.find_candidate(
        device["sensor"], _as_record_rows(rows), device["switch"],
        now=now, scale=device["scale"])
    if not candidate:
        # Which nights failed and why is worth more than "no candidate", and it
        # is one call away.
        nights = standby.night_windows(now - standby.LOOKBACK_DAYS * 86400, now)
        reasons = {}
        for _day, night_start, night_end in nights:
            verdict = standby.assess_night(_as_record_rows(rows), night_start,
                                           night_end, scale=device["scale"])
            key = "qualified" if verdict["qualifies"] else verdict["reason"]
            reasons[key] = reasons.get(key, 0) + 1
        return ("NO CANDIDATE — "
                + ", ".join(f"{name} {count}" for name, count in sorted(reasons.items())))
    evidence = candidate["evidence"]
    return (f"candidate: {evidence['watts']} W on {evidence.get('nights_per_month')} "
            f"nights/month, {evidence['kwh_per_month']} kWh/month")


def _clear() -> int:
    """Removes every event for exactly these three sensors.

    Scoped by entity rather than by a marker file: these entity ids belong to
    test devices that exist for this script, so there is nothing of anyone
    else's to lose, and a marker would be one more thing that can disagree with
    the store. Written straight through sqlite3 because core/record.py exposes
    no delete-by-entity — it prunes by age, which is a different question.
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
    parser.add_argument("--nights", type=int, default=standby.LOOKBACK_DAYS,
                        help=f"how many nights to plant (default {standby.LOOKBACK_DAYS})")
    parser.add_argument("--device", choices=sorted(devices.DEVICES) + ["all"],
                        default="all")
    parser.add_argument("--dry-run", action="store_true",
                        help="generate and judge, write nothing")
    parser.add_argument("--clear", action="store_true",
                        help="remove every planted row for these sensors")
    parser.add_argument("--seed", type=int, default=20260831,
                        help="jitter seed, so two runs plant the same house")
    args = parser.parse_args()

    if args.clear:
        print(f"removed {_clear()} event(s) for {len(devices.DEVICES)} sensor(s)")
        return 0

    # Ending YESTERDAY. Today is still being lived through — the live simulator
    # is writing into it, and standby.night_windows only counts a night that
    # falls whole inside the window anyway.
    last = date.today() - timedelta(days=1)
    days = [last - timedelta(days=offset) for offset in range(args.nights - 1, -1, -1)]
    now = _epoch(last + timedelta(days=1), 0, 0)

    chosen = sorted(devices.DEVICES) if args.device == "all" else [args.device]
    print(f"{'planning' if args.dry_run else 'planting'} {args.nights} night(s), "
          f"{days[0]} to {days[-1]}\n")

    for name in chosen:
        device = devices.DEVICES[name]
        rows = _series(device, days, random.Random(args.seed + len(name)))
        print(f"  {name:5} {device['sensor']}")
        print(f"        {len(rows)} rows, {device['standby_w']} W overnight "
              f"reported in {device['unit']}")
        print(f"        expected: {device['expect']}")
        print(f"        detector: {_verdict(device, rows, now)}")
        if not args.dry_run:
            stored = record.record_many(device["sensor"], rows, source="history")
            print(f"        stored:   {stored} new row(s)")
        print()

    if args.dry_run:
        print("nothing was written — drop --dry-run to plant it")
        return 0

    print("planted. Now:\n"
          "  1. add these sensors AND their switches to the watchlist in the panel\n"
          "  2. restart the add-on after 09:00 local — the review pass runs within 15 min\n"
          "  3. watch the log for REVIEW:, then look in the Suggestions tab")
    return 0


if __name__ == "__main__":
    sys.exit(main())
