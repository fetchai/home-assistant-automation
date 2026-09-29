"""Stage D2: ask a model where this device's idle window is, and refuse
anything the grid does not back.

core/detectors/schedule.py finds the windows a grid PROVES — maximal runs of
half-hours idle on every observation (D1). That is deterministic and it is
right about the clean cases. What it cannot do is read the grid the way a
person does: proposal §3 stage D2's own example is a household that leaves for
work and comes back, whose grid says 08:30-17:00 Mon-Fri *and* says Saturday
morning looks idle too — and the reason the window exists does not apply on a
Saturday. A model holds priors about human life that no run-length scan has,
and that is the whole of what it is asked for here.

THE MODEL PROPOSES; IT DOES NOT JUDGE
-------------------------------------
§7.2's decision 5, and it is the line this module is built along. The answer
that comes back is a CANDIDATE. Every judgement downstream of it — whether the
evidence moved, how many wrong firings disqualify, whether a week counts as
measured — stays a named constant with its argument beside it, and
core/replay.py still grades this window without asking anything.

That would not be enough on its own. §7.1 is blunt about why: replay only
catches a window the record PROVES was in use, and schedule.py's own exposure
is that unobserved stretches read as flatness. A window laid across half-hours
nobody watched sails through grading, because there is no evidence to
contradict it. Decision 3 chose to let the model answer FREE-FORM rather than
pick from a list the detector enumerated, which makes `refusal` below the
safety boundary outright, not a second opinion. If a check here is wrong,
nothing downstream catches it.

REFUSE, NEVER REPAIR. NO RETRY, NO NUDGE.
-----------------------------------------
There is no return path in this module that hands back a modified window.
Nothing is clamped to the nearest legal window, trimmed to the cells that
pass, or stripped of the one weekday that failed. A window that fails any of
decision 3's six checks is discarded whole and `propose` returns None.

And there is exactly one call. A refusal is not sent back to the model with
the reason attached, and no corrective message is appended and the call
repeated. That is deliberate: a proposer that quietly self-corrects until
something passes turns a bad prompt into a stream of plausible windows nobody
reviewed, and the refusal — which is logged with its reason — is the only
signal that the prompt is wrong. The fallback is D1's own pick, which WP4
wires; it is not another attempt at this one.

Parsing tolerance is not repair, and the two are worth keeping apart. The
model may wrap its JSON in a fence or in prose, and `parse_answer` will find
the object inside that. It will not change a value it found.

WHAT LEAVES THE HOUSE, AND WHY §10.1 PERMITS IT
-----------------------------------------------
Two things, and nothing else: `schedule.runs_text(grid)`, and the device's
name when the caller passes one.

What goes is an AGGREGATE, and a smaller one than the grid `grid_text`
renders: at most a handful of clock spans per weekday, over ninety days — no
timestamps of individual events, no entity_id, no measured watts, no dates, no
rows. It cannot be read back into when anybody was in or what they switched
on: it says that this stretch of this weekday was idle on every one of the
times it was watched. §3's own line, and the reason stage D2 was affordable at
all. `runs_text` sends strictly less than `grid_text` would, since every
stretch in it is derived from the cells and no cell survives it.

The name is core/disclosure.py's deliberate hole: NAME_FIELDS is never
inspected for content, because an agent that may not say "the kitchen
television" cannot hold a conversation about a house. That decision is made
there and is only being relied on here. It is passed through whitespace
collapsing and a length cap before it reaches the prompt — not redaction, but
a friendly_name is household-editable text going into an instruction, and a
name carrying its own newlines and directives is not something to hand a model
verbatim.

This module is under core/, not tools/, so tests/test_disclosure.py's
"no tool calls json.dumps" grep does not reach it. That is a reason to state
the boundary here in prose rather than a reason to have skipped the argument:
nothing mechanical is enforcing it, so the list above is the enforcement.

`Grid.dates` — which holds per-date detail — is deliberately not sent. Nothing
about describing the grid's SHAPE needs it, and it is the one part of the
structure that could place a household on a calendar.

Also not sent, because nobody asked yet: anything at all. §7.2's decision 1 is
that this egress is opt-in and off by default, and that option is WP4's.
Nothing calls `propose` in this build.

SYNCHRONOUS, ON PURPOSE
-----------------------
§7.1's third finding. `tasks/review.py` hands `review.run_once` to an executor
thread, where there is no event loop — so `core/agent_runtime.py`'s `ainvoke`
is unusable here and its client is unreachable anyway, since importing it
would pull the whole tool registry into a detector's import graph (decision
5). This builds its own client, calls `invoke`, and owns its own timeout.

EVERY FAILURE IS A None
-----------------------
Timeout, transport error, an answer that is not JSON, a window the validator
refuses, no API key configured — all of them return None and log. §11's rule
is that the agent going quiet is acceptable; an exception escaping this
function would be raised inside the daily pass and would take that morning's
other suggestions, verifications and rollbacks with it.
"""
import hashlib
import json
import logging

