"""The analyst: the model half of design §15.2 — a daily run that explores the
house's record, and stores each idea it submits with a chart drawn from it.

Two plans built this. `docs/PROPOSAL-MODEL-ANALYST.md` §5B gave the run, the
one file it keeps and its interval (`tasks/analyst.py`, which owns `due` /
`stamp_run`); `docs/PROPOSAL-FREE-HAND-ANALYST.md` replaced its two audited
passes with one free-hand pass (F-4..F-11). `core/review.py` never imports
this module, so its "No model, anywhere" stays literally true. Split pure /
I/O the way `core/review.py` is, for the same reason: every judgement here is
a function over literals a test can state.

WHY ITS OWN LOOP AND NOT `agent_runtime._run_agent` (D-1)
-----------------------------------------------------------
Everything `_run_agent` does besides call tools belongs to chat: the no-tool
nudge, parking a destructive call for approval, stopping on a rollback
confirmation or a suggestion question, and binding the whole registry. None
of it has a place here — there is nobody to nudge, park for or ask. The
analyst binds only `EXPLORER_READ_TOOLS` plus `Submit ideas`, checks the
allowlist again on every call (a model can emit a name it was never given),
and stops on its submit tool. It never holds a destructive tool, never calls
`gate.propose` and never writes to Home Assistant: what it stores is a
`validated` row, and asking the user stays `review._propose`'s.

WHY THE MODEL NEVER SEES A VERDICT (D-3)
-----------------------------------------
The submit call is intercepted by name and no ToolMessage is appended for it,
so there is nothing for the model to read and argue with. Refuse, never
repair (design §15.2, `scheduler_llm.py`'s rule): a refusal is a log line,
and the signal that the prompt needs work IS that log line, not a second
attempt with the reason attached.

WHY CHECK, THEN STORE (D-9)
----------------------------
An idea is the model's words; the house checks (`auditor.check_config`,
`duplicate_of`) are what let it become a row. A refusal stores nothing at
all — not a `candidate` to be cleaned up — so the queue `review` ranks only
ever holds ideas that name real devices and services and copy nothing that
exists. The claim audit that also recounted the model's figures here was
retired by F-9 (2.23.0): the evidence is the chart, which code draws.

WHY THE KEY IS THE SIGNATURE (D-8)
-----------------------------------
Two submissions with the same triggers and the same action targets are the
same suggestion, whatever the model called them. The key is therefore derived
from `auditor.signature` — the very tuple C-6's duplicate check compares — over
the normalised config, so the wording can change without the suggestion
multiplying, and `upsert_analyst_candidate`'s "new words are a new claim" path
is what a rephrasing reaches. The signature is made of frozensets, whose
iteration order (and so whose `repr`) follows string hashing and changes with
PYTHONHASHSEED; the key hashes the SORTED pairs, C-6's "sorted set" taken
literally, so a restart cannot re-key every open idea.

WHY THE LAST RUN IS A FILE, AND WHY IT FAILS CLOSED (D-10)
------------------------------------------------------------
The review's cadence lives in the store; the analyst's has nothing there to
count, and a re-run is not free the way a second review is: it costs money and
sends the house's data out a second time. So the last run date is persisted
and stamped BEFORE the run starts, and a file that cannot be read is treated
as today already run, logged, and overwritten with today — `core/models.py`'s
budget file, for the same reason: a full disk or a torn write must never buy a
second run.

THE RUN (free-hand proposal F-4..F-9, F-WP3)
----------------------------------------------
`run_explorer` is the run. ONE pass on the strong tier explores the record
with `EXPLORER_READ_TOOLS` — the read-only inventory and record tools plus the
two free-hand record tools — and ends by calling `Submit ideas`, under D-1..
D-3's rules (allowlist checked on every call, the submit call intercepted by
name and never answered, no verdict shown), and:

- `take_strong_call` before EVERY model call, because every call in this run
  is a strong one; None ends the run, and nothing downgrades (§4.1).
- A budget of EXPLORER_MAX_STEPS model calls and EXPLORER_MAX_QUERIES calls
  to the two record tools together (F-5). The steps reached end the pass with
  nothing submitted; a response past the queries is refused whole and told
  what remains (2.19.1), and EXPLORER_WARN_AT calls from the end the model is
  told once to submit. A call identical to one already run this pass is not
  run again and spends nothing (2.23.1): field run 4 sent one query eleven
  times (ten of them repeats) and submitted with one model call to spare.
- Orientation up front (2.19.1): the first message carries the record
  overview, every existing automation's trigger and target entities, the
  sensors the power detectors cover and the open suggestions
  (`_explorer_context`), so no model call goes on fetching them.
- Its tools come from its OWN registry, never through
  `agent_runtime._execute_tool_sync`: the two record tools are not in chat's
  registry (proposal §6 — chat must not get them) and so cannot be run from
  there, and every call runs on its own thread under
  EXPLORER_TOOL_TIMEOUT_SECONDS (2.18.2's rule; F-1's deviation left that
  outer bound to this loop). The model call itself is bounded by
  ANALYST_MODEL_TIMEOUT_SECONDS through `models.chat_model`.

What every tool call asked and answered is kept, in order, as `sent` (F-3,
F-6): it LEFT the house, so every idea's row says what went — not only the
two record tools' answers (2.21.6): a profile, a pattern list or an entity
list crossed the boundary just the same. EXPLORER_MAX_CALLS bounds it. Each idea
is then checked against the HOUSE (D-9, above), and a refusal stores nothing.
What passes is stored through `upsert_analyst_candidate` and
`suggestions.mark_shown`, with its chart DRAWN: `charts.draw` computes the
series and the "what the rule would have done" markers from the record before
the row is written (F-7, F-8, F-WP4), so the row holds the evidence, not a
request for it. A spec the record cannot draw keeps the idea and carries its
`error` (F-7) — the chart is the evidence, and its absence is evidence too.
A chart that DID draw and shows the rule never acting drops the idea
(`never_acted`, 2.23.1): that is not an absent chart but one showing nothing,
and a person shown it can only answer no.

The first message also carries `household_answers` (F-11, F-WP6): the
latest FEEDBACK_PROMPT_MAX answers to earlier explorer ideas — yes, no or
rolled_back, the title, the reason typed — each one only as the change
journal backs it (`household_answers`, `_answer_of`), capped in count and in
bytes.
"""
from datetime import date, datetime
import copy
import hashlib
import json
import logging
import os
import threading
import time

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from core import activity
from core import agent_runtime
from core import artefacts
from core import auditor
from core import charts
from core import disclosure
from core import models
from core import review
from core import settings
from core import suggestions
from core import watchlist
from core.detectors import power

