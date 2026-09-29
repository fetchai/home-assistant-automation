"""Design §9's artefacts: the shape of the messages, written once.

§9 gives the product five artefacts, "each rendered twice: rich and tappable on
the dashboard card, plain text and answerable in one word in ASI1". Four of
them are built here: the digest, the detail and the automation readback belong
to a suggestion that has been raised and not yet answered, and the verification
report belongs to one that was applied a week ago. The anomaly is Phase 3's.

Rendered HERE rather than in the panel, and the reason is not tidiness. §9's
rules are about what a rendering may claim:

    "Every suggestion shows its evidence. No number without the observation
     behind it"
    "The plain-text rendering is the design constraint, not the fallback. If it
     cannot be read and decided in a chat message, it does not ship"
    "One suggestion per approval ... never a single yes covering several
     changes"

A rule enforced in Python for ASI1 and again in JavaScript for the panel is a
rule with two implementations and one of them will drift. So the panel is sent
the rendered lines the same way `rest_activity` is sent `gate.describe_change`,
and the chat is sent the same lines joined into a message. The two surfaces
differ in layout, never in what they claim.

Two shapes, and the words that differ between them
--------------------------------------------------

A suggestion is a claim about a device inside a WINDOW, and there are two
detectors raising them: core/detectors/standby.py about 01:00-05:00 every
night, and core/detectors/schedule.py about a stretch of the day that names
its weekdays. The claim is the same shape either way — this device never
dropped below this level, on this many of the times the window came round —
and so is every sentence below. What differs is what the blobs call the counts
in them: a night blob says `nights`, `nights_examined`, `night_window`, and a
schedule blob says `occurrences`, `occurrences_examined`, `schedule_window`,
because "nights" is a lie about 11:00 on a Tuesday.

Nothing here holds a table of that. The detector that wrote the blob declares
its own names beside the code that writes them (`EVIDENCE_FIELDS`), the
grading's names are core/replay.py's and are asked of it (`fields_for`), and
this module asks whichever owns the fact — the same rule core/review.py's
decision 7 settled for the pass. `core/detectors.for_kind` is how the row's
`kind` becomes the module to ask; it is in the detectors' own package rather
than in the pass because the pass cannot be imported from here (it reaches
core/verify.py, and that imports this module).

What follows from it is the rule 2.14.5 stated for the pass, in a renderer's
terms: a field this cannot read means the clause is OMITTED. Never printed
with a blank in it, never with a zero standing in for a count nobody has, and
never with the night's word over another shape's number. A sentence that has
lost its observation is a sentence §9 would rather not have, and a card that
says a device "draws power overnight" about a 07:00-17:00 weekday window is
worse than one that says less.

The noun a household reads
--------------------------

One decision, and it is not a lookup: the card calls a schedule's firings
OCCURRENCES, the same word the grading uses, and takes it from
`replay.fields_for` rather than inventing a warmer one here.

"Day" is what a person would say and is the one word this may not use. The
same card already prints "Measured over 90 days" and "46 of those 90 days had
readable data" about the LOOKBACK, and a second meaning of the word one line
below turns "Days left out: 4" into a sentence about neither. "Weekday" reads
best of all and is false: the detector finds weekend runs too, and proposes
them.

What is left is the grading's word, and taking it is the point rather than the
concession. A household reading "13 occasions" beside a log line saying
`occurrences_saved: 13` has to work out that they are one count; two
vocabularies for one unit is exactly the drift "rendered once" is written
against. So there is no noun declared in this module, and there is no second
one anywhere.

The one judgement in here
-------------------------

`estimate.basis`. core/replay.py may only tighten a claim, so when its own
recomputed saving comes out larger than the detector's it reports the
DETECTOR's four figures instead and says so in `basis`. From the outside those
two estimates look identical — same keys, same units — and printing "the replay
found you would save 1.4 kWh a month" over a detector-sourced figure is a
sentence nobody can defend and nobody can reproduce.

So there is exactly one place in this module that reads `basis`
(`_measured_on`), and every number that leaves carries the counts belonging to
whichever side produced it: the replay's credited nights when the replay's
figure is being quoted, the detector's qualifying nights when the detector's
is. `replay_lines` then reports what the replay measured under its own heading,
never merged into the estimate.

Artefact 4 is where that matters most, and it is why `basis` was carried this
far. A verification report says "the estimate was X, the measurement is Y", and
naming the wrong side as the author of X is precisely the failure §7.1 spent a
module preventing. The verdict core/verify.py writes carries `estimate_basis`
as its own audit record; the SENTENCE still reads it through `_measured_on`,
because one place is one place.

What artefact 4 may claim
-------------------------

§7.2 constrains the claim rather than inflating it, and three of its four rules
are enforced in this module rather than argued for elsewhere:

- the comparison window is always stated, on both sides;
- the result is a RANGE or the sentence "too small to separate from noise".
  There is no third form. `verify.NOISE_FLOOR_WATTS` decides which, and this
  module never prints a bare midpoint without the range beside it;
- the rollback is offered whatever the number says, including when there is no
  number — and when it genuinely cannot be offered, the reason is
  core/rollback.py's own words rather than a second set of rules invented here.

The outcome names arrive as plain strings, like the detector's exclusion
reasons and the replay's outcomes above them. Importing core/verify.py for its
constants would be this module's only reason to know that module exists.

The conversational path
-----------------------

Modelled on core/rollback.py decision 4, deliberately and exactly, because the
same thing is true here: the model picks WHICH suggestion to put up, and a
model that could also answer would be approving a write into someone's home by
writing an argument.

    'Show a suggestion' returns a QUESTION, never an approval. The agent loop
    recognises it in the tool RESULT — a result is built by this process, so
    the model cannot forge one — `rest_chat` parks it in its own session slot
    (never `pending_approval`, which `gate.apply` answers), and the user's own
    next message is read by `agent_runtime.parse_approval_decision`, which
    matches the whole normalised message and does not use the LLM.

Where it differs from a rollback: the answer is not executed here. A rollback
is pre-authorised and `rollback.answer` runs it; a proposal is the opposite —
it is exactly the write nobody has approved yet — so the decision goes to
`/rest/approve`'s change_id path, the same `gate.apply` and the same
`review.settle` the panel's buttons reach. Two surfaces, one exit. That is also
what makes a chat "no" a COOLDOWN rather than a verdict: `review.settle` reads
the journal row and calls `suggestions.mark_dismissed`, and it would not be
reached at all by a path that answered the question itself.

A suggestion no detector raised
-------------------------------

Everything above is about a detector's row. A row the analyst raised (design
§15, proposal §5) has no blob in any detector's vocabulary, no replay and no
estimate — and the OMIT rule, applied to it, would drop every clause and
leave a card with nothing on it. That rule is right for a kind a NEWER build
wrote and wrong for a row this build wrote on purpose, so the branch is on
`source`, never on `kind`: every public renderer below that has something to
say about an analyst row tests `_is_analyst` first and hands the row to its
own path before `detectors.for_kind` is asked anything (B-1). A detector row
never reaches that path, which is what keeps its rendering byte-for-byte what
it was.

What the analyst path may claim is narrower than the detector path, and the
narrowing is the design:

- the EVIDENCE LABEL (design §15.3) is on the headline and in `detail`, in
  words — replayed, checked against your history, not tested. It is the
  auditor's and is read off the row; nothing here upgrades it.
- a *not tested* row opens with `UNTESTED_WARNING`, first line of the text
  form and its own field in `detail`, because §15.3 fixes that sentence.
- the only numbers are RECOUNTED ones (B-3). Evidence lines are built from
  claims whose `result` is `matched`, and print `recounted` — the figure the
  core counted — never `stated`, the one the model wrote. A claim nobody
  could check is stored and not shown as evidence at all.
- the model's `title`, `rationale` and claim `text` are DATA. They are
  printed as they are, flattened to a line where a line is what the slot
  holds, and never parsed, never searched for a number, never used to decide
  anything. The rationale sits under `REASONING_HEADING`, which says in the
  household's words that it is the agent's own reasoning and was not checked.
- the readback is the CONFIG (B-4): the alias, then the YAML — the form chat
  approvals already use. `builder.readback` describes only the shapes the
  builder emits, and a plain-language description of an arbitrary
  automation would be a second description nothing checks.
- there is no estimate and no basis. `_measured_on` is never asked about an
  analyst row, since both of its sides are detector-shaped.

A row from the explorer run (`evidence_level = 'shown'`, free-hand proposal
F-6, F-10) narrows it differently: no claims, so no evidence lines and no
*not tested* warning — its evidence is a chart a person judges, which the
headline points at ("suggested by the analyst — see the chart"). `why` is the
model's `finding`, and its `rule_text` sits under `RULE_HEADING` where an
audited row's rationale sits, both printed as the agent's words. The chart
itself is the panel's to draw (F-WP5); the text surfaces carry the words and
the YAML readback. What the panel prints AROUND the chart — the caption, the
sentence for a chart the record could not draw, a clamped window, markers or
why there are none, days left out, what was sent, the household's answer — is
`shown_detail`'s, for the reason above: those sentences are claims about the
evidence, and the panel draws the series and lays these out.

This module holds no store and makes no call. It renders rows it is handed and
builds a question out of one of them; reading the rows is the surface's job.
"""
from datetime import datetime, timezone
import json

