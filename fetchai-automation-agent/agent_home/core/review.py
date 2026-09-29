"""The review pass: the daily walk from a watched power sensor to one question.

Design §7 stages 3 to 5 — reason, validate, propose — and the caller
core/detectors/, core/builder.py and core/replay.py were each written against
by name. They were built bottom-up so that each could be argued with on its
own, and until this module none of them was reachable from the running agent
at all:

    for each detector, over ITS window   standby.scan, schedule.scan
      is this waste?                     <detector>.find_candidate
      record it                          suggestions.upsert_candidate
      what change would fix it?          builder.build
      would that rule have worked?       replay.run — over THE SAME rows
      record the grade                   suggestions.mark_validated
    rank across every kind, and pick one
      put it to a person                 gate.propose
      record the question                suggestions.mark_proposed

Split the way core/observer.py is split from core/observation.py, and for the
concrete reason WP4 gives rather than a stylistic one: everything this module
DECIDES is a pure function over rows and rows a test can state as a literal,
`run_once` is the only thing here that touches a store or Home Assistant, and
tasks/review.py only registers the interval. The evidence floors downstream are
fourteen days and ten nights, so a judgement reachable only through a database
would be a judgement nothing in this repo can exercise.

No model, anywhere. §7.1 draws the line — "the model proposes candidates; it
does not get to grade them" — and this is the thing that would be most tempting
to ask an LLM to do: pick the best suggestion, decide whether the evidence
moved, decide whether one bad night is too many. Every one of those is a
threshold below with its argument written next to it, because the answer a
model gives is one nobody can reproduce, argue with, or explain the change in
from yesterday. This module does not import core/agent_runtime.py.
Nor core/analyst.py or core/models.py (proposal D-1): an analyst row reaches
this pass as a row in the store, ranked and withdrawn by the rules below — its
chart spec's entities are read off the row, nothing is recounted or asked.

And not transitively either, which is a decision rather than an accident.
Proposal §7.1 warned this line would become transitively false once
`schedule.scan` could call a model on the pass's behalf. §7.8 answered D-2 no:
the model proposer is not admitted, and the schedule detector picks its window
from `schedule.backed_windows`, a deterministic scan that did everything the
model did in §7.7's reading. Nothing `run_once` reaches calls a model.


THE SEVEN DECISIONS, AND WHAT EACH IS SETTLED AS
------------------------------------------------
Each of these was deferred here explicitly by a module that refused to settle
it alone. They are stated together because they are one design; each is also
stated again at the code that makes it.

1. EVIDENCE DRIFT — what counts as "the evidence changed".
   The detector's window slides daily, so the blob moves every morning, and
   `upsert_candidate` treats moved evidence as grounds to clear a replay. Left
   alone nothing would ever stay `validated` long enough to be proposed. The
   fix is `evidence_fingerprint`: the claim being made is the KIND of claim,
   the device, the switch, the level to the watt, how often to within a few
   times a month, and the hours. Nothing else — not the window dates, not the
   coverage counts, not the histogram of excluded nights — because those move
   without the claim moving, and the frequency itself moves by one night every
   morning purely because the window slid.
   The last two of those are read through the RAISING DETECTOR'S names for
   them (decision 7), because the two shapes do not share them. Read by the
   night's names alone, a schedule blob comes back None and "" for both, the
   tuple collapses to (device, switch, watts), and the window — the one field
   that tells a night claim about a television apart from a daytime claim
   about the same television — is the half that vanished.
   When the fingerprint matches, the pass hands the STORED blob back to
   `upsert_candidate` rather than today's, so the store's own byte comparison
   sees "the same waste, described the same way" and the replay survives.
   That is not a trick to defeat the store: it is the honest answer. §9 makes
   the evidence travel with the suggestion, the replay was graded over the
   window the stored evidence names, and rewriting today's window onto a grade
   from three days ago would print a window the replay never saw.
   `EVIDENCE_HOLD_DAYS` is the other half — evidence is not held forever, or
   the sentence a user reads would describe a fortnight that has passed.

2. HOW MANY WRONG FIRINGS DISQUALIFY — `MAX_WRONG_NIGHTS`, and it is zero.
   `replay.run` reports the wrong count and `would_have_interrupted` as facts
   and enforces nothing, deliberately, because a policy hidden inside the
   grading is a policy nobody can find. Here it is. A "wrong" night is one
   where the record PROVES the rule would have cut power to a device somebody
   was about to use, and replay's own asymmetry is what makes zero the right
   line: absence of proof is not proof of absence, so the count that comes back
   is a floor rather than an estimate. Tolerating one seen means tolerating an
   unknown number unseen, and §12 says the number that matters is the rollback
   rate.

3. CREATE OR UPDATE — `PROPOSAL_TOOL`, and it is create.
   builder.py names the trap: proposing an update against an automation that
   does not exist makes `gate._inverse_for` journal the change `one_way`, which
   is a reversible change advertised as irreversible and, worse, one nothing
   can roll back. Home Assistant's config endpoint is the same POST either way,
   so the update would have SUCCEEDED and left that lie in the audit trail. So
   the pass proposes a creation, and the stable id `builder.automation_id_for`
   computes is simply not used: `tool_ha_create_automation` mints Home
   Assistant's own, `gate._created_inverse` reads it back out, and the delete
   that undoes the change names the id the home actually carries. Nothing needs
   the stable one — dedupe is the suggestions index, and rollback goes through
   the journal — so the alternative (teaching the create tool an id argument,
   which is a change to what the model is shown) buys nothing this pass wants.

4. WHICH SERIES REPLAY GETS — the one the detector was judged on, handed over.
   Each detector's `scan` returns its candidates' rows with them and records
   nothing itself, so the config is graded against exactly the series the
   measurement printed beside it came from. The pass never reads the record
   for a candidate twice: a second read is a different window, and grading
   against it would mean the number shown and the number graded describe
   different nights. Two DETECTORS do read the same entity twice — decision 6
   — but never the same candidate, and never through this pass's own hand.

5. CADENCE, RANK AND VOLUME — daily after `REVIEW_HOUR`, best first, one new
   question a day, never more than `MAX_OPEN_PROPOSALS` open at once, and
   never two about one device. All four are limits across every detector
   rather than within one: §9's digest is three lines whoever raised them, so
   a fourth open proposal is one the digest cannot show, and a question nobody
   can see is a question that should not have been asked yet. Ranking is by
   the ESTIMATE, because the estimate is the claim being made rather than the
   raw measurement behind it, and `kwh_per_month` is the one figure both
   shapes write under one name — a night's kilowatt hours and a schedule's are
   the same quantity, which is what makes one ordering over two kinds honest.
   "Never two about one device" used to be structural and is not any more.
   The store's partial unique index makes one live row per KEY and the key is
   `kind:entity`, so `standby:sensor.tv` and `schedule:sensor.tv` are two
   rows — a real pair on a television with a night plateau and a daytime one.
   `claimed_entities` is what restores it, and it is stated as three states
   rather than as a count, because the reason is not volume: two questions are
   two things one word could answer, two applied rules cut one switch, and
   §7.2 measures each of them by subtracting two weeks of the SAME sensor.
   And the withdrawal rule `gate.propose` is counting on is here, because the
   session TTL deliberately will not expire an agent-raised proposal: a
   proposal is withdrawn when its evidence stops overlapping today — a
   question asked about a window that no longer touches the present is
   re-asked with a fresh number, rather than left standing. That age is the
   RAISING DETECTOR'S own lookback, thirty days for a night and ninety for a
   schedule, since it is the point at which that detector's window no longer
   touches now. §11's case is withdrawn faster: an entity the agent no longer
   watches is one it cannot measure anything about, so the suggestion goes
   with it. Withdrawing is `retire`, never `dismiss`: a retired key sits
   outside that unique index, so the same waste can be raised again, while a
   dismissal would be a lie about who said no and §12 counts dismissals.

   Analyst rows are the exception to "across every detector", and only to the
   volume half of it (free-hand F-12, F-WP9). Under one set of limits an idea
   with no estimate sorted behind every detector row that claimed a saving and
   reached a person about a week late, one a day. So the analyst's rows take
   none of the daily slot and none of `MAX_OPEN_PROPOSALS`: they are put to the
   household once a week, on `settings.DIGEST_DAY`, as a digest topped up to
   `DIGEST_SIZE` open — `digest_due` / `digest_allowed`, pure, beside
   `proposals_allowed`. "Never two about one device" is NOT split: a device is
   one device whoever noticed it, so `claimed_entities` is read over both
   sources, and the digest runs before the daily proposal because a blocked
   analyst idea waits a week where a blocked detector row waits a day.

6. COST AND LOCKING — thirty days AND ninety, times every watched power
   sensor, daily, against a store the observation thread is writing to
   constantly. `record.between` opens no transaction and this pass opens none
   either: the rule CLAUDE.md states, and the one `scan_relations` once broke
   by holding the write lock across a whole-table read. Everything this pass
   writes goes to activity.db — a different file from record.db — so a
   suggestion upsert cannot block an observation even in principle. The record
   is READ ONLY here.
   Two detectors means two reads of every watched sensor per day, and that is
   deliberate rather than tolerated. Each detector's `scan` is the whole of
   its own I/O and derives its own window, so a shared read would have to be
   the longest one sliced by this module — which is this module choosing how
   far back a detector looks, the thing decision 7 exists to stop. What the
   second read costs is one more bounded `record.between` per sensor; what it
   does not cost is memory, because each scan drops the series of every entity
   that produced no candidate where it read it, and the two scans run one
   after the other rather than at once. The traffic to Home Assistant does not
   double either: ONE registry round trip serves every detector and the
   verification, and the preflights are per candidate.
   The whole pass runs in an executor thread; see tasks/review.py.

7. WHERE THE PER-KIND ANSWER LIVES — on the detector, and there is no table
   here. core/replay.py holds one (`_KINDS`: how to GRADE a config of this
   kind) and core/verify.py holds one (how to MEASURE a week of it), and a
   third one here would be a third place for a detector added tomorrow to be
   half-registered. So this module holds `DETECTORS`, a tuple of MODULES, and
   asks each of them the four things it needs: its kind, how far back it looks
   (`LOOKBACK_DAYS`), the scan itself, and what the blob IT wrote calls the
   two fields the fingerprint reads (`EVIDENCE_FIELDS`). Every one of those is
   a fact about the detector and was already stated in the detector's own
   module — replay's table reads `standby.LOOKBACK_DAYS` off it by name for
   exactly this reason. The fifth thing, what the GRADING calls its counts, is
   replay's own vocabulary and is asked of replay (`replay.fields_for`) rather
   than restated.
   The rule that goes with it: a blob whose field this cannot read is REFUSED,
   never read as zero. Reading an absent `nights_wrong` as zero passes every
   candidate the record says would have cut power to a device in use, and
   reading an absent window as "" makes two different claims fingerprint
   alike. So `evidence_fingerprint` answers None for a kind with no detector
   (a partial tuple would compare EQUAL to another partial tuple, which is the
   one answer that must not be reached by failing to look), `disqualified`
   refuses a kind replay cannot name the fields of, `rank` sorts a grading it
   cannot read after every one it can, and an unknown kind's evidence is aged
   against the LONGEST lookback any detector has rather than against zero.


THE SEAM AT THE OTHER END
-------------------------
`settle` is the link core/gate.py deliberately does not make. The gate does not
know what a suggestion is and must not learn — it hands back a change_id and
whatever raised the change links its own record to it. So when an agent-raised
change is answered, the answer comes back here: applied, and the suggestion
gets the baseline §7.2's verification will be measured against; rejected, and
it is a dismissal with a cooldown rather than a verdict; anything else, and it
is retired so tomorrow can ask again with a number that is true.

What `settle` reads is the JOURNAL ROW, not the tool result. `gate.apply` has
already decided whether the write landed and written that decision down, and a
second reading of the same string here could disagree with it.

The baseline is captured at APPLY time and not at propose time, which is what
`suggestions.mark_applied`'s own docstring asks for: from the moment the
automation is written the record is being changed by it, so a "before"
computed a week later would be reading the after. Its window ENDS when the
change landed rather than when this ran — the two differ only when `_reconcile`
gets here late — and it is measured over `BASELINE_DAYS`, which is
`VERIFY_AFTER_DAYS` and not the detector's thirty, because §7.2's instruction
is to compare like periods and a month before against a week after is not one.

For a rule that happens on the weekdays it names, a week of it is not seven
days: the length is `verify.days_for`, whatever offers the same number of
OCCURRENCES. `settle` reads that figure ONCE and uses it for both the
baseline's window and the week to wait, so the two ends of one comparison
cannot disagree about how long a week is.

Those numbers, and the measuring itself, are core/verify.py's. The "before" and
the "after" have to be one statistic at one scale over one length of week or
the subtraction between them means nothing, and the way to make that true is
for both to be the same function call rather than for this module to promise
it.


AND STAGE 7, WHICH RUNS FROM HERE
---------------------------------
§7.2's verification is a seventh stage with its own judgements, and it lives in
core/verify.py for the same reason the detector, the builder and the replay
engine live in theirs: it is a thing to be argued with on its own. What is
decided HERE is only where it runs, and the answer is "in this pass".

Not a schedule of its own. It needs a week to have passed, not a tight loop,
and this pass already knows what day it is — a second interval would be a
second thing to keep in step with `REVIEW_HOUR`, a second registry round trip
against the same home, and a second place for the cost rules in decision 6 to
be got wrong. So `run_once` reads the registry once, hands that one snapshot to
the detector and to `verify.run_due`, and the verification is one more line in
the same summary.
"""
from datetime import date, timedelta
import json
import logging
import os
import threading
import time

