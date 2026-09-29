"""The standby-load detector: a device that never stops drawing power overnight.

First of design §8's four suggestion types, and the one chosen to go first
because its whole loop is honest — the measure is watts, the saving is
arithmetic, and there is no comfort trade-off to weigh. What it looks for is a
*plateau*: a stretch of the night during which a power sensor never drops below
some level, night after night, on a device nobody was using.

Deterministic, and that is the point rather than a simplification. §7.1 draws
the line — "the model proposes candidates; it does not get to grade them" — and
a detector that asked an LLM whether a series looked like standby would make
every number downstream unfalsifiable: nobody could reproduce it, argue with
it, or say why yesterday's answer differed from today's. Every threshold below
is therefore a named constant with its reasoning attached, because a magic
number here is a number nobody can argue with later.

Split the way core/observer.py and core/observation.py are split. Everything
from `night_windows` to `find_candidate` is pure: it takes an ordered list of
transitions and returns a candidate or None, with no store, no clock it did not
receive and no Home Assistant. `scan` is the thin layer that fetches the series
and pairs it to a switch. The reason is concrete rather than stylistic — the
evidence floor here is fourteen days and ten nights, and no test harness in
this repo can plant that much history, so the rules have to be exercisable
against a list literal or they cannot be exercised at all.


READING THE RECORD, WHICH IS NOT A TIME SERIES
----------------------------------------------
core/record.py stores one row per *meaningful transition*, not one row per
sample, and a detector that reads it as samples finds nothing on exactly the
devices it exists for. Four of its rules shape everything below:

- a numeric row is written only once the value moves by 5% or 0.5 W, whichever
  is larger (observer.NUMERIC_MIN_CHANGE_FRACTION / NUMERIC_MIN_ABS_CHANGE).
  A flat standby draw crosses neither, and so writes nothing at all;
- observer.NUMERIC_MIN_INTERVAL_SECONDS is a 60-second floor between rows;
- observer.NUMERIC_MAX_GAP_SECONDS writes a row anyway after fifteen minutes of
  silence. That heartbeat is the only reason a plateau is visible: a flat
  overnight draw still leaves about four rows an hour, and their sameness IS
  the observation;
- prev_duration and the gaps between timestamps carry how long a state lasted.
  The plateau's length is never a count of readings.

THE TRAP that follows: absence of rows means two different things. Fifteen
minutes of silence on a watched, reporting entity means the value did not move.
Two hours of silence means the agent was not looking — the entity was
unavailable, it was not yet watched, the add-on was restarting, or record.prune()
collapsed those rows into the profile and deleted them. Read as flatness, that
second case produces a rock-solid plateau over a stretch where nothing was
observed, and does it most confidently at the old end of the record where the
evidence is thinnest. So any gap materially longer than the heartbeat is
treated as missing coverage and the night it lands in is disqualified — never
interpolated across.

AND STATISTICS ROWS ARE NOT EVIDENCE OF A FLOOR. A row with source='statistics'
came from Home Assistant's own five-minute or hourly aggregates, which
core/backfill.py falls back on for an entity too dense to read in full. Those
are bucket MEANS. A device that switched off for four minutes inside a
five-minute bucket produces the same mean as one that did not, and "never
dropped below" is this detector's entire claim. A night whose evidence rests on
such a row is counted, reported in the evidence as excluded, and never reaches
the floor test.


A CANDIDATE WITH NO CONTROLLABLE SWITCH IS NOT A CANDIDATE
----------------------------------------------------------
The power sensor is paired to a switch on the same device before anything is
proposed, and an unpaired sensor produces nothing. Standby draw nobody can act
on is a fact about the house, not a suggestion, and §9's rule is that a
suggestion is a thing the user answers in one word — there is no one-word
answer to "your television draws 12 W and there is nothing either of us can do
about it". Discarding it loses no information either: the draw is still in the
behavioural record, still visible in the panel's per-entity profile, and still
available to the conversational tools if someone asks. What is discarded is
only the claim that it is actionable.


AND NEITHER IS A DEVICE THAT ONLY HAS ONE LEVEL
-----------------------------------------------
The four numeric tests in `assess_night` filter shape, and what survives them
is a small, perfectly steady draw, all night, most nights — which describes
standby draw and equally describes a router, a NAS, an aquarium pump or a relay
behind a boiler quietly doing its job. `device_levels` is the test that
separates them, and its argument is that you cannot call 15 W "standby" without
having seen what "in use" looks like on that same device. It reads the whole
window rather than the nights, because the in-use level only exists in the
daytime series, and a device that never reached one is discarded.


WHAT THE NUMBERS MEAN
---------------------
`watts` is the median of the per-night minima — the level the device
demonstrably never went under, on a typical qualifying night. Median rather
than mean so one unusual night cannot move it.

`hours_per_day` is the observed night window and nothing more. The device may
well idle at the same level all afternoon, but the record cannot show that: the
daytime series is the device being used. Projecting the plateau across 24 hours
would be exactly the "weak number" §7.1 discards suggestions for, so the
monthly figure is deliberately a lower bound, and the evidence says which hours
it covers.

`kwh_per_month` is bounded the same way in the other dimension: it is projected
over the nights the plateau was actually seen on, never over every night in the
month. A device idling on ten nights of thirty is credited with ten, and
`nights_per_month` in the evidence says so. The nights that produced no reading
— the entity offline, the agent restarting, the record already collapsed — are
not evidence that the device was drawing anything, and a figure that assumed
they were would be a third of a suggestion's headline number invented.

One interaction worth knowing about, for whatever schedules this daily: the
evidence blob moves every day because the window moves, and
suggestions.upsert_candidate treats moved evidence as grounds to invalidate a
replay. That is the correct behaviour for evidence that really changed; whether
a one-day window slide should count as changed is a decision for the review
pass, not for the detector, so nothing here tries to hold the evidence still —
and nothing here writes to the suggestion store at all. core/review.py settles
it, and `scan` hands the candidate over rather than recording it.
"""
from datetime import datetime
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

