"""The agent's own behavioural record: one row per meaningful state transition.

Neither Home Assistant store is enough on its own. The recorder keeps every
state change but purges after ~10 days by default — long before the replay
window the agent needs. Long-term statistics survive indefinitely but only
cover numeric sensors with a state_class, which excludes occupancy, doors,
lights and switches: nearly everything the product reasons about. So the agent
keeps its own record, and does not depend on how the user configured theirs.
(core/backfill.py does read those statistics, but only as the last resort for an
entity whose raw history is too large to fetch — hence the two backfilled
sources, 'history' and 'statistics', which the panel counts together and the
row itself keeps apart.)

Only this event log grows with time; the profile figures the panel shows are
computed from it on read rather than stored, so there is exactly one copy of
the truth to keep correct.

SQLite rather than a flat file: the questions asked of this table are all
"what did THIS entity do between THESE times", retention is one DELETE, and
the writer (the observation thread) and the readers (the REST handlers, on the
uAgent's event loop) need to work on it at the same time. It is in /data for
the same reason pairing.json is — /app is wiped by every add-on rebuild.
"""
from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
import sqlite3
import threading
import time

from core import settings

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(settings.DATA_DIR, "record.db")

# Events older than this are dropped. The design budgets the record at tens of
# MB in steady state, and this window is what keeps that true; the aggregates
# the profiles are built from would eventually be collapsed here instead of
# discarded, which is why the number lives in one place.
RETENTION_DAYS = int(os.environ.get("RECORD_RETENTION_DAYS", "90"))

# ...and the other half of the same promise (§6.2). RETENTION_DAYS bounds the
# record in TIME, which on a home whose sensors report every few seconds is no
# bound on SIZE at all — 2.7.0 and 2.7.1 demonstrated exactly that. This is the
# ceiling in bytes: as the record approaches it, enforce_cap() re-runs the same
# collapse with a shorter window, so the oldest events degrade into profiles
# early instead of the record growing without limit. 0 disables it, leaving the
# time-only behaviour.
#
# This one is an add-on option (record_max_mb) rather than an env var like its
# neighbours below, because it is the only number here that depends on the
# machine rather than on the data: /data on an SD-card Pi and /data on an SSD
# differ by orders of magnitude, and only the user knows which they have. The
# RELATION_* knobs and the retention window are judgements about what makes a
# useful record, and stay where a developer sets them.
MAX_BYTES = int(os.environ.get("RECORD_MAX_MB") or "256") * 1024 * 1024

# Where the cap starts biting, and where it stops. Shortening begins before the
# ceiling rather than at it — a bound first enforced once it is reached is a
# bound already broken — and runs until the record is comfortably under, so the
# next hourly pass does not immediately start again.
CAP_PRESSURE_FRACTION = 0.9
CAP_TARGET_FRACTION = 0.8

# The shortest window the cap may squeeze the record into. Below a week the
# record can no longer answer "what does this house do on a Sunday", which is
# the question it exists for. A home that cannot fit a week inside its cap
# wants a larger cap or fewer watched entities — not a record too short to
# reason from — so this floor holds and the shortfall is logged instead.
CAP_MIN_RETENTION_DAYS = 7

# How soon after entity A changes a change in entity B still counts as a
# response to it, for the relations scan below. Taken from the design doc's
# own example (§6.1): "motion in hall -> hall light within 2 min".
RELATION_WINDOW_SECONDS = int(os.environ.get("RELATION_WINDOW_SECONDS", "120"))

# How far back the relations scan reads. 0 means "everything the events table
# still holds", which is already bounded by RETENTION_DAYS — the knob exists
# for a home whose record is large enough that it wants the scan narrower than
# its retention, not because the scan needs it to stay affordable.
RELATION_SCAN_DAYS = int(os.environ.get("RELATION_SCAN_DAYS", "0"))

# A pair needs at least this many occurrences to be kept. Without a floor, one
# coincidental pair of events would be indistinguishable from a real habit —
# the design's "pruned by support" (§6.1).
RELATION_MIN_SUPPORT = int(os.environ.get("RELATION_MIN_SUPPORT", "3"))

# How long a writer waits for another connection's transaction before giving
# up. SQLite allows one writer at a time even in WAL mode, so every write here
# is competing with the hourly maintenance jobs; this is the margin that
# absorbs them. It is a margin, not a guarantee — a maintenance job that holds
# the lock for longer than this is a bug in that job, and was.
BUSY_TIMEOUT_SECONDS = 15

# One connection per thread. sqlite3 connections cannot be shared across
# threads, and this store deliberately has a writer thread and reader threads.
_local = threading.local()

# entity_id -> small integer id. Bounded by the number of watched entities, and
# only ever added to, so it is safe to share across threads without a lock.
_entity_ids = {}

