"""Replay: running a candidate automation over history that has already
happened, to see how it would have behaved.

Design §7 stage 4, and one of the two capitalised steps §2 calls the product.
A detector found a window — `core/detectors/standby.py` a night plateau,
`core/detectors/schedule.py` a stretch of the week — `core/builder.py` turned
it into a config, and this is the step between that config and a person being
asked about it: the triggers are enumerated over the stored record, the
conditions are evaluated at each of them exactly as Home Assistant would have,
and what comes out is a count, a consequence and a bounded saving.

No model, ever. §7.1 is explicit — "the model proposes candidates; it does not
get to grade them" — and this is the grading. Every number below comes out of
arithmetic over rows a person can go and look at, which is the only way the
figure printed in the digest is one they can disagree with.

PURE, AND WITH NO I/O HALF AT ALL
---------------------------------
core/observer.py has core/observation.py behind it and standby.py has `scan`;
this module has neither, and that is deliberate rather than unfinished. Its
caller (WP7) has already read the series — it is the same window the detector
was judged on, thirty days for a night and ninety for a schedule — and reading
it a second time here would mean grading a config against a slightly different
series than the one that produced the measurement beside it. So the series is
handed in.

The other reason is the same one WP4 gives: the evidence floor is ten nights
out of thirty and no harness in this repo can plant that much history, so
everything here has to be exercisable against a list literal or it cannot be
exercised at all. tests/test_replay.py states a fortnight as a literal.


THE SUPPORTED SUBSET, AND WHY IT REFUSES BY DEFAULT
---------------------------------------------------
This grades exactly the shapes `builder.build_config` emits and nothing else:
two `time` triggers, an empty top-level condition, one `choose` branching on
trigger id with a `numeric_state` guard on the cutting branch, `switch.turn_off`
and `switch.turn_on` sequences, `mode: single` — and, in the second shape, a
`condition: time` naming weekdays on that same cutting branch and nowhere else.

A `time` condition carrying `after:` or `before:` is not that shape, and is
refused by the mechanism every unknown key is refused by: `_CONDITIONS` states
which keys a condition may carry, `weekday` is the only one it may, and a
restriction on the hours as well as the days would move the instants this
grading is placed at — which is the one thing it may not approximate.

Anything outside that — an unknown trigger platform, a top-level condition, a
`for:` on the guard, a service this does not model, a stray top-level key — is
refused, and a refusal means the candidate is DISCARDED. It does not mean a
suggestion shown with a weaker number. §7.1 allows exactly two outcomes for a
candidate, validated or discarded, and "we could not fully evaluate this rule"
is below the evidence threshold by any reading. Approximating the parts we do
understand and quietly ignoring the rest is how a number gets attached to
behaviour nobody checked.

Dispatched the way core/rollback.py dispatches an inverse, and for the reason
its decision 3 gives: an operation the table does not know is refused, never
improvised. `_TRIGGERS`, `_CONDITIONS` and `_SERVICES` are the whole vocabulary
and a new trigger type is a new entry in one of them. There are deliberately no
entries for platforms nothing in this phase produces — generality built against
a single config shape is generality built in the wrong direction, and the
extension point is what is wanted here, not the speculative breadth.

The subset is not the only thing checked, because it is not the only way the
grading can be about something other than what it claims. `run` is told WHICH
entity the series belongs to and refuses if that is not the entity the config
guards. Nothing else can catch it: a config guarding one sensor, replayed
against another sensor's thirty days, produces a clean blob with a plausible
saving and no symptom anywhere downstream — the suggestion is stored validated,
and the number describes a device nobody asked about. The caller derives the
two from a candidate that normally names one entity in `evidence` and another
in `entities`, so "normally the same" is exactly the kind of invariant that has
to be asserted rather than assumed. It is a required argument rather than an
optional one for that reason: a default would make forgetting it silent.


TWO SHAPES, AND HOW THIS KNOWS WHICH ONE IT IS GRADING
------------------------------------------------------
`kind` is an argument to `run`, and a required one. It is not sniffed out of
the config and it is not imported.

Not imported, because the module that knows the two shapes is core/builder.py,
and that module pulls `_preflight_automation` in from the Home Assistant tools.
PURE, AND WITH NO I/O HALF AT ALL above is a property this module HAS rather
than a coincidence it enjoys, and an import that made it false would make it
false for every caller at once.

Not sniffed, because the sniff is wrong in exactly the way WP5 argued it was
from the other end. A night rule somebody hand-edited a `condition: time` into
looks like a schedule rule to anything reading the config alone, and it would
then be graded over the five weekdays that condition names rather than over the
seven the rule actually fires on. What the kind answers is what the BUILDER
built — `review.py` has it as `built["kind"]` — and that is the only honest
answer to "what shape is this".

Required for the reason `entity_id` is required, and the failure it would hide
is the same size: a default makes forgetting it silent. And, exactly as
`entity_id` is checked against the guard it claims to be the series for, the
kind is checked against the config it claims to describe. There is one
structural difference between the two shapes — whether the cutting branch
restricts itself to named weekdays — and a `standby` kind over a config that
does, or a `schedule` kind over one that does not, is a refusal rather than a
best guess. That check is the whole use of the difference; nothing else here
reads it to decide anything.


THE FIELD NAMES, WHICH ARE ON DISK AND ARE READ BY THREE MODULES
----------------------------------------------------------------
The blob below is stored by `suggestions.mark_validated` and read back by name
somewhere else entirely: `review.disqualified` reads the wrong count to
enforce MAX_WRONG_NIGHTS = 0, `review.rank` reads the saved count,
core/artefacts.py reads `nights_examined`, `nights_fired`, `nights_saved` and
`nights_wrong`, and the estimate's `hours_per_night` and `nights_per_month`
are read by artefacts and by core/verify.py.

The review pass reads its two through `fields_for` below rather than by name,
which is why it can be handed either shape; artefacts still reads the night's
names directly, and generalising THAT is its own work package.

So a blanket rename is not a rename, it is a migration across three modules —
and the one field that would fail QUIETLY is the one that matters most.
`disqualified` used to read `nights_wrong` with an `or 0` behind it, so
against a blob that had renamed the field it would read absent as zero and
pass every candidate the record says would have cut power to a device in use.
It now asks `fields_for` what this kind calls the field and refuses a kind the
table does not know, which is the same refusal made one layer up.

The line drawn here is the one WP3b drew in the detector, for its reason. A
field that COUNTS something in nights takes the occurrence vocabulary for a
schedule replay — `occurrences_examined`, `occurrences_fired`,
`occurrences_guard_failed`, `occurrences_wrong`, `occurrences_saved`,
`occurrences_per_month`, `hours_per_occurrence`, `occurrences`, and
`min_saved_occurrences` inside `thresholds` — because "nights" is a lie about a
window that happens at 11:00 on a Tuesday, a standby blob's names are on disk
on live installs, and a schedule blob's have never existed anywhere. Everything
that is not a count of nights keeps ONE name for both shapes:
`would_have_interrupted`, `bounded_by_detector`, `outcomes`, `reasons`,
`guard`, `switch`, `window`, `hours_window`, `thresholds`, `watts`,
`kwh_per_month`, and every field of a per-date entry.

Mapped in `_FIELDS`, one table, rather than assembled as two blobs — the same
device `_DETECTOR_FIELDS` already is for the detector's own spelling of the
four estimate figures.

Two consequences are deliberately left to later work packages, and neither is
edited here:
  - WP7 (core/artefacts.py) and WP8 (core/review.py) read the names above and
    have to learn the second set. Until they do a schedule blob renders as
    nothing and ranks as nothing, which is the safe direction to be wrong in,
    and is why nothing produces a schedule candidate yet.
  - `verify._same_rule` compares two plans field by field. When this was
    written it did NOT compare `days`, so a live rule whose weekday
    restriction had been edited still read as the rule that was graded; WP6
    added that comparison, over the `days` `plan_for` carries.

One collision is worth naming because the night shape already has it.
`evidence["nights"]` is an INTEGER — the detector's count of qualifying nights
— and `replay["nights"]` is the LIST of per-date verdicts. Two blobs, one word,
two meanings, and `occurrences` inherits exactly the same collision from
`schedule.measure`. Read the blob you are in.


THE UNIT LINE, WHICH IS A FACTOR OF A THOUSAND
----------------------------------------------
`below` in the config is in the SENSOR'S RAW REPORTED UNITS. builder.py divides
STANDBY_MAX_WATTS by the scale precisely so the condition can be compared
against what Home Assistant will actually see, and core/record.py stores the
raw reported state too. So the guard comparison here is raw against raw and the
scale must NOT be applied to it a second time — WP6 shipped that bug once in
its readback ("less than 0.03 W" on a kilowatt sensor) and caught it in review.

Every value below is on one side of that line or the other, and each site says
which. Watts appear only where a number is reported to a person or multiplied
into kWh; raw units appear only in the guard comparison. `standby.assess_night`
takes the scale and works in watts, which is why it is handed one.


THE THREE QUESTIONS, AND WHAT EACH HONESTLY MEANS HERE
------------------------------------------------------
*Would it have fired?* The trigger instants are wall-clock times, so they are
enumerated from the calendar rather than found in the record — 01:00 happened
whatever the sensor was doing. The interesting number is not how many 01:00s
there were but on how many of them the `numeric_state` guard would have PASSED,
and that can be fewer than the detector's qualifying nights: the guard is one
instant and the detector's plateau is four hours.

*Would it have fired wrongly?* This is the question only replay answers, and
for either shape the device's own power trace answers it: on an occurrence
where the guard passed at the cutting trigger, did the sensor later show draw
above the level that means this device is IN USE, before the restoring one? If
it did, cutting power would have interrupted something the household was doing.
No occupancy and no cross-entity evidence is reached for, because none is
needed.

WHICH level that is differs between the shapes, and it differs the way the two
detectors differ. A night candidate is judged against
`standby.STANDBY_MAX_WATTS` — a claim about every device in the house. A
schedule candidate is judged against the device's OWN active floor, which is
already in the plan and needs no second constant fetching: `guard.below_w` is
that number in watts, because `builder._schedule_shape` wrote the guard from
`active_floor` and from nothing else. Reading it back off the plan is what
keeps this comparison and the sentence the card prints — "it only switches off
below N W" — the same number rather than two that agree today.

The asymmetry bounds the whole claim and is why the two are not symmetric in
the code. **A device used later that night is PROOF the automation would have
been wrong.** A device not used is NOT proof it would have been right — §7.1
says consequences can only be estimated, because the record shows what happened
and not what would have happened after an intervention. Someone whose
television was cut at 01:00 might have gone and turned the socket back on, and
nothing in the record can say so. So proof of wrongness is counted wherever it
is visible, including across a stretch that is otherwise too patchy to credit,
while absence of it is never read as a clean night on its own — a night is
credited only when its whole span was readable.

What CREDITS an occurrence is the detector's own answer to "was this span
readable and idle throughout", CALLED rather than reimplemented — and the two
detectors answer it differently because they are claiming different things.
`standby.assess_night` wants a plateau: above the floor, under the ceiling, and
FLAT across the whole night. A schedule's claim is not about flatness at all;
it is that the device stayed under its own active floor for the whole stretch,
which is what its card says and what its 336 cells were each judged on. So the
answer for that shape is schedule.py's — the interval series it condenses the
record into, and the cell verdict it reads off them, asked about the
occurrence's own span instead of about a half-hour. Two implementations of "was
this device idle" is the drift core/detectors/levels.py was extracted to
prevent, and a span this credits that the detector excluded would be two
components describing one window differently inside one suggestion.

*What would it have saved?* Recomputed from the nights that survive both
checks, with the same statistic the detector uses (the median of the per-night
minima) so that the two figures differ only in WHICH nights counted. Replay may
only tighten the detector's estimate, never inflate it: if the recomputed
figure comes out above it, `_estimate` reports the detector's instead and says
so, and logs a warning — because one of the two is then wrong and shipping the
larger of two numbers that disagree is the opposite of what §7.2 asks.

The rate it is projected at carries the weekdays with it. A Mon-Fri window
comes round about twenty-one times a month and not thirty, which is
`schedule.occurrences_per_month`'s own weekday fraction; a rule naming all
seven days has a fraction of one and the arithmetic is the arithmetic it always
was. Leaving it out would inflate every weekday claim by half — and quietly,
because the detector's smaller figure would then be the one reported and
`bounded_by_detector` the only trace of it.

The estimate is taken from ONE side whole, never assembled from both. §9
renders it as a sentence — "about 12 W for four hours on thirty nights, which
is roughly 1.4 kWh a month" — and a kWh figure taken from the detector while
the three numbers beside it stayed the replay's is a sentence whose own
arithmetic does not come out. That is why there are two objects rather than
one: `replay` holds what this module measured and its four figures always
multiply out together, `estimate` holds what is being CLAIMED and says in
`basis` whose it is. The disagreement is not hidden by either — comparing them
is what `bounded_by_detector` is for.


FIDELITY — INHERITED FROM WP4, NOT RE-LITIGATED
------------------------------------------------
The record is a jitter-filtered transition log, not a series of samples. Every
rule standby.py established applies here unchanged and for identical reasons,
so they are imported rather than restated: `source='statistics'` rows are
bucket MEANS and cannot show that a value stayed below a threshold; a silence
materially longer than `observer.NUMERIC_MAX_GAP_SECONDS` is missing coverage
rather than a flat reading and is never interpolated across; 'unavailable' and
'unknown' are not zero watts.

The disqualification reasons are standby's own constants, not a parallel
vocabulary — a night excluded here for the same reason it was excluded there
must read the same in the evidence, or §9's rule that the observation travels
with the number produces two observations of one night. The one reason added is
`REASON_NO_TRIGGER`, and it is added because the detector has no equivalent
concept: it measures a span, and only an instant can fail to exist.

A statistics row does one thing here it cannot do there: a bucket mean ABOVE
the ceiling proves the underlying readings reached it, because a maximum is
never below a mean. That is the one claim a mean supports, and it is used —
see `_peak_watts`. It still cannot support the converse, so such a row still
blocks credit.

The schedule shape reaches that vocabulary by a second route and lands on the
same words, which is not luck: core/detectors/schedule.py declared
`REASON_NO_COVERAGE`, `REASON_UNAVAILABLE`, `REASON_STATISTICS` and
`BELOW_FLOOR` with standby's values deliberately, "so that one renderer can
read the `excluded` dict of either blob". The one verdict with no counterpart
is `ACTIVE`, and `_SCHEDULE_REASONS` maps it onto `standby.REASON_IN_USE`
rather than carrying it through as a fifth word — it is the same fact about a
span, and §9 puts these in front of a person.


WHICH DATES ARE OCCURRENCES, AND WHICH ARE NOT EXAMINED AT ALL
---------------------------------------------------------------
There are now two ways a date can fail to be an occurrence of the rule, and
collapsing them into one is how a weekday rule comes to report "credited on 10
of 90" for a rule that only ever had 64 chances.

A Saturday under a Mon-Fri rule is NOT an occurrence the rule declined. It is
not an occurrence. Home Assistant evaluates the weekday condition before the
guard is ever reached, so there is nothing about that date the rule could have
got right or wrong, and it must not enter the denominator the monthly figure is
projected over. Such a date is skipped before the trigger instants are even
placed on it.

A date the clock skipped the trigger instant on IS an occurrence and IS counted
as examined, under OUTCOME_NO_TRIGGER. That reasoning is decision 1 below and
is unchanged: "did not fire because the clock skipped it" is a fact about an
occurrence that really came round, and dropping it takes a real date out of the
denominator. The two must stay the same way round.

`window.windows` applies both of those rules already, for a SPEC — and `run`
still places the config's OWN two instants rather than calling it, deliberately
and for the reason the comment there gives: what is being placed is the
automation that was WRITTEN, not the window it was derived from, and noticing
when those two differ is the whole point of this module. What `run` takes from
the config rather than from a spec is the weekday set itself, read out of the
cutting branch's `condition: time` by `plan_for`. The filter is
`window.windows`' filter; it is just made about the rule instead of about the
finding.


HOW FAR BACK, WHICH IS NOT ONE NUMBER EITHER
---------------------------------------------
`lookback_days` defaults to None and resolves to the DETECTOR'S own, per kind:
thirty days for a night, ninety for a schedule. It used to default to one
number, and the number was standby's — which against a schedule candidate would
grade a claim measured over ninety days across thirty days of record, credit
roughly a third of the occurrences, and then compare that count against a floor
set for the other window. All three are wrong and none of them raises.

That is the floor's own rule rather than a new one: what a kind is judged
against comes from the detector that raised it, never from a number chosen
here. `review.py` still passes one lookback for the whole pass; teaching it to
pass each detector's own is WP8, and this is why it has to.


DST, WHICH IS NOT A ROUNDING ERROR
-----------------------------------
The window is thirty days of LOCAL times and will cross a transition twice a
year, where a night is 23 or 25 hours and the trigger's wall-clock time may not
exist or may happen twice. Three decisions, written down because the failure
mode of leaving them implicit is a night that silently has no trigger instant
and then reads as a night the guard failed:

1. A wall time that does not exist on a date (the hour was skipped forward over)
   makes that night `REASON_NO_TRIGGER`. Home Assistant would not have fired,
   and "did not fire because the clock skipped it" is a different fact from
   "did not fire because the device was in use". `window.instant` detects it by
   round-tripping mktime through localtime rather than by knowing any zone's
   rules — the round trip started here, and is now the seam's single answer,
   which is why the detector's window drops a night this would report as
   having no trigger rather than measuring a shorter one.
2. A wall time that happens twice fires once, at whichever instant the C
   library resolves. A second turn-off of an already-off switch changes nothing,
   so nothing here looks for the repeat.
3. The credited span is the elapsed time between the two resolved instants,
   CAPPED at the nominal span the config's own clock times describe. A
   23-hour night credits the three hours that really elapsed; a 25-hour night
   credits four rather than five, because replay may only tighten.
4. A weekday restriction and a clock change are independent, and are applied in
   that order — the weekday filter first, the instant second. A date the rule
   does not name never reaches `window.instant` at all; a date it does name and
   the clock ate is OUTCOME_NO_TRIGGER, exactly as it is for a rule that names
   all seven. Doing it the other way round costs nothing on a night rule and
   silently changes a weekday rule's denominator, which is the section above.


THE SWITCH'S OWN SERIES IS NOT READ, AND THAT IS A DECISION
------------------------------------------------------------
A device the household already switched off itself is a different fact from one
that idled, and the switch's state is where that difference is written. It is
still not read here, for three reasons.

It changes no number. The saving is the draw that would have been avoided, and
the sensor already carries it: a device someone switched off reads near zero,
falls under `standby.STANDBY_MIN_WATTS`, and is excluded by
`REASON_BELOW_FLOOR` with nothing credited — the same outcome reading the
switch would have produced, reached from the series the claim is actually made
about.

It would cost candidates for a reason that is not about the evidence. The
watchlist is opt-in and fails closed (core/watchlist.py): a household may well
have put the power sensor on it and not the switch, and a replay that required
the switch's series would refuse to validate exactly the candidates the
detector was able to find.

And it would double the vocabulary. Every coverage, statistics and availability
rule above would have to be answered again for a second series and the two
reconciled, to decide a question the first series already answers.

What the switch's series WOULD add is whether the household has the habit half
in place already — and standby.MIN_QUALIFYING_FRACTION is where that judgement
lives, on the detector's side, where it belongs.
"""
from datetime import datetime, timedelta
import logging
import math
import time