from tools.tool_analyst import tool_submit_ideas
from tools.tool_record import overview_payload
from tools.tool_record_query import tool_record_query
from tools.tool_record_timeline import tool_record_timeline
from tools.tool_homeassistant import automations as automations_mod
from tools.tool_homeassistant import registry
from tools.tool_homeassistant._client import _ha_get

logger = logging.getLogger(__name__)

# D-10: two hours before review.REVIEW_HOUR, so the morning's review ranks what
# the analyst stored and at most one question still goes out that day.
ANALYST_HOUR = 7

RUN_PATH = os.path.join(settings.DATA_DIR, "analyst_run.json")

# D-2: what the analyst may call, by name — the same strings `agent_runtime.tools`
# answers to. Deliberately absent: every name in DESTRUCTIVE_TOOLS, the service
# call, `Roll back a change`, `Get pending messages` (collecting hands a message
# over and nobody would receive it), and `Show an agent suggestion` / `Show a
# measured result` (each puts a question, and there is no one to put it to).
# Also absent since 2.21.4: `Get Home Assistant entity history`. It reads Home
# Assistant's own recorder for ANY entity, watched or not, so it would let an
# unattended run send the history of a device the household never chose to
# have watched — the analyst's history is the watched record only. Chat keeps
# it: there a person asked. The inventory tools stay — an idea has to name real
# devices, services and areas.
# tests/test_analyst_run.py holds that none of these is gated and each exists.
ANALYST_READ_TOOLS = frozenset({
    "Datetime get tool",
    "List Home Assistant entities",
    "Get Home Assistant entity details",
    "List Home Assistant devices",
    "List Home Assistant services",
    "List Home Assistant areas",
    "List entities in Home Assistant area",
    "Get behavioural record overview",
    "Get entity behaviour profile",
    "Get observed patterns",
    "List Home Assistant automations",
    "Get Home Assistant automation",
    "List agent suggestions",
})

# The explorer run (F-WP3). The two record tools by the names they answer to:
# analyst-only, in no chat registry, so the explorer resolves them from its
# own (`_explorer_registry`).
RECORD_QUERY_TOOL = "Query the behavioural record"
RECORD_TIMELINE_TOOL = "Get entity timeline"
RECORD_TOOLS = frozenset({RECORD_QUERY_TOOL, RECORD_TIMELINE_TOOL})

# D-2's allowlist plus the record tools — what the explorer holds. Still nothing gated: tests hold that
# it never meets DESTRUCTIVE_TOOLS ∪ {SERVICE_CALL_TOOL}.
EXPLORER_READ_TOOLS = ANALYST_READ_TOOLS | RECORD_TOOLS

# F-5's budget, per run. Named EXPLORER_ rather than the proposal's ANALYST_
# because the audited run's per-pass cap held that name until F-9 retired it
# (2.23.0). Both stated in the prompt, pinned by a test.
#
# Steps went 10 → 16 in 2.19.1 on field run 1's evidence: 10 calls ran out
# with nothing submitted, about half of them spent orienting (four separate
# automation reads, the overview, patterns, suggestions) — which the first
# message now carries instead. What is left still needs room for two or three
# candidates at one or two queries each, plus the submission. A step is one
# model call, and one call may batch several tool calls, so the queries are
# not what is short: 5 of 20 were used. Queries stay at 20.
EXPLORER_MAX_STEPS = 16     # model calls — each one a strong-tier call
EXPLORER_MAX_QUERIES = 20   # calls to the two record tools together

# When this many model calls remain, one message tells the model so and to
# submit with what it has (2.19.1). Two, not one: the call that reads the
# warning may still want one last query before the call that submits. A
# budget note, not a verdict — the submission itself is still never answered.
EXPLORER_WARN_AT = 2

# F-6: ideas one `Submit ideas` may carry. Past this they are dropped, logged.
EXPLORER_IDEAS_MAX = 3

# 2.18.2's rule applied to the model: one call from the explorer's executor
# thread waits at most this long per attempt (models.MODEL_MAX_RETRIES bounds
# the attempts).
ANALYST_MODEL_TIMEOUT_SECONDS = 90

