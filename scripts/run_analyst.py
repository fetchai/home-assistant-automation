#!/usr/bin/env python3
"""Run the analyst once, now, and show what it stored and where that stands
in the review's queue.

The analyst runs itself at ANALYST_HOUR (07:00) once a day (tasks/analyst.py);
waiting for tomorrow morning to see whether a planted pattern is found is
what this saves. It calls the agent's own `analyst.run_explorer` — the same
pass, the same allowlist, the same house checks, the same store — so a row it
stores is a row the running agent's next review ranks, exactly as if 07:00 had
done it. It needs the household's ASI1 key, so run it through
with_addon_key.py, which also carries the strong-tier options and the
`analyst_enabled` switch across.

WHY IT STAMPS THE RUN FILE, AND REFUSES A SECOND RUN
-----------------------------------------------------
D-10: a run costs money and sends the house's data out, and the persisted
stamp is what stops a restart buying a second one. A dev script that bypassed
that would be the one way round it. So this stamps BEFORE running, exactly as
tasks/analyst.py does — the 07:00 task then sees today as done — and refuses
when today is already stamped unless --force says a second run is intended.
A stamp that cannot be written refuses too, tasks/analyst.py's rule.

WHAT --review DOES
------------------
Runs `review.run_once` afterwards, in this process: the pass tasks/review.py
runs at REVIEW_HOUR. Every limit it applies is counted from the store (one new
question a day, three open at most), so running it here cannot ask more than
the agent would; it can only ask it sooner. It is also the WHOLE pass —
withdrawals, verification, the detectors' candidates — not an analyst-only
step, which is why it is a flag and not the default.

WHAT --trace DOES
-----------------
Prints the `Submit ideas` arguments as the model sent them, before they are
validated, whether they pass or not. The agent itself logs a REFUSED idea's
arguments (cut to analyst.RAW_ARGS_LOG_CHARS); this shows the accepted ones
too, in full — the finding, the rule and the chart request to hold against
what the queries answered. It wraps `analyst.validate_ideas` in this process
only; nothing about the run changes.

(--mode, which picked the audited two-pass run or the explorer, went with
`analyst_mode` when F-9 retired the audited run, 2.23.0.)

WHAT --redraw ID DOES
---------------------
Draws one stored explorer row's chart again from its own spec and config
(`charts.redraw`, F-WP4) and writes it back through `suggestions.set_chart` —
for a row stored before charts were drawn at store time (`series: null`), or
refused a window F-WP4 now clamps. No model call and no key; the row's state
and its place in the queue do not move.

WHAT IT CANNOT CHANGE
---------------------
M-1: a detector row that claims a saving ranks ahead of any analyst row
(D-6), so on a record that also holds the standby and schedule test devices
the analyst's row can queue behind them for days. The queue report says so
with the actual positions rather than letting --review look broken.

Usage (inside the add-on container):
    python3 with_addon_key.py run_analyst.py
    python3 with_addon_key.py run_analyst.py --review
    python3 run_analyst.py --show            # no model call, no key needed
    python3 run_analyst.py --redraw 15       # no model call, no key needed
    python3 with_addon_key.py run_analyst.py --force
    python3 with_addon_key.py run_analyst.py --force --trace
"""
import argparse
import json
import logging
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (os.environ.get("AGENT_HOME"), "/app",
                   os.path.join(os.path.dirname(_HERE),
                                "fetchai-automation-agent", "agent_home")):
    if _candidate and os.path.isdir(os.path.join(_candidate, "core")):
        sys.path.insert(0, _candidate)
        break
else:
    sys.exit("could not find agent_home — run this inside the add-on container, "
             "or set AGENT_HOME to a checkout's agent_home directory")

from core import artefacts                   # noqa: E402
from core import review                      # noqa: E402
from core import suggestions                 # noqa: E402