from core import settings
from core import window
from core.detectors import schedule

logger = logging.getLogger(__name__)

# ASI1's OpenAI-compatible endpoint — the same base_url core/agent_runtime.py
# builds its chat client against, repeated here rather than imported because
# decision 5 forbids the import and one string is a cheaper duplicate than a
# detector that drags the tool registry in behind it.
BASE_URL = "https://api.asi1.ai/v1"

# Pinned to zero, which is decision 3's last paragraph. Free-form output is
# already not reproducible in the sense WP5 would like — the same grid can be
# read two defensible ways — and WP5's job is to diff this proposer against
# D1 and against replay's verdict. Output that ALSO varies run to run is not
# something a harness can read at all. ASI1 does not promise to honour this;
# asking for it costs nothing and is the difference between "the model
# disagreed with D1" and "the model disagreed with itself".
TEMPERATURE = 0

# Seconds to wait for one answer, and deliberately NOT settings.LLM_TIMEOUT.
#
# That constant is 60 and is sized for the chat path, where a person is
# watching a cursor and will wait a minute for an answer they asked for
# themselves. Nobody is watching this one. It runs inside review.run_once,
# which also owes that morning its verifications, its rollbacks and its other
# suggestions, and decision 2 consults the model once per candidate entity —
# so the pass's worst case is this number times the number of devices, paid
# serially by a thread that has other work queued behind it.
#
# Thirty is where a slow answer stops being worth waiting for. The payload is
# under a kilobyte in and one small JSON object out, so a healthy endpoint
# answers in a few seconds; past half a minute the endpoint is degraded rather
# than thinking, and the right response to a degraded endpoint on a daily timer
# is to skip today's schedule question and let the rest of the pass run. Lower
# than the chat path's rather than higher, for exactly that reason.
TIMEOUT_SECONDS = 30

# One call, no transport-level retries either. langchain's own retry is about
# 5xx and connection resets rather than re-prompting, so it is not what
# decision 3's no-retry rule is aimed at — but it silently multiplies the
# timeout above by the retry count, which is the one thing the number is
# chosen to bound. A failed call is a None and tomorrow's pass asks again.
MAX_RETRIES = 0

# How much of a device's name is passed through. Long enough for anything a
# household actually types ("Living room television, left socket") and short
# enough that a name is a name rather than a paragraph arriving inside an
# instruction.
NAME_MAX_CHARS = 80

# How much of an unreadable answer goes into the log line. Enough to see what
# came back instead of JSON; not the whole reply, because a model that
# answered with an essay would put the essay in the add-on log.
_EXCERPT_CHARS = 200


# Bumped whenever `prompt_for`'s text changes in a way that could change the
# model's answer for an unchanged grid — wording, the worked example, the
# stated constraints, all of it. core/schedule_cache.py (D-4/WP3) folds this
# into what a cached answer is keyed on: a cached window is only a true
# answer to THIS prompt, and a revised prompt makes every stored answer an
# answer to a different question until this number moves. Exposed here
# rather than guessed at by the cache, because this module is the one thing
# that knows when the text actually changed.
PROMPT_VERSION = 2