# And to its tools: each call runs on its own thread and is abandoned past
# this. The query tool bounds itself at 2 s; this is the outer wall for it and
# for every Home Assistant read the explorer holds.
EXPLORER_TOOL_TIMEOUT_SECONDS = 30

# Tool calls a run may make in all, record tools included — the bound on
# `sent`, which keeps one entry per call (2.21.6). A response that would pass
# it is refused whole, like the query budget, so a call that is not listed
# never runs: the card's list stays complete rather than cut. 20 record calls
# plus room for the orientation reads the first message usually spares.
EXPLORER_MAX_CALLS = 48

# How much of each tool answer `sent` keeps (F-6). The model saw the
# whole answer; the row keeps enough to show what kind of thing left.
SENT_ANSWER_MAX_CHARS = 2048

# F-11's prompt block (F-WP6): how many of the household's answers to earlier
# ideas the explorer's first message carries, newest first, and the most the
# block may weigh as it leaves (disclosure.emit's JSON, in UTF-8 bytes). By
# count, twenty answers of an 80-character title and a 500-character reason
# are ~12 KB of JSON, and a reason typed in a non-Latin script is escaped to
# six bytes a character — so the count alone does not bound it. The oldest
# answers go first until it fits; an answer is never cut, since half a
# reason can say the opposite of the whole one.
FEEDBACK_PROMPT_MAX = 20
FEEDBACK_PROMPT_MAX_BYTES = 8192

# How much of a refused idea's raw arguments the log keeps. The log is D-3's
# only signal that the prompt is wrong, and the first field run of the audited
# run ended in a refusal with nothing to say why. Local log only — never handed
# back to the model (refuse, never repair).
RAW_ARGS_LOG_CHARS = 1500

# Guards RUN_PATH: the run happens in an executor thread, and a read-then-
# overwrite of a corrupt file must not interleave with a stamp.
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------

def _sorted_pairs(pairs) -> tuple:
    # By repr, because a pair may hold None beside a string and Python will
    # not order those against each other.
    return tuple(sorted(pairs, key=repr))


def analyst_key(cfg):
    """D-8: `analyst:` + the first 16 hex digits of the sha1 of the config's
    signature, over the config normalised as `run_explorer` normalises it
    (`automations._normalize_automation_cfg`). None for
    a config that is not an object."""
    if not isinstance(cfg, dict):
        return None
    normalised = automations_mod._normalize_automation_cfg(copy.deepcopy(cfg))
    triggers, actions = auditor.signature(normalised)
    stable = (_sorted_pairs(triggers), _sorted_pairs(actions))
    return "analyst:" + hashlib.sha1(repr(stable).encode("utf-8")).hexdigest()[:16]


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_ideas(args):
    """F-6: `Submit ideas`' arguments → a list of `(idea | None, why, raw)`,
    one per idea the model sent, in order.

    A malformed idea is refused ALONE — nothing here is about to spend a
    strong call per idea, so the good ones are kept. Each
    needs a title, a finding, a rule in words, a config object and a chart
    object; what the chart ASKS for is judged later (`charts.draw`), because
    a chart the record cannot draw keeps the idea (F-7). Ideas past
    EXPLORER_IDEAS_MAX are refused as such. A submission with no list at all
    is one refusal."""
    ideas = args.get("ideas") if isinstance(args, dict) else None
    if not isinstance(ideas, list) or not ideas:
        return [(None, "the submission holds no ideas", args)]
    out = []
    for index, idea in enumerate(ideas):
        if index >= EXPLORER_IDEAS_MAX:
            out.append((None, f"idea {index + 1} is past the {EXPLORER_IDEAS_MAX} a run may "
                              f"submit", idea))
            continue
        if not isinstance(idea, dict):
            out.append((None, "the idea is not an object", idea))
            continue
        title, finding, rule = (_text(idea.get(k)) for k in ("title", "finding", "rule_text"))
        config, chart = idea.get("config"), idea.get("chart")
        if not title:
            why = "the idea has no title"
        elif not finding:
            why = "the idea has no finding"
        elif not rule:
            why = "the idea has no rule_text"
        elif not isinstance(config, dict) or not config:
            why = "the idea has no config"
        elif not isinstance(chart, dict):
            why = "the idea has no chart request"
        else:
            out.append(({"title": title, "finding": finding, "rule_text": rule,
                         "config": config, "chart": chart}, "", idea))
            continue
        out.append((None, why, idea))
    return out


def _chart_entities(chart) -> list:
    """The entity ids a chart column names, whatever its error."""
    spec = chart.get("spec") if isinstance(chart, dict) else None
    entities = spec.get("entities") if isinstance(spec, dict) else None
    if not isinstance(entities, list):
        return []
    return [e.strip() for e in entities if isinstance(e, str) and e.strip()]


def never_acted(chart) -> str:
    """Why a drawn chart shows its rule doing nothing, or "" (2.23.1).

    Only the one unambiguous case: a chart drawn without error, a rule inside
    the marker subset (`markers` a list, not None), no marker at all and no
    note. A note with an empty list is a rule acting on something the chart
    does not draw, or fires the record could not judge — not known to be
    nothing, so kept. A chart type that carries no markers (heatmap,
    daily_count) says nothing either way, so kept. An idea whose chart is its
    only evidence and shows nothing has no evidence to show — field run 4's
    hallway rule fired after the household had already switched the light
    off, every time, and reached the queue as a card saying so."""
    if not isinstance(chart, dict) or chart.get("error") or chart.get("markers_note"):
        return ""
    if chart.get("markers") != []:
        return ""
    spec = chart.get("spec") or {}
    try:
        days = max(1, round((int(spec["end"]) - int(spec["start"])) / 86400))
    except (KeyError, TypeError, ValueError):
        return f"its {spec.get('type', 'chart')} shows the rule would not have acted once"
    return (f"its {spec.get('type', 'chart')} over the {days} day(s) drawn shows the rule "
            f"would not have acted once")


