"""The suggestion lifecycle: every candidate the agent finds, from first sight
to measured outcome.

Design §7 gives a suggestion seven stages and one rule — "nothing is held only
in the model's head". This is where the stages are held. A candidate found by
the review pass, the replay that validated it, the change it was proposed
through, the baseline captured when it was applied and the verdict a week later
are all one row, growing a column at a time, so that a restart between any two
stages loses nothing but the process.

A third table in activity.db rather than a store of its own, for the reason
that file's own docstring already gives. Proposing a suggestion writes the
suggestion row AND queues the outbox message that announces it, and a home
where those two disagree — a suggestion the user is never shown, or a digest
line for a suggestion that was never recorded — is worse than either failing.
One file, one transaction. That is also why this module does not open its own
connection: two connections to one file are two transactions, which would buy
the shared filename and none of the guarantee. It borrows core/activity.py's
thread-local connection and adds its table to it.

Never pruned, like `changes` and unlike record.db. The behavioural record is an
input and is bounded at ninety days; this is the audit trail of what the agent
proposed to do with someone's home, and §12 measures the product on acceptance,
dismissal and rollback rates computed from exactly these rows. A retention
window would quietly delete the denominator.

`key` is the load-bearing column. It is the stable identity of a candidate —
"standby:sensor.tv_power" — and the daily review re-derives the same key from
the same waste every single day. Dedupe is therefore not an optimisation but
the difference between a product and a nuisance: a review that ran twice must
not put the same suggestion in front of the user twice. It is enforced by a
partial UNIQUE index over the non-retired rows, so it is a property of the
data rather than a promise about upsert_candidate() staying correct; retired
rows are outside it, because a key that was retired years ago must not stop the
same waste being noticed again.

State transitions are enforced here too. An illegal one is logged and refused
rather than applied, because the states are not decoration: `applied` is the
claim that something was written into a home, and `verified` the claim that it
was measured. Code that could set either by accident would make both unusable
as evidence.

A row has one of two SOURCES, and the store is where that is first stated
(proposal §5, B-1). `detector` rows are raised by a detector module, graded by
the replay and reach `validated` through `mark_validated`. `analyst` rows are
raised by the model (design §15, `core/analyst.py`), carry the model's own
title and reasoning as DATA, and reach `validated` through `mark_shown` (the
explorer run: a chart a person judges). `mark_audited`, the audited run's
entry (claims recounted, labelled `checked` / `untested`), was retired with
that run (free-hand F-9, 2.23.0); rows it wrote keep their `claims` and label
and are withdrawn by `review`. Same table, same keys discipline, same
state machine: an analyst suggestion the user dismissed is held by the same
cooldown, and one they applied is verified and offered for rollback by the
same pass. What differs is the proof behind `validated`, and each entry says
which proof it accepts.
"""
from datetime import datetime, timezone
import json
import logging
import sqlite3
import threading

from core import activity

logger = logging.getLogger(__name__)

# The states, in the order design §7 walks them. `retired` is reachable from
# anywhere: §11 requires a suggestion whose entity disappeared to be withdrawn
# at whatever stage it had reached.
#
# Read the map as "what may happen next", and note the two edges that are not
# in the straight line:
#
#   validated -> candidate   evidence that moved makes a replay stale, and a
#                            stale replay is exactly the "weak number" §7.1
#                            refuses to show
#   dismissed -> candidate   a dismissal is a cooldown, not a verdict. Once it
#                            expires the review may raise the same waste again
#
# kept and rolled_back are ends. The user has seen the measured result and
# answered; re-proposing the same change afterwards would be arguing with them.
_TRANSITIONS = {
    "candidate": {"validated", "retired"},
    "validated": {"candidate", "proposed", "retired"},
    "proposed": {"dismissed", "applied", "retired"},
    "dismissed": {"candidate", "retired"},
    "applied": {"verified", "retired"},
    "verified": {"kept", "rolled_back", "retired"},
    "kept": {"retired"},
    "rolled_back": {"retired"},
    "retired": set(),
}

# What active() shows: the suggestions that are still somebody's business,
# either the agent's or the user's. `dismissed` is not here because it belongs
# there only once its cooldown has run out — see active().
_LIVE_STATES = ("candidate", "validated", "proposed", "applied", "verified")

# Columns held as JSON. `config` is among them because the proposed automation
# is read back as a dict by the replay engine and the gate, never as text —
# unlike changes.old_config/new_config, which are the YAML a human is shown.
_JSON_COLUMNS = ("entities", "evidence", "estimate", "replay", "config",
                 "baseline", "verdict", "claims", "chart", "sent", "feedback")

# Where a row came from (B-1). Everything downstream that has to treat the two
# differently branches on this, never on `kind` — an unknown kind is a row a
# newer build wrote, an analyst row is one this build wrote on purpose.
SOURCE_DETECTOR = "detector"
SOURCE_ANALYST = "analyst"