from core import activity
from core import artefacts
from core import builder
from core import detectors
from core import gate
from core import observer
from core import replay
from core import settings
from core import suggestions
from core import verify
from core import watchlist
from core.detectors import schedule
from core.detectors import standby
from tools.tool_homeassistant import registry

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 7. which detectors run, and where each kind's answer comes from
# --------------------------------------------------------------------------
# Every detector the pass runs, each over ITS OWN window. Adding one is adding
# it to that tuple; there is no second list to keep in step and no `if kind ==`
# below.
#
# A tuple of MODULES rather than a table of per-kind settings, and that is
# decision 7. Everything needed to run a detector and to read the blob it
# wrote is a fact about the detector and is already stated in the detector's
# own module — `KIND`, `LOOKBACK_DAYS`, `scan` and `EVIDENCE_FIELDS` —
# and core/replay.py's table reads two of those off these same modules by name.
#
# The tuple itself lives in core/detectors/__init__.py rather than here, and
# that is 2.14.6 rather than a change of mind: core/artefacts.py has to know
# which detector wrote a blob in order to name the fields in it, and it cannot
# ask this module — the chain from here to core/verify.py ends at the renderer.
# One tuple, in the package the detectors are in, and the pass reads it like
# everyone else.
#
# The order is the order they are scanned in and nothing else. Which question
# gets asked is `rank`'s answer, over every validated suggestion of any kind.
DETECTORS = detectors.DETECTORS

# The longest window any detector reads. Derived rather than written down, so
# that a detector added above cannot leave it stale.
_LONGEST_LOOKBACK_DAYS = max(int(d.LOOKBACK_DAYS) for d in DETECTORS)


def detector_for(kind):
    """The detector that raises this kind of candidate, or None.

    `detectors.for_kind` under this module's own name, because every caller
    here asks it about a suggestion row the pass is holding. None is a
    refusal, and every caller treats it as one rather than falling back to the
    night: a kind with no detector is a row written by a version of this agent
    that had one this build does not — core/verify.py refuses to measure such
    a row and core/builder.py refuses to build it, and nothing here may read a
    field off its blob by guessing the vocabulary.
    """
    return detectors.for_kind(kind)


# --------------------------------------------------------------------------
# 1. when the evidence has changed (and when it has only moved)
# --------------------------------------------------------------------------
# The quantisation of the two figures the fingerprint carries. Both are the
# resolution the CLAIM is made at rather than the resolution the record has:
# §9 renders this as "about 12 W on most nights", and 12.1 W is the same
# sentence as 12.0 W. Finer than this and the fingerprint changes on the noise
# the median was chosen to absorb, which is the drift it exists to ignore;
# coarser and a real change in the claim would pass unnoticed.
#
# One watt is also comfortably above the record's own resolution
# (observer.NUMERIC_MIN_ABS_CHANGE is 0.5 W), so a difference this notices is
# always a difference the record could actually see.
FINGERPRINT_WATTS_STEP = 1.0

# The frequency is quantised much more coarsely, and that is the whole of
# decision 1 in one number. The window slides by one day every morning, so a
# night leaves the back of it and a night joins the front, and the count of
# qualifying nights moves by one on a home where coverage is anything less than
# perfect. Re-grading on that is re-grading on the calendar rather than on the
# house. Five a month is about a sixth of the claim, and the point at which the
# sentence itself changes — "nearly every night" is not "most nights". Slow
# drift below that is caught by EVIDENCE_HOLD_DAYS instead.
#
# ONE step for both shapes, because it is one quantity: how many times a month
# this happens, on a scale that runs to about thirty either way, rendered by §9
# in the same sentence. It is more generous than a schedule needs — a ninety
# day window sliding one day moves an occurrence count by about a third of one,
# where a thirty day window moves a night count by a whole one — and that
# slack errs towards HOLDING a replay, which EVIDENCE_HOLD_DAYS already bounds
# at a week. A per-kind step would be a second threshold to argue with for a
# difference that only ever makes the coarser side safer.
FINGERPRINT_PER_MONTH_STEP = 5.0

# How long stored evidence may be held before it is refreshed anyway, even
# though the claim has not moved. The blob names the window it was measured
# over and §9 shows that window to a person, so evidence held indefinitely
# would eventually describe a fortnight nobody remembers. A week is the point
# at which "measured over the thirty days to the 14th" stops reading as a
# statement about now. Refreshing costs one replay, over rows already in hand.
EVIDENCE_HOLD_DAYS = 7


