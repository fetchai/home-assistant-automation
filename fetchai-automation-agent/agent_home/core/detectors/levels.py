"""What a device's own two levels are — a histogram, not a median.

Stage A of docs/PROPOSAL-SCHEDULE-DETECTOR.md §3: histogram an entity's
readings, find the modes, require a real valley between them, and answer with
the device's idle level and the floor above which it is in use — or None when
the device only ever showed one level.

Deliberately its own module rather than a second method on standby.py. The
proposal's whole argument for building this now is that the next work
package — the schedule detector's weekly grid — asks the identical question
("was this device idle") for every one of 336 cells, and a second
implementation of "the device's two levels" is exactly the drift CLAUDE.md
argues against. So it is built once, here, with no notion of a night, a
window, or a switch — those are standby.py's business, not this one's.

Pure and stdlib only, importing nothing from core/ except core/observer.py:
this sits BELOW every detector rather than beside one, so it must not depend
on any of them. `numpy` is not in requirements.txt and is not being added for
this — a histogram over at most a few thousand floats does not need it.

WHY A HISTOGRAM AND NOT A MEDIAN
---------------------------------
The 2.13.0 stand-in took the median of the whole window as the idle level and
a multiplicative threshold above it. That assumes the device spends most of
its life idle, which is true of a set-top box and false of a desktop or an
always-on media player: used more than half the time, its median lands ON the
active level, the floor moves to three times THAT, and a real night plateau is
rejected. A histogram does not care which cluster is larger — it asks whether
there are two clusters at all, and if so, reports the lower one, whichever
holds more readings.

WHY THE LOWEST MODE, EVEN WHEN IT ISN'T THE BIGGEST
------------------------------------------------------
This is the whole fix. Picking the largest mode, or the median, is exactly
what rejects the heavily-used device the 2.13.0 note describes. Idle is a
statement about the LEVEL a device sits at when nobody is using it, not about
which level it sits at most often — a desktop idling 40% of the day and
working 60% of it is still idle at the lower of its two levels.

OFF, IDLE AND ACTIVE ARE THREE CLUSTERS, NOT TWO
--------------------------------------------------
A device genuinely switched off for part of the window reads near zero. If
that near-zero cluster were allowed to be a mode, "the lowest mode" would pick
it: idle_watts would come back near zero, active_floor would collapse to
almost nothing, and every device would clear it — the inverse of the bug this
module exists to fix, and worse. The fix is IDLE_FLOOR_WATTS: a reading below
it never joins a bin at all, so an off-cluster never becomes a candidate mode
in the first place and the lowest mode that DOES form is the device's real
idle level. This is deliberately not the same rule as
standby.MIN_QUALIFYING_FRACTION, which already discards "a plug the user
switches off themselves" on a different axis (how many NIGHTS stayed below
the floor) — duplicating that judgement here would just put it in two places.
"""
import math

from core import observer

# --------------------------------------------------------------------------
# the bin scale
# --------------------------------------------------------------------------
# Same value as standby.PLATEAU_SPREAD_FRACTION — "flat" is a relative idea
# everywhere in this codebase, not an absolute one — duplicated rather than
# imported because this module sits BELOW every detector: importing
# standby.py from here would make a detector-agnostic module depend on one
# specific detector, backwards from how the two are meant to relate.
#
# Divided by three so that three adjacent bins together span exactly the
# width one genuine plateau is allowed to move across: three consecutive 10%
# steps compound to 1.10**3 ≈ 1.331, matching 1 + 0.30. A wider fraction would
# spread one real plateau across too few bins to look like a mode at all; a
# narrower one would smear it across so many that a single plateau could be
# mistaken for two.
BIN_WIDTH_FRACTION = 0.30 / 3
BIN_RATIO = 1.0 + BIN_WIDTH_FRACTION