from core import observer
from core import window
from core.detectors import schedule
from core.detectors import standby

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# what a night's outcome can be
# --------------------------------------------------------------------------
# One per night, mutually exclusive, ordered here as the evaluation reaches
# them. Every night in the window gets exactly one, because §9's rule is that
# the observation travels with the number and a replay reporting "14 of 30"
# without saying what happened to the other sixteen is the number without the
# observation.
OUTCOME_SAVED = "saved"              # fired, span clean, credited
OUTCOME_WRONG = "wrong"              # fired, and the device was used afterwards
OUTCOME_NO_CREDIT = "no_credit"      # fired, but nothing can be claimed for it
OUTCOME_NOT_FIRED = "not_fired"      # the guard was read, and it failed
OUTCOME_UNREADABLE = "unreadable"    # the guard instant itself could not be read
OUTCOME_NO_TRIGGER = "no_trigger"    # the wall time does not exist on that date

# The two reasons standby.py has no equivalent for, and both are added rather
# than borrowed because borrowing would conflate two different answers.
#
# It measures a span, and a span always exists — only an INSTANT can be skipped
# over by a clock change.
REASON_NO_TRIGGER = "no_trigger"
# And it has one word, REASON_IN_USE, for a device that was working at any
# point in the night. Here the two ends of the night are the two different
# questions: working at the cutting instant means the rule left the device
# alone (question 1, and standby's own word is right for it), while working
# afterwards means the rule cut power to a device that was in use (question 2).
# One reason string covering both would make `reasons` unreadable in exactly
# the place §9 wants it read.
REASON_USED_LATER = "used_later"