_SCHEMA = (
    # The dimension table is the design's "entity ref": the growing table
    # stores a small integer instead of repeating a 40-character entity_id on
    # every row.
    "CREATE TABLE IF NOT EXISTS entities ("
    " id INTEGER PRIMARY KEY, entity_id TEXT NOT NULL UNIQUE)",

    # prev_duration is how long the PREVIOUS state lasted, in seconds. Storing
    # it per row is what makes on-fraction and mean-duration a single scan
    # instead of a self-join over consecutive rows.
    "CREATE TABLE IF NOT EXISTS events ("
    " id INTEGER PRIMARY KEY,"
    " entity INTEGER NOT NULL,"
    " ts INTEGER NOT NULL,"
    " state TEXT NOT NULL,"
    " prev_state TEXT,"
    " prev_duration INTEGER,"
    " source TEXT NOT NULL DEFAULT 'live')",

    # UNIQUE, not just an index: backfill re-reads windows the live listener
    # may already have covered, and every insert is INSERT OR IGNORE, so an
    # overlapping run is a no-op rather than a double count.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_entity_ts ON events(entity, ts)",

    # ...and time on its own, because two whole-record passes ask "what
    # happened after this instant" across ALL entities: prune()'s DELETE and
    # the ordered read scan_relations() walks. Neither can use the index above,
    # whose leading column is the entity.
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)",

    # Where a pruned entity's history goes instead of vanishing: one row per
    # entity, updated in place, never growing with time — the design's "Fixed
    # size" promise for profiles (§6.1). prune() merges into this before it
    # deletes; profile()/first_ts()/summary()/stats() read it back in on top of
    # whatever is still live. by_hour/by_day are JSON arrays (24 and 7 ints) —
    # a few bytes each, not worth 31 extra columns.
    "CREATE TABLE IF NOT EXISTS profiles ("
    " entity INTEGER PRIMARY KEY REFERENCES entities(id),"
    " since INTEGER," # earliest ts EVER collapsed into this row
    " until INTEGER," # latest ts collapsed so far (~ the prune cutoff, last merge)
    " events INTEGER NOT NULL DEFAULT 0,"
    " on_seconds INTEGER NOT NULL DEFAULT 0,"  # SUM(prev_duration) WHERE prev_state='on'
    " on_count INTEGER NOT NULL DEFAULT 0,"    # COUNT of those rows — AVG() cannot be
                                                # summed across merges, so the mean has to
                                                # be recomputed from its own numerator/denominator
    " total_seconds INTEGER NOT NULL DEFAULT 0,"
    " by_hour TEXT NOT NULL DEFAULT '[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]',"
    " by_day TEXT NOT NULL DEFAULT '[0,0,0,0,0,0,0]')",

    # Observed co-occurrences (design §6.1's third record layer): how often
    # entity_b changes within RELATION_WINDOW_SECONDS after entity_a changes.
    # Directed — (A, B) and (B, A) are different observations — and fixed
    # size, rebuilt in full by scan_relations() rather than grown like events.
    "CREATE TABLE IF NOT EXISTS relations ("
    " entity_a INTEGER NOT NULL REFERENCES entities(id),"
    " entity_b INTEGER NOT NULL REFERENCES entities(id),"
    " count INTEGER NOT NULL,"
    " since INTEGER NOT NULL,"  # earliest ts_a contributing, this scan
    " until INTEGER NOT NULL,"  # latest ts_a contributing, this scan
    " PRIMARY KEY (entity_a, entity_b))",
)


def _now():
    return int(datetime.now(tz=timezone.utc).timestamp())


def _connect():
    """Returns this thread's connection, opening and initialising it on first
    use. Raises sqlite3.Error — every public function below handles that."""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == DB_PATH:
        return conn
    if conn is not None:
        # DB_PATH changed under us (only tests do this) — drop the stale handle.
        close()
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    # WAL is what lets the observation thread keep writing while a REST handler
    # reads; the default rollback journal would have them blocking each other.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for statement in _SCHEMA:
        conn.execute(statement)
    conn.commit()
    _local.conn = conn
    _local.path = DB_PATH
    return conn