import yaml

from core import builder
from core import detectors
from core import replay
from core.detectors import standby

# What a parked question is recognised by on its way from the tool through the
# agent loop to the REST handler. A third marker beside gate.APPROVAL_MARKER
# and rollback.CONFIRMATION_MARKER, and distinct from both for the reason
# rollback gives about its own: the three are answered by three different code
# paths, and one way to tell them apart is what keeps that true.
QUESTION_MARKER = "__suggestion_question__"

# And what a parked VERDICT question is recognised by. A fourth marker, for the
# fourth code path that answers one: `gate.apply` answers an approval,
# `rollback.answer` answers a rollback confirmation, `/rest/approve` answers a
# proposal, and `verify.answer` answers this. Same argument as the other three
# — the loop cannot return four kinds of thing without four ways to tell them
# apart, and a question answered by the wrong path is a write nobody asked for.
VERDICT_MARKER = "__verification_verdict__"

# How artefact 4's question is put, in the one form
# agent_runtime.parse_approval_decision can answer. §9 gives the user one word,
# and that function knows YES and NO and nothing else — deliberately, since it
# is what stands between a model and a write. "Keep" and "roll back" would need
# either a second vocabulary or an LLM to read it, so the choice is phrased as
# the yes/no it has to be, with both halves named so a one-word answer is
# unambiguous about what it agreed to.
VERDICT_PROMPT = "Reply YES to keep it, or NO to roll it back."

# How long a parked question stays answerable. The PROPOSAL is meant to wait —
# gate.pending_for deliberately gives it no expiry — but the parked chat
# question is not the proposal, it is one conversation's copy of it, and a
# stray "yes" hours later must not write an automation. Expiring it changes
# nothing about the suggestion: it is still open, still in the digest, still
# answerable from the panel or by asking again.
QUESTION_TTL_SECONDS = 600

# §9's digest is three lines per source. The same numbers bound
# review.MAX_OPEN_PROPOSALS (the detectors') and review.DIGEST_SIZE (the
# analyst's, free-hand F-12) for the same reason and are not imported from
# there: this is what a message may carry, those are what the pass may raise,
# and they answer to each other rather than one being derived from the other.
# Six rather than three since F-WP9 gave the two sources separate caps — at
# three, the listing ASI1 chat reads would hide up to three open questions
# the panel shows.
DIGEST_LINES = 6

# The weekly analyst digest's message (free-hand F-12, F-13). The card itself
# — chart, YAML, "what was sent" — is the panel's; this is its text and a
# pointer there.
ANALYST_DIGEST_TITLE = "This week's ideas from the analyst"
ANALYST_DIGEST_POINTER = ("Each one is waiting in the agent's panel, with the chart it is based "
                          "on: answer it there with Yes, No, or No, because…")

# The detector's own exclusion reasons, in words a person reads. A suggestion
# that says "no_coverage × 4" has shown its working to nobody. Anything not in
# here is printed as it comes, because a reason invented by a newer detector is
# still better than silence about four missing nights.
_REASONS = {
    "no_coverage": "the agent was not looking",
    "statistics": "only averaged data was available",
    "unavailable": "the sensor reported nothing",
    "below_floor": "the device really did switch off",
    "in_use": "the device was in use",
    "not_flat": "the draw moved too much to be a plateau",
}

# The replay's outcomes for the firings it did NOT credit, in the same voice.
# `saved` and `wrong` are stated in sentences of their own above these, so only
# the remainder is named here — but it IS named: a rule that would have failed
# its guard on six nights and one the agent simply could not read on six nights
# are different rules, and "ran on 24 of 30" says neither.
# No commas inside a phrase: these are joined into one clause, and a phrase
# with a comma in it turns a list of three reasons into a sentence of six.
# `{noun}` is the shape's own word for one firing — see THE NOUN A HOUSEHOLD
# READS. Only one phrase needs it, and the rest are formatted anyway rather
# than branched on, because a phrase that grows one later should not also have
# to find the place that decides whether to fill it in.
_OUTCOMES = {
    "not_fired": "the device was still drawing power",
    "no_credit": "the {noun} was too patchy to credit",
    "unreadable": "the check itself could not be read",
    "no_trigger": "the clock skipped that hour",
}


# Design §15.3's labels in the words a household reads. Keyed by the stored
# `evidence_level`; anything else reads as the weakest, because B-2 makes the
# label the weakest thing true of a row and an unknown level is not evidence
# of anything stronger.
_EVIDENCE_WORDS = {
    "replayed": "replayed",
    "checked": "checked against your history",
    "untested": "not tested",
    # The explorer run's (free-hand F-10): no label of trust, a pointer to the
    # evidence — the chart a person judges.
    "shown": "suggested by the analyst — see the chart",
}

# Fixed by design §15.3 for every *not tested* row, word for word.
UNTESTED_WARNING = ("I couldn't test this against your history — check it yourself "
                    "before approving.")

# What the model's rationale is printed under. It says what the text is — the
# agent's own reasoning — and what it is not, because a paragraph of fluent
# argument beside checked evidence reads as more evidence unless told otherwise.
REASONING_HEADING = "Why the agent suggests this (its own reasoning, not checked)"

# What a `shown` row's rule_text is printed under (free-hand F-6): the rule in
# the agent's own words, beside the YAML that is what would actually be written.
RULE_HEADING = "What it would do (the agent's words)"

# Artefact 4's sentence for a change with nothing to measure (proposal §5,
# B-5), in full, because it is the whole of the result.
NOT_MEASURED_SENTENCE = "The agent had nothing it could measure for this change."

# A shown row's chart (free-hand F-7, F-8), in the proposal's words. The error
# sentence opens with F-7's; the detail after the colon is core/charts.py's.
CHART_ERROR_SENTENCE = "The agent asked for a chart the record could not draw"
# A row stored before F-WP4 holds the request with `series: null` and no error:
# nothing was refused, nothing was drawn (charts.redraw draws it).
CHART_NOT_DRAWN = "This idea was stored before charts were drawn, so it has no chart yet."
NO_MARKERS_FOR_TYPE = "This kind of chart does not show what the rule would have done."
NO_MARKERS_IN_WINDOW = ("The rule would not have acted on the charted devices in this "
                        "window.")
# What `sent` is, and what it is not: every record read of the run, as the
# model received it (cut to 2 KB each). The run's opening message also
# crossed the boundary and is not in the list — said, not left to be assumed.
SENT_HEADING = "What was sent to the model for this"
SENT_NOTE = ("Every answer the agent received in this run, as it was sent. The run also "
             "started from an overview of the record and your existing automations.")

# The chart's name on the card, per core/charts.py type. A type not here is
# one a newer build drew, and its caption is omitted (the OMIT rule).
_CHART_NAMES = {
    "timeline": "Timeline",
    "overlay": "Overlay",
    "heatmap": "Changes by weekday and hour",
    "daily_count": "Changes per day",
}


def _now() -> int:
    """UTC seconds. Its own, like core/rollback.py's, so that this module can
    be read and tested without importing the gate and langchain behind it."""
    return int(datetime.now(tz=timezone.utc).timestamp())


def _count(value) -> str:
    """A count in a sentence: "none" rather than "only 0".

    Zero is the commonest answer in the branches that use this — a week nobody
    watched — and "only 0 of the 7 nights" reads as a machine filling a slot.
    """
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    return "none" if number <= 0 else f"only {number}"


def _number(value):
    """A stored figure as a number, for the places one is compared rather than
    printed — or None when it cannot be read as one.

    None rather than zero, and that is the OMIT rule at the one place a figure
    is arithmetic before it is words: a count this cannot parse is a count
    nobody has, and reading it as zero would print a measurement that says the
    agent saw nothing.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(value):
    """A stored number as text, with a trailing .0 dropped.

    Every figure here came out of a `round()` and lands in a sentence a person
    reads; "21.0 nights" is the kind of thing that makes a measurement look
    machine-made rather than measured.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _many(count, noun: str) -> str:
    """"7 nights", "1 night", "" when either half is missing.

    The singular is not a nicety. Every one of these counts can come out at
    one — a week with one readable night, a rule that fired once — and "the 1
    nights to 2026-09-15" is the sentence that tells a reader nobody looked at
    this.

    EXACTLY one, which is not the same test as rounding to one. Some of these
    are projections rather than tallies: `nights_per_month` on a device present
    for a fortnight of the window comes out at 1.5, and truncating it to decide
    the plural reads "1.5 night a month".
    """
    if not noun or count is None:
        return ""
    plural = "" if _number(count) == 1 else "s"
    return f"{_num(count)} {noun}{plural}"


