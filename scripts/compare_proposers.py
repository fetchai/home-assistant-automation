#!/usr/bin/env python3
"""D1 vs D2, over a real record: the harness proposal §7.3's WP5 is.

docs/PROPOSAL-SCHEDULE-DETECTOR.md §5 held stage D2 back "until there is
something to compare its answers against". This is that something. Decision
3 chose a free-form model answer over a fixed candidate set, which is the
right call for the reason §7.2 gives — a validator that refuses whatever the
grid does not back is a stronger safety boundary than a menu the detector
enumerated — but it has a cost this script exists to pay: D2's answer is not
reproducible run to run, so there is no fixed expected output to assert
against in a test. The only way to find out whether the model is any good is
to run it against a real household's record and grade what comes back the
same way the shipped pass would, which is what this does.

It is also what keeps core/scheduler_llm.py (WP2) exercised against a real
ASI1 response for as long as the soak (D-6) runs, rather than sitting behind
a stub the whole time.

READ-ONLY. ABSOLUTELY.
-----------------------
This script computes two candidate windows per entity and grades both, and
it stops there. It never calls suggestions.upsert_candidate, gate.propose, or
anything else that records a judgement, and it never writes to record.db,
activity.db, the schedule cache, or Home Assistant. A harness that polluted
the store would corrupt the very soak it exists to inform — D-6 counts real
proposals, and a row this script planted would be counted as one. The one
piece of Home Assistant traffic it makes is the same one core/builder.py's
`build` always makes on a candidate — `_preflight_automation`'s test-create,
which core/review.py's own validation pass makes for exactly the same
candidates every day and which builder.py's own docstring is explicit writes
nothing that survives the call.

Nothing here calls core/review.py. `_validate` there does exactly the
build-then-grade sequence this script needs — builder.build, then
replay.run over the same rows — and this copies that SHAPE rather than the
function, because review.py's own imports (gate, suggestions, activity) are
the store this script must stay away from, and the shortest way to guarantee
that is to never import the module that imports them.

WHAT "D1" AND "D2" MEAN HERE
-----------------------------
For every watched entity whose record produces a weekly grid (two levels a
threshold can separate, `core.detectors.schedule.device_levels`):

  D1  the window `schedule.schedules` picks — the identical-runs scan, and
      what the add-on shipped up to 2.14.14. Called by name: since 2.14.15
      `find_candidate` picks from `backed_windows` instead, so reading this
      column off a candidate would report the two deterministic proposers as
      agreeing always.
  D2  `core.scheduler_llm`'s public surface — `prompt_for`, `parse_answer`,
      `refusal` — called directly rather than through `propose`, because
      `propose` collapses a refusal to `None` and logs the reason, and D-3's
      last paragraph is explicit that the refusal IS the finding: "a run
      where D2 is refused nine times out of ten is telling you the prompt is
      wrong." This script needs the raw answer and the reason beside the
      grade, which is more than `propose` returns.
  D1+ `schedule.backed_windows(grid, couple_weekend=True)` — the same
      deterministic scan, without `schedules()`'s identical-runs
      restriction, plus the weekend-coupling rule §3 stage D2's own
      Mon-Fri-plus-Saturday example needs. Proposal §7.5's bench table is why
      this is here at all: two of its five ragged shapes close most of the
      gap to D2 with no model in the loop, which is §7's own open question
      and is what this third column exists to answer. Picked the SAME way D1
      is — the best entry that does not span midnight, not simply the
      first — because `find_candidate` filters exactly that out for
      `builder._no_midnight`'s reason and a harness that skipped the filter
      would be grading a window the shipped pass could never build.

All three, when they produce a window, are built and replayed exactly the way
core/review.py grades a live candidate, so they are compared on the one
measure that decides anything downstream: would this window's rule have cut
power to a device in use.

THE CACHE IS DELIBERATELY NOT USED
-----------------------------------
core/schedule_cache.py (WP3) exists so an unchanged grid costs no call in the
shipped pass. This script is measuring the proposer itself, and a cache hit
would silently hand back yesterday's answer and hide today's variance — which
is the exact thing decision 3's last paragraph says this harness has to
measure. So it is never imported here.

RUNNING IT
----------
Needs a real record, so it runs the way scripts/plant_standby.py does —
inside the add-on's own container, where /data is mounted:

    docker exec -it addon_<slug>_fetchai-automation-agent python3 - < scripts/compare_proposers.py

or copy the scripts directory in and run it there.

    python3 scripts/compare_proposers.py --dry-run
    python3 scripts/compare_proposers.py
    python3 scripts/compare_proposers.py --json /tmp/run1.json
    python3 scripts/compare_proposers.py --data-dir /path/to/a/copy/of/data
    python3 scripts/compare_proposers.py --stability 7 --dry-run

`--dry-run` renders each entity's grid and the exact prompt `core.scheduler_llm`
would send, and calls nothing — no ASI1 key needed. Without it, `ASI1_API_KEY`
must be set in the environment; a missing key is reported and the script
exits, not a traceback. The call is a small stdlib `urllib` POST to ASI1's
OpenAI-compatible endpoint rather than a real langchain client, for the same
reason the AMENDMENT to core/scheduler_llm.py exists: this script has to run
on stdlib alone, like every other script in this directory, and
`propose(grid, client=...)` accepts anything with `.invoke(prompt)`.

`--stability N` re-runs D1's and D1+'s picks with `now` shifted back 1..N
days and reports, per entity, how many distinct windows each produced —
`_stability_report`'s own docstring carries the argument, including why D2
is deliberately left out of it. It builds and replays nothing, so it costs
nothing beyond what D1 and D1+ already cost for free, and composes with
`--dry-run` exactly as the rest of this section does.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# review.MAX_WRONG_NIGHTS, restated rather than imported (named for this
# kind's own noun — review.py's table calls it "nights" for one shape and
# "occurrences" for this one; the VALUE is what must stay in step, not the
# name). Duplicated instead of pulled in from core/review.py for the reason
# given in the module docstring above: that module's import graph (gate,
# suggestions, activity) is the store this script must stay away from, and
# one small constant is a cheaper duplicate than a harness that reaches past
# it for one number — the same trade core/scheduler_llm.py itself makes
# about BASE_URL.
# --------------------------------------------------------------------------
MAX_WRONG_OCCURRENCES = 0

# How much of the model's raw answer the TEXT report shows inline. The JSON
# report (--json) always carries the whole thing; this is only about keeping
# a terminal readable. scheduler_llm._EXCERPT_CHARS makes the same trade for
# its log line, at a shorter length because a log line is one of many and
# this report is the one place a refusal's exact wording is meant to be read.
_RAW_EXCERPT_CHARS = 400


# --------------------------------------------------------------------------
# finding agent_home — scripts/plant_standby.py's own resolution, repeated
# rather than imported: it runs before DATA_DIR can be set, so nothing under
# agent_home/ may be imported yet, this script's own copy included.
# --------------------------------------------------------------------------
def _add_agent_home_to_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.environ.get("AGENT_HOME"), "/app",
                      os.path.join(os.path.dirname(here),
                                   "fetchai-automation-agent", "agent_home")):
        if candidate and os.path.isdir(os.path.join(candidate, "core")):
            sys.path.insert(0, candidate)
            return
    sys.exit("could not find agent_home — run this inside the add-on container, "
              "or set AGENT_HOME to a checkout's agent_home directory")


def _bootstrap(data_dir):
    """Sets DATA_DIR and imports agent_home, in that order.

    core/settings.py computes every store path (DATA_DIR itself, and
    everything derived from it) at IMPORT time — tests/conftest.py's own
    first move is this same override, for the same reason. Nothing under
    agent_home/ may be imported before this line runs.
    """
    os.environ["DATA_DIR"] = data_dir
    _add_agent_home_to_path()
    global settings, window, builder, replay, record, scheduler_llm, schedule, power, registry
    from core import settings
    from core import window
    from core import builder
    from core import replay
    from core import record
    from core import scheduler_llm
    from core import watchlist
    from core.detectors import schedule
    from core.detectors import power
    from tools.tool_homeassistant import registry
    return watchlist


# --------------------------------------------------------------------------
# the live client — stdlib only, per the AMENDMENT's whole point
# --------------------------------------------------------------------------
class _ASI1Client:
    """`.invoke(prompt) -> str`, over ASI1's OpenAI-compatible endpoint.

    Deliberately not langchain's `ChatOpenAI`: this script has to run on
    stdlib alone like its neighbours in scripts/, and `propose`'s (and this
    script's own) `client` seam takes anything with `.invoke`. Pinned to
    `scheduler_llm.TEMPERATURE` (zero) and timed out at
    `scheduler_llm.TIMEOUT_SECONDS`, the same numbers the shipped pass would
    use, so a call made here says something about what the shipped pass would
    have seen.
    """

    def __init__(self, base_url, model, api_key, timeout):
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    def invoke(self, prompt):
        payload = json.dumps({
            "model": self._model,
            "temperature": scheduler_llm.TEMPERATURE,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        request = urllib.request.Request(
            self._url, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._api_key}"})
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------
# D2: scheduler_llm's public surface, unpacked so a refusal is legible
# --------------------------------------------------------------------------
def _empty_d2_result(prompt):
    """The shape every D2 result carries, before anything is known — shared
    by `_ask_d2` and `--dry-run`'s placeholder so the two never drift apart
    on what a caller may read off a `d2` dict."""
    return {"prompt": prompt, "raw": None, "call_error": None,
            "parse_error": None, "declined": False, "spec": None,
            "refusal": None}


def _ask_d2(grid, name, client):
    """One call, scheduler_llm.propose's own steps taken apart.

    `propose` collapses everything past the call into a spec or a logged
    None, and decision 3's last paragraph is explicit that the refusal is
    the finding this harness exists to produce — so this calls `prompt_for`,
    `parse_answer` and `refusal` directly rather than `propose`, to keep the
    raw answer and the exact reason. Same one call, same no-repair, no-retry
    rule; nothing here asks the model twice or corrects what it said.
    """
    prompt = scheduler_llm.prompt_for(grid, name)
    result = _empty_d2_result(prompt)
    try:
        answer = client.invoke(prompt)
    except Exception as error:
        result["call_error"] = str(error)
        return result
    result["raw"] = answer if isinstance(answer, str) else str(answer)
    try:
        spec = scheduler_llm.parse_answer(answer)
    except ValueError as error:
        result["parse_error"] = str(error)
        return result
    if spec is None:
        result["declined"] = True
        return result
    result["spec"] = spec
    result["refusal"] = scheduler_llm.refusal(grid, spec)
    return result


# --------------------------------------------------------------------------
# building and grading one window — core/review.py's _validate, copied in
# shape only
# --------------------------------------------------------------------------
def _as_float(value):
    """`core/observer.py`'s `as_float`, the numeric-or-None part only — a
    replay blob's own fields are already numbers or None, never a raw Home
    Assistant state string, so the availability-string handling that
    function also does has nothing to catch here. Restated rather than
    imported for the same reason `MAX_WRONG_OCCURRENCES` is above."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _disqualified(replayed, kind):
    """review.disqualified, restated locally — see the module docstring for
    why this is a duplicate rather than an import.

    Keeps review.py's `would_have_interrupted` fallback: a blob that cannot
    name HOW OFTEN the rule would have been wrong, but that says it WOULD
    have interrupted at all, must still be a refusal — reading a missing
    count as zero would report a dangerous window as clean.
    """
    fields = replay.fields_for(kind)
    if fields is None:
        return f"no replay vocabulary for a {kind!r} candidate"
    blob = replayed or {}
    counted = _as_float(blob.get(fields["wrong"]))
    if counted is None and blob.get("would_have_interrupted"):
        return ("the record shows it would have cut power to a device in use, and "
                "this script cannot read how often from the grading it was given")
    wrong = int(counted or 0)
    if wrong > MAX_WRONG_OCCURRENCES:
        examined = blob.get(fields["examined"])
        return (f"would have cut power to a device in use on {wrong} of "
                f"{examined} {fields['noun']}s")
    return None


