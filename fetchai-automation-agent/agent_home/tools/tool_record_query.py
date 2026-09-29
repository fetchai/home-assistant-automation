"""The free-hand analyst's read tool: one SQL SELECT over the behavioural record.

Proposal F-1 (docs/PROPOSAL-FREE-HAND-ANALYST.md). The other record tools
(tools/tool_record.py) answer questions we chose in advance — a profile, the
relations table — and a pattern outside them is invisible: "device B stays on
while sensor A says the room is empty" is an ABSENCE of co-change, which no
aggregate here shows. SQL lets the model ask the question it actually has.

Safety is structural, not advisory — nothing in the tool's description is
relied on to keep the model from writing. On EVERY call, in this order:

1. A fresh connection to `file:<record.DB_PATH>?mode=ro`, autocommit, never the
   record's shared thread-local one. A read-only connection in WAL mode never
   takes the write lock, so CLAUDE.md's "never hold the record's write lock
   while scanning" holds by construction, and a read succeeds while the
   observation thread is mid-write.
2. The TEMP table of watched entity ids and the TEMP views are created, THEN
   `PRAGMA query_only = ON` — in that order because SQLite counts creating a
   temp object as a write and refuses it once query_only is on (the proposal
   listed them the other way round; see its Deviations section). Nothing
   model-written runs between the two.
3. The authorizer is installed after both, so it never has to allow our own
   setup. It allows SELECT, RECURSIVE, reads of `ev` and `rel` (and of
   ephemeral CTE/subquery results, which SQLite reports with no database
   name), reads the watch-filtered inner views make, and the functions in
   _FUNCTIONS. Everything else is denied — every write, ATTACH, PRAGMA
   (including pragma table-valued functions, which also arrive as a read of a
   table not allowed), DDL, transactions, load_extension, any function not
   listed, reading sqlite_master, and reading events / entities / relations
   directly. A denial is remembered so the answer can NAME what was refused
   rather than hand the model SQLite's bare "not authorized".
4. A progress handler aborts the statement past QUERY_TIMEOUT_SECONDS of wall
   time, fetching included. docs/TODO.md: the agent froze on 2026-09-21 with a
   worker thread that never returned and no one knows why, so a tool that runs
   model-written SQL bounds its own time instead of trusting the query.
5. The SQL must be exactly one statement, checked before it is executed.
6. Rows are fetched with fetchmany, capped by row count and by the size of the
   serialised answer; `truncated` says whenever either cap withheld a row.
7. The connection is closed in a finally, on every path.

What is readable, and why:
- `ev` — events joined to entity ids, with `local_time`, for entities watched
  NOW. The one view the model needs: it never has to join integer keys or
  convert epochs, which is where a hand-written query goes wrong silently
  rather than loudly.
- `rel` — the relations table with both entity ids joined, kept only when BOTH
  are watched now. The directed co-change counts core/record.py's
  scan_relations already computed; a cheap starting point for "what moves
  together" before querying `ev` for it.
- `events`, `entities`, `relations` are NOT readable directly (2.21.3).
  Unwatching an entity does not delete its rows — only retention and the size
  cap do — so the record holds entities that USED to be watched, for up to
  RETENTION_DAYS. F-3's bound is "watched", so the filter is the watchlist as
  it is at the call, applied in the views, and a direct read of a table would
  walk round it.
- How "only through the views" is enforced: SQLite's authorizer passes the
  inner-most view or trigger a read is made for — but a CTE's name is passed
  the same way, so `WITH ev AS (SELECT * FROM events) SELECT * FROM ev` looks
  exactly like a read through the real `ev`. The filtering is therefore done
  by inner views named with a random token fresh per call (`ev` and `rel` are
  plain SELECTs over them), and the tables are readable only when the reader
  is one of those names. The model is never told them, cannot read
  sqlite_temp_master, and a name that leaked would be dead by the next call.
- `profiles` is NOT readable. It holds collapsed history (JSON arrays per hour
  and day) that needs json functions to read and that core/record.py's
  `between` never returns — so a figure from it could not be drawn by the chart
  that must back every idea (F-7). An argument from data no picture can show is
  the thing this proposal exists to stop.

What may leave the house (F-3, a widening of design §10.1): the rows as
returned, within the caps, through `disclosure.emit` like every other answer.
The views show only entities watched at the call — the watchlist is the bound.

Analyst-only: not in agent_runtime.tools (chat does not get it, proposal §6).
"""
import secrets
import sqlite3
import time
from typing import Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import disclosure
from core import record
from core import watchlist