# --------------------------------------------------------------------------
# 2. how many would-have-been-wrong nights disqualify a candidate
# --------------------------------------------------------------------------
# None. A night replay marks `wrong` is one where the record PROVES the rule
# would have cut power to a device that was then used — the television that
# dies at 03:00, which is the exact failure that costs the product its user.
#
# Zero rather than a small tolerance because of the asymmetry replay.py is
# built around: a device used later is proof the rule was wrong, while a device
# not used is not proof it was right. Nights too patchy to credit still count
# their wrongness, and nights nobody could read count nothing at all — so the
# number that comes back is a LOWER BOUND on how often this would misfire.
# Tolerating one seen is tolerating an unknown number unseen, and §7.1 discards
# a candidate rather than showing it with a weak number.
#
# A disqualified candidate is not retired: it stays a candidate, and tomorrow's
# pass grades it again over a window that has moved. A household that stops
# using the device after midnight gets the suggestion; nothing here decides for
# them permanently.
MAX_WRONG_NIGHTS = 0


# --------------------------------------------------------------------------
# 3. what a proposal is proposed as
# --------------------------------------------------------------------------
# Create, never update — see the docstring. The string is the tool's name
# because that is how core/agent_runtime.py's registry and core/gate.py's
# vocabulary both match tools; it must stay one of gate.PROPOSABLE_TOOLS, which
# tests/test_review_pass.py asserts rather than trusting.
PROPOSAL_TOOL = "Create Home Assistant automation"


# --------------------------------------------------------------------------
# 5. cadence, rank and volume
# --------------------------------------------------------------------------
# The local hour the daily pass runs at. After the night window it reasons
# about has closed (standby.NIGHT ends at 05:00) with room for the recorder
# and core/backfill.py to have caught up, and inside the morning §9's digest is
# written for. Local because everything else about a night here is local.
#
# It is still the NIGHT's hour with a second detector in the pass, and that is
# not an oversight. A schedule window has no closing time in common — the one
# the proposal opens with runs to 17:00 — so there is no hour that follows
# every window, and waiting for today's would mean asking the question in the
# evening. What today's unfinished hours cost is their own cells: they are
# unobserved, out of ninety days of them, and the detector's floor is stated in
# observations per cell for exactly that reason.
REVIEW_HOUR = 9

# One new question a day. The pass is not a queue to be drained: a run that
# proposed everything it found is the failure mode §12 describes, where the
# user stops reading. Daily also means the ramp on a home with several standby
# loads is one question a morning, most valuable first, and a household that
# answers keeps getting the next one.
MAX_PROPOSALS_PER_RUN = 1

# ...and never more than this many open at once. §9's digest is three lines,
# so a fourth open proposal is one the digest cannot show — and a question the
# user cannot see is a question that should not have been asked yet. It is also
# the back-pressure that makes the daily cadence safe: proposals that nobody
# answers stop the pass adding more.
#
# Both limits are the DETECTORS' since F-WP9 (2.24.0); analyst rows neither
# count against them nor take them. See the digest below.
MAX_OPEN_PROPOSALS = 3

# The analyst's cadence (free-hand F-12, F-WP9): one digest a week, on the day
# the household picked (settings.DIGEST_DAY), at REVIEW_HOUR — the digest is a
# step of this same daily pass, so it has no hour of its own to disagree with.
#
# Three, and the same three bounds how many analyst questions may be open at
# once: a digest day with last week's ideas still unanswered TOPS UP to three
# rather than adding three more, which is the back-pressure MAX_OPEN_PROPOSALS
# is for detectors — a household that does not answer is not handed a longer
# list. A separate cap rather than a share of the detectors' three, because a
# shared one is exactly what left analyst ideas starving behind savings.
DIGEST_SIZE = 3

# How long an analyst idea may wait at `validated` for a digest before it is
# retired. Not UNSEEN_RETIRE_DAYS: that ages a row on `updated_at`, which the
# analyst touches only by submitting the same idea again, and D-12 shows it
# what is already queued precisely so that it does not — so under a weekly
# digest a week's age would retire every leftover the day before its second
# digest. Two digests' worth: an idea that did not fit this week gets next
# week's, and if it still has not been asked it goes, and the analyst may
# find it again over a record that has moved on.
DIGEST_WAIT_DAYS = 14

# time.struct_time's tm_wday order. The option's own spellings, so a name the
# add-on schema accepts is one this reads.
DIGEST_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
                   "saturday", "sunday")
DEFAULT_DIGEST_DAY = "saturday"

# When the last digest was held. A stamped file rather than the store, because
# "held" includes a digest day with nothing to raise: counted from proposed
# rows, an empty Saturday would leave the week's digest owed, and the first idea
# stored on Tuesday would be put to the household on Tuesday — a cadence nobody
# picked. Fails closed (`last_digest_at`), like core/analyst.py's run file.
DIGEST_PATH = os.path.join(settings.DATA_DIR, "digest.json")
_digest_lock = threading.Lock()

def evidence_max_age_days(kind) -> int:
    """When an open proposal's evidence stops being worth asking about.

    The session TTL deliberately will not expire one of these
    (gate.pending_for says why), so this is the only thing that withdraws it —
    and it is stated as an age of the EVIDENCE rather than of the question,
    because "the user has not answered yet" is not a reason to stop asking.

    It is the RAISING DETECTOR'S own lookback, and that is why it is a function
    rather than a constant. At `LOOKBACK_DAYS` the window the evidence was
    measured over no longer overlaps today at all, and the sentence a user
    reads is then about a stretch that has entirely passed — which is thirty
    days for a night and ninety for a schedule. One number for both would
    either withdraw a schedule proposal two months before its evidence stopped
    describing now, or leave a night one standing for three.

    A kind with no detector is aged against the LONGEST lookback there is. Not
    zero, which is what reading a missing answer as a number would come to and
    would withdraw every such question the morning it was asked; and not never,
    because the evidence is still stale eventually and the longest window any
    detector reads is the latest date at which that can be said of anything
    this pass has written.

    The suggestion is retired rather than dismissed — a retired key sits
    outside the store's partial unique index, so the next pass raises the same
    waste as a fresh candidate, replays it, and asks again with a number that
    is true. A dismissal would be a lie about who said no, and §12 counts
    dismissals.
    """
    detector = detector_for(kind)
    return int(detector.LOOKBACK_DAYS) if detector else _LONGEST_LOOKBACK_DAYS

# And when a candidate nobody has proposed stops being worth holding.
# `upsert_candidate` stamps updated_at every time the pass finds the same waste
# again, so time since it is exactly "how long since the detector last saw
# this" — no counter needed, and no jumpiness: a candidate that dips below the
# floor for one night is found again the next morning, while one whose device
# was unplugged ages out. Retired, again, so the same waste can come back.
UNSEEN_RETIRE_DAYS = 7


# --------------------------------------------------------------------------
# the seam at the other end: what a verification will be measured against
# --------------------------------------------------------------------------
# §7.2's week, and the length of the baseline that will be compared against it.
# Both live in core/verify.py rather than here, because "compare like periods"
# is the verification's rule and a window this module owned would be a second
# opinion about how long a week is. Named here so the seam below reads as one
# sentence.
#
# They are the length for a rule that happens every day, which is every rule
# any home has today. A rule whose window names its weekdays is measured over
# `verify.days_for(row)` instead — as many whole weeks as it takes to offer the
# same number of OCCURRENCES — and that is read once in `settle` for both the
# baseline and the wait.
VERIFY_AFTER_DAYS = verify.AFTER_DAYS
BASELINE_DAYS = verify.BASELINE_DAYS


# --------------------------------------------------------------------------
# pure rules
# --------------------------------------------------------------------------
def _local_date(ts):
    """The local calendar date an instant falls on."""
    return date.fromtimestamp(int(ts))


def _start_of_local_day(ts) -> int:
    """Midnight local, on the date an instant falls on.

    Through time.mktime with tm_isdst at -1, so that on a date the clock jumped
    the C library resolves it rather than an offset guessed here.

    Deliberately NOT core/window.py's `instant`, and the difference is the
    question rather than the implementation. The seam answers "did this wall
    time happen", because a measurement taken across an hour that did not exist
    is a measurement of something else. This asks "when did today start", which
    has an answer in every zone including the handful that have skipped
    midnight itself — and it is compared against `updated_at` to count how many
    proposals were raised today. A None here would not withdraw a claim; it
    would raise the throttled second question of the day.
    """
    day = _local_date(ts)
    return int(time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1)))


def _quantise(value, step):
    """A number at the resolution the claim is made at, or None."""
    number = observer.as_float(value)
    if number is None or not step:
        return None
    return round(number / float(step))