# Design §15.3's three labels, strongest first. The label is the weakest thing
# true of the row (B-2) and is set by core code, never by the model. Since F-9
# (2.23.0) nothing writes `checked`: it is read on rows the retired claim
# audit stored. `untested` is also the placeholder level of an analyst row at
# `candidate`, before `mark_shown` gives it EVIDENCE_SHOWN.
EVIDENCE_REPLAYED = "replayed"
EVIDENCE_CHECKED = "checked"
EVIDENCE_UNTESTED = "untested"
EVIDENCE_LEVELS = (EVIDENCE_REPLAYED, EVIDENCE_CHECKED, EVIDENCE_UNTESTED)

# The explorer run's level (free-hand proposal F-10) — not a fourth rung on
# §15.3's ladder but a different kind of evidence: nothing was recounted or
# replayed, and the row carries a chart request a person judges instead. Kept
# out of EVIDENCE_LEVELS on purpose; `review.rank` places it where `untested`
# sorts, and says so beside D-6.
EVIDENCE_SHOWN = "shown"

# An analyst row's title is one line, because it is the headline of every §9
# artefact including the one-line digest.
TITLE_MAX_CHARS = 80

# What the panel's Yes / No / No, because… may record on a shown row (F-11).
FEEDBACK_ANSWERS = ("yes", "no")

# The third answer, which no person types (F-WP6): core/rollback.py writes it
# after an inverse of the row's change SUCCEEDED, and set_feedback takes it
# only with the journal row of that rollback — so the REST endpoint, which
# has no rollback id to give, cannot claim it.
FEEDBACK_ROLLED_BACK = "rolled_back"

# A "No, because…" is free text a person typed, and F-WP6 hands the last
# twenty of them to the model — bounded like a title, for the same reason.
FEEDBACK_REASON_MAX_CHARS = 500

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS suggestions ("
    " id INTEGER PRIMARY KEY,"
    # The stable candidate identity the daily review re-derives, e.g.
    # "standby:sensor.tv_power".
    " key TEXT NOT NULL,"
    " kind TEXT NOT NULL,"
    " created_at INTEGER NOT NULL,"
    " updated_at INTEGER NOT NULL,"
    " state TEXT NOT NULL DEFAULT 'candidate',"
    # What the suggestion is about, and why. `evidence` is the observation
    # behind the number, and §9 requires it to travel with the suggestion —
    # so it is stored, not recomputed at render time from a record that has
    # moved on since.
    " entities TEXT,"
    " evidence TEXT,"
    # Filled by validation: what the replay did, what it estimates was saved,
    # and the automation config that was replayed. All three or none — a
    # config nobody replayed is a candidate, whatever it looks like.
    " estimate TEXT,"
    " replay TEXT,"
    " config TEXT,"
    # The activity.changes row this was put to the user through. The approval,
    # the inverse and the rollback all live there; this is only the pointer, so
    # that there is one recorded approval per write and not two half-copies.
    " change_id INTEGER,"
    # Captured at apply time, because §7.2's comparison needs the "before" and
    # the record it was drawn from is already being changed by the automation
    # that was just written.
    " baseline TEXT,"
    " verify_after INTEGER,"
    " verdict TEXT,"
    # When a dismissal stops holding the same suggestion back.
    " dismissed_until INTEGER,"
    " retired_reason TEXT,"
    # Proposal §5.2 — who raised the row and what proves it. Also in
    # _ADDED_COLUMNS, which is how a store created before them gets them.
    " source TEXT NOT NULL DEFAULT 'detector',"
    " evidence_level TEXT DEFAULT 'replayed',"
    # The model that authored an analyst row; NULL on detector rows.
    " model TEXT,"
    # Analyst rows only, and text the model wrote: rendered, never parsed, and
    # never a source of a number (B-3).
    " title TEXT,"
    " rationale TEXT,"
    " claims TEXT)",

    # The dedupe rule, as a constraint rather than a convention. Partial, so
    # that retired rows keep their keys — the history of what was once
    # proposed for this entity is part of the audit trail — while never
    # blocking the same waste from being noticed again years later.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_suggestions_open_key"
    " ON suggestions(key) WHERE state != 'retired'",

    # due_for_verification() runs on a schedule and asks only this question.
    "CREATE INDEX IF NOT EXISTS idx_suggestions_verify ON suggestions(verify_after)",
)

# Columns added after the table shipped — core/activity.py's pattern for
# `changes`, for its reason: CREATE TABLE IF NOT EXISTS does nothing to a store
# that already exists, and this one is the audit trail §12 is computed from, so
# it is migrated in place and never rebuilt. The defaults are what an existing
# row IS: every row written before this column existed was raised by a detector
# and, if validated at all, validated by a replay.
_ADDED_COLUMNS = (
    ("source", "TEXT NOT NULL DEFAULT 'detector'"),
    ("evidence_level", "TEXT DEFAULT 'replayed'"),
    ("model", "TEXT"),
    ("title", "TEXT"),
    ("rationale", "TEXT"),
    ("claims", "TEXT"),
    # The explorer run's idea (free-hand proposal §4.1): the model's sentence,
    # its rule in plain words, the chart request (JSON: spec, and the series /
    # markers / error F-WP4 fills) and what the run sent out for it (JSON).
    ("finding", "TEXT"),
    ("rule_text", "TEXT"),
    ("chart", "TEXT"),
    ("sent", "TEXT"),
    # The household's answer to a shown row (F-11, JSON: answer, reason, at).
    # Added with F-WP5 rather than F-WP6: the panel's card writes it.
    ("feedback", "TEXT"),
)