# Below this a reading is not distinguishable from the sensor reading zero —
# 2 x observer.NUMERIC_MIN_ABS_CHANGE, the same value and the same reasoning
# as standby.STANDBY_MIN_WATTS: the record cannot resolve a movement smaller
# than that at all, so nothing under it is a level anybody could act on.
# Duplicated for the reason BIN_WIDTH_FRACTION is: this module does not import
# standby.py.
#
# This is also decision 5's whole answer. A reading below the floor never
# joins a bin, so an off-cluster (or a sensor reporting export as a negative
# number) can never become a candidate mode — "the lowest mode" then finds the
# device's real idle level rather than the level at which it is switched off.
IDLE_FLOOR_WATTS = 2 * observer.NUMERIC_MIN_ABS_CHANGE

# How far apart, as a ratio, two modes must sit before the gap between them is
# a state change rather than the same state read twice. The 2.13.0 note this
# module answers named this ACTIVE_GAP_MULTIPLE; the value and its reasoning
# both carry over unchanged, only the home has moved: FLAT's 30% cubed is
# about 2.2, and three times leaves margin on that — a DVR idling at 15 W and
# recording at 25 W (1.67x) still does not clear it, a television idling at
# 12 W and playing at 120 W (10x) clears it easily.
MODE_GAP_MULTIPLE = 3.0

# The valley's width, in bins, that the ratio above corresponds to on this bin
# scale — derived, not picked a second time: how many BIN_RATIO steps compound
# to MODE_GAP_MULTIPLE.
VALLEY_MIN_WIDTH_BINS = math.ceil(math.log(MODE_GAP_MULTIPLE) / math.log(BIN_RATIO))

# Decision 3 asked for a second, independent test here — how EMPTY the valley
# is, not just how WIDE — reasoned about below and then found not to need a
# constant of its own. A device transitioning between states can leave one
# genuine reading mid-ramp, and the worry was that a valley required to be
# perfectly empty would be defeated by exactly that noise. But `_raw_runs`
# only ever returns maximal contiguous stretches of nonzero bins, so every bin
# strictly between two of ITS entries is zero by construction — there is no
# partially-occupied valley for a second test to measure between adjacent raw
# runs; that stray mid-ramp reading is not sitting IN a gap, it forms its OWN
# one- or two-count raw run instead. It is MIN_MODE_READINGS below that does
# the job "empty enough" was reaching for: too small a run to be a state,
# dropped from `qualifying` in two_levels exactly like a router's flat line
# would be, regardless of whether _modes left it standing alone or merged it
# into a neighbour on width. A prior version of this file carried a separate
# VALLEY_MAX_OCCUPANCY_FRACTION constant computing the busiest bin in that
# same always-empty range — a check on a quantity that is provably always
# zero, confirmed by instrumenting this function and fuzzing 200 randomised
# bimodal-plus-noise inputs without ever observing a nonzero occupancy — so it
# is retired here rather than kept as a threshold that never triggers.

# How many readings a cluster needs before it is a state rather than a spike.
# Unchanged from 2.13.0's ACTIVE_MIN_READINGS, and so is its reasoning: the
# question is whether a state was ever demonstrated, not how often, so this is
# absolute rather than a fraction of the series — a television watched twice a
# week is as much a television as one watched nightly. Twenty readings is five
# hours of a value held at the record's heartbeat, or about twenty minutes of
# real use, since a device being used moves and so writes at
# observer.NUMERIC_MIN_INTERVAL_SECONDS instead. It now applies to EITHER
# cluster, not only the active one, because a histogram has no built-in notion
# of which mode is which until after this test runs.
MIN_MODE_READINGS = 20


# --------------------------------------------------------------------------
# the histogram
# --------------------------------------------------------------------------
def _bin_index(value):
    """Which geometric bin a reading falls in, or None below the floor.

    Negative values (a sensor reporting export) fail the floor test the same
    way a too-small positive one does — both are excluded rather than binned,
    which is what stops either from ever becoming a candidate idle level.
    """
    if value is None or value < IDLE_FLOOR_WATTS:
        return None
    return int(math.floor(math.log(value / IDLE_FLOOR_WATTS) / math.log(BIN_RATIO)))