def _sent_entry(name, args, answer) -> dict:
    """F-6: one tool call as `sent` keeps it — which tool, what it was
    asked, the answer cut to SENT_ANSWER_MAX_CHARS and whether it was cut,
    and, for a query, how many rows came back."""
    text = str(answer)
    cut = len(text) > SENT_ANSWER_MAX_CHARS
    entry = {"tool": name, "query": copy.deepcopy(args) if isinstance(args, dict) else {},
             "answer_excerpt": text[:SENT_ANSWER_MAX_CHARS] + ("…" if cut else ""),
             "rows": None, "truncated": cut}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list):
        entry["rows"] = len(parsed["rows"])
    return entry


def automation_digest(automations) -> list:
    """Every existing automation as `{alias, triggers, targets}` — the entity
    ids its trigger names and those its action names, at any depth
    (`auditor._entity_ids`). Built from the full configs the run already
    fetched for `duplicate_of`, so the orientation costs no Home Assistant
    round trip. Field run 1 spent four model calls reading these one by one;
    the entities are what the model needs to see what is taken, and the whole
    config is still one allowlisted call away."""
    digest = []
    for cfg in automations or []:
        if not isinstance(cfg, dict):
            continue
        cfg = automations_mod._normalize_automation_cfg(copy.deepcopy(cfg))
        digest.append({"alias": cfg.get("alias") or "",
                       "triggers": sorted(auditor._entity_ids(cfg.get("trigger"))),
                       "targets": sorted(auditor._entity_ids(cfg.get("action")))})
    return digest


def detector_pairs(snapshot, watched) -> list:
    """Every watched power sensor a power detector would reason over, with the
    switch it pairs to: `[{"power_sensor", "switch"}]`, sorted.

    The detectors' own two questions (`core/detectors/power.py`), asked the way
    `standby.scan` and `schedule.scan` ask them — a unit that makes it a power
    sensor, and exactly one controllable switch on its device. Derived, never
    listed: a sensor paired tomorrow is covered tomorrow, and one without a
    switch (which no detector reads) is left for the explorer."""
    states = (snapshot or {}).get("states") or {}
    entity_rows = [r for r in (snapshot or {}).get("entities") or []
                   if isinstance(r, dict) and r.get("entity_id")]
    by_device = registry.entities_by_device(entity_rows)
    registry_of = {r["entity_id"]: r for r in entity_rows}
    pairs = []
    for entity_id in sorted(watched or ()):
        row = registry_of.get(entity_id)
        if power.power_scale(states.get(entity_id), row) is None:
            continue
        switch = power.controllable_switch(entity_id, (row or {}).get("device_id"), by_device)
        if switch:
            pairs.append({"power_sensor": entity_id, "switch": switch})
    return pairs


def journal_answer(change_id, journal) -> tuple:
    """What the change journal says the household decided about one change:
    `(answer, at)`, or `("", None)` when it says nothing settled.

    `journal` maps a change id to its `activity.change` row (the undo's row
    too, when there is one). Undone → `rolled_back`, at the undo; approved
    AND applied → `yes`; rejected → `no`. Anything else — still proposed, an
    approval claimed and not yet run, a write that failed, a proposal that
    expired or was refused — is no answer that took effect."""
    change = journal.get(change_id) if change_id else None
    if not change:
        return "", None
    if change.get("undone_by"):
        undo = journal.get(change["undone_by"]) or {}
        return suggestions.FEEDBACK_ROLLED_BACK, undo.get("decided_at") or change.get("decided_at")
    if change.get("decision") == "approved" and change.get("status") == "applied":
        return "yes", change.get("decided_at")
    if change.get("decision") == "rejected":
        return "no", change.get("decided_at")
    return "", None


def _answer_of(row, journal):
    """One idea's answer as the block carries it — `(at, {title, answer,
    reason})` — or None.

    The stored feedback counts only when the journal AGREES with it, checked
    against the change it was given for. The panel records the answer at
    /rest/suggestions/feedback BEFORE it posts to /rest/approve (F-WP5's
    order, so a lost reason stops the panel before anything is decided), and
    that approve can then be refused — already answered from ASI1 chat,
    expired, lost on the way. The row keeps an answer that never took effect
    (found live on 2026-09-23). The journal holds what did, so the
    disagreement is settled here, where the answer is read, rather than by
    reordering two posts the panel sends for a reason.

    A contradicted answer — or none, for an idea answered from ASI1 chat,
    which writes no feedback — falls back to the journal's own answer with
    no reason: the reason explained an answer that did not happen, and a
    chat "no" is as much a refusal as the card's. So the block says what the
    household decided, whichever surface they used."""
    title = suggestions._one_line(row.get("title"), suggestions.TITLE_MAX_CHARS)
    feedback = row.get("feedback") if isinstance(row.get("feedback"), dict) else None
    if feedback:
        said, at = journal_answer(feedback.get("change_id") or row.get("change_id"), journal)
        if said and said == feedback.get("answer"):
            reason = suggestions._one_line(feedback.get("reason"),
                                           suggestions.FEEDBACK_REASON_MAX_CHARS)
            return feedback.get("at") or at or 0, {"title": title, "answer": said,
                                                   "reason": reason}
    said, at = journal_answer(row.get("change_id"), journal)
    if not said:
        return None
    return at or 0, {"title": title, "answer": said, "reason": ""}