# --------------------------------------------------------------------------
# which vocabulary a blob is written in
#
# TWO SHAPES in the module docstring, as the two lines every sentence below
# starts from. Neither of these holds a name: they ask the detector that wrote
# the blob and the module that graded it, and answer {} when either cannot be
# asked — which is the OMIT rule's one implementation.
# --------------------------------------------------------------------------
def _kind_of(row, blob=None) -> str:
    """Whose vocabulary this blob is written in.

    The BLOB's own kind first, and the row's only when it has none. Every
    measurement core/verify.py writes records the kind it was taken in, and
    the ones on disk without one pre-date the second detector — so a blob that
    says is read as it says, and a blob that does not is read as the
    suggestion holding it. The two disagree only about data nothing here can
    repair, and preferring the blob is what keeps a week measured as one shape
    from being printed in the other's words.

    Neither saying is the NIGHT, which is `verify.shape_of`'s reading of the
    same row and is deliberately not the OMIT rule: an UNKNOWN kind is a build
    that has no detector for it and every clause about it is dropped, while a
    MISSING one is a row from before there was a second shape and the only
    reading history allows is the one there was. The two modules have to agree
    about this or core/verify.py measures a week as a night and this renders
    the report of it with the evidence stripped off — a saving claimed with no
    observation behind it, which is the one thing §9 forbids.
    """
    return str((blob or {}).get("kind") or (row or {}).get("kind") or standby.KIND)


def _measured_words(row, blob=None) -> dict:
    """What the DETECTOR's blob calls its counts, plus the noun — or {}.

    The evidence behind a proposal and the two weeks behind a verification are
    both written by the detector's own measurement function, so one
    declaration answers for both. The noun comes from core/replay.py for the
    reason THE NOUN A HOUSEHOLD READS gives, and a kind either module cannot
    name answers {} rather than a half-filled dict: a caller that got some of
    the names would print some of the sentence.

    `EVIDENCE_FIELDS["window"]` names the field in the DETECTOR's blob and is
    not the one to use on a verification measurement — core/verify.py writes
    the window's label under `night_window` whichever shape took it, so that
    two weeks can be compared, and `verification_windows` reads that name.
    """
    kind = _kind_of(row, blob)
    detector = detectors.for_kind(kind)
    graded = replay.fields_for(kind) or {}
    if detector is None or not graded.get("noun"):
        return {}
    return {**detector.EVIDENCE_FIELDS, "noun": graded["noun"]}


def _graded_words(row) -> dict:
    """...and what core/replay.py's blob calls its counts, plus the same noun.

    Asked of the module that wrote them rather than restated, which is what
    `replay.fields_for` is public for. The estimate's four figures are named
    in this vocabulary too — `hours_per_night` and `nights_per_month` on one
    shape, `hours_per_occurrence` and `occurrences_per_month` on the other.
    """
    return replay.fields_for(_kind_of(row)) or {}


# --------------------------------------------------------------------------
# whose number is this
# --------------------------------------------------------------------------
def _measured_on(row) -> dict:
    """{"source", "counted", "examined"} — the observation the headline figure
    is entitled to quote.

    The only place `estimate.basis` is read. core/replay.py hands back the
    detector's figures whenever its own came out higher, so the counts printed
    beside the number have to come from the same side or the sentence is two
    measurements pretending to be one.

    Each side is read in its OWN vocabulary as well as from its own blob: the
    detector's counts through the names it declared, the grading's through
    `replay.fields_for`. They are not the same names — a night blob's
    `nights_examined` is an evidence count and the replay's is a grading count
    — and reading either through the other's is the quiet failure both tables
    exist to prevent.
    """
    estimate = row.get("estimate") or {}
    if estimate.get("basis") == "detector":
        evidence = row.get("evidence") or {}
        words = _measured_words(row)
        return {
            "source": "detector",
            "counted": evidence.get(words["qualified"]) if words else None,
            "examined": evidence.get(words["examined"]) if words else None,
        }
    replayed = row.get("replay") or {}
    words = _graded_words(row)
    return {
        "source": "replay",
        "counted": replayed.get(words["saved"]) if words else None,
        "examined": replayed.get(words["examined"]) if words else None,
    }


_SOURCE_PHRASE = {
    "replay": "replaying the rule against the record",
    "detector": "the measurement itself — the replay's own figure came out "
                "higher, so the smaller one is quoted",
}


def label_for(row) -> str:
    """What to call this suggestion's device, in the household's own words.

    The alias core/builder.py wrote, because that is the name the household
    will see in Home Assistant once the automation exists, and a suggestion
    that calls it one thing and the automation another is two names for one
    device. The entity id is the fallback and never the preference.

    The suffix is taken off by `builder.label_of`, which knows all of them.
    Stripping the night's by hand here read a schedule automation's alias
    whole and called the device "Office printer off on a schedule".
    """
    if _is_analyst(row):
        return _analyst_label(row)
    alias = builder.label_of(row.get("config"))
    if alias:
        return alias
    entities = row.get("entities") or []
    evidence = row.get("evidence") or {}
    return str(evidence.get("switch") or (entities[1] if len(entities) > 1 else "")
               or evidence.get("entity") or (entities[0] if entities else "a device"))


# --------------------------------------------------------------------------
# artefact 1 — the digest
# --------------------------------------------------------------------------
def headline(row) -> str:
    """One suggestion as one line carrying one number and its observation.

    §9 asks the digest for "one line and one number each". The number is the
    kWh, because that is the claim; what stops it being a bare number is the
    firings it was counted on, and those come from `_measured_on` rather than
    from whichever count is largest.

    The window is on the end of every one of these, and it is what carries a
    digest holding two claims about one television: 01:00-05:00 every night
    and 07:00-17:00 on weekdays are two questions, and a line that named
    neither would leave "yes" meaning either. Without a kWh the line says what
    was found rather than a figure nobody measured — and that sentence names
    no hours of its own, because the window clause beside it already does.
    """
    if _is_analyst(row):
        return _analyst_headline(row)
    estimate = row.get("estimate") or {}
    kwh = estimate.get("kwh_per_month")
    measured = _measured_on(row)
    counted, examined = measured["counted"], measured["examined"]
    words = _measured_words(row)
    line = f"{label_for(row)}: "
    line += (f"about {_num(kwh)} kWh a month" if kwh is not None
             else "power drawn while it is idle")
    if words and counted is not None and examined:
        line += f", from {_num(counted)} of {_many(examined, words['noun'])} measured"
    window = (row.get("evidence") or {}).get(words["window"]) if words else None
    if window:
        line += f" ({window})"
    return line


def digest(rows) -> dict:
    """§9 artefact 1: the open proposals, at most three per source.

    Not a list of everything the store holds. The rows arrive ranked by the
    pass that raised them, and a line past DIGEST_LINES is one
    review.MAX_OPEN_PROPOSALS and review.DIGEST_SIZE were written to make
    impossible — so one here would mean something has gone wrong upstream,
    and truncating says so quietly rather than printing a digest nobody asked
    for.
    """
    rows = list(rows or [])[:DIGEST_LINES]
    return {
        "count": len(rows),
        "suggestions": [
            {
                "suggestion_id": row.get("id"),
                "change_id": row.get("change_id"),
                "line": headline(row),
            }
            for row in rows
        ],
    }


def digest_text(rows) -> str:
    """The same lines as one chat message."""
    rows = list(rows or [])[:DIGEST_LINES]
    if not rows:
        return "The agent has not raised any suggestions that are waiting for an answer."
    lines = [f"{i}. {headline(row)}" for i, row in enumerate(rows, start=1)]
    return "\n".join(["Waiting for your answer:"] + lines)