def close():
    """Drops this thread's connection and the id cache. Tests call it when they
    repoint DB_PATH; nothing in production needs it."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None
    _local.path = None
    _entity_ids.clear()


def _entity_key(conn, entity_id: str) -> int:
    """Maps an entity_id to its small integer id, creating it if new."""
    cached = _entity_ids.get(entity_id)
    if cached is not None:
        return cached
    conn.execute("INSERT OR IGNORE INTO entities (entity_id) VALUES (?)", (entity_id,))
    # Committed before it is cached, and before the caller's own write. The
    # cache is process-wide and outlives any transaction, so an id it hands out
    # has to already be on disk: a rolled-back retry below must not be able to
    # take the entities row back out from under an id another thread is
    # already writing events against.
    conn.commit()
    row = conn.execute("SELECT id FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
    _entity_ids[entity_id] = row["id"]
    return row["id"]


def _is_locked(error) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _write(conn, run, attempts: int = 3):
    """Runs a write closure and commits it, retrying while another connection
    holds the write lock.

    sqlite3's own `timeout` already waits out a lock it meets on the way in;
    this covers the case that timeout cannot — the lock being released and
    immediately re-taken by a third party, which is exactly what a busy
    observation thread and an hourly maintenance job do to each other. The
    closure must be safe to run twice: everything here is INSERT OR IGNORE."""
    for attempt in range(attempts):
        try:
            result = run(conn)
            conn.commit()
            return result
        except sqlite3.OperationalError as e:
            if not _is_locked(e) or attempt == attempts - 1:
                raise
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            time.sleep(0.25 * (attempt + 1))


def record_transition(entity_id: str, ts: int, state: str,
                      prev_state=None, prev_duration=None, source: str = "live") -> bool:
    """Stores one transition. Returns True if it was new.

    Never raises: a record that cannot be written must not take the agent down
    with it — the home stays controllable, the observation is what is lost."""
    if not entity_id or state is None:
        return False
    try:
        conn = _connect()
        key = _entity_key(conn, entity_id)
        cur = _write(conn, lambda c: c.execute(
            "INSERT OR IGNORE INTO events (entity, ts, state, prev_state, prev_duration, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (key, int(ts), str(state), prev_state, prev_duration, source),
        ))
        return cur.rowcount > 0
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not store a transition for {entity_id}: {e}")
        return False


def record_many(entity_id: str, rows: list, source: str = "history") -> int:
    """Stores a batch of transitions for one entity, returning how many were
    new. Rows are (ts, state, prev_state, prev_duration) tuples — the shape
    core.backfill produces from a Home Assistant history series."""
    if not entity_id or not rows:
        return 0
    try:
        conn = _connect()
        key = _entity_key(conn, entity_id)
        values = [(key, int(ts), str(state), prev_state, prev_duration, source)
                  for ts, state, prev_state, prev_duration in rows]

        def insert(c):
            # Counted inside the closure: a retried attempt starts its own
            # count, so a rolled-back first try is not added to the second.
            before = c.total_changes
            c.executemany(
                "INSERT OR IGNORE INTO events"
                " (entity, ts, state, prev_state, prev_duration, source)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )
            return c.total_changes - before

        return _write(conn, insert)
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not store a batch for {entity_id}: {e}")
        return 0


def last_ts(entity_id: str):
    """The most recent recorded timestamp for an entity, or None if it has
    never been recorded. This is where backfill resumes from."""
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT MAX(ts) AS ts FROM events WHERE entity ="
            " (SELECT id FROM entities WHERE entity_id = ?)",
            (entity_id,),
        ).fetchone()
        return row["ts"] if row and row["ts"] is not None else None
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not read the last timestamp for {entity_id}: {e}")
        return None


def first_ts(entity_id: str):
    """The oldest timestamp EVER recorded for an entity, or None — live or
    collapsed into its profile, whichever reaches further back.

    How far back the record actually reaches — which is not the same as how far
    back it could. Home Assistant's recorder may hold history from before the
    agent was watching, and backfill uses this to notice that. Without the
    profiles half, this would creep forward by itself every time prune() runs:
    the true first-ever event is exactly what pruning removes first."""
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT MIN(v.ts) AS live, p.since AS collapsed FROM entities e"
            " LEFT JOIN events v ON v.entity = e.id"
            " LEFT JOIN profiles p ON p.entity = e.id"
            " WHERE e.entity_id = ?",
            (entity_id,),
        ).fetchone()
        if not row:
            return None
        candidates = [t for t in (row["live"], row["collapsed"]) if t is not None]
        return min(candidates) if candidates else None
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not read the first timestamp for {entity_id}: {e}")
        return None


def last_state(entity_id: str):
    """The latest {state, ts} for an entity, or None. The observation loop
    seeds itself from this after a restart so the first live event is judged
    against what was actually last recorded, not against nothing."""
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT state, ts FROM events WHERE entity ="
            " (SELECT id FROM entities WHERE entity_id = ?)"
            " ORDER BY ts DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
        return {"state": row["state"], "ts": row["ts"]} if row else None
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not read the last state for {entity_id}: {e}")
        return None


def state_at(entity_id: str, ts: int):
    """The state in force just before `ts`: the newest row with ts < ts, or
    None if nothing was recorded before it (never watched yet at that instant,
    or the record does not reach back that far).

    core/timeline.py's `runs()` needs this to know the state a window's FIRST
    run should open in — without it, a window that opens mid-state can only be
    reported as starting at its first change inside the window, silently
    understating how long that first state had already been running. One
    query on idx_events_entity_ts (entity, ts DESC), no write lock — this is a
    plain read like between() and recent(), never inside a transaction."""
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT state FROM events WHERE entity ="
            " (SELECT id FROM entities WHERE entity_id = ?)"
            "   AND ts < ?"
            " ORDER BY ts DESC LIMIT 1",
            (entity_id, int(ts)),
        ).fetchone()
        return row["state"] if row else None
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"RECORD: could not read the state before {ts} for {entity_id}: {e}")
        return None


def recent(entity_id: str, limit: int = 50) -> list:
    """The newest transitions for one entity, most recent first."""
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT ts, state, prev_state, prev_duration, source FROM events"
            " WHERE entity = (SELECT id FROM entities WHERE entity_id = ?)"
            " ORDER BY ts DESC LIMIT ?",
            (entity_id, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not read recent events for {entity_id}: {e}")
        return []


def between(entity_id: str, start: int, end: int) -> list:
    """One entity's transitions between two instants, OLDEST first.

    The other read shapes cannot answer a question about a span: recent() is
    newest-first with a count limit, so widening the span means guessing a
    limit and silently losing the old end of it, and profile() has already
    aggregated away the thing a detector needs — the individual transitions, in
    order, with the gaps between them intact.

    Half-open [start, end) so two adjacent windows tile without counting the
    boundary row twice. `source` travels with every row because it changes what
    the row is evidence OF: a 'statistics' row is a bucket mean from Home
    Assistant's own aggregates, and a mean cannot show that a value never
    dropped below a floor. Only the caller knows whether that matters, so this
    hands the distinction on rather than resolving it.

    The window is the bound — there is no row limit. A caller asking for one
    entity over weeks gets thousands of rows, which is what a detector reads;
    a caller wanting fewer asks for a shorter span.

    No transaction is opened, and none may be: this is a scan, the observation
    thread is writing the whole time, and a scan holding the write lock is how
    scan_relations once wedged the record (see _relation_counts).
    """
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT ts, state, prev_state, prev_duration, source FROM events"
            " WHERE entity = (SELECT id FROM entities WHERE entity_id = ?)"
            "   AND ts >= ? AND ts < ?"
            " ORDER BY ts",
            (entity_id, int(start), int(end)),
        ).fetchall()
        return [dict(r) for r in rows]
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"RECORD: could not read {entity_id} between {start} and {end}: {e}")
        return []


def summary() -> list:
    """One row per entity the record holds LIVE events for: how much was seen,
    over what span, and what it was last doing. This is the Record tab's table.

    `first_seen` is widened by a LEFT JOIN onto profiles so it does not creep
    forward every time prune() runs — the entity's true start, even where the
    rows that proved it have since been collapsed. An entity with NO live
    events left at all (everything it ever did has been collapsed) still will
    not appear here at all, though — that would need this query's shape to
    change from an inner join to something that reaches entities with no
    matching events row, which is a real gap, just not this one's fix."""
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT e.entity_id AS entity_id, COUNT(*) AS events,"
            "       MIN(MIN(v.ts), COALESCE(p.since, MIN(v.ts))) AS first_seen,"
            "       MAX(v.ts) AS last_seen,"
            "       SUM(CASE WHEN v.source IN ('history', 'statistics') THEN 1 ELSE 0 END)"
            "         AS backfilled"
            " FROM events v JOIN entities e ON e.id = v.entity"
            " LEFT JOIN profiles p ON p.entity = v.entity"
            " GROUP BY v.entity ORDER BY last_seen DESC"
        ).fetchall()
        # Resolved separately: the last state per entity is a different shape of
        # question from the aggregate above, and one query per shape stays
        # readable where a window function would not.
        latest = {
            r["entity_id"]: r["state"]
            for r in conn.execute(
                "SELECT e.entity_id AS entity_id, v.state AS state FROM events v"
                " JOIN entities e ON e.id = v.entity"
                " WHERE v.id IN (SELECT MAX(id) FROM events GROUP BY entity)"
            ).fetchall()
        }
        out = []
        for r in rows:
            item = dict(r)
            item["last_state"] = latest.get(r["entity_id"], "")
            out.append(item)
        return out
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not build the summary: {e}")
        return []