def household_answers(rows, journal) -> list:
    """F-11's block: the latest FEEDBACK_PROMPT_MAX answers the journal backs,
    newest first, `[{title, answer, reason}]` — the oldest dropped until the
    block, as disclosure.emit writes it, fits FEEDBACK_PROMPT_MAX_BYTES.

    The reason is text a household member typed, arriving in a model prompt.
    It stays a JSON string value under the fixed key `household_answers`:
    json.dumps escapes every quote, brace and line break in it, so nothing it
    says can close its string and read as a new key or a new section; it is
    collapsed to one line and capped again here (scheduler_llm._clean_name's
    rule for a household's text inside an instruction); and the explorer's
    prompt says what the block is for and that it is never an instruction."""
    answers = [a for a in (_answer_of(row, journal) for row in rows or []) if a]
    answers.sort(key=lambda pair: pair[0], reverse=True)
    block = [entry for _at, entry in answers[:FEEDBACK_PROMPT_MAX]]
    while block and len(disclosure.emit(block).encode("utf-8")) > FEEDBACK_PROMPT_MAX_BYTES:
        block.pop()
    return block


def explorer_prompt() -> str:
    """The explorer's system prompt with this module's budget in it — stated
    from the constants the loop enforces, so the two cannot drift."""
    return settings.analyst_explorer_message(EXPLORER_MAX_STEPS, EXPLORER_MAX_QUERIES,
                                             EXPLORER_WARN_AT, FEEDBACK_PROMPT_MAX)


def _raw(args) -> str:
    """A submission's arguments as the model sent them, cut to
    RAW_ARGS_LOG_CHARS for the log line beside a refusal."""
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(args)
    if len(text) > RAW_ARGS_LOG_CHARS:
        text = text[:RAW_ARGS_LOG_CHARS] + f"… ({len(text)} chars)"
    return text


def _local_date(ts) -> str:
    return date.fromtimestamp(int(ts)).isoformat()


def due(now, last_date=None) -> bool:
    """D-10: after ANALYST_HOUR local, and not already run on this local date.
    `last_date` is what `last_run_date()` answers, handed in so the rule is
    one a test can walk across midnight."""
    now = int(now)
    if time.localtime(now).tm_hour < ANALYST_HOUR:
        return False
    return last_date != _local_date(now)


# ---------------------------------------------------------------------------
# I/O — the run-date file
# ---------------------------------------------------------------------------

def _write_locked(state: dict) -> bool:
    tmp = f"{RUN_PATH}.tmp"
    try:
        os.makedirs(os.path.dirname(RUN_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, RUN_PATH)
        return True
    except OSError as e:
        logger.error(f"ANALYST: failed to persist {RUN_PATH}: {e}")
        return False


def last_run_date(now=None):
    """The local date the analyst last ran, as `YYYY-MM-DD`, or None if it
    never has.

    A missing file is a fresh install. A file that exists but cannot be read
    fails closed: today counts as already run, and today is written back, so a
    crash loop over a corrupt file cannot re-run the analyst on every
    restart."""
    today = _local_date(time.time() if now is None else now)
    with _lock:
        try:
            with open(RUN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("date"), str):
                raise ValueError("unexpected structure")
            date.fromisoformat(data["date"])
            return data["date"]
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeDecodeError) as e:
            logger.warning(f"ANALYST: could not read {RUN_PATH} ({e}) — failing closed, "
                           f"today treated as already run")
            _write_locked({"date": today})
            return today


def stamp_run(now=None) -> bool:
    """Records that the analyst runs today, BEFORE it runs. Returns whether
    the stamp was written — a run whose stamp could not be written is one a
    restart would repeat, so the caller does not start it."""
    stamp = {"date": _local_date(time.time() if now is None else now)}
    with _lock:
        return _write_locked(stamp)


# ---------------------------------------------------------------------------
# I/O — the run
# ---------------------------------------------------------------------------

def _preconditions():
    """D-11, before any model is built: → (why skipped, None) or ("", the
    registry snapshot, the services list, the automation configs).

    `/services` and the automation configs are read here too, once, for the
    same reason the registry is: every idea's house check is handed them
    (`auditor.check_config`, `duplicate_of`), and a run that cannot read them
    would drop every idea — after paying for it."""
    if not settings.ANALYST_ENABLED:
        return "analyst_enabled is off", None
    if not settings.ASI1_API_KEY:
        return "ASI1_API_KEY is empty", None
    if not watchlist.watched_entities():
        return "the watchlist is empty, so there is no record to explore", None
    ok, snapshot = registry.fetch_registry()
    if not ok:
        return f"could not read the Home Assistant registry ({snapshot})", None
    ok, services = _ha_get("/services")
    if not ok:
        return f"could not read Home Assistant's services ({services})", None
    ok, automations = auditor._fetch_automation_configs()
    if not ok:
        return f"could not read the existing automations ({automations})", None
    return "", (snapshot, services, automations)