def _show(now: int):
    """Every analyst row still in play, rendered as the user would read it,
    then the queue the review ranks."""
    rows = suggestions.active(now=now)
    ours = [r for r in rows if r.get("source") == suggestions.SOURCE_ANALYST]
    print(f"\n{len(ours)} analyst suggestion(s) in play")
    for row in ours:
        print(f"\n--- #{row['id']}  {row['state']}  ({row.get('evidence_level')})  "
              f"model {row.get('model')}")
        print(artefacts.detail_text(row))

    open_now = [r for r in rows if r.get("state") == "proposed"]
    queue = review.rank([r for r in rows if r.get("state") == "validated"])
    today = review._start_of_local_day(now)
    proposed_today = sum(1 for r in open_now if int(r.get("updated_at") or 0) >= today)
    slots = review.proposals_allowed(len(open_now), proposed_today)
    print(f"\nreview queue: {len(open_now)}/{review.MAX_OPEN_PROPOSALS} open, "
          f"{proposed_today} proposed today, {slots} slot(s) for the next pass")
    for position, row in enumerate(queue, 1):
        mark = "  <- analyst" if row.get("source") == suggestions.SOURCE_ANALYST else ""
        print(f"  {position:2}. #{row['id']:<4} {artefacts.headline(row)}{mark}")
    if not queue:
        print("  (nothing validated)")
    elif ours and all(r.get("source") != suggestions.SOURCE_ANALYST for r in queue[:max(slots, 1)]):
        print("  The next pass proposes the top row, not an analyst one (D-6: a saving "
              "ranks first). It also skips any row touching an entity an open proposal "
              "already holds, which this list does not model.")


def _redraw(suggestion_id: int) -> int:
    """`charts.redraw`, and what it drew, in one screen."""
    from core import charts
    chart = charts.redraw(suggestion_id)
    if chart is None:
        print(f"#{suggestion_id}: not redrawn — not an explorer row with a chart (see the log)")
        return 1
    print(f"#{suggestion_id}: redrawn — {chart['spec']}")
    if chart["error"]:
        print(f"  error: {chart['error']}")
    if chart["window_clamped"]:
        print(f"  window clamped: {chart['window_clamped']}")
    series = chart["series"] or {}
    if "days" in series:
        print(f"  {len(series['days'])} day(s) drawn"
              + (f", {series.get('days_dropped', 0)} dropped for size" if series.get("truncated") else ""))
    markers = chart["markers"]
    print(f"  markers: {'none' if markers is None else len(markers)}"
          + (f" — {chart['markers_note']}" if chart["markers_note"] else ""))
    return 0


def _trace(analyst):
    """Wraps the validator so the submission's raw arguments are printed as
    the model sent them, before validation decides anything."""
    def wrap(name):
        original = getattr(analyst, name)

        def traced(args, *rest):
            print(f"\n--- TRACE {name}: raw arguments\n"
                  f"{json.dumps(args, indent=2, ensure_ascii=False, default=str)}")
            result = original(args, *rest)
            # validate_ideas answers a list of (idea | None, why, raw), one
            # per idea.
            for verdict in result:
                if verdict[0] is None:
                    print(f"--- TRACE {name}: refused — {verdict[1]}")
            return result
        setattr(analyst, name, traced)

    wrap("validate_ideas")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--show", action="store_true",
                        help="only show the analyst rows and the queue; call no model")
    parser.add_argument("--force", action="store_true",
                        help="run even though the analyst already ran today")
    parser.add_argument("--review", action="store_true",
                        help="run the whole review pass afterwards")
    parser.add_argument("--trace", action="store_true",
                        help="print each submission's raw arguments before validation")
    parser.add_argument("--redraw", type=int, metavar="ID",
                        help="draw one stored explorer row's chart again; call no model")
    args = parser.parse_args(argv)
    # The ANALYST:/AUDIT:/REVIEW: lines are the agent's own account of the
    # run, and the reason a refusal is a log line rather than a reply (D-3).
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    now = int(time.time())
    if args.redraw is not None:
        return _redraw(args.redraw)
    if args.show:
        _show(now)
        return 0

    from core import analyst                 # langchain; only when a run is asked for
    if args.trace:
        _trace(analyst)

    last = analyst.last_run_date(now)
    if last == analyst._local_date(now) and not args.force:
        print(f"the analyst already ran today ({last}) — a second run costs a second "
              f"set of calls; pass --force if that is intended, or --show to look")
        return 1
    if not analyst.stamp_run(now):
        print(f"could not write {analyst.RUN_PATH} — not running (tasks/analyst.py's rule)")
        return 1

    summary = analyst.run_explorer(now)
    if summary.get("skipped"):
        print(f"\nskipped: {summary['skipped']}")
        return 1
    if args.review:
        print(f"\nreview: {review.run_once()}")
    _show(int(time.time()))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