def profile(entity_id: str, days: int = 30) -> dict:
    """The per-entity aggregates: mostly computed on read from live events, but
    widened with the entity's collapsed profile when the requested window
    reaches further back than the live table still holds.

    Hour-of-day and day-of-week are in LOCAL time on purpose: "this light comes
    on around seven" is a statement about the household's clock, and Home
    Assistant sets the container's timezone to the home's.

    Collapsed history has no sub-day resolution, so when it is included it is
    included WHOLE — there is no way to trim it to exactly `days`. That is what
    `includes_collapsed_history` is for: a caller that asked for the last 400
    days is told the answer actually covers "everything the record holds",
    which is a different, less precise claim than "exactly the last 400 days".

    `changes_to_state` and `on_hours` are the exception to all of the above:
    LIVE EVENTS ONLY, over [`window_start`, now), where `window_start` =
    max(now − days, first_ts), and `includes_collapsed_history` does not apply
    to them. They were added as the two figures the audited analyst's claims
    could cite (proposal §5A C-1), recounted through `between()`, which never
    sees collapsed history — so a figure widened with the collapsed profile
    could never have recounted. That audit is retired (free-hand F-9, 2.23.0);
    the rule stays, because a count and the window printed beside it must be
    one statement, and the window starts no earlier than `first_ts` for the
    same reason: clipping the on-spans to that start is what makes them so.
    """
    empty = {"by_hour": [0] * 24, "by_day": [0] * 7, "events": 0,
             "on_fraction": None, "mean_on_duration": None, "days": days,
             "includes_collapsed_history": False,
             "changes_to_state": {}, "on_hours": None, "window_start": None}
    try:
        conn = _connect()
        now = _now()
        since = now - int(days) * 86400
        entity_row = conn.execute(
            "SELECT id FROM entities WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        if not entity_row:
            return empty
        entity = entity_row["id"]

        rows = conn.execute(
            "SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour,"
            "       CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INTEGER) AS weekday,"
            "       COUNT(*) AS n"
            " FROM events WHERE entity = ? AND ts >= ? GROUP BY hour, weekday",
            (entity, since),
        ).fetchall()

        by_hour = [0] * 24
        by_day = [0] * 7
        total = 0
        for r in rows:
            by_hour[r["hour"]] += r["n"]
            # strftime('%w') counts from Sunday; the panel reads Monday-first.
            by_day[(r["weekday"] + 6) % 7] += r["n"]
            total += r["n"]

        durations = conn.execute(
            "SELECT SUM(CASE WHEN prev_state = 'on' THEN prev_duration ELSE 0 END) AS on_seconds,"
            "       SUM(CASE WHEN prev_state = 'on' AND prev_duration IS NOT NULL"
            "            THEN 1 ELSE 0 END) AS on_count,"
            "       SUM(COALESCE(prev_duration, 0)) AS total_seconds"
            " FROM events WHERE entity = ? AND ts >= ?",
            (entity, since),
        ).fetchone()
        on_seconds = durations["on_seconds"] or 0
        on_count = durations["on_count"] or 0
        total_seconds = durations["total_seconds"] or 0

        # Widen with the collapsed profile only when the window actually
        # reaches into it — comparing against the persisted cutoff (`until`),
        # never against RETENTION_DAYS, so this stays correct even if that
        # setting changes across a restart.
        collapsed = conn.execute(
            "SELECT since, until, events, on_seconds, on_count, total_seconds,"
            "       by_hour, by_day FROM profiles WHERE entity = ?",
            (entity,),
        ).fetchone()
        includes_collapsed = bool(collapsed and collapsed["until"] is not None
                                  and since <= collapsed["until"])

        # Whether 'on' is even a state this entity ever takes, checked across
        # its WHOLE history (not just this window) and widened with the
        # collapsed profile's on_count. Without this, a cover or climate
        # entity — which never uses the literal word 'on' — divides 0 by a
        # nonzero total_seconds and reports on_fraction 0.0, indistinguishable
        # from a light that really is always off. "No on-concept" and "on-
        # concept, currently zero" are different claims.
        has_on_history = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM events WHERE entity = ?"
            " AND (state = 'on' OR prev_state = 'on')) AS e",
            (entity,),
        ).fetchone()["e"]
        has_on_history = bool(has_on_history) or bool(collapsed and (collapsed["on_count"] or 0) > 0)
        if includes_collapsed:
            for h, n in enumerate(json.loads(collapsed["by_hour"])):
                by_hour[h] += n
            for d, n in enumerate(json.loads(collapsed["by_day"])):
                by_day[d] += n
            total += collapsed["events"] or 0
            on_seconds += collapsed["on_seconds"] or 0
            on_count += collapsed["on_count"] or 0
            total_seconds += collapsed["total_seconds"] or 0

        # The citable figures — live rows only, see the docstring. first_ts()
        # is read here rather than by the caller so the window and the figures
        # come from one `now`.
        first = first_ts(entity_id)
        window_start = max(since, first) if first is not None else since
        changes_to_state = {
            r["state"]: r["n"]
            for r in conn.execute(
                "SELECT state, COUNT(*) AS n FROM events"
                " WHERE entity = ? AND ts >= ? AND ts < ? GROUP BY state ORDER BY state",
                (entity, window_start, now),
            ).fetchall()
        }
        # Each 'on' row closes a span (ts - prev_duration, ts); only the part
        # after window_start counts. The span a row closes always ends at its
        # own ts < now, so only the start needs clipping.
        on_hours = None
        if has_on_history:
            clipped = conn.execute(
                "SELECT SUM(ts - MAX(ts - prev_duration, ?)) AS s FROM events"
                " WHERE entity = ? AND ts >= ? AND ts < ?"
                "   AND prev_state = 'on' AND prev_duration IS NOT NULL"
                "   AND ts - MAX(ts - prev_duration, ?) > 0",
                (window_start, entity, window_start, now, window_start),
            ).fetchone()["s"]
            on_hours = (clipped or 0) / 3600.0

        return {
            "by_hour": by_hour,
            "by_day": by_day,
            "events": total,
            # None, not 0.0, when there is nothing to divide, OR when 'on' is
            # not a state this entity's history has ever shown: "no data yet",
            # "no on/off concept", and "never on" must not render as the same
            # thing.
            "on_fraction": (on_seconds / total_seconds) if (total_seconds and has_on_history) else None,
            "mean_on_duration": (on_seconds / on_count) if on_count else None,
            "days": days,
            "includes_collapsed_history": includes_collapsed,
            "changes_to_state": changes_to_state,
            "on_hours": on_hours,
            "window_start": window_start,
        }
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not build a profile for {entity_id}: {e}")
        return empty


