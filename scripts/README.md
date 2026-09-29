# Dev scripts

Test data for the behavioural record, and the two measuring instruments built on it. Nothing here is part
of the add-on — the `Dockerfile` copies only named paths under `fetchai-automation-agent/`, so none of it
reaches the shipped image. Every script is stdlib only. The ones that talk to Home Assistant expect
`SUPERVISOR_TOKEN` in the environment (any Home Assistant add-on shell has it); the ones that call ASI1
expect `ASI1_API_KEY`, which `with_addon_key.py` supplies from the add-on's own configuration.

| Script | What it does |
|---|---|
| `scenario.py` | The shared generator: a plausible week in a small home, as state transitions. Not run directly. |
| `plant_test_history.py` | Writes backdated history into **Home Assistant's recorder**, which the agent then warms its record from through its own backfill. |
| `simulate_test_devices.py` | Publishes state over MQTT as it happens, until stopped. |
| `standby_devices.py` | The shared profiles for the three standby test devices. Not run directly. |
| `plant_standby.py` | Writes thirty nights of overnight power **straight into the agent's record**, because the recorder cannot reach back far enough. |
| `simulate_standby.py` | Keeps those three devices reporting power over MQTT, until stopped. |
| `schedule_devices.py` | The shared profiles for the three **schedule** test devices — ragged weekday edges D1 cannot merge. Not run directly. |
| `plant_schedule.py` | Writes ninety days of power straight into the agent's record for those three. |
| `simulate_schedule.py` | Announces those three to Home Assistant over MQTT discovery, then keeps them reporting. |
| `analyst_devices.py` | The shared profiles for the four **analyst** test devices — habits between entities, and of entities with no power reading, which no detector looks for. Not run directly. |
| `plant_analyst.py` | Writes thirty days of those habits straight into the agent's record, each habit's figure counted first. `--watch` adds them to the watchlist. |
| `simulate_analyst.py` | Announces those devices over MQTT discovery, then plays each day at the wall clock. |
| `run_analyst.py` | Runs the analyst once, now, and shows what it stored and where that sits in the review's queue. |
| `probe_asi1_models.py` | Asks ASI-1 which model names it answers to, so `asi1_model_strong` is set from an answer. Writes nothing. |
| `compare_proposers.py` | Runs the schedule detector's three proposers — the shipped deterministic grid scan (D1), the same scan without its identical-runs restriction (D1+), and `core/scheduler_llm.py`'s model (D2) — over the same real record and grades all three the way `core/review.py` would, without recording anything. |
| `bench_scheduler_prompt.py` | The same two proposers over **synthetic** grids with a known best answer, for the shapes no planted fixture has. Touches no record and no Home Assistant. |
| `peer_probe.py` | Plays another agent against the add-on's agent-to-agent protocol, from inside its container: `connect`, then `query` (entities, state or profile) once approved in the panel. |
| `with_addon_key.py` | Runs either of those two inside the add-on's container with the household's own configured ASI1 key, which a `docker exec` does not inherit. |

The first three drive the six original MQTT test entities, the next three the three standby devices, the
next three the three schedule devices, and the next four the three analyst devices. Every one of them
refuses to touch anything else. `compare_proposers.py`, `bench_scheduler_prompt.py` and
`with_addon_key.py` write nothing at all.

## The schedule test devices

`plant_standby.py`'s three devices are idle across the **same** half-hours on every weekday, and
identical weekday runs are the one shape `schedule.schedules` merges outright — so D1 is already
optimal on them and the model proposer can only draw. `schedule_devices.py` holds three whose edges
differ day to day, which is the gap `schedules()`' own docstring names:

| device | shape | what it is for |
|---|---|---|
| `study` | a different leaving and returning time every weekday | D1 merges only Mon with Thu; the five-day intersection is worth about twice that |
| `landing` | four identical weekdays and a Friday that ends an hour early | D1 drops Friday rather than shorten the other four |
| `spare` | five office weekdays plus a Saturday the household is out longer | including Saturday scores higher and is the wrong answer |

