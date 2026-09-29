"""What the agent did, and what it still has to say.

Two tables, one database, because they are written together. Approving a change
writes the change AND queues the message that announces it, and a home where
those two disagree — an automation written with nothing to show for it, or a
notification about a write that never happened — is worse than either failing.
One file, one transaction.

Separate from record.db on purpose. The behavioural record is pruned at ninety
days; none of this may ever be. What the agent wrote into someone's home is the
audit trail (design §4: the panel is the "local record of everything proposed,
applied and rolled back"), and an audit trail with a retention window is not one.

`changes` is the design's recorded approval (§5): nothing reaches Home Assistant
without a row here, written BEFORE the user is asked and updated when they
answer. It carries the inverse operation alongside, so that reversibility is a
property of the data rather than a promise about future code — and core/rollback.py
is the code that executes them.

For a change the agent raised itself the row is more than the record of the
question: it IS the question. There is no session holding a copy, so core/gate.py
reconstructs what it needs to execute from these columns whenever the answer
arrives — days later, or after a restart. That is why `source` has a third value
that names no surface at all, and why the startup sweep below leaves those rows
alone.

A rollback gets a row of its OWN, linked to the change it undoes by `undo_of`,
rather than rewriting that change's outcome. The reasoning is in
core/rollback.py, where the choice is made; what it costs here is one extra
column each way — `undo_of` on the undo, `undone_by` on the original — and what
it buys is that "applied" never has to stop meaning applied.

`outbox` is the delivery layer's queue (§4). The core writes a message and never
learns which channel carried it; the consumers in core/delivery.py stamp their
own column when they take one. Rows are kept after delivery rather than deleted:
the panel shows what was sent as much as what is waiting.

In /data for the same reason pairing.json and record.db are — /app is part of
the image and is wiped by every add-on rebuild.
"""
from datetime import datetime, timezone
import json
import logging
import os
import sqlite3
import threading

from core import settings

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(settings.DATA_DIR, "activity.db")

# One connection per thread, as in core/record.py: the observation thread, the
# uAgent event loop and the executor threads all reach this store.
_local = threading.local()

# Which column each delivery channel stamps. A dict rather than an f-string:
# the channel name reaches SQL, and it is not going to do so unchecked.
_CHANNELS = {
    "push": "pushed_at",     # Home Assistant notification — urgent messages only
    "chat": "collected_at",  # ASI1, pulled when the user asks
    "panel": "seen_at",      # the Activity tab, on open
}

# What the `actor` column holds for a change with no session behind it. A
# sentinel rather than "": an empty actor is what a chat-raised change recorded
# before session ids were passed down looks like, and the two must not be
# confused. It cannot collide with a real session id — uAgent addresses start
# with 'agent1', the panel makes 'session_xxxx'.
#
# NOT a way to derive AGENT_SOURCE any more. It used to be: source_for(sid)
# special-cased this exact string, which meant a client posting session_id
# "agent:self" to /rest/post got its own chat-raised proposal journaled as
# agent-raised — escaping the restart sweep AND becoming answerable twice, once
# by change_id (because pending_for's source check passed) and once by session
# (because park_approval still parks it in that session's memory regardless of
# what source it was recorded under). See gate.propose, which is now the only
# way to record AGENT_SOURCE — by stating it explicitly, not by encoding it in
# a session id that a client controls. rest_chat.handle_post rejects this value
# as an inbound session_id outright, cheap defence in depth on top of that.
AGENT_SESSION = "agent:self"
AGENT_SOURCE = "agent"

# A change another uAgent proposed (core/peers.py, phase 2 of agent-to-agent),
# its address in `actor`. Granted exactly the way AGENT_SOURCE is — stated by
# gate.propose, never derived from a string a client sends; source_for reads
# every agent1… session as "asi1" and cannot produce it.
PEER_SOURCE = "peer"

# The sources whose proposals live in the journal row alone — nothing parked in
# any session memory — so they survive a restart and are answered by change_id.
SESSIONLESS_SOURCES = frozenset({AGENT_SOURCE, PEER_SOURCE})

# The one place "asked from" is phrased for a person, so the panel and the chat
# tool render the same three words rather than two ternaries that can drift —
# see FIX 4. 'agent' must never collapse into either surface: a change nobody
# asked for is not "the panel" wearing a false audit trail.
SOURCE_LABELS = {
    "asi1": "ASI1 chat",
    "panel": "the panel",
    AGENT_SOURCE: "the agent itself",
    PEER_SOURCE: "another agent",
}