# The suggestion kind, and the prefix of every key this detector derives. The
# key is the stable identity suggestions.py dedupes on, so it is built from the
# entity alone — never from a timestamp, a measured value or a rank, all of
# which move between runs and would put the same waste in front of the user
# every morning as something new.
KIND = "standby"

# ...and what this detector's own evidence blob calls the figures a reader has
# to find by NAME rather than by position: how many of the window's occurrences
# the claim was seen on, how many were observed at all, how many there were,
# how often the waste happens, how long it lasts, and the window it happens in.
#
# Declared here, beside the code that writes them, because they are a fact
# about this detector and there is nowhere else they could be stated without
# being stated twice. There are two readers. core/review.py's fingerprint takes
# `per_month` and `window` — the claim it compares across two mornings is the
# device, the switch, the level, how often, and the hours, and the last two are
# the only ones whose names differ between detectors; the other three
# (`entity`, `switch`, `watts`) are common to every shape and are read
# directly. core/artefacts.py takes the counts, because §9's rule is that no
# number is printed without the observation behind it and the observation is
# these.
#
# `window` names the field in THIS blob. A verification measurement written by
# core/verify.py carries the same label under `night_window` whichever shape
# took it — deliberately, so that two weeks can be compared — so a reader of
# one of those blobs uses that name and not this key.
#
# These names are on disk in activity.db on live installs and both readers go
# through here: changing one is a data migration, not a rename.
EVIDENCE_FIELDS = {
    "qualified": "nights",
    "observed": "nights_observed",
    "examined": "nights_examined",
    "per_month": "nights_per_month",
    "hours": "hours_per_day",
    "window": "night_window",
}


# --------------------------------------------------------------------------
# when a night is
# --------------------------------------------------------------------------
# 01:00 to 05:00, local, every day. Local because "night" is a statement about
# the household's clock, and Home Assistant sets the container's timezone to
# the home's — the same reason record.profile() buckets by local hour.
#
# Narrower than the night a person would name, on purpose. The claim being made
# is that nobody was using the device, and the two ends of the night are where
# that claim is weakest: television and dishwasher use runs past midnight, and
# early shifts start before six. Four hours in the deepest part of the night is
# the stretch a household is most reliably not in, and four hours is already
# long enough that a plateau across it is not a coincidence.
#
# Declared as a core/window.py spec rather than as a pair of integers, and that
# is the whole of this detector's stake in the seam: what "a night" is, is now
# a value this module owns and hands out. Everything downstream that has to
# agree about the hours — the config core/builder.py writes, the dates
# core/replay.py places, the week core/verify.py measures again — takes this
# spec rather than reaching for the numbers inside it, so a detector with a
# different window is a different spec rather than a different set of callers.
#
# Every day of the week, and the spec SAYS so rather than every rule below
# assuming it: a window naming its weekdays would be enumerated over those and
# no others without a line of this file changing.
NIGHT = window.spec("01:00", "05:00")

# The length of it, which is what the monthly figure is multiplied by. Named
# because that arithmetic reads as a sentence about nights rather than as a
# call, and derived rather than typed because the window above is the only
# place the hours are stated.
NIGHT_HOURS = window.hours(NIGHT)

# Silence longer than this is missing coverage, not a flat reading — the trap
# in this module's docstring. Twice the record's own heartbeat: one missed
# heartbeat is an add-on restart or a recorder pause and says nothing about the
# entity, while two consecutive misses mean the agent was not watching. There
# is no reading to be inferred across it, so the night is disqualified.
COVERAGE_MAX_GAP_SECONDS = 2 * observer.NUMERIC_MAX_GAP_SECONDS


