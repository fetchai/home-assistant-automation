#!/usr/bin/env python3
"""D1 vs D2 over SYNTHETIC grids with a known best answer — the bench the
prompt in `core/scheduler_llm.py` is actually tuned against.

WHY THIS EXISTS BESIDE compare_proposers.py
--------------------------------------------
That script is the real measurement: real records, real grades, the pass's own
arithmetic. It cannot tell a better prompt from a lucky one, and the reason is
the fixtures rather than the script. Every planted test device is idle across
the SAME half-hours on every weekday, and identical weekday runs are the one
shape `schedule.schedules` merges outright — so D1 is already optimal on all
of them and the best D2 can score is a draw. A prompt that got worse and a
prompt that got better both read as "agreed with D1".

The cases below are the shapes D1 documents itself as unable to reach: runs
that differ weekday to weekday, whose intersection is true of every one of
them and is never proposed because it is maximal on none of them. Each one
carries a right answer this file computes by brute force, so a prompt change
is a number that moved rather than an impression.

WHAT IT IS NOT
--------------
Not a test, and deliberately not under tests/. It calls a live endpoint, its
answers are not reproducible by contract (decision 3), and a CI job that fails
because a model had an off day is a CI job people learn to ignore. It is a
measuring instrument, run by hand, the way `compare_proposers.py` is.

Not evidence about a household either. The grids here are hand-written, so a
result says the prompt can be read, not that the reading is right about
anybody's house. `compare_proposers.py` is what says the second thing.

TOUCHES NOTHING
---------------
No record, no Home Assistant, no suggestion row, no cache, no /data. It builds
`Grid` tuples in memory and needs `ASI1_API_KEY` in the environment, or
`--dry-run` and no key at all.

    python3 scripts/bench_scheduler_prompt.py --dry-run
    python3 scripts/bench_scheduler_prompt.py
    python3 scripts/bench_scheduler_prompt.py --case commuter --repeat 3
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import date, timedelta

# --------------------------------------------------------------------------
# How often each weekday appears in a LOOKBACK_DAYS window. Ninety days is
# just under thirteen weeks, and the number matters only in that it has to
# clear MIN_OCCURRENCES — a cell observed fewer times than that is not inside
# any window `window_is_backed` accepts, so a bench built under the floor
# would measure the validator rather than the prompt.
# --------------------------------------------------------------------------
OBSERVATIONS = 13

# core/detectors/schedule.py's own PROJECTION_DAYS_PER_MONTH, restated rather
# than imported for the reason compare_proposers.py restates
# MAX_WRONG_OCCURRENCES: this file's scores have to stay comparable across
# releases of that module, and a score that silently follows a constant it
# does not print is a score nobody can check by hand. The value has to STAY in
# step even though the name does not — it is the same projection
# `occurrences_per_month` divides by, and a bench reading 30.44 against a
# detector reading 30 inflates every score it prints by a percent and a half.
PROJECTION = 30


def _add_agent_home_to_path():
    """plant_standby.py's own resolution, repeated. Nothing under agent_home
    may be imported before DATA_DIR is set, this script's copy included."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.environ.get("AGENT_HOME"), "/app",
                      os.path.join(os.path.dirname(here),
                                   "fetchai-automation-agent", "agent_home")):
        if candidate and os.path.isdir(os.path.join(candidate, "core")):
            sys.path.insert(0, candidate)
            return
    sys.exit("could not find agent_home — run this inside the add-on container, "
             "or set AGENT_HOME to a checkout's agent_home directory")


# core/settings.py computes every store path at import time, so this is set
# before the import and points nowhere real: this script reads no store and
# writes none, and a DATA_DIR of /data would be an invitation to.
os.environ.setdefault("DATA_DIR", os.path.join(os.sep, "tmp", "bench-scheduler-prompt"))
_add_agent_home_to_path()

from core import settings                    # noqa: E402
from core import scheduler_llm               # noqa: E402
from core import window                      # noqa: E402
from core.detectors import schedule          # noqa: E402