def _queue_payload(now) -> dict:
    """D-12: what is already queued, so the model does not raise it again,
    and the detectors' findings as facts."""
    rows = suggestions.active(now=now)
    detector_rows = [r for r in rows if r.get("source") != suggestions.SOURCE_ANALYST]
    return {
        "open_suggestions": [
            {"title": row.get("title") or artefacts.headline(row),
             "source": row.get("source"), "state": row.get("state")}
            for row in rows
        ],
        "detector_findings": artefacts.digest_text(review.rank(detector_rows)),
    }


def _household_answers() -> list:
    """`household_answers` over the store and the journal: every answered
    idea's row, and the journal rows its answers are checked against (the
    undo's too). Local SQLite reads only."""
    rows = suggestions.answered_ideas()
    journal = {}
    for row in rows:
        feedback = row.get("feedback") if isinstance(row.get("feedback"), dict) else {}
        for change_id in (row.get("change_id"), feedback.get("change_id")):
            if change_id and change_id not in journal:
                change = activity.change(change_id)
                journal[change_id] = change
                if change and change.get("undone_by"):
                    journal[change["undone_by"]] = activity.change(change["undone_by"])
    return household_answers(rows, journal)


def _explorer_context(now, snapshot, automations, answers=None) -> str:
    """The explorer's first message (2.19.1): the orientation field run 1
    spent half its budget fetching, handed over instead. The record overview
    (the overview tool's own builder), every existing automation's entities
    (`automation_digest`, from the configs `_preconditions` fetched), the
    sensors the power detectors cover (`detector_pairs`, from the registry
    snapshot it fetched), `_queue_payload`, and F-11's
    `household_answers` (F-WP6). No new Home Assistant call. One payload
    through the boundary — every part of it leaves."""
    payload = {
        "record_overview": overview_payload(),
        "existing_automations": automation_digest(automations),
        "covered_by_detectors": detector_pairs(snapshot, watchlist.watched_entities()),
    }
    payload.update(_queue_payload(now))
    payload["household_answers"] = list(answers or [])
    return disclosure.emit(payload)


# ---------------------------------------------------------------------------
# I/O — the explorer run (F-WP3)
# ---------------------------------------------------------------------------

def _explorer_registry() -> dict:
    """EXPLORER_READ_TOOLS, name → the instance that answers to it.

    ANALYST_READ_TOOLS are chat's very instances, looked up by name; the
    two record tools are built here, because
    they are in no other registry — proposal §6 keeps them from chat, and
    `agent_runtime._execute_tool_sync` could not find them if it were asked.
    A test holds that every allowlisted name resolves."""
    registry = {t.name: t for t in agent_runtime.tools if t.name in ANALYST_READ_TOOLS}
    for tool in (tool_record_query(), tool_record_timeline()):
        registry[tool.name] = tool
    return registry


def _run_bounded(tool, args) -> str:
    """One tool call on its own daemon thread, abandoned past
    EXPLORER_TOOL_TIMEOUT_SECONDS with an error the model can read. A thread
    that never returns is left behind rather than waited on: 2.18.2's rule
    is that nothing holds the run open on I/O nobody bounded, and a stuck
    read must cost the run one answer, not the run."""
    box = {}

    def work():
        try:
            box["answer"] = tool._run(**args)
        except Exception as e:
            box["answer"] = disclosure.text(f"Tool error: {e}")

    worker = threading.Thread(target=work, name="analyst-tool", daemon=True)
    worker.start()
    worker.join(EXPLORER_TOOL_TIMEOUT_SECONDS)
    if worker.is_alive():
        logger.warning(f"ANALYST: {tool.name} did not answer in "
                       f"{EXPLORER_TOOL_TIMEOUT_SECONDS}s — abandoned")
        return disclosure.emit({"error": f"the tool did not answer within "
                                         f"{EXPLORER_TOOL_TIMEOUT_SECONDS} s"})
    return box.get("answer", "")


