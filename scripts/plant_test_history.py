#!/usr/bin/env python3
"""Plant backdated history for the MQTT test entities into Home Assistant's
recorder, so the agent can warm its behavioural record from it.

Why the recorder and not the agent's own store: the agent already knows how to
read HA's history — that is what core/backfill.py does on every connect. Writing
here means the data arrives through the real path, end to end, and Home
Assistant's own history UI can confirm independently that it is there. Seeding
the agent's database directly would prove nothing about either.

Nine days by default. The recorder purges at purge_keep_days (10 by default)
and the agent's own backfill floor is MAX_BACKFILL_DAYS (10), so anything older
would be written, purged, and never read.

Safety, in order:
  * --dry-run works on a COPY of the database, actually performs the insert
    there, and reports what Home Assistant's own history query would return
    from it. Nothing is guessed.
  * A real run takes a consistent backup first, through SQLite's backup API —
    a file copy of a live database is not a backup.
  * The insert is one short transaction. SQLite serialises writers, so a
    running Home Assistant is safe; it waits, or we do.
  * Every inserted row id is recorded, so --clear removes exactly what was
    planted and nothing else.

Usage:
    python3 scripts/plant_test_history.py --dry-run
    python3 scripts/plant_test_history.py
    python3 scripts/plant_test_history.py --clear
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario

DB_PATH = "/config/home-assistant_v2.db"
MARKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".planted.json")
BACKUP_DIR = "/share"

# HA stores a genuine state change with last_changed_ts NULL — it equals
# last_updated_ts, so it is not repeated. That is also exactly what the
# recorder's significant_changes_only filter looks for, and the agent's
# backfill passes that flag. Writing a value here instead of NULL would leave
# every planted row invisible to the agent while looking perfectly fine in the
# database.
INSERT_SQL = """
INSERT INTO states (metadata_id, state, last_updated_ts, last_changed_ts,
                    last_reported_ts, attributes_id, old_state_id, origin_idx)
VALUES (?, ?, ?, NULL, NULL, ?, NULL, 0)
"""


def _connect(path, read_only=False):
    if read_only:
        con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    else:
        con = sqlite3.connect(path, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    return con


def _metadata(con):
    """{entity_id: metadata_id} for the test entities, and their latest
    attributes_id so HA's own history UI still renders units and names."""
    ids, attrs = {}, {}
    for entity_id in scenario.ENTITIES:
        row = con.execute(
            "SELECT metadata_id FROM states_meta WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise SystemExit(
                f"{entity_id} has never been recorded by Home Assistant — nothing to attach history to."
            )
        ids[entity_id] = row["metadata_id"]
        latest = con.execute(
            "SELECT attributes_id FROM states WHERE metadata_id = ? AND attributes_id IS NOT NULL"
            " ORDER BY last_updated_ts DESC LIMIT 1",
            (row["metadata_id"],),
        ).fetchone()
        attrs[entity_id] = latest["attributes_id"] if latest else None
    return ids, attrs


def _insert(con, events, ids, attrs):
    """Inserts every event in one transaction, returning the row ids used.

    One transaction because SQLite serialises writers: a running Home Assistant
    waits for this lock rather than interleaving with it, which is also what
    makes the recorded id range exclusively ours."""
    with con:
        before = con.execute("SELECT COALESCE(MAX(state_id), 0) FROM states").fetchone()[0]
        con.executemany(INSERT_SQL, [
            (ids[entity_id], state, float(ts), attrs[entity_id])
            for entity_id, ts, state in events
        ])
        after = con.execute("SELECT COALESCE(MAX(state_id), 0) FROM states").fetchone()[0]
    return before, after