# --------------------------------------------------------------------------
# the prompt (CLAUDE.md: the prompt is code)
# --------------------------------------------------------------------------
def prompt_for(grid, name="") -> str:
    """The question, with `schedule.runs_text` in it.

    Five decisions, and each is an argument rather than a preference.

    **THE MODEL IS SHOWN CLOCK TIMES, NOT THE CHARACTER GRID.** The single
    largest thing in this text, and it was settled by measurement — the
    argument and the numbers are in the section comment above
    `schedule.runs_text`. In one sentence: asked to do nothing but read the
    longest run of digits out of `grid_text`'s Monday row, asi1-mini got both
    ends wrong and then converted its own answer to the wrong clock time. A
    reader that cannot index the grid cannot check any rule stated about it,
    and what it falls back on is what an idle window usually looks like —
    which is precisely the "answers from a prior, not from the data" this
    prompt was rewritten to stop. `runs_text` admits exactly the windows
    `refusal` admits, so nothing legal was given up to remove the counting.

    **The constraints are stated IN the prompt, not checked afterwards only.**
    This module may not repair anything, so every window the model returns
    that breaks a rule is a wasted call and a day with no suggestion. A model
    told "minutes are 00 or 30" produces far fewer of those than one corrected
    after the fact, and since correcting after the fact is exactly what is
    forbidden, stating them is the only lever there is. They are stated in the
    terms of what the model is HOLDING — "inside that day's listed stretch"
    rather than "clears MIN_OCCURRENCES" — because a rule it cannot check
    against what is in front of it is a rule it can only guess at. That is the
    same argument the previous version made about the grid's digits, and it is
    the argument that condemns the grid: the digits were all 9, so the rule
    stated in their terms was one no answer could fail and none could use.

    **The job is named as the choice D1 refuses to make.** `schedules`' own
    docstring says where it stops: runs merge across weekdays only when they
    are IDENTICAL, so a Monday 07:30-17:00 and a Tuesday 07:00-17:30 stay two
    windows and their intersection — true of both days — is never proposed.
    Choosing among those intersections is what this prompt asks for, in those
    words, because a model asked "where is the idle window" answers with a
    picture of a day and a model asked "which of these stretches overlap on
    days that belong together" answers about the list.

    **The weekend rule is stated as something checkable, not as a sentiment.**
    §3 stage D2's example is declining to extend a weekday window to Saturday.
    Told only that, a model also declines on a device that is genuinely unused
    all week — the earlier text's cost, measured: over a grid idle 00:00-18:00
    on all seven days it answered `{"window": null}` and gave up a window D1
    had. So the test named here is one the list answers: seven days with the
    SAME stretch is a fact about the device and the weekend belongs; a weekend
    whose stretch differs from the weekdays' is the household happening to be
    out, and next Saturday they may be home.

    **JSON only, one flat object, and an explicit way to decline.** The parser
    tolerates a fence or prose around the object because models emit both, and
    finding an object inside those is not the same thing as changing what is
    in it. Everything past that is strict: three keys, two strings, a list of
    integers. `{"window": null}` costs one line and is what stands between a
    model with no legal answer and an invented one.
    """
    lattice = " or ".join(f"{minute:02d}"
                          for minute in range(0, 60, schedule.SLOT_MINUTES))
    named = _clean_name(name)
    about = f'The device is named "{named}".\n\n' if named else ""
    return (
        "You are choosing when one electrical device in a home can be switched off at the\n"
        "wall without anybody in the house noticing. Below are the stretches of each weekday\n"
        "that this device's own record shows it sitting idle.\n"
        "\n"
        f"{about}"
        f"{schedule.runs_text(grid)}\n"
        "\n"
        "What you are being asked for\n"
        "----------------------------\n"
        "ONE start time, ONE end time, and the weekdays they apply to. The same hours run on\n"
        "every weekday you name, so the stretch you choose has to fit inside the listed\n"
        "stretch of every single one of them — read the times off the list above and do not\n"
        "estimate them.\n"
        "\n"
        "The list is what the record proves. What it cannot tell you is which days belong\n"
        "together, and that is the judgement being asked of you. Weekdays a household is out\n"
        "at work are one kind of day and the weekend is another: a Saturday that looks idle\n"
        "may only mean the household happened to be out, and next Saturday they may be home\n"
        "all day. So if Saturday's or Sunday's stretch DIFFERS from the weekdays' — longer,\n"
        "shorter, or at other hours — leave that day out. If every day of the week shows the\n"
        "SAME stretch, that is a fact about the device rather than about anybody's week, and\n"
        "every day belongs in the answer.\n"
        "\n"
        "Past that, do not give away hours or days the list above backs: a longer stretch\n"
        "across more days saves more electricity.\n"
        "\n"
        "Rules the answer must obey\n"
        "--------------------------\n"
        "A window that breaks any of these is discarded whole. There is no second attempt and\n"
        "no chance to correct it, so check the answer against the list before giving it.\n"
        "\n"
        f"1. Both times land on the half hour: the minutes are {lattice}, nothing else.\n"
        "2. The window starts and ends on the same day. It may not run through midnight, so the\n"
        "   end time is strictly later than the start time.\n"
        f"3. It is at least {_span_phrase(schedule.MIN_RUN_SLOTS)} long, "
        f"and at most {_span_phrase(schedule.MAX_RUN_SLOTS)}.\n"
        "4. For EVERY weekday you name, your start time is at or after the start of one of\n"
        "   that day's listed stretches AND your end time is at or before the end of that\n"
        "   same stretch.\n"
        "5. A weekday with no stretch listed may not be named at all, whatever hours you pick.\n"
        "6. Leaving a day out costs nothing; including one whose stretch does not cover your\n"
        "   hours voids the whole answer.\n"
        "\n"
        "Your answer\n"
        "-----------\n"
        "One JSON object and nothing else:\n"
        "\n"
        '  {"start": "HH:MM", "end": "HH:MM", "days": [0, 1, 2, 3, 4]}\n'
        "\n"
        "where days are weekday numbers, Monday 0 through Sunday 6.\n"
        "\n"
        'If no stretch in the list can meet the rules, answer exactly: {"window": null}\n'
    )