def stats() -> dict:
    """Whole-record totals, for the Record tab's header line.

    `oldest` is widened across every entity's profile too, for the same reason
    first_ts() is: without it, "how far back does the record reach" creeps
    forward by an hour every time prune() runs, which is exactly the honesty
    Get behavioural record overview promises not to lose."""
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT COUNT(*) AS events, MIN(ts) AS oldest, MAX(ts) AS newest,"
            "       SUM(CASE WHEN source IN ('history', 'statistics') THEN 1 ELSE 0 END)"
            "         AS backfilled"
            " FROM events"
        ).fetchone()
        collapsed_since = conn.execute(
            "SELECT MIN(since) AS since FROM profiles"
        ).fetchone()["since"]
        oldest_candidates = [t for t in (row["oldest"], collapsed_since) if t is not None]
        size = 0
        for suffix in ("", "-wal"):
            try:
                size += os.path.getsize(DB_PATH + suffix)
            except OSError:
                pass
        return {
            "events": row["events"] or 0,
            "backfilled": row["backfilled"] or 0,
            "oldest": min(oldest_candidates) if oldest_candidates else None,
            "newest": row["newest"],
            "entities": conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"],
            "bytes": size,
            "retention_days": RETENTION_DAYS,
            "max_bytes": MAX_BYTES,
            # Whether the cap is currently the thing deciding how far back the
            # record reaches. When it is, `retention_days` is the configured
            # window and no longer the one in force — a caller that reports one
            # without the other is telling the user the record remembers more
            # than it does.
            "capped": bool(MAX_BYTES) and _used_bytes(conn) >= MAX_BYTES * CAP_PRESSURE_FRACTION,
        }
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not read stats: {e}")
        return {"events": 0, "backfilled": 0, "oldest": None, "newest": None,
                "entities": 0, "bytes": 0, "retention_days": RETENTION_DAYS,
                "max_bytes": MAX_BYTES, "capped": False}