# Which connection this thread has already applied the schema to. Thread-local
# for the same reason the connection is, and keyed on the connection object
# rather than on a flag, because core/activity.py hands back a new one whenever
# a test repoints DB_PATH.
_local = threading.local()


def _now():
    return int(datetime.now(tz=timezone.utc).timestamp())


def _conn():
    """Returns the thread's activity.db connection with this table on it.

    Raises sqlite3.Error — every public function below handles that.
    """
    conn = activity._connect()
    if getattr(_local, "schema_done", None) is not conn:
        for statement in _SCHEMA:
            conn.execute(statement)
        _migrate(conn)
        conn.commit()
        _local.schema_done = conn
    return conn


def _migrate(conn):
    """Adds any column this version expects and an older store lacks."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(suggestions)")}
    for column, kind in _ADDED_COLUMNS:
        if column not in have:
            conn.execute(f"ALTER TABLE suggestions ADD COLUMN {column} {kind}")
            logger.info(f"SUGGESTIONS: added suggestions.{column} to an existing store")


def close():
    """Drops this thread's connection, via the module that owns it. Tests call
    it when they repoint DB_PATH; nothing in production needs it."""
    _local.schema_done = None
    activity.close()


def _begin(conn):
    """Opens a write transaction if one is not already open.

    upsert_candidate() checks for an existing row and then writes, and those two
    have to be one step or two review passes racing could both find nothing. The
    lock is safe to take here in a way it is not in core/record.py: what it
    guards is a single indexed lookup by key, not a scan of a growing table.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _row(row) -> dict:
    """A sqlite3.Row as a plain dict, with the JSON columns decoded."""
    out = dict(row)
    for key in _JSON_COLUMNS:
        if key in out and out[key]:
            try:
                out[key] = json.loads(out[key])
            except (TypeError, ValueError):
                pass
    return out


def _dump(value):
    """A JSON column's value, or NULL where there is nothing to say."""
    return json.dumps(value) if value not in (None, "", [], {}) else None


def _transition(suggestion_id, to_state: str, columns: dict, source=None) -> bool:
    """Moves one suggestion to `to_state`, setting `columns` with it.

    The single place a state is written. A move the map does not allow is
    logged and refused: the caller finding out is the point, because a stage
    reached out of order means the lifecycle has been driven by something that
    misread it, and applying it anyway would leave a row claiming a home was
    changed when it was not.

    `source`, when given, is the only source the move is legal for. The three
    entries to `validated` accept different proof (B-6, F-10), and a replay written
    onto an analyst row — or an audit onto a detector row — would be a label
    the row's own evidence does not back; it is checked inside the same
    transaction as the state for the same reason the state is.
    """
    if not suggestion_id:
        return False
    try:
        conn = _conn()
        _begin(conn)
        row = conn.execute("SELECT state, source FROM suggestions WHERE id = ?",
                           (int(suggestion_id),)).fetchone()
        if row is None:
            conn.rollback()
            logger.error(f"SUGGESTIONS: no suggestion {suggestion_id} to move to {to_state}")
            return False
        if source is not None and row["source"] != source:
            conn.rollback()
            logger.error(f"SUGGESTIONS: refused to move suggestion {suggestion_id}"
                         f" to {to_state} — it is a {row['source']} row, and this"
                         f" entry is for {source} rows")
            return False
        current = row["state"]
        if to_state not in _TRANSITIONS.get(current, set()):
            conn.rollback()
            logger.error(f"SUGGESTIONS: refused to move suggestion {suggestion_id}"
                         f" from {current} to {to_state}")
            return False
        assignments = {"state": to_state, "updated_at": _now(), **columns}
        # The WHERE still names the state that was checked: the row is read and
        # written inside one IMMEDIATE transaction, and this costs nothing to
        # keep true if that ever stops being so.
        sets = ", ".join(f"{name} = ?" for name in assignments)
        conn.execute(f"UPDATE suggestions SET {sets} WHERE id = ? AND state = ?",
                     (*assignments.values(), int(suggestion_id), current))
        conn.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"SUGGESTIONS: could not move suggestion {suggestion_id}"
                     f" to {to_state}: {e}")
        return False


