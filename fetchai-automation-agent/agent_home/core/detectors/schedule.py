"""The schedule detector: a device idle across the same stretch of the week,
week after week, with nobody at home to want it on.

Second of the deterministic detectors, and the one
docs/PROPOSAL-SCHEDULE-DETECTOR.md was raised for. core/detectors/standby.py
asks one question about one fixed window — was the draw flat across 01:00-05:00
— and on a household that leaves at 07:00 and comes back at 17:00 that misses
about two thirds of the waste: the daytime idle stretch is longer than the
night one and the night detector cannot see it at all. What this module looks
for is the stretch ITSELF. It reads the record as intervals, lays them on a
weekly grid, and reports the longest run of half-hours this device was idle on
every occasion it was observed — which on that household is 07:00-17:00 Mon-Fri,
the pattern the proposal opens with.

Deterministic, for the reason core/detectors/__init__.py gives: §7.1 draws the
line at "the model proposes candidates; it does not get to grade them", and
every threshold below is a named constant with its reasoning attached because a
magic number here is a number nobody can argue with later.

BUILT AHEAD OF ITS CALLER, DELIBERATELY
---------------------------------------
Nothing calls `scan` today. core/review.py still calls standby.scan and only
that, and the four modules a candidate would have to travel through — the
builder, the replay engine, the verification and the §9 artefacts — are all
still written about a night. Teaching them a second window is the next work
package; putting this one behind it would mean writing the grid, the replay's
per-weekday DST arithmetic and the readback sentence in a single change nobody
could review. So this lands first and alone, the way core/window.py did, and
for the same reason: a seam is cheaper to open with nothing leaning on it.

What that costs is that the evidence blob below has never been rendered. What
it buys is that its vocabulary is still free to be honest — see the last
section here.

STAGES
------
The proposal's §3, stages B, C and D1, in order, and each of them is a pure
function over the one before:

  B  `intervals`     transitions -> (start, end, state) over the whole window
  C  `weekly_grid`   those intervals -> 7 weekdays x 48 half-hours
  D1 `schedules`     that grid -> maximal all-idle runs, merged across weekdays

Stage A is not here: it is core/detectors/levels.py, shared with standby.py and
built one work package early precisely because this module asks its question —
"was this device idle" — for every one of 336 cells. Stage D2, handing the grid
to a model, is core/scheduler_llm.py; what is HERE for it is `grid_text` (WP1),
the input that module reads, and `window_is_backed` (WP2), the grid's own
answer about a window it proposes. Both land uncalled and nothing here takes a
proposer callable — that seam is WP4. Stage E is core/replay.py and is
unchanged.

Since 2.14.15 the D1 line above names the REFERENCE rather than what ships.
`find_candidate` picks from `backed_windows(grid, couple_weekend=True)` — D1+
in proposal §7.7 — and `schedules` is kept, uncalled by the add-on, as the
column that picker was measured against. §7.8 answered D-2 no: the model is not
admitted, WP4 is cancelled, and the seam named above is never built.

Split the way standby.py is split, and for the same concrete reason rather than
a stylistic one: everything from `intervals` to `find_candidate` takes rows and
a clock it was handed and returns a candidate or None, with no store, no Home
Assistant and no real time.time(). `scan` is the only I/O, and it records
nothing — what happens to a candidate is core/review.py's business, not a
detector's.


READING THE RECORD, WHICH IS NOT A TIME SERIES
----------------------------------------------
Everything standby.py's section of this name says applies here unchanged, and
it is the trap that matters most, because this module makes a claim about
twenty times as many hours. core/record.py stores one row per meaningful
transition: a flat draw crosses no threshold and writes nothing, and the only
reason it is visible at all is observer.NUMERIC_MAX_GAP_SECONDS writing a
heartbeat row every fifteen minutes into the silence.

So absence of rows means two different things. Fifteen minutes of it on a
watched, reporting entity means the value did not move. Two hours of it means
the agent was NOT LOOKING — a restart, an entity not yet watched, a stretch
record.prune() has already collapsed. Read as flatness, that second case
produces a rock-solid all-idle run across hours during which nothing was
observed, and does it most confidently at the old end of the record where the
evidence is thinnest. That is the failure this module is most exposed to, and
decision 1 is the answer to it.


THE DECISIONS
-------------
1. WHAT AN INTERVAL IS, AND WHAT IS NOT IDLE. Stage B condenses the record to
   (start, end, state), and the states a stretch can be in are idle, active,
   OFF, and not observed. The last is spelled by WHICH of three reasons it was
   — a gap the agent cannot see across, an entity that was gone, or a bucket
   mean — because that reason is what the evidence has to report, and an
   interval recording only "unobserved" would have to be walked a second time
   to find out why. A gap longer than COVERAGE_MAX_GAP_SECONDS, an
   'unavailable' or 'unknown' state, and a source='statistics' row all land
   there. None of them lands as idle. See `intervals`.

   OFF is the one that is easiest to leave out and costs the most. A reading
   under IDLE_MIN_WATTS is the device already dark — a timer the household
   already runs, a deep sleep, or a sensor reporting export as a negative
   number — and it is not a standing draw anybody can be offered. Counted as
   idle it lets a run extend straight across the hours the device was already
   off, and then `measure`'s per-occurrence minimum reports the OFF level as
   the draw: a nineteen-hour window claiming 0.17 kWh a month, or a negative
   one. standby.py has always refused that night as REASON_BELOW_FLOOR; this
   refuses that half-hour, under the same name and for the same reason.

2. IDLE AGAINST WHAT THRESHOLD. `levels.two_levels` over the whole window — day
   and night both, because the active cluster only exists in the daytime series
   — gives this device's own idle level and the floor above which it is in use,
   and a reading below that floor is idle. A device the histogram answers None
   for is unimodal, and produces no candidate at all: you cannot call 15 W
   "idle" without having seen what "in use" looks like on the same device, and
   a router, a NAS or an aquarium pump is exactly the device whose power must
   not be cut. That check is not an extra test here, it is structural — without
   two levels there is no threshold to build a grid from, so it runs first.

   Note the asymmetry with standby.py, which still uses the global
   STANDBY_MAX_WATTS for its in-use verdict. That is deliberate: moving standby
   onto a per-device floor would change automations users have already
   approved, and the proposal files it as an optional follow-on. A new detector
   with no installed base has no such debt.

3. WHEN A CELL COUNTS AS OBSERVED, AND WHEN IT COUNTS AS IDLE. A half-hour
   partly covered by unobserved time is the same question core/window.py
   answers with its "whole occurrence only" rule, asked about thirty minutes
   instead of about a night — and it is resolved the same direction, which is
   the direction this pipeline fails in everywhere else. Any unobserved second
   inside a cell and the cell was not observed at all: not idle, not active,
   not counted. A partly-covered half-hour is a shorter observation dressed up
   as a full one.

   Cells are placed by LOCAL wall clock, which the household's day is measured
   in and which core/window.py already owns. Twice a year that is visible:
   - on a spring-forward date a row of cells NEVER HAPPENED. `window.bounds`
     answers None for them, they are not placed, and any run containing one is
     simply not an occurrence on that date — the same refusal window.windows
     makes about a night an hour of which the clock ate.
   - on a fall-back date one hour HAPPENS TWICE. The cells tile the local date
     end to end, so the last cell before the repeat absorbs it and is ninety
     real minutes long. It counts as ONE occurrence, and it has to be idle
     across all ninety of those minutes to count as idle. Both directions cost
     the claim rather than flattering it.

4. REAL COVERAGE, NEVER AN ASSUMED NINETY DAYS. LOOKBACK_DAYS is 90 where
   standby's is 30, and the reason is the unit of the claim: a weekday claim
   needs weekday samples, and §2's table is blunt about it — ~4 samples of
   "Monday 11:00" at thirty days is a coincidence, ~13 at ninety is a pattern.
   The staleness standby avoids by reading only thirty days is handled here by
   a different rule instead: a cell has to be idle on EVERY observation, so a
   household that changed its habits three weeks ago has recent active
   occurrences in those cells and the run dies on them. Old evidence cannot
   outvote new evidence, it can only be outvoted by it.

   But the record may not HOLD ninety days. record.enforce_cap() halves the
   retention window repeatedly, down to a seven-day floor, on a home watching
   fast-reporting sensors — so a ninety-day read can come back with forty-five
   days or fewer, and a blob that printed the nominal window would be claiming
   coverage it never had. Every count below is therefore measured rather than
   assumed, and the evidence carries `window` (what was asked for) and
   `coverage` (what was actually there) as two separate fields on purpose.

5. WHAT D1 RETURNS, AND HOW WEEKDAYS ARE MERGED. Maximal contiguous runs of
   cells idle on every observation, per weekday, then merged across the
   weekdays that produced the IDENTICAL run into one core/window.py spec with
   its days set accordingly — which is what turns five separate Mon-Fri runs
   into the one weekday office pattern the proposal is about. Three sub-
   decisions, each argued again at the code:
   - a run does NOT wrap midnight (`_runs_for`);
   - MIN_RUN_SLOTS is the shortest run worth proposing, and MAX_RUN_SLOTS the
     longest: a device idle round the clock wants a window spanning several
     DATES, which the no-wrap rule already puts outside this module, and
     `window.spec` refuses to describe a twenty-four-hour one at all;
   - a weekday whose run is a NEAR miss is not merged (`schedules`).

6. THE EVIDENCE BLOB SPEAKS IN OCCURRENCES. CLAUDE.md's rule that `night_window`
   , `nights`, `nights_observed`, `nights_examined`, `nights_per_month` and
   `hours_per_day` may not be renamed is about blobs that are ON DISK on live
   installs — that is, about standby's. No blob of this kind exists anywhere, so
   this one is free to be honest, and "nights" would be a lie about a window
   that happens at 11:00 on a Tuesday. It says `occurrences`,
   `occurrences_observed`, `occurrences_examined`, `occurrences_per_month`,
   `hours_per_occurrence` and `schedule_window`. `excluded` keeps its name and
   its three reason strings keep their values: that is not dual-naming, it is
   the same word meaning the same thing about a different unit, and it is what
   lets one renderer read both blobs.

   The cost was named rather than papered over, and is now paid.
   `review.evidence_fingerprint` reads four blob fields beside the kind, and
   TWO of them do not exist here: `nights_per_month` and `night_window`. Their
   equivalents are
   `occurrences_per_month` and `schedule_window`, and the pass reads them
   through `EVIDENCE_FIELDS` below — declared by each detector, beside the
   code that writes the blob, so that the fingerprint asks the raising
   detector what it called them instead of guessing. Nothing here carries a
   second set of names so that the old fingerprint accidentally works: a
   fingerprint that silently matched on fields meaning something else is worse
   than one that has to be changed.
"""
from collections import namedtuple
from datetime import datetime, timedelta
import bisect
import logging
import time