# The outcomes that mean the rule would have run. Named rather than counted
# inline, because "fired" is the headline of question 1 and it includes the
# nights the firing turned out to be wrong — a count that quietly dropped those
# would be the automation's own best case reported as its behaviour.
_FIRED = (OUTCOME_SAVED, OUTCOME_WRONG, OUTCOME_NO_CREDIT)


# --------------------------------------------------------------------------
# the evidence floor (§7.1)
# --------------------------------------------------------------------------
# The same floor the detector applies, deliberately reused rather than restated:
# a replay that credits fewer nights than the measurement it is checking is a
# replay that disagrees with it, and the honest response to two components
# disagreeing about a house is to discard the candidate rather than to pick the
# one whose number is nicer.
#
# Where it bites is exactly there. When the config matches the detector — which
# is what builder.py guarantees — the credited nights are the detector's
# qualifying nights and this is satisfied by construction. It only fails when
# the config being graded describes something else: different hours, the wrong
# entity, a threshold off by the factor of a thousand above. That is what it is
# for.
#
# The ARGUMENT generalises exactly and the NUMBER does not, so there is one of
# these per kind and each is its own detector's. Ten qualifying nights out of
# thirty is what standby.py needs before it will claim a plateau; eight
# occurrences of a weekday out of ninety days is what schedule.py needs before
# it will claim a weekly shape, and it needs eight rather than ten for reasons
# argued at MIN_OCCURRENCES about how long a habit can hide. A single constant
# chosen here would be a third opinion about evidence, held by the component
# whose whole job is to check the other two against each other.
MIN_SAVED_NIGHTS = standby.MIN_QUALIFYING_NIGHTS
MIN_SAVED_OCCURRENCES = schedule.MIN_OCCURRENCES


# --------------------------------------------------------------------------
# the vocabulary: triggers
# --------------------------------------------------------------------------
def _time_trigger(trigger: dict):
    """A `time` trigger: fires at one wall-clock time, every local day.

    `at` is Home Assistant's "HH:MM:SS". The seconds are required rather than
    defaulted because builder.py writes them, and a config that spelled the
    time differently is a config this did not build and has not been read.
    """
    at = str(trigger.get("at") or "")
    parts = at.split(":")
    if len(parts) != 3:
        return None, f"a time trigger at {at!r} is not written as HH:MM:SS"
    try:
        hour, minute, second = (int(p) for p in parts)
    except (TypeError, ValueError):
        return None, f"a time trigger at {at!r} is not written as HH:MM:SS"
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        return None, f"a time trigger at {at!r} is not a time of day"
    return {"kind": "time", "at": at, "hms": (hour, minute, second)}, None


def _time_instant(spec: dict, day):
    """The instant a `time` trigger fires on one local date, or None when the
    clock skipped over it.

    core/window.py's answer, not one of this module's own. The round-trip that
    can tell a skipped hour from a real one started here and is now the seam's
    single answer about the clock, which is what makes "there was no trigger
    instant" the same fact for the rule being graded and for the window it was
    derived from — the distinction the whole DST branch below rests on.
    """
    return window.instant(day, *spec["hms"])


# platform -> (parse the trigger, the keys it may carry, resolve it on a date)
#
# The whole vocabulary. A trigger platform absent from this table is refused by
# `_parse_trigger`, never approximated — the same rule core/rollback.py's `_OPS`
# states for inverses, and for the same reason: a rule graded on an
# approximation of what it does is graded on something else.
#
# Both halves are needed for a new platform: reading it, and placing it on a
# calendar date. What a new platform would ALSO need is a decision about how
# its span is bounded — `plan_for` derives that from two wall-clock times, and
# that arithmetic is the one part of this module that is genuinely specific to
# `time`. It is refused there rather than approximated here.
_TRIGGERS = {
    "time": (_time_trigger, {"platform", "at", "id"}, _time_instant),
}


def _instant_on(spec: dict, day):
    """One trigger's firing instant on one local date, through the table."""
    return _TRIGGERS[spec["platform"]][2](spec, day)


# --------------------------------------------------------------------------
# the vocabulary: conditions
# --------------------------------------------------------------------------
def _trigger_condition(condition: dict):
    """`{condition: trigger, id: ...}` — which branch of the `choose` this is."""
    trigger_id = condition.get("id")
    if not trigger_id or not isinstance(trigger_id, str):
        return None, "a trigger condition names no trigger id"
    return {"kind": "trigger", "id": trigger_id}, None


def _numeric_state_condition(condition: dict):
    """`{condition: numeric_state, entity_id: ..., below: ...}` — the guard.

    `below` is kept in the sensor's RAW reported units, exactly as it sits in
    the config, and is compared against the record's raw states. See the unit
    line in this module's docstring; converting it here would be the factor of
    a thousand.
    """
    entity_id = condition.get("entity_id")
    if not entity_id or not isinstance(entity_id, str):
        return None, "a numeric_state condition names no entity"
    if "below" not in condition:
        return None, f"a numeric_state condition on {entity_id} has no `below`"
    below = observer.as_float(condition.get("below"))
    if below is None:
        return None, f"the `below` threshold on {entity_id} is not a number"
    return {"kind": "numeric_state", "entity_id": entity_id, "below": below}, None


# Home Assistant's spelling of the weekdays, in date.weekday() order so the
# index IS the weekday number core/window.py uses. builder.py declares the same
# tuple and this does not import it, which is the one place in this pipeline a
# duplicate is not drift: both modules are quoting Home Assistant's own schema
# rather than each other, builder to WRITE it and this to READ it, and neither
# is free to change it. Importing would also be importing `_preflight_automation`
# — see TWO SHAPES in the module docstring.
_HA_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_NUMBERS = {name: number for number, name in enumerate(_HA_WEEKDAYS)}