def _grade(entity_id, switch_entity_id, name, scale, base_evidence, spec, rows, now):
    """builder.build then replay.run over `spec`, review._validate's own
    sequence. Never raises; every failure is a labelled None in the result.
    """
    result = {"window": window.label(spec), "build_error": None,
              "replay_error": None, "disqualified": None,
              "replay": None, "estimate": None}
    evidence = {
        **base_evidence,
        "schedule": {"start": window.starts_at(spec), "end": window.ends_at(spec),
                     "days": sorted(spec.days)},
        "schedule_window": window.label(spec),
    }
    candidate = {"key": f"compare_proposers:{schedule.KIND}:{entity_id}",
                 "kind": schedule.KIND, "entities": [entity_id, switch_entity_id],
                 "evidence": evidence}
    built = builder.build(candidate, name=name, scale=scale)
    if not built.get("ok"):
        result["build_error"] = str(built.get("error"))
        return result
    graded, error = replay.run(built["config"], entity_id, rows, built["kind"],
                               scale=scale, evidence=evidence, now=now)
    if error:
        result["replay_error"] = str(error)
        return result
    result["replay"] = graded["replay"]
    result["estimate"] = graded["estimate"]
    result["disqualified"] = _disqualified(graded["replay"], built["kind"])
    return result


