"""Design §7.2 — the honest part: a week later, measure the same thing again.

Stage 7 of the lifecycle, and the one the product is judged on. Everything
before it is the agent making a case; this is the agent going back to the same
sensor a week later and saying what actually happened, with the rollback on the
table whatever the number says.

    at apply time      measure_week   the "before", stored on the suggestion
    a week later       measure_week   the "after", over the same length of week
                       compare        the judgement, and the one claim it makes
                       artefacts      §9 artefact 4, rendered once for both
                                      surfaces
    whenever           answer         keep, or roll back — the user's word

Split the way core/observer.py is split from core/observation.py and for WP4's
concrete reason rather than a stylistic one: everything this module DECIDES is
a pure function over a suggestion row and a list of transitions a test can
state as a literal, and `run_due` is the only thing here that touches a store,
a clock or Home Assistant. `core/review.py` calls it from the daily pass that
already exists — there is no second schedule and no second registry round trip.

The same statistic, never a second implementation. Both weeks are measured by
the detector that raised the suggestion — `standby.measure_nights` for a night,
`schedule.measure` for a window that names its weekdays — at the same scale,
over the same number of occurrences, at the same hours, through the one call
`measure` below. The hours, the weekdays and the level they are judged against
are a SHAPE handed in rather than constants read here, so that "the same" is
something a caller states rather than something two calls happen to agree on. A
baseline computed by a second reading of "the median of the per-occurrence
minima" would be free to drift from the one the suggestion was raised on, and
the difference between two such numbers would be a difference in the arithmetic
rather than in the home.

No model, anywhere — §7.1's line, and it applies with more force here than
upstream. A saving a model asserted is a saving nobody can reproduce, and this
is the number the user is being invited to check.


WHAT THIS CANNOT DO, WHICH IS THE WHOLE DESIGN OF IT
----------------------------------------------------
§7.2: "There is no counterfactual. Weather, season, occupancy and behaviour all
move underneath the measurement, and a naive before/after difference will
sometimes produce a number that is simply wrong. Reporting a confident figure
the user can disprove would damage trust faster than reporting nothing."

So the claim is CONSTRAINED rather than inflated, in four ways that are each a
piece of code below:

- the measure is one the change directly controls — the device's own overnight
  draw, never a whole-home total;
- the periods are alike and both are stated: a week of occurrences against the
  same week of occurrences, at the same hours, on the same weekdays, and the
  report prints both windows — "alike" being a check in `_like_periods` rather
  than an assumption;
- the answer is a RANGE, or the sentence "too small to separate from noise".
  There is no third form, and in particular there is no single confident
  number: `NOISE_FLOOR_WATTS` below is what draws that line, and it is this
  work package's own decision;
- the rollback is offered whatever the number says. A verification that only
  offered an undo when the news was bad would be an agent grading its own
  homework.

And one more, which §7.1 spent a module preventing: the report names WHOSE
estimate it is comparing against. core/replay.py may only tighten a claim, so
when its own recomputed saving came out larger it reported the DETECTOR's
figures and said so in `estimate.basis`. A verification that printed "the
replay said 1.4 kWh, the measurement says 1.1" over a detector-sourced estimate
would be attributing a number to the side that did not produce it. `basis`
travels into the verdict for exactly that reason, and core/artefacts.py reads
it in the one place it reads it anywhere.


THE FOUR DECISIONS THIS WORK PACKAGE OWNS
-----------------------------------------
1. A WEEK THAT COULD NOT BE READ.
   `standby.measure_nights` answers a week it could not read with every figure
   EMPTY rather than zeroed, deliberately, and its docstring says why: "0.0
   kWh" for a week nobody observed is indistinguishable from a device that
   genuinely drew nothing, and subtracting it would report the whole of the
   after as a saving. So an empty AFTER is never "saved everything", and an
   empty BEFORE is never "saved nothing".

   What happens next differs between the two, because one of them can change
   and the other cannot:

   - the AFTER week may be readable next week — the sensor was offline, the
     add-on was restarting, the household went away. So it is measured AGAIN,
     `RETRY_AFTER_DAYS` later, up to `MAX_ATTEMPTS` times, and then reported as
     unmeasured with the rollback still offered. Waiting forever would leave a
     suggestion at `applied` that §12 can never count.
   - the BEFORE week is a photograph taken at apply time and the automation has
     been running over the record ever since. It will never become readable, so
     there is nothing to wait for: the report goes out at once saying the
     comparison cannot be made, and offers the rollback.

   The distinction that makes any of this possible is the OBSERVED count —
   `nights_observed`, or `occurrences_observed` for the other shape (decision
   9): an occurrence excluded as `below_floor` was READ and found to have
   dropped, which is what a working automation looks like, while one excluded
   as `no_coverage` was not read at all.

2. THE RULE MAY NOT HAVE BEEN RUNNING.
   A week measured while the automation was disabled, edited into something
   else or deleted is not a verification of anything — and reporting a saving
   from it would be the agent taking credit for the weather. So the automation
   is checked before any number is claimed, in the register core/rollback.py's
   `_check` and core/gate.py's `_drift_refusal` established: a plain-text
   refusal phrased for the person reading it, returned as a value, never
   raised, and never improvised around.

   The check is `replay.plan_for` over what Home Assistant holds NOW against
   the config the suggestion stored — the same parser that graded the rule, so
   the five things compared are exactly the five the grading depended on
   (when it cuts, when it restores, on which weekdays, what it guards on, and
   which switch). A dict comparison would report an edit every time, since Home
   Assistant adds `id` and fills in defaults on the way through.

   Deleted and already-rolled-back are different from edited and disabled, and
   are treated differently: there is nothing left to keep or roll back, so the
   suggestion is retired with a message saying so (§11: "user told which and
   why") rather than parked at `verified` waiting for an answer that has no
   meaning.

3. KEEP OR ROLL BACK IS THE USER'S ANSWER, NOT A STATE THE CODE SETS.
   The report is delivered at `verified`. `kept` and `rolled_back` are reached
   only through `answer` below, and only from a person — in the panel by
   clicking, in chat by the WP8 pattern: a QUESTION recognised in the tool
   RESULT, parked in its own session slot, read by
   `agent_runtime.parse_approval_decision`, which matches the whole normalised
   message and never uses the LLM.

   The undo is `rollback.roll_back` and nothing else. There is no second
   rollback path here and no re-gating: core/rollback.py decision 1 already
   settled that the OPERATION is pre-authorised by the approval that let the
   original change through, and this is the same act from the other end — the
   user asking for the state they had before they said yes. What was ever in
   question is only WHICH change, and here nobody is choosing: the suggestion
   row names the change it was applied through.

   A failed rollback is relayed verbatim and the suggestion stays `verified`.
   §11 wants that case explicit and actionable, `roll_back`'s `message` is
   written to be the whole answer, and moving the row to `rolled_back` over a
   rollback that did not happen would be the audit trail lying about a home.

4. URGENCY — none. A weekly report is the definition of something that can
   wait, and §11 reserves the Home Assistant push for what cannot: the escape
   hatch failing, a write nobody can tell landed. Spending the one
   agent-to-user path there is on a weekly digest is how a household learns to
   ignore it. So the message is queued `normal` and core/delivery.py decides
   where it goes, as it does for every other producer — this module names no
   channel.


THE SECOND WINDOW, AND THE SEVEN PLACES IT LANDS
-------------------------------------------------
A window is no longer "the night". core/detectors/schedule.py raises a
candidate over 07:00-17:00 on the weekdays it names, and every one of the four
decisions above had the night folded into it somewhere. The four are unchanged
in MEANING — a week that could not be read is still measured again and never
reported as "saved nothing", the rule is still checked before a number is
claimed, keep and roll back are still the user's word, and none of it pushes.
What follows is the seven places the night was an assumption rather than a
parameter.

Nothing produces a schedule candidate yet — core/review.py still calls
`standby.scan` and only that — so no home reaches any of this. The night's own
behaviour is unchanged down to the blob it writes, which is the check this work
package is measured by.

5. HOW LONG A WEEK IS, WHICH IS THE ONE PROMISE HERE THAT MOVES.
   §7.2 promises a report a week after applying, and `AFTER_DAYS = 7` gives a
   Mon-Fri rule about five occurrences to measure on — which the proposal calls
   not defensible — and a Sat-Sun rule two. So what is held constant is
   OCCURRENCES rather than days, and `days_for` turns that into whole weeks
   from the suggestion's own stored window.

   It is not a global bump and must not become one. `review.settle` writes
   `verify_after` and captures the baseline at apply time, from this same
   function over this same row, so every suggestion already on disk keeps the
   promise it was made: a seven-day baseline against a seven-day after, a week
   later. The argument is stated again where the constant is.

6. "COMPARE LIKE PERIODS" LOSES AN ASSUMPTION AND GAINS A CHECK.
   `_like_periods` could say "both windows are AFTER_DAYS long by
   construction". With a length derived per suggestion that sentence is no
   longer true, and two weeks of different lengths compared as though they were
   alike is exactly the failure it exists to catch — so it now checks the
   length as well as the hours. It still compares LABELS rather than specs,
   because the label is what the baseline stored and what §9 printed, and
   `window.label` renders the weekdays of a window that names them for free.

7. THE LEVEL A SCHEDULE WEEK IS MEASURED AGAINST COMES FROM WHAT WAS STORED.
   `schedule.intervals` needs this device's own active floor, and re-deriving
   it by histogramming the week being measured would histogram a device the
   automation now switches OFF for most of that window: a different threshold,
   so a different measurement, and the comparison would be between two things
   rather than two measurements of one thing. `_idle_floor` takes it from the
   suggestion, in the order `builder.readback` takes its three sources — by how
   well each knows THIS config — and a week whose level cannot be recovered is
   a blob that says so, never a guess.

8. THE WINDOW IS THE SUGGESTION'S OWN, AND IS VALIDATED THE WAY IT WAS BUILT.
   `standby.NIGHT` is a declaration about the house and may be read off the
   detector. A schedule's window is a FINDING about one device, so it is
   rebuilt from `evidence["schedule"]` through `builder._spec_from` — the same
   function that wrote the automation, so the hours measured and the hours the
   rule runs over cannot come apart. It refuses a truncated blob rather than
   defaulting the weekdays, because defaulting them to all seven would measure
   the two days the evidence never looked at.

9. THE FIELD NAMES, AND THE FIVE FUNCTIONS THAT READ THEM.
   A schedule measurement says `occurrences_observed`, `occurrences_examined`,
   `occurrences`, `occurrences_per_month` and `hours_per_occurrence`; a standby
   one does not move by a character, because those names are on disk in
   activity.db on live installs. One table, `_KINDS`, the way core/replay.py
   holds its own — and the measurement records its own `kind` so the readers
   can tell which vocabulary the counts under it are written in.

   The trap `readable` documents is unchanged and is the reason the table
   exists at all: the floor is on OBSERVED, never on qualified, because a
   working automation produces a week in which nothing qualifies.

10. NOISE_FLOOR_WATTS KEEPS ITS VALUE, AND BOTH OF ITS ARGUMENTS HOLD FOR BOTH
   DETECTORS. The record's own resolution is a fact about the record. The
   residual a successful week can hide is bounded by `standby.STANDBY_MIN_WATTS`
   for a night and by `schedule.IDLE_MIN_WATTS` for a window, and those are the
   same `2 x observer.NUMERIC_MIN_ABS_CHANGE` by construction rather than by
   coincidence. So the number is not recomputed per kind. What IS per
   measurement is its size on the kWh axis: `floor_kwh` converts with the
   measurement's own hours and occurrences, so a window seen on 21 weekday
   occurrences has a different-sized floor from one seen on 30 nights.

11. THE WEEKDAYS ARE PART OF THE RULE, SO `_same_rule` COMPARES THEM.
   A rule edited from Mon-Fri to every day is not the rule that was graded, and
   until this it verified as though it were. `replay.plan_for` carries the days;
   they are compared there, through the same parser, for the reason that
   docstring gives about comparing dicts.


A ROW WITH NOTHING TO MEASURE
-----------------------------
12. NOTHING TO MEASURE IS AN OUTCOME, NOT A FAILURE (proposal §5, B-5).
   A suggestion the analyst raised has no detector, so it has no shape, no
   sensor and no statistic this module could take a week of — and until
   Phase J gives it one, every such row is in that position. `measurable` is
   the one place that is asked. Such a row is reported at its FIRST due date
   with `OUTCOME_NOT_MEASURED`: no retry, because nothing about next week
   would make it measurable, and never `unmeasured`, because that outcome
   means a week that COULD have been read and was not, and it counts toward
   `MAX_ATTEMPTS`. The report still offers keep or roll back — §7.2's fourth
   rule has no exception for a change nobody could measure.

   Decision 2 applies unchanged: deleted or already rolled back is withdrawn,
   disabled or edited is `not_running`. What differs is how "edited" is read.
   `_same_rule` goes through `replay.plan_for`, which refuses every config the
   builder did not write — so for an analyst row it would call every week an
   edit. `_same_config` compares what Home Assistant runs instead, with the
   defaults `_write_automation` fills taken out, which is the dict comparison
   decision 2 argues against for a detector row and the only honest one for a
   config nothing here can parse.
"""
from collections import namedtuple
from datetime import date
import logging
import math
import time