def _time_condition(condition: dict):
    """`{condition: time, weekday: [...]}` — which days the branch may run on.

    The days only. `after:` and `before:` are not in the allowed keys below, so
    a time condition restricting the HOURS as well is refused by the same
    mechanism an unknown key is: it would move the instants this grading is
    placed at, and the span it bounds, away from the two `time` triggers the
    whole arithmetic is derived from.

    A name Home Assistant does not spell is refused rather than dropped, which
    is where this differs from `builder._weekdays_of`. That one is rendering a
    sentence about a config already stored and a day it cannot read is one word
    missing from prose; this is deciding which dates the rule ran on, and a
    dropped day is an occurrence silently taken out of the denominator.
    """
    days = condition.get("weekday")
    if not isinstance(days, list) or not days:
        # An empty list is a rule that can never fire, and guessing it meant
        # every day is the widening `builder._spec_from` refuses for the same
        # reason: a window whose days are unknown is a window nobody measured.
        return None, "a time condition names no weekdays"
    numbers = set()
    for name in days:
        number = _WEEKDAY_NUMBERS.get(name) if isinstance(name, str) else None
        if number is None:
            return None, f"a time condition names {name!r}, which is not a weekday"
        numbers.add(number)
    return {"kind": "time", "days": frozenset(numbers)}, None


# condition -> (parse it, the keys it may carry)
#
# `above` and `for` are deliberately NOT in the allowed keys for numeric_state.
# A `for:` is the shape the record is least faithful to — NUMERIC_MIN_INTERVAL
# is a 60-second floor and NUMERIC_MAX_GAP a fifteen-minute heartbeat, so
# "below the threshold continuously for five minutes" is a question a
# transition log answers by guessing. builder.py does not write one, and
# refusing is what stops one arriving unnoticed.
#
# `after` and `before` are the same omission on the `time` condition, and the
# same refusal comes out of the same line of `_parse_condition`. A time
# condition here says WHICH DAYS the cutting branch may run on and nothing
# else; one that also restricted the hours would move the instants this
# grading is placed at away from the two triggers the whole span is derived
# from, which is a rule this module would then be scoring an approximation of.
_CONDITIONS = {
    "trigger": (_trigger_condition, {"condition", "id"}),
    "numeric_state": (_numeric_state_condition, {"condition", "entity_id", "below"}),
    "time": (_time_condition, {"condition", "weekday"}),
}


# --------------------------------------------------------------------------
# the vocabulary: services
# --------------------------------------------------------------------------
# service -> what it does to the target, in the only terms this module models.
# A service that is not a supply being cut or restored has no effect this can
# score — `notify.mobile_app` would be a real effect with a real consequence
# and no number attached — so it is refused rather than treated as a no-op.
_SERVICES = {
    "switch.turn_off": "off",
    "switch.turn_on": "on",
}


# --------------------------------------------------------------------------
# parsing the config: reject by default, at every level
# --------------------------------------------------------------------------
# Top-level keys that may appear. Everything else — `variables`,
# `trigger_variables`, `max_exceeded`, Home Assistant's newer `triggers`/
# `conditions`/`actions` spellings — is refused, because each of them changes
# what the automation does and none is read here.
_ALLOWED_TOP_LEVEL = {"id", "alias", "description", "trigger", "condition", "action", "mode"}

# `single` only. The others describe what happens when a run overlaps another,
# and this shape's two triggers are four hours apart on a sequence of one
# service call — but a config asking for `queued` or `parallel` is asking for
# behaviour this does not model, and guessing it does not matter is exactly the
# guess the subset exists to prevent.
_ALLOWED_MODE = "single"


def _unknown_keys(mapping: dict, allowed) -> list:
    return sorted(k for k in mapping if k not in allowed)


def _parse_trigger(trigger):
    if not isinstance(trigger, dict):
        return None, "a trigger is not a mapping"
    platform = trigger.get("platform")
    entry = _TRIGGERS.get(platform)
    if not entry:
        return None, f"no replay is defined for a {platform!r} trigger"
    parse, allowed, _resolve = entry
    unknown = _unknown_keys(trigger, allowed)
    if unknown:
        return None, f"a {platform} trigger carries {', '.join(unknown)}, which is not replayed"
    trigger_id = trigger.get("id")
    if not trigger_id or not isinstance(trigger_id, str):
        # The `choose` branches on it. A trigger without one cannot be told
        # apart from the other, and the branch that runs would be a guess.
        return None, f"a {platform} trigger has no id, so no branch can be matched to it"
    spec, error = parse(trigger)
    if error:
        return None, error
    spec["id"] = trigger_id
    spec["platform"] = platform
    return spec, None


def _parse_condition(condition):
    if not isinstance(condition, dict):
        return None, "a condition is not a mapping"
    kind = condition.get("condition")
    entry = _CONDITIONS.get(kind)
    if not entry:
        return None, f"no replay is defined for a {kind!r} condition"
    parse, allowed = entry
    unknown = _unknown_keys(condition, allowed)
    if unknown:
        return None, f"a {kind} condition carries {', '.join(unknown)}, which is not replayed"
    return parse(condition)


def _parse_service(step):
    """One step of a branch's sequence."""
    if not isinstance(step, dict):
        return None, "an action step is not a mapping"
    unknown = _unknown_keys(step, {"service", "target"})
    if unknown:
        return None, f"an action step carries {', '.join(unknown)}, which is not replayed"
    service = step.get("service")
    effect = _SERVICES.get(service)
    if not effect:
        return None, f"no replay is defined for the {service!r} service"
    target = step.get("target")
    if not isinstance(target, dict) or _unknown_keys(target, {"entity_id"}):
        return None, f"the target of {service} is not a single entity_id"
    entity_id = target.get("entity_id")
    if not entity_id or not isinstance(entity_id, str):
        # A list of entities is a rule about several devices, and the saving
        # below is arithmetic over one power sensor. Refused rather than
        # scored against whichever one the evidence happens to name.
        return None, f"the target of {service} is not a single entity_id"
    return {"effect": effect, "entity_id": entity_id}, None


def _parse_branch(branch):
    if not isinstance(branch, dict):
        return None, "a choose branch is not a mapping"
    unknown = _unknown_keys(branch, {"conditions", "sequence"})
    if unknown:
        return None, f"a choose branch carries {', '.join(unknown)}, which is not replayed"
    conditions = branch.get("conditions")
    sequence = branch.get("sequence")
    if not isinstance(conditions, list) or not conditions:
        return None, "a choose branch has no conditions"
    if not isinstance(sequence, list) or not sequence:
        return None, "a choose branch has no sequence"

    # Three buckets rather than two, because a weekday restriction is not a
    # guard: a guard is read off the record at one instant and decides whether
    # the rule fired, and this decides whether the occurrence happened at all.
    # Counting it as a guard would trip `plan_for`'s "exactly one guard" check
    # on every schedule config, and reading it as one would put a date the rule
    # never ran on into the denominator as a date it declined.
    trigger_ids, guards, days = [], [], []
    for condition in conditions:
        parsed, error = _parse_condition(condition)
        if error:
            return None, error
        if parsed["kind"] == "trigger":
            trigger_ids.append(parsed["id"])
        elif parsed["kind"] == "time":
            days.append(parsed["days"])
        else:
            guards.append(parsed)
    if len(trigger_ids) != 1:
        # Exactly one, because the branch is selected by which trigger fired
        # and a branch matching two triggers or none is a branch whose
        # selection this cannot reproduce.
        return None, "a choose branch does not match exactly one trigger id"

    effects = []
    for step in sequence:
        parsed, error = _parse_service(step)
        if error:
            return None, error
        effects.append(parsed)
    return {"trigger_id": trigger_ids[0], "guards": guards, "days": days,
            "effects": effects}, None


def _parse_action(action):
    """The action block: exactly one `choose`, and nothing beside it."""
    if not isinstance(action, list) or len(action) != 1:
        return None, "the action is not a single choose block"
    block = action[0]
    if not isinstance(block, dict) or _unknown_keys(block, {"choose"}):
        # `default:` lands here. Nothing in this phase writes one, and a
        # default branch is behaviour that runs on every night no branch
        # matched — which is every night, since the guard is what fails.
        return None, "the action is not a single choose block"
    branches = block.get("choose")
    if not isinstance(branches, list) or not branches:
        return None, "the choose block has no branches"
    out = []
    for branch in branches:
        parsed, error = _parse_branch(branch)
        if error:
            return None, error
        out.append(parsed)
    return out, None


def _hms_seconds(hms) -> int:
    hour, minute, second = hms
    return hour * 3600 + minute * 60 + second