# --------------------------------------------------------------------------
# what a plateau is
# --------------------------------------------------------------------------
# Below this the plateau is not distinguishable from the sensor reading zero.
# observer.NUMERIC_MIN_ABS_CHANGE is 0.5 W, so the record cannot resolve a
# movement smaller than that at all; twice it is the smallest level at which a
# drop to zero would itself have been recorded as a change. There is also
# nothing to save down here — a 1 W draw is about 0.7 kWh a year.
STANDBY_MIN_WATTS = 2 * observer.NUMERIC_MIN_ABS_CHANGE

# Above this, the device is doing work rather than idling, and switching it off
# is a different suggestion with a comfort or function trade-off behind it —
# §8's "idle heating" type, not this one. 30 W is comfortably above the standby
# draw of the appliances this is for (set-top boxes, game consoles, amplifiers,
# desktop chargers) and comfortably below a fridge compressor or a router
# carrying the house.
STANDBY_MAX_WATTS = 30.0

# How much a plateau may move and still be one. Both halves are needed: the
# fraction is what "flat" means at any scale, and the absolute allowance is the
# record's own resolution at the bottom of it — a genuine 1 W plateau is stored
# as 1.0 and 1.5 by NUMERIC_MIN_ABS_CHANGE alone, which is 50% and would fail a
# purely relative test on the recording rule rather than on the device.
PLATEAU_SPREAD_FRACTION = 0.30
PLATEAU_SPREAD_WATTS = 2 * observer.NUMERIC_MIN_ABS_CHANGE


# --------------------------------------------------------------------------
# the evidence floor (§7.1)
# --------------------------------------------------------------------------
# Below either of these there is no candidate — not a candidate with a caveat.
# §7.1 is explicit that a candidate under the threshold is discarded rather
# than shown with a weak number, and a record that does not reach back far
# enough is a normal answer on a young install, not an error.
#
# Fourteen days is two of every weekday: a plateau that is really the weekend
# cannot masquerade as a habit. Ten qualifying nights out of that is the
# smallest count at which a single anomalous night moves the median by less
# than the thresholds above.
MIN_COVERAGE_DAYS = 14
MIN_QUALIFYING_NIGHTS = 10

# ...and of the nights that produced usable evidence at all, this many must
# qualify. The count above says the plateau happened often; this says the
# device is not one the user ALREADY switches off — ten plateau nights against
# twenty on which it really dropped to nothing is a habit half in place, and
# the suggestion would be arguing with something the household already does.
# Four in five absorbs the odd night the device was genuinely used late.
#
# What this is NOT is the guard on the monthly figure. It once was, and it was
# the wrong place for it: the nights it leaves out of its denominator are the
# unreadable ones, so a device that was simply absent for two thirds of the
# window sailed through at a clean 1.0 and had its plateau projected across
# every day of the month anyway. The projection is bounded where it is
# computed instead — see kwh_per_month in find_candidate.
MIN_QUALIFYING_FRACTION = 0.8


# --------------------------------------------------------------------------
# does the device have a second state at all
# --------------------------------------------------------------------------
# The failure this section exists for: everything above filters SHAPE, and a
# small, perfectly flat, all-night draw is a television on standby and equally
# a router, a NAS, an aquarium pump or a relay behind a boiler. Nothing
# downstream catches the difference either — the automation's runtime guard
# does not fire on a 15 W router, and core/replay.py looks for evidence the
# device was USED after a cut, which for a router never appears because a
# router never leaves standby-shaped draw. The only thing standing between that
# and a home losing its network at 01:00 is the person reading the card.
#
# The test that separates them is nearly free: you cannot call 15 W "standby"
# without ever having seen what "in use" looks like on that same device. So the
# WHOLE window's readings are read — day as well as night, because the active
# level only exists in the daytime series — and a device never observed well
# above its own usual level is discarded before anybody is asked about it.
#
# The histogram itself — the bin scale, how far apart two modes must sit, and
# how empty the valley between them has to be — lives in core/detectors/levels
# .py, not here. That module has no notion of a night or a switch, and this
# detector has no notion of a bin; `device_levels` below is a caller of it.
# 2.13.0 shipped this test as a median with a multiplicative threshold above
# it, ACTIVE_GAP_MULTIPLE / ACTIVE_GAP_WATTS / ACTIVE_MIN_READINGS among the
# constants here — that stand-in is gone now that the real histogram exists;
# see levels.MODE_GAP_MULTIPLE and levels.MIN_MODE_READINGS for where its
# reasoning (unchanged) and its values (unchanged) now live.