Unlike every other script here, `simulate_schedule.py` also **announces** its devices — it publishes the
MQTT discovery configs itself, so the set can be rebuilt from the repo rather than from whatever is
retained on the broker. The sensor and the switch share one `device` block on purpose:
`power.controllable_switch` pairs them by device_id and pairs nothing at all when a device offers more
or fewer than one controllable sibling.

```bash
python3 scripts/simulate_schedule.py --announce    # once — creates the six entities
python3 scripts/plant_schedule.py --dry-run        # generate and judge, write nothing
python3 scripts/plant_schedule.py                  # ninety days into the record
python3 scripts/simulate_schedule.py --check       # one round, read every entity back
```

Then add each sensor **and** its switch to the watchlist in the panel — nothing is watched by default
and `core/watchlist.py` fails closed. `simulate_schedule.py --retire` clears the discovery configs and
takes the entities away again.

## The analyst test devices

Every device above is a power sensor, because both detectors read watts. The analyst (`core/analyst.py`,
proposal §5B) exists for what they cannot see, so these four report no power at all, and whatever
reaches the Suggestions tab from them came from the model. The first three were shaped so that one claim
type of the audited run's vocabulary (retired, free-hand F-9) was the natural way to back each up — the
column below still names the figure that shows it; the fourth, `study`, was shaped so that none could —
only the explorer's own SQL finds it (free-hand F-WP8):

| device | habit | the figure that shows it | the automation it invites |
|---|---|---|---|
| `hallway` | motion, then the light by hand within a minute, **evenings only** | `follows` (~146 in 30 days) | motion → light, with a time or sun condition |
| `bathroom` | extractor fan on every morning, forgotten for 3-6 h on most days | `transitions` (30) + `on_hours` (~80 h) | turn it off after N minutes |
| `porch` | light on by hand ~19:00 on weekdays, later at weekends | `transitions` with `hours` [18, 20) and `weekdays` 0-4 (~20) | a weekday time or sunset trigger |
| `study` | occupancy sensor + desk lamp: lamp on while someone works on weekdays, **left on in the empty room** on about half the evenings until 21:00-23:00 | none — an absence of co-change; the explorer's overlap query (`tool_record_query` example (b)) | occupancy off for N minutes → lamp off |

```bash
python3 scripts/simulate_analyst.py --announce          # once — creates the six entities
python3 scripts/simulate_analyst.py --check             # publish the current states, read them back
python3 scripts/plant_analyst.py --dry-run              # generate, count each habit, write nothing
python3 scripts/plant_analyst.py --watch                # thirty days into the record, then watch them
python3 scripts/simulate_analyst.py                     # keep them living, at the wall clock
python3 scripts/with_addon_key.py run_analyst.py        # the analyst, now instead of at 07:00
python3 scripts/run_analyst.py --show                   # no model call: the analyst rows and the queue
python3 scripts/with_addon_key.py run_analyst.py --force --trace   # print the submitted ideas as sent
```

All of it inside the add-on's container, as with `plant_standby.py`. `--dry-run` prints each habit's
figure counted straight from the generated rows, plus the record's own `_relation_counts` — so a seed too
weak for "Get observed patterns" to list is caught before anything is stored. `--watch` posts to
`/rest/observation/selection`, the endpoint the panel's picker uses, adding these entities and keeping the
rest of the selection as it is; never the file, which the running process would overwrite.

**Every event sits between minute 3 and minute 12 of its quarter hour**, so none falls within 120 s of
the power devices' quarter-hour heartbeats — otherwise `scan_relations` pairs the hallway light with every
plug in the house. It cannot do the same for the original scenario's temperature sensor (every 5 minutes)
or its kitchen motion, and after planting the live readback shows those pairs at 10-25 against the
hallway's ~146. That noise is real; ranking it below the actual habit is the model's job.

