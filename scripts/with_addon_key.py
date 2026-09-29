#!/usr/bin/env python3
"""Run one of these scripts with the add-on's OWN configured ASI1 key.

`compare_proposers.py`, `bench_scheduler_prompt.py` and `run_analyst.py` need
`ASI1_API_KEY` in the environment, and inside the add-on's container it is not
there: `run.sh` exports it into the agent's process, and a `docker exec` gets a
fresh environment that never ran `run.sh`. The key is in `/data/options.json`,
where the Supervisor wrote what the household typed into the add-on's
configuration.

So this reads that file IN-PROCESS, puts the key in `os.environ`, and hands off
to the named script unchanged — `runpy` rather than a subprocess, so the key
never becomes a command line anybody's `ps` can read and never reaches a shell
history. It is never printed either; the banner says only whether it was found.

Nothing else. It sets two variables and runs somebody else's `main`, which is
the whole point: the script being run is the one that gets reviewed, and
copying its argument handling here would be a second place for it to drift.

    docker exec <container> python3 /path/with_addon_key.py compare_proposers.py --json /tmp/run.json
    docker exec <container> python3 /path/with_addon_key.py bench_scheduler_prompt.py --repeat 3
"""
import json
import os
import runpy
import sys

# What the add-on's configuration calls each value, and what the agent reads it
# as. core/settings.py owns the second name; run.sh's `jq` line owns the
# mapping, and this is that line in Python rather than a third convention.
_OPTIONS = (("asi1_api_key", "ASI1_API_KEY"), ("asi1_model", "ASI1_MODEL"),
            ("asi1_model_strong", "ASI1_MODEL_STRONG"),
            ("asi1_strong_daily_cap", "ASI1_STRONG_DAILY_CAP"))

_OPTIONS_PATH = os.path.join(os.sep, "data", "options.json")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.exit(f"usage: {os.path.basename(__file__)} <script.py> [args...]")

    try:
        with open(_OPTIONS_PATH) as handle:
            options = json.load(handle)
    except OSError as error:
        sys.exit(f"could not read {_OPTIONS_PATH} — this runs inside the add-on's "
                 f"own container, where /data is mounted ({error})")

    for option, variable in _OPTIONS:
        if options.get(option):
            os.environ[variable] = str(options[option])
    # run_analyst.py: the kill switch has to cross too, or a household that
    # turned the analyst off gets it run anyway from here. run.sh's own line,
    # `false` and nothing else is off — `if options.get(...)` above would drop
    # exactly the value that matters.
    os.environ["ANALYST_ENABLED"] = "false" if options.get("analyst_enabled") is False else "true"
    # The model is named only when the household set one. Naming the fallback
    # here would be a second copy of core/settings.py's default, free to drift
    # from it and printed as if it were fact.
    print("ASI1_API_KEY:", "set" if os.environ.get("ASI1_API_KEY") else "MISSING",
          "| model:", os.environ.get("ASI1_MODEL") or "whatever core/settings.py defaults to")

    script = argv[0]
    if not os.path.isabs(script):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), script)
    # argv[0] is the script's own name, the way it would be if python3 had been
    # given it directly — argparse prints it in usage lines.
    sys.argv = [os.path.basename(script)] + argv[1:]
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