def evidence_fingerprint(evidence, kind):
    """What would have to change before a replay stopped describing the same
    claim — or None when the blob's own vocabulary cannot be read.

    Decision 1. The claim is: this kind of claim, about this device, cut at
    this switch, drawing about this much, that often a month, between these
    hours. Everything else in the blob is bookkeeping about how the claim was
    reached — which days were covered, which nights were excluded and why, the
    exact window — and every one of those moves each morning without the claim
    moving. Fingerprinting them would revalidate a suggestion daily and propose
    it never.

    The last two are read through `EVIDENCE_FIELDS`, because the two shapes do
    not share the names: a night blob says `nights_per_month` and
    `night_window`, a schedule blob says `occurrences_per_month` and
    `schedule_window`. Read by the night's names alone a schedule blob answers
    None and "" for both, the tuple collapses to (device, switch, watts) — and
    the window, the one field that would tell a night claim about a television
    apart from a daytime claim about the same television, is the half that
    vanished. The kind leads the tuple for the same reason, so that the
    distinction survives even a device whose two windows happen to be measured
    at the same level and the same frequency.

    None, and not a shorter tuple, for a kind with no detector. A tuple missing
    the fields it could not read compares EQUAL to another one missing the same
    fields, and "the evidence has not moved" is precisely the answer that must
    never be reached by failing to look.

    Deliberately NOT hashed. A tuple compares the same way, and the values in
    it can be read in a log line by someone asking why a suggestion was
    revalidated.
    """
    detector = detector_for(kind)
    if detector is None:
        return None
    fields = detector.EVIDENCE_FIELDS
    blob = evidence or {}
    return (
        str(kind or ""),
        str(blob.get("entity") or ""),
        str(blob.get("switch") or ""),
        _quantise(blob.get("watts"), FINGERPRINT_WATTS_STEP),
        _quantise(blob.get(fields["per_month"]), FINGERPRINT_PER_MONTH_STEP),
        # The rendered window, which is the one description of the hours that
        # is on disk beside the claim. It moves only when the detector's spec
        # does — a change to what is being claimed, and one that should discard
        # every replay graded before it. With a second detector it is also what
        # distinguishes two claims about one device.
        str(blob.get(fields["window"]) or ""),
    )


def evidence_age_days(evidence, now):
    """How many days ago the window the evidence was measured over ended, or
    None when the blob does not say."""
    window = (evidence or {}).get("window") or {}
    measured_to = window.get("to")
    if not measured_to:
        return None
    try:
        return (_local_date(now) - date.fromisoformat(str(measured_to))).days
    except (TypeError, ValueError):
        return None


def evidence_to_store(existing, fresh, kind, now):
    """Which evidence blob today's upsert should carry: (evidence, why).

    Decision 1, applied. `existing` is the open suggestion row for this key or
    None; `fresh` is what the detector measured this morning; `kind` is whose
    vocabulary both of them are written in. One kind covers both sides because
    a key IS `kind:entity` — a row and the candidate that matched it cannot
    disagree about which detector raised them.

    Handing back the STORED blob is what holds a replay still. It is not a way
    around suggestions.upsert_candidate's rule — it is the honest reading of
    it. The replay on that row was graded over the window the stored evidence
    names, and §9 requires the evidence a user is shown to be the evidence the
    number came from, so replacing it with today's window while keeping
    yesterday's grade would print a window nothing was graded over.
    """
    if not existing:
        return fresh, "new"
    stored = existing.get("evidence")
    if existing.get("state") != "validated" or not isinstance(stored, dict):
        # Nothing to protect. Holding exists to keep a REPLAY, and only a
        # validated row has one: a candidate never had one, a dismissal past
        # its cooldown had its cleared, and a proposal is not touched by the
        # store at all. Holding here would be worse than pointless — the pass
        # is about to grade today's rows, and it would be storing a
        # measurement taken over one window beside a grading taken over
        # another.
        return fresh, "fresh"
    stored_print = evidence_fingerprint(stored, kind)
    if stored_print is None or stored_print != evidence_fingerprint(fresh, kind):
        # The `is None` is not redundant. Both sides are the same kind, so an
        # unreadable one is unreadable on both — and `None != None` is False,
        # which would call a blob nothing could read "unchanged" and hold a
        # replay nobody can confirm still describes the same claim.
        return fresh, "moved"
    age = evidence_age_days(stored, now)
    if age is not None and age >= EVIDENCE_HOLD_DAYS:
        return fresh, "aged"
    return stored, "held"


def disqualified(replayed, kind):
    """Why a graded candidate must not be proposed, or None.

    Decision 2. replay.run has already refused everything it can refuse — an
    unsupported config, the wrong entity's series, too few credited firings —
    and what is left is the one thing it reports without judging: how often the
    rule would have been wrong.

    The count is read by the KIND'S OWN name for it, through
    `replay.fields_for` — decision 7, and it is the table that chose those
    words rather than a copy of them here. `nights_wrong` on a night blob,
    `occurrences_wrong` on a schedule one: core/replay.py's docstring names
    this as the one field that would fail QUIETLY if it were read by one name,
    absent and taken as zero behind an `or 0`, passing every candidate the
    record says would have cut power to a device in use.

    Two refusals rather than one, because there are two ways the count can be
    unreadable. A kind replay cannot name the fields of is refused outright —
    that is a blob this build has no vocabulary for at all. A kind it CAN name
    but whose blob is missing the count is caught by `would_have_interrupted`,
    which keeps ONE name for both shapes so that a grading saying it would have
    interrupted is refused even when how often cannot be read.
    """
    blob = replayed or {}
    fields = replay.fields_for(kind)
    if fields is None:
        return (f"the grading is written in a vocabulary this pass does not know "
                f"({kind!r}), so how often the rule would have been wrong cannot be read")
    counted = observer.as_float(blob.get(fields["wrong"]))
    if counted is None and blob.get("would_have_interrupted"):
        return ("the record shows it would have cut power to a device in use, and this "
                "pass cannot read how often from the grading it was given")
    wrong = int(counted or 0)
    if wrong > MAX_WRONG_NIGHTS:
        examined = blob.get(fields["examined"])
        return (f"the record shows it would have cut power to a device in use on "
                f"{wrong} of {examined} {fields['noun']}s")
    return None


def _credited(row):
    """How many firings this row's grading credited, in its own vocabulary, or
    None when that cannot be read."""
    fields = replay.fields_for((row or {}).get("kind"))
    if fields is None:
        return None
    return observer.as_float((row.get("replay") or {}).get(fields["saved"]))


# F-10: an evidence level that sorts as another. See `rank`.
_RANKED_AS = {suggestions.EVIDENCE_SHOWN: suggestions.EVIDENCE_UNTESTED}


def rank(rows) -> list:
    """Validated suggestions, most worth asking about first — across every
    detector rather than within one.

    By the ESTIMATE rather than the replay's own figure, because the estimate
    is what is being claimed: replay.py may report the detector's numbers
    instead of its own when the two disagree, and ranking on the half that is
    not being shown would order the digest by a number nobody sees. Its
    `kwh_per_month` is the one figure every shape writes under one name, which
    is what makes a single ordering over two kinds honest — a night's kilowatt
    hours and a schedule's are the same quantity, compared the same way.

    Then by the evidence LABEL, strongest first — replayed, checked, untested
    (proposal D-6) — because the analyst's rows share these limits with the
    detectors' (M-1) and this is where the two meet. An analyst row has no
    estimate, so it sorts at zero kWh, behind every detector row that claims a
    saving: a *Checked* model idea queues behind a *Replayed* detector one.
    That is M-1's accepted consequence, not an accident of the sort — ranking
    it higher would need a saving the record produced, and that is Phase J's.
    Among the zero-saving rows the stronger label goes first. A label this
    cannot read sorts after all three, for the reason the grading below does.
    The explorer run's `shown` (free-hand F-10) is not a rung on that ladder
    — nothing was recounted, a person judges a chart — and sorts exactly where
    `untested` does. Since F-WP9 that no longer decides WHEN an analyst idea
    is asked: the weekly digest takes analyst rows apart from the detectors'
    (`digest_order`), and one ordering over both sources is what the §9
    listing still wants.

    Then by how many firings were credited — between two similar savings the
    better-evidenced one goes first — and that one is NOT a shared name
    (`nights_saved` against `occurrences_saved`), so it is read through
    `replay.fields_for`. A grading whose vocabulary cannot be read sorts after
    every grading that can, rather than being read as zero and landing among
    the genuinely thin ones — which is every analyst row, since nothing
    replayed it. Then by key, so the order does not wobble between two runs
    that found the same thing; between two analyst rows of one label that is
    deterministic but arbitrary, and deliberately nothing better.
    """
    def order(row):
        estimate = row.get("estimate") or {}
        credited = _credited(row)
        label = _RANKED_AS.get(row.get("evidence_level"), row.get("evidence_level"))
        return (
            -(observer.as_float(estimate.get("kwh_per_month")) or 0.0),
            suggestions.EVIDENCE_LEVELS.index(label)
            if label in suggestions.EVIDENCE_LEVELS else len(suggestions.EVIDENCE_LEVELS),
            0 if credited is not None else 1,
            -(credited or 0.0),
            str(row.get("key") or ""),
        )
    return sorted(rows or [], key=order)


def proposals_allowed(open_now, proposed_today) -> int:
    """How many new questions this run may raise.

    Decision 5, as one formula with two clauses. The first is the cadence — one
    a day, and it is counted from the STORE rather than from a variable in this
    process, so a restart at noon cannot ask a second question the same day.
    The second is §9's digest: three lines, and a proposal that would not fit in
    it is one the user has no surface to see.
    """
    try:
        room = MAX_OPEN_PROPOSALS - int(open_now)
        today = MAX_PROPOSALS_PER_RUN - int(proposed_today)
    except (TypeError, ValueError):
        return 0
    return max(0, min(room, today))