# Wall time one query may run, fetching included. Every legitimate question
# about a record of watched entities finishes in well under a second on the
# indexes events has; 2 s leaves room for a slow board without letting a
# runaway cross join or unbounded recursive CTE hold a worker for longer than
# a person would notice.
QUERY_TIMEOUT_SECONDS = 2

# How long to wait on SQLITE_BUSY when opening/reading. A read-only WAL reader
# is only ever busy during WAL recovery or a truncating checkpoint, both brief;
# waiting longer would be the unbounded wait the freeze taught us not to add.
CONNECT_TIMEOUT_SECONDS = 1.0

# Rows one answer may carry. Matches disclosure.MAX_ITEMS, past which the
# boundary would cut the list itself — and a model that needs more than 500
# rows needs an aggregate, not a longer list.
QUERY_MAX_ROWS = 500

# Rows returned when the model does not ask. The analyst's budget is 20
# queries a run (F-5); 20 full-size answers would fill its context with rows.
DEFAULT_MAX_ROWS = 100

# Size of the serialised answer. 32 KB is roughly 8k tokens — one answer may be
# large, but not so large that one careless SELECT * spends the whole run.
QUERY_MAX_BYTES = 32768

# Room kept for the envelope (keys, columns, note) when rows fill the budget.
_ENVELOPE_BYTES = 512

# Longest string or blob SQLite may build. Time alone does not bound memory:
# a recursive CTE doubling a string with replace() reaches a gigabyte in thirty
# steps, well inside 2 s. 1 MB is far past any value the record holds.
_MAX_VALUE_BYTES = 1_000_000

# VM instructions between progress-handler calls: often enough that the
# timeout is honoured to a few milliseconds, rare enough to cost nothing.
_PROGRESS_STEPS = 1000

# The watched entity ids at the call, filled before query_only. Only the inner
# views below read it.
_WATCHED_TABLE = "CREATE TEMP TABLE watched (entity_id TEXT PRIMARY KEY)"

# Created on the read-only connection before the authorizer is installed.
# {ev} and {rel} are the per-call random names of the inner views (docstring:
# a CTE can borrow any name the model knows).
_VIEWS = (
    "CREATE TEMP VIEW {ev} AS"
    " SELECT n.entity_id AS entity_id, e.ts AS ts,"
    " datetime(e.ts, 'unixepoch', 'localtime') AS local_time,"
    " e.state AS state, e.prev_state AS prev_state,"
    " e.prev_duration AS prev_duration, e.source AS source"
    " FROM main.events e JOIN main.entities n ON n.id = e.entity"
    " WHERE n.entity_id IN (SELECT entity_id FROM temp.watched)",
    "CREATE TEMP VIEW {rel} AS"
    " SELECT a.entity_id AS entity_a, b.entity_id AS entity_b,"
    " r.count AS count, r.since AS since, r.until AS until"
    " FROM main.relations r"
    " JOIN main.entities a ON a.id = r.entity_a"
    " JOIN main.entities b ON b.id = r.entity_b"
    " WHERE a.entity_id IN (SELECT entity_id FROM temp.watched)"
    " AND b.entity_id IN (SELECT entity_id FROM temp.watched)",
    "CREATE TEMP VIEW ev AS SELECT * FROM temp.{ev}",
    "CREATE TEMP VIEW rel AS SELECT * FROM temp.{rel}",
)

