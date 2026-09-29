"""What a RUN is: the one place a series of record rows becomes stretches of
time spent in one state, rather than a list of changes.

Proposal F-2 (docs/PROPOSAL-FREE-HAND-ANALYST.md): `tools/tool_record_timeline.py`
hands the model "on 06:52-10:41", not four separate rows it would have to
merge itself, and F-WP4's `timeline`/`overlay` charts draw exactly this same
shape — so this module is written once, the way core/window.py is the one
answer about the household's clock, so that what the model was told and what
the user is later shown cannot differ by having two implementations of "merge
adjacent same-state rows" that quietly disagree.

PURE, like core/window.py: stdlib only, no store, no clock it was not handed.
A `record.between()` row is a plain dict here (`ts`, `state`, ...); nothing in
this module opens record.db.

DURATION IS ALWAYS FROM EPOCHS, NEVER FROM WALL CLOCKS
--------------------------------------------------------
`runs()` only ever subtracts, compares and copies the integers it is given —
it never converts a timestamp to a local `datetime` and back. That is what
keeps a run crossing a DST change correct on both axes at once: the elapsed
seconds between its two ends are still `to_ts - from_ts`, exactly what a caller
who wants a duration should compute, while each end printed as a wall-clock
time (the caller's job, not this module's — see the deviations note in
docs/PROPOSAL-FREE-HAND-ANALYST.md §5.1) is independently correct because it is
converted on its own. A version that measured "hours between two HH:MM strings"
would be off by one on the two days a year the clock moves; this one cannot be,
because it never looks at a clock at all.
"""


def runs(rows, start, end, state_before=None):
    """Turns one entity's transitions inside [start, end) into runs: a list of
    (from_ts, to_ts, state) tuples, oldest first, covering the whole window.

    `rows` is `record.between()`'s shape: dicts with at least `ts` (the instant
    the row's `state` began) and `state`, oldest first, `ts` inside [start, end)
    — the caller's job, not this one's; nothing here re-checks the window rows
    were read from.

    The first run opens at `start`. In `state_before` when it is known (the
    state in force at `start` — see `record.state_at`), so a window that opens
    mid-state is not silently reported as starting only at its first change.
    Unknown (`None`), the first run instead opens at the first row's own `ts`
    — the earliest instant this window can actually vouch for a state.

    Consecutive rows reporting the SAME state merge into one run rather than
    each starting a new one: the record does not guarantee its rows always
    alternate (a caller with `state_before` already matching the first row's
    state is one legitimate way to reach this), and a run boundary with no
    state change either side of it is not a run boundary.

    The last run is clipped to `end` — nothing after it is known either, so it
    is reported as lasting until the edge of the window asked for, not until
    whatever it happens to do next.
    """
    start, end = int(start), int(end)
    if start >= end:
        return []

    if state_before is not None:
        cur_state = state_before
        cur_start = start
        pending = rows
    elif rows:
        cur_state = rows[0]["state"]
        cur_start = int(rows[0]["ts"])
        pending = rows[1:]
    else:
        return []

    out = []
    for row in pending:
        ts = int(row["ts"])
        state = row["state"]
        if state == cur_state or ts <= cur_start:
            # Same state as the run already open, or a row that cannot open a
            # new one (out of order, or equal to where this run already
            # starts) — merged into the run in progress rather than cut here.
            continue
        out.append((cur_start, ts, cur_state))
        cur_start, cur_state = ts, state

    if cur_start < end:
        out.append((cur_start, end, cur_state))
    return out


def is_numeric(rows) -> bool:
    """True when every row's `state` parses as a number — a sensor whose
    readings are (almost) never exactly equal twice, so "runs" would mean one
    run per row and tell nobody anything. False for an empty list: with
    nothing to look at there is no basis for calling it numeric rather than
    simply unanswered.
    """
    if not rows:
        return False
    for row in rows:
        try:
            float(row["state"])
        except (TypeError, ValueError):
            return False
    return True