def _grade_reusing(entity_id, switch_entity_id, name, scale, base_evidence, spec, rows,
                    now, graded_so_far):
    """`_grade`, but handing back an EARLIER grade outright when `spec` is
    byte-for-byte one already computed in this report.

    `graded_so_far` is `[(spec, grade), ...]` for whichever of D1 and D1+ this
    call comes after. With a third proposer in the harness, three windows
    landing on the same spec is now the ORDINARY case — the grid, not the
    picker, is usually what decides the answer, and D1, D1+ and a validated D2
    agree on it far more often than any one of them differs from another. Re-
    grading an identical spec would ask Home Assistant to validate, and replay
    to grade, a config that is byte-for-byte one already built and replayed;
    reusing it instead pays that cost once per DISTINCT window this entity's
    grid produced rather than once per proposer that reached it.
    """
    for earlier_spec, earlier_grade in graded_so_far:
        if earlier_spec == spec:
            return earlier_grade
    return _grade(entity_id, switch_entity_id, name, scale, base_evidence, spec, rows, now)


def _best_window(found):
    """The best entry in a `schedules()`/`backed_windows()` list that does not
    span midnight — `find_candidate`'s own picking rule, applied to either
    list the same way. A run ending at local midnight is a true claim
    `builder._no_midnight` still refuses to build, so the entry ranked first
    is not always the one worth grading; the next distinct entry still is.
    """
    return next((run for run in found if not window.spans_midnight(run["window"])), None)


