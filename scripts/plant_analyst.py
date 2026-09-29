#!/usr/bin/env python3
"""Plant thirty days of the analyst test devices' habits straight into the
agent's behavioural record — the evidence the analyst reads and its charts
are drawn from.

WHY THE RECORD AND NOT THE RECORDER
-----------------------------------
plant_standby.py's argument, one floor lower. The explorer reads only the
record, and a chart is clamped to where the record starts, so the evidence is
exactly as long as the record is — and the recorder path
(plant_test_history.py) reaches back ten days at most. Ten days of a weekday
habit is seven or eight occurrences; thirty is twenty-odd, which is a habit a
person would believe. What this does not prove is the same list
plant_standby.py states: that the listener or backfill would have written
these rows. It proves everything after a record exists.

Rows are source='history', the source backfill writes, because that is what
they stand in for.

WHAT --dry-run CHECKS
---------------------
The figure each device's habit comes to, counted straight from the generated
rows — the number an analyst ought to find — plus the relation counts
`record._relation_counts` (the function behind "Get observed patterns") would
produce for them. A seed whose hallway pattern sits under
RELATION_MIN_SUPPORT would not be listed at all, and that is worth knowing
before anything is stored. (It used to recount the audited run's claims with
core/auditor.py's own `recount`; that half was retired with the run, F-9.)

AFTER PLANTING
--------------
`record.scan_relations()` runs once, so the patterns are in the relations
table now rather than at the agent's next hourly pass, and the pairs involving
these devices are read back out of the live store as proof.

--watch adds exactly these entities to the watchlist through the agent's
own /rest/observation/selection — the endpoint the panel's picker posts to,
so the running process updates its in-memory set and backfills them. Never
the file directly: `core/watchlist.py` loads it once and writes through, so a
file edited underneath it would be overwritten by the next panel save. The
existing selection is kept as it is; only these are added.

WHERE TO RUN IT
---------------
Inside the add-on's own container (/data is private to it):

    docker cp scripts app_local_fetchai-automation-agent:/tmp/devscripts
    docker exec app_local_fetchai-automation-agent python3 /tmp/devscripts/plant_analyst.py --dry-run

Usage:
    python3 scripts/plant_analyst.py --dry-run
    python3 scripts/plant_analyst.py --watch
    python3 scripts/plant_analyst.py --days 30
    python3 scripts/plant_analyst.py --clear
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyst_devices as devices

# plant_standby.py's own resolution: the container copies agent_home/ to /app,
# a checkout has it beside this script.
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
from core import watchlist                   # noqa: E402

# The agent's REST port inside the container. proxy.py forwards the panel's
# POSTs here; this script posts to it directly for the same endpoint.
AGENT_URL = os.environ.get("AGENT_URL", "http://127.0.0.1:8099")


def _rows(events: list) -> dict:
    """entity_id → record_many rows (ts, state, prev_state, prev_duration),
    oldest first — backfill's shape, since these rows stand in for its."""
    by_entity = {}
    for ts, entity_id, state in events:
        by_entity.setdefault(entity_id, []).append((ts, state))
    rows = {}
    for entity_id, series in by_entity.items():
        prev_state, prev_ts, out = None, None, []
        for ts, state in series:
            out.append((ts, state, prev_state, None if prev_ts is None else ts - prev_ts))
            prev_state, prev_ts = state, ts
        rows[entity_id] = out
    return rows


def _switch_ons(series, hours=None, weekdays=None) -> int:
    """How many rows turned `series` on — optionally only at these local
    hours [from, to) and on these weekdays (Monday 0)."""
    n = 0
    for ts, state, _prev, _dur in series:
        if state != "on":
            continue
        local = time.localtime(ts)
        if hours and not hours[0] <= local.tm_hour < hours[1]:
            continue
        if weekdays is not None and local.tm_wday not in weekdays:
            continue
        n += 1
    return n