def plan_for(config):
    """The config, read into something replayable: (plan, None) or (None, error).

    Pure, and the whole subset gate. The plan it returns names the two roles
    this module can score — the branch that cuts the supply behind a guard, and
    the branch that restores it — because the SAVING is arithmetic that only
    means anything once those two are identified. A config parsing cleanly into
    branches nobody can pair that way is refused here rather than scored as
    something it is not.

    It reports what the config SAYS and judges nothing about which shape that
    makes it: `days` is the weekdays the cut is restricted to (every day when
    it is not restricted at all) and `restricted` is whether the restriction
    was written. `run` is where those are checked against the kind it was told,
    for the reason `entity_id` is checked there rather than here — this is the
    parser, and core/verify.py calls it to compare two configs with no kind in
    its hand at all.
    """
    if not isinstance(config, dict) or not config:
        return None, "there is no config to replay"
    unknown = _unknown_keys(config, _ALLOWED_TOP_LEVEL)
    if unknown:
        return None, f"the config carries {', '.join(unknown)}, which is not replayed"

    mode = config.get("mode", _ALLOWED_MODE)
    if mode != _ALLOWED_MODE:
        return None, f"no replay is defined for mode {mode!r}"

    # The top-level condition list must be EMPTY. Not "is ignored if empty" —
    # a non-empty one gates every branch, including the one that switches the
    # device back on, and a rule that can leave a device off all day is not a
    # rule whose saving this may compute. builder.py writes [] on purpose and
    # explains why; anything else came from somewhere this cannot vouch for.
    condition = config.get("condition", [])
    if not isinstance(condition, list):
        return None, "the top-level condition is not a list"
    if condition:
        return None, "a top-level condition is not replayed"

    triggers = config.get("trigger")
    if not isinstance(triggers, list) or not triggers:
        return None, "the config has no triggers"
    by_id = {}
    for trigger in triggers:
        spec, error = _parse_trigger(trigger)
        if error:
            return None, error
        if spec["id"] in by_id:
            return None, f"two triggers share the id {spec['id']!r}"
        by_id[spec["id"]] = spec

    branches, error = _parse_action(config.get("action"))
    if error:
        return None, error
    for branch in branches:
        if branch["trigger_id"] not in by_id:
            return None, (f"a choose branch matches trigger {branch['trigger_id']!r}, "
                          f"which no trigger declares")

    cutting = [b for b in branches if all(e["effect"] == "off" for e in b["effects"])]
    restoring = [b for b in branches if all(e["effect"] == "on" for e in b["effects"])]
    if len(cutting) != 1 or len(restoring) != 1 or len(branches) != 2:
        return None, ("this replay scores one branch that cuts a supply and one that "
                      "restores it, and the config does not have exactly those two")
    cut, restore = cutting[0], restoring[0]

    switches = {e["entity_id"] for e in cut["effects"]} | {e["entity_id"] for e in restore["effects"]}
    if len(switches) != 1:
        return None, "the two branches do not act on the same entity"
    switch = switches.pop()

    if len(cut["guards"]) != 1:
        # No guard at all would be a rule that cuts power every night whatever
        # the device is doing, and that is not the rule the estimate describes.
        return None, "the cutting branch does not carry exactly one guard"
    guard = cut["guards"][0]
    if restore["guards"] or restore["days"]:
        # A guard on the restore is how a device stays off all day. builder.py
        # says so where it writes the branch unconditionally — and a weekday
        # restriction there is the same failure one weekday further on: a
        # device cut on Friday would stay cut until Monday. One refusal covers
        # both, because what it is protecting is one thing.
        return None, "the restoring branch is guarded, so the supply may not come back"

    if len(cut["days"]) > 1:
        # Two weekday conditions on one branch intersect, and intersecting them
        # here would be this module deciding what a config it did not build
        # meant. Refused, never approximated — `_TRIGGERS` states the rule.
        return None, "the cutting branch carries more than one weekday restriction"

    cut_trigger = by_id[cut["trigger_id"]]
    restore_trigger = by_id[restore["trigger_id"]]
    if cut_trigger["platform"] != "time" or restore_trigger["platform"] != "time":
        return None, "this replay pairs two time triggers, and these are not both time triggers"
    nominal = _hms_seconds(restore_trigger["hms"]) - _hms_seconds(cut_trigger["hms"])
    if nominal <= 0:
        # The supply would be restored before it was cut, so the pairing that
        # bounds the span — and therefore the saving — does not hold. Refused
        # rather than wrapped to the next day, which would be this module
        # inventing a rule the config does not state.
        return None, "the supply is restored before it is cut, so no span can be bounded"

    return {
        "cut_trigger": cut_trigger,
        "restore_trigger": restore_trigger,
        "guard": guard,
        "switch": switch,
        "nominal_span": nominal,
        # EVERY_DAY rather than None when nothing restricts the cut, so that
        # the date filter in `run` is one expression for both shapes and the
        # night shape's behaviour is unchanged by construction rather than by a
        # branch somebody has to keep true. Whether the restriction was WRITTEN
        # is the separate flag, because that is the structural fact the kind is
        # checked against and a seven-day restriction is not the same config as
        # no restriction at all.
        "days": cut["days"][0] if cut["days"] else window.EVERY_DAY,
        "restricted": bool(cut["days"]),
    }, None


# --------------------------------------------------------------------------
# the two shapes: what differs between them, in one table
# --------------------------------------------------------------------------
# The blob's own field names, per kind. THE FIELD NAMES in the module docstring
# is the argument: the names on the left are what this module calls the four
# counts, the hours, the projection, the per-date detail and the floor, and the
# names on the right are what each shape writes them out as. A schedule blob
# speaks in occurrences because "nights" is a lie about 11:00 on a Tuesday; a
# standby blob does not move, because its names are on disk on live installs
# and three modules read them by name.
_NIGHT_FIELDS = {
    "examined": "nights_examined",
    "fired": "nights_fired",
    "guard_failed": "nights_guard_failed",
    "wrong": "nights_wrong",
    "saved": "nights_saved",
    "per_month": "nights_per_month",
    "hours": "hours_per_night",
    "detail": "nights",
    "min_saved": "min_saved_nights",
}
_OCCURRENCE_FIELDS = {
    "examined": "occurrences_examined",
    "fired": "occurrences_fired",
    "guard_failed": "occurrences_guard_failed",
    "wrong": "occurrences_wrong",
    "saved": "occurrences_saved",
    "per_month": "occurrences_per_month",
    "hours": "hours_per_occurrence",
    "detail": "occurrences",
    "min_saved": "min_saved_occurrences",
}

# ...and the detector's own names for the four figures the ESTIMATE carries, so
# that `_estimate` can fall back to the measurement whole. The night map is not
# an identity because standby.py calls the hours `hours_per_day` — a plateau is
# a fact about a device's day — while the estimate calls them `hours_per_night`,
# a fact about one firing. The schedule map IS an identity, and that is not
# laziness either: schedule.py's blob had never been written when WP3b chose
# its vocabulary, so both sides were free to agree, and they did.
_NIGHT_DETECTOR_FIELDS = {
    "kwh_per_month": "kwh_per_month",
    "watts": "watts",
    "hours_per_night": "hours_per_day",
    "nights_per_month": "nights_per_month",
}
_SCHEDULE_DETECTOR_FIELDS = {
    "kwh_per_month": "kwh_per_month",
    "watts": "watts",
    "hours_per_occurrence": "hours_per_occurrence",
    "occurrences_per_month": "occurrences_per_month",
}


def _night_ceiling(plan, scale: float) -> float:
    """The level that means a device is IN USE, for a night rule: the global
    one, in WATTS.

    `standby.STANDBY_MAX_WATTS` is a claim about every device in the house, and
    it is deliberately not read off the plan even though builder.py derived the
    guard from it and the two are the same number. The guard is a threshold the
    rule compares at one instant; this is what "in use" MEANS to the detector
    that raised the candidate, and a config edited to guard at a different
    level has not changed the detector's mind about that.
    """
    return standby.STANDBY_MAX_WATTS


def _schedule_ceiling(plan, scale: float) -> float:
    """...and for a schedule rule: this device's own active floor, in WATTS.

    Off the plan rather than out of a second constant, because there is no
    second constant to fetch — `builder._schedule_shape` wrote `below` from the
    candidate's `active_floor` and from nothing else, and refuses to write the
    guard at all when the evidence has lost it. `guard.below_w` in the blob is
    this same product, which is what keeps the comparison made here and the
    sentence the card prints the same number.

    RAW times scale is WATTS. The guard comparison in `_assess` is raw against
    raw and never touches this; this is watts against watts and never touches
    the guard. That is the unit line in the module docstring, at the one site
    where both numbers are in scope.
    """
    return plan["guard"]["below"] * float(scale)


def _night_series(plan, rows, start, end, scale: float):
    """The night credit check reads the rows it is handed, so there is nothing
    to prepare once for the window."""
    return None


def _night_credit(prepared, rows, start, end, scale: float):
    """Was this night readable and a plateau throughout: (watts, None) or
    (None, reason).

    `standby.assess_night`, called rather than reimplemented — a night this
    credits and the detector excluded would be two components describing one
    night differently in one suggestion. WATTS come back, because assess_night
    has already applied the scale.
    """
    verdict = standby.assess_night(rows, start, end, scale=scale)
    if not verdict["qualifies"]:
        return None, verdict["reason"]
    return verdict["watts"], None


def _schedule_series(plan, rows, start, end, scale: float):
    """The whole window as schedule.py's intervals, condensed ONCE.

    Once, and over the whole window rather than per occurrence, for a reason
    that is about correctness and not about cost: `schedule.intervals` marks
    its head and its tail unobserved because there is no row on the far side of
    them to read the silence against, so a series cut to one occurrence would
    report every occurrence as unreadable at both ends. Built over the window,
    the row carried in from before an occurrence is inside the series and the
    interval holding it reaches into the span — which is the same carried row
    `_reading_at` and `standby.assess_night` both read, from the other side.

    The threshold handed over is the device's own active floor in WATTS, which
    is what `intervals` compares against; it is the same product
    `_schedule_ceiling` returns, and there is no third derivation of it.
    """
    series = schedule.intervals(rows, start, end,
                                _schedule_ceiling(plan, scale), scale=scale)
    # The starts alongside, because `_cell_verdict` and `_lowest` bisect them
    # and rebuilding the list per occurrence would be ninety of them a window.
    return series, [interval.start for interval in series]