# --------------------------------------------------------------------------
# the cases
#
# Slots are half-hours: 0 = 00:00, 1 = 00:30, ... 47 = 23:30, and a span is an
# INCLUSIVE (first, last). A weekday absent from the dict is a weekday the
# device was in use across, which is what makes the weekend cases weekends.
# --------------------------------------------------------------------------
CASES = {
    # The commuter. Every weekday's edges differ, so `schedules` can merge
    # only Mon with Thu and proposes a two-day window; the five-day
    # intersection is worth nearly twice that and no run scan will name it.
    # This is the gap `schedules`' docstring calls the seam stage D2 plugs
    # into, at full size.
    "commuter": {0: (15, 33), 1: (14, 34), 2: (16, 32), 3: (15, 33), 4: (14, 30)},

    # The near miss: four identical weekdays and a fifth half an hour short.
    # D1 drops Friday entirely rather than shorten the other four, which is
    # the narrower trigger §7.2's decision 2 was considered against.
    "nearmiss": {0: (16, 33), 1: (16, 33), 2: (16, 33), 3: (16, 33), 4: (16, 31)},

    # §3 stage D2's own example, and the ONE case where the highest-scoring
    # window is the wrong answer: five office weekdays plus a Saturday that
    # was idle longer still. Nothing here is refused either way, so this is
    # not a validator test — it is the whole of what a prior buys.
    "weekend": {0: (16, 32), 1: (16, 32), 2: (16, 32), 3: (16, 32), 4: (16, 32),
                5: (16, 40)},

    # The cost of the weekend rule, stated rather than discovered later: a
    # device nobody uses at all, whose weekend stretch is WIDER than its
    # weekdays'. Declining the weekend gives up two days. It ties with D1
    # here rather than losing to it, which is why the rule is worth its cost.
    "weekend_wider": {0: (18, 33), 1: (18, 33), 2: (18, 33), 3: (18, 33),
                      4: (18, 33), 5: (16, 35), 6: (16, 35)},

    # The shape every planted fixture already has. D1 is optimal; D2 agreeing
    # is the pass, and it is here because the prompt this file replaced
    # answered {"window": null} to it — a blanket "do not extend to the
    # weekend" reads as "decline" on a device that is idle all week.
    "flat": {day: (0, 35) for day in range(7)},

    # Nothing observed anywhere. The only right answer is to decline, and the
    # validator refusing a window here is the live proof that free-form output
    # plus a hard validator (decision 3) is the safe shape.
    "blank": {},
}


def build(spans) -> schedule.Grid:
    """A Grid whose `cells` and `dates` agree, from {weekday: (first, last)}.

    Both halves, because D1 reads `dates` and the prompt reads `cells`, and a
    bench whose two proposers saw different evidence would be measuring
    itself. Built by hand rather than through `weekly_grid` so a case is a
    line of slot numbers a person can check against the answer.
    """
    cells, dates = {}, {}
    if not spans:
        # `weekly_grid`'s own answer for a record that reached nothing: all
        # 336 cells present and every one of them unobserved.
        for weekday in range(7):
            for slot in range(schedule.SLOTS_PER_DAY):
                cells[(weekday, slot)] = {"idle": 0, "observed": 0}
        return schedule.Grid(cells, {})

    # An arbitrary Monday. Nothing downstream reads the calendar — `schedules`
    # counts dates per weekday and `occurrences_per_month` divides — so the
    # only thing this date has to be is a Monday, so that `weekday()` lines up
    # with the keys above.
    monday = date(2026, 6, 1)
    for weekday in range(7):
        span = spans.get(weekday)
        verdicts = [schedule.IDLE if span and span[0] <= slot <= span[1]
                    else schedule.ACTIVE
                    for slot in range(schedule.SLOTS_PER_DAY)]
        for slot in range(schedule.SLOTS_PER_DAY):
            cells[(weekday, slot)] = {
                "idle": OBSERVATIONS if verdicts[slot] == schedule.IDLE else 0,
                "observed": OBSERVATIONS,
            }
        dates[weekday] = [(monday + timedelta(days=weekday + 7 * week), list(verdicts))
                          for week in range(OBSERVATIONS)]
    return schedule.Grid(cells, dates)