def analyst_digest_text(rows) -> str:
    """The weekly digest's message: each idea's card text — what it is, what
    was found, what it would do — and where to answer it (F-13).

    Numbered, but not a question. Every idea is its own proposal with its own
    change_id, answered one at a time; a numbered list that invited "yes" to
    it would be the single yes covering several changes §9 forbids. So the
    message asks nothing and points at the panel, where each card has its own
    buttons.
    """
    blocks = []
    for i, row in enumerate(list(rows or [])[:DIGEST_LINES], start=1):
        lines = [f"{i}. {_flat(row.get('title')) or _analyst_label(row)}"]
        finding = _flat(row.get("finding"))
        if finding:
            lines.append(f"   {_sentence(finding)}")
        _, rule = _analyst_words(row)
        if rule:
            lines.append(f"   It would: {_flat(rule)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks + [ANALYST_DIGEST_POINTER])


# --------------------------------------------------------------------------
# artefact 2 — the detail
# --------------------------------------------------------------------------
def why(row) -> str:
    """What was found, before any claim is made about fixing it.

    The sentence §9 forbids getting wrong, and the one this module was left
    night-shaped for a release: the reason a device is being proposed for is
    the hours it sat idle, and those hours come off the blob under the raising
    detector's own name for them. "Draws power overnight" about a 07:00-17:00
    weekday window is a true sentence about a different claim.

    The fallback names no window and no hours, because it is reached exactly
    when there are none to name — a blob missing its level or its window, or a
    kind this build has no detector for. It still says what was found.
    """
    if _is_analyst(row):
        return _analyst_why(row)
    evidence = row.get("evidence") or {}
    words = _measured_words(row)
    watts = evidence.get("watts")
    window = evidence.get(words["window"]) if words else None
    if watts is None or not window:
        return f"{label_for(row)} appears to be drawing power while it is not being used."
    qualified, examined = evidence.get(words["qualified"]), evidence.get(words["examined"])
    seen = (f" on {_num(qualified)} of the {_many(examined, words['noun'])} examined"
            if qualified and examined else "")
    return (f"{label_for(row)} never dropped below about {_num(watts)} W between "
            f"{window}{seen}, which is standby draw rather than use.")


def evidence_lines(row) -> list:
    """The observations behind the numbers, one statement each.

    Everything here is read off the stored evidence blob and nothing is
    recomputed: core/suggestions.py stores the evidence precisely so that what
    the user reads is what was measured, and not what a record that has moved
    on since would say today.
    """
    if _is_analyst(row):
        return _analyst_evidence(row)
    evidence = row.get("evidence") or {}
    words = _measured_words(row)
    noun = words.get("noun", "")
    lines = []
    # `window`, `covered_days`, `watts_range` and `excluded` are the same names
    # in both shapes and are read directly; only the word for one firing
    # differs, and the two clauses that need it are omitted without it.
    window = evidence.get("window") or {}
    if window.get("from") and window.get("to"):
        lines.append(f"Measured over {_num(window.get('days'))} days, "
                     f"{window['from']} to {window['to']}.")
    covered = _number(evidence.get("covered_days"))
    span = _number(window.get("days"))
    if covered is not None and span:
        # Both detectors count the calendar DATES the record holds a row on,
        # and a window that opens mid-afternoon touches one more date than it
        # spans — so this can arrive as 91 out of 90, which reads as a mistake
        # and is only a boundary. Capped at the window, where it means what it
        # says: every day of it had data.
        #
        # Both figures go through `_number`, which answers None for a value it
        # cannot read — and that omits the clause rather than capping a zero
        # into it. A stored count nothing can parse is a count nobody has, and
        # "0 of those 30 days had readable data" is the most alarming sentence
        # this module could invent out of one.
        lines.append(f"{_num(min(covered, span))} of those {_num(span)} days "
                     "had readable data.")
    low_high = evidence.get("watts_range") or []
    if len(low_high) == 2 and noun:
        # "The lowest reading on each night", not "the nightly minimum": the
        # adjective has no form the other shape can borrow, and the phrase that
        # spells the statistic out is the one a reader can check against the
        # figure beside it.
        lines.append(f"The lowest reading on each {noun} ranged from {_num(low_high[0])} W "
                     f"to {_num(low_high[1])} W; {_num(evidence.get('watts'))} W is the "
                     "middle of them.")
    idle = evidence.get("idle_watts")
    active_floor = evidence.get("active_floor")
    if idle is not None and active_floor is not None:
        # Both or neither. Every blob written before the detector learned to
        # measure the device's two levels lacks them, and half of this sentence
        # would claim a level nobody measured.
        lines.append(f"Across the whole window it sat around {_num(idle)} W and was "
                     f"seen above {_num(active_floor)} W as well, so it has an in-use "
                     "level and this is not it.")
    excluded = evidence.get("excluded") or {}
    if excluded and noun:
        # Largest first: the reason that cost the most of them is the one a
        # reader disagreeing with the conclusion would want first, and the
        # alphabetical order of an internal key name means nothing to them.
        # The reasons themselves are the detector's own and say nothing about
        # nights, which is why both shapes' exclusions read through one table.
        parts = [f"{_num(count)} × {_REASONS.get(reason, reason)}"
                 for count, reason in sorted(
                     ((count, reason) for reason, count in excluded.items()),
                     key=lambda pair: (-pair[0], pair[1]))]
        lines.append(f"{noun.capitalize()}s left out: " + "; ".join(parts) + ".")
    return lines


def estimate_sentence(row) -> str:
    """The claim, in one sentence, with the side it came from named.

    core/replay.py's `estimate` is taken WHOLE from one side and its four
    figures multiply out together — so they are printed together, and the
    breakdown is never assembled from two blobs.

    Two of the four are named in the grading's vocabulary — `hours_per_night`
    and `nights_per_month` on one shape, `hours_per_occurrence` and
    `occurrences_per_month` on the other — so they are asked for by the name
    the kind gives them. Read by the night's names alone a schedule estimate
    loses its breakdown entirely and the sentence becomes the bare kWh §9 is
    written against.
    """
    if _is_analyst(row):
        # No estimate exists, and "no saving can be estimated from what was
        # measured" would claim a measurement that never happened.
        return ""
    estimate = row.get("estimate") or {}
    kwh = estimate.get("kwh_per_month")
    if kwh is None:
        return "No saving can be estimated from what was measured."
    words = _graded_words(row)
    watts = estimate.get("watts")
    hours = estimate.get(words["hours"]) if words else None
    per_month = estimate.get(words["per_month"]) if words else None
    breakdown = ""
    if watts is not None and hours is not None and per_month is not None:
        breakdown = (f"about {_num(watts)} W for {_num(hours)} hours on "
                     f"{_many(per_month, words['noun'])} a month, which is ")
    source = _SOURCE_PHRASE.get(_measured_on(row)["source"], "")
    return (f"Estimated saving: {breakdown}roughly {_num(kwh)} kWh a month"
            + (f" (from {source})." if source else "."))


def replay_lines(row) -> list:
    """What the grading actually found, under its own heading.

    Kept apart from the estimate on purpose. These counts are the replay's
    whatever `basis` says, and the one number that matters most is the third:
    review.MAX_WRONG_NIGHTS is zero, so a proposal that reached a user has a
    zero here — and saying so is the difference between "we think this is safe"
    and "the record contains no night on which this would have been wrong".

    Each clause is guarded on its own field rather than the whole block on the
    vocabulary, which is 2.14.5's OMIT rule read exactly: the guard's
    threshold is one number under one name in both shapes, and a kind this
    build cannot name the counts of has not stopped it being true.
    """
    if _is_analyst(row):
        return []
    replayed = row.get("replay") or {}
    if not replayed:
        return []
    words = _graded_words(row)
    noun = words.get("noun", "")
    examined = replayed.get(words["examined"]) if words else None
    lines = []
    if examined:
        lines.append(
            f"Replayed against the same {_many(examined, noun)}: the rule would have run on "
            f"{_num(replayed.get(words['fired']))}, saved on "
            f"{_num(replayed.get(words['saved']))}.")
    wrong = replayed.get(words["wrong"]) if words else None
    if wrong is not None:
        # "before the power came back", not "before morning": the claim is
        # that the cut itself was the problem, and a daytime rule's cut ends
        # at five in the afternoon.
        lines.append(
            f"On {_num(wrong)} of them it would have cut power to a device that was in use "
            "again before the power came back."
            if wrong else
            f"There is no {noun} in the record on which it would have cut power to a device "
            "that was then used.")
    rest = _unclaimed(replayed, noun)
    if rest:
        lines.append(f"The remaining {noun}s: " + "; ".join(rest) + ".")
    guard = replayed.get("guard") or {}
    if guard.get("below_w") is not None:
        lines.append(f"It only switches off below {_num(guard['below_w'])} W, so a device "
                     "still in use at that moment is left alone.")
    return lines


def _unclaimed(replayed, noun: str) -> list:
    """The firings the replay examined and did not credit, each with its
    reason — or nothing at all when the shape's noun is unknown, since one of
    the phrases is written about it.

    §9's rule applied to the grading the same way `evidence_lines` applies it
    to the measurement: "ran on 24 of 30, saved on 21" leaves nine nights
    unexplained, and the blob has already said what happened on every one of
    them. Ordered by count so the largest omission is read first, and `saved`
    and `wrong` are left out because the two sentences above state them.
    """
    if not noun:
        return []
    outcomes = replayed.get("outcomes") or {}
    counted = [(count, _OUTCOMES[name].format(noun=noun))
               for name, count in outcomes.items()
               if name in _OUTCOMES and count]
    return [f"{_num(count)} × {phrase}"
            for count, phrase in sorted(counted, key=lambda pair: -pair[0])]


# --------------------------------------------------------------------------
# artefact 3 — the readback
# --------------------------------------------------------------------------
def readback(row) -> str:
    """What will actually be written, in plain language.

    core/builder.py's own sentence, rebuilt from the config stored on this row
    rather than restated here — the alternative is a second description of one
    automation, which is the thing §9 asks the readback to prevent.

    The ceiling is handed over from the REPLAY's `guard.below_w` when the row
    has one, because that is the config's own threshold converted to watts by
    the engine that graded it — the same number `replay_lines` prints a
    paragraph above. Without it the builder falls back to the detector's live
    constant, and a constant that moved between the proposal and the answer
    would put two different thresholds for one automation in one message.
    """
    if _is_analyst(row):
        return _analyst_readback(row)
    guard = (row.get("replay") or {}).get("guard") or {}
    return builder.readback(row.get("config"), row.get("evidence"),
                            ceiling_w=guard.get("below_w"))


# --------------------------------------------------------------------------
# artefact 4 — the verification report
#
# §7.2's honest part, rendered. core/verify.py has already decided what may be
# claimed; nothing here re-derives a figure, and nothing here decides whether a
# difference is real. What this owns is the sentence, and the rule that the
# sentence never says more than the verdict does.
# --------------------------------------------------------------------------

# The outcome of a verification, as the one clause the headline turns on.
# Plain strings rather than core/verify.py's constants, for the reason _REASONS
# and _OUTCOMES above are: this module renders what it is handed and does not
# import the modules that produce it.
_VERDICTS = {
    "saved": "less draw in that window",
    "no_change": "no measurable change",
    "worse": "MORE draw in that window than before",
    "unmeasured": "could not be measured",
    "no_baseline": "cannot be compared",
    "not_running": "the rule was not running",
    "not_measured": "nothing the agent could measure",
}


def _window_phrase(measured, words) -> str:
    """"the 7 nights to 2026-07-08", or "" when the blob does not say.

    The count is how many of the window's firings that week OFFERED, read off
    the measurement in its own vocabulary — never the length of the week in
    days. For a night the two are one number, which is why this was written as
    one; for a Mon-Fri rule they are ten occurrences over a fortnight, and
    printing "the 14 occurrences" would be the length of the week wearing the
    unit of the claim.

    A week that could not be measured at all has no count, and falls back to
    the span in days: it is the one thing such a blob still knows, and it is
    the half a reader would check. "The week to X" is what used to stand here
    and is gone — 2.14.4 made the after-length per suggestion, so seven days
    is now an assumption rather than a constant, and a fortnight reported as a
    week is a claim nobody measured.
    """
    window = (measured or {}).get("window") or {}
    if not window.get("to"):
        return ""
    examined = (measured or {}).get(words["examined"]) if words else None
    if examined:
        return f"the {_many(examined, words['noun'])} to {window['to']}"
    days = window.get("days")
    return f"the {_many(days, 'day')} to {window['to']}" if days else f"the period to {window['to']}"


def _span_phrase(measured) -> str:
    """"the week", "the 14 days", "the period" — how long the before ran for.

    Seven days is allowed to be called a week and nothing else is. §7.2 states
    the comparison window on both sides, and a fortnight of a weekday rule
    described as "the week before the change" states a length that was not
    measured — which is the one thing this sentence exists to prevent.
    """
    days = ((measured or {}).get("window") or {}).get("days")
    if not days:
        return "the period"
    try:
        return "the week" if int(days) == 7 else f"the {_num(days)} days"
    except (TypeError, ValueError):
        return "the period"


def _week_phrase(measured) -> str:
    """"that week", "those 14 days" — the same length, pointed at.

    `_span_phrase` names the stretch and this one refers back to it, and both
    exist for the same reason: §7.2 measures a WEEK of a nightly rule and a
    fortnight of a Mon-Fri one, so "that week" is a sentence that has to earn
    its noun. "That stretch" when the blob cannot say how long it was, rather
    than the week it probably was.
    """
    days = ((measured or {}).get("window") or {}).get("days")
    if not days:
        return "that stretch"
    try:
        return "that week" if int(days) == 7 else f"those {_num(days)} days"
    except (TypeError, ValueError):
        return "that stretch"


def _range_phrase(verdict, magnitude: bool = False) -> str:
    """The saving as §7.2 allows it to be stated: a range, and never a bare
    midpoint.

    `verify.compare` records a midpoint too, for a reader who wants one number.
    It is deliberately not printed on its own anywhere: the whole reason there
    is a range is that the two ends are the honest bound, and a single figure
    quoted from between them is exactly the confident number §7.2 says would
    damage trust faster than reporting nothing.

    `magnitude` drops the sign, and exists for the one sentence that carries it
    in the words instead: a range on the `worse` outcome is negative, and "drew
    MORE overnight than before, by between -1.32 and -1.08 kWh a month" states
    the opposite of the clause it sits in. Taking the size of each end also
    swaps their order, so they are re-sorted rather than negated in place.
    """
    span = (verdict or {}).get("saved_range_kwh_per_month") or []
    if len(span) != 2:
        return ""
    ends = sorted(abs(end) for end in span) if magnitude else list(span)
    low, high = round(ends[0], 2), round(ends[1], 2)
    if low == high:
        return f"about {_num(low)} kWh a month"
    return f"between {_num(low)} and {_num(high)} kWh a month"


def verification_headline(row) -> str:
    """One line: what a week of measurement came to, and over what.

    A saving leads with the range, because that is the claim and §9's digest
    asks a line for "one number". Every other outcome leads with the words,
    because there is no number to lead with and inventing one is the whole
    thing §7.2 is written against.
    """
    verdict = row.get("verdict") or {}
    outcome = str(verdict.get("outcome") or "")
    span = _range_phrase(verdict) if outcome == "saved" else ""
    said = f"{span} less" if span else _VERDICTS.get(outcome, outcome or "measured")
    line = f"{label_for(row)}: {said}"
    after = verdict.get("after") or {}
    window = _window_phrase(after, _measured_words(row, after))
    if window:
        line += f" over {window}"
    return line


def verification_windows(row) -> str:
    """§7.2's "compare like periods, and state the comparison window", as the
    sentence that states it.

    Both windows, always, and in the order they happened. A before/after with
    only one date on it is a number the reader cannot check.
    """
    verdict = row.get("verdict") or {}
    after_blob = verdict.get("after") or {}
    before_blob = verdict.get("before") or row.get("baseline") or {}
    after = _window_phrase(after_blob, _measured_words(row, after_blob))
    before = _window_phrase(before_blob, _measured_words(row, before_blob))
    # `night_window` whichever shape took the measurement: core/verify.py
    # writes the window's own label under that one name in both, deliberately,
    # because it is what `_like_periods` compares across two blobs a week
    # apart. It is NOT the detector's `EVIDENCE_FIELDS["window"]`, which names
    # the field in the blob the DETECTOR wrote.
    night = before_blob.get("night_window") or ""
    if not before:
        return ""
    span = _span_phrase(before_blob)
    if not after:
        return f"{span.capitalize()} before the change was {before}."
    # "Comparing", not "Measured over": this sentence is printed for a week
    # that could not be read as well as for one that could, and the second half
    # of the report is the only thing entitled to say a measurement happened.
    return (f"Comparing {after} with {before} — {span} before the change was "
            f"applied" + (f", both between {night}." if night else "."))


def verification_evidence(row) -> list:
    """What each of the two weeks actually showed, one statement each.

    §9's rule applied to a before and an after: no number without the
    observation behind it, on BOTH sides. The counts come off the two
    measurement blobs and nothing is recomputed here — core/verify.py measured
    them with the detector's own function and stored what it got.
    """
    if _is_analyst(row):
        return []
    verdict = row.get("verdict") or {}
    before = verdict.get("before") or row.get("baseline") or {}
    after = verdict.get("after") or {}
    # Whether the after week counts as MEASURED is core/verify.py's judgement,
    # already made and recorded as the outcome. Re-deriving it here from the
    # counts would be a second implementation of `readable` — and the one that
    # would get it wrong, since a week with one readable night and a week with
    # seven look identical in every field except the one this must not
    # reinterpret.
    measured = str(verdict.get("outcome") or "") in ("saved", "no_change", "worse")
    label = label_for(row)
    lines = []
    # Each week in the vocabulary IT was written in, read off the blob rather
    # than off the row: they are one shape in every case a home can produce,
    # and asking each blob is what keeps one sentence from being printed in the
    # other's words if they ever are not. A week whose kind cannot be named has
    # no counts that may be printed, and §9 would rather say nothing.
    before_words = _measured_words(row, before)
    after_words = _measured_words(row, after)

    watts = before.get("watts")
    if watts is not None and before_words:
        lines.append(f"Before: {label} never dropped below about {_num(watts)} W on "
                     f"{_num(before.get(before_words['qualified']))} of the "
                     f"{_many(before.get(before_words['examined']), before_words['noun'])}"
                     f" in {_week_phrase(before)}.")
    elif before and before_words:
        lines.append(f"Before: {_week_phrase(before)} could not be measured — "
                     f"{_count(before.get(before_words['observed']))} of its "
                     f"{_many(before.get(before_words['examined']) or 0, before_words['noun'])}"
                     " could be read.")

    if not after_words:
        return lines
    if after.get("watts") is not None:
        lines.append(f"After: it still never dropped below about {_num(after['watts'])} W, "
                     f"on {_num(after.get(after_words['qualified']))} of the "
                     f"{_many(after.get(after_words['examined']), after_words['noun'])}.")
    elif measured and after.get(after_words["observed"]):
        # The successful shape, and the one that is easiest to render wrongly:
        # nothing qualified as a plateau BECAUSE the device was switched off.
        # `_REASONS['below_floor']` is the detector's own words for that, used
        # here so the two surfaces describe one observation the same way.
        lines.append(f"After: {_REASONS['below_floor']} on all "
                     f"{_num(after.get(after_words['observed']))} of the "
                     f"{_many(after.get(after_words['examined']), after_words['noun'])}"
                     " that could be read.")
    elif after:
        lines.append(f"After: {_count(after.get(after_words['observed']))} of the "
                     f"{_many(after.get(after_words['examined']) or 0, after_words['noun'])}"
                     f" {_week_phrase(after)} could be read at all.")
    return lines


def verification_result(row) -> str:
    """The claim, in one sentence — or the sentence that says there is none.

    The four things §7.2 permits: a range, "too small to separate from noise",
    "measurably worse", or a reason there is no measurement. Nothing else is
    reachable from here, which is the point: there is no branch in this
    function that prints a confident single figure.
    """
    verdict = row.get("verdict") or {}
    outcome = str(verdict.get("outcome") or "")
    floor = verdict.get("floor_kwh_per_month")
    span = _range_phrase(verdict)

    if outcome == "saved":
        watts = verdict.get("saved_watts")
        # "in that window", never "overnight": the window is named in the
        # sentence above this one and is 07:00-17:00 on weekdays as often as
        # it is the small hours.
        detail = f" — about {_num(watts)} W less in that window" if watts is not None else ""
        return f"Measured result: {span} less{detail}."
    if outcome == "no_change":
        return ("Measured result: nothing that can be separated from noise. The difference "
                f"between the two weeks is smaller than {_num(floor)} kWh a month, which is "
                "the least this measurement can tell apart, so no saving is claimed.")
    if outcome == "worse":
        # The magnitude, because "MORE ... than before" already carries the
        # direction and a signed figure after "by" would state the opposite.
        return (f"Measured result: the device drew MORE in that window than before, by "
                f"{_range_phrase(verdict, magnitude=True)}. That is not what was expected, "
                "and rolling the change back is the straightforward answer.")
    if outcome == "not_measured":
        # B-5. The whole result: no number, no reason to retry, and not the
        # "could not be read" sentence below — nothing failed to be read.
        return NOT_MEASURED_SENTENCE
    reason = str(verdict.get("reason") or "")
    if not reason:
        return "No measured result, and no reason recorded for that."
    if _is_analyst(row):
        # An analyst row never claimed a saving, so the clause below denying
        # one would be answering a claim nobody made.
        return f"No measured result: {reason}."
    tried = ""
    try:
        if outcome == "unmeasured" and int(verdict.get("attempts") or 1) > 1:
            tried = f" The agent tried {_num(verdict.get('attempts'))} times."
    except (TypeError, ValueError):
        tried = ""
    # Never "it saved nothing". core/verify.py refuses to zero an empty
    # measurement for exactly this sentence's sake, and the sentence has to
    # honour it: a week nobody could read is not a week in which nothing
    # happened.
    return (f"No measured result: {reason}.{tried} No saving is claimed either way — "
            "a week that could not be read is not a week in which nothing was saved.")


def verification_estimate(row) -> str:
    """What was claimed when the user said yes, and whose claim it was.

    The one sentence artefact 4 exists to make possible. `_measured_on` is
    still the only place `basis` is read; core/verify.py's copy of it on the
    verdict is the audit record, not a second source for this sentence.
    """
    estimate = (row.get("estimate") or {}).get("kwh_per_month")
    if estimate is None:
        return ""
    source = _SOURCE_PHRASE.get(_measured_on(row)["source"], "")
    return (f"When this was proposed the estimate was about {_num(estimate)} kWh a month"
            + (f", from {source}." if source else "."))


def verification_choice(row, why_not: str = "") -> str:
    """The rollback, offered whatever the number said.

    §7.2's fourth rule, and the one with no exception in it: a verification
    that only offered an undo when the news was bad would be an agent grading
    its own homework. `why_not` is core/rollback.py's own refusal for the
    change behind this suggestion — asked of the same `_check` the panel and
    the chat ask, so the user is never offered an undo that would then be
    refused, and never told an undo is impossible for a reason invented here.
    """
    if why_not:
        return (f"There is no rollback to offer for this one: {why_not} Keeping it is "
                "the only answer, unless you edit the automation in Home Assistant "
                "yourself.")
    return ("Keep it, or roll it back — the agent recorded how to undo this when it was "
            "applied, and will remove the automation it added.")


def verification(row, why_not: str = "") -> dict:
    """§9 artefact 4 as fields: what the panel lays out and the chat joins into
    a message. One dict for both, like `detail`."""
    verdict = row.get("verdict") or {}
    return {
        "suggestion_id": row.get("id"),
        "change_id": row.get("change_id"),
        "state": row.get("state"),
        "label": label_for(row),
        "outcome": verdict.get("outcome"),
        "headline": verification_headline(row),
        "windows": verification_windows(row),
        "evidence": verification_evidence(row),
        "result": verification_result(row),
        "estimate": verification_estimate(row),
        "choice": verification_choice(row, why_not),
        # The panel disables the button rather than offering an undo that would
        # be refused; the sentence above says why in the same breath.
        "undoable": not why_not,
        "why_not": why_not,
        "basis": None if _is_analyst(row) else _measured_on(row)["source"],
        # Who raised it, so a surface can leave out the measurement's own
        # headings for a change that was never going to have a measurement.
        "source": "analyst" if _is_analyst(row) else "detector",
        "verified_at": verdict.get("verified_at"),
    }


def verification_text(row, why_not: str = "") -> str:
    """The same artefact as one plain-text message, answerable in one word.

    §9's design constraint taken literally, and this is the artefact it bites
    hardest on: the whole of a week's measurement, its two windows, what was
    claimed and who claimed it, and the choice — in something a person reads on
    a phone and answers with a word.
    """
    parts = [verification_headline(row)]
    windows = verification_windows(row)
    if windows:
        parts.append(windows)
    lines = verification_evidence(row)
    if lines:
        parts.append("\n".join(lines))
    parts.append(verification_result(row))
    estimated = verification_estimate(row)
    if estimated:
        parts.append(estimated)
    parts.append(verification_choice(row, why_not))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# the whole suggestion, for whichever surface is asking
# --------------------------------------------------------------------------
def detail(row) -> dict:
    """§9 artefacts 2 and 3 as fields: what the panel lays out and the chat
    joins into a message.

    One dict for both, so that the two surfaces cannot come to differ in what
    they claim — only in how it is arranged.

    The analyst's fields (§5.3) are on every row, empty on a detector's, so a
    surface reads one shape: `source`, `evidence_level` and the label in
    words, `warning`, and the rationale with its heading. `readback_yaml` is
    the config alone for a surface that lays it out as a diff; `readback` is
    the same config as the chat reads it.
    """
    if _is_analyst(row):
        return _analyst_detail(row)
    return {
        "suggestion_id": row.get("id"),
        "change_id": row.get("change_id"),
        "state": row.get("state"),
        "label": label_for(row),
        "headline": headline(row),
        "why": why(row),
        "evidence": evidence_lines(row),
        "estimate": estimate_sentence(row),
        "replay": replay_lines(row),
        "readback": readback(row),
        # Which side the figure came from, carried as data as well as prose:
        # the panel says it in a tag where the sentence would be repetition.
        "basis": _measured_on(row)["source"],
        "source": "detector",
        "evidence_level": "replayed",
        "evidence_label": _EVIDENCE_WORDS["replayed"],
        "warning": "",
        "reasoning": "",
        "reasoning_heading": "",
        "readback_yaml": "",
    }


def detail_text(row) -> str:
    """The same artefact as one plain-text message, answerable in one word.

    §9's design constraint, taken literally: this is the whole thing a user in
    ASI1 sees before they type yes or no, so everything they would need in
    order to disagree has to be in it — what was found, over what window, what
    would happen, and what it is expected to be worth.
    """
    if _is_analyst(row):
        return _analyst_detail_text(row)
    # §5A.1: every card shows its evidence label, detector rows included —
    # B kept them byte-for-byte, which left the panel's tag as the only place
    # a detector row said *replayed*. Same word, same slot as the analyst
    # path's, on the line that states what was found.
    parts = [f"{why(row)} ({_EVIDENCE_WORDS['replayed']})"]
    lines = evidence_lines(row)
    if lines:
        parts.append("\n".join(lines))
    parts.append(estimate_sentence(row))
    graded = replay_lines(row)
    if graded:
        parts.append("\n".join(graded))
    written = readback(row)
    if written:
        parts.append(f"What would be written:\n{written}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# a suggestion no detector raised — A SUGGESTION NO DETECTOR RAISED above
# --------------------------------------------------------------------------
def _is_analyst(row) -> bool:
    """Whether the row is the analyst's. `source`, never `kind` (B-1)."""
    return str((row or {}).get("source") or "") == "analyst"


def _level_of(row) -> str:
    """The stored evidence label, or the weakest when it is not one of the
    three (B-2)."""
    level = str((row or {}).get("evidence_level") or "")
    return level if level in _EVIDENCE_WORDS else "untested"


def _flat(text) -> str:
    """Model-written text for a one-line slot: whitespace collapsed, nothing
    else touched. It is printed, never read."""
    return " ".join(str(text or "").split())


def _analyst_label(row) -> str:
    """The alias the config would write — the name HA will show, for
    `label_for`'s reason — and the model's title before there is a config."""
    alias = _flat((row.get("config") or {}).get("alias")
                  if isinstance(row.get("config"), dict) else "")
    return alias or _flat(row.get("title")) or "a change the agent suggests"


def _analyst_headline(row) -> str:
    """"<title> (<label>)". No number: until the auditor's claim vocabulary
    gives a recounted figure a noun, a figure here would stand alone, which is
    what §9's digest rule forbids."""
    title = _flat(row.get("title")) or _analyst_label(row)
    return f"{title} ({_EVIDENCE_WORDS[_level_of(row)]})"


def _is_shown(row) -> bool:
    """An explorer-run row (free-hand F-10): a finding, a rule and a chart,
    no claims."""
    return _level_of(row) == "shown"


def _sentence(text) -> str:
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _analyst_why(row) -> str:
    """The title as a sentence — the only statement of what is proposed that
    the row carries, and the model's own words for it. A `shown` row carries
    one more: its finding, the sentence the chart is there to show."""
    if _is_shown(row):
        finding = _flat(row.get("finding"))
        if finding:
            return _sentence(finding)
    title = _flat(row.get("title")) or _analyst_label(row)
    return _sentence(title)


def _analyst_words(row):
    """(heading, text) for the model's own prose on the card: a `shown` row's
    rule under RULE_HEADING — its finding is already `why` — and an audited
    row's rationale under REASONING_HEADING. ("", "") when there is none."""
    if _is_shown(row):
        rule = str(row.get("rule_text") or "").strip()
        return (RULE_HEADING, rule) if rule else ("", "")
    reasoning = str(row.get("rationale") or "").strip()
    return (REASONING_HEADING, reasoning) if reasoning else ("", "")


def _recounted(value) -> str:
    """A recounted figure as text, or "" when it is not a figure. Numbers and
    short strings only — a structure here is Phase C's to name, and printing
    its repr would be a figure with no noun."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return _num(value)
    if isinstance(value, str):
        return _flat(value)
    return ""


def _analyst_evidence(row) -> list:
    """Matched claims only, each with the figure the core recounted (B-3).

    A `not_checkable` claim is not evidence and is not printed as any; a
    matched claim whose recount cannot be printed, or whose text is empty, is
    omitted rather than shown half-filled — the OMIT rule, applied to claims.
    `stated` is never read here.
    """
    lines = []
    for claim in row.get("claims") or []:
        if not isinstance(claim, dict) or claim.get("result") != "matched":
            continue
        text, figure = _flat(claim.get("text")), _recounted(claim.get("recounted"))
        if text and figure:
            lines.append(f"{text} — recounted from your history: {figure}")
    return lines


def _analyst_yaml(row) -> str:
    """The stored config as YAML, dumped the way core/gate.py dumps the
    `new_yaml` a chat approval shows — so the readback and the approval of
    the same change are the same text."""
    config = row.get("config")
    if not isinstance(config, dict) or not config:
        return ""
    return yaml.dump(config, allow_unicode=True, sort_keys=False).strip()


def _analyst_readback(row) -> str:
    """B-4: the alias, then the config as a fenced YAML block — the form
    `agent_runtime.format_approval_for_chat` already puts in front of a user
    who is about to say yes."""
    block = _analyst_yaml(row)
    if not block:
        return ""
    alias = _flat((row.get("config") or {}).get("alias"))
    fenced = f"```yaml\n{block}\n```"
    return f"{alias}\n{fenced}" if alias else fenced


def _analyst_warning(row) -> str:
    return UNTESTED_WARNING if _level_of(row) == "untested" else ""


def _analyst_detail(row) -> dict:
    """`detail` for an analyst row — the same keys, the analyst's values. A
    `shown` row's rule_text rides in `reasoning` under RULE_HEADING, so the
    panel prints it where it prints an audited row's rationale."""
    heading, reasoning = _analyst_words(row)
    level = _level_of(row)
    return {
        "suggestion_id": row.get("id"),
        "change_id": row.get("change_id"),
        "state": row.get("state"),
        "label": label_for(row),
        "headline": headline(row),
        "why": why(row),
        "evidence": evidence_lines(row),
        "estimate": "",
        "replay": [],
        "readback": readback(row),
        "basis": None,
        "source": "analyst",
        "evidence_level": level,
        "evidence_label": _EVIDENCE_WORDS[level],
        "warning": _analyst_warning(row),
        "reasoning": reasoning,
        "reasoning_heading": heading,
        "readback_yaml": _analyst_yaml(row),
    }


def _analyst_detail_text(row) -> str:
    """`detail_text` for an analyst row. The warning is the first line, before
    anything the model wrote, so that no reading of the message reaches the
    reasoning without passing it."""
    parts = []
    warning = _analyst_warning(row)
    if warning:
        parts.append(warning)
    parts.append(f"{why(row)} ({_EVIDENCE_WORDS[_level_of(row)]})")
    lines = evidence_lines(row)
    if lines:
        parts.append("\n".join(lines))
    heading, words = _analyst_words(row)
    if words:
        parts.append(f"{heading}:\n{words}")
    written = readback(row)
    if written:
        parts.append(f"What would be written:\n{written}")
    return "\n\n".join(parts)


def _local(ts) -> str:
    """An epoch as the local wall clock, to the minute — the zone the chart's
    days were cut in (core/charts.py reads `time.localtime` too)."""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")


def _span(window_) -> str:
    try:
        return f"{_local(window_['start'])} to {_local(window_['end'])}"
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return ""


def _capital(text) -> str:
    return text[:1].upper() + text[1:] if text else text


def _chart_caption(chart, names=None) -> str:
    """"Timeline of Hall light, Hall motion, 2026-09-10 00:00 to 2026-09-17
    00:00". Each entity by the name `names` gives it, its id where it gives
    none, and the DRAWN window; "" when either cannot be named."""
    spec = chart.get("spec") if isinstance(chart.get("spec"), dict) else {}
    name = _CHART_NAMES.get(spec.get("type"))
    names = names if isinstance(names, dict) else {}
    entities = [_flat(names.get(e)) or e
                for e in spec.get("entities") or [] if isinstance(e, str)]
    span = _span(spec)
    if not name or not entities or not span:
        return ""
    caption = f"{name} of {', '.join(entities)}, {span}"
    if spec.get("state") and spec.get("type") in ("heatmap", "daily_count"):
        caption += f" — changes to “{_flat(spec['state'])}”"
    return caption


def _clamped_sentence(chart) -> str:
    """window_clamped as a sentence: what was asked, what is drawn, why."""
    clamped = chart.get("window_clamped")
    if not isinstance(clamped, dict):
        return ""
    asked, drawn = _span(clamped.get("asked")), _span(clamped.get("drawn"))
    why = "; ".join(str(w) for w in clamped.get("why") or [] if w)
    if not asked or not drawn:
        return ""
    sentence = f"The agent asked for {asked}; the chart shows {drawn}"
    return _sentence(f"{sentence}, because {why}" if why else sentence)


def _markers_sentence(chart) -> str:
    """What the markers show, or why there are none. `markers` None is either
    a rule outside the subset (the note, CANNOT_DRAW_RULE first, says which
    part) or a chart type that carries none (no note). An empty list with a
    note is a rule acting on nothing charted; without one, a rule that would
    not have acted — which is a finding, and printed as one."""
    markers, note = chart.get("markers"), _flat(chart.get("markers_note"))
    if markers is None:
        return _sentence(_capital(note)) if note else NO_MARKERS_FOR_TYPE
    if not isinstance(markers, list):
        return ""
    if not markers:
        return _sentence(_capital(note)) if note else NO_MARKERS_IN_WINDOW
    sentence = (f"The markers show the {_many(len(markers), 'time')} the rule would have "
                f"acted on a charted device")
    return _sentence(f"{sentence}; {note}" if note else sentence)


def _truncated_sentence(chart) -> str:
    series = chart.get("series") if isinstance(chart.get("series"), dict) else {}
    dropped = series.get("days_dropped")
    if not series.get("truncated") or not dropped:
        return ""
    return f"The oldest {_many(dropped, 'day')} were left out to keep the chart small."


def _feedback_sentence(row) -> str:
    """The household's own answer, as it was given (F-11) — dated, because a
    row the agent raises again keeps it. `rolled_back` (F-WP6) is written by
    the rollback path, not typed, so it is said as what happened."""
    feedback = row.get("feedback")
    if not isinstance(feedback, dict) or feedback.get("answer") not in (
            "yes", "no", "rolled_back"):
        return ""
    if feedback["answer"] == "rolled_back":
        when = f" on {_local(feedback['at'])[:10]}" if feedback.get("at") else ""
        return f"You applied this and then rolled it back{when}."
    line = f"You answered {feedback['answer']}"
    if feedback.get("at"):
        line += f" on {_local(feedback['at'])[:10]}"
    reason = _flat(feedback.get("reason"))
    return f"{line}, because: {reason}" if reason else f"{line}."


def shown_detail(row, names=None) -> dict:
    """The sentences a `shown` row's card prints around its chart (F-WP5).

    Beside `detail`, not in it: `detail` is one shape for every row and both
    surfaces, and these belong to the panel's card of an explorer idea only.
    {} for any other row. `chart_error` set means the panel draws no chart and
    prints the sentence — F-7's for a chart the record could not draw,
    CHART_NOT_DRAWN for a row older than the drawing; otherwise the caption, a
    clamped window, the markers and days left out each say their part, ""
    where there is nothing to say. No point of the series is read here beyond
    its `truncated` count — the panel draws it. `names` maps an entity id to
    the household's name for it, for the caption; an id it lacks is printed
    as the id."""
    if not (_is_analyst(row) and _is_shown(row)):
        return {}
    chart = row.get("chart") if isinstance(row.get("chart"), dict) else {}
    error = _flat(chart.get("error")) if chart else "no chart was stored"
    if error:
        missing = _sentence(f"{CHART_ERROR_SENTENCE}: {error}")
    elif chart.get("series") is None:
        missing = CHART_NOT_DRAWN
    else:
        missing = ""
    return {
        "chart_error": missing,
        "chart_caption": "" if missing else _chart_caption(chart, names),
        "chart_clamped": "" if missing else _clamped_sentence(chart),
        "chart_markers": "" if missing else _markers_sentence(chart),
        "chart_truncated": "" if missing else _truncated_sentence(chart),
        "sent_heading": SENT_HEADING,
        "sent_note": SENT_NOTE,
        "feedback_line": _feedback_sentence(row),
    }


# --------------------------------------------------------------------------
# The conversational path: putting ONE suggestion to the user.
#
# Nothing below writes anything, reaches Home Assistant, or executes a
# decision. It builds a question, holds it on a session, and recognises an
# answer it is handed. See the module docstring; the argument is core/
# rollback.py decision 4's, applied to a change that is NOT pre-authorised —
# which is why the answer leaves here for /rest/approve rather than acting.
# --------------------------------------------------------------------------
def question(row) -> dict:
    """The payload the chat tool returns for one suggestion: the artefact plus
    the marker, or a refusal explaining why it cannot be answered here.

    ONE suggestion. §9: "never a single yes covering several changes" — so this
    takes a row, not a list, and the parked slot holds the one change_id it
    names.
    """
    if not row:
        return {
            "asked": False,
            "reason": "There is no such suggestion. List the agent's suggestions first "
                      "and use one of the ids it gives.",
        }
    if row.get("state") != "proposed" or not row.get("change_id"):
        return {
            "asked": False,
            "reason": f"That suggestion is {row.get('state') or 'unknown'} rather than waiting "
                      "for an answer, so there is nothing to approve. Relay this to the user "
                      "as it is.",
        }
    return {
        QUESTION_MARKER: True,
        "change_id": row.get("change_id"),
        "suggestion_id": row.get("id"),
        "question": detail_text(row),
        "label": label_for(row),
    }


def question_in(tool_result):
    """The question payload inside a tool answer, or None.

    Recognised in the RESULT for the reason rollback.confirmation_in gives: a
    result is built by this process and a tool argument is written by the
    model, and the thing being recognised here is a request to put a WRITE to
    the user.
    """
    if not tool_result or QUESTION_MARKER not in str(tool_result):
        return None
    try:
        parsed = json.loads(tool_result)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) and parsed.get(QUESTION_MARKER) else None


def park_question(memory: dict, parsed: dict, session_id: str = "") -> str:
    """Holds the question on the session and returns the text to send.

    Its own slot, never `memory["pending_approval"]`. That slot carries a
    conversation to resume — a messages_snapshot, an ai_message, a tool_call_id
    — and `handle_approve` answers it by resuming the model from them. An
    agent-raised proposal has no such conversation and must not be given an
    invented one; what it has is a journal row, and `gate.pending_for` rebuilds
    the pending from that. So this holds a change_id and nothing else.

    Nothing is journalled here. The row already exists — `gate.propose` wrote
    the whole question to it days ago, which is the only reason this session
    can be handed a change_id at all.
    """
    memory["pending_suggestion"] = {
        "change_id": parsed.get("change_id"),
        "suggestion_id": parsed.get("suggestion_id"),
        "question": parsed.get("question", ""),
        "created_at": _now(),
    }
    return f"{parsed.get('question', '')}\n\nReply YES to apply this, or NO to dismiss it."


def question_expired(pending: dict) -> bool:
    """True if a parked question is too old to still be answerable."""
    created_at = (pending or {}).get("created_at")
    if not created_at:
        return False
    return _now() - created_at > QUESTION_TTL_SECONDS


def reask(pending: dict) -> str:
    """What to say when the reply was not a decision.

    The whole artefact again, not "yes or no?". §9's one-word answer only works
    if the question is still on the screen, and by this point it may be several
    messages up.
    """
    return (f"{(pending or {}).get('question', '')}\n\n"
            "Reply YES to apply this, or NO to dismiss it. Nothing has been changed.")


# --------------------------------------------------------------------------
# ...and the same shape for artefact 4's answer.
#
# The same pattern as the block above and for the same reason, with one thing
# reversed: a proposal is a write nobody has approved, while a verification's
# "no" runs an operation core/gate.py authorised a week ago. That is why the
# answer leaves here for core/verify.py's `answer` rather than for
# /rest/approve — but WHICH path answers is still decided by a marker in a
# tool RESULT, never by an argument the model wrote.
# --------------------------------------------------------------------------
def verdict_question(row, why_not: str = "") -> dict:
    """The payload the chat tool returns for one verification report: the
    artefact plus the marker, or a refusal explaining why it cannot be answered
    here.

    ONE report. §9's "never a single yes covering several changes" applies to a
    keep-or-roll-back exactly as it does to an apply-or-dismiss.
    """
    if not row:
        return {
            "asked": False,
            "reason": "There is no such suggestion. List the agent's suggestions first "
                      "and use one of the ids it gives.",
        }
    if row.get("state") != "verified":
        return {
            "asked": False,
            "reason": (f"That suggestion is {row.get('state') or 'unknown'} rather than "
                       "waiting on a measured result, so there is nothing to keep or roll "
                       "back. Relay this to the user as it is."),
        }
    return {
        VERDICT_MARKER: True,
        "suggestion_id": row.get("id"),
        "change_id": row.get("change_id"),
        "question": verification_text(row, why_not),
        "label": label_for(row),
        # A report whose change cannot be undone still has to be answerable, or
        # it would sit at `verified` forever. YES keeps it either way; NO is
        # what `why_not` has already explained the agent cannot do.
        "undoable": not why_not,
    }


def verdict_in(tool_result):
    """The verdict payload inside a tool answer, or None.

    Recognised in the RESULT, like `question_in` and
    `rollback.confirmation_in`: a result is built by this process and cannot be
    forged by the model, and what is being recognised is a request to put an
    UNDO to the user.
    """
    if not tool_result or VERDICT_MARKER not in str(tool_result):
        return None
    try:
        parsed = json.loads(tool_result)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) and parsed.get(VERDICT_MARKER) else None