# schedule.py's cell verdicts in this module's reason vocabulary. Four of the
# five need no translation at all — WP3b gave them standby's values on purpose,
# "so that one renderer can read the `excluded` dict of either blob" — and
# ACTIVE is the one with no counterpart there. It becomes REASON_IN_USE rather
# than a fifth word because it is the same fact about a span, and §9 puts these
# in front of a person. Written out in full rather than as a one-entry patch,
# so that the correspondence is visible where it is relied on.
#
# The lookup below falls back to the verdict itself rather than refusing,
# unlike every other table here, and the difference is what is being dispatched
# on: those tables choose BEHAVIOUR for a config, where a wrong guess is a rule
# graded as something else. This chooses a WORD for an exclusion the detector
# has already made, and a verdict schedule.py adds later reaching the blob
# under its own name is the honest reading of it — the occurrence is excluded
# either way.
_SCHEDULE_REASONS = {
    schedule.ACTIVE: standby.REASON_IN_USE,
    schedule.BELOW_FLOOR: standby.REASON_BELOW_FLOOR,
    schedule.REASON_NO_COVERAGE: standby.REASON_NO_COVERAGE,
    schedule.REASON_UNAVAILABLE: standby.REASON_UNAVAILABLE,
    schedule.REASON_STATISTICS: standby.REASON_STATISTICS,
}


def _schedule_credit(prepared, rows, start, end, scale: float):
    """Was this occurrence readable and under the device's own floor
    throughout: (watts, None) or (None, reason).

    schedule.py's own answer, asked about the occurrence's span instead of
    about one of its 336 half-hours — `_cell_verdict` takes two instants and
    has never cared which two. Reaching across the underscore is the same
    deliberate reach `_reading_at` makes for `standby._gap_reason`, and for the
    same reason: a second implementation of "was this device idle across this
    stretch" is a second answer, free to differ from the one the detector's own
    evidence reports about the same window.

    Note what is NOT asked. A night has to be FLAT as well as low, because
    standby.py's claim is a plateau; this claim is that the device stayed under
    its active floor, which is what the card says and what every cell under the
    proposal was judged on. Asking for flatness here would refuse occurrences
    the evidence beside it counted.
    """
    series, starts = prepared
    verdict = schedule._cell_verdict(series, starts, start, end)
    if verdict != schedule.IDLE:
        return None, _SCHEDULE_REASONS.get(verdict, verdict)
    watts = schedule._lowest(series, starts, start, end)
    if watts is None:
        # Unreachable for a span `_cell_verdict` called idle — an idle verdict
        # means every interval touching it carried a reading. Left in rather
        # than asserted, because the failure it would otherwise become is a
        # credited occurrence with no watts in it, and the honest name for a
        # span nothing could be read across is already here.
        return None, standby.REASON_NO_COVERAGE
    return watts, None


# kind -> everything that differs between the two shapes. One table, dispatched
# the way `_TRIGGERS`, `_CONDITIONS` and `_SERVICES` are and refused the same
# way: a kind with no row here is refused by `run`, never given whichever of
# these two looks closest.
#
# `restricted` is the structural difference the kind is CHECKED against (see
# TWO SHAPES in the module docstring); everything else is what that kind is
# graded with, and every one of them comes from the detector that raised it
# rather than from a number chosen here.
_KINDS = {
    standby.KIND: {
        "restricted": False,
        "noun": "night",
        "fields": _NIGHT_FIELDS,
        "detector_fields": _NIGHT_DETECTOR_FIELDS,
        "min_saved": MIN_SAVED_NIGHTS,
        "lookback_days": standby.LOOKBACK_DAYS,
        "projection_days": standby.PROJECTION_DAYS_PER_MONTH,
        "floor_w": standby.STANDBY_MIN_WATTS,
        "max_gap_s": standby.COVERAGE_MAX_GAP_SECONDS,
        "ceiling": _night_ceiling,
        "series": _night_series,
        "credit": _night_credit,
    },
    schedule.KIND: {
        "restricted": True,
        "noun": "occurrence",
        "fields": _OCCURRENCE_FIELDS,
        "detector_fields": _SCHEDULE_DETECTOR_FIELDS,
        "min_saved": MIN_SAVED_OCCURRENCES,
        "lookback_days": schedule.LOOKBACK_DAYS,
        "projection_days": schedule.PROJECTION_DAYS_PER_MONTH,
        "floor_w": schedule.IDLE_MIN_WATTS,
        "max_gap_s": schedule.COVERAGE_MAX_GAP_SECONDS,
        "ceiling": _schedule_ceiling,
        "series": _schedule_series,
        "credit": _schedule_credit,
    },
}


def fields_for(kind):
    """What a kind's replay blob calls the counts in it, and the noun they
    count — or None for a kind with no row in the table.

    The table above, served to the one caller that has to READ a blob this
    module wrote rather than write one. core/review.py asks how often the rule
    would have been wrong (decision 2) and how many firings it would have
    saved (the rank tiebreak), and both of those are `nights_*` on one shape
    and `occurrences_*` on the other. Public, and a copy, so that the pass
    reads the grading's own vocabulary instead of holding a second table of
    it: THE FIELD NAMES above chose these words, and one answer per kind means
    one place that chose them.

    None rather than the night's row, which is the refusal `run` makes from
    this same table and for the same reason. A blob whose field a caller
    cannot read must be refused, never read as zero — reading `nights_wrong`
    off an occurrence blob is exactly the quiet failure THE FIELD NAMES names.
    """
    shape = _KINDS.get(str(kind or ""))
    if shape is None:
        return None
    return {**shape["fields"], "noun": shape["noun"]}


# --------------------------------------------------------------------------
# reading the record at an instant, and across a span
# --------------------------------------------------------------------------
def _local_date(ts):
    """The local calendar date an instant falls on."""
    return datetime.fromtimestamp(int(ts)).date()


def _reading_at(rows, instant, max_gap):
    """The sensor's state in force at an instant: (raw value, None) or
    (None, reason).

    RAW, in the units the sensor reports — this is what the guard is compared
    against and the scale must not touch it.

    The row that matters is the last one at or before the instant, because the
    record writes on change: the value in force at 01:00 is whatever was
    carried in from before it, and a reading taken only from rows sitting
    inside some window would be a reading of whatever happened to move. The
    checks are ordered as standby.assess_night orders them, so that the reason
    reported is the strongest thing true about the instant.

    `max_gap` is the KIND's, handed in rather than read off standby.py here.
    The two detectors declare the same number and declare it separately — a
    restatement schedule.py makes on purpose — so a hard-coded one would be
    this instant judged by one detector's coverage rule while the span beside
    it was judged by the other's, under the single `max_gap_s` the blob prints
    for both. The floor, the ceiling and the lookback are per kind for the
    same reason: what a candidate is judged against comes from the detector
    that raised it.
    """
    carried = None
    for row in rows:
        ts = row.get("ts")
        if ts is None:
            continue
        if int(ts) <= instant:
            carried = row
        else:
            break
    if carried is None:
        return None, standby.REASON_NO_COVERAGE
    if instant - int(carried["ts"]) > max_gap:
        # Which of the two silences this is — the agent not watching, or the
        # entity gone — is a distinction standby.py argues at length and
        # answers in one place. Reaching across the underscore is deliberate:
        # a second copy of that rule here is a second answer that can drift
        # from the one the detector's own evidence reports for the same night.
        return None, standby._gap_reason(carried)
    if str(carried.get("source") or "") == "statistics":
        return None, standby.REASON_STATISTICS
    value = observer.as_float(carried.get("state"))
    if value is None:
        # Home Assistant would have evaluated `numeric_state` false here and
        # not fired. The night is still excluded rather than counted as a
        # guard failure, because 'unavailable' says the device may have been
        # drawing anything at all — and a night that cannot be read is not
        # evidence about the device either way.
        return None, standby.REASON_UNAVAILABLE
    return value, None


def _peak_watts(rows, start, end, scale):
    """The highest draw the record SHOWS between two instants, in WATTS, or
    None if it shows nothing.

    Question 2's whole evidence. Strictly after `start`, because the reading at
    `start` is the guard and it already passed.

    Statistics rows are read here and only here. A bucket MEAN above the
    ceiling proves the underlying readings reached it, since a maximum is never
    below a mean — that is the one claim a mean supports, and this is the one
    place the module needs it. It still cannot show that a value stayed below
    anything, which is why such a row goes on blocking credit everywhere else.

    Gaps are not checked, deliberately: a spike that IS visible is proof
    whatever else the night is missing. The asymmetry runs the other way and is
    handled by the caller, which credits a night only when its span was whole.
    """
    peak, at = None, None
    for row in rows:
        ts = row.get("ts")
        if ts is None:
            continue
        ts = int(ts)
        if ts <= start:
            continue
        if ts >= end:
            break
        # raw -> watts, because the ceiling it is about to be compared against
        # is stated in watts.
        value = observer.as_float(row.get("state"))
        if value is None:
            continue
        watts = value * scale
        if peak is None or watts > peak:
            peak, at = watts, ts
    return peak, at