# --------------------------------------------------------------------------
# the lifecycle
# --------------------------------------------------------------------------
def upsert_candidate(key: str, kind: str, entities=None, evidence=None):
    """Records what the review pass found, or recognises it as something
    already known. Returns the suggestion's id, or None if it could not be
    written.

    This is the module's most important behaviour and it is a negative one: the
    review runs every day over the same house and re-derives the same key from
    the same waste, so the second run and the two-hundredth must produce no new
    suggestion at all. The partial UNIQUE index makes that structural; what is
    decided here is what an already-known candidate does with the fresh
    evidence.

    Three answers, by how far the suggestion has got:

    - not yet shown to anyone (candidate, or validated with the evidence
      changed under it) — take the new evidence. A replay validated against
      figures that have moved is stale, so the suggestion drops back to
      `candidate` and is revalidated before it can be proposed again.
    - already in front of the user, or already in the home (proposed, applied,
      verified, and the two ends) — change nothing. §9 requires the evidence to
      travel with the suggestion, and rewriting it underneath a proposal the
      user is reading would make the reasoning they were shown untrue.
    - dismissed — held until the cooldown expires, then reopened as a fresh
      candidate. That is what a cooldown is for: the user said "not now", not
      "never", and `retire` is where "never" lives.
    """
    if not key:
        return None
    try:
        conn = _conn()
        now = _now()
        _begin(conn)
        row = conn.execute(
            "SELECT * FROM suggestions WHERE key = ? AND state != 'retired'",
            (str(key),),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO suggestions (key, kind, created_at, updated_at, state,"
                " entities, evidence) VALUES (?, ?, ?, ?, 'candidate', ?, ?)",
                (str(key), str(kind), now, now, _dump(entities), _dump(evidence)),
            )
            conn.commit()
            return cur.lastrowid

        state = row["state"]
        if state not in ("candidate", "validated", "dismissed"):
            # Already the user's business. Nothing here may touch it.
            conn.rollback()
            return row["id"]

        if state == "dismissed" and (row["dismissed_until"] or 0) > now:
            conn.rollback()
            return row["id"]

        fresh = _dump(evidence)
        seen = _dump(entities)
        if state != "dismissed" and (fresh, seen) == (row["evidence"], row["entities"]):
            # The same waste, described the same way: today's review found what
            # yesterday's did. Nothing to record but the fact that it is still
            # true — and in particular a validated candidate keeps its replay,
            # or it would be revalidated every morning and never proposed.
            conn.execute("UPDATE suggestions SET updated_at = ? WHERE id = ?",
                         (now, row["id"]))
            conn.commit()
            return row["id"]

        # Reopening, or re-evidencing: whatever validation produced belonged to
        # the figures that have just been replaced, and is cleared rather than
        # left to be proposed as if it still applied.
        conn.execute(
            "UPDATE suggestions SET updated_at = ?, state = 'candidate', entities = ?,"
            " evidence = ?, estimate = NULL, replay = NULL, config = NULL,"
            " change_id = NULL, dismissed_until = NULL WHERE id = ?",
            (now, seen, fresh, row["id"]),
        )
        conn.commit()
        return row["id"]
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"SUGGESTIONS: could not record candidate {key!r}: {e}")
        return None


def mark_validated(suggestion_id, replay=None, estimate=None, config=None) -> bool:
    """Records that the candidate was replayed against the record, and what
    came out.

    All three together, because they are one fact: this config, replayed, gave
    this estimate. Storing the estimate without the replay behind it would be
    the number without the observation §9 forbids.

    Detector rows only — the first of three entries to `validated` (B-6,
    F-10), and its invariant is still "replay, estimate and config, all three
    or none". An analyst row has no replay to give it; it enters through
    `mark_shown`.
    """
    return _transition(suggestion_id, "validated", {
        "replay": _dump(replay),
        "estimate": _dump(estimate),
        "config": _dump(config),
    }, source=SOURCE_DETECTOR)


def mark_shown(suggestion_id, config=None, finding="", rule_text="", chart=None,
               sent=None) -> bool:
    """Records that an idea from the analyst's explorer run passed the house
    checks, with the config to propose, the model's sentence and rule, the
    chart request that is its evidence, and what the run sent out for it.

    The analyst's entry to `validated` (free-hand proposal F-9, F-10). Its
    invariant: "a shown row carries a config and a chart request, never
    claims". The chart is the evidence a person judges (F-7), so a row
    without one has nothing behind it and is refused; claims belonged to the
    retired audited run's rows, and a row carrying both would read as
    recounted when nothing was. `evidence_level` is written as EVIDENCE_SHOWN here and
    nowhere else. A chart whose spec the record could not draw still counts —
    it carries its `error`, and the card says so (F-7) — but the `spec` key
    must be there.

    Claims, replay and estimate are cleared in the same write: no figure
    rides on a shown row.
    `finding` is the model's one sentence and `rule_text` its rule, stored as
    DATA and rendered as its words, never parsed. `sent` is F-6's list of
    what the run's record tools answered — the fact behind "what was sent for
    this"; an empty list stores NULL.
    """
    if not isinstance(config, dict) or not config:
        logger.error(f"SUGGESTIONS: refused to show suggestion {suggestion_id}"
                     f" — there is no config to propose")
        return False
    if not isinstance(chart, dict) or "spec" not in chart:
        logger.error(f"SUGGESTIONS: refused to show suggestion {suggestion_id}"
                     f" — a shown row needs the chart request that is its evidence")
        return False
    sentence = str(finding or "").strip()
    rule = str(rule_text or "").strip()
    if not sentence or not rule:
        logger.error(f"SUGGESTIONS: refused to show suggestion {suggestion_id}"
                     f" — it needs the agent's finding and its rule in words")
        return False
    if sent is not None and not isinstance(sent, (list, tuple)):
        logger.error(f"SUGGESTIONS: refused to show suggestion {suggestion_id}"
                     f" — what was sent is not a list")
        return False
    return _transition(suggestion_id, "validated", {
        "config": _dump(config),
        "evidence_level": EVIDENCE_SHOWN,
        "claims": None,
        "replay": None,
        "estimate": None,
        "finding": sentence,
        "rule_text": rule,
        "chart": _dump(chart),
        "sent": _dump(list(sent or [])),
    }, source=SOURCE_ANALYST)