def _device_hours(grade):
    """The `device_hours_per_month` a graded window actually measured:
    `hours_per_occurrence x occurrences_per_month` off its own `estimate`,
    the same product `schedules()`/`backed_windows()` rank their own
    candidates by (see their `device_hours_per_month`). Read off the GRADE
    rather than off either ranked list, because D2's window never appears in
    one — this is the one figure all three proposers' windows carry, once
    graded, regardless of which one proposed the spec.
    """
    estimate = (grade or {}).get("estimate") or {}
    hours_per_occurrence = estimate.get("hours_per_occurrence")
    per_month = estimate.get("occurrences_per_month")
    if hours_per_occurrence is None or per_month is None:
        return None
    return hours_per_occurrence * per_month


def _preferred(entries):
    """Of the windows a grid actually produced, which one the record likes
    best — proposal §7.5's own ranking (`device_hours_per_month`) applied
    ACROSS proposers rather than within one.

    `entries` is `(name, spec, grade)` for D1, D1+ and D2, in that order.
    Ranked only among entries whose grade came back with `disqualified` None —
    a window that would have cut power to a device in use is not in the
    running at any saving, whatever it claims to be worth — and among those,
    by `_device_hours`, tie-broken by the window's own start (`window.starts_at`)
    so two runs of this script order identically.

    The tie-break decides an ORDER, not the fact of a tie: when two or more
    entries share the top score exactly, that is reported rather than
    silently resolved by whichever sorts first, because three proposers
    landing on the same spec — the common case `_grade_reusing` now expects —
    means their scores are not merely close, they are the same number.
    """
    ranked = []
    for proposer_name, spec, grade in entries:
        if spec is None or grade is None or grade.get("disqualified") is not None:
            continue
        hours = _device_hours(grade)
        if hours is None:
            continue
        ranked.append((proposer_name, hours, window.starts_at(spec)))
    if not ranked:
        return {"winner": None, "tie": False, "candidates": [],
                "device_hours_per_month": None}
    ranked.sort(key=lambda entry: (-entry[1], entry[2]))
    top_hours = ranked[0][1]
    tied = [entry[0] for entry in ranked if entry[1] == top_hours]
    return {"winner": ranked[0][0], "tie": len(tied) > 1, "candidates": tied,
            "device_hours_per_month": round(top_hours, 2)}