def _is_analyst(row) -> bool:
    """Whether the analyst raised this row. `source`, never `kind` (B-1)."""
    return (row or {}).get("source") == suggestions.SOURCE_ANALYST


def digest_weekday(name) -> int:
    """The option's day as a `tm_wday`. Anything this does not recognise is the
    default's day, never an error and never "no digest": a typo that silenced
    the analyst would be a failure nobody sees."""
    value = str(name or "").strip().lower()
    if value not in DIGEST_WEEKDAYS:
        value = DEFAULT_DIGEST_DAY
    return DIGEST_WEEKDAYS.index(value)


def digest_instant(now, weekday) -> int:
    """The most recent digest moment at or before `now`: `weekday` at
    REVIEW_HOUR local.

    Built as a wall clock and handed to mktime with the DST flag left to it,
    as `_start_of_local_day` is, so the week that loses an hour still has its
    digest at 09:00 rather than at 08:00 or 10:00.
    """
    now = int(now)
    local = time.localtime(now)
    day = date(local.tm_year, local.tm_mon, local.tm_mday) - timedelta(
        days=(local.tm_wday - int(weekday)) % 7)
    for back in (0, 7):
        when = day - timedelta(days=back)
        instant = int(time.mktime((when.year, when.month, when.day,
                                   REVIEW_HOUR, 0, 0, 0, 0, -1)))
        if instant <= now:
            return instant
    return instant


def digest_due(now, weekday, last_digest_at) -> bool:
    """Whether this pass holds the week's digest.

    Due once the latest digest moment has passed and no digest has been held
    since it. That one rule is both decisions F-WP9 recorded: a restart on
    digest day finds the stamp at or after the moment and sends nothing twice,
    and a digest day the add-on was down for is still owed the next morning —
    the latest moment is still last Saturday's until next Saturday's arrives —
    and is held once, because holding it stamps past that moment.

    No stamp is not "never held": `last_digest_at` reads a missing file as
    held now, so a fresh install's first digest is the next digest day rather
    than the first review after the update.
    """
    try:
        return int(last_digest_at) < digest_instant(now, weekday)
    except (TypeError, ValueError):
        return False


def digest_allowed(open_analyst) -> int:
    """How many analyst ideas this digest may raise: enough to bring the open
    ones back to DIGEST_SIZE. Last week's unanswered ideas are not replaced —
    withdrawing a question a person may be halfway through reading is the
    agent changing its mind on their time — and not stacked either."""
    try:
        return max(0, DIGEST_SIZE - int(open_analyst))
    except (TypeError, ValueError):
        return 0


def digest_order(rows) -> list:
    """Analyst ideas in the order a digest takes them: the longest waiting
    first, then `rank`.

    "Best first" among `shown` rows has no number to stand on — no estimate,
    no recount, the chart is the label (F-10) — so `rank` orders them by key,
    deterministic and arbitrary. Waiting longest is a real reason: it is the
    idea closest to DIGEST_WAIT_DAYS, and a leftover taken first is a leftover
    that gets the second digest the wait was sized for. Stable over `rank`,
    so two ideas stored by one run keep `rank`'s order.
    """
    return sorted(rank(rows), key=lambda row: int(observer.as_float(row.get("created_at")) or 0))


def _observed(row):
    """The entity a suggestion's claim is MEASURED on.

    `evidence["entity"]` is the detector's own answer and is what every blob
    carries; `entities[0]` is the same entity — whose series the scan read —
    and is the fallback for a row whose evidence could not be read back.
    """
    row = row or {}
    evidence = row.get("evidence") or {}
    entities = row.get("entities") or []
    return evidence.get("entity") or (entities[0] if entities else None)


def _claims(row) -> set:
    """Every entity a suggestion's change would touch: the device it measures
    AND the switch it would cut.

    Both, and that is deliberately not the question `_observed` answers.
    `_observed` is for the watchlist check, where only the MEASURED entity may
    be required — a household may well watch the power sensor and not the
    plug, which is a case the detectors support. This is for
    `claimed_entities`, where the switch is half the reason the rule exists:
    `power.controllable_switch` pairs a sensor with the one controllable
    sibling on its device, so a device carrying TWO watched power sensors
    pairs both of them with the SAME switch. Claiming only the sensor would
    let two suggestions naming two different sensors be proposed, and they are
    two automations cutting one socket — the second of the three reasons
    below, reached without either of the other two.
    """
    row = row or {}
    named = set(row.get("entities") or [])
    named.add(_observed(row))
    return {entity for entity in named if entity}


# The states in which a device is already spoken for: a question outstanding, a
# rule live and having its week measured, or a measured result waiting to be
# kept or rolled back. The user's and the verification's, never the agent's.
_SPOKEN_FOR = ("proposed", "applied", "verified")


def claimed_entities(rows) -> set:
    """The entities a second question may not be asked about yet.

    Decision 5's last clause, and it is here because it stopped being
    structural the moment there was a second detector. The store's partial
    unique index makes one live row per KEY and a key is `kind:entity`, so
    `standby:sensor.tv` and `schedule:sensor.tv` are two rows, two replays and
    two questions — and on a television with a night plateau and a daytime one
    that is the ordinary case rather than a corner of it.

    Three reasons they must not both be live, none of which is volume:

      §9 answers a suggestion in one word. Two questions about one television,
      a line apart in a three-line digest, are two things "yes" could mean.

      They would be two automations cutting one switch, and §7.2 measures each
      by subtracting two weeks of the SAME sensor. A second rule applied inside
      the first one's week moves the after, and the report would credit the
      saving to whichever rule happened to be the one being measured. This is
      why the claim is every entity a suggestion NAMES rather than the one it
      measures — see `_claims`: a device with two watched power sensors pairs
      both of them with one switch, and two claims about two sensors are still
      two automations cutting one socket.

      And the detector that has not been applied yet keeps reading a record the
      applied one is now changing. That much is honest — the cut hours read
      `below_floor` and are excluded rather than counted twice — but it is only
      honest while one claim at a time is live.

    So the three states are the user's and the verification's business, and the
    agent's own are deliberately not among them. A `candidate` or a `validated`
    row costs nothing and is left to accumulate; tomorrow's pass ranks it
    against everything else. A `dismissed` one is a cooldown on ONE key,
    because a "no" to the night question is not a "no" to the daytime one. And
    `kept` and `rolled_back` are ends: the verification is over, the device is
    free, and the next claim about it is measured over a record that already
    includes whatever was kept.
    """
    claimed = set()
    for row in rows or []:
        if (row or {}).get("state") not in _SPOKEN_FOR:
            continue
        claimed |= _claims(row)
    return claimed


def withdrawal_reason(row, now, watched=None):
    """Why this suggestion should be withdrawn now, or None to leave it alone.

    Decision 5's other half. The two timeouts are on the EVIDENCE rather than
    on the user, and both are deliberately smooth: a candidate the detector
    misses for one night is found again the next morning, and a proposal is not
    withdrawn for going unanswered — going unanswered is allowed, since nobody
    promised to read it today.

    The third rule is §11's: an entity the agent can no longer observe is one
    it cannot measure a change to, whether it disappeared from Home Assistant
    or the household simply stopped watching it. Only the observed entity is
    checked, never the switch — the watchlist is opt-in and a household may
    well have put the power sensor on it and not the plug, which is a case the
    detector supports and this must not retire.

    A `dismissed` row is never withdrawn here, and that is not an omission: its
    cooldown is enforced by upsert_candidate finding the live row, and retiring
    it would put it outside the store's partial unique index and let the same
    waste be raised again tomorrow — turning "not now" into "asked again in the
    morning", which is exactly what the cooldown exists to prevent. An
    `applied` one belongs to the verification, not here.

    An analyst row is answered by `_analyst_withdrawal_reason` before any of
    this is asked (B-1): `_observed` reads `entities[0]`, which for an analyst
    row is whichever entity sorted first rather than one anything measured.
    """
    if (row or {}).get("source") == suggestions.SOURCE_ANALYST:
        return _analyst_withdrawal_reason(row, now, watched)

    state = (row or {}).get("state")
    if state not in ("candidate", "validated", "proposed"):
        return None

    # An EMPTY watchlist is not evidence that anything stopped being watched:
    # it is what core/watchlist.py answers for a home that watches nothing AND
    # what it answers when it cannot read its own file, and acting on it would
    # retire every open suggestion at once over a failed read. Skipped rather
    # than applied — the timeouts below still hold.
    if watched:
        observed = _observed(row)
        if observed and observed not in watched:
            return f"{observed} is no longer watched, so nothing about it can be measured"

    if state == "proposed":
        age = evidence_age_days(row.get("evidence"), now)
        if age is not None and age >= evidence_max_age_days(row.get("kind")):
            return (f"the evidence behind it was measured over a window that ended "
                    f"{age} days ago, and no longer describes now")
        return None

    seen = observer.as_float(row.get("updated_at"))
    if seen is None:
        return None
    idle = int((int(now) - int(seen)) / 86400)
    if idle >= UNSEEN_RETIRE_DAYS:
        return f"the waste it describes has not been found for {idle} days"
    return None