def _clean_name(name) -> str:
    """A device name flattened to one line and capped.

    Not redaction — decision made in core/disclosure.py, and names deliberately
    leave the house intact. This is about the name arriving inside an
    instruction: friendly_name is text a household types, and text a household
    types can contain newlines, headings and sentences that read as further
    rules. Collapsing whitespace and capping the length leaves a name a name.
    """
    flattened = " ".join(str(name or "").split())
    return flattened[:NAME_MAX_CHARS]


def name_print(name) -> str:
    """A digest of the name this prompt would carry — what D-4's cache has to
    key on besides the grid, because the name is a second INPUT and it moves
    the answer.

    Measured, not assumed. `sensor.lounge_tv_power_draw` and
    `sensor.server_rack_power_draw` hold grids identical character for
    character; over the same grid the prompt built from "Lounge TV Power"
    comes back with all seven days and the one built from "Server rack Power"
    comes back with Mon-Fri, three times each at temperature 0. That is the
    model reading "server rack" as a thing a household does not stop at the
    weekend — a prior about human life, which is the whole of what stage D2
    was admitted for — but it means a cache keyed on the grid alone would
    hand a renamed device the answer to its old name, for as long as its
    weekly shape held. That was read once as D2 answering non-deterministically
    (the 2026-09-17 comparison's first finding); it is not, and this is the
    line that records why.

    A digest rather than the name itself so nothing writes household text into
    a new file in DATA_DIR. It is not a secret and this is not redaction —
    core/disclosure.py's NAME_FIELDS is deliberate about names leaving intact
    — it is that a cache is a store of shapes and ids, and keeping it that way
    costs one line.

    Over `_clean_name`'s output, not the raw name, so that two names the
    prompt cannot tell apart do not key differently.
    """
    return hashlib.sha256(_clean_name(name).encode("utf-8")).hexdigest()[:16]