# --------------------------------------------------------------------------
# one entity, start to finish
# --------------------------------------------------------------------------
def _entity_report(entity_id, switch_entity_id, name, scale, client, dry_run, now):
    start = now - schedule.LOOKBACK_DAYS * 86400
    rows = record.between(entity_id, start, now)
    device = schedule.device_levels(rows, scale=scale)
    if device is None:
        return None

    series = schedule.intervals(rows, start, now, device["active_floor"], scale=scale)
    grid = schedule.weekly_grid(series)

    base_evidence = {"entity": entity_id, "switch": switch_entity_id, **device,
                      "thresholds": {"idle_below_w": device["active_floor"]}}

    # `schedules` BY NAME, never through `find_candidate`. This read it from
    # the candidate until 2.14.15, which was right while `find_candidate`
    # picked from `schedules` and became silently wrong the moment it picked
    # from `backed_windows` instead: both columns then came from the same
    # function and the script reported "agree" on every device in the house.
    # The D1 column means one fixed thing — the window the identical-runs scan
    # picks — and it has to go on meaning that however the shipped picker
    # moves, or a comparison run after a change cannot be read against one run
    # before it.
    #
    # It also makes the two deterministic columns comparable, which they were
    # not before: D1+ below has always been picked straight off its own scan,
    # while a candidate carries `find_candidate`'s further floors — a window
    # whose watts could not be measured, a grid too thinly covered — so the
    # old D1 could be None where D1+ had a window for reasons that were
    # nothing to do with which scan found it.
    d1_best = _best_window(schedule.schedules(grid))
    d1 = {"window": None, "grade": None}
    d1_spec = None
    graded = []  # [(spec, grade), ...] — what _grade_reusing has to check against
    if d1_best is not None:
        d1_spec = d1_best["window"]
        d1["window"] = window.label(d1_spec)
        d1["grade"] = _grade(entity_id, switch_entity_id, name, scale, base_evidence,
                             d1_spec, rows, now)
        graded.append((d1_spec, d1["grade"]))

    # D1+ (WP-C): the same grid's own `backed_windows(couple_weekend=True)`,
    # picked the same way D1 is — see the module docstring's "WHAT 'D1' AND
    # 'D2' MEAN HERE" for why this exists and why the pick is `_best_window`
    # rather than `[0]`.
    d1_plus_best = _best_window(schedule.backed_windows(grid, couple_weekend=True))
    d1_plus = {"window": None, "grade": None}
    d1_plus_spec = None
    if d1_plus_best is not None:
        d1_plus_spec = d1_plus_best["window"]
        d1_plus["window"] = window.label(d1_plus_spec)
        d1_plus["grade"] = _grade_reusing(entity_id, switch_entity_id, name, scale,
                                          base_evidence, d1_plus_spec, rows, now, graded)
        graded.append((d1_plus_spec, d1_plus["grade"]))

    if dry_run:
        d2 = _empty_d2_result(scheduler_llm.prompt_for(grid, name))
        d2_validated = False
    else:
        d2 = _ask_d2(grid, name, client)
        d2_validated = d2["spec"] is not None and d2["refusal"] is None

    d2_grade = None
    d2_validated_spec = d2["spec"] if d2_validated else None
    if d2_validated:
        d2_grade = _grade_reusing(entity_id, switch_entity_id, name, scale, base_evidence,
                                  d2_validated_spec, rows, now, graded)

    if dry_run:
        relationship = "dry-run"
    elif d1_spec is not None and d2_validated_spec is not None:
        relationship = "agree" if d1_spec == d2_validated_spec else "differ"
    elif d1_spec is not None and d2_validated_spec is None:
        relationship = "d1-only"
    elif d1_spec is None and d2_validated_spec is not None:
        relationship = "d2-only"
    else:
        relationship = "neither"

    # D1 against D1+, computed the same way as the relationship above — never
    # gated on dry_run, because neither proposer needs ASI1 to answer.
    if d1_spec is not None and d1_plus_spec is not None:
        d1_plus_relationship = "agree" if d1_spec == d1_plus_spec else "differ"
    elif d1_spec is not None and d1_plus_spec is None:
        d1_plus_relationship = "d1-only"
    elif d1_spec is None and d1_plus_spec is not None:
        d1_plus_relationship = "d1-plus-only"
    else:
        d1_plus_relationship = "neither"

    preferred = _preferred([("D1", d1_spec, d1["grade"]),
                            ("D1+", d1_plus_spec, d1_plus["grade"]),
                            ("D2", d2_validated_spec, d2_grade)])

    return {
        "entity": entity_id,
        "switch": switch_entity_id,
        "name": name,
        # §7.6: two entities can fingerprint alike and still be different
        # questions, because the prompt also carries the name and the name
        # moves the answer. Printed beside the fingerprint below so a reader
        # is not invited to read "identical grid" as "identical question".
        "name_print": scheduler_llm.name_print(name),
        "grid_fingerprint": schedule.grid_fingerprint(grid),
        # The full character rendering, for --dry-run's report only. It is
        # NOT what the prompt carries any more — scheduler_llm sends
        # `runs_text` — and that is exactly why it is worth printing here: a
        # person reading a disagreement wants the cells, and the fingerprint
        # above collapses three states to three letters without their counts.
        "grid_text": schedule.grid_text(grid),
        "d1": d1,
        "d1_plus": d1_plus,
        "d2": d2,
        "d2_grade": d2_grade,
        "relationship": relationship,
        "d1_plus_relationship": d1_plus_relationship,
        "preferred": preferred,
    }