from core import observer
from core import record
from core import watchlist
from core import window
from core.detectors import levels
from core.detectors import power
from tools.tool_homeassistant import registry

logger = logging.getLogger(__name__)

# The suggestion kind, and the prefix of every key this detector derives. Built
# from the entity alone for the reason standby.py's is — a key carrying a
# timestamp, a measured value or a rank moves between runs and puts the same
# waste in front of the user every morning as something new. That rules out the
# window label too, tempting as it is: a run that shifts by one half-hour is
# the same waste, and a key that moved with it would propose it twice.
KIND = "schedule"

# ...and what this detector's evidence blob calls the figures a reader has to
# find by NAME, in standby.py's words for the same set. This is the half of
# decision 3's named cost that is paid on this side: the blob says
# `occurrences_per_month` and `schedule_window` where a night blob says
# `nights_per_month` and `night_window`, and its counts say `occurrences`
# where a night blob says `nights`. core/review.py's fingerprint and
# core/artefacts.py's sentences read whichever the raising detector declares,
# rather than this module carrying a second set of names so that the night's
# reader accidentally works.
EVIDENCE_FIELDS = {
    "qualified": "occurrences",
    "observed": "occurrences_observed",
    "examined": "occurrences_examined",
    "per_month": "occurrences_per_month",
    "hours": "hours_per_occurrence",
    "window": "schedule_window",
}


# --------------------------------------------------------------------------
# the three states a stretch of the record can be in (decision 1)
# --------------------------------------------------------------------------
IDLE = "idle"
ACTIVE = "active"

# ...and a third OBSERVED verdict, which decision 1 originally left out and
# should not have. A reading below this is the device already switched OFF, not
# idling: the household (or a timer, or the device's own deep sleep) has
# already cut it, and there is nothing here to propose cutting. Read as idle it
# does two kinds of damage — a run extends straight across the hours the device
# was already dark, and `measure`'s per-occurrence MINIMUM then reports the OFF
# level as the standing draw, so a card claims a nineteen-hour window worth
# 0.17 kWh a month. A sensor reporting export as a negative number is the same
# failure with the sign flipped, and produces a negative saving.
#
# This is standby.REASON_BELOW_FLOOR's rule, its value and its name: "it really
# did drop — no standing draw". The constant is levels.IDLE_FLOOR_WATTS rather
# than a fourth copy of 2 x observer.NUMERIC_MIN_ABS_CHANGE, because it is
# exactly the same question that module already answers — below it a reading is
# not distinguishable from the sensor reading zero — and because a reading the
# histogram refused to bin must not then be called this device's idle level by
# the grid.
BELOW_FLOOR = "below_floor"
IDLE_MIN_WATTS = levels.IDLE_FLOOR_WATTS

# The three verdicts that mean the half-hour WAS observed, whatever it showed.
OBSERVED = (IDLE, ACTIVE, BELOW_FLOOR)

# ...and the third, spelled by which of three things it was. Every one of these
# is counted and reported in the evidence, because §9's rule is that the
# observation travels with the number, and "idle on twelve of thirteen
# occurrences" and "idle on twelve of forty" are not the same claim.
#
# The three strings hold the same values standby.py's do. They are not imported
# from it — a detector importing another detector is backwards, which is the
# whole of decision 0 — and they are not renamed either, so that one renderer
# can read the `excluded` dict of either blob.
REASON_NO_COVERAGE = "no_coverage"     # a gap the agent cannot see across
REASON_UNAVAILABLE = "unavailable"     # the entity was not reporting a number
REASON_STATISTICS = "statistics"       # bucket means; cannot evidence a floor

# In priority order: an occurrence can fail for several of these at once and
# the evidence names one, so it names the strongest thing true about it. The
# agent not looking beats the entity being gone beats a bucket mean, which is
# the order standby.assess_night runs its own checks in.
UNOBSERVED = (REASON_NO_COVERAGE, REASON_UNAVAILABLE, REASON_STATISTICS)
_REASON_RANK = {reason: rank for rank, reason in enumerate(UNOBSERVED)}

# One stretch of the record during which nothing changed: half-open [start,
# end), a state, and the watts that were in force if it was observed at all.
# A tuple because an interval is a value, and the watts travel with it because
# the measured figure is a minimum over the intervals a window covers — reading
# the rows a second time to find it would be a second implementation of "what
# was this device drawing".
Interval = namedtuple("Interval", "start end state watts")

# Silence longer than this is missing coverage, not a flat reading — the trap
# in this module's docstring. Twice the record's own heartbeat: one missed
# heartbeat is an add-on restart or a recorder pause and says nothing about the
# entity, while two consecutive misses mean the agent was not watching. The
# value and the argument are standby.COVERAGE_MAX_GAP_SECONDS's, restated
# rather than imported for the reason the reason strings above are.
COVERAGE_MAX_GAP_SECONDS = 2 * observer.NUMERIC_MAX_GAP_SECONDS


# --------------------------------------------------------------------------
# the grid (decision 3)
# --------------------------------------------------------------------------
# Half-hours, 7 x 48 = 336 cells, straight from proposal §3 stage C. Half an
# hour is the resolution a household schedule is actually written at — people
# leave at half past eight, not at 08:37 — and it is coarse enough that the
# record's fifteen-minute heartbeat leaves at least two rows in every cell, so
# a cell is never judged on a single reading.
SLOT_MINUTES = 30
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES

# One core/window.py spec per half-hour, built once at import. Using window.py
# rather than arithmetic here is what buys decision 3's whole DST answer for
# nothing: `window.bounds` already returns None when the clock skipped over
# either end, and the last slot's spec spans midnight exactly the way a night
# does, so the 48 of them tile a local date end to end with no gap and no
# overlap.
_CELL_SPECS = None  # built below, once _slot_clock exists

# The 7 x 48 of (times idle, times observed) the proposal names, and the
# per-date detail underneath it. Both, because they answer different questions
# and one cannot be derived from the other: the cells are stage C's output and
# what a rendering reads, while WHOLE occurrences of a multi-cell run can only
# be counted per date. The minimum of a run's cell counts is an UPPER bound on
# that, not a lower one — a cell observed on Mondays 1-8 and a neighbour
# observed on Mondays 9-16 both count eight, and the run was never once
# observed whole.
Grid = namedtuple("Grid", "cells dates")


# --------------------------------------------------------------------------
# the evidence floor (§7.1, decisions 4 and 5)
# --------------------------------------------------------------------------
# How far back one scan reads. Three times standby's, and decision 4 in the
# module docstring is the argument: the unit of this claim is a weekday, ~4
# samples of one is a coincidence and ~13 is a pattern. What the record
# actually held is measured and reported separately — this is a request, not a
# finding.
LOOKBACK_DAYS = 90

# Below this many observations a cell is not evidence, and below it a whole
# window is not either — one constant applied at both granularities, because
# they are the same question asked at two scales and a run that cleared the
# per-cell floor while its own whole occurrences did not would rest on fewer
# observations than every cell in it.
#
# Eight is where a habit stops being able to hide. A device used on one weekday
# in four leaves a clean cell with probability 0.75**8 = 0.10, so nine times in
# ten one of those occurrences is active and the run dies on it; at the four
# occurrences a thirty-day window offers that probability is 0.32, and a
# quarter-of-the-time habit would survive a third of the time. Eight
# occurrences of one weekday is eight weeks, which a ninety-day window offers
# with five to spare — and which a record squeezed to six weeks by
# record.enforce_cap() does not offer at all. That home gets no schedule
# suggestion, which is §7.1's answer rather than a gap: under the threshold a
# candidate is discarded, not shown with a weak number.
#
# There is deliberately no SEPARATE coverage-days floor of the kind
# standby.MIN_COVERAGE_DAYS is. This one already is one, expressed in the unit
# the claim is made in: a run cannot reach eight occurrences of a weekday
# inside a record shorter than fifty days, and a second floor counting
# calendar days would be the same refusal made less precisely.
MIN_OCCURRENCES = 8

# The shortest run worth proposing, in cells — two hours. Two reasons landing
# on one number. Below about this a stretch of idleness is the gap BETWEEN two
# uses rather than a part of the household's day, and a schedule built on it
# would cut power in the middle of an evening. And the device has to be off
# long enough to be worth switching: a relay has a finite number of cycles, and
# a NAS or a printer can take a good part of half an hour to come back, so a
# thirty-minute cut can cost more than the 0.1 kWh a month it saves.
MIN_RUN_SLOTS = 2 * 60 // SLOT_MINUTES

# ...and the longest, which is the whole day minus one cell. A run covering all
# 48 slots is a device idle round the clock on that weekday — a workshop tool
# plug used only at weekends, a holiday-flat appliance — and it is refused
# rather than proposed, for two reasons that are the same reason.
#
# core/window.py will not describe it: `spec("00:00", "00:00")` RAISES, because
# "a window with no ends is one nothing can be measured across, and guessing
# which of the two readings was meant is how a detector comes to claim
# twenty-four hours of anything". It is right to. And the automation such a
# window would build has no off period at all — one trigger cutting power at
# midnight and another restoring it at the same instant.
#
# What that device actually wants is "off from Monday until Saturday", a window
# spanning several DATES, which decision 5's no-wrap rule already puts outside
# this module. So the boundary is drawn here rather than at a spec constructor
# that would raise out of the daily pass and take every other suggestion,
# verification and rollback due that morning down with it.
MAX_RUN_SLOTS = SLOTS_PER_DAY - 1