def _span_phrase(slots) -> str:
    """A number of half-hour cells as a person says it, for the prompt."""
    hours, minutes = divmod(int(slots) * schedule.SLOT_MINUTES, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minutes")
    return " ".join(parts)


# --------------------------------------------------------------------------
# reading the answer (tolerance, and where it stops)
# --------------------------------------------------------------------------
def parse_answer(text):
    """The model's reply as a `window.Window`, None if it declined, or a
    ValueError carrying the reason it could not be read.

    Three outcomes rather than two, because "there is no window here" is a
    legitimate answer the prompt asks for explicitly and is not the same event
    as an answer nobody can parse: one is the model being right about a thin
    grid, the other is the prompt or the endpoint being wrong. They are logged
    differently and only the second is worth looking at.

    Raises rather than returning a reason, which is the shape `window.spec`
    already refuses in — so a model answering "25:00" and a model answering
    with an apology arrive at `propose` as the same kind of failure under one
    `except`.

    Every tolerance here is about FINDING the object. Nothing below changes a
    value it found: the times go to `window.spec` as written, and the days are
    required to be integers already rather than coerced from whatever turned
    up, because `int(1.5)` is a repair wearing a cast's clothes.
    """
    body = _json_object(_content_of(text))

    # The prompt's own decline form. Read as "the key is present and null"
    # rather than as "no start key", so an answer that simply lost its fields
    # is still a parse failure and still gets logged as one.
    if "window" in body and body["window"] is None:
        return None

    start, end, days = body.get("start"), body.get("end"), body.get("days")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError(f"start and end must be \"HH:MM\" strings, not {start!r} and {end!r}")
    # bool is an int in Python, and [True, False] is not a set of weekdays.
    if (not isinstance(days, list) or not days
            or not all(isinstance(day, int) and not isinstance(day, bool) for day in days)):
        raise ValueError(f"days must be a non-empty list of weekday integers, not {days!r}")
    return window.spec(start, end, days)


def _json_object(text):
    """The first JSON object in the answer, or a ValueError.

    A fence is not stripped as a separate step and neither is prose, because
    neither needs to be: an object inside ```json ... ``` is still a balanced
    run of braces, and so is one with a sentence either side of it. So the
    candidates are the whole reply first — the well-behaved case, and the one
    that catches a reply which is a bare JSON object — then each balanced run
    in the order it appears.
    """
    body = str(text)
    for candidate in [body.strip(), *_braced_runs(body)]:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"no JSON object in the answer: {_excerpt(body)}")


def _content_of(answer):
    """The text of whatever the client handed back.

    langchain returns a message object whose `content` is a string, but some
    providers put a list of blocks there instead, and a stub in a test may
    return the string itself. All three read the same way here rather than in
    `propose`, so `parse_answer` takes whatever the client gave it and there is
    one place that knows what a reply looks like.
    """
    content = getattr(answer, "content", answer)
    if isinstance(content, list):
        return "".join(str(getattr(block, "text", None)
                           or (block.get("text", "") if isinstance(block, dict) else block))
                       for block in content)
    return content


def _braced_runs(text):
    """Every balanced `{...}` run in the text, outermost first.

    String-aware, so a brace inside a JSON string value — a device name, most
    plausibly — does not close the object early and turn a readable answer
    into a refusal.
    """
    depth = 0
    opened = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                opened = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                yield text[opened:index + 1]


def _excerpt(text) -> str:
    """As much of an unreadable answer as belongs in a log line."""
    flattened = " ".join(str(text).split())
    if len(flattened) <= _EXCERPT_CHARS:
        return repr(flattened)
    return repr(flattened[:_EXCERPT_CHARS] + "...")