# --------------------------------------------------------------------------
# one night
# --------------------------------------------------------------------------
def _assess(plan, graded, rows, day, scale):
    """One local date's verdict: which outcome, why, and what it is worth.

    Returns the per-night dict that ends up in the blob. The trigger instants
    are resolved from the calendar rather than looked for in the record,
    because a time trigger fires whatever the sensor was doing.

    `graded` is what the shape decided once for the whole window — the in-use
    ceiling in watts, the coverage rule, the credit check, and whatever that
    check needed preparing. Handed down rather than looked up per date, so that the ceiling
    a hundred occurrences are compared against is provably one number and the
    interval series ninety of them are read off is provably one series.

    The date is already known to be one the rule names: `run` filters on the
    weekdays before it gets here, because a date the rule never ran on has no
    verdict to give and must not enter the denominator. See WHICH DATES ARE
    OCCURRENCES in the module docstring.
    """
    cut_at = _instant_on(plan["cut_trigger"], day)
    restore_at = _instant_on(plan["restore_trigger"], day)
    if cut_at is None or restore_at is None:
        return _outcome(day, OUTCOME_NO_TRIGGER, REASON_NO_TRIGGER)

    # DST decision 3: the elapsed span, capped at what the clock times describe.
    span = min(restore_at - cut_at, plan["nominal_span"])

    # RAW against RAW. `below` came out of the config in the sensor's reported
    # units and `reading` is the sensor's reported state; applying `scale` to
    # either is the factor of a thousand this module's docstring is about.
    reading, reason = _reading_at(rows, cut_at, graded["max_gap_s"])
    if reason:
        return _outcome(day, OUTCOME_UNREADABLE, reason)
    if reading >= plan["guard"]["below"]:
        # The device was doing something at the cutting instant, so the rule
        # would have left it alone. standby's own word for that state.
        return _outcome(day, OUTCOME_NOT_FIRED, standby.REASON_IN_USE)

    # It would have fired. Everything from here is about what that meant.
    #
    # WATTS against WATTS. `_peak_watts` has applied the scale and the ceiling
    # is in watts whichever shape supplied it — the global standby level for a
    # night, this device's own active floor for a schedule.
    peak, peak_at = _peak_watts(rows, cut_at, restore_at, scale)
    if peak is not None and peak > graded["ceiling_w"]:
        return _outcome(day, OUTCOME_WRONG, REASON_USED_LATER,
                        peak_w=round(peak, 1), peak_at=_local_time(peak_at))

    # No proof of use. That is not proof there was none — see the asymmetry in
    # the module docstring — so the occurrence is credited only if its whole
    # span was readable and idle throughout, which is exactly the judgement the
    # detector that raised the candidate already makes. Reused rather than
    # reimplemented, and reused per SHAPE: a span this credits and the detector
    # excluded, or the reverse, would be two components describing one window
    # differently in the same suggestion.
    watts, reason = graded["credit"](graded["prepared"], rows,
                                     cut_at, restore_at, scale)
    if reason:
        return _outcome(day, OUTCOME_NO_CREDIT, reason)
    # WATTS from here: the credit check has already applied the scale, and what
    # follows is multiplied into kWh and printed for a person.
    return _outcome(day, OUTCOME_SAVED, None, watts=watts, hours=span / 3600.0)


def _outcome(day, outcome, reason, watts=None, hours=None, peak_w=None, peak_at=None):
    night = {"date": day.isoformat(), "outcome": outcome}
    if reason:
        night["reason"] = reason
    if watts is not None:
        night["watts"] = round(watts, 1)
    if hours is not None:
        night["hours"] = round(hours, 2)
    if peak_w is not None:
        night["peak_w"] = peak_w
    if peak_at is not None:
        night["peak_at"] = peak_at
    return night


def _hours_window(plan) -> str:
    """The hours this rule was graded over, as one phrase.

    The config's own two trigger times rather than a detector's window, because
    the config is what was graded — and the weekdays when the config names
    them, for the same reason: "07:00:00-17:00:00 local" beside a rule that
    only runs Monday to Friday claims more hours than were measured.

    Shaped like `window.label` and rendered with its `days_phrase`, so the two
    sentences agree about how a household says "Mon-Fri". It is not that label
    and must not become it: these are the trigger times to the second, and
    core/verify.py compares `window.label`'s output literally across a week.
    """
    hours = f"{plan['cut_trigger']['at']}-{plan['restore_trigger']['at']} local"
    if not plan["restricted"]:
        return hours
    return f"{hours} {window.days_phrase(plan['days'])}"


def _local_time(ts) -> str:
    """An instant as the household's clock read it — what a sentence needs."""
    return time.strftime("%H:%M", time.localtime(int(ts))) if ts else ""