# Thirty days to a month, for the kWh figure — standby's constant and its
# argument unchanged: the estimate is bounded rather than precise, and rounding
# the month down keeps it on the conservative side of the truth. It is a month
# of OCCURRENCES, never of calendar days: what gets multiplied by it is the
# rate at which the window was actually seen idle, on the weekdays it names.
PROJECTION_DAYS_PER_MONTH = 30

# How far a weekend day's idle run may reach past a window's edges and still be
# named by it, in cells — `backed_windows(couple_weekend=True)`'s allowance, and
# zero. Not a tuned threshold: every case in hand separates at zero. The
# weekday-plus-idle-Saturday shape carries eight cells of Saturday slack against
# the weekday window and must lose Saturday; the device idle 03:00-18:00 every
# day carries none on Saturday or Sunday and must keep both. Nothing recorded
# sits between the two, so any value from one to seven would draw the same line
# for a reason no record supplied, and would read afterwards as though one had.
# A future non-zero value has to arrive with the case that needs it.
WEEKEND_MAX_SLACK_SLOTS = 0


def _slot_clock(slot: int) -> str:
    """The wall-clock time a slot boundary sits at. Slot SLOTS_PER_DAY is
    midnight again, which is what makes the last cell of a date end where the
    next one begins."""
    minute_of_day = (int(slot) * SLOT_MINUTES) % (24 * 60)
    return window.clock(minute_of_day // 60, minute_of_day % 60)


_CELL_SPECS = tuple(window.spec(_slot_clock(slot), _slot_clock(slot + 1))
                    for slot in range(SLOTS_PER_DAY))


def _local_date(ts):
    """The local calendar date an instant falls on."""
    return datetime.fromtimestamp(int(ts)).date()


# --------------------------------------------------------------------------
# stage B: transitions -> intervals (decision 1)
# --------------------------------------------------------------------------
def _reading(row, idle_below: float, scale: float):
    """One row as (state, watts): what the record says was true from this
    row's timestamp until the next one's.

    The statistics test comes FIRST, before the value is even read. A
    source='statistics' row carries a perfectly good number — it is a bucket
    mean from Home Assistant's own five-minute or hourly aggregates — and that
    is exactly the problem: a device that ran for four minutes inside a
    five-minute bucket produces the same mean as one that sat still, and "idle
    throughout" is this module's entire claim. So the number is not read at
    all rather than read and distrusted.
    """
    if str(row.get("source") or "") == "statistics":
        return REASON_STATISTICS, None
    value = observer.as_float(row.get("state"))
    if value is None:
        # 'unavailable' and 'unknown' are recorded deliberately and are not
        # zero watts — the device may have been drawing anything at all.
        return REASON_UNAVAILABLE, None
    watts = value * float(scale)
    if watts < IDLE_MIN_WATTS:
        # Off, not idle — and the watts travel anyway, because it is a reading.
        return BELOW_FLOOR, watts
    return (IDLE if watts < idle_below else ACTIVE), watts


def _gap_reason(state) -> str:
    """Which of the two silences this is, judged by the state it starts from.

    Both look identical in the record — no rows — and they are different facts
    about the home. Silence after a NUMBER is the agent not watching: a
    restart, an entity not yet on the watchlist, a stretch record.prune() has
    already collapsed. Silence after 'unavailable' is the ENTITY being gone,
    and the record says so exactly once, because observer.should_record's
    string branch suppresses the repeats. Reporting both as "no coverage" would
    tell a reader the agent was not looking when it was looking at a device
    that had disappeared.
    """
    return REASON_UNAVAILABLE if state == REASON_UNAVAILABLE else REASON_NO_COVERAGE


def intervals(rows, start, end, idle_below: float, scale: float = 1.0) -> list:
    """The record between two instants as contiguous stretches of one state.

    `rows` is the entity's transitions; anything outside [start, end) is
    ignored. What comes back TILES that span — every second belongs to exactly
    one interval, with no gaps and no overlaps — which is what lets a cell's
    verdict be read off the intervals it touches without also checking that
    they covered it.

    A stretch between two rows more than COVERAGE_MAX_GAP_SECONDS apart is
    unobserved IN FULL, rather than the earlier reading being held for as long
    as the heartbeat would have guaranteed it. That would be the false
    confidence this module's docstring is about: silence does not say WHEN the
    agent stopped looking, so a restart ten seconds after the row leaves the
    next half hour just as unread as the rest of it.

    The head and the tail are unobserved for a stronger version of the same
    reason. Between two rows, the second row is itself proof that the agent was
    still watching across the gap, which is what makes a short one readable at
    all. Before the first row and after the last there is no such row, so there
    is nothing the silence can be read against — which is also standby.py's
    rule about a night with nothing before it.
    """
    start, end = int(start), int(end)
    readings = []
    for row in sorted((r for r in (rows or []) if r.get("ts") is not None),
                      key=lambda r: int(r["ts"])):
        ts = int(row["ts"])
        if start <= ts < end:
            readings.append((ts, _reading(row, idle_below, scale)))

    if not readings:
        return [Interval(start, end, REASON_NO_COVERAGE, None)] if end > start else []

    out = []
    if readings[0][0] > start:
        out.append(Interval(start, readings[0][0], REASON_NO_COVERAGE, None))
    for index, (ts, (state, watts)) in enumerate(readings):
        if index + 1 < len(readings):
            next_ts = readings[index + 1][0]
            if next_ts - ts > COVERAGE_MAX_GAP_SECONDS:
                out.append(Interval(ts, next_ts, _gap_reason(state), None))
            else:
                out.append(Interval(ts, next_ts, state, watts))
        else:
            out.append(Interval(ts, end, _gap_reason(state), None))
    return out


# --------------------------------------------------------------------------
# stage C: intervals -> the weekly grid (decision 3)
# --------------------------------------------------------------------------
def _cell_verdict(series, starts, cell_start, cell_end) -> str:
    """What one half-hour, on one date, was: IDLE, ACTIVE, or the reason it was
    not observed.

    Decision 3's whole-occurrence rule lives in the order of the two tests at
    the end. ANY unobserved second inside the cell and the answer is that
    reason, whatever else the cell also showed — a half-hour partly covered by
    a blind spot is a shorter observation dressed up as a full one, and this is
    the same refusal window.windows makes about a night half inside a range.
    """
    reason = None
    saw_active = False
    saw_off = False
    first = max(0, bisect.bisect_right(starts, cell_start) - 1)
    for interval in series[first:]:
        if interval.start >= cell_end:
            break
        if interval.end <= cell_start:
            continue
        if interval.state in _REASON_RANK:
            if reason is None or _REASON_RANK[interval.state] < _REASON_RANK[reason]:
                reason = interval.state
        elif interval.state == ACTIVE:
            saw_active = True
        elif interval.state == BELOW_FLOOR:
            saw_off = True
    if reason is not None:
        return reason
    # In use beats already-off when a cell held both: "somebody used this" is
    # the stronger statement about a half-hour, and either one is equally fatal
    # to a run, so nothing downstream turns on which is reported.
    if saw_active:
        return ACTIVE
    return BELOW_FLOOR if saw_off else IDLE


def weekly_grid(series) -> Grid:
    """Stage C: 7 weekdays x 48 half-hours, each carrying how often it was
    observed and how often it was idle, plus the per-date detail a run is
    counted from.

    The span is taken from the intervals themselves rather than passed
    alongside them, because they tile it — a start and end that could disagree
    with the series is a second source for one fact.

    A cell is PLACED on a date only when `window.bounds` gives it two ends and
    both of them fall inside the span. Everything else is None, and None is not
    a fourth state: it means that half-hour is not an observation this window
    holds either way. Two things land there, and they land there for different
    reasons that are worth keeping straight —
      - the clock skipped it. On a spring-forward date those wall-clock times
        never happened, so there is nothing to have observed. The cost is the
        same one window.py's docstring already accepts: a home loses one
        occurrence a year, out of a claim that needs eight.
      - it fell outside the read. The first and last dates of any window are
        partial, and a cell half-read is the same refusal as a cell
        half-observed.
    Neither is counted as examined, which is what stops a run's denominator
    including dates on which the window did not occur.

    A fall-back date is the other direction and needs no special case at all:
    the specs tile the local date end to end, so the cell that ends at the
    repeated hour's far side is ninety real minutes long and has to be idle
    across all of them. One occurrence, measured strictly.
    """
    if not series:
        return Grid({}, {})
    start, end = series[0].start, series[-1].end
    starts = [interval.start for interval in series]

    # Pre-populated whole, so the grid is always the 336 cells the proposal
    # names and a reader can ask about any of them — including the ones this
    # record never reached, whose answer is zero observations rather than a
    # KeyError.
    cells = {(weekday, slot): {"idle": 0, "observed": 0}
             for weekday in range(7) for slot in range(SLOTS_PER_DAY)}
    dates = {}

    day = _local_date(start)
    last = _local_date(end)
    while day <= last:
        verdicts = []
        for slot in range(SLOTS_PER_DAY):
            bounds = window.bounds(_CELL_SPECS[slot], day)
            if bounds is None or bounds[0] < start or bounds[1] > end:
                verdicts.append(None)
                continue
            verdict = _cell_verdict(series, starts, bounds[0], bounds[1])
            verdicts.append(verdict)
            if verdict in OBSERVED:
                cell = cells[(day.weekday(), slot)]
                cell["observed"] += 1
                if verdict == IDLE:
                    cell["idle"] += 1
        dates.setdefault(day.weekday(), []).append((day, tuple(verdicts)))
        day += timedelta(days=1)
    return Grid(cells, dates)


# --------------------------------------------------------------------------
# the grid, rendered for a model (§3 stage D2 / decision 3 — WP1 only)
# --------------------------------------------------------------------------
# grid_text lands with no caller — the module docstring's "BUILT AHEAD OF ITS
# CALLER" applies to it exactly as it does to `schedules` itself. WP2's
# core/scheduler_llm.py is what reads this text; nothing in this change
# writes a prompt or makes a request, and nothing here may start.
#
# The one real decision is the character set: one ASCII character per cell,
# carrying BOTH its state and a bucketed observation count, rather than a
# 48-character symbol row with counts reported separately (as a per-run
# table, say). That single choice is what makes all five of §7's rendering
# constraints hold at once rather than trading one against another:
#
#   - three states, not two. '?' (never observed) is its own symbol, not the
#     low end of the "sometimes active" scale. This module's own opening
#     section names the failure the whole package exists to avoid — an
#     unobserved stretch reading as flatness — and a rendering that let
#     "never observed" and "observed and mixed" share an alphabet would be
#     asking WP2's validator to recover a distinction this text had already
#     thrown away.
#   - the count travels with the cell. A cell seen twice and a cell seen
#     thirteen times render as different characters ('2' against a capped
#     '9'), because the count IS the character rather than a footnote next
#     to it.
#   - the size is fixed, not merely usual. 336 cells is 336 characters of
#     grid body no matter what the record holds. A per-run count table does
#     not have that property — a device flickering between states every
#     half-hour turns a run-length summary into hundreds of short entries,
#     and "verify the 1-2 KB claim" would then depend on the household
#     rather than on the format. One character per cell cannot blow out.
#   - nothing to infer. The header states the three symbols before the grid
#     appears, so a model reading this cold has the whole encoding in the
#     first five lines.
#
# The count is capped rather than carried in full — see GRID_TEXT_COUNT_CAP.
GRID_TEXT_COUNT_CAP = 9
# Nothing downstream of this text asks a numeric question sharper than "did
# this clear MIN_OCCURRENCES" (8): the model that reads this grid proposes a
# window, it does not grade one — D-3's validator re-reads the real `cells`
# for that, never this rendering. A single-digit cap keeps every comparison
# the grid needs to make ("this is thin", "this is not") intact while
# keeping the alphabet to one character per cell; carrying the true count in
# full would cost a second character on every cell that cleared the floor by
# any margin, for a distinction nothing reads.

# Bucket 1..9 in a second alphabet, so a "sometimes active" cell's count
# reads on the same scale as an "idle every time" cell's digit without the
# two states sharing a character.
_MIXED_MARKS = "abcdefghi"

# window.py's own convention for a household's short weekday names, kept as
# a second copy rather than importing window._DAY_NAMES: that name is
# underscored because it is window.label's own implementation detail, and
# this module already owns the Mon..Sun ordering everywhere else in it (the
# grid is indexed by Python's weekday() numbering, and so is this).
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Every four hours (eight half-hour slots), which is the same spacing the
# proposal's own hand-drawn sketch in §3 stage C used for its column labels.
_RULER_STEP_SLOTS = 8


def _cell_mark(cell) -> str:
    """One character for one cell. '?' if it was never observed; otherwise a
    digit if it was idle on every observation, or a letter if it was not —
    both alphabets carrying the same bucketed count, so a cell's confidence
    reads the same regardless of which state it landed in."""
    observed = cell["observed"]
    if observed == 0:
        return "?"
    bucket = min(observed, GRID_TEXT_COUNT_CAP)
    if cell["idle"] == observed:
        return str(bucket)
    return _MIXED_MARKS[bucket - 1]


def _ruler(indent: str) -> str:
    """A once-per-grid time axis. Advisory only: WP2's validator checks a
    proposed window against the real cells, never against where a label
    happens to land in this text, so a label is allowed to run into the
    following column rather than earning a second line to avoid it."""
    row = [" "] * SLOTS_PER_DAY
    for slot in range(0, SLOTS_PER_DAY, _RULER_STEP_SLOTS):
        for offset, char in enumerate(_slot_clock(slot)):
            if slot + offset < SLOTS_PER_DAY:
                row[slot + offset] = char
    return indent + "".join(row)


def grid_text(grid) -> str:
    """Stage C's grid as text a model can read — §3 stage D2's input, built
    here because the grid is this module's own data and WP2's
    core/scheduler_llm.py is what will import the detector for it, never the
    other way round.

    Reads `cells` only. `dates` answers "was this whole run observed on this
    date", which is what an occurrence COUNT is built from
    (`_count_run`) — nothing about describing the grid's SHAPE needs that,
    and reaching into it here would make this a second, looser
    re-derivation of `_runs_for` rather than a rendering of stage C's actual
    output.

    See the section comment above this function for the one real decision
    (a single character per cell, carrying both state and count) and its
    argument against the alternative of a symbol row plus a separate count
    table. At the full 336 cells this renders to under 700 bytes including
    the header and ruler — comfortably inside §3's "1-2 KB" estimate even
    though, unlike the estimate's own sketch, every cell here also carries
    its count.

    A cell is read with `_runs_for`'s own `.get(...) or default`, not a bare
    index: `weekly_grid` promises all 336 keys but does not always keep that
    promise — an empty series makes it return `Grid({}, {})` — so this
    reads the same way `_runs_for` already does about the same dict rather
    than trusting the doc comment over the code next to it.
    """
    indent = "    "
    lines = [
        "Weekly idle grid: 48 half-hour slots per day, 00:00-24:00 local, Mon-Sun.",
        "One character per cell:",
        "  ?    never observed",
        "  1-9  idle on every observation; digit = times observed (capped at 9)",
        "  a-i  observed, active at least once; a=1 .. i=9 times observed (capped)",
        _ruler(indent),
    ]
    for weekday in range(7):
        row = "".join(_cell_mark(grid.cells.get((weekday, slot)) or {"idle": 0, "observed": 0})
                      for slot in range(SLOTS_PER_DAY))
        lines.append(f"{_WEEKDAY_ABBR[weekday]} {row}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the same grid, read off in clock times (the prompt's input)
#
# `grid_text` above is the honest full picture and `runs_text` below is what
# core/scheduler_llm.py actually sends. Both exist, and which one the prompt
# carries was decided by measurement rather than taste.
#
# The character grid asks its reader to convert a position into a time: the
# idle stretch on a Monday row is "the run of digits starting at index 15,
# nineteen characters long", and 07:30-17:00 is only reachable by counting
# those characters. Asked to do nothing else — no rules, no priors, just
# "report the longest run of digits per row" — asi1-mini answered slot 16 to
# slot 34 on a grid whose run is slot 15 to slot 33, and then rendered its own
# slot 34 as 18:00 rather than 17:30. Two errors, one in counting and one in
# the arithmetic, on the one device in the fixture set with a real office
# pattern. That is what the live comparison was reading as a model answering
# from a prior about office hours: a model that cannot index the grid falls
# back on what an idle window usually looks like, and 09:30-15:30 is what that
# sounds like.
#
# So this renders the stretches themselves, in clock times, and the counting
# stops being the model's job.
#
# NOTHING IS LOST BY SENDING THIS INSTEAD. That is the load-bearing claim and
# it is exact rather than approximate. `window_is_backed` admits a window only
# where every cell it covers, on every day it names, was idle on all of its
# observations and was observed at least MIN_OCCURRENCES times — which is
# `_runs_for`'s `qualifies`, cell for cell. So every window that can ever
# validate is a sub-range of one of these stretches on each of its days, and a
# stretch shorter than MIN_RUN_SLOTS cannot contain one at all. The
# character grid carries strictly more detail; none of the extra detail can
# change which answers are legal.
#
# What it DOES give up is the ability to see a near-miss: a half-hour idle on
# twelve of thirteen observations vanishes here, where the character grid
# would have shown it as a letter. That is deliberate. Such a cell cannot be
# inside a legal window, so showing it can only invite an answer the validator
# then refuses — and a refusal costs the day's suggestion.
#
# The size argument grid_text was built on survives. A run table is unbounded
# in principle — the comment above GRID_TEXT_COUNT_CAP makes exactly that
# objection against one — but the MIN_RUN_SLOTS filter bounds it by FORMAT
# rather than by household: at four cells minimum, no day can carry more than
# SLOTS_PER_DAY // MIN_RUN_SLOTS stretches, so this is at most 84 lines
# however much the device flickers, and in practice one or two a day.
# --------------------------------------------------------------------------
def _run_clock(first_slot, last_slot) -> str:
    """One run as a household reads it. The end is written 24:00 rather than
    the 00:00 `_slot_clock` returns for slot SLOTS_PER_DAY: the modulo there
    is right for building a spec, where midnight is midnight, and wrong in a
    line a reader scans top to bottom, where "07:30-00:00" reads as a window
    running backwards."""
    end = _slot_clock(last_slot + 1)
    return f"{_slot_clock(first_slot)}-{'24:00' if last_slot + 1 == SLOTS_PER_DAY else end}"


def runs_text(grid) -> str:
    """The grid's idle stretches per weekday, in clock times — the text
    core/scheduler_llm.py puts in front of the model.

    Reads `cells` only, through `_runs_for`, so this is a RENDERING of the
    detector's own run scan rather than a second, looser re-derivation of it.
    D1 and D2 therefore disagree about which stretch to propose and never
    about which stretches exist.

    A day with no stretch says which of the two reasons applies. They are not
    the same fact and the difference is the one this detector is most exposed
    to: "the device was in use" is evidence, and "nobody watched" is the
    absence of it. A model told only "nothing on Sunday" has no way to tell a
    weekend in use from a weekend the record never reached, and the second is
    where a window nobody can contradict comes from.
    """
    lines = [
        "Idle stretches in this device's record, over the last ninety days, local time.",
        "A stretch is listed only where the device was idle on EVERY observation of every",
        f"half-hour in it, and every one of those half-hours was watched at least "
        f"{MIN_OCCURRENCES} times.",
        "Stretches shorter than "
        f"{MIN_RUN_SLOTS * SLOT_MINUTES // 60} hours are left out; a window may not be that short.",
        "",
    ]
    for weekday in range(7):
        runs = _runs_for(grid.cells or {}, weekday)
        if runs:
            detail = ", ".join(_run_clock(first, last) for first, last in runs)
        elif any((grid.cells.get((weekday, slot)) or {"observed": 0})["observed"]
                 for slot in range(SLOTS_PER_DAY)):
            detail = "no stretch — the device was in use somewhere in every one"
        else:
            detail = "nothing watched on this weekday at all"
        lines.append(f"  {_WEEKDAY_ABBR[weekday]}  {detail}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# does the grid back this window? (§7.2 decision 3, checks 1-3)
# --------------------------------------------------------------------------
def _slot_bounds(spec):
    """The half-hour cells a window covers, as an inclusive (first, last).

    The end is taken to the cell it lands in or past — floor on the start,
    ceiling on the end — so a window that is NOT on the lattice is checked
    against a superset of the cells it touches rather than a subset. That is
    the fail-closed direction: the extra cell can only add a refusal, never
    hide one. It is not a licence to send a misaligned window here, which
    core/scheduler_llm.py refuses outright (decision 3's check 6, and its
    reason: a window the grid cannot be indexed by is one none of the checks
    below mean anything about).
    """
    start_minute = spec.start_hour * 60 + spec.start_minute
    end_minute = spec.end_hour * 60 + spec.end_minute
    first_slot = start_minute // SLOT_MINUTES
    last_slot = -(-end_minute // SLOT_MINUTES) - 1
    return first_slot, last_slot


def window_is_backed(grid, spec):
    """Whether this grid's own cells back this window — the reason it does
    not, or None if they do.

    The first three of decision 3's six checks, and they live here rather than
    in core/scheduler_llm.py because they are questions about the GRID, which
    is this module's data: every cell the window covers idle on every
    observation of it, every one of those cells clearing MIN_OCCURRENCES in
    its own right, and the named days observed at all. The other three are
    about the window's own shape and belong with the thing that asked for it.
    The same separation core/observer.py and core/observation.py are split on:
    the rule is testable against a literal grid with no client anywhere near
    it.

    §7.1 is why this exists at all rather than leaving it to the grader.
    Replay only catches a window the record PROVES was in use, and this
    module's own docstring names the failure it is most exposed to —
    unobserved stretches read as flatness. A window laid across half-hours
    nobody watched passes grading cleanly, because there is no evidence to
    contradict it. So the question asked here is not "did this go wrong", it
    is "was this ever watched", which only the cells can answer.

    It REFUSES; it never repairs. There is deliberately no return path that
    hands back a trimmed window, a clamped one or the subset of days that
    would have passed — a window that is not backed is discarded whole, and
    the reason is returned so the log says which cell killed it.

    A reason names the FAULT and never the window. The caller has the spec and
    prints its label once in front of whichever reason came back, so a reason
    carrying its own copy would put the window in the log line twice.
    """
    if window.spans_midnight(spec):
        # A floor beneath decision 3's check 4, not a second copy of it: that
        # check is about what an automation may do (builder._no_midnight's
        # reason), this is about what can be looked up. The grid is cut at
        # local midnight, so a window running through it has its cells under
        # two weekday keys and there is nothing here to index.
        return "runs through local midnight, which the grid is cut at"

    unseen = {weekday for weekday in spec.days
              if not any((grid.cells.get((weekday, slot)) or {"idle": 0, "observed": 0})["observed"]
                         for slot in range(SLOTS_PER_DAY))}
    if unseen:
        # Checked a day at a time before the per-cell walk below, which would
        # also catch it but would report it as one thin cell rather than as a
        # weekday this record has nothing about at all.
        return f"{window.days_phrase(unseen)} never observed at all"

    first_slot, last_slot = _slot_bounds(spec)
    for weekday in sorted(spec.days):
        for slot in range(first_slot, last_slot + 1):
            cell = grid.cells.get((weekday, slot)) or {"idle": 0, "observed": 0}
            if cell["observed"] < MIN_OCCURRENCES:
                return (f"{_WEEKDAY_ABBR[weekday]} {_slot_clock(slot)} was observed "
                        f"{cell['observed']} times, under the {MIN_OCCURRENCES} a cell needs")
            if cell["idle"] != cell["observed"]:
                return (f"{_WEEKDAY_ABBR[weekday]} {_slot_clock(slot)} was active on "
                        f"{cell['observed'] - cell['idle']} of its {cell['observed']} observations")
    return None


# --------------------------------------------------------------------------
# the grid's classified shape (D-4, WP3 — core/schedule_cache.py's key)
# --------------------------------------------------------------------------
# The three marks `grid_fingerprint` reduces a cell to. Not `grid_text`'s
# alphabet: that one also carries a bucketed COUNT, which is exactly the part
# that must not be in a cache key (see the function's docstring).
_FP_UNUSABLE = "u"
_FP_IDLE = "i"
_FP_ACTIVE = "a"

# The default for a cell `weekly_grid` never wrote — same value `grid_text`
# and `window_is_backed` fall back to, shared here rather than re-literalled
# per miss since this loop runs the full 336 cells every call. Never mutated.
_FP_DEFAULT_CELL = {"idle": 0, "observed": 0}


def grid_fingerprint(grid) -> str:
    """The grid's classified SHAPE, as a 336-character string, one mark per
    (weekday, slot) in `grid_text`'s own order (weekday-major, slot-minor) —
    never over raw `observed`/`idle` counts.

    core/schedule_cache.py exists because `schedule.scan` re-reads a live
    window every morning: a date drops off the back and a new one joins the
    front, so nearly every cell's `observed` count moves by one every single
    day. A fingerprint over those numbers would change daily, and the cache
    would then miss every time while looking like it worked — review
    decision 1's trap (core/review.py), in this module's costume.

    So each cell is reduced to the one tri-state distinction anything
    downstream actually asks about — the boundary `window_is_backed` tests,
    at MIN_OCCURRENCES, which is NOT `grid_text`'s own boundary of `observed
    == 0`: a cell seen twice reads as an ordinary digit in `grid_text` but
    marks 'u' here, because two observations do not clear the floor a window
    needs to rest on.
      - 'u' unusable: never observed, or observed short of MIN_OCCURRENCES;
      - 'i' backed idle: observed enough, AND idle on every observation;
      - 'a' backed active: observed enough, AND active at least once.

    THE INVARIANT THIS BUYS, AND WHY IT MAKES THE CACHE SAFE: two grids with
    equal fingerprints give `window_is_backed` the same answer for every
    possible window. Its midnight guard depends only on the WINDOW's own
    shape, never on the grid, so it needs nothing derived from this string
    at all. Its never-observed-weekday guard looks like a second question
    but is not one: a weekday that fails it has `observed == 0` on every one
    of its cells, which is a special case of `observed < MIN_OCCURRENCES` —
    the same 'u' a weekday gets from thin-but-nonzero observation — so the
    per-cell loop below refuses on that weekday's first cell for the same
    reason the guard would, just phrased differently in the log line. Every
    check the function makes therefore reduces to the one per-cell question
    this fingerprint already answers (`observed < MIN_OCCURRENCES`, `idle !=
    observed`), so two fingerprints that agree, agree cell-for-cell on both
    questions everywhere. A test in tests/test_schedule_detector.py asserts
    the None/not-None equivalence directly rather than leaving it as an
    argument.

    Not hashed to an opaque digest. 336 bytes goes into JSON unchanged, and
    stays something a person can read when asking why a cache entry did or
    did not match — `review.evidence_fingerprint`'s own reason for staying a
    readable tuple rather than a hash.
    """
    marks = []
    for weekday in range(7):
        for slot in range(SLOTS_PER_DAY):
            cell = grid.cells.get((weekday, slot)) or _FP_DEFAULT_CELL
            observed = cell["observed"]
            if observed < MIN_OCCURRENCES:
                marks.append(_FP_UNUSABLE)
            elif cell["idle"] == observed:
                marks.append(_FP_IDLE)
            else:
                marks.append(_FP_ACTIVE)
    return "".join(marks)


# --------------------------------------------------------------------------
# stage D1: the grid -> a schedule (decision 5)
# --------------------------------------------------------------------------
def _runs_for(cells, weekday) -> list:
    """Maximal contiguous (first slot, last slot) runs of cells that were idle
    on EVERY observation and observed often enough to say so.

    The loop runs 0..47 and stops, which is decision 5's first half: a run does
    NOT wrap midnight. It could — core/window.py specs span midnight and
    standby.py's night nearly does — and it is left out for three reasons. The
    grid is keyed by weekday, so a wrapping run's cells live under two of them
    and "the days it starts on" stops being "the days it was observed on",
    which is the exact ambiguity window.py's start-day convention exists to
    remove. The overnight stretch is already standby.py's window, and a second
    detector proposing it would put two suggestions about one device in front
    of one user. And the cost is only that an always-idle device is described
    as two true windows rather than one — local midnight is the seam the grid
    is already cut on, and cutting there is what keeps a run's weekdays
    unambiguous.
    """
    runs = []
    opened = None
    for slot in range(SLOTS_PER_DAY):
        cell = cells.get((weekday, slot)) or {"idle": 0, "observed": 0}
        qualifies = (cell["observed"] >= MIN_OCCURRENCES
                     and cell["idle"] == cell["observed"])
        if qualifies and opened is None:
            opened = slot
        elif not qualifies and opened is not None:
            runs.append((opened, slot - 1))
            opened = None
    if opened is not None:
        runs.append((opened, SLOTS_PER_DAY - 1))
    return [run for run in runs
            if MIN_RUN_SLOTS <= run[1] - run[0] + 1 <= MAX_RUN_SLOTS]


def _strongest(verdicts):
    """Which reason an occurrence is excluded under when more than one applies.

    None when nothing in the run was unobserved — which cannot happen for a
    run this module built, since every cell in one is idle on every observation
    it has, so a placed cell is IDLE or unobserved and never ACTIVE. Left as
    None rather than defaulted to a plausible reason: a null in the blob is
    visible, and a wrong reason counted under a real name is not.
    """
    present = [verdict for verdict in verdicts if verdict in _REASON_RANK]
    return min(present, key=_REASON_RANK.__getitem__) if present else None


def _count_run(rows, first_slot, last_slot) -> dict:
    """How often one weekday's run actually happened, was observed whole, and
    was idle — counted per DATE rather than per cell, for the reason the Grid
    tuple gives."""
    idle_on = []
    examined = 0
    excluded = {}
    for day, verdicts in rows:
        cells = verdicts[first_slot:last_slot + 1]
        if any(verdict is None for verdict in cells):
            # The window did not occur whole on this date, so it is not an
            # occurrence of it — window.windows' rule, applied to a run.
            continue
        examined += 1
        if all(verdict == IDLE for verdict in cells):
            idle_on.append(day)
            continue
        reason = _strongest(cells)
        excluded[reason] = excluded.get(reason, 0) + 1
    return {"dates": idle_on, "examined": examined, "excluded": excluded}


def occurrences_per_month(spec, observed: int, examined: int) -> float:
    """How often this window comes round in a month, at the rate it was
    actually seen idle rather than at the rate the calendar offers it.

    One implementation, because it is used twice — once to rank the runs
    against each other and once to write the figure into the evidence — and two
    would be free to drift. The shape is standby.measure_nights's: what gets
    projected is `observed / examined`, never 1.0, so a device seen idle on
    half its Mondays is credited with half of them. The weekday fraction is the
    part that is new, and it is what keeps a Mon-Fri window at about 21 a month
    instead of 30.
    """
    if not examined:
        return 0.0
    return (float(observed) / float(examined)
            * PROJECTION_DAYS_PER_MONTH * len(spec.days) / 7.0)


def _slot_spec(first_slot, last_slot, days):
    """The core/window.py spec a run of cells describes. The only constructor
    used anywhere here — a run is turned into a window once, and every label
    printed downstream is window.label of it."""
    return window.spec(_slot_clock(first_slot), _slot_clock(last_slot + 1), days)


def schedules(grid) -> list:
    """Stage D1: every window this device is idle across, best first.

    Runs are merged across weekdays only when they are IDENTICAL — the same
    first and last slot — which is decision 5's third half and the deliberate
    under-reach in this module. A Monday run of 07:00-17:00 and a Tuesday run
    of 07:30-17:00 are left as two, even though their intersection would be a
    true claim about both days. Three reasons. An intersection is not maximal
    on either weekday, so it is a window nothing in the record picked out.
    Choosing among the intersections of seven weekdays means ranking a longer
    window on fewer days against a shorter one on more, and D1 has no rule for
    that which is not arbitrary. And the thing that DOES have one is the model
    in stage D2 — the proposal's argument for letting it into the pipeline at
    all is that it holds priors about human life no clustering has, and can
    answer "weekday office pattern, 08:30-17:00 Mon-Fri" while declining to
    extend it to Saturday. This is the seam that work package plugs into, and
    §7.1's instinct in the meantime is to discard rather than to show a weak
    number.

    Ranked by hours x occurrences per month — the saving with the watts left
    out, which is the only part of it a grid can know. The tie-break is the
    window's own start, so two runs worth the same amount come back in the same
    order on every run of the pass.

    NO LONGER WHAT `find_candidate` PICKS FROM, since 2.14.15 — that is
    `backed_windows(grid, couple_weekend=True)`, and the paragraph above about
    the model's seam is history: §7.8 answered D-2 no, because the intersection
    scan did everything the model did without one. Kept unchanged rather than
    deleted because it is the reference: the D1 column §7.7's reading measured
    D1+ against, and the only way to re-run that comparison if shipping D1+
    It has to be called BY NAME to be that, and scripts/compare_proposers.py
    learned this the hard way: its D1 column read the window off
    `find_candidate` until 2.14.15, which was right while that picked from
    here and became silently wrong the moment it picked from `backed_windows`
    — both of the script's deterministic columns then came from one function
    and it reported the two proposers agreeing on every device in the house.
    """
    by_slots = {}
    for weekday, rows in sorted((grid.dates or {}).items()):
        for first_slot, last_slot in _runs_for(grid.cells or {}, weekday):
            counted = _count_run(rows, first_slot, last_slot)
            # The per-cell floor above said the CELLS were seen often enough;
            # this says the WINDOW was. They differ only when coverage came and
            # went inside the day rather than by the date, and when they do it
            # is this one the claim actually rests on.
            if len(counted["dates"]) < MIN_OCCURRENCES:
                continue
            by_slots.setdefault((first_slot, last_slot), []).append(counted)

    found = []
    for (first_slot, last_slot), per_weekday in by_slots.items():
        days = frozenset(day.weekday() for counted in per_weekday
                         for day in counted["dates"])
        spec = _slot_spec(first_slot, last_slot, days)
        dates = sorted(day for counted in per_weekday for day in counted["dates"])
        examined = sum(counted["examined"] for counted in per_weekday)
        excluded = {}
        for counted in per_weekday:
            for reason, count in counted["excluded"].items():
                excluded[reason] = excluded.get(reason, 0) + count
        per_month = occurrences_per_month(spec, len(dates), examined)
        found.append({
            "window": spec,
            "slots": (first_slot, last_slot),
            "dates": dates,
            "examined": examined,
            "excluded": excluded,
            "occurrences_per_month": per_month,
            "device_hours_per_month": window.hours(spec) * per_month,
        })
    found.sort(key=lambda run: (-run["device_hours_per_month"],
                                window.starts_at(run["window"])))
    return found


def _bounds_the_window(first_slot, last_slot, run) -> bool:
    """Whether a day whose covering idle run is `run` is one the window
    [first_slot, last_slot] describes: its SLACK — the cells the run reaches
    past the window, (first - run start) + (run end - last) — is within
    WEEKEND_MAX_SLACK_SLOTS.

    Slack is measured over the run that COVERS the candidate and no other. A
    day's idleness elsewhere in it — a separate early-morning stretch, say — is
    not idleness the window's edges could have been set by, and counting it
    would refuse a day whose one relevant run stops exactly where the window
    does."""
    slack = (first_slot - run[0]) + (run[1] - last_slot)
    return slack <= WEEKEND_MAX_SLACK_SLOTS


def backed_windows(grid, couple_weekend=False) -> list:
    """Stage D1 without the identical-runs restriction: every window this grid
    backs on any set of weekdays, ranked by the same quantity `schedules()`
    ranks by. `find_candidate` picks from this, with `couple_weekend=True`,
    since 2.14.15. It landed uncalled beside `schedules()` so that the harness
    could measure the proposers apart, and proposal §7.7 is that measurement:
    over eight watched devices graded by replay, the model proposer was
    preferred over both deterministic ones 0 times out of 8, and this function
    beat it on `desk_setup` and `server_rack` and matched it everywhere else.
    §7.8 answered D-2 on that reading, and this is the proposer that ships.

    WHY IT EXISTS. `schedules()` merges a weekday's runs into a window only
    when they are IDENTICAL, and its own docstring calls the intersection of a
    Monday 07:00-17:00 and a Tuesday 07:30-17:00 "a window nothing in the
    record picked out". That under-reach is not theoretical and its size is
    measured: on the commuter shape in scripts/bench_scheduler_prompt.py —
    every weekday's edges half an hour apart — `schedules()` can merge only
    Monday with Thursday and proposes a two-day window worth 0.73 kWh a month,
    while the five-day intersection it cannot name is worth 1.45. In the
    quantity actually ranked here that is 81 device-hours a month against 161,
    and proposal §7.5 records the same pair from the other side: it is row 1 of
    the bench table, the gap a model closed exactly.

    WHY THE RANKING IS NOT A NEW ARBITRARINESS. The second of `schedules()`'
    three reasons is that choosing among the intersections of seven weekdays
    means ranking a longer window on fewer days against a shorter one on more,
    "and D1 has no rule for that which is not arbitrary". But `schedules()`
    already ranks the runs it does find by `device_hours_per_month`, and picks
    a first among them on that rule alone. Applying one rule to a larger
    candidate set is not a second rule. What the intersections genuinely do
    need a prior for is the case the metric gets WRONG — §3 stage D2's weekday
    pattern plus an idle Saturday, where the highest score is the answer the
    proposal says to refuse — and that coupling is NOT part of the ranking. It
    is `couple_weekend` below, off unless asked for, so that the harness can
    measure the reach and the prior separately rather than reading one number
    for both.

    THE EDGE PREFILTER, AND WHY IT LOSES NOTHING. Candidate ends are taken only
    from the set of run STARTS and the set of run ENDS, never from all 48 x 48
    pairs, and that is lossless rather than a shortcut. Fix any set of days D.
    The largest window every day in D backs is [max of their run starts, min of
    their run ends] — both ends are run edges by construction. Any other window
    backed by exactly D sits inside that one: same days, fewer hours, and since
    the score is hours x days it scores strictly lower, so it can never be the
    best answer for D. Every day set therefore has its optimum at a pair of run
    edges, and enumerating those pairs enumerates every optimum there is. The
    prefilter also bounds the cost by the FORMAT rather than by the household:
    a weekday has a handful of qualifying runs, so this is tens of candidates
    and not thousands — which matters because `scan` runs it daily over every
    watched sensor.

    Windows that run through local midnight are left in exactly as
    `schedules()` leaves them. `find_candidate` is what refuses those, for
    `builder._no_midnight`'s reason, and it refuses both functions' output the
    same way; filtering here would put that rule in a second place.

    THE WEEKEND COUPLING, `couple_weekend`. The one shape the ranking gets
    wrong is §3 stage D2's own example, and the real record for
    sensor.spare_room_desk_power_draw has it: idle 08:00-16:30 Mon-Fri,
    08:00-20:30 Saturday, nothing on Sunday. Read without a prior this returns
    08:00-16:30 Mon-Sat at 219 device-hours a month against Mon-Fri's 182, and
    Mon-Sat is the answer the proposal says to decline. The weekday window's
    hours are set by a household leaving for work and coming back; Saturday's
    idleness is not that, and next Saturday they may be home all day.

    A window's hours are set by the days that BOUND it — the days whose idle
    run stops exactly where the window stops. A day with zero slack
    (`_bounds_the_window`) is one the window's own edges describe. A weekend
    day with slack is idle past those edges, so the window's boundary is not
    describing that day at all: the device was idle then for a reason of its
    own, and asserting the weekday boundary on it is a claim its record does
    not make. So a window may name a weekend day only when that day's slack is
    zero. It is a slack test and not "weekends are different": a device idle
    03:00-18:00 every day, whose Mon-Wed and Fri runs reach back to midnight,
    keeps Saturday and Sunday, because their runs stop exactly at 03:00 and
    18:00.

    ONLY THE WEEKEND, on purpose. sensor.landing_media_hub_power_draw is idle
    08:00-17:00 Mon-Thu and 08:00-16:00 Friday, and the right answer is
    08:00-16:00 Mon-Fri — on which Monday to Thursday each carry two cells of
    slack. The same test applied to working days would throw that window away
    and leave Friday alone. What separates the weekend is a prior about human
    life, which is the one thing the cells cannot supply; `window.WEEKENDS` is
    that prior, and encoding it as a constant is the point of the rule.

    And ONLY WHEN THE WINDOW ALSO NAMES A WORKING DAY. A Saturday-and-Sunday
    window has no weekday boundary to be inconsistent with, and testing it
    would refuse a legitimate weekend-only device for nothing. "Names" means
    the days that survive the per-weekday floor, which is why the test is
    applied to the admitted set as a whole rather than day by day as each is
    counted: a Monday the floor drops does not make a window a weekday window.

    The edge prefilter stays lossless under it. A window without a weekend day
    is untouched; one with a weekend day must end exactly on that day's run
    edges, which are in the enumerated sets by construction.

    Default False, and nothing may flip it here: the harness has to read the
    reach and the prior as separate numbers, and making the prior the default
    is a decision for once that reading exists.
    That reading now exists (§7.7), and the decision was taken at the CALLER:
    `find_candidate` passes True. The default stays False so the harness can
    still read the two apart.

    The aggregation below is a second copy of `schedules()`' own, deliberately.
    Factoring it out would mean editing `schedules()`, and the whole point of
    landing this beside it is that the shipped proposer is untouched while both
    are being read against each other.
    The shipped proposer is now this one, and `schedules()` is kept unchanged
    as the reference it was measured against — so the copy stays for the same
    reason, from the other side.
    """
    cells = grid.cells or {}
    by_weekday = grid.dates or {}

    runs = {weekday: _runs_for(cells, weekday) for weekday in range(7)}
    starts = {run[0] for spans in runs.values() for run in spans}
    ends = {run[1] for spans in runs.values() for run in spans}

    found = []
    for first_slot in sorted(starts):
        for last_slot in sorted(ends):
            if not MIN_RUN_SLOTS <= last_slot - first_slot + 1 <= MAX_RUN_SLOTS:
                continue
            admitted = []
            for weekday in range(7):
                # A day backs the candidate when ONE of its runs covers it
                # whole. Not two adjacent ones: `_runs_for` returns maximal
                # runs, so two of them are separated by a cell that failed the
                # idle-on-every-observation test, and a window spanning that
                # cell is exactly what must not be proposed. Runs are disjoint,
                # so at most one covers.
                covering = next((run for run in runs[weekday]
                                 if run[0] <= first_slot and last_slot <= run[1]),
                                None)
                if covering is None:
                    continue
                counted = _count_run(by_weekday.get(weekday) or [],
                                     first_slot, last_slot)
                # `schedules()`' own per-weekday floor, for its own reason: the
                # cells were seen often enough, and this says the WINDOW was.
                # Applied per weekday rather than to the total, so a candidate
                # keeps the days it can evidence and drops the ones it cannot
                # rather than being carried by its strongest day.
                if len(counted["dates"]) < MIN_OCCURRENCES:
                    continue
                admitted.append((weekday, covering, counted))

            names_working_day = any(weekday not in window.WEEKENDS
                                    for weekday, _run, _counted in admitted)
            per_weekday = [
                counted for weekday, covering, counted in admitted
                if not (couple_weekend and names_working_day
                        and weekday in window.WEEKENDS
                        and not _bounds_the_window(first_slot, last_slot, covering))
            ]
            if not per_weekday:
                continue

            days = frozenset(day.weekday() for counted in per_weekday
                             for day in counted["dates"])
            spec = _slot_spec(first_slot, last_slot, days)
            dates = sorted(day for counted in per_weekday for day in counted["dates"])
            examined = sum(counted["examined"] for counted in per_weekday)
            excluded = {}
            for counted in per_weekday:
                for reason, count in counted["excluded"].items():
                    excluded[reason] = excluded.get(reason, 0) + count
            per_month = occurrences_per_month(spec, len(dates), examined)
            found.append({
                "window": spec,
                "slots": (first_slot, last_slot),
                "dates": dates,
                "examined": examined,
                "excluded": excluded,
                "occurrences_per_month": per_month,
                "device_hours_per_month": window.hours(spec) * per_month,
            })
    found.sort(key=lambda run: (-run["device_hours_per_month"],
                                window.starts_at(run["window"])))
    return found


# --------------------------------------------------------------------------
# what the device was drawing while it sat there
# --------------------------------------------------------------------------
def _lowest(series, starts, span_start, span_end):
    """The lowest reading observed across one span, or None if none was."""
    lowest = None
    first = max(0, bisect.bisect_right(starts, span_start) - 1)
    for interval in series[first:]:
        if interval.start >= span_end:
            break
        if interval.end <= span_start or interval.watts is None:
            continue
        if lowest is None or interval.watts < lowest:
            lowest = interval.watts
    return lowest


def measure(series, spec, dates, examined: int, excluded=None) -> dict:
    """The arithmetic over one window's idle occurrences: what the device drew
    across them, and what that is worth over a month.

    Separated from `find_candidate` the way standby.measure_nights is, and for
    the same reason: §7.2's verification takes the same measurement again a
    week later, and a baseline computed by a second implementation of "the
    median of the per-occurrence minima" would be a number free to drift from
    this one without anything failing. The FLOORS are not here — what makes a
    measurement a candidate is find_candidate's business, and a baseline is
    recorded whatever it comes to, including a week nothing could be read in,
    which comes back with every figure EMPTY rather than zeroed.

    `watts` is the median of the per-occurrence minima — the level this device
    demonstrably never went under while the window was running, on a typical
    occurrence. The minimum because "never dropped below" is the claim and a
    mean would assert a level the device was sometimes under; the median across
    occurrences so one unusual day cannot move it.
    """
    starts = [interval.start for interval in series]
    minima = []
    for day in dates or []:
        bounds = window.bounds(spec, day)
        if bounds is None:
            continue
        lowest = _lowest(series, starts, bounds[0], bounds[1])
        if lowest is not None:
            minima.append(lowest)

    hours = window.hours(spec)
    if not minima:
        # Nothing was measured, and the derived figures are left EMPTY rather
        # than zeroed, exactly as standby.measure_nights leaves them: a
        # baseline reporting 0.0 kWh for a week nobody could read is
        # indistinguishable from one reporting a device that genuinely drew
        # nothing, and §7.2's verification would then report the whole of the
        # after as a loss.
        return {
            "watts": None,
            "watts_range": None,
            "hours_per_occurrence": hours,
            "kwh_per_month": None,
            "occurrences_per_month": None,
            "occurrences": 0,
            "occurrences_observed": 0,
            "occurrences_examined": int(examined),
            "excluded": dict(excluded or {}),
        }

    # Counted from the minima rather than from `dates`, so the number printed
    # is the number of occurrences that produced a reading. The two are the
    # same for a window built by `schedules` — an occurrence idle throughout
    # was observed throughout — and where they ever differ it is this one the
    # figure beside it was computed from.
    seen = len(minima)
    per_month = occurrences_per_month(spec, seen, examined)
    watts = round(observer.median(minima), 1)
    return {
        "watts": watts,
        "watts_range": [round(min(minima), 1), round(max(minima), 1)],
        "hours_per_occurrence": hours,
        "kwh_per_month": round(watts * hours * per_month / 1000.0, 2),
        # What the figure above was projected over, stated rather than left to
        # be re-derived: §9 wants the sentence writable from the evidence
        # without arithmetic.
        "occurrences_per_month": round(per_month, 1),
        "occurrences": seen,
        # Every occurrence observed whole was idle — that is what a run IS —
        # so this equals `occurrences` for a window this module proposes. It is
        # still stated, because the reader of a blob should not have to know
        # that to read the exclusions underneath it.
        "occurrences_observed": seen,
        "occurrences_examined": int(examined),
        "excluded": dict(excluded or {}),
    }


# --------------------------------------------------------------------------
# the device's own two levels (decision 2)
# --------------------------------------------------------------------------
def device_levels(rows, scale: float = 1.0):
    """The device's own two levels — {"idle_watts", "active_floor"} — or None
    when it only ever had one. A caller of core/detectors/levels.py, over the
    readings core/detectors/power.py pulls out of the rows.

    The WHOLE window's transitions, day and night both, and that is the point
    rather than an oversight: the active cluster exists only in the hours the
    device is used, so a check restricted to the hours this module's claim is
    about could never see the thing that makes the claim true.

    There is no minimum-readings floor of its own here, unlike
    standby.device_levels. levels.MIN_MODE_READINGS is already one and is the
    honest one — it asks whether either cluster was ever demonstrated, which is
    the actual question — and a second floor derived from this detector's own
    counts would refuse the same devices for a less direct reason. Both
    refusals fall the same way in any case: "too little data to tell whether
    this is a router" is not a weaker version of "this device is idle all day",
    it is the absence of the observation the claim rests on.
    """
    return levels.two_levels(power.readings(rows, scale))


# --------------------------------------------------------------------------
# the whole judgement
# --------------------------------------------------------------------------
def find_candidate(entity_id: str, rows, switch_entity_id=None, now=None,
                   lookback_days: int = LOOKBACK_DAYS, scale: float = 1.0):
    """Transitions in, candidate or None out.

    Returns {"key", "kind", "entities", "evidence"} ready for
    suggestions.upsert_candidate, or None — and None is the ordinary answer.
    Most devices have no weekly shape, most young records cannot reach eight
    occurrences of a weekday, and neither is an error.

    `rows` are re-sorted AND clipped to the window this call is judging, for
    standby.find_candidate's reason: the window is derived here from `now` and
    `lookback_days` rather than taken from the caller, so a caller handing over
    more history than it asked to be judged would otherwise have the coverage
    figures below satisfied by rows no cell ever looked at, and the stored
    evidence would contradict the window printed beside it.

    ONE candidate, from the best window, even when the grid holds several. Two
    windows on one device are two questions, and §9's rule is that a suggestion
    is answered in one word; the others are still in the grid tomorrow if this
    one is dismissed.
    """
    if not entity_id:
        return None
    if not switch_entity_id:
        # Enforced in the rules rather than in the caller, so the discard is
        # testable without a registry — and so a second caller cannot forget
        # it. Idleness nobody can act on is a fact about the house, not a
        # suggestion.
        logger.debug(f"SCHEDULE: {entity_id} has no controllable switch — not a candidate")
        return None

    end = int(now) if now else int(time.time())
    start = end - int(lookback_days) * 86400
    rows = sorted(
        (r for r in (rows or [])
         if r.get("ts") is not None and start <= int(r["ts"]) < end),
        key=lambda r: int(r["ts"]))

    # Decision 2, and it runs first because it is structural rather than an
    # extra test: without two levels there is no threshold to call a cell idle
    # against, so a unimodal device — a router, a NAS, an aquarium pump, a
    # relay behind a boiler — never reaches a grid at all.
    device = device_levels(rows, scale=scale)
    if device is None:
        logger.debug(f"SCHEDULE: {entity_id} only ever showed one level — a device "
                     "doing its job, not one waiting to be used")
        return None

    series = intervals(rows, start, end, device["active_floor"], scale=scale)
    grid = weekly_grid(series)
    found = backed_windows(grid, couple_weekend=True)
    if not found:
        return None

    # The best window THIS AUTOMATION SHAPE CAN EXPRESS, which is not always
    # `found[0]`. A run ending at slot 47 ends at local midnight, and
    # `window.spans_midnight` calls that a crossing — 22:00-00:00 is a true
    # claim about the device, `backed_windows` is right to report it, and it is one
    # `builder._no_midnight` refuses and `replay.plan_for` cannot bound (the
    # restore would be before the cut). Skipped HERE rather than left to the
    # builder, because a refusal that arrives there takes the whole device with
    # it: the next run in `found` is still a window this shape can propose, and
    # the one the grid ranked second is a better answer than none.
    best = next((run for run in found if not window.spans_midnight(run["window"])), None)
    if best is None:
        logger.debug(f"SCHEDULE: every window found for {entity_id} ends at local "
                     "midnight, which this automation shape cannot express")
        return None
    spec = best["window"]
    measured = measure(series, spec, best["dates"], best["examined"], best["excluded"])
    if measured["watts"] is None:
        return None

    observed_cells = [cell for cell in grid.cells.values() if cell["observed"]]
    covered_days = len({_local_date(row["ts"]) for row in rows})
    return {
        "key": f"{KIND}:{entity_id}",
        "kind": KIND,
        "entities": [entity_id, switch_entity_id],
        # Written to be read by a person, because §9 renders it as a sentence
        # and treats that rendering as the design constraint rather than the
        # fallback: the hours, the weekdays, the watts, how often it was seen,
        # and what was left out and why. Not a dump — the 336 cells stay in the
        # grid, and the rows stay in the record.
        "evidence": {
            "entity": entity_id,
            "switch": switch_entity_id,
            # The measurement travels whole rather than being restated field by
            # field: it is one calculation, and its figures only multiply out
            # together.
            **measured,
            # The device's own two levels, travelling whole for the same
            # reason. Recorded as evidence of how the claim was reached rather
            # than as part of the claim — and `active_floor` is load-bearing
            # here in a way it is not in standby.py: it is the threshold every
            # one of those 336 cells was judged against.
            **device,
            # The window as §9 prints it, rendered once by core/window.py —
            # "07:00-17:00 local Mon-Fri". Nothing here writes a label itself.
            "schedule_window": window.label(spec),
            # ...and the same window as the two ends and the weekdays it is
            # made of, so the work package that writes the automation rebuilds
            # the spec rather than parsing the sentence above. Not a second
            # name for one field: one of them is prose and one of them is the
            # value, and the prose is what a person is shown.
            "schedule": {
                "start": window.starts_at(spec),
                "end": window.ends_at(spec),
                "days": sorted(spec.days),
            },
            # What was ASKED for...
            "window": {
                "from": _local_date(start).isoformat(),
                "to": _local_date(end).isoformat(),
                "days": int(lookback_days),
            },
            # ...how much of it the record actually held, under the name every
            # shape states it by. standby.py's blob calls it this and §9's
            # rendering prints one sentence from it for either shape, which is
            # why it is here as well as inside `coverage` below: the sub-blob
            # is decision 4's audit record and reads as a set, and a renderer
            # that had to know this detector nests it would be a renderer that
            # knows about detectors. One expression, stated twice, and never
            # two counts.
            "covered_days": covered_days,
            # ...and what was actually there, which is decision 4's whole
            # point: record.enforce_cap() can have halved the retention window
            # more than once, and a blob printing only the field above would be
            # claiming ninety days of coverage it never had.
            "coverage": {
                "days_requested": int(lookback_days),
                # Calendar dates the record holds at least one row on, which
                # is standby.covered_days' statistic and reads against the
                # figure above at a glance: 90 asked, 46 there, and the date
                # below says where the record actually starts. A window that
                # begins mid-afternoon touches one more date than it spans, so
                # this can be `days_requested + 1` on a record with no gaps at
                # all — the comparison that matters is the order of magnitude.
                "days_observed": covered_days,
                "measured_from": (_local_date(rows[0]["ts"]).isoformat()
                                  if rows else None),
                "cells_observed": len(observed_cells),
                "cells_at_floor": sum(1 for cell in observed_cells
                                      if cell["observed"] >= MIN_OCCURRENCES),
                "cells_total": len(grid.cells),
            },
            # The rules that produced the numbers above travel with them, so a
            # reader disagreeing with the conclusion can see which threshold to
            # argue with rather than having to find this file. `idle_below_w`
            # is this DEVICE's floor rather than a global constant, which is
            # the asymmetry with standby.py decision 2 names.
            "thresholds": {
                "idle_below_w": device["active_floor"],
                "min_occurrences": MIN_OCCURRENCES,
                "min_hours": MIN_RUN_SLOTS * SLOT_MINUTES / 60.0,
                "slot_minutes": SLOT_MINUTES,
                "max_gap_s": COVERAGE_MAX_GAP_SECONDS,
            },
        },
    }


# --------------------------------------------------------------------------
# the thin layer
# --------------------------------------------------------------------------
def scan(now=None, lookback_days: int = LOOKBACK_DAYS, snapshot=None) -> list:
    """Reads the record for every watched power sensor and returns the windows
    it found, most valuable first — each candidate carrying the series it was
    judged on.

    The I/O half, and all of it: one registry round trip, the switch pairing,
    and one bounded record read per sensor. It exposes a function and schedules
    nothing, it records nothing, and it proposes nothing to a home — for
    standby.scan's reasons, which are worth restating because they are the
    shape of every detector here. What calls it is the reasoning schedule's
    business. What happens to a candidate is the approval gate's. And the
    upsert into core/suggestions.py belongs to the review pass, because the
    decision at that call site is whether today's evidence differs enough from
    yesterday's to throw away a replay — a decision about replays, which this
    module does not know about.

    `snapshot` is a registry the caller has already read. The daily pass reads
    one and hands it to everything that needs the same three answers — the
    states, the entity rows, the device grouping — so a second detector running
    in the same pass costs no second round trip.

    `rows`, `scale` and `name` travel with the candidate for the reason
    core/replay.py's docstring gives: the grading has to run over the same
    series the measurement came from, and a caller that read the record again
    would grade the config against a window that had moved underneath it.

    Only a candidate's rows are held. An entity that produced no candidate has
    its series dropped where it was read — which matters more here than in
    standby.scan, because this reads ninety days rather than thirty, and ninety
    days times every watched sensor must never be in memory at once.
    """
    watched = set(watchlist.watched_entities())
    if not watched:
        return []

    if snapshot is None:
        ok, data = registry.fetch_registry()
        if not ok:
            logger.warning(f"SCHEDULE: could not read the registry — skipping this scan ({data})")
            return []
        snapshot = data
    data = snapshot

    states = data.get("states") or {}
    entity_rows = data.get("entities") or []
    by_device = registry.entities_by_device(entity_rows)
    names = registry.names_from(data)
    registry_of = {
        row.get("entity_id"): row
        for row in entity_rows
        if isinstance(row, dict) and row.get("entity_id")
    }

    end = int(now) if now else int(time.time())
    start = end - int(lookback_days) * 86400

    found = []
    for entity_id in sorted(watched):
        registry_row = registry_of.get(entity_id)
        scale = power.power_scale(states.get(entity_id), registry_row)
        if scale is None:
            continue
        switch_entity_id = power.controllable_switch(
            entity_id, (registry_row or {}).get("device_id"), by_device)
        if not switch_entity_id:
            # Nothing to act on — and nothing to read, so the record is not
            # scanned for it either.
            continue
        rows = record.between(entity_id, start, end)
        candidate = find_candidate(entity_id, rows, switch_entity_id,
                                   now=end, lookback_days=lookback_days, scale=scale)
        if not candidate:
            continue
        candidate["rows"] = rows
        candidate["scale"] = scale
        # The SWITCH's name, not the sensor's: it is the thing that will be
        # switched off, and the automation's alias is what a household reads
        # months later.
        candidate["name"] = names.get(switch_entity_id, "")
        found.append(candidate)

    found.sort(key=lambda c: c["evidence"]["kwh_per_month"], reverse=True)
    if found:
        logger.info(f"SCHEDULE: {len(found)} idle window(s) found across "
                    f"{len(watched)} watched entity(ies)")
    return found