# (database, table) pairs a SQLITE_READ may name whoever reads them. The
# database is part of the key so the views are only ever the temp ones
# created above.
_READABLE = frozenset({("temp", "ev"), ("temp", "rel")})

# The record's own tables: readable only by the inner views; named in the
# refusal so the model is pointed at the views instead.
_TABLES = frozenset({"events", "entities", "relations"})

# Every function a query may call. `cast`, `case`, `between`, `in` and `||`
# are syntax, not functions, and never reach the authorizer. `like`/`glob` do:
# they are the LIKE/GLOB operators, which a model filtering 'light.%' needs.
# `substring`, `ltrim`/`rtrim`, `format`, `if` and `string_agg` are SQLite's
# aliases or siblings of listed ones; `unixepoch` is how 'now' becomes an
# epoch to compare `ts` with.
_FUNCTIONS = frozenset({
    # date and time
    "date", "time", "datetime", "julianday", "strftime", "unixepoch", "timediff",
    # aggregates
    "count", "sum", "avg", "min", "max", "total", "group_concat", "string_agg",
    # scalars
    "abs", "round", "coalesce", "ifnull", "nullif", "iif", "if", "instr", "substr",
    "substring", "length", "lower", "upper", "trim", "ltrim", "rtrim", "replace",
    "printf", "format", "like", "glob",
    # window functions
    "row_number", "rank", "dense_rank", "lag", "lead", "first_value", "last_value",
    "ntile",
})

# Action codes → what the refusal says. Anything not here is named by its code.
_ACTIONS = {
    sqlite3.SQLITE_INSERT: "writing (INSERT)",
    sqlite3.SQLITE_UPDATE: "writing (UPDATE)",
    sqlite3.SQLITE_DELETE: "writing (DELETE)",
    sqlite3.SQLITE_ATTACH: "ATTACH",
    sqlite3.SQLITE_DETACH: "DETACH",
    sqlite3.SQLITE_PRAGMA: "PRAGMA",
    sqlite3.SQLITE_TRANSACTION: "a transaction",
    sqlite3.SQLITE_SAVEPOINT: "a savepoint",
    sqlite3.SQLITE_ALTER_TABLE: "ALTER TABLE",
    sqlite3.SQLITE_ANALYZE: "ANALYZE",
    sqlite3.SQLITE_REINDEX: "REINDEX",
}
_SCHEMA_TABLES = {"sqlite_master", "sqlite_temp_master", "sqlite_schema", "sqlite_temp_schema"}
_DDL = {name: getattr(sqlite3, name) for name in dir(sqlite3)
        if name.startswith(("SQLITE_CREATE_", "SQLITE_DROP_"))}


def _refusal(action, arg1, arg2) -> str:
    """What one denied authorizer call was, in words the model can act on."""
    if action == sqlite3.SQLITE_FUNCTION:
        return f"the function {arg2}() is not allowed"
    if action == sqlite3.SQLITE_READ:
        if arg1 in _TABLES:
            return f"reading {arg1} directly is not allowed — query the views ev and rel"
        return f"reading {arg1} is not allowed"
    # CREATE and DROP ask to write the schema table before they ask for
    # themselves, and the first denial ends the prepare — so this is the call
    # a CREATE TABLE is refused on, and "INSERT into sqlite_master" would
    # name the wrong thing.
    if arg1 in _SCHEMA_TABLES and action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
                                             sqlite3.SQLITE_DELETE):
        return "changing the schema (CREATE / DROP / ALTER) is not allowed"
    for name, code in _DDL.items():
        if code == action:
            return f"{name[len('SQLITE_'):].replace('_', ' ')} is not allowed"
    what = _ACTIONS.get(action, f"operation {action}")
    return f"{what} {arg1 or ''}".rstrip() + " is not allowed"