def _verify(con, ids, since, until):
    """Asks the database what Home Assistant's history query would return.

    Mirrors the recorder's significant_changes_only filter, which is the flag
    the agent's backfill sends — so this reports what the AGENT would see, not
    merely what was written."""
    out = {}
    for entity_id, metadata_id in ids.items():
        out[entity_id] = con.execute(
            "SELECT COUNT(*) FROM states WHERE metadata_id = ?"
            "  AND last_updated_ts >= ? AND last_updated_ts <= ?"
            "  AND (last_changed_ts IS NULL OR last_changed_ts = last_updated_ts)",
            (metadata_id, since, until),
        ).fetchone()[0]
    return out


def _report(title, counts):
    print(f"\n{title}")
    for entity_id in scenario.ENTITIES:
        print(f"   {entity_id:42} {counts.get(entity_id, 0):>6}")
    print(f"   {'TOTAL':42} {sum(counts.values()):>6}")


def plant(args):
    now = time.time()
    since = now - args.days * 86400
    events = scenario.generate(since, now, seed=args.seed)
    _report(f"Generated {args.days} days of history", scenario.summarise(events))
    print(f"   window: {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} → {datetime.fromtimestamp(now):%Y-%m-%d %H:%M}")

    if args.dry_run:
        # Rehearse on a copy: a real insert, a real query, no risk.
        rehearsal = os.path.join(BACKUP_DIR, "ha-recorder-rehearsal.db")
        with _connect(args.db, read_only=True) as src, sqlite3.connect(rehearsal) as dst:
            src.backup(dst)
        con = _connect(rehearsal)
        ids, attrs = _metadata(con)
        _insert(con, events, ids, attrs)
        _report("Home Assistant's history query would then return", _verify(con, ids, since, now))
        con.close()
        os.remove(rehearsal)
        print("\nDry run only — the real database was not touched.")
        return

    backup = os.path.join(BACKUP_DIR, f"ha-recorder-backup-{int(now)}.db")
    with _connect(args.db, read_only=True) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    print(f"\nBackup written to {backup} ({os.path.getsize(backup) // 1024} kB)")

    con = _connect(args.db)
    ids, attrs = _metadata(con)
    before, after = _insert(con, events, ids, attrs)
    _report("Home Assistant's history query now returns", _verify(con, ids, since, now))
    con.close()

    with open(MARKER_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "db": args.db, "backup": backup, "planted_at": int(now),
            "state_id_from": before + 1, "state_id_to": after,
            "rows": len(events), "window": [int(since), int(now)],
        }, f, indent=2)
    print(f"\nPlanted {len(events)} rows (state_id {before + 1}–{after}).")
    print(f"Undo with: python3 {os.path.basename(__file__)} --clear")
    print("\nThe agent picks this up on its next backfill — restart the add-on, or wait for the")
    print("15-minute re-resolve. Look for 'BACKFILL: recovered N transition(s)' in its log.")


def clear(args):
    if not os.path.exists(MARKER_PATH):
        raise SystemExit("Nothing recorded as planted — nothing to clear.")
    with open(MARKER_PATH, encoding="utf-8") as f:
        marker = json.load(f)

    con = _connect(marker.get("db", args.db))
    with con:
        # By row id, not by time window: genuine history shares that window and
        # must not be touched.
        deleted = con.execute(
            "DELETE FROM states WHERE state_id BETWEEN ? AND ?",
            (marker["state_id_from"], marker["state_id_to"]),
        ).rowcount
    con.close()
    os.remove(MARKER_PATH)
    print(f"Removed {deleted} planted row(s) (of {marker['rows']} planted).")
    print("The agent's own record still holds whatever it already backfilled — that is its copy,")
    print("and this script never touches it.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=DB_PATH, help=f"recorder database (default {DB_PATH})")
    parser.add_argument("--days", type=int, default=9,
                        help="how far back to plant (default 9; the recorder purges at 10)")
    parser.add_argument("--seed", type=int, default=20260824, help="scenario seed")
    parser.add_argument("--dry-run", action="store_true",
                        help="rehearse on a copy and report what HA would return")
    parser.add_argument("--clear", action="store_true", help="remove previously planted rows")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"No recorder database at {args.db}")
    clear(args) if args.clear else plant(args)


if __name__ == "__main__":
    main()