from core import activity
from core import artefacts
from core import builder
from core import observer
from core import record
from core import replay
from core import rollback
from core import suggestions
from core import window
from core.detectors import schedule
from core.detectors import standby
from tools.tool_homeassistant import registry
from tools.tool_homeassistant.automations import read_automation

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# the window: a week against a week (decision 5)
# --------------------------------------------------------------------------
# §7.2: "a week after applying, the agent compares the same measure before and
# after". Both halves of that comparison are this length, and the baseline that
# core/review.py captures at apply time takes its length from here rather than
# stating its own — a thirty-day before against a seven-day after is a
# difference in the measurement rather than in the home.
#
# Seven nights is below the detector's evidence floor of ten, and that is
# correct: a floor is what makes a measurement a CANDIDATE, and neither of
# these is a candidate. One is the "before" of a change the user has already
# approved and the other is what became of it.
#
# What the seven actually promises is seven OCCURRENCES of the window being
# measured, which for a rule that happens every day is seven days. `days_for`
# below is that same promise made to a rule that happens on the weekdays it
# names, and it is the one figure in this phase that moves. It is deliberately
# not a global bump: `review.settle` writes `verify_after` and captures the
# baseline at apply time from `days_for` over the same row, so a suggestion
# already applied is a seven-day baseline against a seven-day after and stays
# one. Nothing on disk moves, and no promise already made to a user changes.
AFTER_DAYS = 7
BASELINE_DAYS = AFTER_DAYS