def _median(values):
    """The middle value, averaging the two middle ones on an even count."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


# --------------------------------------------------------------------------
# the whole judgement
# --------------------------------------------------------------------------
def run(config, entity_id, rows, kind, scale: float = 1.0, evidence: dict = None,
        now=None, lookback_days: int = None):
    """Replays a config over one entity's series: (result, None) or (None, error).

    `result` is {"replay": ..., "estimate": ...} — the two blobs
    suggestions.mark_validated takes, built here so the caller never
    re-derives a number from the other. `error` is a plain sentence, and it
    means the candidate is DISCARDED: an unsupported config, a series belonging
    to the wrong entity, and evidence below the floor are all refusals, because
    §7.1 gives a candidate exactly two fates and "partly evaluated" is not one.

    `entity_id` is whose transitions `rows` are, and it is required: it is
    checked against the entity the config actually guards, and a mismatch is
    the one way this could grade a real rule against a real series and be
    describing neither.

    `kind` is which detector raised the candidate, and it is required for the
    same reason and checked the same way — against the config it claims to
    describe. It decides every number that differs between the two shapes: the
    window read back, the floor the credited count is compared against, the
    level that means the device is in use, what counts as an idle span, and
    what the blob's fields are called. See TWO SHAPES and THE FIELD NAMES in
    the module docstring.

    `rows` are that entity's transitions, ascending — the same read the
    detector was judged on. They are re-sorted and clipped to the window
    derived here from `now` and `lookback_days`, for the reason
    standby.find_candidate gives: a caller handing over more history than it
    asked to be judged would otherwise have the counts below satisfied by rows
    no night in the window ever looked at.

    `scale` is watts per reported unit (standby.power_scale's answer for this
    sensor). `evidence` is the detector's own evidence blob, and the figures it
    already computed are what the estimate falls back to if the replay's comes
    out higher.

    `lookback_days` defaults to the KIND'S detector's, never to one number —
    thirty days for a night and ninety for a schedule. HOW FAR BACK in the
    module docstring is why.
    """
    shape = _KINDS.get(kind)
    if shape is None:
        # A kind with no row in the table is refused rather than given
        # whichever of the two shapes looks closest — core/rollback.py's rule
        # about an operation the table does not know, and builder.py makes the
        # same refusal from the other end for the same candidate.
        return None, f"no replay is defined for a {kind!r} candidate"

    plan, error = plan_for(config)
    if error:
        return None, error

    if plan["restricted"] != shape["restricted"]:
        # The one structural difference between the shapes, checked against
        # what the builder says it built. A night rule somebody edited a
        # `condition: time` into would otherwise be graded over the five days
        # that condition names rather than the seven it fires on, and a
        # schedule rule whose restriction was edited away would be graded over
        # two days it never ran on. Both are a refusal, not a best guess.
        says = ("restricts its cut to named weekdays" if plan["restricted"]
                else "cuts on every day of the week")
        return None, (f"this is a {kind} candidate and the config {says}, which is not "
                      f"the shape a {kind} rule has — grading one as the other would "
                      f"count occurrences on dates the rule never ran")

    if not entity_id or str(entity_id) != plan["guard"]["entity_id"]:
        return None, (f"the series handed in is {entity_id or 'nothing'}, but the config "
                      f"guards {plan['guard']['entity_id']} — replaying one against the "
                      f"other would grade a rule nobody wrote")

    try:
        scale = float(scale)
    except (TypeError, ValueError):
        return None, "the sensor's unit scale is unknown, so nothing can be converted to watts"
    if not math.isfinite(scale) or scale <= 0:
        # isfinite as well as positive, for the reason builder.py states where
        # it writes the threshold: NaN compares False against every operator
        # and would sail through a bare `<= 0`, and every watt figure below
        # would then be NaN in a blob a person is shown.
        return None, "the sensor's unit scale is unknown, so nothing can be converted to watts"

    lookback_days = shape["lookback_days"] if lookback_days is None else lookback_days
    end = int(now) if now else int(time.time())
    start = end - int(lookback_days) * 86400
    rows = sorted(
        (r for r in (rows or [])
         if r.get("ts") is not None and start <= int(r["ts"]) < end),
        key=lambda r: int(r["ts"]))

    # Whatever the shape's credit check needs decided once for the whole
    # window rather than once per date — see `_assess`.
    graded = {
        "ceiling_w": shape["ceiling"](plan, scale),
        "max_gap_s": shape["max_gap_s"],
        "credit": shape["credit"],
        "prepared": shape["series"](plan, rows, start, end, scale),
    }

    nights = []
    day = _local_date(start)
    last = _local_date(end)
    while day <= last:
        if day.weekday() not in plan["days"]:
            # Not an occurrence the rule declined — not an occurrence. Home
            # Assistant evaluates the weekday condition before the guard is
            # reached, so there is nothing here the rule could have got right
            # or wrong, and a Saturday counted as examined under a Mon-Fri rule
            # is a denominator a third too large. Skipped BEFORE the instants
            # are placed, which is the order DST decision 4 states. For a rule
            # that names every day this is never taken.
            day += timedelta(days=1)
            continue
        # Placed by whichever instants exist, so that a night the clock
        # skipped over is still counted as EXAMINED rather than dropping out of
        # the denominator — which is how a DST night would come to read as a
        # night on which nothing happened. A date neither trigger can be placed
        # on cannot be located in the window at all and is left out.
        bounds = [t for t in (_instant_on(plan["cut_trigger"], day),
                              _instant_on(plan["restore_trigger"], day))
                  if t is not None]
        # Whole nights only. A night half inside the window is a shorter
        # observation dressed up as a full one — window.windows' rule, applied
        # here to the config's own two instants rather than to a spec, because
        # what is being placed is the automation that was written and not the
        # window it came from — and the arithmetic below assumes the entire
        # span was available to look at.
        if bounds and start <= min(bounds) and max(bounds) <= end:
            nights.append(_assess(plan, graded, rows, day, scale))
        day += timedelta(days=1)

    if not nights:
        return None, (f"no whole {shape['noun']} of the automation's own hours falls "
                      f"inside the window")

    # Counted per OUTCOME, never rolled into one flat tally. The same reason
    # string legitimately arrives from two different questions — a night the
    # guard failed and a night whose span standby.assess_night rejected are
    # both REASON_IN_USE, because the detector's carried row can carry a value
    # from before the cutting instant into its judgement of the span — and a
    # flat `{in_use: 1}` beside `nights_guard_failed: 0` is unreadable in
    # exactly the place §9 wants it read. It is also what REASON_USED_LATER was
    # introduced to prevent one level down, so undoing it here would be
    # pointless.
    outcomes, reasons = {}, {}
    for night in nights:
        outcome = night["outcome"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        reason = night.get("reason")
        if reason:
            by_reason = reasons.setdefault(outcome, {})
            by_reason[reason] = by_reason.get(reason, 0) + 1

    saved = [n for n in nights if n["outcome"] == OUTCOME_SAVED]
    fired = sum(outcomes.get(o, 0) for o in _FIRED)
    wrong = outcomes.get(OUTCOME_WRONG, 0)

    if len(saved) < shape["min_saved"]:
        return None, (f"only {len(saved)} of {len(nights)} {shape['noun']}s would have "
                      f"been saved, below the floor of {shape['min_saved']} "
                      f"(outcomes: {_phrase(outcomes)})")

    # Both medians, and both for the reason standby.py gives for using one: a
    # single unusual night cannot move it. The same statistic as the detector's
    # deliberately, so that the two figures differ only in WHICH nights counted
    # — which is the thing replay is for — rather than in how they were added up.
    watts = round(_median([n["watts"] for n in saved]), 1)
    hours = round(_median([n["hours"] for n in saved]), 2)
    # `len(saved) / len(nights)` is the rate this rule was credited at, as
    # before. The weekday fraction beside it is what keeps a Mon-Fri window at
    # about 21 occurrences a month rather than 30, and it is
    # `schedule.occurrences_per_month`'s own formula — reproduced rather than
    # called for the reason the date loop above gives, that what is being
    # projected here is the CONFIG's occurrences and that function takes a
    # spec. For a rule naming all seven days the fraction is 1 and this is the
    # arithmetic it always was.
    fields = shape["fields"]
    per_month = (len(saved) / len(nights) * shape["projection_days"]
                 * len(plan["days"]) / 7.0)
    replayed = {
        "kwh_per_month": round(watts * hours * per_month / 1000.0, 2),
        "watts": watts,
        fields["hours"]: hours,
        fields["per_month"]: round(per_month, 1),
    }
    estimate = _estimate(replayed, evidence, shape["detector_fields"])
    bounded = estimate["basis"] == "detector"

    blob = {
        # What the rule is, restated from the config rather than from the
        # candidate, because the config is what was graded.
        "guard": {
            "entity": plan["guard"]["entity_id"],
            # Both units, side by side and labelled. This pair is the artefact
            # that would have caught the kilowatt bug on sight: `below` is what
            # Home Assistant compares against the raw reading, `below_w` is the
            # same threshold in the unit a person has.
            "below": plan["guard"]["below"],
            "below_w": round(plan["guard"]["below"] * scale, 1),
        },
        "switch": plan["switch"],
        "hours_window": _hours_window(plan),
        "window": {
            "from": _local_date(start).isoformat(),
            "to": _local_date(end).isoformat(),
            "days": int(lookback_days),
        },
        # Question 1.
        fields["examined"]: len(nights),
        fields["fired"]: fired,
        fields["guard_failed"]: outcomes.get(OUTCOME_NOT_FIRED, 0),
        # Question 2. `would_have_interrupted` is a fact, not a policy: how many
        # such nights disqualify a candidate is the review pass's call, and this
        # reports rather than enforces it.
        fields["wrong"]: wrong,
        "would_have_interrupted": bool(wrong),
        # Question 3. These four are what the REPLAY measured and always
        # multiply out together, whatever the estimate beside them ends up
        # reporting — `bounded_by_detector` is what says the two differ, and
        # `estimate` is the one to render.
        fields["saved"]: len(saved),
        "watts": watts,
        fields["hours"]: hours,
        fields["per_month"]: replayed[fields["per_month"]],
        "kwh_per_month": replayed["kwh_per_month"],
        "bounded_by_detector": bounded,
        # Every night that did not count, and why. §9's rule: "14 of 30" with
        # nothing said about the other sixteen is the number without the
        # observation.
        "outcomes": outcomes,
        "reasons": reasons,
        fields["detail"]: nights,
        # The rules that produced the numbers above, each of them the kind's
        # own: the level that meant this device was in use, the level under
        # which there was no standing draw to avoid, the floor the credited
        # count had to clear, and the silence that stops being a flat reading.
        # `ceiling_w` is the one that moves per DEVICE rather than per kind —
        # it is `guard.below_w` for a schedule — so it is read back off the
        # same value the grading used rather than restated from a constant.
        "thresholds": {
            "ceiling_w": round(graded["ceiling_w"], 1),
            "floor_w": shape["floor_w"],
            fields["min_saved"]: shape["min_saved"],
            "max_gap_s": graded["max_gap_s"],
        },
    }

    return {"replay": blob, "estimate": estimate}, None


def _estimate(replayed: dict, evidence, detector_fields: dict) -> dict:
    """The figure to report, taken WHOLE from one side: the replay's or the
    detector's.

    §7.1's rule is that replay may only tighten. If the recomputed figure comes
    out larger, the two components disagree about the same house and the honest
    response is to report the smaller and say so rather than to pick the nicer
    number — and to log it, because a persistent gap there is a defect in one
    of them and the log is where anyone would go looking.

    Whole is the part that is easy to get wrong. Clamping the kWh alone and
    leaving the three numbers beside it at the replay's values produces an
    estimate that is not a calculation: §9 renders it as one sentence, and "12 W
    for four hours on thirty nights, which is roughly 0.5 kWh a month" is
    arithmetic the reader can do in their head and find wrong. So when the
    detector's figure is the one being reported, its breakdown comes with it. A
    component the evidence does not carry is left out rather than borrowed back
    from the replay, because a borrowed one would put the inconsistency straight
    back in.

    `detector_fields` is the KIND's map from the four names the estimate uses
    to the names that detector's evidence blob spells them with — handed in
    rather than looked up, because this function never learns which shape it is
    reporting for and does not need to.

    Which side is suspect is worth naming rather than leaving to be guessed. The
    detector's figure is a median over its qualifying nights and the replay's is
    a median over a subset of them, so a replay coming out higher means either
    that the config's hours are not the hours that were measured — the case this
    whole module exists to catch — or that the subset dropped the low nights and
    kept the high ones. Neither is a reason to raise the claim.
    """
    # `basis` travels with the number rather than being re-derived: a
    # verification report (§7.2) comparing a measured week against an estimate
    # needs to know whose estimate it was.
    if not isinstance(evidence, dict):
        return {**replayed, "basis": "replay"}
    detector = observer.as_float(evidence.get("kwh_per_month"))
    if detector is None or replayed["kwh_per_month"] <= detector:
        return {**replayed, "basis": "replay"}

    logger.warning(
        f"REPLAY: replayed saving {replayed['kwh_per_month']} kWh/month exceeds the "
        f"detector's {detector} — the config's hours or the nights it credited disagree "
        f"with the measurement, and the detector's figures are the ones being reported")
    estimate = {"basis": "detector"}
    for name, field in detector_fields.items():
        value = observer.as_float(evidence.get(field))
        if value is not None:
            estimate[name] = round(value, 2)
    return estimate


def _phrase(outcomes: dict) -> str:
    """The outcome counts as one readable clause, for a refusal a human reads
    in a log line."""
    return ", ".join(f"{name} {count}" for name, count in sorted(outcomes.items()))