def _collapse_before(conn, cutoff: int) -> int:
    """Folds every event older than `cutoff` into its entity's profiles row,
    before prune() deletes them. Returns how many entities were touched.

    The merge arithmetic happens in Python, not SQL: the histograms are JSON
    columns, so there is no clean SQL-side way to add two of them elementwise,
    and doing the whole merge in one place — read old, add new, write final —
    is easier to get right than splitting it between a query and an UPSERT's
    SET clause.
    """
    hist_rows = conn.execute(
        "SELECT entity,"
        "       CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour,"
        "       CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INTEGER) AS weekday,"
        "       COUNT(*) AS n"
        " FROM events WHERE ts < ? GROUP BY entity, hour, weekday",
        (cutoff,),
    ).fetchall()
    dur_rows = conn.execute(
        "SELECT entity, MIN(ts) AS since, MAX(ts) AS until, COUNT(*) AS events,"
        "       SUM(CASE WHEN prev_state = 'on' THEN prev_duration ELSE 0 END) AS on_seconds,"
        "       SUM(CASE WHEN prev_state = 'on' AND prev_duration IS NOT NULL"
        "            THEN 1 ELSE 0 END) AS on_count,"
        "       SUM(COALESCE(prev_duration, 0)) AS total_seconds"
        " FROM events WHERE ts < ? GROUP BY entity",
        (cutoff,),
    ).fetchall()
    if not dur_rows:
        return 0

    by_entity = {}
    for r in hist_rows:
        bucket = by_entity.setdefault(r["entity"], {"by_hour": [0] * 24, "by_day": [0] * 7})
        bucket["by_hour"][r["hour"]] += r["n"]
        # strftime('%w') counts from Sunday; profile() reads Monday-first.
        bucket["by_day"][(r["weekday"] + 6) % 7] += r["n"]

    for d in dur_rows:
        bucket = by_entity.setdefault(d["entity"], {"by_hour": [0] * 24, "by_day": [0] * 7})
        bucket.update({
            "since": d["since"], "until": d["until"], "events": d["events"],
            "on_seconds": d["on_seconds"] or 0, "on_count": d["on_count"] or 0,
            "total_seconds": d["total_seconds"] or 0,
        })

        existing = conn.execute(
            "SELECT since, until, events, on_seconds, on_count, total_seconds,"
            "       by_hour, by_day FROM profiles WHERE entity = ?",
            (d["entity"],),
        ).fetchone()

        since = bucket["since"] if existing is None or existing["since"] is None \
            else min(existing["since"], bucket["since"])
        until = bucket["until"] if existing is None or existing["until"] is None \
            else max(existing["until"], bucket["until"])
        events = bucket["events"] + (existing["events"] if existing else 0)
        on_seconds = bucket["on_seconds"] + (existing["on_seconds"] if existing else 0)
        on_count = bucket["on_count"] + (existing["on_count"] if existing else 0)
        total_seconds = bucket["total_seconds"] + (existing["total_seconds"] if existing else 0)
        by_hour = bucket["by_hour"]
        by_day = bucket["by_day"]
        if existing:
            by_hour = [a + b for a, b in zip(by_hour, json.loads(existing["by_hour"]))]
            by_day = [a + b for a, b in zip(by_day, json.loads(existing["by_day"]))]

        conn.execute(
            "INSERT INTO profiles (entity, since, until, events, on_seconds, on_count,"
            "  total_seconds, by_hour, by_day) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(entity) DO UPDATE SET"
            "  since = excluded.since, until = excluded.until, events = excluded.events,"
            "  on_seconds = excluded.on_seconds, on_count = excluded.on_count,"
            "  total_seconds = excluded.total_seconds,"
            "  by_hour = excluded.by_hour, by_day = excluded.by_day",
            (d["entity"], since, until, events, on_seconds, on_count, total_seconds,
             json.dumps(by_hour), json.dumps(by_day)),
        )
    return len(by_entity)