def _majority(occurrences: int) -> int:
    """More than half of them, rounded up.

    One arithmetic, two applications: the floor a week has to clear to be half
    of a comparison, stated once here so that the every-day answer below and
    the per-week one in `min_read` cannot come apart.
    """
    return (int(occurrences) // 2) + 1


# How many of a week's nights must have been READ for that week to be half of a
# comparison. A majority of the week, so that one unusual night cannot be most
# of either side, and derived from the week rather than typed as a number so it
# stays a majority if the window above ever moves — which `min_read` is now
# what carries, per measurement, for a week whose occurrences are not days.
#
# READ, not qualified — and the difference is the whole point. A working
# automation produces a week in which NO night qualifies as a plateau, because
# the device was switched off; those nights are excluded as `below_floor`,
# which `standby._OBSERVED_REASONS` counts as observed. Putting this floor on
# `nights` instead of `nights_observed` would declare every successful
# verification unmeasurable.
MIN_NIGHTS_READ = _majority(AFTER_DAYS)

# --------------------------------------------------------------------------
# 1. the noise floor — the decision this work package owns
# --------------------------------------------------------------------------
# The smallest change in nightly draw a before/after comparison is entitled to
# call a saving. Below it the report says the effect is too small to separate
# from noise, and prints no number at all: §7.2 would rather say nothing than
# say something the user can disprove.
#
# Two independent arguments land on the same value, which is why it is this one
# and not a round number chosen for looking careful.
#
# The record's own resolution. observer.NUMERIC_MIN_ABS_CHANGE (0.5 W) is the
# smallest movement the behavioural record writes down at all, so two levels
# closer together than that are not two levels — they are one level the record
# could not tell apart. Each side of this subtraction is a median of per-night
# minima drawn from that record and carries that uncertainty, so their
# difference carries it twice.
#
# And what "the draw is gone" actually means. A night whose minimum fell below
# standby.STANDBY_MIN_WATTS is excluded as `below_floor` — the detector saying
# the device really did drop, not that it reached zero. The residual a
# successful week can therefore hide is up to STANDBY_MIN_WATTS, which is the
# same 2 x NUMERIC_MIN_ABS_CHANGE by construction. So the floor absorbs the
# residual instead of it being added a second time to the after side, where it
# would be counted twice.
#
# That second argument is about one detector's constant and holds for both
# (decision 10). An occurrence of a schedule window whose reading fell under
# `schedule.IDLE_MIN_WATTS` is excluded as `below_floor` by that detector for
# the same reason and with the same words — and IDLE_MIN_WATTS is
# `levels.IDLE_FLOOR_WATTS`, which is this same 2 x NUMERIC_MIN_ABS_CHANGE.
# Three constants, one number, arrived at independently: below it a reading is
# not distinguishable from the sensor reading zero, whoever is asking. So the
# floor is not recomputed per kind, and nothing here reads a detector to find
# it.
#
# It is stated in WATTS because that is the unit the record's resolution is in.
# `floor_kwh` converts it onto the kWh-a-month axis the claim is made on, using
# the measurement's OWN hours and occurrences rather than the detector's live
# constants — a device seen on ten nights of the month has a smaller floor than
# one seen on thirty, because it has a smaller claim, and a window seen on 21
# weekday occurrences has a different-sized one again.
NOISE_FLOOR_WATTS = 2 * observer.NUMERIC_MIN_ABS_CHANGE

# --------------------------------------------------------------------------
# 1. and how many times a week that could not be read is tried again
# --------------------------------------------------------------------------
# Another like period. A week that could not be read is re-measured over the
# next one rather than over a longer window, because lengthening it would stop
# it being a comparison with the baseline.
#
# This is that length for a rule that happens every day. What the code waits is
# `days_for(row)` — the same week again, whatever that suggestion's week is —
# because a retry over a different length of window is the one thing
# `_like_periods` would then have to refuse.
RETRY_AFTER_DAYS = AFTER_DAYS

# ...and how many times in total, including the first. Three attempts is about
# a month, by which point either the sensor came back or it is not coming back,
# and a home is not served by an agent that goes on quietly waiting. The last
# attempt does not go quiet either: it reports that the change could not be
# measured, says why, and offers the rollback — which is the whole of §7.2's
# instruction applied to the case where there is no number.
MAX_ATTEMPTS = 3


# What a verification concluded. `saved` / `no_change` / `worse` are the three
# readings of a measurement that happened; the other three are the ways there
# was no measurement to read, and each of them still ends with the rollback on
# the table.
OUTCOME_SAVED = "saved"                  # measurably less draw, stated as a range
OUTCOME_NO_CHANGE = "no_change"          # inside the noise floor — say nothing
OUTCOME_WORSE = "worse"                  # measurably more draw than before
OUTCOME_UNMEASURED = "unmeasured"        # the after week could not be read
OUTCOME_NO_BASELINE = "no_baseline"      # the before never was, and never will be
OUTCOME_NOT_RUNNING = "not_running"      # the rule that was graded was not the one running
OUTCOME_NOT_MEASURED = "not_measured"    # nothing about this change is measurable — decision 12

# There is deliberately no vocabulary of "gone" reasons beside these. The two
# ends of decision 2 that are not a report at all — deleted, and already rolled
# back — are carried as the prose `still_running` returns, because nothing
# dispatches on them: they lead to one call, `suggestions.retire`, whose only
# other argument is the sentence a person reads. §12 loses nothing by that; the
# change journal is where a rollback is counted, and it holds both cases.


# --------------------------------------------------------------------------
# pure rules
# --------------------------------------------------------------------------
def _local_date(ts):
    """The local calendar date an instant falls on."""
    return date.fromtimestamp(int(ts))


def window_of(end, days) -> dict:
    """The window a measurement covers, as §9 prints it."""
    end = int(end)
    return {
        "from": _local_date(end - int(days) * 86400).isoformat(),
        "to": _local_date(end).isoformat(),
        "days": int(days),
    }


# --------------------------------------------------------------------------
# what a week of THIS suggestion is measured over (decisions 7, 8 and 9)
# --------------------------------------------------------------------------
# Which detector's arithmetic, over which window, against which level. A tuple
# because it is a value: two suggestions sharing a shape are measured
# identically, which is the property every comparison in this module rests on.
Shape = namedtuple("Shape", "kind spec idle_below")


def _positive(value):
    """A finite positive number, or None.

    `builder._floor_from`'s check, applied to the one source it does not read.
    `below: NaN` serialises, survives a round trip through JSON and then makes
    every comparison against it false — a threshold that silently classifies
    nothing.
    """
    number = observer.as_float(value)
    if number is None or not math.isfinite(number) or number <= 0:
        return None
    return number


def _night_parts(row):
    """(spec, idle_below, "") — what a standby suggestion is measured over.

    `standby.NIGHT`, read straight off the detector, and that is right for this
    shape and this shape alone (decision 8). The night is a DECLARATION about
    the house: every candidate that detector has ever raised was measured over
    it, and the evidence blob records its label rather than its two ends, so
    there is nothing else it could be rebuilt from.

    No threshold. `standby.assess_night` judges a plateau against the global
    standby ceiling, so there is no per-device level to recover and none to
    lose.
    """
    return standby.NIGHT, None, ""


def _idle_floor(row):
    """(watts, "") | (None, why not): the level a schedule week's draw is
    judged against. Decision 7.

    Never re-derived from the week being measured, and that IS the decision.
    `schedule.intervals` needs this device's own active floor, and the obvious
    way to get one is to histogram the week — which would histogram a device
    the automation now switches off for most of that window. A different
    threshold is a different measurement, and the before/after would then be a
    comparison between two things rather than two measurements of one thing.

    So it comes from what was stored, in the order `builder.readback` takes its
    three sources for the ceiling: by how well each knows THIS config. The
    replay's `guard.below_w` first — it is the threshold the automation that
    actually ran carries, converted into watts, and it is the number the
    grading itself used (`replay._schedule_ceiling` is the same product), so
    the grading, the sentence the card printed and this measurement are one
    figure. Then the detector's own `active_floor`, through
    `builder._floor_from` rather than a second reading of it, because that is
    the number the guard was written FROM and one step back is still this
    config.

    There is deliberately no constant behind the two. A schedule's floor is per
    DEVICE — `_floor_from`'s whole point — so a week whose level cannot be
    recovered is a blob that says so, never one measured against a level
    nobody chose.
    """
    guard = ((row or {}).get("replay") or {}).get("guard") or {}
    graded = _positive(guard.get("below_w"))
    if graded is not None:
        return graded, ""
    # `_floor_from` applies the same check to its own two sources, so what
    # comes back from it is either a usable level or nothing at all.
    found = builder._floor_from((row or {}).get("evidence"))
    if found is not None:
        return float(found), ""
    return None, ("the level this device's idle draw was judged against is not on the "
                  "suggestion any more, so the week cannot be measured the way the rule "
                  "was graded")


def _schedule_parts(row):
    """(spec, idle_below, "") — or a refusal in the third slot. Decision 8.

    The window is rebuilt from `evidence["schedule"]` through
    `builder._spec_from`: the same function that wrote the automation's two
    trigger times and its weekday condition, so the hours measured here and the
    hours the rule actually runs over cannot come apart. It refuses a truncated
    blob rather than defaulting the weekdays, and that refusal is load-bearing
    — defaulting a missing `days` to all seven would measure the two days the
    evidence never looked at, beside a figure projected over the five it named.

    The spec travels even when the LEVEL did not: `days_for` reads it to size
    the attempt, and a week that is about to report this refusal still wants
    the right length of window printed beside it.
    """
    spec, error = builder._spec_from((row or {}).get("evidence"))
    if error:
        return None, None, error
    idle_below, error = _idle_floor(row)
    return spec, idle_below, error


def _measure_nights(rows, start, end, shape, scale: float) -> dict:
    """A week of nights, the standby detector's way."""
    return standby.measure_nights(rows, window.windows(shape.spec, start, end),
                                  scale=scale, spec=shape.spec)


def _measure_occurrences(rows, start, end, shape, scale: float) -> dict:
    """...and a week of a schedule rule's own window, that detector's way.

    Three of its own pieces, joined here the way `standby.measure_nights` joins
    its two. `schedule.intervals` condenses the week ONCE and over the whole
    span, which is `replay._schedule_series`' argument rather than a
    performance one: a series cut to a single occurrence reports both of that
    occurrence's ends unobserved, because there is no row on the far side of
    them to read the silence against. `schedule._cell_verdict` then says what
    each occurrence was — it takes two instants and has never cared which two —
    and `schedule.measure` does the arithmetic. Reaching across the underscore
    is the reach core/replay.py already makes for this same pair, and for its
    reason: a second implementation of "was this device idle across this
    stretch" is a second answer, free to differ from the one the suggestion's
    own evidence reports about the same window. The reason words are that
    module's table too, for the same reason again.

    `occurrences_observed` is counted HERE and replaces the one
    `schedule.measure` derives. That is decision 9's trap rather than a
    correction of the detector: there, every occurrence of a run was idle —
    that is what a run IS — so the occurrences it measured and the occurrences
    it observed are the same number. The week this module measures is the
    opposite case. A working automation switches the device off, every
    occurrence comes back `below_floor`, nothing qualifies, and a floor on the
    qualifying count would declare every successful verification unmeasurable.
    `schedule.OBSERVED` is that detector's own list of the verdicts that mean
    the agent was looking, which is the question `readable` asks.
    """
    series = schedule.intervals(rows, start, end, shape.idle_below, scale=scale)
    starts = [interval.start for interval in series]
    occurrences = window.windows(shape.spec, start, end)
    idle_on, observed, excluded = [], 0, {}
    for day, occurrence_start, occurrence_end in occurrences:
        verdict = schedule._cell_verdict(series, starts, occurrence_start, occurrence_end)
        if verdict in schedule.OBSERVED:
            observed += 1
        if verdict == schedule.IDLE:
            idle_on.append(day)
            continue
        reason = replay._SCHEDULE_REASONS.get(verdict, verdict)
        excluded[reason] = excluded.get(reason, 0) + 1
    return {
        **schedule.measure(series, shape.spec, idle_on, len(occurrences), excluded),
        "occurrences_observed": observed,
    }


# What each shape's measurement calls the four counts, the rate it is projected
# at and the hours it covers. Decision 9, and the table is the device
# `replay._DETECTOR_FIELDS` already is, for the same reason: `readable`,
# `min_read`, `kwh_interval`, `floor_kwh` and `_unreadable_reason` are five
# readers of one vocabulary, and five `if kind ==` would be five places for the
# second one to be forgotten.
#
# A standby blob does not move by a character — those names are on disk in
# activity.db on live installs and core/artefacts.py reads them by name — and a
# schedule blob says what `schedule.measure` writes, which has never existed
# anywhere.
#
# `night_window` is the one field both shapes share and it is deliberately NOT
# in here. It holds a window's LABEL rather than a count of nights, and it is
# what `_like_periods` compares across two blobs written a fortnight apart: a
# comparison reading two different field names would find "" on both sides and
# report two unlike weeks as alike, which is the direction this module refuses
# to fail in. `window.label` renders "07:00-17:00 local Mon-Fri" for a window
# that names its weekdays, so the field describes itself whichever shape wrote
# it.
_NIGHT_FIELDS = {
    "observed": "nights_observed",
    "examined": "nights_examined",
    "qualified": "nights",
    "per_month": "nights_per_month",
    "hours": "hours_per_day",
    "noun": "night",
}
_OCCURRENCE_FIELDS = {
    "observed": "occurrences_observed",
    "examined": "occurrences_examined",
    "qualified": "occurrences",
    "per_month": "occurrences_per_month",
    "hours": "hours_per_occurrence",
    "noun": "occurrence",
}

# kind -> everything that differs between the two shapes, in one table, the way
# core/replay.py holds its own and refusing the same way: a kind with no row
# here is refused by `shape_of`, never given whichever of the two looks
# closest.
#
# `floor_hours` and `projection_days` are the last resort `floor_kwh` falls back
# to when a blob could not say what it measured, and each of them is the KIND's
# own. The night has exactly one window and its length is a constant; a
# schedule has no default window at all, which is `builder._spec_from`'s whole
# argument, so its fallback is the shortest run that detector will ever propose
# rather than a borrowed four hours.
_KINDS = {
    standby.KIND: {
        "parts": _night_parts,
        "measure": _measure_nights,
        "fields": _NIGHT_FIELDS,
        "floor_hours": standby.NIGHT_HOURS,
        "projection_days": standby.PROJECTION_DAYS_PER_MONTH,
    },
    schedule.KIND: {
        "parts": _schedule_parts,
        "measure": _measure_occurrences,
        "fields": _OCCURRENCE_FIELDS,
        "floor_hours": schedule.MIN_RUN_SLOTS * schedule.SLOT_MINUTES / 60.0,
        "projection_days": schedule.PROJECTION_DAYS_PER_MONTH,
    },
}


def _rules_for(kind) -> dict:
    """The table's row for a kind, falling back to the night's.

    The fallback is for a BLOB and not for a suggestion. Every measurement
    written from here on records its own kind and every one already on disk was
    a night, so a blob with nothing to say is a night by the only reading
    history allows. A SUGGESTION whose kind this does not know is refused by
    `shape_of` instead, because there the same fallback would measure something
    nobody asked about and report it as a week of this rule.
    """
    return _KINDS.get(str(kind or ""), _KINDS[standby.KIND])


def _fields_of(measured) -> dict:
    """Which vocabulary a measurement is written in, read off the measurement
    itself."""
    return _rules_for((measured or {}).get("kind"))["fields"]


def shape_of(row):
    """(Shape, "") | (None, why not): what this suggestion's two weeks are
    measured over. Decisions 7 and 8, and the one place either is answered.

    Pure, and it reads only what is STORED on the row — never the live
    detector. The detector's idea of this device's idle hours moves every
    morning; the window the user approved does not, and it is the one both
    halves of a comparison have to agree about.

    A row with no `kind` at all is read as a night: the column is NOT NULL in
    core/suggestions.py, so the only rows without one were written by hand
    before there was a second shape. A row carrying a kind this does not know
    is the other case and is refused — measuring it as a night would be a
    measurement of something nobody asked about, reported as a week of this
    rule.
    """
    row = row or {}
    kind = str(row.get("kind") or standby.KIND)
    rules = _KINDS.get(kind)
    if rules is None:
        return None, (f"the agent has no way to measure a {kind} suggestion, so there is "
                      "no week to compare")
    spec, idle_below, error = rules["parts"](row)
    if error or spec is None:
        return None, error or "the window this suggestion was raised over cannot be rebuilt"
    return Shape(kind, spec, idle_below), ""


# The shape every suggestion on disk has, and `measure`'s default.
NIGHT_SHAPE = Shape(standby.KIND, standby.NIGHT, None)


# --------------------------------------------------------------------------
# decision 5: how long this suggestion's week is
# --------------------------------------------------------------------------
def _weeks_for(spec) -> int:
    """How many whole weeks it takes for `spec` to come round AFTER_DAYS times.

    Whole weeks, and that is the second half of the decision. Ten days over a
    Mon-Fri rule holds two Mondays and one Friday, and a claim about a
    household's week should not weigh its Mondays twice — so the sample covers
    whole weeks and every weekday the rule names contributes equally to both
    ends of the subtraction.
    """
    per_week = max(1, len(spec.days))
    return max(1, -(-AFTER_DAYS // per_week))


def measurable(row) -> bool:
    """Whether this suggestion has anything this module could measure a week
    of. Decision 12, and the one place it is asked — `review.settle` asks it
    before taking a baseline, `_verify_one` before taking the after.

    False for an analyst row: it has no detector, so no shape and no sensor.
    Decided by `source` rather than by `shape_of` failing, because a DETECTOR
    row whose window cannot be rebuilt is a measurement that failed and must
    say why, while an analyst row is one that was never going to happen — and
    reporting the second in the first's words would tell the user something
    went wrong with a change that is working as designed.
    """
    return str((row or {}).get("source") or "detector") != "analyst"


def days_for(row) -> int:
    """How long a week is for this suggestion, in days. Decision 5.

    §7.2 promises a report a week after applying, and for a rule that happens
    every day a week is seven of them. For a rule that happens on the weekdays
    it names it is not: `AFTER_DAYS` would give a Mon-Fri rule five occurrences
    to be measured on — which the proposal calls not defensible — and a Sat-Sun
    rule two. A flat fourteen fixes the first and makes the second worse.

    So what is held constant is OCCURRENCES rather than days: AFTER_DAYS of
    them, over however many whole weeks offer that many. Mon-Fri is a
    fortnight and ten of them; Sat-Sun is four weeks and eight. The every-day
    case is not a special case and is not written as one — one week, seven
    occurrences, seven days, which is AFTER_DAYS and is exactly what every
    baseline already on disk was measured over.

    What a given span actually HOLDS can be one fewer, and always has been:
    `window.windows` counts only occurrences falling whole inside it, so a
    measurement taken at noon loses the one still running. That is the same
    rule, failing in the same direction, that every night has been read under.

    Derived from the STORED window and from nothing else, which is what makes
    the promise already made to an applied suggestion unmovable:
    `review.settle` reads this at apply time for both the baseline's length and
    the week to wait, and `_verify_one` reads it again a week later over the
    same unchanged row.

    A row whose window cannot be rebuilt gets AFTER_DAYS. That is a length for
    an attempt which is going to report a refusal anyway — `shape_of` is what
    refuses it, and refuses it for the same reason.

    The tail case is a rule that happens on one weekday, which comes out at
    seven weeks. That is the honest answer rather than an oversight: a rule
    that acts once a week has to be watched for seven weeks to be seen acting
    seven times, and reporting sooner on fewer occurrences is the thing §7.2
    objects to. Nobody is made to wait for it either — §11 offers the rollback
    without waiting for the weekly report.
    """
    rules = _KINDS.get(str((row or {}).get("kind") or standby.KIND))
    spec = rules["parts"](row)[0] if rules else None
    return _weeks_for(spec) * 7 if spec else AFTER_DAYS


# --------------------------------------------------------------------------
# measuring one of them
# --------------------------------------------------------------------------
def measure(rows, end, days=AFTER_DAYS, scale: float = 1.0, shape=NIGHT_SHAPE) -> dict:
    """One week of a suggestion's own window, measured the detector's way.
    Never a second implementation.

    The detector that raised the candidate, over the occurrences of
    `shape.spec`, at the scale the caller resolved, with the window and the
    hours stated beside the figures so that the report can print them without
    re-deriving anything. Both halves of every comparison in this module come
    from here, which is what makes them two measurements of one thing.

    `shape` is WHAT is being measured — whose arithmetic, over which window,
    against which level — and it defaults to the night because that is the
    shape every suggestion on disk has. It is a parameter rather than three
    constants read inside, because the two halves of a comparison have to be
    measured over the SAME window, the one the suggestion was raised on: a week
    measured over whatever the live detector calls a night would silently
    become a measurement of something else the moment there is more than one
    detector, and there is one now. `_like_periods` catches that after the
    fact; a shape handed in is what stops it happening.

    The blob records its own `kind`, which is how `readable`, `min_read`,
    `kwh_interval` and `floor_kwh` know which vocabulary the counts under it
    are written in. A blob without one is a night, because every blob written
    before there was a second shape was.

    Returns the measurement whatever it comes to, including a week on which
    nothing could be read — that comes back with every measured figure EMPTY,
    which `readable` below is what reads.
    """
    end = int(end)
    start = end - int(days) * 86400
    rows = sorted(
        (r for r in (rows or [])
         if r.get("ts") is not None and start <= int(r["ts"]) < end),
        key=lambda r: int(r["ts"]))
    return {
        "kind": shape.kind,
        "night_window": window.label(shape.spec),
        "window": window_of(end, days),
        "unit_scale_w": scale,
        **_rules_for(shape.kind)["measure"](rows, start, end, shape, scale),
    }


def min_read(measured) -> int:
    """How many of this week's occurrences must have been READ for it to be
    half of a comparison.

    A majority of what the week actually offered, which for a night is four of
    seven and is MIN_NIGHTS_READ. It is derived from the measurement rather
    than from AFTER_DAYS because a week is no longer seven of anything: a
    Mon-Fri rule's fortnight offers ten occurrences and wants six of them, and
    holding it to the night's four would accept a week that was over half
    missing.

    A blob that cannot even say how many occurrences it offered falls back to
    the every-day floor rather than to the majority of nothing, which is one.
    """
    fields = _fields_of(measured)
    examined = int(observer.as_float((measured or {}).get(fields["examined"])) or 0)
    return _majority(examined) if examined > 0 else MIN_NIGHTS_READ


def readable(measured) -> bool:
    """Whether enough of this week's occurrences were seen for it to be half of
    a comparison.

    On the OBSERVED count rather than the qualifying one, for the reason
    MIN_NIGHTS_READ gives: a week in which nothing qualified because the device
    was switched off every time is the successful case, not the unmeasurable
    one. That trap is the same in both vocabularies and is the reason the table
    above exists — `nights_observed` for a night, `occurrences_observed` for a
    window, and never `nights` or `occurrences`.
    """
    fields = _fields_of(measured)
    observed = observer.as_float((measured or {}).get(fields["observed"]))
    return observed is not None and observed >= min_read(measured)


def kwh_interval(measured, hours=None):
    """[low, high] kWh a month this week's occurrences amount to, or None.

    The same arithmetic the detector does for its single `kwh_per_month`, taken
    over the two ends of `watts_range` instead of the median — which is what
    turns one figure into the RANGE §7.2 asks for. The occurrences are the ones
    the draw was actually seen on, exactly as the detector projects them; a
    week is never credited with occurrences it did not observe.

    A week that was READ and in which nothing qualified is [0.0, 0.0] and not
    None: the device dropped below the detector's floor every time, which is
    what a working automation looks like. What it may still be drawing under
    that floor is carried by NOISE_FLOOR_WATTS rather than added here, where it
    would be counted a second time.
    """
    measured = measured or {}
    fields = _fields_of(measured)
    hours = observer.as_float(hours if hours is not None else measured.get(fields["hours"]))
    per_month = observer.as_float(measured.get(fields["per_month"]))
    span = measured.get("watts_range") or []
    if not readable(measured) or hours is None:
        return None
    if not measured.get(fields["qualified"]):
        return [0.0, 0.0]
    if per_month is None or len(span) != 2:
        return None
    return [round(span[0] * hours * per_month / 1000.0, 3),
            round(span[1] * hours * per_month / 1000.0, 3)]


def floor_kwh(baseline, hours=None) -> float:
    """NOISE_FLOOR_WATTS on the axis the claim is made on. Decision 10's second
    half.

    Converted with the BASELINE's own hours and occurrences rather than the
    detector's live constants, because the floor has to be the same size as the
    thing it is guarding: a device seen on ten nights of the month is claiming
    a third as much as one seen on thirty, and a fixed kWh floor would wave the
    first one through and stop the second. That generalises to a window with no
    work at all — a ten-hour window seen on 21 weekday occurrences is a bigger
    claim than a four-hour one seen on 30 nights, and gets a bigger floor.

    The two fallbacks are the kind's own and are the last resort. They are
    reached only for a blob that could not say what it measured, which is a
    verdict carrying no saving for them to guard — there the figure is context
    beside a report that has no number in it, never a threshold anything is
    tested against.
    """
    baseline = baseline or {}
    rules = _rules_for(baseline.get("kind"))
    fields = rules["fields"]
    hours = observer.as_float(hours if hours is not None else baseline.get(fields["hours"]))
    per_month = observer.as_float(baseline.get(fields["per_month"]))
    if hours is None:
        hours = rules["floor_hours"]
    if per_month is None:
        per_month = rules["projection_days"]
    return round(NOISE_FLOOR_WATTS * hours * per_month / 1000.0, 3)


def _like_periods(before, after) -> str:
    """"" if the two weeks are comparable, or why they are not.

    §7.2's "compare like periods" as a check rather than an assumption. TWO
    things can disagree, and until there was a second window only one of them
    could.

    The hours, which come from the window spec the measurement was taken over —
    a thing the detectors' own docstrings name as one of the figures their
    author is least sure of. If it moved between the two measurements, the
    difference between the figures is partly a difference in what was measured,
    and saying so is cheaper than printing a number with one hour of somebody
    else's evening in it. Compared as the LABEL rather than as the spec, and
    that is not laziness: the label is what the baseline stored a week ago, it
    is what §9 printed to the user, and it is the only description of that
    measurement's hours that survives on disk. Two specs rendering the same
    label are the same window by the only definition a reader has — and
    `window.label` names the weekdays of a window that has them, so
    "07:00-17:00 local Mon-Fri" is distinguishable from the same hours every
    day for nothing.

    And the LENGTH, which is decision 6. This docstring used to say that both
    windows were AFTER_DAYS long by construction and that the hours were
    therefore all there was left to check. With `days_for` deriving a length
    per suggestion that sentence is no longer true, and two weeks of different
    lengths compared as though they were alike is precisely the failure this
    exists to catch: ten occurrences of a fortnight subtracted from five of a
    week is a measurement of the calendar.
    """
    was = str((before or {}).get("night_window") or "")
    now = str((after or {}).get("night_window") or "")
    if was and now and was != now:
        return (f"the before was measured over {was} and the after over {now}, so the two "
                "are not measurements of the same thing")
    was_days = observer.as_float(((before or {}).get("window") or {}).get("days"))
    now_days = observer.as_float(((after or {}).get("window") or {}).get("days"))
    if was_days and now_days and was_days != now_days:
        return (f"the before covers {int(was_days)} days and the after {int(now_days)}, so "
                "the two are not measurements of the same length of week")
    return ""


def compare(row, rows, end, scale: float = 1.0, now=None, attempts: int = 1) -> dict:
    """THE judgement: a suggestion row and a week of transitions in, the verdict
    §9 artefact 4 is rendered from out.

    Pure. `row` is the suggestion as core/suggestions.py hands it back — it
    carries the baseline captured at apply time and the estimate the user was
    shown — and `rows` is the record's transitions for the observed entity over
    the week being measured. Nothing here reads a store, a clock or Home
    Assistant, which is what lets a test state a week as a list literal.

    The verdict carries both windows, both measurements, the range and the
    floor, and the estimate WITH its basis. Everything a reader would need in
    order to disagree, because that is what §9 means by the evidence travelling
    with the number.

    What is being measured comes off the row rather than out of a caller, which
    is what keeps this pure AND what keeps the after a measurement of the same
    thing the before was: `shape_of` and `days_for` are the same two answers
    `review.settle` took at apply time, over the same unchanged row.
    """
    row = row or {}
    baseline = row.get("baseline") or {}
    estimate = row.get("estimate") or {}
    # The shape and the length both come off the row, which is what keeps this
    # pure and what keeps the after a measurement of the same thing the before
    # was: `review.settle` sized the baseline from `days_for` over this same
    # unchanged row a week ago.
    shape, why_not = shape_of(row)
    after = measure(rows, end, days_for(row), scale=scale, shape=shape) if shape else {}
    hours = observer.as_float(baseline.get(_fields_of(baseline)["hours"]))

    verdict = {
        "outcome": None,
        "reason": "",
        "attempts": int(attempts),
        "verified_at": int(now) if now else int(end),
        "before": baseline,
        "after": after,
        "floor_kwh_per_month": floor_kwh(baseline, hours),
        "saved_kwh_per_month": None,
        "saved_range_kwh_per_month": None,
        "saved_watts": None,
        # Whose claim is being compared against, carried rather than looked up
        # later: core/replay.py may report the DETECTOR's figures under its own
        # `estimate`, and a report that read `basis` from a row which had moved
        # on would name the wrong side.
        "estimate_kwh_per_month": estimate.get("kwh_per_month"),
        "estimate_basis": estimate.get("basis"),
    }

    if shape is None:
        # Decisions 7 and 8 from the other end. Nothing about this suggestion
        # can be measured — not this week and not last week either, because the
        # baseline was taken through the same refusal — so it is the same fate
        # as a before that never was: reported at once, with the rollback
        # offered, and never retried, since a stored blob does not become
        # readable by waiting.
        return {**verdict, "outcome": OUTCOME_NO_BASELINE, "reason": why_not}

    mismatch = _like_periods(baseline, after)
    if mismatch:
        return {**verdict, "outcome": OUTCOME_NO_BASELINE, "reason": mismatch}

    before_span = kwh_interval(baseline, hours)
    if before_span is None:
        # Decision 1's second half. The before was a photograph taken the
        # moment the automation was written, and the record has been under the
        # automation's influence ever since — so this cannot improve by
        # waiting, and there is nothing to try again.
        return {**verdict, "outcome": OUTCOME_NO_BASELINE,
                "reason": "the week before the change was applied could not be measured, "
                          "so there is nothing to compare this week against"}

    after_span = kwh_interval(after, hours)
    if after_span is None:
        # Decision 1's first half. This may well be readable next week.
        return {**verdict, "outcome": OUTCOME_UNMEASURED,
                "reason": _unreadable_reason(after)}

    # The range, and the only claim this module makes. Widest-honest: the
    # smallest saving consistent with both weeks against the largest.
    low = round(before_span[0] - after_span[1], 3)
    high = round(before_span[1] - after_span[0], 3)
    floor = verdict["floor_kwh_per_month"]
    verdict.update({
        # Widened by the floor on both sides before it is printed. The two ends
        # above are the spread the nights themselves showed, and a device whose
        # draw never wavered would collapse them to a single figure — which is
        # exactly the confident number §7.2 says would damage trust faster than
        # reporting nothing. The floor is what the difference is uncertain BY,
        # so it is the honest half-width of the interval as well as the line
        # below which there is no interval worth printing.
        "saved_range_kwh_per_month": [round(low - floor, 3), round(high + floor, 3)],
        # The midpoint, for a reader who wants one number — never printed
        # WITHOUT the range beside it, which is core/artefacts.py's rule to
        # keep. Unaffected by the widening, which is symmetric.
        "saved_kwh_per_month": round((low + high) / 2.0, 2),
        "saved_watts": _watts_difference(baseline, after),
    })

    # Tested on the UNWIDENED ends: the question "is this difference bigger
    # than the floor" and the interval printed for it are two uses of one
    # number, and asking it of the already-widened range would be applying the
    # floor twice.
    if low > floor:
        return {**verdict, "outcome": OUTCOME_SAVED}
    if high < -floor:
        # The draw is measurably HIGHER than before. Reported rather than
        # rounded away into "no change": this is the case where the user most
        # wants the rollback, and an agent that only reported its wins would be
        # grading its own homework.
        return {**verdict, "outcome": OUTCOME_WORSE}
    # The two weeks overlap inside the floor. §7.2: say the effect is too small
    # to separate from noise, and do not print a number that says otherwise.
    return {**verdict, "outcome": OUTCOME_NO_CHANGE}


def _watts_difference(before, after):
    """How much less the device drew overnight, or None.

    A secondary figure, never the claim — the claim is the kWh range, and this
    is the same difference on the axis the plateau was measured on, for a
    sentence a person can picture. An after week in which nothing qualified is
    the whole of the before: the level was not merely lower, there was no
    plateau left to measure. What it may still be drawing under the standby
    floor is why this is printed as "about", and why NOISE_FLOOR_WATTS is the
    thing that decides whether any of it is claimed at all.
    """
    was = observer.as_float((before or {}).get("watts"))
    now = observer.as_float((after or {}).get("watts"))
    if was is None:
        return None
    return round(was - now, 1) if now is not None else round(was, 1)


def _unreadable_reason(after) -> str:
    """Why this week was not enough of a week, in the detector's own words.

    The exclusion counts are already on the blob and §9's rule is that the
    observation travels with the number — so a week that could not be read says
    how many of its occurrences were missing and not merely that some were.

    The noun is the kind's, because "3 of the 10 nights" is a lie about a rule
    that runs at 11:00 on a Tuesday, and the floor is `min_read`'s rather than
    the constant: a fortnight is held to six and saying four would describe a
    different refusal from the one that was made.
    """
    fields = _fields_of(after)
    observed = int(observer.as_float(after.get(fields["observed"])) or 0)
    examined = int(observer.as_float(after.get(fields["examined"])) or 0)
    seen = "none" if observed <= 0 else f"only {observed}"
    return (f"{seen} of the {examined} {fields['noun']}s in the week could be read, "
            f"and a comparison needs at least {min_read(after)}")


def should_retry(verdict) -> bool:
    """Whether a verification that produced no measurement is worth taking
    again. Decision 1: only the AFTER week can change."""
    verdict = verdict or {}
    if verdict.get("outcome") != OUTCOME_UNMEASURED:
        return False
    return int(observer.as_float(verdict.get("attempts")) or 1) < MAX_ATTEMPTS


def automation_state(states, automation_id) -> str:
    """'on', 'off', whatever else Home Assistant says, or "" when no automation
    entity carries this config id.

    Home Assistant names an automation's ENTITY after its alias and keeps the
    config id in an attribute, so the id recorded in the journal's inverse
    cannot be looked up directly. Read out of the states the daily pass already
    fetched, rather than by asking Home Assistant a second question.
    """
    wanted = str(automation_id or "")
    if not wanted:
        return ""
    for entity_id, state in (states or {}).items():
        if not str(entity_id).startswith("automation."):
            continue
        attributes = (state or {}).get("attributes") or {}
        if str(attributes.get("id") or "") == wanted:
            return str(state.get("state") or "")
    return ""


def _same_rule(stored, live) -> bool:
    """Whether Home Assistant is holding the rule that was graded.

    Through `replay.plan_for`, the parser that graded it, so what is compared
    is exactly what the grading depended on: when the supply is cut, when it is
    restored, on which weekdays, what the guard reads and at what level, and
    which switch is acted on. Comparing the two dicts instead would report an
    edit on every verification — Home Assistant adds `id` on the way out and
    `_write_automation` fills in `mode`, `description` and `condition` on the
    way in — and a check that always fires is a check nobody keeps.

    The weekdays are decision 11, and they are compared as `days` rather than
    as `restricted`. `plan_for` answers EVERY_DAY when nothing restricts the
    cut, so one expression covers both shapes and the night's behaviour is
    unchanged by construction rather than by a branch somebody has to keep
    true. What the grading depended on is which DATES the rule cuts on, and
    that is what `days` says: a rule edited from Mon-Fri to every day is not
    the rule that was graded and reads as an edit here, while a rule whose
    seven-day restriction was written out longhand fires on exactly the same
    dates and does not — a check that fired on a difference the measurement
    cannot see is the other way to build one nobody keeps.

    A live config `plan_for` refuses is not the same rule either: it has been
    edited into something this cannot grade, which is the same conclusion by a
    shorter route.
    """
    was, error = replay.plan_for(stored)
    if error or not was:
        return False
    now, error = replay.plan_for(live)
    if error or not now:
        return False
    return (
        was["cut_trigger"].get("hms") == now["cut_trigger"].get("hms")
        and was["restore_trigger"].get("hms") == now["restore_trigger"].get("hms")
        and was["days"] == now["days"]
        and was["switch"] == now["switch"]
        and was["guard"].get("entity_id") == now["guard"].get("entity_id")
        and was["guard"].get("below") == now["guard"].get("below")
    )


def _behaviour_of(config) -> dict:
    """What Home Assistant runs, out of an automation config: triggers,
    conditions, actions and mode, in the singular keys HA stores, with the
    defaults `_write_automation` fills in made explicit on both sides.

    `id`, `alias` and `description` are left out on purpose — HA adds the
    first on the way out, and renaming a rule or describing it does not change
    what it does.
    """
    config = dict(config or {})
    for plural, singular in (("triggers", "trigger"), ("conditions", "condition"),
                             ("actions", "action")):
        if plural in config and singular not in config:
            config[singular] = config.pop(plural)
    return {
        "trigger": config.get("trigger") or [],
        "condition": config.get("condition") or [],
        "action": config.get("action") or [],
        "mode": config.get("mode") or "single",
    }


def _same_config(stored, live) -> bool:
    """Whether Home Assistant is running the config that was approved, for a
    suggestion whose config nothing here can parse. Decision 12.

    A config compared as data, which `_same_rule` refuses for a detector row
    because it would fire on every default HA fills in. Those defaults are
    exactly what `_behaviour_of` normalises, so what is left to differ is a
    trigger, a condition, an action or the mode — an edit to what runs.
    An empty side is never the same rule.
    """
    if not isinstance(stored, dict) or not stored:
        return False
    if not isinstance(live, dict) or not live:
        return False
    return _behaviour_of(stored) == _behaviour_of(live)


def still_running(change, stored_config, live_ok, live_config, state,
                  graded: bool = True) -> tuple:
    """Whether the week just measured is a week the graded rule was running.
    Returns (gone_reason, not_running_reason) — at most one of them non-empty,
    and both empty when there is nothing wrong.

    Decision 2, and the register is core/rollback.py `_check`'s: every answer is
    a sentence phrased for the person who will read it, returned as a value,
    with nothing raised and nothing improvised. The two return slots are two
    different fates — `gone_reason` means there is nothing left to keep or roll
    back and the suggestion is retired, `not_running_reason` means the
    automation is still there but the week measured nothing about it, so a
    report goes out saying so and the rollback is still offered.

    The two inputs describing Home Assistant are deliberately independent
    readings: `state` is what the daily pass's own registry snapshot says about
    the automation entity, and `live_ok`/`live_config` are one GET of the stored
    config. "Deleted" is only claimed when BOTH agree that there is nothing
    there — a single failed GET is as likely to be a slow Home Assistant as a
    missing automation, and retiring a suggestion over one is the kind of
    silent loss §11 exists to prevent.

    `graded` is False for a suggestion whose config the replay never graded —
    an analyst row (decision 12). Everything else here holds for it as it
    stands; only the edit check changes, from `_same_rule` to `_same_config`.
    """
    change = change or {}
    if change.get("undone_by"):
        return (f"it was rolled back as change #{change['undone_by']} before the week was up, "
                "so there was nothing running to measure"), ""

    automation_id = ((change.get("inverse") or {}).get("automation_id")
                     if isinstance(change.get("inverse"), dict) else None)
    if not automation_id:
        # No inverse means the create's result never yielded an id — the one
        # case core/gate.py cannot record one. Nothing can be checked and
        # nothing can be undone, so this is reported rather than retired and
        # `rollback.why_not` supplies the sentence that says why there is no
        # undo on offer.
        return "", ("the agent has no record of which automation was written, so it cannot "
                    "check whether the rule was running this week")

    if not live_ok and not state:
        return (f"automation {automation_id} is no longer in Home Assistant, so there is "
                "nothing left to measure or to undo"), ""

    if state and state not in ("on", "unavailable", "unknown"):
        # 'off' is a disabled automation and is the case this exists for. An
        # entity that is unavailable, or that has no state row because Home
        # Assistant was still starting, is not evidence of anything — so it
        # falls through and the measurement says what it found.
        return "", (f"automation {automation_id} has been switched off in Home Assistant, "
                    "so the rule was not running for some or all of the week")

    same = _same_rule if graded else _same_config
    if live_ok and not same(stored_config, live_config):
        return "", (f"automation {automation_id} has been edited since it was written, so what "
                    "ran this week is not the rule that was measured and graded")

    # A config that could not be read while the entity is plainly there and
    # switched on: the edit check is the only thing lost, and the week is still
    # a week the rule was running. Measured rather than refused — claiming it
    # was not running would be a stronger statement than one failed GET
    # supports.
    return "", ""


# --------------------------------------------------------------------------
# the I/O half
# --------------------------------------------------------------------------
def _scale_for(entity, snapshot):
    """(scale, "") for a power sensor, or (None, why not).

    The unit is read again rather than remembered. It is the one thing that
    would silently scale every figure here by a thousand if it had changed
    since the suggestion was raised, and `standby.power_scale` is the one
    implementation of reading it.
    """
    if not entity:
        return None, "the suggestion names no entity to measure"
    if not snapshot:
        return None, "the entity registry could not be read"
    states = snapshot.get("states") or {}
    registry_row = next((r for r in (snapshot.get("entities") or [])
                         if isinstance(r, dict) and r.get("entity_id") == entity), None)
    scale = standby.power_scale(states.get(entity), registry_row)
    if scale is None:
        return None, f"{entity} does not report power in a unit this understands"
    return scale, ""


def _switch_of(row):
    """Which switch a suggestion acts on, or None.

    The evidence names it; `entities[1]` is the fallback, and it is guarded
    rather than indexed — a row carrying one entity is not a crash, it is a row
    with no paired switch, and this function is on the path that must always
    answer with a blob rather than raise (`measure_week`).
    """
    row = row or {}
    entities = row.get("entities") or []
    return ((row.get("evidence") or {}).get("switch")
            or (entities[1] if len(entities) > 1 else None))


def observed_entity(row):
    """Which entity a suggestion's measurement is about.

    The evidence names it; `entities[0]` is the fallback, and the two agree
    everywhere the detector wrote them. Never the switch — the switch is what
    is acted on, and the draw is what is measured.
    """
    row = row or {}
    evidence = row.get("evidence") or {}
    entities = row.get("entities") or []
    return evidence.get("entity") or (entities[0] if entities else None)


def measure_week(row, end, days=None, snapshot=None, measured_at=None) -> dict:
    """One week of this suggestion's own sensor, read and measured.

    The I/O around `measure`, and what core/review.py's `settle` calls at apply
    time to capture the "before". The "after" is read by `run_due` below and
    measured by `compare`, which is pure so that a week can be stated as a
    literal — but BOTH go through `measure`, and that is the part that has to
    be one implementation: same statistic, same scale question, same length of
    window is what makes the subtraction between them mean anything.

    Best effort, and it always answers with a blob. At apply time the change is
    already live in someone's home and refusing to record a baseline because
    Home Assistant was slow would leave the suggestion stuck at `proposed` with
    an automation running; a week later, a blob that says what it could not
    measure is what decision 1 reads to decide whether to try again.

    `snapshot` is the registry the caller has already read. The daily pass takes
    one round trip for the whole run and hands it in here; `settle` runs inside
    a request that has none, leaves it unset, and this reads its own.

    `days` unset means this suggestion's own length of week — `days_for` — so a
    caller that knows nothing about windows cannot accidentally ask for seven
    of something that does not happen seven times. `review.settle` passes it
    explicitly because it needs the same figure a second time, for the week to
    wait before the after is taken.
    """
    end = int(end)
    entity = observed_entity(row)
    # Resolved before anything is read, because it is the cheapest refusal here
    # and the only one that needs no registry at all. It is also what the frame
    # below is built from: the window's label and the length both come out of
    # it (decisions 5 and 8).
    shape, why_not = shape_of(row)
    days = days_for(row) if days is None else int(days)
    frame = {
        # The row's own kind even when the shape did not resolve, so that a
        # blob which measured nothing is still read back in the vocabulary it
        # would have been written in.
        "kind": shape.kind if shape else str((row or {}).get("kind") or standby.KIND),
        "entity": entity,
        "switch": _switch_of(row),
        "applied_at": end,
        "measured_at": int(measured_at) if measured_at else end,
        # The window this measurement was TAKEN over — the suggestion's own,
        # which for a night is the live `standby.NIGHT` and for a schedule is
        # the one rebuilt from the stored evidence. This blob describes what
        # THIS measurement covered, so a label that named anything else would
        # make the baseline claim hours it was not taken over — and
        # `_like_periods` compares two of these labels precisely to notice when
        # the window moved between the two measurements. Empty when the shape
        # could not be resolved, because a window nobody can rebuild has no
        # label, and inventing the night's would be comparing the after against
        # hours nothing was ever measured over. Stated here as well as inside
        # `measure` because the error paths below return this frame as the
        # whole blob.
        "night_window": window.label(shape.spec) if shape else "",
        "window": window_of(end, days),
    }
    if not entity:
        return {**frame, "error": "the suggestion names no entity to measure"}
    if shape is None:
        return {**frame, "error": why_not}

    if snapshot is None:
        ok, data = registry.fetch_registry()
        if not ok:
            return {**frame, "error": f"the registry could not be read ({data})"}
        snapshot = data

    scale, why_not = _scale_for(entity, snapshot)
    if scale is None:
        return {**frame, "error": why_not}

    rows = record.between(entity, end - int(days) * 86400, end)
    return {**frame, **measure(rows, end, days, scale=scale, shape=shape)}


def run_due(now=None, due=None, snapshot=None) -> int:
    """Verifies every applied suggestion whose week is up. Returns how many
    were settled one way or another.

    The only thing in this module that touches a store, a clock or Home
    Assistant, and it is called once a day from core/review.py's pass rather
    than from a schedule of its own — §7.2 needs a week to have passed, not a
    tight loop, and the pass already knows what day it is.

    `snapshot` is the pass's one registry round trip. Without it nothing here
    can resolve a unit or read an automation's state, so the run is skipped
    entirely and tried again tomorrow: `verify_after` is already in the past,
    so no attempt is spent and a Home Assistant that was briefly unreachable
    costs a day rather than a third of the retries.
    """
    now = int(now) if now else int(time.time())
    rows = suggestions.due_for_verification(now=now) if due is None else due
    if not rows:
        return 0
    if not snapshot:
        logger.info(f"VERIFY: {len(rows)} suggestion(s) are due but the registry could not "
                    f"be read — trying again tomorrow")
        return 0

    done = 0
    for row in rows:
        try:
            if _verify_one(row, now, snapshot):
                done += 1
        except Exception as e:
            # One suggestion that cannot be verified must not stop the others,
            # and must not stop the daily pass either — the record keeps
            # filling and the home stays controllable whatever happens here
            # (§11). The row keeps its `verify_after`, so tomorrow tries again.
            logger.error(f"VERIFY: suggestion {row.get('id')} could not be verified: {e}")
    return done


def _bare_verdict(row, outcome, reason, attempts, now) -> dict:
    """A verdict for a week that produced no measurement.

    The same shape `compare` returns, with the measured fields empty rather
    than absent, so that §9's rendering and core/suggestions.py's `verdict`
    column read one structure whatever happened. EMPTY and not zeroed, for the
    reason `standby.measure_nights` gives about its own figures: "saved
    nothing" and "could not measure" are different claims about a home.
    """
    return {
        "outcome": outcome,
        "reason": reason,
        "attempts": int(attempts),
        "verified_at": int(now),
        "before": row.get("baseline") or {},
        "after": {},
        "floor_kwh_per_month": floor_kwh(row.get("baseline")),
        "saved_kwh_per_month": None,
        "saved_range_kwh_per_month": None,
        "saved_watts": None,
        "estimate_kwh_per_month": (row.get("estimate") or {}).get("kwh_per_month"),
        "estimate_basis": (row.get("estimate") or {}).get("basis"),
    }


def _not_measured_verdict(row, attempts, now) -> dict:
    """A verdict for a change there was nothing to measure about. Decision 12.

    `_bare_verdict`'s shape, so every reader of the `verdict` column reads one
    structure — with the floor left EMPTY rather than computed. `floor_kwh`
    would fall back to the night's hours and give a kWh figure for a change
    that has no kWh axis at all, and a number printed beside nothing is the
    thing §9 forbids.
    """
    return {
        **_bare_verdict(row, OUTCOME_NOT_MEASURED,
                        "the agent had nothing it could measure for this change",
                        attempts, now),
        "floor_kwh_per_month": None,
    }


def _verify_one(row, now, snapshot) -> bool:
    """One suggestion, from due to answered-or-reported. Returns whether it
    moved."""
    key = row.get("key")
    change = activity.change(row.get("change_id")) or {}
    automation_id = ((change.get("inverse") or {}).get("automation_id")
                     if isinstance(change.get("inverse"), dict) else None)

    live_ok, live_config = (True, None)
    if automation_id:
        live_ok, live_config = read_automation(automation_id)

    gone, not_running = still_running(
        change, row.get("config"), live_ok, live_config,
        automation_state((snapshot or {}).get("states"), automation_id),
        graded=measurable(row))

    if gone:
        # Decision 2's other end: nothing left to keep or roll back, so there
        # is no question to put to anyone. Retired, and said out loud — §11
        # requires the user to be told which and why.
        if not suggestions.retire(row["id"], gone):
            return False
        activity.queue(
            "verification_withdrawn",
            title="A measured result is no longer coming",
            body=(f"The agent was going to report a week's measurement for "
                  f"{artefacts.label_for(row)}, and cannot: {gone}."),
            payload={"suggestion_id": row.get("id"), "change_id": row.get("change_id")},
        )
        logger.info(f"VERIFY: withdrew {key} — {gone}")
        return True

    attempts = int(observer.as_float((row.get("verdict") or {}).get("attempts")) or 0) + 1

    if not_running:
        # A report, not a measurement: the week says nothing about the rule
        # because the rule was not the one running. Delivered at `verified`
        # like any other, because the user still has a keep-or-roll-back
        # decision in front of them and §7.2 offers it whatever the number
        # says — here there is no number, which is itself the finding. Not
        # retried either: an automation somebody disabled or edited is a
        # decision they have already made, and measuring it again next week
        # would be waiting for them to change their mind.
        return _report(row, _bare_verdict(row, OUTCOME_NOT_RUNNING, not_running,
                                          attempts, now), change)

    if not measurable(row):
        # Decision 12: reported now, at the first due date, and never retried —
        # there is no week that would be more measurable than this one.
        return _report(row, _not_measured_verdict(row, attempts, now), change)

    entity = observed_entity(row)
    scale, why_not = _scale_for(entity, snapshot)
    if scale is None:
        return _retry_or_report(
            row, _bare_verdict(row, OUTCOME_UNMEASURED, why_not, attempts, now), change, now)

    # Sized by the same function `compare` sizes the measurement with, over the
    # same row, so the record read and the week measured out of it are one
    # window rather than two that happen to agree.
    rows = record.between(entity, now - days_for(row) * 86400, now)
    verdict = compare(row, rows, now, scale=scale, now=now, attempts=attempts)
    return _retry_or_report(row, verdict, change, now)


def _retry_or_report(row, verdict, change, now) -> bool:
    """Decision 1: measure again next week, or say it could not be measured."""
    if not should_retry(verdict):
        return _report(row, verdict, change)
    # Another like period: the same length of week again, which is
    # RETRY_AFTER_DAYS for a rule that happens every day and a fortnight for one
    # that happens on five weekdays. A retry over a different length is the one
    # thing `_like_periods` would then have to refuse.
    if not suggestions.defer_verification(
            row["id"], now + days_for(row) * 86400, verdict):
        return False
    logger.info(f"VERIFY: {row.get('key')} could not be measured "
                f"(attempt {verdict.get('attempts')} of {MAX_ATTEMPTS}) — "
                f"{verdict.get('reason')}")
    return True


def _report(row, verdict, change) -> bool:
    """Records the verdict at `verified` and puts §9 artefact 4 in the outbox.

    Decision 3: this is as far as the code goes. `kept` and `rolled_back` are
    the user's words and are reached only through `answer` below.

    Decision 4: `normal`, never urgent. A weekly report is the definition of
    something that can wait, and the producer does not name a channel anyway —
    core/delivery.py decides where it goes.
    """
    if not suggestions.record_verdict(row["id"], verdict, state="verified"):
        return False
    stated = suggestions.suggestion(row["id"]) or {**row, "verdict": verdict}
    activity.queue(
        "verification",
        title="A week later: the measured result",
        body=artefacts.verification_text(stated, rollback.why_not(change)),
        payload={"suggestion_id": row.get("id"), "change_id": row.get("change_id"),
                 "outcome": verdict.get("outcome")},
    )
    logger.info(f"VERIFY: {row.get('key')} — {verdict.get('outcome')} "
                f"({verdict.get('saved_range_kwh_per_month')} kWh/month, "
                f"floor {verdict.get('floor_kwh_per_month')})")
    return True


# --------------------------------------------------------------------------
# decision 3: the answer, and the one place it lands
# --------------------------------------------------------------------------
def answer(suggestion_id, keep: bool, session_id: str = "") -> dict:
    """The user's word on a delivered report. Returns {ok, message, state}.

    ONE exit, reached from both surfaces — the panel posts to
    /rest/suggestions/verdict and a chat "yes" is read by
    `agent_runtime.parse_approval_decision` and sent here — for the reason
    /rest/approve takes a change_id: a second place to record a verdict is a
    second place for a rollback to forget to be recorded as one.

    Nothing is gated and nothing is re-approved. The undo is the operation
    core/gate.py computed when the change was approved, run by the one module
    that executes an inverse, and core/rollback.py decision 1 has already
    argued why asking again would be asking permission to withdraw permission.
    What was ever in question was WHICH change, and nobody is choosing here:
    the suggestion row names it.

    `message` is plain text and is the whole answer either way. On a failed
    rollback it is `roll_back`'s own words — which name the automation and what
    Home Assistant said — and the suggestion stays at `verified`, because
    moving it to `rolled_back` over an undo that did not happen would be the
    audit trail lying about a home (§11).
    """
    row = suggestions.suggestion(suggestion_id)
    if not row:
        return {"ok": False, "state": "", "message": f"There is no suggestion {suggestion_id}."}
    if row.get("state") != "verified":
        return {"ok": False, "state": row.get("state") or "",
                "message": (f"That suggestion is {row.get('state') or 'unknown'} rather than "
                            "waiting on a measured result, so there is nothing to answer.")}

    verdict = {**(row.get("verdict") or {}),
               "answered_at": int(time.time()),
               "answer": "kept" if keep else "rolled_back"}

    if keep:
        if not suggestions.record_verdict(row["id"], verdict, state="kept"):
            return {"ok": False, "state": "verified",
                    "message": "The decision could not be recorded, so nothing was changed."}
        activity.queue(
            "verification_kept", title="Automation kept",
            body=(f"You kept the automation the agent added for "
                  f"{artefacts.label_for(row)}, after seeing what it measured."),
            payload={"suggestion_id": row.get("id"), "change_id": row.get("change_id")})
        logger.info(f"VERIFY: {row.get('key')} kept")
        return {"ok": True, "state": "kept",
                "message": "Kept. The automation stays as it is, and the agent will not "
                           "raise this again."}

    undone = rollback.roll_back(row.get("change_id"), session_id=session_id)
    if not undone.get("ok"):
        # Left at `verified` on purpose: the question is still open because the
        # answer did not take effect, and `roll_back`'s message says what the
        # user can do about it.
        logger.warning(f"VERIFY: {row.get('key')} could not be rolled back — "
                       f"{str(undone.get('message'))[:200]}")
        return {"ok": False, "state": "verified", "message": undone.get("message", "")}

    suggestions.record_verdict(row["id"], {**verdict, "rollback_id": undone.get("rollback_id")},
                               state="rolled_back")
    logger.info(f"VERIFY: {row.get('key')} rolled back as change {undone.get('rollback_id')}")
    return {"ok": True, "state": "rolled_back", "message": undone.get("message", "")}