def _authorizer(refused: list, inner: frozenset):
    """The authorizer callback, recording the first thing it refused.
    `inner` holds this call's random names of the watch-filtered views."""
    def check(action, arg1, arg2, db_name, source):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            # No database name: a CTE or a subquery's ephemeral result, which
            # can only hold what was read from an allowed table to build it.
            if db_name is None or (db_name, arg1) in _READABLE:
                return sqlite3.SQLITE_OK
            # The inner views hold only watched rows, whoever reads them; the
            # tables and the watched list only when an inner view is the
            # reader — `source` is SQLite's inner-most view, and only these
            # views bear these names (a CTE would have to guess the token).
            if db_name == "temp" and arg1 in inner:
                return sqlite3.SQLITE_OK
            if source in inner and ((db_name == "main" and arg1 in _TABLES)
                                    or (db_name, arg1) == ("temp", "watched")):
                return sqlite3.SQLITE_OK
        elif action == sqlite3.SQLITE_FUNCTION:
            if (arg2 or "").lower() in _FUNCTIONS:
                return sqlite3.SQLITE_OK
        if not refused:
            refused.append(_refusal(action, arg1, arg2))
        return sqlite3.SQLITE_DENY
    return check


def _one_statement(sql: str):
    """The single statement in `sql`, or None if it holds zero or several.

    Python's execute() would also refuse a second statement, but only as a
    ProgrammingError after preparing the first; checking here means nothing
    runs at all. The newline before the added ';' keeps a trailing `-- comment`
    from swallowing it."""
    text = sql.strip()
    if not text:
        return None
    if not text.endswith(";"):
        text += "\n;"
    if not sqlite3.complete_statement(text):
        return None
    # The first ';' that completes a statement ends it; anything after that
    # but whitespace is a second statement.
    for i, ch in enumerate(text):
        if ch == ";" and sqlite3.complete_statement(text[:i + 1]):
            if text[i + 1:].strip():
                return None
            return text[:i + 1]
    return None


def _value(value):
    """A cell the boundary can serialise. Nothing in the record is a blob, but
    CAST(... AS BLOB) makes one."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob, {len(value)} bytes>"
    return value


def _run_query(sql: str, max_rows: int) -> dict:
    """Runs one statement under F-1's guards and returns the payload."""
    refused = []
    deadline = [None]
    timed_out = [False]

    def progress():
        if deadline[0] is not None and time.monotonic() > deadline[0]:
            timed_out[0] = True
            return 1
        return 0

    try:
        conn = sqlite3.connect(f"file:{record.DB_PATH}?mode=ro", uri=True,
                               timeout=CONNECT_TIMEOUT_SECONDS, isolation_level=None)
    except sqlite3.Error as e:
        return {"error": f"the record could not be opened read-only: {e}"}
    try:
        conn.row_factory = sqlite3.Row
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_VALUE_BYTES)
        conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        token = secrets.token_hex(8)
        names = {"ev": f"_ev_{token}", "rel": f"_rel_{token}"}
        conn.execute(_WATCHED_TABLE)
        conn.executemany("INSERT OR IGNORE INTO temp.watched VALUES (?)",
                         [(e,) for e in watchlist.watched_entities()])
        for view in _VIEWS:
            conn.execute(view.format(**names))
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer(refused, frozenset(names.values())))
        conn.set_progress_handler(progress, _PROGRESS_STEPS)

        deadline[0] = time.monotonic() + QUERY_TIMEOUT_SECONDS
        try:
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description or ()]
            fetched = cursor.fetchmany(max_rows + 1)
        except sqlite3.Error as e:
            if refused:
                return {"error": f"refused: {refused[0]}. Only one read-only SELECT over "
                                 f"the views ev and rel is allowed."}
            if timed_out[0]:
                return {"error": f"the query took longer than {QUERY_TIMEOUT_SECONDS:g} s "
                                 f"and was stopped"}
            return {"error": f"the query failed: {e}"}

        withheld_by_rows = len(fetched) > max_rows
        fetched = fetched[:max_rows]
        budget = QUERY_MAX_BYTES - _ENVELOPE_BYTES - len(disclosure.emit(columns))
        rows = []
        for row in fetched:
            cells = [_value(v) for v in row]
            size = len(disclosure.emit(cells)) + 2  # the ", " between rows
            if size > budget:
                break
            budget -= size
            rows.append(cells)
        withheld_by_bytes = len(rows) < len(fetched)

        payload = {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": withheld_by_rows or withheld_by_bytes,
        }
        if withheld_by_bytes:
            payload["note"] = (f"More rows matched; stopped at {len(rows)} to keep the answer "
                               f"under {QUERY_MAX_BYTES // 1024} KB. Select fewer columns or "
                               f"aggregate.")
        elif withheld_by_rows:
            payload["note"] = (f"More rows matched; stopped at max_rows={max_rows} "
                               f"(ceiling {QUERY_MAX_ROWS}). Aggregate, or narrow the window.")
        return payload
    except sqlite3.Error as e:
        return {"error": f"the record could not be prepared for querying: {e}"}
    finally:
        conn.close()