# --------------------------------------------------------------------------
# every watched entity — schedule.scan's own I/O, mirrored rather than
# called, because scan() only returns entities that ALREADY found a D1
# candidate and this needs every entity that produces a grid at all — D2's
# whole value is in the entities D1 found nothing on.
# --------------------------------------------------------------------------
def _watched_entities(watchlist):
    watched = set(watchlist.watched_entities())
    if not watched:
        return []
    ok, data = registry.fetch_registry()
    if not ok:
        sys.exit(f"could not read the Home Assistant registry: {data}")
    states = data.get("states") or {}
    entity_rows = data.get("entities") or []
    by_device = registry.entities_by_device(entity_rows)
    names = registry.names_from(data)
    registry_of = {row.get("entity_id"): row for row in entity_rows
                   if isinstance(row, dict) and row.get("entity_id")}

    found = []
    for entity_id in sorted(watched):
        registry_row = registry_of.get(entity_id)
        scale = power.power_scale(states.get(entity_id), registry_row)
        if scale is None:
            continue
        switch_entity_id = power.controllable_switch(
            entity_id, (registry_row or {}).get("device_id"), by_device)
        if not switch_entity_id:
            continue
        found.append((entity_id, switch_entity_id, names.get(switch_entity_id, ""), scale))
    return found


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def _replay_line(grade):
    if grade is None:
        return "not graded"
    if grade["build_error"]:
        return f"no usable config — {grade['build_error']}"
    if grade["replay_error"]:
        return f"did not survive replay — {grade['replay_error']}"
    if grade["disqualified"]:
        return f"refused — {grade['disqualified']}"
    fields = replay.fields_for(schedule.KIND)
    replayed = grade["replay"] or {}
    estimate = grade["estimate"] or {}
    return (f"credited {replayed.get(fields['saved'])}, "
           f"wrong {replayed.get(fields['wrong'])} of {replayed.get(fields['examined'])} "
           f"{fields['noun']}s, ~{estimate.get('kwh_per_month')} kWh/month")


def _preferred_line(preferred) -> str:
    """One line: which proposer's graded window the record likes best, that
    none is in the running, or that it was a tie — a tie is reported rather
    than resolved silently, since three proposers landing on the same spec is
    the ordinary case `_grade_reusing` now expects, not a coincidence."""
    if preferred["winner"] is None:
        return "preferred: none — no proposer's window is in the running"
    hours = preferred["device_hours_per_month"]
    if preferred["tie"]:
        candidates = preferred["candidates"]
        names = (" and ".join(candidates) if len(candidates) <= 2
                else ", ".join(candidates[:-1]) + f", and {candidates[-1]}")
        return f"preferred: tie between {names} — {hours} device-hours/month each"
    return f"preferred: {preferred['winner']} — {hours} device-hours/month"