def source_label(source: str) -> str:
    """The one phrase for where a change was asked from, or the panel default
    for anything unrecognised — there is no fourth surface today."""
    return SOURCE_LABELS.get(source, SOURCE_LABELS["panel"])

_SCHEMA = (
    # One row per destructive operation, from the moment it is proposed. The row
    # exists before the user has answered, which is what lets the panel show
    # "awaiting your answer" for a request that arrived over ASI1.
    "CREATE TABLE IF NOT EXISTS changes ("
    " id INTEGER PRIMARY KEY,"
    " proposed_at INTEGER NOT NULL,"
    " decided_at INTEGER,"
    " source TEXT NOT NULL DEFAULT 'panel',"
    " actor TEXT NOT NULL DEFAULT '',"
    " tool TEXT NOT NULL,"
    " args TEXT,"
    " target TEXT,"
    " decision TEXT NOT NULL DEFAULT 'proposed',"
    " status TEXT,"
    " result TEXT,"
    " old_config TEXT,"
    " new_config TEXT,"
    # The operation that undoes this one, as JSON, or NULL where nothing does.
    " inverse TEXT,"
    " reversibility TEXT NOT NULL DEFAULT 'one_way',"
    # The two halves of a rollback link. `undo_of` is set on the rollback's own
    # row and names the change it reverses; `undone_by` is set on that change
    # once the rollback has actually succeeded. Both, because either alone
    # leaves a question the panel has to answer with a scan: which change did
    # this undo, and has this change been undone.
    " undo_of INTEGER,"
    " undone_by INTEGER,"
    # The outbox message that asked the question, so that whoever answers can
    # withdraw it. On the session-backed path it is also held in memory; on the
    # agent-raised one this column is the only copy there is, because there is
    # no session to hold anything.
    " notice_id INTEGER)",

    "CREATE INDEX IF NOT EXISTS idx_changes_proposed ON changes(proposed_at)",

    "CREATE TABLE IF NOT EXISTS outbox ("
    " id INTEGER PRIMARY KEY,"
    " created_at INTEGER NOT NULL,"
    " kind TEXT NOT NULL,"
    " urgency TEXT NOT NULL DEFAULT 'normal',"
    # Plain text, because §4 makes the text rendering the design constraint and
    # not the fallback: a message that cannot be read in a chat line does not
    # ship. `payload` carries the structure a richer surface can use instead.
    " title TEXT NOT NULL DEFAULT '',"
    " body TEXT NOT NULL DEFAULT '',"
    " payload TEXT,"
    " pushed_at INTEGER,"
    " collected_at INTEGER,"
    " seen_at INTEGER)",

    "CREATE INDEX IF NOT EXISTS idx_outbox_created ON outbox(created_at)",
)


def _now():
    return int(datetime.now(tz=timezone.utc).timestamp())


# Columns added after the table shipped. CREATE TABLE IF NOT EXISTS does nothing
# to a store that already exists, and this one may never be dropped and rebuilt —
# it is the audit trail, and an audit trail that starts again at every upgrade is
# not one.
_ADDED_COLUMNS = (
    ("undo_of", "INTEGER"),
    ("undone_by", "INTEGER"),
    ("notice_id", "INTEGER"),
)


def _migrate(conn):
    """Adds any column this version expects and an older store lacks."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(changes)")}
    for column, kind in _ADDED_COLUMNS:
        if column not in have:
            conn.execute(f"ALTER TABLE changes ADD COLUMN {column} {kind}")
            logger.info(f"ACTIVITY: added changes.{column} to an existing store")


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
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for statement in _SCHEMA:
        conn.execute(statement)
    _migrate(conn)
    conn.commit()
    _local.conn = conn
    _local.path = DB_PATH
    return conn


def close():
    """Drops this thread's connection. Tests call it when they repoint DB_PATH;
    nothing in production needs it."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None
    _local.path = None