def score(spec) -> float:
    """`schedules`' own ranking quantity for one window — hours x weekdays / 7
    x days per month. The saving with the watts left out, which is the only
    part of it a grid can know, and the number both proposers are read by
    here so that "better" means better by the shipped measure."""
    return window.hours(spec) * len(spec.days) / 7.0 * PROJECTION


def best_possible(grid):
    """The highest-scoring window this grid actually backs, or None.

    Brute force over every (first, last, days), which is 48 x 48 x 7 and
    therefore free. Deliberately not clever: a clever version would be a
    second implementation of the thing under test, and this number is the
    yardstick both proposers are held against.

    Membership is `_runs_for`'s, not a re-reading of the spans dict — the same
    predicate `window_is_backed` enforces — so a window this calls best is one
    the validator provably accepts.
    """
    runs = {weekday: schedule._runs_for(grid.cells or {}, weekday)
            for weekday in range(7)}
    best = None
    for first in range(schedule.SLOTS_PER_DAY):
        for last in range(first, schedule.SLOTS_PER_DAY):
            slots = last - first + 1
            if not schedule.MIN_RUN_SLOTS <= slots <= schedule.MAX_RUN_SLOTS:
                continue
            days = {weekday for weekday, spans in runs.items()
                    if any(run[0] <= first and last <= run[1] for run in spans)}
            if not days:
                continue
            spec = schedule._slot_spec(first, last, days)
            if best is None or score(spec) > score(best):
                best = spec
    return best


class _ASI1Client:
    """compare_proposers._ASI1Client, restated. Same reason it is a duplicate
    there rather than an import from the module under test: these scripts run
    on stdlib alone, and `propose(grid, client=...)` takes anything with
    `.invoke`."""

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


def _d2_line(grid, name, client):
    """One call, `propose`'s steps taken apart the way compare_proposers takes
    them apart — decision 3's last paragraph again: the refusal IS the
    finding, and `propose` collapses it to a logged None."""
    prompt = scheduler_llm.prompt_for(grid, name)
    try:
        raw = client.invoke(prompt)
    except Exception as error:                        # noqa: BLE001 — reported, not raised
        return f"call failed — {error}"
    try:
        spec = scheduler_llm.parse_answer(raw)
    except ValueError as error:
        return f"unreadable — {error}: {raw!r}"
    if spec is None:
        return "declined"
    refused = scheduler_llm.refusal(grid, spec)
    if refused:
        return f"{window.label(spec)} — REFUSED: {refused}"
    return f"{window.label(spec)} ({score(spec):.0f})"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", default="all", choices=sorted(CASES) + ["all"])
    parser.add_argument("--dry-run", action="store_true",
                        help="print each case's stretches and prompt, call nothing")
    parser.add_argument("--repeat", type=int, default=1,
                        help="ask the same case more than once — decision 3 says "
                             "the answers are not reproducible, so this is how "
                             "far apart they land")
    parser.add_argument("--name", default="Lounge TV",
                        help="the device name the prompt carries")
    args = parser.parse_args(argv)

    client = None
    if not args.dry_run:
        if not settings.ASI1_API_KEY:
            sys.exit("ASI1_API_KEY is not set — export it, or pass --dry-run to "
                     "read the prompt without spending a call")
        client = _ASI1Client(scheduler_llm.BASE_URL, settings.ASI1_MODEL,
                             settings.ASI1_API_KEY, scheduler_llm.TIMEOUT_SECONDS)

    for case in (sorted(CASES) if args.case == "all" else [args.case]):
        grid = build(CASES[case])
        ideal = best_possible(grid)
        found = schedule.schedules(grid)
        d1_spec = found[0]["window"] if found else None

        print("=" * 72)
        print(case)
        print(f"  best: {window.label(ideal)} ({score(ideal):.0f})" if ideal
              else "  best: none — the only right answer is to decline")
        print(f"  D1:   {window.label(d1_spec)} ({score(d1_spec):.0f})" if d1_spec
              else "  D1:   no candidate")
        if args.dry_run:
            print(scheduler_llm.prompt_for(grid, args.name))
            continue
        for _attempt in range(max(1, args.repeat)):
            print(f"  D2:   {_d2_line(grid, args.name, client)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