def park_verdict(memory: dict, parsed: dict, session_id: str = "") -> str:
    """Holds the verification question on the session and returns the text to
    send.

    Its own slot again — a fourth question is a fourth answering path, and the
    one thing that must stay impossible is a YES meant for one of them being
    read by another. Nothing is journalled: the report is already recorded on
    the suggestion row, and the change it might undo was journalled with its
    inverse a week ago.
    """
    memory["pending_verdict"] = {
        "suggestion_id": parsed.get("suggestion_id"),
        "change_id": parsed.get("change_id"),
        "question": parsed.get("question", ""),
        "created_at": _now(),
    }
    return f"{parsed.get('question', '')}\n\n{VERDICT_PROMPT}"


def verdict_expired(pending: dict) -> bool:
    """True if a parked verification question is too old to answer.

    Same window as the other two. The REPORT does not expire — it stays at
    `verified` in the panel and in the store — this only withdraws one
    conversation's copy of it, exactly as an expired suggestion question leaves
    the proposal open.
    """
    created_at = (pending or {}).get("created_at")
    if not created_at:
        return False
    return _now() - created_at > QUESTION_TTL_SECONDS


def reask_verdict(pending: dict) -> str:
    """The whole report again when the reply was not a decision."""
    return (f"{(pending or {}).get('question', '')}\n\n{VERDICT_PROMPT} "
            "Nothing has been changed.")