def _print_report(reports, dry_run):
    tallies = {"agree": 0, "differ": 0, "d1-only": 0, "d2-only": 0, "neither": 0,
              "dry-run": 0}
    refused = 0
    preferred_tallies = {"D1": 0, "D1+": 0, "D2": 0, "none": 0}
    ties = 0
    d2_over_both = 0
    for report in reports:
        tallies[report["relationship"]] += 1
        d2 = report["d2"]
        if d2.get("call_error") or d2.get("parse_error") or d2.get("declined") or d2.get("refusal"):
            refused += 1

        preferred = report["preferred"]
        if preferred["winner"] is None:
            preferred_tallies["none"] += 1
        else:
            preferred_tallies[preferred["winner"]] += 1
            if preferred["tie"]:
                ties += 1
            elif preferred["winner"] == "D2":
                d2_over_both += 1

        print(f"== {report['entity']} ({report['name'] or 'unnamed'}) — "
             f"switch {report['switch']} ==")
        print(f"   grid: {report['grid_fingerprint']}  name: {report['name_print']}")
        print(f"   D1: {report['d1']['window'] or 'no candidate'}")
        if report["d1"]["window"]:
            print(f"       {_replay_line(report['d1']['grade'])}")
        print(f"   D1+: {report['d1_plus']['window'] or 'no candidate'}")
        if report["d1_plus"]["window"]:
            print(f"       {_replay_line(report['d1_plus']['grade'])}")
        print(f"   D1 vs D1+: {report['d1_plus_relationship']}")

        if dry_run:
            print(report["grid_text"])
            print()
            print(report["d2"]["prompt"])
            print("   D2: --dry-run, not asked")
        elif d2["call_error"]:
            print(f"   D2: call failed — {d2['call_error']}")
        elif d2["parse_error"]:
            excerpt = (d2["raw"] or "")[:_RAW_EXCERPT_CHARS]
            print(f"   D2: unreadable answer — {d2['parse_error']}")
            print(f"       raw: {excerpt!r}")
        elif d2["declined"]:
            print("   D2: declined — the model found no window in this grid")
        elif d2["refusal"]:
            print(f"   D2: proposed {window.label(d2['spec'])}, refused — {d2['refusal']}")
        else:
            print(f"   D2: {window.label(d2['spec'])}")
            print(f"       {_replay_line(report['d2_grade'])}")
        print(f"   relationship: {report['relationship']}")
        print(f"   {_preferred_line(preferred)}")
        print()

    print("== summary ==")
    print(f"entities compared: {len(reports)}")
    if dry_run:
        print("(--dry-run: nothing was asked)")
        return
    print(f"D1 and D2 agreed: {tallies['agree']}")
    print(f"D1 and D2 differed: {tallies['differ']}")
    print(f"D1 found something D2 did not validate: {tallies['d1-only']}")
    print(f"D2 validated something D1 did not find: {tallies['d2-only']}")
    print(f"neither found anything: {tallies['neither']}")
    print(f"D2 refused/declined/failed: {refused} of {len(reports)}")
    print(f"D1 preferred: {preferred_tallies['D1']}")
    print(f"D1+ preferred: {preferred_tallies['D1+']}")
    print(f"D2 preferred: {preferred_tallies['D2']}")
    print(f"proposers tied: {ties}")
    print(f"no proposer in the running: {preferred_tallies['none']}")
    # §7's own open question — whether D1+'s wider reach into the grid makes
    # D2 unnecessary — turns on exactly this number: how often the model beat
    # BOTH deterministic proposers outright, tie included as "did not beat".
    print(f"D2 preferred over both deterministic proposers: {d2_over_both}")


# --------------------------------------------------------------------------
# --stability: does the CLAIM move day to day, not how it would grade
# --------------------------------------------------------------------------
def _windows_for(rows, scale, now):
    """D1's and D1+'s picks over one `now` — stages B/C/D1 only (`intervals`,
    `weekly_grid`, `schedules`/`backed_windows`), stopped short of `_grade`.
    `_stability_report` needs exactly this and nothing past it: the question
    it is answering is whether the CLAIM moves, not how any one day's claim
    would have replayed.

    Returns `(d1_window_or_None, d1_plus_window_or_None)`. `device_levels`
    answering None (the device showed one level on this shifted read) reports
    as a pair of Nones rather than an error — the ordinary "no candidate"
    verdict, one day earlier or later.
    """
    start = now - schedule.LOOKBACK_DAYS * 86400
    device = schedule.device_levels(rows, scale=scale)
    if device is None:
        return None, None
    series = schedule.intervals(rows, start, now, device["active_floor"], scale=scale)
    grid = schedule.weekly_grid(series)
    d1_best = _best_window(schedule.schedules(grid))
    d1_plus_best = _best_window(schedule.backed_windows(grid, couple_weekend=True))
    return (d1_best["window"] if d1_best else None,
            d1_plus_best["window"] if d1_plus_best else None)