def _relation_counts(rows: dict) -> dict:
    """What "Get observed patterns" would show for these rows alone, by
    running the record's own `_relation_counts` over an in-memory copy. Alone
    is the point: pairs with the rest of the house are noise this seed is
    built to avoid (analyst_devices.py, "WHAT THE TIMES HAVE TO SATISFY"), and
    the live readback after planting is where any of it would show."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE events (entity TEXT, ts INTEGER)")
    conn.executemany("INSERT INTO events VALUES (?, ?)",
                     [(entity_id, ts) for entity_id, series in rows.items()
                      for ts, *_ in series])
    counts = record._relation_counts(conn, record.RELATION_WINDOW_SECONDS, 0)
    conn.close()
    return {pair: n for pair, (n, _first, _last) in counts.items()
            if n >= record.RELATION_MIN_SUPPORT}


def _report(rows: dict) -> bool:
    """Prints each device's habit as the figure an analyst ought to find,
    counted straight from the generated rows. True when the hallway pair clears
    RELATION_MIN_SUPPORT — the one floor the record itself applies, below
    which "Get observed patterns" would not list it at all."""
    relations = _relation_counts(rows)
    follows = relations.get((devices.HALLWAY_MOTION, devices.HALLWAY_LIGHT), 0)
    fan = rows.get(devices.BATHROOM_FAN, [])
    fan_hours = sum(e - s for s, e in _stretches(fan, "on")) / 3600
    porch = _switch_ons(rows.get(devices.PORCH_LIGHT, []), hours=(18, 20), weekdays=range(5))
    lamp_h, lunch_h = _study_lamp_alone(rows)
    print("  what the analyst ought to find, counted from these rows:")
    print(f"    hallway   light follows motion: {follows}×")
    print(f"    bathroom  fan switched on {_switch_ons(fan)}×, on for {fan_hours:.1f} h in all")
    print(f"    porch     switched on at 18-20 on weekdays: {porch}×")
    print(f"    study     lamp on while the study is empty: {lamp_h:.1f} h, of which "
          f"{lunch_h:.1f} h over lunch")

    print("\n  what \"Get observed patterns\" would list for these devices alone:")
    for (a, b), n in sorted(relations.items(), key=lambda kv: -kv[1]):
        print(f"    {n:4}×  {a} → {b}")
    return follows >= record.RELATION_MIN_SUPPORT


def _stretches(series, state) -> list:
    """(start, end) of every stretch `series` spent in `state` that a later
    row closed — the query tool's example (b) shape, over the rows as
    planted."""
    return [(ts - dur, ts) for ts, _s, prev, dur in series if prev == state and dur]


def _study_lamp_alone(rows: dict) -> tuple:
    """Hours the study lamp was on while the study was empty, and how many of
    them fell in the 12:00-13:00 lunch hour — the figure only the explorer's
    SQL can reach (analyst_devices.py, "study")."""
    empty = _stretches(rows.get(devices.STUDY_OCCUPANCY, []), "off")
    lit = _stretches(rows.get(devices.STUDY_LAMP, []), "on")
    total = lunch = 0.0
    for a0, a1 in empty:
        for b0, b1 in lit:
            overlap = min(a1, b1) - max(a0, b0)
            if overlap > 0:
                total += overlap
                if time.localtime(max(a0, b0)).tm_hour == 12:
                    lunch += overlap
    return total / 3600, lunch / 3600


def _clear() -> int:
    """Removes every event for exactly these entities — plant_standby.py's
    `_clear`, scoped the same way and for the same reason."""
    removed = 0
    with sqlite3.connect(record.DB_PATH, timeout=30) as conn:
        for entity_id in devices.ENTITY_IDS:
            cursor = conn.execute(
                "DELETE FROM events WHERE entity ="
                " (SELECT id FROM entities WHERE entity_id = ?)", (entity_id,))
            removed += cursor.rowcount or 0
        conn.commit()
    return removed


def _live_relations():
    """The pairs the LIVE relations table now holds that involve these
    devices — including any with the rest of the house, which is exactly what
    the dry run could not show."""
    ours = set(devices.ENTITY_IDS)
    return [r for r in record.relations(limit=500)
            if r["entity_a"] in ours or r["entity_b"] in ours]


def _watch() -> bool:
    """Adds these entities to the watchlist through the panel's own endpoint.

    The current selection is read from the store the running process writes
    through (core/watchlist.py never re-reads it, so the file is what memory
    holds), and posted back with these added — devices and entities the
    household chose stay exactly as they were."""
    current = watchlist.selection()
    entities = sorted(set(current.get("entities") or []) | set(devices.ENTITY_IDS))
    body = {"devices": list(current.get("devices") or []), "entities": entities}
    request = urllib.request.Request(
        f"{AGENT_URL}/rest/observation/selection", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            answer = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, urllib.error.HTTPError) as error:
        print(f"  watchlist: could not reach the agent at {AGENT_URL} — {error}")
        return False
    if answer.get("error"):
        print(f"  watchlist: the agent refused — {answer['error']}")
        return False
    print(f"  watchlist: {len(devices.ENTITY_IDS)} entities added "
          f"(selection now {len(entities)} entities, {len(body['devices'])} devices)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30,
                        help="how many days to plant, ending yesterday (default 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="generate and judge, write nothing")
    parser.add_argument("--watch", action="store_true",
                        help="after planting, add these entities to the watchlist")
    parser.add_argument("--clear", action="store_true",
                        help="remove every planted row for these entities")
    parser.add_argument("--seed", type=int, default=20260921,
                        help="per-day jitter seed; simulate_analyst.py must use the same")
    args = parser.parse_args()

    if args.clear:
        print(f"removed {_clear()} event(s) for {len(devices.ENTITY_IDS)} entities")
        print(f"relations rescanned: {record.scan_relations()} pair(s) held")
        return 0

    # Ending YESTERDAY, as plant_standby.py does: today belongs to the live
    # simulator, and a planted row after a live one would sit in the chain
    # with the wrong prev_state.
    last = date.today() - timedelta(days=1)
    days = [last - timedelta(days=offset) for offset in range(args.days - 1, -1, -1)]
    events = []
    for day in days:
        events.extend(devices.day_events(day, devices.day_rng(args.seed, day)))
    rows = _rows(events)

    print(f"{'planning' if args.dry_run else 'planting'} {args.days} day(s), "
          f"{days[0]} to {days[-1]}\n")
    for name, device in devices.DEVICES.items():
        print(f"  {name:8} {device['expect']}")
        for entity in device["entities"].values():
            print(f"           {entity['entity_id']:40} {len(rows[entity['entity_id']]):5} rows")
    print()
    good = _report(rows)
    print()

    if args.dry_run:
        print("the hallway pattern clears the record's floor" if good else
              "WARNING: the hallway pattern is under RELATION_MIN_SUPPORT — "
              "\"Get observed patterns\" would not list it")
        print("nothing was written — drop --dry-run to plant it")
        return 0 if good else 1

    for entity_id in devices.ENTITY_IDS:
        stored = record.record_many(entity_id, rows[entity_id], source="history")
        print(f"  stored {stored:5} new row(s) for {entity_id}")
    print(f"\n  relations rescanned: {record.scan_relations()} pair(s) held in total")
    live = _live_relations()
    for r in live[:12]:
        print(f"    {r['count']:4}×  {r['entity_a']} → {r['entity_b']}")
    if not live:
        print("    none involve these devices — run with --dry-run to see why")
    print()

    if args.watch and not _watch():
        return 1
    print("\nplanted. Next:")
    if not args.watch:
        print(f"  - add the {len(devices.ENTITY_IDS)} entities to the watchlist (panel, or re-run with --watch)")
    print("  - keep them alive:  python3 simulate_analyst.py")
    print("  - run the analyst now instead of at 07:00:  "
          "python3 with_addon_key.py run_analyst.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