`run_analyst.py` stamps `analyst_run.json` before it runs, as `tasks/analyst.py` does, so the 07:00 run
then treats today as done, and it refuses a second run the same day without `--force` (D-10: each run
costs money and sends the house's data out). `with_addon_key.py` carries `analyst_enabled`,
`asi1_model_strong` and `asi1_strong_daily_cap` across as well as the key, so the kill switch holds here
too. `--review` also runs the whole review pass in the same process. Its limits are counted from the store,
so it can only ask sooner, never more often. `--trace` prints the `Submit ideas` arguments before
validation, accepted or not. The agent's own log keeps only a refused idea's, cut to 1500 characters.
Hold a traced finding against the query answers the same run logged.

**What it cannot change.** The analyst's row is stored at `validated`, but only the review puts it to a
person, under M-1's limits: one new question a day, three open at once, and a detector row that claims a
saving ranks ahead of any analyst row (D-6). On a record that also holds the standby and schedule
devices, that means waiting until the open proposals are answered. `--show` prints the queue with the
real positions. `plant_analyst.py --clear` removes the planted rows. `simulate_analyst.py --retire`
removes the entities.

## Which ASI-1 models exist

`asi1_model_strong` has been unset since it existed, because no name for it was ever verified — the
analyst proposal's §7 catalogue comes from documentation. Explorer field run 1 ran on `asi1-mini`
because of it. `probe_asi1_models.py` asks the API instead:

```sh
python3 scripts/with_addon_key.py probe_asi1_models.py              # GET /v1/models; if refused, probe each name
python3 scripts/with_addon_key.py probe_asi1_models.py --probe      # the list AND one call per name
python3 scripts/with_addon_key.py probe_asi1_models.py --model asi1-pro   # one more name to try
```

First `GET https://api.asi1.ai/v1/models`, the OpenAI-compatible list. If it answers, its ids are printed and
nothing else is asked, unless `--probe` is given — a listed id is not proof it serves chat completions. If
the list is refused, absent or empty, one chat completion with `max_tokens: 1` per candidate (`asi1-mini`,
`asi1`, `asi1-extended`, `asi1-fast`, `asi1-ultra`, `asi1-graph`, plus every listed id and every `--model`),
each printed `OK` or `FAIL` with the server's reason. The last line lists the names that answered. Exit 0
when one did, 1 when none did, 2 without a key. The key goes only into the Authorization header and is
replaced by `<key>` in anything printed, in case an error echoes the request. Then set `asi1_model_strong`
in the add-on's options from the result — and see the 2.19.1 CHANGELOG entry on the daily cap: one
explorer run can take 16 of the default 20 strong calls.

## Comparing the three schedule proposers

proposal §7.3's WP5, extended by WP-C. `core/detectors/schedule.py` finds the window it can prove two
ways — `schedules()` (D1, the shipped scan, merging only IDENTICAL weekday runs) and
`backed_windows(couple_weekend=True)` (D1+, the same scan without that restriction, plus the
weekend-coupling rule §3 stage D2's own example needs) — and `core/scheduler_llm.py` (WP2, D2) is a
free-form model answer nothing yet wires in, whose output decision 3 says is not reproducible run to
run. Whether D1+'s wider reach makes D2 unnecessary is §7's own open question, and this script answers
it by grading all three the way the shipped pass would, live, against a real record. It reads the
record and calls ASI1, and it never writes to `record.db`, `activity.db`, the schedule cache, or a
suggestion row.

```bash
python3 scripts/compare_proposers.py --dry-run          # render each grid and prompt, call nothing
python3 scripts/compare_proposers.py                     # needs ASI1_API_KEY in the environment
python3 scripts/compare_proposers.py --json /tmp/run.json
python3 scripts/compare_proposers.py --stability 7 --dry-run   # does D1's/D1+'s pick move day to day?
```

`--stability N` re-runs D1's and D1+'s picks with `now` shifted back 1..N days and reports how many
distinct windows each produced — never D2, which would cost a live call per entity per day asked
about. `compare_proposers.py`'s own `_stability_report` docstring has the full argument.

Like `plant_standby.py`, it needs a real `/data` and Home Assistant's own API, so it runs inside the
add-on's own container. Unlike anything else here, it also calls ASI1 — with a small stdlib `urllib`
client rather than langchain, which is what the WP2 amendment (moving `ChatOpenAI`'s import out of
module scope) exists to make possible.

The key is the one snag. `run.sh` exports `ASI1_API_KEY` into the agent's process and a `docker exec`
never runs `run.sh`, so the environment inside the container does not have it. `with_addon_key.py`
reads it out of `/data/options.json` in-process and hands off, which keeps it off every command line:

```bash
docker cp scripts app_local_fetchai-automation-agent:/tmp/devscripts
docker exec app_local_fetchai-automation-agent sh -c \
  'cd /tmp/devscripts && python3 with_addon_key.py compare_proposers.py --json /tmp/run.json'
```

## Benching the prompt itself

`compare_proposers.py` is the real measurement and it cannot tell a better prompt from a lucky one.
Every planted device is idle across the same half-hours on **every** weekday, and identical weekday
runs are the one shape `schedule.schedules` merges outright — so D1 is already optimal on all of
them, the best D2 can score is a draw, and a prompt that got worse reads the same as one that got
better.

`bench_scheduler_prompt.py` holds the shapes D1 documents itself as unable to reach: runs whose edges
differ weekday to weekday, whose intersection is true of all of them and maximal on none. Each case
carries a best answer the script computes by brute force from `_runs_for`'s own predicate, so a prompt
change is a number that moved. It builds `Grid` tuples in memory — no record, no Home Assistant, no
`/data`, nothing to clean up afterwards.

```bash
python3 scripts/bench_scheduler_prompt.py --dry-run     # every case's stretches and prompt, no key needed
python3 scripts/bench_scheduler_prompt.py               # needs ASI1_API_KEY
python3 scripts/bench_scheduler_prompt.py --case weekend --repeat 3
```

It is not a test and is deliberately not under `tests/`: it calls a live endpoint, decision 3 says its
answers are not reproducible by contract, and a CI job that fails because a model had an off day is a
CI job people learn to ignore. It is also not evidence about a household — the grids are hand-written,
so a result says the prompt can be read, not that the reading is right about anybody's house. That
second thing is what `compare_proposers.py` is for.

## Planting history

```bash
python3 scripts/plant_test_history.py --dry-run   # rehearse on a copy, report what HA would return
python3 scripts/plant_test_history.py             # back up, then plant 9 days
python3 scripts/plant_test_history.py --clear     # remove exactly the rows it planted
```

Nine days because the recorder purges at ten by default, and the agent's own backfill floor is ten.

The dry run is a real insert into a real copy of the database, queried with the same
`significant_changes_only` filter the agent's backfill uses — so it reports what the *agent* would see,
not merely what was written.

After planting, the agent picks it up on its next backfill: restart the add-on, or wait for the
15-minute re-resolve. Look for `BACKFILL: recovered N transition(s)`.

## Simulating live

```bash
python3 scripts/simulate_test_devices.py --check       # verify payloads reach the entities
python3 scripts/simulate_test_devices.py               # idle gaps compressed 20x
python3 scripts/simulate_test_devices.py --speed 1     # real time, nothing compressed
python3 scripts/simulate_test_devices.py --speed 300 --report 45
```

Publishes to state topics rather than command topics, because that is what a real device reports on and
because the motion and temperature entities have no command topic.

### What `--speed` does, and what it deliberately does not

It compresses **idle time only**. Any interval shorter than 120 seconds plays at its real length,
however high the number goes.

Speeding up the whole clock is the obvious implementation and it produces a corrupt record. The agent
stores how long each state *lasted*, and on-fraction and mean-duration are computed from exactly those
numbers — so a light "on" for two seconds instead of five minutes is not a fast simulation, it is a wrong
one. Worse, `core/observer.py` debounces anything under two seconds as flapping, so at 60x a 45-second
motion pulse arrives 0.75 seconds long and is discarded outright: the faster you run it, the less reaches
the record.

So a motion burst still takes its real 20–90 seconds and a light still stays on for minutes; only the
hours of nothing in between are skipped. The practical floor is around 75 minutes per simulated day —
that is the sum of the intervals that must be played honestly, and no setting goes below it.

The temperature sensor is not on that timeline at all. A continuous stream has no idle gaps to skip, so
it ticks on the wall clock (`--report`, default every 120s) and the curve is sampled at whatever
simulated moment the run has reached. Real intervals, real values, no contradiction.

**For volume, plant history instead.** That is the division of labour between the two scripts: nine days
in one transaction, correctly timestamped, versus a live run that can only ever produce events at the
speed a real home produces them.

## What the scenario contains

Each pattern maps to a suggestion type the design puts in scope for v1 (§8), so the replay and suggestion
work has something real to find:

- **motion → kitchen light** within 30–120 s, about three times in four — the *manual habit*
- **living room switch left on overnight** on roughly a third of nights — the *standby load*
- **blind and thermostat on a daily rhythm**, weekdays differing from weekends — shape for the
  hour-of-day and day-of-week profiles
- **temperature sampled every 5 minutes**, mostly noise — deliberately, since thinning it is the numeric
  threshold rule's job


## Testing the suggestion pipeline (Phase 2)

The standby detector needs `MIN_COVERAGE_DAYS` (14) of coverage and `MIN_QUALIFYING_NIGHTS` (10) inside a
30-day window. Home Assistant's recorder purges at 10 days, so `plant_test_history.py` cannot produce a
candidate however long you run it — which is why `plant_standby.py` writes to `record.db` directly, and
what that costs is set out in its docstring.

**It has to run inside the add-on's own container.** `/data` is private to an add-on, so a Terminal add-on
shell cannot see `record.db`.

```bash
python3 scripts/plant_standby.py --dry-run
```

The dry run generates the series, runs the agent's **own** `standby.find_candidate` over it, and prints
the verdict per device without writing anything. A seed that would not have qualified is worth catching
before it is in the store rather than after the review pass silently finds nothing.

### The three devices, and what each one is for

| Device | Unit | Overnight | Expected outcome |
|---|---|---|---|
| `tv` | W | 12 W | **Proposed** — a clean standby load |
| `desk` | W | 7.5 W | **Graded and withheld** — replay counts 4 nights it would have interrupted, and `review.MAX_WRONG_NIGHTS` is 0 |
| `rack` | kW | 0.012 kW | **Proposed**, with the same estimate as `tv` — if the two differ, the unit line is broken |

`desk` is the only one that tests the *grading* rather than the finding, and `rack` is the only one that
crosses the factor of a thousand between what Home Assistant compares against and what a person is shown.

### The whole run

```bash
python3 scripts/plant_standby.py
```

Then:

1. Publish the six MQTT discovery configs (three sensors, three switches, one switch per device — two on
   one device is refused by `controllable_switch`), retained.
2. Start `simulate_standby.py` so the entities have a unit and a registry row. Without a unit the sensor
   is skipped before its planted history is ever read.
3. Add all six entities to the watchlist in the panel. An empty watchlist means nothing is recorded.
4. Restart the add-on **after 09:00 local**. `review.due` is "past `REVIEW_HOUR`, not run today" and the
   interval ticks four times an hour, so the pass runs within 15 minutes. Look for `REVIEW:` in the log.
5. The suggestion appears in the Suggestions tab, and in chat via `List agent suggestions`.

Re-running the planter is a no-op — `events(entity, ts)` is `UNIQUE` and inserts are `INSERT OR IGNORE`.
`--clear` removes every event for exactly these three sensors.

### The verification half

`VERIFY_AFTER_DAYS` is 7, so the measured result is a week away by design. To see it sooner, backdate
`verify_after` on the suggestion row in `activity.db` and plant an "after" week that draws less.