# --------------------------------------------------------------------------
# the validator (§7.2 decision 3, and the safety boundary)
# --------------------------------------------------------------------------
def refusal(grid, spec):
    """Why this grid does not back this window, or None if it does.

    Decision 3's six checks in full. The three about the window's own SHAPE are
    here; the three about the GRID are `schedule.window_is_backed`, which owns
    them because the cells are that module's data and because they have to be
    testable against a literal grid with no client in sight.

    The order is not cosmetic. The midnight rule comes first because a window
    running through midnight has no single weekday to index cells under. The
    lattice comes next because a length in cells is only a meaningful number
    once both ends land on a cell boundary — decision 3's own reason for the
    check, that a window the grid cannot be indexed by is one none of the other
    checks mean anything about. The length is third, and it is the cheap
    arithmetic that stands between a bad answer and 336 cell lookups.

    A reason, not a correction. Nothing here returns a fixed window, and
    `propose` does nothing with the string but log it.

    The reasons name the FAULT and not the window, here and in
    `schedule.window_is_backed` both: `propose` prints the label once in front
    of whichever reason came back, so a reason carrying its own copy would put
    the window in the log line twice — and only for the three checks that
    happen to live on this side of the split.
    """
    if window.spans_midnight(spec):
        # core/builder._no_midnight's reason: the automation this becomes has
        # its cut and its restore as two time triggers, and a cut on Friday
        # restored on Saturday is a rule whose saving nobody may compute.
        # schedule.py's own runs stop at local midnight for the same reason,
        # so accepting one here would be the model reaching somewhere D1
        # cannot.
        return "runs through midnight"

    # Minute OF DAY on both ends, which is what schedule._slot_bounds divides
    # to find a cell — not the minute within the hour. The two are the same
    # question only while SLOT_MINUTES divides 60, and the one thing this check
    # exists to guarantee is that the window can be indexed by the grid.
    if any((hour * 60 + minute) % schedule.SLOT_MINUTES
           for hour, minute in ((spec.start_hour, spec.start_minute),
                                (spec.end_hour, spec.end_minute))):
        return f"is not on the {schedule.SLOT_MINUTES}-minute lattice the grid is made of"

    slots = window.minutes(spec) // schedule.SLOT_MINUTES
    if slots < schedule.MIN_RUN_SLOTS:
        return (f"is {slots} cells, under the {schedule.MIN_RUN_SLOTS} "
                "a window worth switching needs")
    if slots > schedule.MAX_RUN_SLOTS:
        # Kept although the two checks above make it unreachable: a window that
        # does not run through midnight and lands on the lattice cannot be
        # longer than 00:00-23:30, which is exactly MAX_RUN_SLOTS. It stays
        # because the bound is decision 3's and belongs stated where the other
        # five are, and because the thing making it unreachable is an
        # arithmetic accident of SLOTS_PER_DAY rather than a rule anybody
        # wrote. tests/test_scheduler_llm.py pins that accident.
        return (f"is {slots} cells, over the {schedule.MAX_RUN_SLOTS} "
                "a single day's window can be")

    return schedule.window_is_backed(grid, spec)


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------
def propose(grid, name="", client=None):
    """Ask the model for one window over this grid, and return it only if the
    grid backs it. None on anything else at all.

    `client` is the seam the tests drive: anything with `invoke(prompt)`
    returning a message or a string. None means build the real one, which
    needs an API key — and a home with no key configured is the ordinary case
    rather than an error, so that is a quiet None too.

    One call. A refusal is logged with its reason and returned as None; it is
    never sent back to the model, and nothing here is attempted twice.
    """
    if client is None:
        if not settings.ASI1_API_KEY:
            logger.debug("schedule proposer: no ASI1 key configured, not asking")
            return None
        try:
            client = _client()
        except Exception as error:
            logger.warning("schedule proposer: could not build a client (%s)", error)
            return None

    try:
        answer = client.invoke(prompt_for(grid, name))
    except Exception as error:
        # Timeout and transport both land here, and both mean the same thing
        # to the pass: no schedule question today, everything else unaffected.
        logger.warning("schedule proposer: no answer from the model (%s)", error)
        return None

    try:
        spec = parse_answer(answer)
    except ValueError as error:
        logger.info("schedule proposer: unreadable answer — %s", error)
        return None
    if spec is None:
        logger.debug("schedule proposer: the model found no window in this grid")
        return None

    reason = refusal(grid, spec)
    if reason is not None:
        # Logged at info rather than debug on purpose: this is the line that
        # makes a bad prompt visible. A proposer that repaired its answers
        # would have nothing to print here and would look like it was working.
        logger.info("schedule proposer: refused %s — %s", window.label(spec), reason)
        return None
    return spec


def _client():
    """The synchronous ASI1 chat client this module calls.

    Built here rather than taken from core/agent_runtime.py, which decision 5
    forbids importing and which would be the wrong object anyway: it is bound
    to the whole tool registry and is used through `ainvoke` on an event loop
    this pass does not have.

    The import is HERE, not at module scope, so that this module — and
    `propose(grid, client=...)` in particular — imports and runs with no
    langchain installed at all. Nothing else in this file touches the
    package; the one place that does is the one place a real client is ever
    built, and only when `propose` is not handed one already.
    """
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=settings.ASI1_MODEL,
        temperature=TEMPERATURE,
        api_key=settings.ASI1_API_KEY,
        base_url=BASE_URL,
        timeout=TIMEOUT_SECONDS,
        max_retries=MAX_RETRIES,
    )