# Fewer numeric readings than this and the window cannot answer the question at
# all. Not a number chosen here: it is the smallest series find_candidate's own
# floors can possibly have left, since ten qualifying nights of four hours each
# admit no gap longer than COVERAGE_MAX_GAP_SECONDS inside any one of them. So
# it never fires from that caller — it is a guard for a second caller later,
# not a rule in force today.
#
# It falls the SAME WAY as the check it guards, and that is the decision worth
# arguing. "Too little data to tell whether this is a router" is not a weaker
# version of "this is a standby draw"; it is the absence of the observation the
# claim rests on, and §7.1 discards a candidate under the threshold rather than
# showing it with a weak number.
MIN_LEVEL_READINGS = MIN_QUALIFYING_NIGHTS * (
    NIGHT_HOURS * 3600 // COVERAGE_MAX_GAP_SECONDS)


# --------------------------------------------------------------------------
# the window, and the projection
# --------------------------------------------------------------------------
# How far back one scan reads. Shorter than record.RETENTION_DAYS on purpose:
# the record may hold ninety days, but a standby load measured two months ago
# may be a device that has since been unplugged, and the review runs daily so
# nothing is lost by keeping the evidence recent. Thirty days is also twice the
# coverage floor, which leaves room for a fortnight of gaps before a real
# candidate stops being findable.
LOOKBACK_DAYS = 30

# Thirty days to a month, for the kWh figure. Not 30.44: the estimate is
# bounded rather than precise, and rounding the month down keeps it on the
# conservative side of the truth like every other number here.
#
# It is a month of NIGHTS, not a month of plateaus: what gets multiplied by it
# is the rate at which the plateau was actually seen, never the flat assumption
# that it happens every night. A device seen idling on ten of thirty nights is
# credited with ten nights in a month, not thirty.
PROJECTION_DAYS_PER_MONTH = 30


# --------------------------------------------------------------------------
# what counts as being able to act on it
# --------------------------------------------------------------------------
# Which domain can cut a supply, and what a power sensor's unit means, both
# moved to core/detectors/power.py when the schedule detector needed the same
# two answers — a shared question belongs BELOW the detectors rather than
# inside one of them, the way core/detectors/levels.py already does it. Their
# reasoning went with them unchanged.
#
# Re-exported rather than left behind, because `standby.CONTROLLABLE_DOMAINS`
# is what core/builder.py has always said and `standby.power_scale` is what
# core/verify.py has always called. Moving a shared answer is not a reason to
# move every caller, and these names are the same values they always were.
CONTROLLABLE_DOMAINS = power.CONTROLLABLE_DOMAINS
POWER_UNITS = power.POWER_UNITS

# Why a night did not qualify. Every one of these is counted and reported in
# the evidence, because §9's rule is that the observation travels with the
# number — and "ten of eleven nights" and "ten of forty" are not the same claim.
REASON_NO_COVERAGE = "no_coverage"      # a gap the agent cannot see across
REASON_STATISTICS = "statistics"        # bucket means; cannot show a floor
REASON_UNAVAILABLE = "unavailable"      # the entity was not reporting a number
REASON_BELOW_FLOOR = "below_floor"      # it really did drop — no standing draw
REASON_IN_USE = "in_use"                # above the standby ceiling
REASON_NOT_FLAT = "not_flat"            # moved too much to be a plateau

# The reasons that mean "the night was observed, and it was not standby", as
# opposed to "the night could not be read". Only these count towards the
# denominator MIN_QUALIFYING_FRACTION is measured against — an add-on restart
# is not evidence that a device stopped idling.
_OBSERVED_REASONS = (REASON_BELOW_FLOOR, REASON_IN_USE, REASON_NOT_FLAT)


# --------------------------------------------------------------------------
# pure rules
# --------------------------------------------------------------------------
# The one shared implementation lives in core/observer.py — every module that
# wanted its own copy (this one, replay.py, and now detectors/levels.py)
# already imports observer, so a fourth copy is exactly the drift CLAUDE.md
# argues against. Aliased rather than called by its new name so `standby._median`
# stays a stable name for the one caller below and for the tests that reach it.
_median = observer.median


def _local_date(ts):
    """The local calendar date an instant falls on."""
    return datetime.fromtimestamp(int(ts)).date()


def night_bounds(day):
    """(start, end) unix seconds of the night beginning on one local date, or
    None when the clock skipped over either end.

    The name is kept because every rule in this module is written about a night
    and reads as one; the arithmetic is core/window.py's, which is also where
    the DST answer now lives. It is the STRICT answer: this used to resolve
    01:00 on a spring-forward date to whatever mktime normalised it to and then
    measure a three-hour night as a four-hour one, and the seam refuses the
    occurrence instead. That costs one night a year out of a window that needs
    ten of thirty, and it fails towards measuring nothing rather than towards
    reporting a span that did not happen.
    """
    return window.bounds(NIGHT, day)


def night_window_label() -> str:
    """The night window as §9 prints it — window.label of this detector's spec.

    Written into every evidence blob and every baseline, and compared against
    by core/verify.py to decide whether two measurements taken a week apart
    were taken over the same hours. That is why there is exactly one renderer
    of it and why it lives with the spec rather than here: two implementations
    of one label would make the comparison fail on the punctuation.
    """
    return window.label(NIGHT)


def night_windows(start, end) -> list:
    """Every night that falls WHOLE inside [start, end), oldest first.

    The "whole" rule and its argument moved to window.windows along with the
    code that enforces it: a partly-covered night is a shorter observation
    dressed up as a full one, and the plateau claim is about four hours rather
    than about however many of them the range happened to contain.
    """
    return window.windows(NIGHT, start, end)


def assess_night(rows, night_start, night_end, scale: float = 1.0) -> dict:
    """Judges one night. Returns {"qualifies", "reason", "watts", "readings"}.

    `rows` is the entity's transitions in ascending ts order — the whole series
    is fine, only the relevant slice is read. The row immediately BEFORE the
    window matters as much as the ones inside it: the record writes on change,
    so the value in force at 01:00 is the one carried in from the last row
    before it, and a night judged only on rows inside the window would be
    judged on whatever happened to move.

    The checks are ordered so that the reason reported is the strongest thing
    true about the night: what could not be read first, what was read second.
    """
    carried = None
    inside = []
    for row in rows:
        ts = row.get("ts")
        if ts is None:
            continue
        ts = int(ts)
        if ts < night_start:
            carried = row
        elif ts < night_end:
            inside.append(row)
        else:
            break

    # Nothing at all before the window: no row to read the silence against.
    if carried is None:
        return _night(False, REASON_NO_COVERAGE)

    sequence = [carried] + inside
    if night_start - int(carried["ts"]) > COVERAGE_MAX_GAP_SECONDS:
        return _night(False, _gap_reason(carried))
    previous = carried
    for row in inside:
        if int(row["ts"]) - int(previous["ts"]) > COVERAGE_MAX_GAP_SECONDS:
            return _night(False, _gap_reason(previous))
        previous = row
    # ...and the tail: silence from the last row to 05:00 is the same gap.
    if night_end - int(previous["ts"]) > COVERAGE_MAX_GAP_SECONDS:
        return _night(False, _gap_reason(previous))

    if any(str(row.get("source") or "") == "statistics" for row in sequence):
        return _night(False, REASON_STATISTICS)

    values = []
    for row in sequence:
        value = observer.as_float(row.get("state"))
        if value is None:
            # 'unavailable' and 'unknown' are recorded deliberately and are not
            # zero watts — the device may have been drawing anything at all.
            return _night(False, REASON_UNAVAILABLE)
        values.append(value * scale)

    low, high = min(values), max(values)
    if low < STANDBY_MIN_WATTS:
        return _night(False, REASON_BELOW_FLOOR, watts=low)
    if high > STANDBY_MAX_WATTS:
        return _night(False, REASON_IN_USE, watts=low)
    if high > low * (1 + PLATEAU_SPREAD_FRACTION) + PLATEAU_SPREAD_WATTS:
        return _night(False, REASON_NOT_FLAT, watts=low)

    # "Never dropped below" is the minimum, and nothing else is: the mean would
    # claim a level the device was sometimes under.
    return _night(True, None, watts=low, readings=len(values))


def _night(qualifies, reason, watts=None, readings=0):
    return {"qualifies": bool(qualifies), "reason": reason,
            "watts": watts, "readings": readings}


def _gap_reason(row) -> str:
    """Which of the two silences this is, judged by the row it starts from.

    Both look identical in the record — no rows — and they are different facts
    about the home. Silence after a NUMBER is the agent not watching: a
    restart, an entity not yet on the watchlist, a stretch record.prune() has
    already collapsed. Silence after 'unavailable' is the ENTITY being gone,
    and the record says so exactly once: observer.should_record's string branch
    suppresses the repeats, so an entity offline for six hours leaves one row
    and then nothing.

    Reporting both as "no coverage" would tell a reader the agent was not
    looking when it was looking at a device that had disappeared — and §9 makes
    the evidence blob the thing a human is shown, so the distinction has to
    survive into it. Neither one qualifies the night either way.
    """
    return (REASON_NO_COVERAGE if observer.as_float(row.get("state")) is not None
            else REASON_UNAVAILABLE)


def device_levels(rows, scale: float = 1.0):
    """The device's own two levels — {"idle_watts", "active_floor"} — or None
    when it only ever had one. A caller of core/detectors/levels.py: this
    function's whole job is reading the record correctly, not histogramming —
    that is levels.two_levels's job, done once for every detector that ever
    asks the question rather than a second time here.

    `rows` is the WHOLE window's transitions, day and night both, and not the
    night slices every other rule in this module reads. That is the entire
    point: the active cluster exists only in the daytime series, so a check
    that looked at the hours the claim is about could never see the thing that
    makes the claim true.

    Weighted by ROW COUNT rather than by how long each reading was in force,
    same as 2.13.0. Rows are transitions, so the two are genuinely different
    statistics, and duration is the more faithful one — but it is faithful in
    the wrong direction here: a device being used writes at the record's
    NUMERIC_MIN_INTERVAL_SECONDS floor because its value keeps moving, while an
    idle one writes once per NUMERIC_MAX_GAP_SECONDS heartbeat, so counting
    rows over-represents the active cluster exactly where the device is most
    active. Under the median this was a safe error rather than a neutral one —
    it made the check easier for a real television to pass without helping a
    router pass at all, since a router has no readings above its own level to
    over-represent. Under the histogram the argument is stronger still: the
    idle level is now the LOWEST mode rather than the middle reading of the
    whole series, so over-representing the active cluster does not touch the
    idle level's value at all, only how confidently the active cluster clears
    MIN_MODE_READINGS — and it can only help it clear that floor, never hurt.

    `active_floor` is a threshold, not a measurement. It is where THIS device
    stops being idle, and nothing downstream uses it yet — core/replay.py's
    in_use verdict and core/builder.py's runtime guard still turn on the global
    STANDBY_MAX_WATTS, and moving them onto a per-device number would change
    automations users have already approved.
    """
    # The reading itself is core/detectors/power.py's — 'unavailable' is not
    # zero watts, and a statistics row IS counted here because a level is not a
    # floor claim. Both rules, and both reasons, moved there with the other
    # shared answers when the schedule detector needed to read a series the
    # same way; what stayed here is the floor below, which is the half that is
    # this detector's own.
    values = power.readings(rows, scale)

    # `not values` as well as the floor: MIN_LEVEL_READINGS is derived from
    # three other constants, and retuning any of them could take it to zero —
    # at which point a histogram over an empty list would have nothing to
    # answer with, where every other refusal here returns None.
    if not values or len(values) < MIN_LEVEL_READINGS:
        return None

    return levels.two_levels(values)


# The switch pairing, in core/detectors/power.py with its reasoning intact —
# "one switch, or nothing" is a statement about a registry rather than about a
# night, and the schedule detector needs exactly the same answer. Re-exported
# so `standby.controllable_switch` keeps naming it.
controllable_switch = power.controllable_switch


def measure_nights(rows, nights, scale: float = 1.0, spec=NIGHT) -> dict:
    """The plateau arithmetic over a set of night windows: what was seen on
    them, and what that is worth over a month.

    `spec` is the window `nights` was enumerated from, and it is here for one
    figure: `hours_per_day` is the window's own length, and reading it off this
    module's constant would have every measurement report four hours whatever
    it was actually taken over. It defaults to the night so that every caller
    measuring a night says nothing; a caller measuring something else has to
    say which window, because a measurement whose hours are guessed is the one
    error §7.2 cannot recover from.

    The keys it answers with are named for NIGHTS, deliberately and for now.
    `nights`, `nights_observed`, `nights_examined`, `nights_per_month`,
    `hours_per_day` and `excluded` are on disk in activity.db on live installs,
    are read by core/artefacts.py, and `night_window` is one of the fields
    review.evidence_fingerprint is built from — reached through
    EVIDENCE_FIELDS above rather than by that literal name, so grepping the
    review pass for it finds nothing and proves nothing. A function that takes
    a window
    and answers in nights is mildly ugly and entirely honest; generalising the
    vocabulary is a migration of stored blobs, and it belongs with the work
    package that introduces the second window rather than with the seam.

    Separated from `find_candidate` because the same measurement is taken twice
    for two different purposes, and the two have to be the same statistic or
    they cannot be compared. Here it is half of judging a candidate. At apply
    time the review pass takes it again over the week before the automation was
    written, as the "before" §7.2's verification is measured against — and a
    baseline computed by a second implementation of "the median of the
    per-night minima" would be a number free to drift from this one without
    anything failing.

    The FLOORS are deliberately not here, and that is the split: what makes a
    measurement a CANDIDATE is find_candidate's business. A baseline is a
    measurement and is recorded whatever it comes to — including a week on
    which nothing could be read, which comes back with every measured figure
    EMPTY rather than zeroed. That is a fact about the week, not an error, and
    not a device that drew nothing.
    """
    watts_by_night = []
    excluded = {}
    observed = 0
    hours_per_day = window.hours(spec)
    for _day, night_start, night_end in nights:
        verdict = assess_night(rows, night_start, night_end, scale=scale)
        if verdict["qualifies"]:
            watts_by_night.append(verdict["watts"])
            observed += 1
            continue
        reason = verdict["reason"]
        excluded[reason] = excluded.get(reason, 0) + 1
        if reason in _OBSERVED_REASONS:
            observed += 1

    qualifying = len(watts_by_night)
    if not qualifying:
        # Nothing was measured, and the derived figures are left EMPTY rather
        # than zeroed. A baseline reporting `kwh_per_month: 0.0` for a week
        # nobody could read is indistinguishable from one reporting a device
        # that genuinely drew nothing, and §7.2's verification would then
        # subtract a measurement that never happened — reporting the whole of
        # the after as a loss. `find_candidate` never reaches this branch; its
        # floors guarantee ten nights.
        return {
            "watts": None,
            "watts_range": None,
            "hours_per_day": hours_per_day,
            "kwh_per_month": None,
            "nights_per_month": None,
            "nights": 0,
            "nights_observed": observed,
            "nights_examined": len(nights),
            "excluded": excluded,
        }

    # The saving is claimed for the nights the plateau was actually SEEN on,
    # and for no others. Multiplying by a whole month regardless is how a
    # device present on ten nights of thirty — used at a holiday flat, or
    # simply offline for a fortnight — got credited with the same figure as one
    # that idled every single night. Neither an unreadable night nor an absent
    # one is evidence that the device was drawing anything, and §7.2's whole
    # instruction is to constrain the claim rather than inflate it.
    nights_per_month = qualifying / len(nights) * PROJECTION_DAYS_PER_MONTH
    # "Never dropped below" is the minimum on each night, and the median across
    # them, so one unusual night cannot move the level being claimed.
    watts = round(_median(watts_by_night), 1)
    return {
        "watts": watts,
        "watts_range": [round(min(watts_by_night), 1), round(max(watts_by_night), 1)],
        "hours_per_day": hours_per_day,
        "kwh_per_month": round(watts * hours_per_day * nights_per_month / 1000.0, 2),
        # What the figure above was projected over, stated rather than left to
        # be re-derived: §9 wants the sentence to be writable from the evidence
        # without arithmetic.
        "nights_per_month": round(nights_per_month, 1),
        "nights": qualifying,
        "nights_observed": observed,
        "nights_examined": len(nights),
        "excluded": excluded,
    }


def find_candidate(entity_id: str, rows, switch_entity_id=None, now=None,
                   lookback_days: int = LOOKBACK_DAYS, scale: float = 1.0):
    """The whole judgement, pure: transitions in, candidate or None out.

    Returns {"key", "kind", "entities", "evidence"} ready for
    suggestions.upsert_candidate, or None — and None is the ordinary answer.
    Most entities are not standby loads, most young records do not reach back
    far enough, and neither is an error.

    `rows` are the entity's transitions, ascending. They are re-sorted AND
    clipped to the window this call is judging, because the window is derived
    here from `now` and `lookback_days` rather than taken from the caller: a
    caller handing over more history than it asked to be judged — ninety days
    read once, thirty days evaluated — would otherwise have the coverage floor
    below satisfied by rows no night in the window ever looked at, and the
    stored evidence would report a coverage figure that contradicts the window
    printed beside it.
    """
    if not entity_id:
        return None
    if not switch_entity_id:
        # Enforced in the rules rather than in the caller, so the discard is
        # testable without a registry — and so it cannot be forgotten by a
        # second caller later.
        logger.debug(f"STANDBY: {entity_id} has no controllable switch — not a candidate")
        return None

    end = int(now) if now else int(time.time())
    start = end - int(lookback_days) * 86400
    rows = sorted(
        (r for r in (rows or [])
         if r.get("ts") is not None and start <= int(r["ts"]) < end),
        key=lambda r: int(r["ts"]))

    nights = night_windows(start, end)
    if not nights:
        return None

    covered_days = len({_local_date(row["ts"]) for row in rows})
    measured = measure_nights(rows, nights, scale=scale)

    if covered_days < MIN_COVERAGE_DAYS or measured["nights"] < MIN_QUALIFYING_NIGHTS:
        return None
    observed = measured["nights_observed"]
    if not observed or measured["nights"] / observed < MIN_QUALIFYING_FRACTION:
        return None

    # `device`, not `levels`: the module of that name is imported at the top of
    # this file and `device_levels` reaches into it, so a local binding here
    # would shadow it for the rest of the function. core/detectors/schedule.py
    # names the same value the same way.
    device = device_levels(rows, scale=scale)
    if device is None or measured["watts"] >= device["active_floor"]:
        # A device never seen in a high state has no idle state either — and
        # neither has one whose only high state IS the plateau being proposed
        # for cutting. The second half is not a corner case: a device switched
        # off, or deep-sleeping, through most of the day has that lower level
        # read as its idle one, and then the very standby draw this suggestion
        # is about comes back as the evidence that the device is ever in use.
        # Both are the same failure, and the invariant that catches it is the
        # one §9's sentence asserts out loud — the level it never went under at
        # night is BELOW the level this device reaches when somebody uses it.
        #
        # Placed after the floors rather than beside the switch-pairing discard
        # above for one reason: reaching it means everything else about this
        # device said "ask the user", so the line is only written where it
        # changed an outcome — and `watts` is only a number to compare against
        # once MIN_QUALIFYING_NIGHTS has guaranteed one.
        logger.debug(f"STANDBY: {entity_id} has no in-use level above its night "
                     "plateau — one level is a device doing its job, not a "
                     "standby draw")
        return None

    return {
        "key": f"{KIND}:{entity_id}",
        "kind": KIND,
        "entities": [entity_id, switch_entity_id],
        # Written to be read by a person, because §9 renders it as a sentence
        # and treats that rendering as the design constraint rather than the
        # fallback: the nights, the watts, the window, and what was left out
        # and why. Not a dump of rows — the rows are still in the record.
        "evidence": {
            "entity": entity_id,
            "switch": switch_entity_id,
            # The measurement travels whole rather than being restated field by
            # field here: it is one calculation, and its figures only multiply
            # out together.
            **measured,
            # The device's own two levels, travelling whole for the same
            # reason: they are one reading of one series. Recorded as evidence
            # of how the claim was reached rather than as part of the claim,
            # which is why review.evidence_fingerprint does not list them — the
            # watts figure already moves when the device does.
            **device,
            "covered_days": covered_days,
            "night_window": night_window_label(),
            "window": {
                "from": _local_date(start).isoformat(),
                "to": _local_date(end).isoformat(),
                "days": int(lookback_days),
            },
            # The rules that produced the numbers above travel with them, so a
            # reader disagreeing with the conclusion can see which threshold to
            # argue with rather than having to find this file.
            "thresholds": {
                "floor_w": STANDBY_MIN_WATTS,
                "ceiling_w": STANDBY_MAX_WATTS,
                "min_nights": MIN_QUALIFYING_NIGHTS,
                "min_days": MIN_COVERAGE_DAYS,
                "max_gap_s": COVERAGE_MAX_GAP_SECONDS,
            },
        },
    }


# --------------------------------------------------------------------------
# the thin layer
# --------------------------------------------------------------------------
# Recognising a power sensor, likewise — a unit and a device class, read the
# same way by every detector that measures watts. core/verify.py calls this
# through this module's name and keeps doing so.
power_scale = power.power_scale


def scan(now=None, lookback_days: int = LOOKBACK_DAYS, snapshot=None) -> list:
    """Reads the record for every watched power sensor and returns what it
    found, most valuable first — each candidate carrying the series it was
    judged on.

    The I/O half, and all of it: one registry round trip, the switch pairing,
    and one bounded record read per sensor. It exposes a function and schedules
    nothing — what calls it daily is the reasoning schedule's business, and
    what happens to a candidate afterwards is the approval gate's. This module
    never proposes a change to a home.

    `snapshot` is a registry the caller has already read. core/verify.py needs
    the same three answers this does — the states, the entity rows, the device
    grouping — and it runs in the same daily pass, so the pass reads them once
    and hands them to both. Left unset, this reads its own, which is what a
    caller with nothing in hand does.

    What it also does not do, any more, is RECORD what it found. The upsert
    into core/suggestions.py belongs to the review pass, because the decision
    made at that call site is whether today's evidence differs enough from
    yesterday's to throw away a replay — and this module does not know what a
    replay is. Making that judgement here would mean making it blind, or
    teaching a detector the suggestion lifecycle.

    `rows` travels with the candidate for the reason core/replay.py's docstring
    gives: the grading has to run over the same series the measurement came
    from, and a caller that read the record a second time would grade the
    config against a window that had moved underneath it. `scale` and `name`
    travel for the same reason in miniature — both are answers this one
    registry round trip already has, and re-deriving them downstream would cost
    a second one.

    Only a candidate's rows are held. An entity that produced no candidate has
    its series dropped where it was read, which is what keeps thirty days times
    every watched sensor from being in memory at once.
    """
    watched = set(watchlist.watched_entities())
    if not watched:
        return []

    if snapshot is None:
        ok, data = registry.fetch_registry()
        if not ok:
            logger.warning(f"STANDBY: could not read the registry — skipping this scan ({data})")
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
        scale = power_scale(states.get(entity_id), registry_row)
        if scale is None:
            continue
        switch_entity_id = controllable_switch(
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
        # switched off, and core/builder.py's label is what a household reads
        # in the automation's alias months later.
        candidate["name"] = names.get(switch_entity_id, "")
        found.append(candidate)

    found.sort(key=lambda c: c["evidence"]["kwh_per_month"], reverse=True)
    if found:
        logger.info(f"STANDBY: {len(found)} standby load(s) found across "
                    f"{len(watched)} watched entity(ies)")
    return found