def _charted_entities(row) -> list:
    """The entity ids a `shown` row's chart spec names — its evidence
    entities (F-10), whether or not the chart could be drawn."""
    chart = (row or {}).get("chart")
    spec = chart.get("spec") if isinstance(chart, dict) else None
    entities = spec.get("entities") if isinstance(spec, dict) else None
    return [e for e in entities if isinstance(e, str)] if isinstance(entities, list) else []


# Free-hand F-9 (2.23.0): why an analyst row the retired claim audit stored is
# withdrawn. Its label (`checked` / `untested`) and its claims came from a check
# that no longer exists, so nothing current stands behind them; the explorer
# may raise the same idea again, with a chart (a retired key blocks nothing).
CLAIM_AUDIT_RETIRED = ("the claim audit that checked it has been retired; the analyst now "
                       "shows its evidence as a chart, and may raise the idea again with one")


def _analyst_withdrawal_reason(row, now, watched=None):
    """`withdrawal_reason` for a row the analyst raised (proposal D-5).

    A row the explorer did not store — anything not `shown`: the retired
    audited run's `checked` / `untested` rows, or a `candidate` whose
    `mark_shown` never happened — is withdrawn outright (CLAIM_AUDIT_RETIRED,
    F-9). A proposed one has its question withdrawn with it, by `_withdraw`.

    A `shown` row (the explorer run, free-hand F-10): its evidence is its
    chart, so the entities that must stay watched are the chart spec's — a
    chart of an entity nobody watches any more can no longer be drawn from
    anything current. Entities that appear only in the config — a trigger, an
    action target — are not checked against the watchlist: nobody promised to
    watch the light an automation switches, and the gate's test-create at
    propose time is what checks they still exist (D-7).

    A proposed row's evidence ages from `updated_at`, the moment
    `mark_proposed` stamped it; the limit is `evidence_max_age_days` for a
    kind with no detector, the longest lookback there is — a new constant
    would be a second opinion on the same question. A `candidate` row ages on
    `UNSEEN_RETIRE_DAYS` exactly as a detector's does: `upsert_analyst_candidate`
    touches `updated_at` whenever the analyst submits the same idea again, so
    "unseen for a week" means the analyst stopped thinking it. A `validated`
    one is waiting for a digest, not for the analyst, and ages on
    DIGEST_WAIT_DAYS instead — see that constant for why a week would retire
    every leftover the day before its second digest.
    """
    state = (row or {}).get("state")
    if state not in ("candidate", "validated", "proposed"):
        return None
    if row.get("evidence_level") != suggestions.EVIDENCE_SHOWN:
        return CLAIM_AUDIT_RETIRED

    # Skipped on an empty watchlist for the reason withdrawal_reason gives.
    if watched:
        gone = sorted(entity for entity in _charted_entities(row) if entity not in watched)
        if gone:
            return (f"{gone[0]} is no longer watched, so the chart behind it "
                    f"cannot be drawn")

    if state == "proposed":
        since = observer.as_float(row.get("updated_at"))
        if since is None:
            return None
        age = int((int(now) - int(since)) / 86400)
        if age >= evidence_max_age_days(row.get("kind")):
            return f"it was asked {age} days ago on evidence that no longer describes now"
        return None

    seen = observer.as_float(row.get("updated_at"))
    if seen is None:
        return None
    idle = int((int(now) - int(seen)) / 86400)
    if state == "validated":
        if idle >= DIGEST_WAIT_DAYS:
            return f"no digest put it to you in the {idle} days since the analyst found it"
        return None
    if idle >= UNSEEN_RETIRE_DAYS:
        return f"the agent has not raised it for {idle} days"
    return None


def due(now, last_run_at=None) -> bool:
    """Whether the daily pass should run at this instant.

    After REVIEW_HOUR local, and not already run on this local date. `now` and
    `last_run_at` are both handed in: the schedule is the caller's memory, and
    a pure rule about it is one a test can walk a week through.
    """
    now = int(now)
    if time.localtime(now).tm_hour < REVIEW_HOUR:
        return False
    if last_run_at and _local_date(last_run_at) == _local_date(now):
        return False
    return True


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------
def run_once(now=None) -> dict:
    """One review. Returns a count of what it did, for the log.

    The I/O half, and the only thing here that touches a store, Home Assistant
    or a clock it was not given. Blocking throughout — SQLite and HTTP — so it
    runs in an executor; tasks/review.py is what schedules it.

    There is no `lookback_days` parameter and there deliberately is not one.
    It used to be a single number defaulting to standby's, which against a
    schedule candidate would have measured a ninety-day claim over thirty days
    of record and then compared the result against a floor set for the other
    window. How far back a detector reads is the detector's own answer
    (decision 7), each scan is called with its own, and `replay.run` is left to
    resolve the same figure from the same place for the grading — so the window
    the rows came from and the window they are graded over cannot disagree.

    Withdrawal runs before proposal so that a slot freed this morning can be
    used this morning.
    """
    now = int(now) if now else int(time.time())
    summary = {"candidates": 0, "validated": 0, "settled": 0, "verified": 0,
               "retired": 0, "digest": 0, "proposed": 0}

    # Decision 6's other half, and WP9's answer to where the verification runs.
    # ONE registry round trip serves the whole pass: the detector needs the
    # units and the switch pairing out of it, and core/verify.py needs the same
    # states to ask whether the automation it wrote a week ago is still
    # switched on. Read here rather than inside either of them, and only when
    # there is something for it to answer — a home that watches nothing and has
    # nothing due makes no call at all.
    #
    # The list of what is due is taken BEFORE anything else runs, on purpose. A
    # suggestion `_reconcile` is about to discover was applied a week ago gets
    # its baseline measured a moment from now, and verifying it in the same
    # pass would compare that baseline against a week it already covers.
    # Tomorrow's pass finds it, with a "before" that means something.
    watched = watchlist.watched_entities()
    due = suggestions.due_for_verification(now=now)
    snapshot = None
    if watched or due:
        ok, data = registry.fetch_registry()
        if ok:
            snapshot = data
        else:
            logger.warning(f"REVIEW: could not read the registry, so nothing is scanned or "
                           f"verified this run ({data})")

    if snapshot is not None:
        # Each detector in turn, each over its own window, all sharing the one
        # registry read above. One after the other rather than at once: the
        # bound decision 6 cares about is how much of the record is in memory,
        # and a scan that has returned is holding only its candidates' rows.
        for detector in DETECTORS:
            for candidate in detector.scan(now=now, snapshot=snapshot):
                summary["candidates"] += 1
                if _consider(candidate, now):
                    summary["validated"] += 1

    summary["settled"] = _reconcile(now)
    summary["verified"] = verify.run_due(now, due=due, snapshot=snapshot)
    summary["retired"] = _withdraw(now)
    # The digest before the daily question: both take devices out of
    # `claimed_entities`, and whichever goes second waits — a day for a
    # detector row, a week for an analyst idea.
    summary["digest"] = _digest(now)
    summary["proposed"] = _propose(now)

    if any(summary.values()):
        logger.info(f"REVIEW: {summary['candidates']} candidate(s), "
                    f"{summary['validated']} validated, {summary['settled']} settled, "
                    f"{summary['verified']} verified, "
                    f"{summary['retired']} withdrawn, {summary['digest']} in the digest, "
                    f"{summary['proposed']} proposed")
    return summary


def _consider(candidate, now) -> bool:
    """Records one candidate and validates it if it is not already. Returns
    whether it was validated by THIS run.

    The upsert is here rather than in the detector because the decision it
    needs — is this the same evidence as yesterday's — is decision 1, and it is
    made just above (`evidence_to_store`).
    """
    key = candidate.get("key")
    kind = candidate.get("kind")
    evidence, why = evidence_to_store(
        suggestions.open_for(key), candidate.get("evidence"), kind, now)

    suggestion_id = suggestions.upsert_candidate(
        key, kind, entities=candidate.get("entities"), evidence=evidence)
    if not suggestion_id:
        # The store said no — a locked database, a schema that would not apply.
        # It is logged there; nothing further can be done with a candidate that
        # has no row to hang a replay, a proposal or a measurement on. Tomorrow
        # finds the same waste under the same key.
        logger.error(f"REVIEW: found waste on {key} but could not record it")
        return False

    row = suggestions.suggestion(suggestion_id)
    if not row or row.get("state") != "candidate":
        # Held validated (decision 1 doing its job), or already the user's
        # business. Either way there is nothing to grade this morning.
        logger.debug(f"REVIEW: {key} is {row.get('state') if row else 'gone'} "
                     f"(evidence {why}) — nothing to validate")
        return False

    return _validate(row, candidate, now)