# Worked queries for the description, kept as constants so a test runs each
# one through the authorizer: an example the tool refuses teaches the model
# the tool is broken.
EXAMPLE_ONS_PER_DAY = (
    "SELECT date(ts, 'unixepoch', 'localtime') AS day, count(*) AS ons FROM ev"
    " WHERE entity_id = 'light.kitchen' AND state = 'on'"
    " AND ts >= unixepoch('now', '-14 days')"
    " GROUP BY day ORDER BY day"
)
# 2.22.1: each stretch must be CLOSED BY A CHANGE, not by a gap. Field run 2's
# 466 "on with no motion" hours were this query, copied faithfully: every long
# stretch of the old test light ended in an `unknown` row (a Home Assistant
# restart with the device gone), so the device had stopped reporting while
# showing "on" — nobody left a light on. A stretch ended by a gap proves
# nothing about how long the state held, so the example leaves it out and the
# description says why.
EXAMPLE_ON_WHILE_OFF = (
    "WITH a_off AS (SELECT ts - prev_duration AS s, ts AS e FROM ev"
    " WHERE entity_id = 'binary_sensor.office_occupancy' AND prev_state = 'off'"
    " AND state NOT IN ('unknown', 'unavailable')),"
    " b_on AS (SELECT ts - prev_duration AS s, ts AS e FROM ev"
    " WHERE entity_id = 'switch.office_fan' AND prev_state = 'on'"
    " AND state NOT IN ('unknown', 'unavailable'))"
    " SELECT round(sum(min(a.e, b.e) - max(a.s, b.s)) / 3600.0, 2) AS hours"
    " FROM a_off a JOIN b_on b ON a.s < b.e AND b.s < a.e"
)
EXAMPLE_DAY_HOUR = (
    "SELECT strftime('%w', ts, 'unixepoch', 'localtime') AS weekday,"
    " strftime('%H', ts, 'unixepoch', 'localtime') AS hour, count(*) AS n FROM ev"
    " WHERE entity_id = 'light.kitchen' AND state = 'on'"
    " GROUP BY weekday, hour ORDER BY weekday, hour"
)
# 2.19.1: field run 1 never queried the porch light's ~19:00 weekday habit.
# (c) shows WHICH hours; this shows whether the time is steady, day by day —
# the figure a "switch it on at 19:00" idea stands on.
EXAMPLE_FIRST_ON_PER_DAY = (
    "SELECT date(ts, 'unixepoch', 'localtime') AS day,"
    " min(strftime('%H:%M', ts, 'unixepoch', 'localtime')) AS first_on FROM ev"
    " WHERE entity_id = 'light.porch' AND state = 'on'"
    " AND strftime('%w', ts, 'unixepoch', 'localtime') BETWEEN '1' AND '5'"
    " AND ts >= unixepoch('now', '-30 days')"
    " GROUP BY day ORDER BY day"
)