def source_for(session_id: str) -> str:
    """Which REAL session a change came from: 'asi1' or 'panel', never 'agent'.

    Derived rather than passed down through five call sites, because it is
    already unambiguous for a genuine session: handlers/chat_handlers.py uses
    the sender's uAgent address as the session id, and every uAgent address
    starts with 'agent1'. The panel makes its own 'session_xxxx' in useChat.js.

    This function deliberately cannot produce AGENT_SOURCE, for any input. It
    used to special-case session_id == AGENT_SESSION — but every session_id
    reaching here came from somewhere outside this process (handle_post takes
    it straight off the client request), so that special case meant a client
    could forge the sentinel and get its own chat-raised proposal journaled as
    agent-raised. Retracted: AGENT_SOURCE is granted exactly one way now,
    gate.propose stating it explicitly, never by a string this function reads.
    An unrecognised session id — including the sentinel itself, should it ever
    reach here — falls to 'panel' like any other, which is at least an honest
    "a surface answered" rather than a manufactured "nobody did".
    """
    sid = str(session_id or "")
    return "asi1" if sid.startswith("agent1") else "panel"


def _row(row) -> dict:
    """A sqlite3.Row as a plain dict, with the JSON columns decoded."""
    out = dict(row)
    for key in ("args", "inverse", "payload"):
        if key in out and out[key]:
            try:
                out[key] = json.loads(out[key])
            except (TypeError, ValueError):
                pass
    return out


# --------------------------------------------------------------------------
# changes — the recorded approval
# --------------------------------------------------------------------------
def record_proposal(tool: str, args: dict, session_id: str = "", target: str = "",
                    old_config: str = "", new_config: str = "",
                    inverse=None, reversibility: str = "one_way", source: str = None,
                    actor: str = None):
    """Writes the row for a change about to be put to the user. Returns its id,
    or None if the store could not be written.

    `source` is normally left unset, and the surface is derived from
    `session_id` the way it always has been — that path is for a real
    conversation, and source_for is good at reading one. gate.propose passes
    `source` explicitly instead, because it has no session at all to derive
    from and must not manufacture one that only LOOKS like it does (see FIX 2
    on core/activity.py's AGENT_SESSION comment). Passing both is not a case
    any caller needs: an explicit `source` always wins, and `session_id` still
    fills the `actor` column either way — gate.propose leaves it at "" because
    there genuinely is no session behind it. `actor`, when given, is written
    instead of `session_id`: a PEER_SOURCE change records the proposing agent's
    address there, which is who asked — not a session anything could resume.

    Never raises. A journal that cannot be written must not stop the home being
    controllable — but the caller is told, because a write the gate could not
    record is one the gate should think twice about.
    """
    try:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO changes (proposed_at, source, actor, tool, args, target,"
            " decision, old_config, new_config, inverse, reversibility)"
            " VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?)",
            (_now(), source if source is not None else source_for(session_id),
             str(actor if actor is not None else (session_id or "")), str(tool),
             json.dumps(args or {}), str(target or ""), old_config or "", new_config or "",
             json.dumps(inverse) if inverse else None, reversibility),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not record the proposal of {tool}: {e}")
        return None


def claim_decision(change_id, decision: str) -> bool:
    """Atomically settles `decision` on a still-open change, leaving `status`
    NULL. Returns whether THIS call won the claim.

    FIX 1. Two callers can both pass the `decision == 'proposed'` read that
    happens before this — a double-tapped Apply, or the approval card and the
    panel answering the same agent-raised proposal — because that read and the
    write that follows it are not one atomic step anywhere upstream. This is
    the actual boundary: a single UPDATE with `decision = 'proposed'` in its
    OWN WHERE clause, which SQLite serialises against every other writer on
    this connection's database. Exactly one concurrent call can match it;
    every other one touches zero rows and must execute nothing.

    `status` is deliberately left NULL rather than settled in the same
    statement. A claimed-but-not-yet-executed row is "we know it was answered,
    we do not yet know whether the write landed" — the same pair
    abandon_running_rollbacks() already reads for a rollback caught mid-flight,
    and abandon_orphaned_approvals() below reads it for exactly this row.
    """
    if not change_id:
        return False
    try:
        conn = _connect()
        cur = conn.execute(
            "UPDATE changes SET decided_at = ?, decision = ? WHERE id = ? AND decision = 'proposed'",
            (_now(), str(decision), int(change_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"ACTIVITY: could not claim change {change_id}: {e}")
        return False


def attach_notice(change_id, notice_id) -> bool:
    """Points a proposal at the outbox message that asked its question.

    Written after the row rather than with it, because the message names the
    change and so cannot exist before it. What it buys is that answering can
    withdraw the question from a surface that was not there when it was asked —
    which is the whole point of a proposal that outlives the session, or that
    never had one.
    """
    if not change_id or not notice_id:
        return False
    try:
        conn = _connect()
        conn.execute("UPDATE changes SET notice_id = ? WHERE id = ?",
                     (int(notice_id), int(change_id)))
        conn.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"ACTIVITY: could not link change {change_id} to message {notice_id}: {e}")
        return False


def record_decision(change_id, decision: str, status: str = None,
                    result: str = "", inverse=None) -> bool:
    """Closes a proposed change: approved, rejected or expired.

    `inverse` is accepted here as well as at proposal time because a creation's
    inverse names the automation id, and that id does not exist until the create
    has run. Passing None leaves whatever was recorded earlier untouched.
    """
    if not change_id:
        return False
    try:
        conn = _connect()
        if inverse is None:
            conn.execute(
                "UPDATE changes SET decided_at = ?, decision = ?, status = ?, result = ?"
                " WHERE id = ?",
                (_now(), decision, status, str(result or "")[:2000], int(change_id)),
            )
        else:
            conn.execute(
                "UPDATE changes SET decided_at = ?, decision = ?, status = ?, result = ?,"
                " inverse = ?, reversibility = 'reversible' WHERE id = ?",
                (_now(), decision, status, str(result or "")[:2000],
                 json.dumps(inverse), int(change_id)),
            )
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not record the decision on change {change_id}: {e}")
        return False


def record_rollback(undo_of, tool: str, args: dict, session_id: str = "",
                    target: str = "", inverse=None, reversibility: str = "one_way"):
    """Opens the row for a rollback that is about to run. Returns its id, or None.

    Written BEFORE the inverse is executed, for the same reason a proposal is
    written before the user answers it: a rollback interrupted halfway leaves a
    trace rather than evaporating. It carries no 'proposed' stage because there
    is nothing to ask — see core/rollback.py on why the original approval
    authorises its own undo — so the decision is settled at birth and only the
    outcome is still open.
    """
    try:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO changes (proposed_at, source, actor, tool, args, target,"
            " decision, inverse, reversibility, undo_of)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pre_authorised', ?, ?, ?)",
            (_now(), source_for(session_id), str(session_id or ""), str(tool),
             json.dumps(args or {}), str(target or ""),
             json.dumps(inverse) if inverse else None, reversibility, int(undo_of)),
        )
        conn.commit()
        return cur.lastrowid
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"ACTIVITY: could not record the rollback of change {undo_of}: {e}")
        return None


def mark_undone(change_id, rollback_id) -> bool:
    """Points a change at the rollback that reversed it.

    Only ever called after that rollback succeeded. Note what this does NOT do:
    it leaves `status` reading 'applied', because the change WAS applied and no
    later event makes that untrue. The undo is the row `undone_by` names.
    """
    if not change_id or not rollback_id:
        return False
    try:
        conn = _connect()
        conn.execute("UPDATE changes SET undone_by = ? WHERE id = ?",
                     (int(rollback_id), int(change_id)))
        conn.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"ACTIVITY: could not link change {change_id} to rollback {rollback_id}: {e}")
        return False