def _stability_report(entities, n, now):
    """`--stability N`: D1's and D1+'s picks with `now` shifted back 1..N
    days, and how many DISTINCT windows each produced over those N re-runs.

    §7.1 names the cost a proposer whose window moves daily carries even when
    every day's answer is individually defensible: a replay re-run per
    candidate per day, and `review.evidence_fingerprint` churn from a claim
    that keeps changing though nothing about the household did. D1+ reaches
    further into the grid than D1 does (WP-C item 1, `backed_windows` without
    `schedules()`'s identical-runs restriction), and nobody has measured
    whether that reach costs more of exactly this than D1's own narrower pick
    does. This is that measurement, and it grades nothing on the N extra
    passes: every day's window is compared against the others, never built
    and never replayed — the count below is of DISTINCT specs, not of
    windows scored.

    D2 is left out on purpose, not by oversight. Its answer costs one live
    ASI1 call per entity per day this asks about — `--stability 7` would
    spend seven calls per entity where D1 and D1+ spend none — and a module
    whose own docstring reads "READ-ONLY. ABSOLUTELY." should not make a
    stability check the most expensive thing it does by default. A live D2
    stability read is the next package's, behind its own flag.
    """
    print("== stability ==")
    for entity_id, switch_entity_id, name, scale in entities:
        d1_windows = set()
        d1_plus_windows = set()
        for offset in range(1, n + 1):
            shifted_now = now - offset * 86400
            start = shifted_now - schedule.LOOKBACK_DAYS * 86400
            rows = record.between(entity_id, start, shifted_now)
            d1_spec, d1_plus_spec = _windows_for(rows, scale, shifted_now)
            if d1_spec is not None:
                d1_windows.add(d1_spec)
            if d1_plus_spec is not None:
                d1_plus_windows.add(d1_plus_spec)
        print(f"   {entity_id}: D1 {len(d1_windows)} distinct window(s), "
             f"D1+ {len(d1_plus_windows)} distinct window(s), over {n} day(s)")
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="/data",
                       help="the agent's DATA_DIR (default /data)")
    parser.add_argument("--json", metavar="PATH",
                       help="also write the full comparison as JSON to PATH")
    parser.add_argument("--dry-run", action="store_true",
                       help="render each entity's grid and prompt, call nothing")
    parser.add_argument("--stability", type=int, default=0, metavar="N",
                       help="re-run D1 and D1+ with now shifted back 1..N days and "
                            "report how many distinct windows each produced; grades "
                            "nothing extra and never calls D2 (see _stability_report)")
    args = parser.parse_args(argv)

    watchlist = _bootstrap(args.data_dir)

    client = None
    if not args.dry_run:
        if not settings.ASI1_API_KEY:
            sys.exit("ASI1_API_KEY is not set — export it, or pass --dry-run to "
                     "read the prompt without spending a call")
        client = _ASI1Client(scheduler_llm.BASE_URL, settings.ASI1_MODEL,
                             settings.ASI1_API_KEY, scheduler_llm.TIMEOUT_SECONDS)

    now = int(time.time())
    entities = _watched_entities(watchlist)
    if not entities:
        print("no watched entity has both a power sensor and a controllable switch")
        return 0

    reports = []
    for entity_id, switch_entity_id, name, scale in entities:
        report = _entity_report(entity_id, switch_entity_id, name, scale,
                                client, args.dry_run, now)
        if report is not None:
            reports.append(report)

    if not reports:
        print("no watched entity's record showed two distinct levels — nothing to grid")
    else:
        _print_report(reports, args.dry_run)

    if args.stability:
        _stability_report(entities, args.stability, now)

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(_full_json(reports, args.dry_run), handle, indent=2, default=str)
        print(f"\nwrote {args.json}")

    return 0


def _grade_fields(grade):
    """A `_grade` result's five fields, or all-None if it was never graded —
    the one unwrap `_full_json` needs for D1's, D1+'s and D2's grade alike."""
    grade = grade or {}
    return {"build_error": grade.get("build_error"),
            "replay_error": grade.get("replay_error"),
            "disqualified": grade.get("disqualified"),
            "replay": grade.get("replay"),
            "estimate": grade.get("estimate")}


def _full_json(reports, dry_run):
    """The same data the text report shows, structured for a later diff."""
    out = []
    for report in reports:
        d2 = report["d2"]
        entry = {
            "entity": report["entity"],
            "switch": report["switch"],
            "name": report["name"],
            "name_print": report["name_print"],
            "grid_fingerprint": report["grid_fingerprint"],
            "relationship": report["relationship"],
            "d1_plus_relationship": report["d1_plus_relationship"],
            "preferred": report["preferred"],
            "d1": {"window": report["d1"]["window"],
                   **_grade_fields(report["d1"]["grade"])},
            "d1_plus": {"window": report["d1_plus"]["window"],
                       **_grade_fields(report["d1_plus"]["grade"])},
            "d2": {
                "prompt": d2["prompt"] if dry_run else None,
                "raw": d2.get("raw"),
                "call_error": d2.get("call_error"),
                "parse_error": d2.get("parse_error"),
                "declined": d2.get("declined", False),
                "proposed": window.label(d2["spec"]) if d2.get("spec") is not None else None,
                "refusal": d2.get("refusal"),
                **_grade_fields(report["d2_grade"]),
            },
        }
        out.append(entry)
    return out


if __name__ == "__main__":
    sys.exit(main())