def prune(retention_days: int = None) -> int:
    """Collapses events past the retention window into their entities'
    profiles, then drops them, returning how many events went.

    Collapse and delete happen in one transaction — a crash between them would
    either lose the about-to-be-deleted data's aggregate, or collapse the same
    events twice on the next run. Never a DELETE without the merge first."""
    days = RETENTION_DAYS if retention_days is None else int(retention_days)
    try:
        conn = _connect()
        cutoff = _now() - days * 86400
        touched = _collapse_before(conn, cutoff)
        cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        conn.commit()
        if cur.rowcount:
            logger.info(
                f"RECORD: pruned {cur.rowcount} event(s) older than {days} days, "
                f"collapsed into {touched} entity profile(s)"
            )
        return cur.rowcount
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not prune: {e}")
        return 0


def _used_bytes(conn) -> int:
    """How many bytes of the database are actually holding data.

    Deliberately not os.path.getsize(): SQLite's DELETE returns pages to a
    freelist and reuses them, it does not shrink the file, so the file size is
    a high-water mark that no collapse can move. Measuring the used pages is
    what makes the cap converge instead of ratcheting the window down to its
    floor on a size that was never going to fall — and the freed pages then
    absorb the next weeks of events, so the file stops growing, which is the
    footprint promise. The alternative, VACUUM, rewrites the whole database
    under an exclusive lock: exactly the long hold this store may not take.
    """
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return max(0, page_count - free) * page_size