class _RecordQueryInput(BaseModel):
    """Input schema for one read-only query."""
    sql: str = Field(
        description="Exactly one SQLite SELECT statement (WITH ... SELECT allowed) over "
                    "the views ev and rel.",
        default="",
    )
    max_rows: int = Field(
        description=f"How many rows to return at most. Defaults to {DEFAULT_MAX_ROWS}, "
                    f"maximum {QUERY_MAX_ROWS}.",
        default=DEFAULT_MAX_ROWS,
    )


class tool_record_query(BaseTool):
    """LLM tool running one read-only SELECT over the behavioural record."""
    name: str = "Query the behavioural record"
    description: str = (
        "Run ONE read-only SQLite SELECT over the add-on's behavioural record: every state change "
        "of the entities the user watches now, from when watching started. Use it to find "
        "habits no ready-made aggregate shows, including absences (a device left on while a room "
        "is empty).\n"
        "View `ev` — one row per state change: entity_id TEXT, ts INTEGER (epoch seconds, UTC), "
        "local_time TEXT ('YYYY-MM-DD HH:MM:SS', the home's local time), state TEXT, prev_state "
        "TEXT, prev_duration INTEGER (seconds the PREVIOUS state lasted, so a row with "
        "prev_state='on' closes an 'on' stretch from ts - prev_duration to ts), source TEXT "
        "('live', or 'history'/'statistics' for backfilled rows). The state still running has no "
        "row closing it yet. A row whose state is 'unknown' or 'unavailable' is a GAP: Home "
        "Assistant had no reading from then on (a restart, a device offline). The stretch it "
        "closes ended in that gap, not in a change, so how long that state really held is not "
        "known — leave stretches closed by a gap out of durations and overlaps, as example (b) "
        "does. A state that held for more than a day and then ended in a gap is usually a device "
        "that stopped reporting, not a habit. Group by local days/hours with date(ts,'unixepoch','localtime') and "
        "strftime(...,'localtime'); compare ts with unixepoch('now','-7 days').\n"
        "View `rel` — how often entity_b changed shortly after entity_a changed: entity_a, "
        "entity_b, count, since, until (epoch seconds).\n"
        f"Limits: one statement over ev and rel only; no writes, PRAGMA or ATTACH; date/time, aggregate, window and "
        f"basic string functions only; {QUERY_TIMEOUT_SECONDS:g} s per query; at most "
        f"{QUERY_MAX_ROWS} rows and {QUERY_MAX_BYTES // 1024} KB per answer — `truncated` true "
        f"means rows were left out, so aggregate instead.\n"
        f"Examples. (a) Changes into 'on' per day: {EXAMPLE_ONS_PER_DAY}\n"
        f"(b) Hours the fan was on while the office was unoccupied (overlap of closed stretches): "
        f"{EXAMPLE_ON_WHILE_OFF}\n"
        f"(c) Day-of-week × hour histogram (weekday 0 = Sunday): {EXAMPLE_DAY_HOUR}\n"
        f"(d) The time it was first switched on each weekday (a steady time is a schedule): "
        f"{EXAMPLE_FIRST_ON_PER_DAY}"
    )
    args_schema: Type[BaseModel] = _RecordQueryInput

    def _run(self, sql: str = "", max_rows: int = DEFAULT_MAX_ROWS) -> str:
        """Returns the query's columns and rows, or an error naming the refusal."""
        statement = _one_statement(sql or "")
        if statement is None:
            return disclosure.emit({"error": "send exactly one complete SELECT statement"})
        try:
            max_rows = int(max_rows)
        except (TypeError, ValueError):
            max_rows = DEFAULT_MAX_ROWS
        max_rows = max(1, min(max_rows, QUERY_MAX_ROWS))
        return disclosure.emit(_run_query(statement, max_rows))