def set_chart(suggestion_id, chart=None) -> bool:
    """Replaces a shown row's chart with one drawn again (`charts.redraw`,
    F-WP4) — for a row stored before charts were drawn at store time.

    Not a transition: the state is not moved, and `updated_at` is NOT
    touched, because the review counts "proposed today" from it and a redraw
    is not a proposal. Only a `shown` analyst row outside `retired` takes
    one, and only a chart with its `spec` — `mark_shown`'s own rule, checked
    in the same IMMEDIATE transaction as the write.
    """
    if not isinstance(chart, dict) or "spec" not in chart:
        logger.error(f"SUGGESTIONS: refused a chart for suggestion {suggestion_id}"
                     f" — it has no spec")
        return False
    try:
        conn = _conn()
        _begin(conn)
        row = conn.execute("SELECT state, source, evidence_level FROM suggestions WHERE id = ?",
                           (int(suggestion_id),)).fetchone()
        if (row is None or row["source"] != SOURCE_ANALYST
                or row["evidence_level"] != EVIDENCE_SHOWN or row["state"] == "retired"):
            conn.rollback()
            logger.error(f"SUGGESTIONS: suggestion {suggestion_id} is not a shown row"
                         f" that can take a chart")
            return False
        conn.execute("UPDATE suggestions SET chart = ? WHERE id = ?",
                     (_dump(chart), int(suggestion_id)))
        conn.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"SUGGESTIONS: could not store a chart for suggestion {suggestion_id}: {e}")
        return False


def set_feedback(suggestion_id, answer, reason="", rollback_id=None) -> dict:
    """Records the household's answer to a shown row: `{answer, reason, at,
    change_id}` (free-hand F-11). Returns the stored feedback, or None when
    refused.

    It RECORDS an answer and does nothing else. It is not an approval and not
    a rejection: the panel sends Yes / No to /rest/approve with the row's
    change_id, and core/gate.py applies or refuses the change there — this
    column holds only what the person said and why, for the next run's
    prompt (F-WP6). So it is not a transition: the state is not moved, and
    `updated_at` is NOT touched (the review counts "proposed today" from it).
    Any state takes one, because the answer and the change it answers are
    two writes and the order the panel sends them in is not this store's to
    assume. Only a `shown` analyst row takes one — the card that asks.

    `change_id` is the row's change AT THE ANSWER (F-WP6): a row the review
    raises again after its cooldown is proposed through a new change, and the
    prompt block checks an answer against the change it was given for, not
    the one that replaced it (analyst.household_answers).

    `rolled_back` (F-WP6) is taken only with `rollback_id`, and only when the
    journal says that rollback undid THIS row's change and was applied —
    core/rollback.py passes it after a successful inverse. The REST endpoint
    has no rollback id to pass, so a person cannot claim one; and neither can
    any caller the journal does not back.

    `reason` is DATA: whitespace collapsed, cut at FEEDBACK_REASON_MAX_CHARS,
    never parsed. A second answer replaces the first.
    """
    word = str(answer or "").strip().lower()
    undo = None
    if word == FEEDBACK_ROLLED_BACK:
        undo = activity.change(rollback_id) if rollback_id else None
        if not undo or undo.get("status") != "applied":
            logger.error(f"SUGGESTIONS: refused {word!r} on suggestion {suggestion_id}"
                         f" — no applied rollback {rollback_id!r} in the journal")
            return None
    elif word not in FEEDBACK_ANSWERS:
        logger.error(f"SUGGESTIONS: refused feedback {answer!r} on suggestion {suggestion_id}"
                     f" — not one of {', '.join(FEEDBACK_ANSWERS)}")
        return None
    try:
        conn = _conn()
        _begin(conn)
        row = conn.execute("SELECT source, evidence_level, change_id FROM suggestions"
                           " WHERE id = ?", (int(suggestion_id),)).fetchone()
        if (row is None or row["source"] != SOURCE_ANALYST
                or row["evidence_level"] != EVIDENCE_SHOWN):
            conn.rollback()
            logger.error(f"SUGGESTIONS: suggestion {suggestion_id} is not a shown row"
                         f" that can take feedback")
            return None
        if undo is not None and (row["change_id"] is None
                                 or undo.get("undo_of") != row["change_id"]):
            conn.rollback()
            logger.error(f"SUGGESTIONS: rollback {rollback_id} did not undo suggestion "
                         f"{suggestion_id}'s change {row['change_id']}")
            return None
        feedback = {"answer": word, "reason": _one_line(reason, FEEDBACK_REASON_MAX_CHARS),
                    "at": _now(), "change_id": row["change_id"]}
        conn.execute("UPDATE suggestions SET feedback = ? WHERE id = ?",
                     (_dump(feedback), int(suggestion_id)))
        conn.commit()
        return feedback
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"SUGGESTIONS: could not store feedback for suggestion {suggestion_id}: {e}")
        return None