def enforce_cap(max_bytes: int = None) -> int:
    """Collapses the record early when it approaches its size cap, halving the
    retention window until the used bytes are back under target. Returns how
    many events were collapsed away.

    Nothing here is a second retention setting: every window it tries is
    derived from RETENTION_DAYS, so that number still lives in exactly one
    place. Nothing is persisted either — the window in force is implicit in
    which events are still there, so a home that quietens down simply keeps its
    newer events and grows back towards its configured retention on its own.

    Events are never dropped without being collapsed first: this calls prune(),
    the same collapse-then-delete transaction, only with a shorter cutoff. The
    price of an early collapse is sub-day resolution over the stretch that
    collapsed, and profile()'s `includes_collapsed_history` goes on reporting it
    honestly for free — it compares the requested window against the PERSISTED
    collapse boundary, never against RETENTION_DAYS, so moving that boundary
    forward moves the flag with it. Profiles themselves are never discarded.

    Each iteration is one prune(), so the write lock is taken and released once
    per step and never held across the measurement — and the halving bounds the
    number of steps at ~log2(RETENTION_DAYS / CAP_MIN_RETENTION_DAYS).
    """
    cap = MAX_BYTES if max_bytes is None else int(max_bytes)
    if cap <= 0:
        return 0
    try:
        conn = _connect()
        used = _used_bytes(conn)
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not measure the record against its cap: {e}")
        return 0
    if used < cap * CAP_PRESSURE_FRACTION:
        return 0

    target = cap * CAP_TARGET_FRACTION
    megabyte = 1024 * 1024
    days = RETENTION_DAYS
    collapsed = 0
    while days > CAP_MIN_RETENTION_DAYS:
        days = max(CAP_MIN_RETENTION_DAYS, days // 2)
        collapsed += prune(retention_days=days)
        try:
            used = _used_bytes(_connect())
        except sqlite3.Error as e:
            logger.error(f"RECORD: could not re-measure the record after collapsing: {e}")
            break
        if used <= target:
            break

    if collapsed:
        logger.info(
            f"RECORD: at {used // megabyte} MB of a {cap // megabyte} MB cap — collapsed "
            f"{collapsed} event(s) into profiles early, window now {days} day(s)"
        )
    if used > cap:
        # The floor held and the cap did not. Saying so is the whole of the
        # response: the fixes are a larger record_max_mb or fewer watched
        # entities, and both are the user's to make.
        logger.warning(
            f"RECORD: still {used // megabyte} MB against a {cap // megabyte} MB cap at the "
            f"{CAP_MIN_RETENTION_DAYS}-day floor — raise record_max_mb or watch fewer entities"
        )
    return collapsed


def _relation_counts(conn, window: int, since: int) -> dict:
    """Counts, for each ordered pair, how many A-events had at least one
    B-event follow them within `window` seconds. One pass over the events
    table in time order, holding only the events still inside the window.

    This was a SQL self-join, and it is what stopped the record accepting
    writes on a home with fast-reporting sensors. Its join condition —
    `b.entity != a.entity AND b.ts > a.ts AND b.ts <= a.ts + window` — cannot
    use idx_events_entity_ts, whose leading column is exactly the one
    constrained by `!=`, so SQLite scanned the whole table once per row. That
    is quadratic: fine on the few thousand rows a handful of lights produce,
    hours on the million a dozen energy sensors do. And all of it ran inside
    the write transaction that `DELETE FROM relations` had already opened, so
    the observation thread timed out against that lock on every single
    transition it tried to store.

    Reading is deliberately all this does. The caller takes the write lock
    afterwards, for the two statements that genuinely need it.
    """
    counts = {}
    # The events whose window is still open, oldest first: each is an A
    # candidate, carrying the set of entities seen changing after it. Bounded
    # by how many events fall inside `window`, not by the size of the record.
    pending = deque()

    def close_out(entity, ts, followers):
        """An A-event that nothing further can follow: fold it into its pairs.
        One increment per A-event however many times B changed — the DISTINCT
        the old query carried, and the reason "43 times" counts occurrences
        rather than events."""
        for follower in followers:
            slot = counts.get((entity, follower))
            if slot is None:
                counts[(entity, follower)] = [1, ts, ts]
            else:
                slot[0] += 1
                slot[1] = min(slot[1], ts)
                slot[2] = max(slot[2], ts)

    cursor = conn.execute(
        "SELECT entity, ts FROM events WHERE ts >= ? ORDER BY ts", (since,)
    )
    for row in cursor:
        entity, ts = row["entity"], row["ts"]
        while pending and pending[0][1] < ts - window:
            close_out(*pending.popleft())
        for a_entity, a_ts, followers in pending:
            # Strictly after, and a different entity — the same two conditions
            # the join carried. Two changes sharing a timestamp are not
            # evidence that either answered the other.
            if a_ts < ts and a_entity != entity:
                followers.add(entity)
        pending.append((entity, ts, set()))
    for candidate in pending:
        close_out(*candidate)
    return counts


def scan_relations(window_seconds: int = None, min_support: int = None) -> int:
    """Recomputes observed co-occurrences: how often entity B changes within
    `window_seconds` after entity A changes. Returns how many pairs the record
    now holds.

    Replaces the whole table every run rather than accumulating. That means a
    pattern that stops recurring drops out on its own once its evidencing
    events age past retention — the design's "pruned by support" (§6.1) as a
    side effect of the recompute, not a separate step — and a backfill landing
    historical events late is picked up cleanly next run, with no watermark to
    get wrong.

    Counts DISTINCT A-events that had at least one B-event follow within the
    window, not every matching B-event — a single A followed by B flickering
    three times counts as one occurrence, matching "43 times" as a count of
    occurrences, not of events."""
    window = RELATION_WINDOW_SECONDS if window_seconds is None else int(window_seconds)
    support = RELATION_MIN_SUPPORT if min_support is None else int(min_support)
    try:
        conn = _connect()
        since = _now() - RELATION_SCAN_DAYS * 86400 if RELATION_SCAN_DAYS else 0
        counts = _relation_counts(conn, window, since)
        rows = [(a, b, n, first, last)
                for (a, b), (n, first, last) in counts.items() if n >= support]

        # Only now is the write lock taken, and only for as long as these two
        # statements need it. Nothing that scans the record may happen between
        # them — see _relation_counts.
        def replace(c):
            c.execute("DELETE FROM relations")
            if rows:
                c.executemany(
                    "INSERT INTO relations (entity_a, entity_b, count, since, until)"
                    " VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
        _write(conn, replace)
        return len(rows)
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not scan relations: {e}")
        return 0


def relations(entity_id: str = None, limit: int = 50) -> list:
    """Observed co-occurrences, strongest first: which entity tends to change
    shortly after which. With entity_id, only pairs where it is the trigger
    (entity_a) — "what usually follows this"."""
    try:
        conn = _connect()
        where = ""
        params = []
        if entity_id:
            where = " WHERE ea.entity_id = ?"
            params.append(entity_id)
        rows = conn.execute(
            "SELECT ea.entity_id AS entity_a, eb.entity_id AS entity_b,"
            "       r.count AS count, r.since AS since, r.until AS until"
            " FROM relations r"
            " JOIN entities ea ON ea.id = r.entity_a"
            " JOIN entities eb ON eb.id = r.entity_b"
            + where +
            " ORDER BY r.count DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logger.error(f"RECORD: could not read relations: {e}")
        return []