def _explore(system, user, now, summary):
    """The explorer's one pass (F-4, F-5; D-1..D-3's rules) → (the `Submit
    ideas` arguments, "", sent, model) or (None, why, sent, model).

    Synchronous throughout — `invoke`, not `ainvoke`: the run is in an
    executor thread with no event loop of its own, and the read tools are
    blocking `_run`s anyway; one call at a time is the point. Before EVERY model call
    one strong-tier call is taken; None ends the pass — the run's model is the
    strong tier or nothing (§4.1). Each response is checked whole before any
    of it runs: a name outside EXPLORER_READ_TOOLS ends the pass (`ANALYST:
    refused — called <name>`); a response whose record-tool calls would take
    the run past EXPLORER_MAX_QUERIES runs none of them (`ANALYST: budget —
    …`, and see below); `Submit ideas` is captured, never answered. A
    last permitted model call that does not submit ends the pass without its
    reads being run — nothing could read their answers. `summary` counts
    `steps` and `queries` as they are spent.

    Two budget notes, neither a verdict (2.19.1). A response past the query
    budget runs none of its calls, but every call in it is ANSWERED with what
    the budget still allows (the API needs an answer per call, and a pass
    that just ended taught field run 1 nothing), and the pass goes on — the
    model may still submit or ask for less. And when EXPLORER_WARN_AT model
    calls remain, one message after that step's answers says how many calls
    and queries are left and to submit with what it has. Once: steps only
    count up, so the count equals EXPLORER_WARN_AT at one step alone.

    A repeat is not run (2.23.1): a call whose `_call_key` already ran this
    pass — in an earlier response or earlier in the same one — is answered
    with `_repeat_note` and spends neither the query budget nor a place in
    `sent`, and the budget checks count only the calls that would run.
    `summary["repeats"]` counts them. A call REFUSED for budget never ran, so
    asking it again is not a repeat."""
    registry = _explorer_registry()
    submit = tool_submit_ideas()
    sent = []
    ran_at = {}     # _call_key → the turn that ran it
    llm = model = None
    history = [SystemMessage(content=system), HumanMessage(content=user)]

    for step in range(EXPLORER_MAX_STEPS):
        strong = models.take_strong_call(datetime.fromtimestamp(now).astimezone())
        if strong is None:
            logger.info(f"ANALYST: over the strong-tier cap after {step} model call(s) — "
                        f"the explorer run ends here")
            return None, "over the strong-tier cap", sent, model
        if llm is None:
            model = strong
            try:
                llm = models.chat_model(strong, tier=models.STRONG,
                                        timeout=ANALYST_MODEL_TIMEOUT_SECONDS
                                        ).bind_tools(list(registry.values()) + [submit])
            except Exception as e:
                return None, f"could not build {strong}: {e}", sent, model
        summary["steps"] += 1
        try:
            response = llm.invoke(history)
        except Exception as e:
            return None, f"the model call failed: {e}", sent, model
        history.append(response)
        calls = list(getattr(response, "tool_calls", None) or [])
        if not calls:
            return None, f"the pass ended without calling {submit.name!r}", sent, model

        for call in calls:
            name = call.get("name")
            if name != submit.name and name not in EXPLORER_READ_TOOLS:
                logger.warning(f"ANALYST: refused — called {name}")
                return None, f"called {name}", sent, model
        for call in calls:
            if call.get("name") == submit.name:
                return call.get("args") or {}, "", sent, model

        if step == EXPLORER_MAX_STEPS - 1:
            break

        # A repeat spends nothing, so the budget checks count only the calls
        # that would run: a response of repeats is never refused for budget.
        fresh, keys = [], set()
        for call in calls:
            key = _call_key(call)
            if key not in ran_at and key not in keys:
                fresh.append(call)
                keys.add(key)
        asked = sum(1 for call in fresh if call.get("name") in RECORD_TOOLS)
        left = EXPLORER_MAX_QUERIES - summary["queries"]
        calls_left = EXPLORER_MAX_CALLS - len(sent)
        if len(fresh) > calls_left:
            logger.info(f"ANALYST: budget — {len(fresh)} tool call(s) asked for, {calls_left} of "
                        f"{EXPLORER_MAX_CALLS} left; the response is refused, none of it run")
            refusal = disclosure.emit({"error": _calls_refusal(len(fresh), calls_left)})
            for call in calls:
                history.append(ToolMessage(content=refusal, tool_call_id=call.get("id")))
        elif asked > left:
            logger.info(f"ANALYST: budget — {asked} record-tool call(s) asked for, {left} of "
                        f"{EXPLORER_MAX_QUERIES} left; the response is refused, none of it run")
            refusal = disclosure.emit({"error": _query_refusal(asked, left)})
            for call in calls:
                history.append(ToolMessage(content=refusal, tool_call_id=call.get("id")))
        else:
            for call in calls:
                name, args = call["name"], call.get("args") or {}
                key = _call_key(call)
                if key in ran_at:
                    summary["repeats"] += 1
                    logger.info(f"ANALYST: repeat — {name} asked again with the arguments of "
                                f"turn {ran_at[key]}; not run")
                    history.append(ToolMessage(
                        content=disclosure.emit({"error": _repeat_note(ran_at[key])}),
                        tool_call_id=call.get("id")))
                    continue
                ran_at[key] = summary["steps"]
                logger.info(f"ANALYST: TOOL CALL {name} | args: {args}")
                answer = _run_bounded(registry[name], args if isinstance(args, dict) else {})
                if name in RECORD_TOOLS:
                    summary["queries"] += 1
                sent.append(_sent_entry(name, args, answer))
                history.append(ToolMessage(content=str(answer), tool_call_id=call.get("id")))

        if EXPLORER_MAX_STEPS - summary["steps"] == EXPLORER_WARN_AT:
            history.append(HumanMessage(content=_budget_warning(
                EXPLORER_WARN_AT, EXPLORER_MAX_QUERIES - summary["queries"])))

    logger.info(f"ANALYST: budget — {EXPLORER_MAX_STEPS} model calls without "
                f"{submit.name!r}; nothing submitted")
    return None, "the step budget ran out", sent, model


def _call_key(call) -> tuple:
    """What makes two tool calls the same call: the name and the arguments,
    key order ignored. Exact, not "equivalent SQL" — field run 4 sent one
    query eleven times character for character, and a looser match would have
    to decide when two different strings ask the same thing."""
    return call.get("name"), json.dumps(call.get("args") or {}, sort_keys=True, default=str)


def _repeat_note(turn) -> str:
    """The answer to a call this run already made. Our own words, no house
    data: the earlier answer is already in the conversation the model reads,
    so nothing leaves the house again and `sent` gains nothing."""
    return (f"Not run: you made this exact call at turn {turn} of this run, and its answer is "
            f"above. A repeat costs no query and would return the same answer. Read that answer, "
            f"ask something different, or call 'Submit ideas'.")