def abandon_running_rollbacks() -> list:
    """Closes every rollback row that never reached an outcome, returning them.

    A rollback whose process died between the row and the answer is the one
    state the store cannot tell apart from one still in flight — and nothing is
    in flight at startup. Recorded as failed rather than quietly dropped: §11
    gives a failed rollback exactly one behaviour, and it is to be loud. The
    caller does the telling; this only makes the row honest.
    """
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM changes WHERE undo_of IS NOT NULL AND status IS NULL"
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE changes SET decided_at = ?, status = 'failed',"
                " result = 'The add-on restarted while this rollback was running.'"
                " WHERE undo_of IS NOT NULL AND status IS NULL",
                (_now(),),
            )
            conn.commit()
            logger.warning(f"ACTIVITY: {len(rows)} rollback(s) did not survive the restart")
        return [_row(r) for r in rows]
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not close interrupted rollbacks: {e}")
        return []


def abandon_orphaned_approvals() -> list:
    """Closes every APPROVAL claimed but never settled, returning them.

    Mirrors abandon_running_rollbacks() one row down: `decision` NOT IN
    ('proposed', 'expired') AND `status IS NULL` is claim_decision's "answered,
    outcome unknown" pair (FIX 1), the same shape a rollback caught mid-flight
    leaves on the `undo_of IS NOT NULL` side. The process died between claiming
    the row and gate.apply's final record_decision, and that is the one state
    nobody can tell "the write landed" from "it didn't" — §11's loud reading.

    Deliberately distinct from expire_stale_proposals(): that sweep only ever
    touches `decision = 'proposed'` rows, "nobody answered yet". This one only
    ever touches rows a claim already moved off 'proposed'. A row cannot be in
    both sets, and letting the two collapse into one query would let an
    unanswered proposal masquerade as a write of unknown outcome, or the other
    way round — two different stories that both need to stay tellable.

    `undo_of IS NULL` keeps this on the approval side of the journal; a
    rollback claimed but not settled is abandon_running_rollbacks()'s row to
    close, not this one's.
    """
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM changes WHERE undo_of IS NULL AND status IS NULL"
            " AND decision NOT IN ('proposed', 'expired')"
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE changes SET status = 'failed',"
                " result = 'The add-on restarted while this change was being applied.'"
                " WHERE undo_of IS NULL AND status IS NULL"
                " AND decision NOT IN ('proposed', 'expired')"
            )
            conn.commit()
            logger.warning(f"ACTIVITY: {len(rows)} approval(s) did not survive the restart mid-write")
        return [_row(r) for r in rows]
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not close interrupted approvals: {e}")
        return []