def _one_line(text, limit=None) -> str:
    """Model-written text as stored: whitespace collapsed, so a title cannot
    carry a line break into a one-line digest, and cut at `limit` with an
    ellipsis. Nothing in it is interpreted."""
    flat = " ".join(str(text or "").split())
    if limit and len(flat) > limit:
        flat = flat[:limit - 1].rstrip() + "…"
    return flat


def upsert_analyst_candidate(key: str, entities=None, title="", rationale="",
                             model=""):
    """Records a candidate the analyst raised, or recognises one already known.
    Returns the suggestion's id, or None if it could not be written.

    The analyst's `upsert_candidate`, and deliberately the same three answers
    by how far the suggestion has got — not yet shown (take the new text, drop
    back to `candidate`, clear the audit), already the user's business (touch
    nothing: the reasoning they were shown must stay what they were shown),
    dismissed (held until the cooldown runs out). A model saying the same
    thing tomorrow is the same suggestion, exactly as a detector re-deriving
    the same waste is.

    The key must be `analyst:<digest>`. The prefix is what keeps an analyst
    row and a detector row from ever sharing the partial UNIQUE index slot —
    a detector key is `<kind>:<entity>`, and "analyst" is not a detector's
    kind. Deriving the digest is the caller's (Phase D).

    Written with `evidence_level = 'untested'`: before the audit there is
    nothing true of the row but that, and B-2 says the label is the weakest
    thing true. `title`, `rationale` and `model` are stored as data; the title
    is flattened to one line of at most TITLE_MAX_CHARS.
    """
    if not key or not str(key).startswith(f"{SOURCE_ANALYST}:") or \
            len(str(key)) <= len(SOURCE_ANALYST) + 1:
        logger.error(f"SUGGESTIONS: refused analyst candidate {key!r} — the key must be"
                     f" 'analyst:<digest>'")
        return None
    heading = _one_line(title, TITLE_MAX_CHARS)
    if not heading:
        logger.error(f"SUGGESTIONS: refused analyst candidate {key!r} — it has no title")
        return None
    reasoning = str(rationale or "").strip() or None
    author = str(model or "").strip() or None
    try:
        conn = _conn()
        now = _now()
        _begin(conn)
        row = conn.execute(
            "SELECT * FROM suggestions WHERE key = ? AND state != 'retired'",
            (str(key),),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO suggestions (key, kind, created_at, updated_at, state,"
                " entities, source, evidence_level, model, title, rationale)"
                " VALUES (?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?)",
                (str(key), SOURCE_ANALYST, now, now, _dump(entities), SOURCE_ANALYST,
                 EVIDENCE_UNTESTED, author, heading, reasoning),
            )
            conn.commit()
            return cur.lastrowid

        if row["source"] != SOURCE_ANALYST:
            conn.rollback()
            logger.error(f"SUGGESTIONS: refused analyst candidate {key!r} — the key"
                         f" belongs to a {row['source']} row")
            return None

        state = row["state"]
        if state not in ("candidate", "validated", "dismissed"):
            conn.rollback()
            return row["id"]

        if state == "dismissed" and (row["dismissed_until"] or 0) > now:
            conn.rollback()
            return row["id"]

        seen = _dump(entities)
        if state != "dismissed" and (seen, heading, reasoning) == (
                row["entities"], row["title"], row["rationale"]):
            # Same suggestion, same words. The audit it passed still describes
            # it, so it keeps its label and its place in the queue.
            conn.execute("UPDATE suggestions SET updated_at = ? WHERE id = ?",
                         (now, row["id"]))
            conn.commit()
            return row["id"]

        # New words are a new claim: whatever the audit concluded was about the
        # old ones, and is cleared back to the weakest label.
        conn.execute(
            "UPDATE suggestions SET updated_at = ?, state = 'candidate', entities = ?,"
            " title = ?, rationale = ?, model = ?, evidence_level = ?, claims = NULL,"
            " config = NULL, replay = NULL, estimate = NULL, change_id = NULL,"
            " finding = NULL, rule_text = NULL, chart = NULL, sent = NULL,"
            " dismissed_until = NULL WHERE id = ?",
            (now, seen, heading, reasoning, author, EVIDENCE_UNTESTED, row["id"]),
        )
        conn.commit()
        return row["id"]
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"SUGGESTIONS: could not record analyst candidate {key!r}: {e}")
        return None