def _validate(row, candidate, now) -> bool:
    """Builds the change this candidate implies, grades it against the same
    rows, and records the grade. Returns whether it validated.

    A failure at any step leaves the suggestion a `candidate`, which is §7.1's
    other fate and not a state to be embarrassed about: the waste is still
    recorded, the evidence is still on the row, and tomorrow's pass grades it
    again over a window that has moved. What must not happen is a row marked
    `validated` that nothing graded.
    """
    key = row.get("key")
    # Read back off the ROW rather than taken from the candidate, so that the
    # config's readback quotes the evidence that was actually stored beside
    # it. They are the same blob today — decision 1 only ever holds a
    # validated row's evidence, and this is reached on a candidate — and that
    # is exactly the invariant worth reading from the store rather than
    # assuming: §9 renders the two as one sentence.
    evidence = row.get("evidence") or {}
    scale = candidate.get("scale", 1.0)

    built = builder.build({**candidate, "evidence": evidence},
                          name=candidate.get("name", ""), scale=scale)
    if not built.get("ok"):
        logger.info(f"REVIEW: {key} produced no usable config — "
                    f"{str(built.get('error'))[:200]}")
        return False

    # entities[0] is whose rows these are — standby.scan read the record for
    # exactly that entity — while the config's guard was built from
    # evidence["entity"]. Handing over the first is what makes replay's check
    # of the second mean something; handing over the config's own answer would
    # be asking it to compare a value with itself.
    entities = candidate.get("entities") or []
    # `lookback_days` is not passed, which is decision 7 at the one call site
    # that could get it wrong: replay resolves the KIND'S own window from the
    # same detector the scan above took it from, so the rows being graded and
    # the window they are graded over come from one answer rather than two.
    graded, error = replay.run(
        built["config"], entities[0] if entities else None, candidate.get("rows"),
        built["kind"], scale=scale, evidence=evidence, now=now)
    if error:
        logger.info(f"REVIEW: {key} did not survive replay — {str(error)[:200]}")
        return False

    refusal = disqualified(graded["replay"], built["kind"])
    if refusal:
        logger.info(f"REVIEW: {key} was graded and refused — {refusal}")
        return False

    if not suggestions.mark_validated(row["id"], replay=graded["replay"],
                                      estimate=graded["estimate"], config=built["config"]):
        return False
    estimate = graded["estimate"]
    # In the grading's own nouns, so the line reads about the thing that was
    # graded: "30 night(s)" for a night rule and "64 occurrence(s)" for a
    # weekday one.
    fields = replay.fields_for(built["kind"]) or {}
    logger.info(f"REVIEW: {key} validated — about {estimate.get('kwh_per_month')} kWh/month "
                f"({estimate.get('basis')}), "
                f"{graded['replay'].get(fields.get('saved'))} "
                f"{fields.get('noun', 'firing')}(s) credited of "
                f"{graded['replay'].get(fields.get('examined'))}")
    return True


def _propose(now) -> int:
    """Puts the best validated DETECTOR suggestion to the user, if this run
    may. Returns how many were proposed.

    Analyst rows are counted out of both limits and out of the choice — they
    are `_digest`'s — and counted IN to `claimed_entities`, which is about the
    device rather than about who noticed it."""
    rows = suggestions.active(now=now)
    open_now = [r for r in rows if r.get("state") == "proposed" and not _is_analyst(r)]
    today = _start_of_local_day(now)
    # Counted from the rows rather than from a variable in this process:
    # mark_proposed stamps updated_at and nothing touches a proposed row
    # afterwards, so this survives the restart that a module global would not.
    proposed_today = sum(1 for r in open_now if int(r.get("updated_at") or 0) >= today)

    slots = proposals_allowed(len(open_now), proposed_today)
    if slots <= 0:
        return 0

    # ...and never a second question about a device that already has one, a
    # rule being measured, or a result waiting to be answered. See
    # claimed_entities: with two detectors the store's unique index no longer
    # says this on its own, because the key it is unique on carries the kind.
    claimed = claimed_entities(rows)
    ready = [r for r in rows
             if r.get("state") == "validated" and not _is_analyst(r)
             and not _claims(r) & claimed]

    done = 0
    for row in rank(ready)[:slots]:
        claims = _claims(row)
        if claims & claimed:
            # Raised a moment ago by this same loop. Unreachable while
            # MAX_PROPOSALS_PER_RUN is one, and cheaper to keep true here than
            # to remember on the day it is not.
            continue
        if _propose_one(row):
            claimed |= claims
            done += 1
    return done


def _digest(now) -> int:
    """The week's analyst digest, if it is due: up to DIGEST_SIZE ideas raised
    through the gate one by one, then one message listing them. Returns how
    many were raised.

    The digest is a cadence and a message, not an approval path. Each idea is
    `_propose_one` exactly as a detector row is — its own `change_id`, its own
    question, answered by `/rest/approve` or the card like any other — and the
    message only says they are there.

    Stamped BEFORE anything is raised, as the analyst stamps its run: a digest
    whose stamp could not be written is one a restart would hold again, so it
    is not held. A crash between the stamp and the last idea costs that week
    the ideas not yet raised; the other order would cost the household the
    same ideas asked twice. And the top-up is counted from the STORE, so even a
    lost stamp cannot put a fourth analyst question open.
    """
    rows = suggestions.active(now=now)
    waiting = [r for r in rows if r.get("state") == "validated" and _is_analyst(r)]
    weekday = digest_weekday(settings.DIGEST_DAY)
    if not digest_due(now, weekday, last_digest_at(now)):
        if waiting:
            logger.info(f"REVIEW: {len(waiting)} analyst idea(s) held for the digest on "
                        f"{DIGEST_WEEKDAYS[weekday].capitalize()}")
        return 0
    if not stamp_digest(now):
        logger.error("REVIEW: the digest is due but could not be stamped, so it is not "
                     "held — a restart would hold it a second time")
        return 0

    open_analyst = sum(1 for r in rows if r.get("state") == "proposed" and _is_analyst(r))
    slots = digest_allowed(open_analyst)
    claimed = claimed_entities(rows)
    raised = []
    for row in digest_order(waiting):
        if len(raised) >= slots:
            break
        claims = _claims(row)
        if claims & claimed:
            # A device with a question, a live rule or a result already — from
            # either source, or from an idea this loop raised a moment ago.
            # Reachable here, unlike in `_propose`: three at once can be two
            # ideas about one lamp. The next idea gets the slot.
            continue
        if _propose_one(row):
            claimed |= claims
            raised.append(suggestions.suggestion(row["id"]) or row)

    logger.info(f"REVIEW: digest — {len(raised)} analyst idea(s) raised of "
                f"{len(waiting)} waiting, {open_analyst} already open")
    if raised:
        activity.queue("analyst_digest", title=artefacts.ANALYST_DIGEST_TITLE,
                       body=artefacts.analyst_digest_text(raised),
                       payload={"suggestion_ids": [r.get("id") for r in raised],
                                "change_ids": [r.get("change_id") for r in raised]})
    return len(raised)