def expire_stale_proposals() -> int:
    """Closes every SESSION-BACKED change still awaiting an answer, returning
    how many.

    Called at startup. A parked approval lives in the session memory of a
    process that no longer exists, so the question can no longer be answered —
    and a row left reading "awaiting your answer" forever would be a lie the
    panel tells indefinitely.

    That reason is about the session, not about the row, which is why the sweep
    is scoped to rows that had one. A change the agent raised itself was never
    parked anywhere: everything needed to put it and to execute it is in the row
    (core/gate.py rebuilds the pending from exactly these columns), and the row
    survived the restart. Expiring it would throw away a question the user can
    still answer — and having nothing to lose in session memory is the entire
    point of raising it that way. Those are withdrawn when they stop being worth
    asking, which is the reasoning schedule's job and not this sweep's.
    """
    try:
        conn = _connect()
        cur = conn.execute(
            "UPDATE changes SET decided_at = ?, decision = 'expired',"
            " result = 'The add-on restarted before this was answered.'"
            " WHERE decision = 'proposed' AND source NOT IN (?, ?)",
            (_now(), *sorted(SESSIONLESS_SOURCES)),
        )
        conn.commit()
        if cur.rowcount:
            logger.info(f"ACTIVITY: {cur.rowcount} unanswered change(s) expired at startup")
        return cur.rowcount
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not expire stale proposals: {e}")
        return 0


def changes(limit: int = 50) -> list:
    """The most recent changes, newest first."""
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM changes ORDER BY proposed_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_row(r) for r in rows]
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not read the change log: {e}")
        return []


def peer_proposal_counts(actor: str, since: int) -> dict:
    """How many changes one agent has proposed: still `open`, and proposed at
    or after `since`. Counted from the journal, so a restart forgets nothing —
    the limits on an agent's proposals are read here, not kept in memory."""
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT SUM(CASE WHEN decision = 'proposed' THEN 1 ELSE 0 END) AS open,"
            "       SUM(CASE WHEN proposed_at >= ? THEN 1 ELSE 0 END) AS recent"
            " FROM changes WHERE source = ? AND actor = ?",
            (int(since), PEER_SOURCE, str(actor)),
        ).fetchone()
        return {"open": int(row["open"] or 0), "since": int(row["recent"] or 0)}
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not count {actor}'s proposals: {e}")
        # Fails closed: an unreadable journal must not lift the limits.
        return {"open": 10**6, "since": 10**6}


def change(change_id) -> dict:
    """One change by id, or None."""
    try:
        conn = _connect()
        row = conn.execute("SELECT * FROM changes WHERE id = ?", (int(change_id),)).fetchone()
        return _row(row) if row else None
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"ACTIVITY: could not read change {change_id}: {e}")
        return None