def mark_proposed(suggestion_id, change_id=None) -> bool:
    """Records that the suggestion has been put to the user, and through which
    change.

    The approval itself is not stored here. `change_id` points at the
    activity.changes row that carries the decision and the inverse, so there is
    one recorded approval per write — the gate's, the one core/rollback.py can
    actually execute — rather than a second copy here that could disagree
    with it.
    """
    return _transition(suggestion_id, "proposed",
                       {"change_id": int(change_id) if change_id else None})


def mark_dismissed(suggestion_id, cooldown_days: int = 30) -> bool:
    """Records a "no", and how long it holds.

    Not a retirement. The waste is still there and the review will find it
    again tomorrow, so a dismissal with no expiry would either silence a real
    problem forever or reduce to asking again the next morning. The cooldown is
    what sits between those, and it is stored as an instant rather than a
    duration so that reading it needs no arithmetic and no memory of when it
    was set.
    """
    try:
        days = max(0, int(cooldown_days))
    except (TypeError, ValueError):
        days = 0
    return _transition(suggestion_id, "dismissed",
                       {"dismissed_until": _now() + days * 86400})


def mark_applied(suggestion_id, baseline=None, verify_after=None) -> bool:
    """Records that the approved automation was written, with the "before" the
    verification will be measured against.

    The baseline is captured now and stored, not derived later. From this
    moment the record is being changed by the automation that was just written,
    so a "before" computed in a week's time would be reading the after.
    """
    return _transition(suggestion_id, "applied", {
        "baseline": _dump(baseline),
        "verify_after": int(verify_after) if verify_after else None,
    })


def defer_verification(suggestion_id, verify_after, verdict=None) -> bool:
    """Records an attempt at verification that could not measure anything, and
    when to try again. NO state change — the suggestion is still `applied`.

    core/verify.py decision 1: a week that could not be read may well be
    readable next week, so it is measured again rather than reported as a
    saving of nothing. What is written is the attempt itself — the reason and
    the count — because a report that eventually says "this could not be
    measured" has to be able to say how many times it tried and what stopped
    it, and a counter kept in a process would be reset by every restart.

    Deliberately not `record_verdict`. That function moves the suggestion to a
    state that CLAIMS something — `verified` is the claim that a measurement
    was taken — and nothing was taken here. The `verdict` column carries what
    the verification has found so far; `state` is what it has concluded, and
    it has concluded nothing yet.
    """
    if not suggestion_id:
        return False
    try:
        conn = _conn()
        _begin(conn)
        row = conn.execute("SELECT state FROM suggestions WHERE id = ?",
                           (int(suggestion_id),)).fetchone()
        if row is None or row["state"] != "applied":
            conn.rollback()
            logger.error(f"SUGGESTIONS: refused to defer the verification of suggestion "
                         f"{suggestion_id} — it is "
                         f"{row['state'] if row is not None else 'not there'}")
            return False
        conn.execute(
            "UPDATE suggestions SET updated_at = ?, verify_after = ?, verdict = ?"
            " WHERE id = ? AND state = 'applied'",
            (_now(), int(verify_after) if verify_after else None,
             _dump(verdict), int(suggestion_id)),
        )
        conn.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"SUGGESTIONS: could not defer the verification of suggestion "
                     f"{suggestion_id}: {e}")
        return False


def record_verdict(suggestion_id, verdict=None, state: str = "verified") -> bool:
    """Records the measured result, and where it left the suggestion.

    Three states can carry a verdict — `verified` when the measurement is in
    and the user has not answered yet, `kept` and `rolled_back` when they have —
    because §7.2 makes the report and the decision two separate moments: the
    agent states a number it can defend, and always offers the rollback
    whatever the number says.
    """
    if state not in ("verified", "kept", "rolled_back"):
        logger.error(f"SUGGESTIONS: {state!r} is not a verdict state")
        return False
    return _transition(suggestion_id, state, {"verdict": _dump(verdict)})


def retire(suggestion_id, reason: str = "") -> bool:
    """Withdraws a suggestion from wherever it had got to.

    §11's "referenced entity disappears" case, and the only exit from a state
    that otherwise has none. It is deliberately not a delete: what the agent
    proposed and why it stopped proposing it is the part of the audit trail
    that explains a gap.
    """
    return _transition(suggestion_id, "retired", {"retired_reason": str(reason or "")[:500]})


# --------------------------------------------------------------------------
# reading it back
# --------------------------------------------------------------------------
def open_for(key: str):
    """The one live suggestion for this candidate key, or None.

    One, not a list: the partial UNIQUE index guarantees it, and the review
    pass is written against that guarantee.
    """
    if not key:
        return None
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT * FROM suggestions WHERE key = ? AND state != 'retired'",
            (str(key),),
        ).fetchone()
        return _row(row) if row else None
    except sqlite3.Error as e:
        logger.error(f"SUGGESTIONS: could not read the suggestion for {key!r}: {e}")
        return None