def last_digest_at(now):
    """When the last digest was held, as an epoch.

    A missing file is a fresh install or the first run of 2.24.0, and is
    written as held NOW: the first digest is the next digest day, not
    whichever review first reads no file — that would be the update's own
    restart. A file that cannot be read fails closed the same way, for the
    reason core/analyst.py's run file does: a crash loop over a corrupt file
    must not hold the digest on every restart. Either costs at most one week.
    """
    now = int(now)
    with _digest_lock:
        try:
            with open(DIGEST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("unexpected structure")
            held = data.get("last_at")
            if isinstance(held, bool) or not isinstance(held, (int, float)):
                raise ValueError("no last_at")
            return int(held)
        except FileNotFoundError:
            logger.info(f"REVIEW: no digest has been held yet — the first is the next "
                        f"{DIGEST_WEEKDAYS[digest_weekday(settings.DIGEST_DAY)].capitalize()}")
        except (OSError, ValueError, UnicodeDecodeError) as e:
            logger.warning(f"REVIEW: could not read {DIGEST_PATH} ({e}) — failing closed, "
                           f"this week's digest treated as held")
        _write_digest_locked(now)
        return now


def stamp_digest(now) -> bool:
    """Records that the digest is held now, BEFORE it is. Returns whether the
    stamp was written."""
    with _digest_lock:
        return _write_digest_locked(int(now))


def _write_digest_locked(now) -> bool:
    tmp = DIGEST_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(DIGEST_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_at": int(now)}, f)
        os.replace(tmp, DIGEST_PATH)
        return True
    except OSError as e:
        logger.error(f"REVIEW: failed to persist {DIGEST_PATH}: {e}")
        return False


def _propose_one(row) -> bool:
    """One suggestion, through the gate, linked back to its journal row."""
    config = row.get("config")
    if not isinstance(config, dict) or not config:
        logger.error(f"REVIEW: {row.get('key')} is validated with no config to propose")
        return False

    raised = gate.propose(PROPOSAL_TOOL, {"config_json": json.dumps(config)})
    if not raised.get("ok"):
        error = str(raised.get("error"))
        # The gate preflights again at propose time, days after the builder
        # did, and an entity that has disappeared in between is exactly what
        # that catches. The suggestion stays validated and is tried again
        # tomorrow, when the same refusal will either repeat or have resolved.
        logger.info(f"REVIEW: {row.get('key')} was not proposed — {error[:200]}")
        if row.get("source") == suggestions.SOURCE_ANALYST:
            # ...except an analyst row (proposal D-7). A detector row's config is
            # rebuilt from fresh evidence every morning, so tomorrow's attempt is
            # a different config; an analyst row's is fixed text that will fail
            # the same way, and would hold its place in the queue while doing
            # so. The auditor left this test-create to the gate (C-4); refused
            # here, the row is retired and the analyst may raise the idea afresh.
            if suggestions.retire(row["id"], error):
                logger.info(f"REVIEW: retired {row.get('key')} — the gate refused its config")
        return False

    change_id = raised["change_id"]
    if suggestions.mark_proposed(row["id"], change_id):
        logger.info(f"REVIEW: proposed {row.get('key')} as change {change_id}")
        return True

    # The change was raised and the suggestion could not be moved to match it.
    # Left alone that is the one thing the dedupe exists to prevent: the row is
    # still `validated`, so tomorrow's pass would propose the same waste again
    # and the user would hold two questions about one device. So the question
    # just raised is withdrawn, and tomorrow tries the whole thing again.
    logger.error(f"REVIEW: change {change_id} was raised but suggestion {row.get('id')} "
                 f"could not be marked proposed — withdrawing it")
    _withdraw_change(change_id, "the suggestion behind it could not be recorded as proposed")
    return False


def _withdraw_change(change_id, reason) -> bool:
    """Settles an open proposal so nobody can answer it — gate.withdraw, which
    says why it is claimed rather than written. Returns whether it was open."""
    return gate.withdraw(change_id, reason)


def _reconcile(now) -> int:
    """Suggestions whose change was answered while nobody was listening.
    Returns how many were moved.

    `settle` runs inside the request that answers a proposal, and a restart
    between gate.apply and that call would otherwise leave the suggestion at
    `proposed` for ever — against a question the journal says is settled, and
    holding one of the three open slots and its key with it. Nothing else would
    ever move it: the withdrawal rules below deliberately refuse to touch a row
    whose change has already been answered, because that answer is the user's.

    The same shape as gate.close_interrupted and
    activity.abandon_running_rollbacks, and the same argument: the journal
    survives the process, so what lost its process is reconciled from the
    journal rather than guessed at.
    """
    moved = 0
    for row in suggestions.active(now=now):
        if row.get("state") != "proposed" or not row.get("change_id"):
            continue
        change = activity.change(row["change_id"])
        if not change or change.get("decision") == "proposed":
            continue
        if not change.get("status"):
            # Claimed but not settled: gate.apply is executing this right now,
            # or a restart caught it mid-write and
            # activity.abandon_orphaned_approvals has not run yet. That pair is
            # "answered, outcome unknown" — the one state nobody may read as an
            # outcome — so it is left for the sweep that owns it.
            continue
        logger.info(f"REVIEW: change {row['change_id']} was answered but "
                    f"{row.get('key')} never heard — settling it now")
        if settle(row["change_id"], now=now):
            moved += 1
    return moved


def _withdraw(now) -> int:
    """Retires the suggestions that have stopped being worth holding, and
    withdraws the questions any of them had open. Returns how many."""
    retired = 0
    watched = set(watchlist.watched_entities())
    for row in suggestions.active(now=now):
        reason = withdrawal_reason(row, now, watched)
        if not reason:
            continue
        if row.get("state") == "proposed" and not _withdraw_change(row.get("change_id"), reason):
            # Answered while this ran. `settle` is about to move the row to
            # wherever the answer took it, and retiring it here would overwrite
            # a decision the user actually made.
            continue
        if suggestions.retire(row["id"], reason):
            logger.info(f"REVIEW: withdrew {row.get('key')} — {reason}")
            retired += 1
    return retired


# --------------------------------------------------------------------------
# the seam core/gate.py deliberately leaves open
# --------------------------------------------------------------------------
def settle(change_id, now=None) -> bool:
    """Moves the suggestion behind an agent-raised change to wherever the
    journal says that change ended up. Returns whether anything moved.

    Called after gate.apply, by the one path that can answer a change with no
    session behind it. The gate hands back a change_id and stops there — it
    does not know what a suggestion is and must not learn — so this is where
    the two records are joined.

    It reads the journal row rather than the tool result. `gate.apply` has
    already decided whether the write landed, and recorded it; a second reading
    of the same string here is a second opinion that can disagree with the one
    in the audit trail.

    A change nobody raised from a suggestion is the ordinary case — most of the
    journal is conversation — and answers False without complaint.

    `now` is the instant the baseline is measured to, and is handed in for the
    same reason every other clock in this module is: a measurement taken
    against a clock nobody passed cannot be tested against a record a test
    planted. The caller leaves it unset, which means the answer arrived now.
    """
    row = activity.change(change_id)
    if not row:
        return False
    suggestion = suggestions.for_change(change_id)
    if not suggestion:
        return False
    if suggestion.get("state") != "proposed":
        logger.info(f"REVIEW: suggestion {suggestion.get('id')} is already "
                    f"{suggestion.get('state')} — change {change_id} moved nothing")
        return False

    decision, status = row.get("decision"), row.get("status")
    now = int(now) if now else int(time.time())

    if decision == "proposed" or not status:
        # Not answered yet, or claimed and not yet settled — activity's
        # "answered, outcome unknown" pair, which is the one state nobody may
        # read as an outcome. It is reachable HERE and not only from
        # `_reconcile`: two callers can answer one proposal (a double-tapped
        # Apply), the loser's gate.apply executes nothing and returns, and this
        # runs while the winner is still inside the write. Reading that as a
        # failure would retire a suggestion whose automation is about to be
        # live in the home — leaving it with no baseline, no verify_after, and
        # its key free for tomorrow's pass to propose the same thing again.
        # Nothing is moved; whoever is writing settles it, and `_reconcile`
        # picks up whatever a restart left behind.
        logger.info(f"REVIEW: change {change_id} has no settled outcome yet — "
                    f"suggestion {suggestion.get('id')} left where it is")
        return False

    if decision == "approved" and status == "applied":
        # The instant the write actually landed, which is normally this one.
        # It is read off the journal rather than assumed, because `_reconcile`
        # reaches this function a day late for a change nobody was listening
        # to when it was answered — and both the baseline window and the week
        # to wait are counted from when the automation started running, not
        # from when this noticed.
        applied_at = int(row.get("decided_at") or now)
        # One figure, read once and used twice. The length of the "before" and
        # the wait before the "after" is taken are the same week by
        # construction rather than by two call sites agreeing — and it is read
        # HERE, where the kind is already known, which is what leaves every
        # suggestion already on disk with the seven-day promise it was made.
        # VERIFY_AFTER_DAYS is what this comes to for a rule that happens every
        # day.
        days = verify.days_for(suggestion)
        return suggestions.mark_applied(
            suggestion["id"],
            baseline=_baseline(suggestion, applied_at, now, days),
            verify_after=applied_at + days * 86400)

    if decision == "rejected":
        # The user's own "no", and the store's word for it: a cooldown rather
        # than a verdict. §12 counts these, which is also why nothing else in
        # this module is allowed to produce one.
        return suggestions.mark_dismissed(suggestion["id"])

    # Refused at apply time (drift, a config that no longer validates), or the
    # write failed. The change is closed and cannot be answered again, so a
    # suggestion left `proposed` would sit against a dead question and keep the
    # key occupied forever. Retiring frees it: tomorrow's pass raises the same
    # waste as a fresh candidate, and if the reason it failed is still true it
    # will fail again at the builder's preflight and never reach anyone.
    return suggestions.retire(
        suggestion["id"], f"change #{change_id} was not applied ({decision}/{status})")


def _baseline(suggestion, applied_at, measured_at, days=BASELINE_DAYS) -> dict:
    """The "before" §7.2's verification will be measured against.

    Captured at apply time and not at propose time, because from the moment the
    automation is written the record is being changed by it — mark_applied's
    own docstring makes that the reason it takes a baseline at all.

    The window ENDS when the change was applied rather than when this ran. The
    two are the same instant on the ordinary path, and are not when
    `_reconcile` gets here a day after a restart — and a "before" that included
    a night the automation was already running would be measuring the after,
    which is the one thing this exists to avoid. The record still holds those
    days, so reading the earlier window costs nothing.

    The measuring itself is core/verify.py's, and every word of "the same
    statistic, at the same scale, over the same length of week" is enforced by
    it being the same function that will take the after. A baseline computed
    here would be a second implementation with nothing stopping it drifting.

    Best effort, and it always answers with a blob. The change has already been
    written into someone's home; refusing to record a baseline because Home
    Assistant was slow to answer would leave the suggestion stuck at `proposed`
    with an automation live in the house — a worse lie than a baseline that
    says what it could not measure.

    `days` is handed in rather than read here for the same reason the measuring
    is not done here: it is the length the caller has already committed to by
    writing `verify_after`, and a second reading of it would be a second
    opinion about how long this suggestion's week is.

    No snapshot is handed over: this runs inside the request that answers a
    proposal, not inside the daily pass, so there is no registry read to share.

    A suggestion `verify.measurable` says has nothing to measure — an analyst
    row (proposal §5, B-5) — gets `{"outcome": "not_measured"}` and no read at
    all. Not a `measure_week` blob with an error in it: that shape means a
    measurement was attempted and failed, and the report a week later would
    then be about a failure that never happened. The week to wait is still
    `days`, which `verify.days_for` answers AFTER_DAYS for such a row.
    """
    if not verify.measurable(suggestion):
        return {"outcome": verify.OUTCOME_NOT_MEASURED}
    return verify.measure_week(suggestion, applied_at, days=days,
                               measured_at=measured_at)