# --------------------------------------------------------------------------
# outbox — the delivery queue
# --------------------------------------------------------------------------
def queue(kind: str, title: str, body: str, urgency: str = "normal", payload=None):
    """Puts a message in the outbox. Returns its id, or None.

    The caller says what happened and how much it matters. It does not say
    where the message goes — that is core/delivery.py's decision, and keeping it
    out of here is what lets a new channel become a consumer rather than a
    change to every producer.
    """
    try:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO outbox (created_at, kind, urgency, title, body, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), str(kind), "urgent" if urgency == "urgent" else "normal",
             str(title or ""), str(body or ""), json.dumps(payload) if payload else None),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not queue a {kind} message: {e}")
        return None


def pending(channel: str, limit: int = 50) -> list:
    """Messages this channel has not taken yet, oldest first.

    The push channel sees only urgent messages. Everything else waits to be
    collected: §4 has exactly one agent-to-user path today, and spending it on
    a digest would train the user to ignore it.
    """
    column = _CHANNELS.get(channel)
    if not column:
        logger.error(f"ACTIVITY: unknown delivery channel {channel!r}")
        return []
    try:
        conn = _connect()
        urgent_only = " AND urgency = 'urgent'" if channel == "push" else ""
        rows = conn.execute(
            f"SELECT * FROM outbox WHERE {column} IS NULL{urgent_only}"
            " ORDER BY created_at, id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_row(r) for r in rows]
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not read pending {channel} messages: {e}")
        return []


def mark(message_id, channel: str) -> bool:
    """Stamps one message as taken by one channel."""
    column = _CHANNELS.get(channel)
    if not column or not message_id:
        return False
    try:
        conn = _connect()
        conn.execute(f"UPDATE outbox SET {column} = ? WHERE id = ?", (_now(), int(message_id)))
        conn.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"ACTIVITY: could not mark message {message_id} for {channel}: {e}")
        return False


def withdraw(message_id) -> bool:
    """Stops a message being delivered, on every channel at once.

    Not a delete: what the agent had to say is part of the record even when it
    stopped being true. This is for the message that has been overtaken —
    "waiting for permission to delete automation 1748", once the user has
    answered. Collecting that in ASI1 an hour later would ask them a question
    they have already settled.
    """
    if not message_id:
        return False
    try:
        conn = _connect()
        stamps = ", ".join(f"{c} = COALESCE({c}, ?)" for c in _CHANNELS.values())
        now = _now()
        conn.execute(f"UPDATE outbox SET {stamps} WHERE id = ?",
                     (*[now] * len(_CHANNELS), int(message_id)))
        conn.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"ACTIVITY: could not withdraw message {message_id}: {e}")
        return False


def messages(limit: int = 50) -> list:
    """The most recent messages, delivered or not, newest first. The panel shows
    what was sent as well as what is waiting."""
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM outbox ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_row(r) for r in rows]
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not read the outbox: {e}")
        return []


def stats() -> dict:
    """Totals for the Activity tab's header, and the unseen count the tab badge
    is drawn from."""
    empty = {"changes": 0, "awaiting": 0, "applied": 0, "rolled_back": 0,
             "messages": 0, "unseen": 0, "undelivered": 0}
    try:
        conn = _connect()
        # `applied` counts the rollbacks too — they are writes into the home like
        # any other. `rolled_back` is the rate design §12 calls "the one that
        # matters", and it counts originals that were undone, not undos.
        c = conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN decision = 'proposed' THEN 1 ELSE 0 END) AS awaiting,"
            " SUM(CASE WHEN status = 'applied' THEN 1 ELSE 0 END) AS applied,"
            " SUM(CASE WHEN undone_by IS NOT NULL THEN 1 ELSE 0 END) AS rolled_back"
            " FROM changes"
        ).fetchone()
        m = conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN seen_at IS NULL THEN 1 ELSE 0 END) AS unseen,"
            " SUM(CASE WHEN urgency = 'urgent' AND pushed_at IS NULL THEN 1 ELSE 0 END) AS undelivered"
            " FROM outbox"
        ).fetchone()
        return {
            "changes": c["n"] or 0,
            "awaiting": c["awaiting"] or 0,
            "applied": c["applied"] or 0,
            "rolled_back": c["rolled_back"] or 0,
            "messages": m["n"] or 0,
            "unseen": m["unseen"] or 0,
            "undelivered": m["undelivered"] or 0,
        }
    except sqlite3.Error as e:
        logger.error(f"ACTIVITY: could not read stats: {e}")
        return empty