def _bin_edges(index):
    """(low, high) watts this bin covers, low inclusive and high exclusive —
    every value that mapped to `index` satisfies low <= value < high."""
    low = IDLE_FLOOR_WATTS * (BIN_RATIO ** index)
    return low, low * BIN_RATIO


def _run(grouped, lo, start_offset, end_offset):
    """One maximal contiguous stretch of occupied bins, as the readings it
    holds rather than only their count — the values travel because idle_watts
    is a median over them, taken once modes have been decided."""
    values = []
    for offset in range(start_offset, end_offset + 1):
        values.extend(grouped.get(lo + offset, []))
    return {"lo": lo + start_offset, "hi": lo + end_offset,
            "count": len(values), "values": values}


def _raw_runs(grouped, counts, lo):
    """Every maximal contiguous stretch of nonzero bins, ascending — the
    candidate modes before any two of them are judged close enough to merge.

    A trailing zero sentinel lets the loop's own `elif` close whatever run is
    still open when `counts` ends, rather than repeating that append after
    the loop for the one still-open case.
    """
    runs = []
    start = None
    end = None
    for offset, count in enumerate(counts + [0]):
        if count > 0:
            if start is None:
                start = offset
            end = offset
        elif start is not None:
            runs.append(_run(grouped, lo, start, end))
            start = None
    return runs


def _modes(grouped):
    """Raw runs merged into modes: two runs are the same mode when the gap
    between them is narrower than VALLEY_MIN_WIDTH_BINS — decision 3's WIDTH
    half of "wide enough and empty enough".

    The EMPTY half turns out not to need a second, independent test here, for
    the reason argued above VALLEY_MIN_WIDTH_BINS's old neighbour: `_raw_runs`
    only ever returns maximal contiguous stretches of nonzero bins, so every
    bin strictly between two of its entries is zero by construction — the gap
    this function measures can never be anything but empty, and
    MIN_MODE_READINGS is what actually catches the noise decision 3 was
    worried about.
    """
    lo, hi = min(grouped), max(grouped)
    counts = [len(grouped.get(i, [])) for i in range(lo, hi + 1)]
    raw_runs = _raw_runs(grouped, counts, lo)

    modes = [raw_runs[0]] if raw_runs else []
    for run in raw_runs[1:]:
        current = modes[-1]
        width = run["lo"] - current["hi"] - 1
        if width >= VALLEY_MIN_WIDTH_BINS:
            modes.append(run)
        else:
            current["hi"] = run["hi"]
            current["count"] += run["count"]
            current["values"].extend(run["values"])
    return modes


def two_levels(values):
    """The device's own two levels — {"idle_watts", "active_floor"} — from a
    flat list of watt readings, or None when it only ever showed one.

    `values` is every numeric reading the caller wants judged, day and night
    both if the caller wants a cluster that only exists by day — this module
    has no opinion about which hours matter, that is entirely the caller's
    window to choose. Already scaled to watts and already filtered to numbers:
    skipping 'unavailable'/'unknown' and applying a sensor's own scale are
    read-the-record decisions the caller is closer to, not a histogram's.

    `idle_watts` is the median of the LOWEST qualifying mode's own readings —
    decision 2's fix, not a refinement of the old one: picking the largest
    mode, or the median of the whole series, is exactly what rejected a
    heavily-used device before. `active_floor` is the bin edge immediately
    above that mode's own readings — the level at which the record stops
    describing this device as idle, which by construction (every value in the
    mode is strictly below that edge) is always above idle_watts. It is a
    threshold, not a measurement: nothing downstream reads it yet.
    """
    grouped = {}
    for value in values or []:
        index = _bin_index(value)
        if index is None:
            continue
        grouped.setdefault(index, []).append(value)
    if not grouped:
        return None

    qualifying = [mode for mode in _modes(grouped) if mode["count"] >= MIN_MODE_READINGS]
    if len(qualifying) < 2:
        return None

    idle = min(qualifying, key=lambda mode: mode["lo"])
    return {
        "idle_watts": round(observer.median(idle["values"]), 1),
        "active_floor": round(_bin_edges(idle["hi"])[1], 1),
    }