def for_change(change_id):
    """The suggestion that was proposed through this journal row, or None.

    The pointer read backwards. `mark_proposed` writes `change_id` so that the
    approval, the inverse and the rollback all live in one place; this is how
    the answer finds its way back — the gate hands a change_id to whoever
    raised it, and on approval that is the only name the suggestion has.

    None is an ordinary answer: most changes in the journal were asked for in a
    conversation and no suggestion was ever behind them.
    """
    if not change_id:
        return None
    try:
        conn = _conn()
        row = conn.execute("SELECT * FROM suggestions WHERE change_id = ?",
                           (int(change_id),)).fetchone()
        return _row(row) if row else None
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"SUGGESTIONS: could not read the suggestion for change {change_id}: {e}")
        return None


def answered_ideas() -> list:
    """Every shown analyst row that carries an answer or a change the
    household may have answered: `id`, `title`, `feedback`, `change_id` —
    what the explorer's prompt block is built from (F-WP6).

    Only those four columns: the rows are never pruned and each holds a chart
    of up to charts.CHART_MAX_BYTES, which the block has no use for. Any
    state, retired included — an answer outlives the row being open. Whether
    an answer took effect is the journal's to say, and
    analyst.household_answers asks it; this only reads.
    """
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT id, title, feedback, change_id FROM suggestions"
            " WHERE source = ? AND evidence_level = ?"
            " AND (feedback IS NOT NULL OR change_id IS NOT NULL)"
            " ORDER BY id DESC",
            (SOURCE_ANALYST, EVIDENCE_SHOWN)).fetchall()
        return [_row(r) for r in rows]
    except sqlite3.Error as e:
        logger.error(f"SUGGESTIONS: could not read the answered ideas: {e}")
        return []


def active(now=None) -> list:
    """Everything still in play, most recently touched first.

    A dismissed suggestion is in play again the moment its cooldown expires,
    and not one second before — which is the whole point of the cooldown, and
    the reason this cannot simply be "not retired and not finished".
    """
    at = int(now) if now else _now()
    try:
        conn = _conn()
        placeholders = ", ".join("?" for _ in _LIVE_STATES)
        rows = conn.execute(
            f"SELECT * FROM suggestions WHERE state IN ({placeholders})"
            " OR (state = 'dismissed' AND COALESCE(dismissed_until, 0) <= ?)"
            " ORDER BY updated_at DESC, id DESC",
            (*_LIVE_STATES, at),
        ).fetchall()
        return [_row(r) for r in rows]
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"SUGGESTIONS: could not read the active suggestions: {e}")
        return []


def proposed() -> list:
    """The suggestions put to the user and not yet answered, newest first.

    §9's digest and WP8's two surfaces all ask exactly this question, so it is
    asked of the store once rather than being a filter over `active()` written
    out at each call site — a second copy is a second chance to include a
    `validated` row nobody has been shown, or one whose change was answered
    somewhere else.

    A row here always has a `change_id`: `mark_proposed` is the only way into
    this state and core/review.py withdraws the change again if it could not
    write one.
    """
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT * FROM suggestions WHERE state = 'proposed'"
            " ORDER BY updated_at DESC, id DESC").fetchall()
        return [_row(r) for r in rows]
    except sqlite3.Error as e:
        logger.error(f"SUGGESTIONS: could not read the open proposals: {e}")
        return []


def due_for_verification(now=None) -> list:
    """Applied suggestions whose verification window has closed, oldest first.

    Inclusive of the instant itself: `verify_after` is when the week is up, not
    when it starts to be up.
    """
    at = int(now) if now else _now()
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT * FROM suggestions WHERE state = 'applied'"
            " AND verify_after IS NOT NULL AND verify_after <= ?"
            " ORDER BY verify_after, id",
            (at,),
        ).fetchall()
        return [_row(r) for r in rows]
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"SUGGESTIONS: could not read suggestions due for verification: {e}")
        return []


def suggestion(suggestion_id):
    """One suggestion by id, or None."""
    if not suggestion_id:
        return None
    try:
        conn = _conn()
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?",
                           (int(suggestion_id),)).fetchone()
        return _row(row) if row else None
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.error(f"SUGGESTIONS: could not read suggestion {suggestion_id}: {e}")
        return None


def stats() -> dict:
    """One count per state, plus the two numbers that are not a state.

    §12 measures the product on acceptance, dismissal and rollback rates, and
    all three are ratios over these counts — which is why they are computed
    from the rows rather than incremented anywhere.
    """
    empty = {state: 0 for state in _TRANSITIONS}
    empty.update({"suggestions": 0, "due": 0})
    try:
        conn = _conn()
        out = dict(empty)
        for row in conn.execute("SELECT state, COUNT(*) AS n FROM suggestions GROUP BY state"):
            if row["state"] in out:
                out[row["state"]] = row["n"]
            out["suggestions"] += row["n"]
        out["due"] = len(due_for_verification())
        return out
    except sqlite3.Error as e:
        logger.error(f"SUGGESTIONS: could not read stats: {e}")
        return empty