def _query_refusal(asked, left) -> str:
    """The answer to every call in a response past the query budget."""
    return (f"Not run: this response asked for {asked} record-tool call(s) and only {left} of "
            f"{EXPLORER_MAX_QUERIES} remain, so none of it ran. Ask for at most {left}, or call "
            f"'Submit ideas' with what you have.")


def _calls_refusal(asked, left) -> str:
    """The answer to every call in a response past EXPLORER_MAX_CALLS."""
    return (f"Not run: this response asked for {asked} tool call(s) and only {left} of "
            f"{EXPLORER_MAX_CALLS} tool calls this run remain, so none of it ran. Ask for at "
            f"most {left}, or call 'Submit ideas' with what you have.")


def _budget_warning(steps_left, queries_left) -> str:
    """The one in-loop budget note (2.19.1). Our own words, no house data."""
    return (f"Budget: {steps_left} model calls remain, the last of them for 'Submit ideas', and "
            f"{queries_left} record-tool calls. Call 'Submit ideas' now with the best of what "
            f"you have found; a run that does not submit stores nothing.")


def _store_shown(idea, cfg, chart, sent, model) -> bool:
    """D-9's second half for an idea, reached only once the house checks
    passed. The key is the config's signature (D-8); `entities` is every
    entity the config names and every one its chart does, so
    `review.claimed_entities` keeps a second question off any of them. The
    finding is the row's `rationale` too — it is what "same words" is
    compared on, so a reworded finding is a new claim and a repeated one only
    touches the row. At `candidate` the row takes `mark_shown`; at
    `validated` it was the same words and keeps what it has; at `proposed`
    or later it is the user's."""
    entities = sorted(set(auditor._entity_ids(cfg)) | set(_chart_entities(chart)))
    suggestion_id = suggestions.upsert_analyst_candidate(
        analyst_key(cfg), entities, idea["title"], idea["finding"], model)
    if suggestion_id is None:
        return False
    row = suggestions.suggestion(suggestion_id) or {}
    if row.get("state") == "candidate":
        return suggestions.mark_shown(suggestion_id, cfg, idea["finding"], idea["rule_text"],
                                      chart, sent)
    return True


def _log_explorer_summary(summary) -> dict:
    logger.info(f"ANALYST: explorer — {summary['steps']} model call(s), {summary['queries']} "
                f"record-tool call(s), {summary['repeats']} repeat(s) not run, "
                f"{summary['submitted']} idea(s) submitted, "
                f"{summary['refused']} refused, {summary['dropped']} dropped, "
                f"{summary['stored']} stored")
    return summary


def run_explorer(now=None) -> dict:
    """One explorer run (F-WP3). Returns a count of what it did, for the log.

    Blocking throughout, so it runs in an executor. It neither checks `due`
    nor stamps — tasks/analyst.py does both first, stamping BEFORE calling
    this (D-10). Preconditions (D-11) before any model is built. Then
    `_explore`. Then per idea: malformed → refused alone, logged
    with its raw arguments; `check_config` / `duplicate_of` against the
    snapshot, services and automations fetched once → dropped, logged, stored
    nothing; otherwise the chart is drawn (`charts.draw`, F-WP4); a chart
    showing the rule never acting (`never_acted`) → dropped the same way;
    otherwise the idea is stored with it, whatever the chart's error."""
    now = int(now) if now else int(time.time())
    summary = {"steps": 0, "queries": 0, "repeats": 0, "submitted": 0, "refused": 0,
               "dropped": 0, "stored": 0, "skipped": "", "ended": ""}

    why, fetched = _preconditions()
    if why:
        logger.info(f"ANALYST: skipped — {why}")
        summary["skipped"] = why
        return summary
    snapshot, services, automations = fetched

    system = explorer_prompt() + settings.get_liveSystemMesaageData()
    # F-11 (F-WP6): the household's latest answers to earlier ideas join the
    # user message — data under its own key, through the same emit — never
    # the system prompt, which says only what the block is for.
    user = _explorer_context(now, snapshot, automations, _household_answers())
    args, why, sent, model = _explore(system, user, now, summary)
    if args is None:
        summary["ended"] = why
        logger.info(f"ANALYST: nothing submitted — {why}")
        return _log_explorer_summary(summary)

    states = snapshot.get("states") or {}
    for idea, why, raw in validate_ideas(args):
        if idea is None:
            summary["refused"] += 1
            logger.info(f"ANALYST: refused an idea — {why} | raw: {_raw(raw)}")
            continue
        summary["submitted"] += 1

        cfg = automations_mod._normalize_automation_cfg(copy.deepcopy(idea["config"]))
        why = auditor.check_config(cfg, states, services)
        if not why:
            duplicate = auditor.duplicate_of(cfg, automations)
            why = f"duplicates {duplicate}'s trigger and action" if duplicate else ""
        if why:
            summary["dropped"] += 1
            logger.info(f"ANALYST: dropped {idea['title']!r} — {why}")
            continue

        chart = charts.draw(idea["chart"], cfg, now)
        why = never_acted(chart)
        if why:
            summary["dropped"] += 1
            logger.info(f"ANALYST: dropped {idea['title']!r} — {why}")
            continue
        if _store_shown(idea, cfg, chart, sent, model):
            summary["stored"] += 1
            logger.info(f"ANALYST: stored {idea['title']!r} (shown"
                        + (f"; chart: {chart['error']}" if chart["error"] else "") + ")")

    return _log_explorer_summary(summary)
