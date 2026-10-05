# Changelog

## [2.28.2] - 2026-09-29

**Fix: an agent this home asks can ask for access itself.**

- A chat message from an agent on the *Agents this home can ask* list, when no question to it was
  waiting and it is not approved, was acknowledged and dropped (the guard against late answers). So an
  agent the household asks — the Energy Agent — could never write to request access. Now
  (`chat_handlers._handle_unprompted`) its text becomes a request to connect like any unknown
  sender's, announced once — and it is NOT answered: two agents that both reply to every message
  would loop. An awaited answer is still an answer; a denied agent's text is still dropped.
- Tests with the real uagents (routing): unprompted → silent request; awaited → answer; denied →
  dropped.

## [2.28.1] - 2026-09-29

**Fix: an approved agent's chat answers from the tools, not from nothing.**

- The first live test of 2.28.0: asked which devices were on, the model called no tool and told the
  agent "none are currently being watched or shared" — with 28 watched. `core/peer_chat.py` now nudges
  once, as the person's chat loop does (`agent_runtime._NO_TOOL_NUDGE`), while no tool has run. The
  peer prompt also says to address the other agent (it had told it to "check your Home Assistant
  dashboard").

## [2.28.0] - 2026-09-29

**Feat: any agent can connect over plain chat — no protocol of ours to implement — and the
`Fetchai-HomePeer` protocol no longer binds an implementer to our docstrings (0.2.0).**

- **Why.** Phases 1–3 let another agent in only over `Fetchai-HomePeer`, which no other agent
  implements; the goal is any agent. Chat is what every Agentverse agent speaks.
- **A chat message from an unknown sender is a request** (`handlers/chat_handlers.py`): recorded with
  `via="chat"` and its first message as the purpose (shown in the panel only), announced once with the
  address only. A person on ASI1 looks the same, so the reply carries the pairing instructions too, and
  **a merely pending request no longer stops a person pairing** (`peers.blocks_pairing`: only an
  approved, denied, blocked or askable agent is refused a code); pairing removes the request. A blocked
  sender gets nothing.
- **An approved agent's chat goes to `core/peer_chat.py`, never `rest_chat`.** Routing order: an
  awaited answer → an approved agent → an askable agent (dropped) → a paired person → the rest. Its
  own memory (20 messages, forgotten after an hour idle), `settings.peer_system_message` (an outside
  agent; its text is never the household's instruction), and its level's tools only — read: `Get watched
  devices` (`peer_protocol.answer`), the record's watched-only overview, profile and relations, the
  clock; propose adds `Propose a change to the household`, bound to that agent's address. A tool the
  model names off that list is not run. The reply passes `disclosure.text`; 20 messages an hour, checked
  before any model call; every exchange kept on the entry (`peers.log_chat`) and shown in the panel.
- **One proposal path.** `peer_handlers.raise_proposal` is `handle_action`'s logic lifted unchanged
  (limits, `vet`, `gate.propose(PEER_SOURCE)`, `add_proposal`); the ActionProposal handler and the chat
  tool both call it.
- **Protocol 0.2.0.** uagents matches a message by `Model.build_schema_digest` — sha256 of its JSON
  schema — and pydantic puts a class docstring into that schema: another implementation had to copy
  ours character for character (a `ConnectRequest` without it digested `015bae…`, ours `f06d6d…`).
  The six message models' docstrings are comments now; the digest is the class name and fields.
- **Tests.** `tests/test_peer_chat.py` (tools by level, the loop with a scripted model, off-list tools,
  rate limit, memory, log, disclosure, proposing from chat, who may still pair; with real uagents the
  handler routing). Two digest tests in `tests/test_peers.py`. `scripts/peer_probe.py chat`.

## [2.27.2] - 2026-09-29

**Fix: every line of another agent's answer is shown as its quote, not only the first.**

- The live test of 2.27.1 got a five-line answer from the Energy Agent; only its first line was a
  quote, so "Data verified from price query…" read as this agent's own statement. `tool_peers._quoted`
  quotes each line, for the answer and for the question shown before sending.

## [2.27.1] - 2026-09-29

**Feat: for an agent that can act, each question waits for your YES in chat before it is sent.**

- **Why.** The live test of 2.27.0 asked the Energy Agent on another home what it can do: it "executes
  purchases, manages wallet transactions" and manages Home Assistant automations. A question to such an
  agent is also a command, carried out on its side — outside this home's approval gate, which only
  guards changes to this home.
- **`confirm` per askable agent** (`peers.add_askable(confirm=True)`, `peers.set_confirm`,
  `/rest/peers/ask/confirm`). On by default; an entry from before this release reads as on. The panel
  has the checkbox on the add form and a switch per agent.
- **The question is parked, not sent.** For a confirm agent `Ask another agent` returns the question
  under `tool_peers.ASK_MARKER`; `agent_runtime` stops at it on both paths (recognised in the RESULT,
  as a rollback's is — the model can neither forge nor answer it); `rest_chat` parks it in its own
  slot, `pending_ask` (one of `_QUESTION_SLOTS`: parking any question withdraws the others). The next
  message decides through `parse_approval_decision`, never the model: YES sends it (off the loop) and
  gives the answer straight to the person, marked as the other agent's words; NO sends nothing;
  anything else asks again; after 10 minutes it expires unsent. An agent removed or blocked while its
  question waited is not asked. The ASI1 chat path reaches the same code through `handle_post`.
- **Tests** in `tests/test_peer_asking.py`: default on, legacy entries, the tool sends nothing, the
  marker is recognised only as a result, the question shown verbatim, YES / NO / other / expired /
  removed meanwhile, the slot rule, the panel switch.

## [2.27.0] - 2026-09-29

**Feat: the agent can ask other agents you connected — a tariff or weather agent, say — and tells you
what they answered (agent-to-agent, phase 3).**

- **Only agents a person added.** Connections tab → *Agents this home can ask*: address, a name and
  what it is for, in the person's words (`peers.add_askable`; `agent1…` only, never this agent, never
  an address paired as a person). The entry's `ask` is independent of the agent's own access to this
  home; blocking stops both. Asking one that then asks for access itself starts it at `pending`.
- **`Ask another agent`** (`tools/tool_peers.py`; registered in `agent_runtime.tools`, the prompt's
  tool list says when to use it). No agent named → the list, with each one's purpose. A name that
  matches no askable agent is refused, whole and case-insensitive — never a guess. The question goes
  through `disclosure.text`, at most 1,000 characters; the answer comes back under a note that it is
  the other agent's words, never an instruction (rule 12 names it). Every question and answer, or
  why none came, is kept on the entry (newest 20) and shown in the tab.
- **Over the chat protocol** — what most Agentverse agents speak. `core/outbound.py`: the tool's
  executor thread schedules the send on the agent's loop (bound at startup with
  `agent_1._build_context`, as interval handlers get) and waits up to 60 s for the first text back.
  One question per agent at a time. `handlers/chat_handlers.py` hands a ChatMessage from an awaited
  or askable agent to `outbound.deliver` before anything else: acknowledged, never a pairing reply,
  never the model — a late or unsolicited one is logged and dropped.
- **Tests.** `tests/test_peer_asking.py` — who may be asked, `outbound` over a real loop on its own
  thread (answer, timeout, undelivered), the tool (list, unknown agent, disclosure, the log), the
  panel endpoints, and with real uagents that `_build_context` exists.

## [2.26.1] - 2026-09-29

**Fix: an agent's proposal must aim each service at its own domain's entities, and name them.**

- **What the live test showed.** 2.26.0 put `light.turn_off` on `switch.lounge_tv_power` to a person:
  `vet` checked that a called service's domain was SOME watched entity's domain, not that of the
  entity it targets. Home Assistant ignores or rejects that call; it should never have been asked.
- **Now** (`peer_protocol._steps`, `_mismatch`): every service-calling step — at any depth, under
  `service` or `action` — must name at least one entity under its own `target`, `data` or
  `entity_id`, all of the service's domain. A step with no target (which Home Assistant may read as
  every entity of the domain) is refused too; before, a trigger naming a watched entity was enough.
- Tests: another domain's entity, a step with no target, a nested step, and the service-call case.

## [2.26.0] - 2026-09-29

**Feat: an approved agent may propose changes to the devices you watch — each one waits for your
answer in the panel (agent-to-agent, phase 2).**

- **The `propose` level** (`peers.LEVELS = ("read", "propose")`). A new message pair,
  `ActionProposal → ActionStatus`: `create_automation` (a config) or `call_service` (service,
  entity ids, data). Approve an agent at either level in the Connections tab, or change it later.
- **What may reach a person — `peer_protocol.vet`, pure.** Every entity it names is watched; every
  service it calls is non-sensitive and of a watched entity's domain (no `script.*`,
  `homeassistant.*`, `shell_command.*` unless such an entity is watched); no `area_id`, `device_id`,
  `floor_id` or `label_id` targets (they reach unwatched devices), none smuggled in `data_json`; the
  automation passes `auditor.check_config` and is no duplicate. Services are read under both
  `service` and `action` — the step key Home Assistant has used since 2024.8, which
  `auditor._service_names` does not read. The agent's `id` and `description` are dropped.
- **Through the gate, as a person's question.** `gate.propose` takes `source`, `actor` and `notice`;
  a proposal is `activity.PEER_SOURCE` with the agent's address as `actor`. `SESSIONLESS_SOURCES`
  (agent, peer) are what `pending_for` answers by id and the restart sweep leaves alone. The gate
  test-creates it, journals it and queues the question — built from the checked service and entity
  ids only: nothing an agent writes (title, reason, alias) enters the outbox, which ASI1's "what's
  new" hands to the model. The phase-1 connect notice now carries the address only, for the same
  reason.
- **Limits**, counted from the journal (`activity.peer_proposal_counts`): 3 open, 5 a day per agent.
  An unreadable journal counts as over the limit.
- **Answered in the panel only** — a card per proposal (the agent's title and reason, what it would
  do, the YAML), Approve / Reject through `/rest/approve` by change_id. Lowering an agent below
  `propose`, denying, blocking or removing it withdraws its open proposals (`gate.withdraw`, which
  `review._withdraw_change` now uses too).
- **The agent is told the outcome** — `tasks/peer_outcomes.py`, every minute: `applied`, `failed`,
  `rejected` or `withdrawn`, once; a failed delivery is retried for a week; a blocked agent is not
  told.
- **Tests.** `tests/test_peer_proposals.py` (vet, the handler, the gate, outcomes, the panel).
  `scripts/peer_probe.py propose`.

## [2.25.0] - 2026-09-29

**Feat: other agents can ask to work with this home — and get nothing until a person approves them
in the panel (agent-to-agent, phase 1).**

- **Why a second list.** A person pairs from ASI1 (`core/pairing.py`) and may answer YES to a change;
  an agent must never be able to, or it approves its own request. So agents live in their own list,
  `core/peers.py` (`/data/peers.json`, atomic, a corrupt file trusts nobody), and the two never share
  an address: an address paired as a person cannot connect as an agent, and the chat handler refuses a
  pairing code from an address known as an agent.
- **Identity.** uagents verifies the signature of every envelope from an `agent1…` address
  (`uagents/mailbox.py`); it does not verify `user1…` senders, so those are refused.
- **Its own protocol, answered by code.** `core/peer_protocol.py`: `Fetchai-HomePeer 0.1.0`, published
  with its manifest. `ConnectRequest → ConnectStatus`, `Query → QueryResult`. Nothing an agent sends
  reaches a prompt — the handlers (`handlers/peer_handlers.py`) decide from the verified address and
  the store.
- **Request → person → level.** An unknown agent's request is `pending` (one per address, at most 20
  waiting, expires after 7 days, a repeat refreshes it) and is announced once through the outbox
  (`peer_request`). The panel's ASI1 tab becomes **Connections**: people above, a new **Agents**
  section below — approve (read only), deny, block, remove. The agent is told the outcome at once
  when reachable, otherwise on its next message. A blocked agent is never answered.
- **The `read` level.** `entities`, `state` and `profile` about **watched** entities only — the
  analyst's bound; an unwatched one asked for is named back under `not_shared` and never looked up.
  Answers go through `disclosure.emit`. 60 queries an hour per agent.
- **Not yet:** proposing changes through the approval gate (phase 2), and this agent asking other
  agents (phase 3). `peers.LEVELS` is `("read",)` until phase 2.
- **Tests.** `tests/test_peers.py` — the store, `answer`, the handlers, the panel endpoints, and with
  real uagents the published manifest. `scripts/peer_probe.py` plays another agent from inside the
  add-on's container. Green on glibc in the add-on image (2761 passed, 15 skipped) and on musl.

## [2.24.2] - 2026-09-29

**Fix: only the Supervisor can reach the add-on — other add-ons, the host and SSH no longer reach
the agent's endpoints without a login.**

- **What was open.** Fetch.ai asked whether the agent's own REST endpoints are protected: uagents
  restricts only its reserved endpoints to 127.0.0.1, and ours carry no credentials. From outside
  the host they were never reachable — the add-on publishes no port, and ingress needs a Home
  Assistant login. Inside it they were: both servers listened on 0.0.0.0, and from the SSH add-on
  `172.30.33.1:8099/health` and `:8098/` answered 200 with no credentials. Any other add-on could
  have called `/rest/pairing/code` (and paired its own ASI1 identity), `/rest/approve` or
  `/rest/post`.
- **The agent listens on 127.0.0.1.** uagents 0.25.1 has no option for the address, so
  `core/uagent.py` rebinds the module constant `uagents.asgi.HOST` before the Agent is built;
  `ASGIServer.serve()` reads it at start. Nothing outside the container needs 8099: the mailbox is
  outbound and `proxy.py` forwards to 127.0.0.1. A per-endpoint `local_only` in uagents, proposed
  to Fetch.ai, would replace the rebind.
- **The proxy answers the Supervisor only.** It must stay on 0.0.0.0 for ingress, so it refuses
  (403, logged) every peer but 172.30.32.2 — the Supervisor, for ingress and the watchdog — and
  loopback, before reading the request. The peer address, never a forwarded header. Home
  Assistant's add-on docs recommend this for ingress.
- **The watchdog asks the proxy.** `watchdog:` now points at 8098; `proxy.py` forwards `GET /health`
  to the agent with an 8 s timeout (inside the Supervisor's 10 s) and answers 503 when it does not
  answer, so a stalled agent loop is still a miss.
- **Tests.** `tests/test_proxy.py`: who may ask, 403 for page, endpoints and /health with nothing
  forwarded, /health passed through and 503 when the agent is down, and config.yaml and proxy.py
  agreeing on the port (and no `ports:`). `tests/test_uagent_bind.py`: the rebind, and — with real
  uagents — that `ASGIServer.serve` still takes its host from that constant, so an upgrade that
  stops reading it fails CI instead of silently reopening 0.0.0.0. `proxy.py` starts its server
  under `__main__` so tests can import it. Green on glibc in the add-on image (2726 passed,
  15 skipped) and on musl (2740 passed, 1 skipped).
- **Not covered:** an add-on with access to Docker (e.g. SSH with protection mode off) can enter
  any container regardless.

## [2.24.1] - 2026-09-26

**Fix: a time the clock shows twice resolves to the same instant whatever ran before it.**

- **What CI showed.** `TestTheClockChanges::test_and_that_cell_has_to_be_idle_across_all_ninety_minutes`
  failed on GitHub Actions (`assert busy_from < cell_end`): the 01:30-02:00 cell on the day the
  clocks go back measured 30 minutes instead of 90. `window.instant` called mktime with
  tm_isdst -1, which leaves the repeated hour to the C library, and glibc resolves it from the
  offset of the PREVIOUS mktime call in the process: in `python:3.12-slim` (the add-on image, and
  CI's Ubuntu) 2026-10-25 01:30 Europe/London was 01:30 UTC after a winter-time call and 00:30 UTC
  after a summer-time one. musl always picks the earlier, so the dev host never failed — and the
  CI result depended on test order, which is why earlier runs on `dev` passed.
- **Not only a test.** The add-on runs on glibc, so on the night the clocks go back a schedule
  cell or standby window starting in the repeated hour, and replay's time trigger in it, could
  land on either fold depending on what the process had computed last.
- **The fix.** `instant` asks for both readings (tm_isdst 0 and 1), keeps those the local clock
  round-trips to, and returns the earlier — what every caller needs: a cell that starts in the
  repeated hour covers both passes, and Home Assistant's time trigger fires in the first fold.
  A skipped time is still None. `first_instant` (charts) now delegates to it.
- **Test.** `test_whatever_mktime_was_asked_before` primes mktime with a winter and a summer date
  before measuring the cell; on glibc it fails without the fix. Full suite green on glibc in the
  add-on image (2707 passed, 15 skipped — the stub-only checks) and on musl (2722 passed).

## [2.24.0] - 2026-09-24

**Feat: the analyst's ideas arrive as one weekly digest of up to three, on a day you pick
(free-hand F-12, F-WP9).**

- **Why.** One set of limits covered both sources: one new question a day, three open, best first
  by `kwh_per_month`. An analyst idea has no estimate, so it sorted behind every detector row that
  claimed a saving. On the real house #17–#20 (study lamp, bathroom fan, hallway, porch) sat
  `validated` while the daily slot went to a detector row, about a week from a person each.
- **The digest.** New option `digest_day` (monday…sunday, default `saturday`). That day's review
  pass (09:00) raises analyst ideas through the gate one by one, each its own `change_id` answered
  as before (panel card, `/rest/approve`), topped up to three open analyst questions — last week's
  unanswered ones are neither replaced nor stacked on. Longest waiting first. Then one normal-urgency
  message, "This week's ideas from the analyst", with each card's title, finding and rule and a
  pointer to the panel. It is never pushed, and ASI1 chat collects it with "what's new".
- **Detector rows keep their daily cadence.** Analyst rows no longer count against
  `MAX_PROPOSALS_PER_RUN` or `MAX_OPEN_PROPOSALS`, and never take the daily slot. "Never two
  questions about one device" still holds across both sources. On digest day the digest runs
  first, so the analyst idea is raised and the detector row waits a day, not the idea a week.
- **Held once a week.** A stamp in `/data/digest.json`, written before anything is raised. A
  restart on digest day sends nothing twice. A digest day the add-on was down is held the next
  morning, once. A missing or unreadable stamp counts as held now: the first digest after this
  update is the next digest day, not today's restart. An empty digest day still counts as the
  week's, so an idea stored on Tuesday waits for Saturday.
- **A waiting idea is kept 14 days, not 7.** The analyst is told what is already queued and does
  not resubmit it, so under the old unseen-for-a-week rule every leftover would be retired the
  day before its second digest.
- "List agent suggestions" in chat now shows up to six open questions (three per source). At three
  it would have hidden open analyst questions the panel shows. The prompt states the new cadence.

## [2.23.1] - 2026-09-24

**Fix: the daily explorer run stops wasting its budget on repeats, drops a rule that would never
have acted, and no longer gives a reason the record cannot show.**

Field run 4 — the first scheduled explorer run on 2.23.0 — found the bathroom fan, hallway and
porch habits unaided, and showed three faults:

- **One query sent eleven times, character for character** — ten of the run's 19 queries were
  repeats. The run spent 19 of its 20 queries and
  15 of its 16 model calls, and submitted with one call to spare. A call identical to one already
  run in the same pass (name and arguments, key order ignored) is now answered "Not run: you made
  this exact call at turn N … its answer is above" — it spends no query, adds nothing to the card's
  "What was sent" (nothing new left the house), and the budget checks count only the calls that
  would run. The run's log line counts `repeat(s) not run`. The prompt says so.
- **"Hallway light off 5 minutes after last motion" reached the queue with a chart showing it would
  never have acted**: the household already switches the light off before the rule's five minutes
  run out. A timeline or overlay chart that draws, carries no note, and shows no marker at all now
  drops the idea, logged `ANALYST: dropped '…' — its overlay over the 7 day(s) drawn shows the rule
  would not have acted once`. An empty chart with a note (the rule acts on something not drawn, or
  fires the record could not judge), a heatmap or daily count (no markers by type), and a chart
  that could not be drawn are kept as before. The prompt tells the model to check this before it
  submits.
- **"Porch light on at sunset" said the times tracked "the darkening hour"** — the record holds no
  sun data, and the times it counted were fixed: about 19:00 on weekdays, about 20:00 at weekends.
  The prompt now says the finding describes only what the record shows (times counted, not causes:
  not sunset, daylight, weather or holidays); a sun trigger may still be the proposed rule.

## [2.23.0] - 2026-09-23

**Change: the claim-audited analyst run is retired — the explorer is the one daily run
(free-hand F-9, F-WP8).**

- **Why now.** F-9 set the bar at one field run of the explorer on the real house. Field run 3
  (2.22.1, `asi1`): the planted "study lamp left on in the empty room" habit — which no claim
  type could state — found on the first query at exactly the planted 74.6 h; the kitchen idea the
  household had refused not raised again; every chart marker checked by hand against the record.
  The audited run's `checked` label had proved weaker than it read (#12: "checked" on 30 switch-ons,
  its time of day unclaimed).
- **The `analyst_mode` option is gone** (`config.yaml`, `run.sh`, `settings`, the Configuration tab).
  `analyst_enabled` is the one switch; its description now says what the daily run sends. An
  install that saved `analyst_mode` keeps a harmless leftover the Supervisor ignores.
- **Removed:** the two-pass run (`analyst.run_once`, `runner_for`, the shortlist / author passes and
  their prompts, `Submit shortlist` and `Submit automation candidate`), the claim half of
  `core/auditor.py` (recount, judge, the window rule, the `checked` / `untested` verdict — `audit()`
  itself), and `suggestions.mark_audited`. Kept: `auditor.check_config` / `duplicate_of` /
  `signature`, the house checks every explorer idea passes before it is stored.
- **Rows the old run stored are withdrawn** by the next review pass, with the reason "the claim
  audit that checked it has been retired; the analyst now shows its evidence as a chart, and may
  raise the idea again with one". A proposed one has its question withdrawn with it. Rows already
  applied are verified and rendered as before.
- The profile tool's notes to the model no longer speak of claims: `events`, `on_fraction` and
  `mean_on_duration_seconds` now say what kind of number each is.
- README: "What leaves your home" describes one daily run; the Configuration table loses its row.
- Scripts: `run_analyst.py` runs the explorer (`--mode` removed, `--trace` prints the submitted
  ideas); `plant_analyst.py --dry-run` counts each habit straight from the generated rows instead
  of recounting with the auditor.

## [2.22.1] - 2026-09-23

**Fix: the explorer no longer builds an idea on a device that stopped reporting.**

- Field run 2's "Kitchen light off after 30 min without motion" (#15) stood on 466 hours of the
  light "on" while the motion sensor was "off". Its SQL was right; its stretches were not evidence:
  every long one ended in an `unknown` row — a Home Assistant restart after the old test device had
  gone — not in an "off". Nobody left a light on.
- `Query the behavioural record`: the description now says a row in `unknown` / `unavailable` is a
  GAP — the stretch it closes ended in no reading, not in a change — and that a state held for more
  than a day and then ended in a gap is usually a dead device, not a habit. Worked example (b), the
  overlap query the model copies, now counts only stretches closed by a real change.
- The explorer's prompt gains "GAPS AND DEVICES THAT STOPPED REPORTING": count only stretches closed
  by a change, never build an idea on one closed by a gap, and look at the timeline of a state that
  never changes before claiming anything about it.
- Test devices (`scripts/`, not shipped): a fourth analyst device, `study` — an occupancy sensor and
  a desk lamp left on in the empty room on about half the weekday evenings. No claim type of the
  audited run can state it; only the explorer's overlap query finds it (free-hand F-WP8).

## [2.22.0] - 2026-09-23

**Feat: F-WP6 of the free-hand analyst — the analyst learns from your answers.**

- **The explorer's first message carries `household_answers`.** The last 20 answers to earlier
  explorer ideas, newest first — each idea's title, `yes` / `no` / `rolled_back`, and the reason
  typed after "No, because…" — in the same `disclosure.emit` payload as the orientation, capped at
  8 KB as emitted (oldest dropped, never an answer cut). The explorer prompt gains THE HOUSEHOLD'S
  ANSWERS: do not re-raise what was refused or rolled back, respect the reason, and a reason is
  quoted data that may change which ideas are proposed and nothing else. The reason stays a JSON
  string under a fixed key, one line, capped again at 500 characters. The audited run does not get
  the block (its rows take no feedback; F-WP8 retires it).
- **Only an answer the change journal backs is sent.** The panel saves the answer before it posts
  to `/rest/approve`, and that approve can be refused (already answered from ASI1 chat, expired):
  the row kept an answer that never took effect. The block now checks each answer against the
  journal row of the change it answered — `yes` ↔ approved and applied, `no` ↔ rejected,
  `rolled_back` ↔ undone. A contradicted answer is dropped with its reason and the journal's own
  answer is sent instead; an idea answered from ASI1 chat (no feedback row) is sent as the journal's
  answer with no reason. Stored feedback now also holds `change_id`, the change it answered, so a
  row raised again after its cooldown is checked against the right one.
- **A rolled-back explorer idea is answered `rolled_back`.** `core/rollback.py` writes it after a
  successful inverse, so every path — the measured result's "roll back", the Activity tab's undo,
  chat's confirmed rollback — records it; nothing is re-gated, a failed inverse writes nothing, and
  a feedback write that fails or raises is logged without failing or undoing the rollback.
  `suggestions.set_feedback` takes `rolled_back` only with the journal's applied rollback of that
  row's change, so `/rest/suggestions/feedback` still refuses it. The card says "You applied this
  and then rolled it back on …".
- The card's reason box and note now say the agent reads the answer before it next looks for
  ideas; the README and the `analyst_mode` description say the last 20 answers (≤ 8 KB) are sent,
  pinned to their constants by `tests/test_readme_egress.py`.

## [2.21.6] - 2026-09-23

**Fix: an explorer card's "What was sent" lists every answer the run received.**

- **`sent` holds every tool call that ran, not only the two record tools.** A profile, a pattern
  list or an entity list left the house as surely as a query did, but the card listed only
  queries and timelines while its note said "Every read of your record". `run_explorer` now keeps
  a `_sent_entry` for every call that ran; the query budget still counts the record tools only.
- **`EXPLORER_MAX_CALLS` (48) bounds it.** The store had no cap on `sent`, and one model response
  may batch any number of calls. A response that would pass 48 tool calls in the run is refused
  whole and told how many remain — the query budget's rule — so a call that is not listed never
  runs, and the list stays complete rather than cut.
- `SENT_NOTE`, the README and the `analyst_mode` description now say "every answer the agent
  received in this run", the opening overview still named separately; `tests/test_readme_egress.py`
  pins the phrase to `SENT_NOTE` and the 48 to its constant.
- README: `agent_display_name` is not used by the agent at present, as the Configuration tab says.

## [2.21.5] - 2026-09-23

**Docs: say what leaves the house (F-WP7 of the free-hand analyst).**

Since 2.21.0 the default daily analyst (`analyst_mode: explorer`) sends ASI-1 raw record rows of
watched devices, not only aggregates. Now the pages a household reads say so.

- **README: "What leaves your home".** What chat sends; what the daily analyst sends in each mode —
  the whole house's inventory, but the history of watched devices only; the bounds, each read from
  the constant that sets it (500 rows / 32 KB per search, 100 rows by default; a timeline ≤ 5
  devices over ≤ 21 days; ≤ 20 searches and timelines and ≤ 16 model calls per run; up to 200
  recent changes per device in audited mode; chat's Home Assistant history up to 200 changes over
  10 days); what is never sent; what an idea card's "What was sent" lists and what it does not;
  how to stop it (`analyst_enabled: false`, or `analyst_mode: audited`). `tests/test_readme_egress.py`
  pins every number to its constant.
- **`translations/en.yaml` (new).** The Configuration tab showed no description for any option;
  every option in `config.yaml`'s schema now has a name and a description, and the analyst options
  say what each mode sends and how to stop it. A test holds that every schema key is described
  and no described key is stale.
- `config.yaml`'s `analyst_mode` comment and `core/disclosure.py`'s table of what each tool emits
  follow 2.21.3–2.21.4. No behaviour change.

## [2.21.4] - 2026-09-23

**Fix: nothing the record holds about an unwatched device reaches ASI-1, and the daily analyst no
longer reads Home Assistant's own history.**

- **The chat record tools filter on the watchlist, in chat and both analyst modes.** 2.21.3 fixed
  the query tool; the three older tools had the same gap — unwatching deletes nothing, and
  `Get entity behaviour profile` returned an unwatched entity's profile and up to 200 of its raw
  transitions. Now:
  - `Get entity behaviour profile` returns only the entity's id, name and `watched: false`, with
    a note that it is not watched, so it is not shown.
  - `Get observed patterns` shows a pattern only when BOTH of its entities are watched, filtered
    before the limit; asked about an unwatched entity it shows none.
  - `Get behavioural record overview` — and `record_overview` in the explorer's first message,
    the same builder — lists watched entities only, and its totals (events, entities, oldest,
    newest, backfilled) are summed over those rows instead of taken from `record.stats()`.
  The panel's Record tab is unchanged: `observation.coverage` over REST still lists an unwatched
  entity that holds history. That never leaves the house.
- **`Get Home Assistant entity history` is out of the analyst's tools (both modes).** It reads
  Home Assistant's recorder for ANY entity — up to 200 changes over up to 10 days — so an
  unattended run could send the history of a device nobody chose to have watched. Chat keeps it.
  Nothing in the audited run depended on it (every claim recounts from the record); the
  explorer's prompt no longer lists it. The inventory tools (entities, devices, areas, services,
  automations, current state and details) stay: an idea has to name real devices.

## [2.21.3] - 2026-09-23

**Fix: the explorer's query tool shows only the entities watched now.**

- **Unwatching deletes nothing, so the query tool filtered nothing.** An entity taken off the
  watchlist keeps its rows in the record until retention (90 days) or the size cap removes them,
  and `Query the behavioural record` read `events` / `entities` / `relations` with no reference
  to the watchlist — so in explorer mode an entity the household had stopped watching could still
  be queried and its rows sent to ASI-1. F-3's bound is "watched", and it was not held.
- **`ev` and `rel` now filter on the watchlist at the call.** The watched ids are copied into a
  TEMP table when the connection opens (before `query_only`, like the views); `ev` keeps an event
  only for a watched entity, `rel` a relation only when BOTH entities are watched.
- **The tables are no longer readable directly.** `SELECT … FROM events` is refused, naming the
  views to use instead. The authorizer knows which view a read is for — but SQLite reports a
  CTE's name the same way, so `WITH ev AS (SELECT * FROM events)` would pass as the real view. The
  filtering views are therefore named with a random token fresh per call; `ev` and `rel` are
  plain SELECTs over them, and the tables are readable only when one of those names is the reader.
- **"the record holds nothing" is no longer said of an unwatched entity.** The timeline tool now
  says it is not watched, so it is not shown; a chart that names one says it is not drawn. The
  record may well hold it. A chart stored before this keeps its stored sentence.
- Not changed here: the chat record tools (overview, profile, patterns), which the analyst also
  calls, still answer from the unfiltered record for an entity that is no longer watched.

## [2.21.2] - 2026-09-23

**Fix: the explorer's chart looks like the rest of the panel.**

- The chart sits in the same framed inset as the automation's YAML and "what was sent" (page
  background, hairline border, small radius) instead of painting a white band per day onto the
  card. Days are ruled apart like the panel's table rows; bars have the 2 px radius of the Record
  tab's hour bars.
- `--chart-1` and `--chart-heat` are now the panel's own `--accent` in both modes, the Record tab's
  blue. Slot 2 stays orange: it is the hue that keeps two lanes apart under colour blindness.
- A heatmap's zero cells are `--surface-strong`, visible inside the frame's page background.

## [2.21.1] - 2026-09-23

**Fix: the explorer's card, after its first look in a real browser.**

- **Charted devices are named as the household names them.** The caption ("Overlay of …"), the
  legend and the chart's hover text use the friendly name from the cache the Record tab reads
  (`observation.name_for`, refreshed by `tasks/observation.py`); an entity with no name yet is shown
  by its id, never a blank. The legend keeps the id on hover. `/rest/suggestions` carries
  `chart_names` for an explorer idea; `artefacts.shown_detail(row, names)` takes the map.
- **The marker legend lists only the shapes the chart draws.** A chart with only "would turn off"
  markers no longer explains ▲.
- **"an overlay", not "a overlay".** `core/charts.py`'s four sentences naming a chart type use its
  `_a()` helper. A chart stored before this keeps its stored wording until it is redrawn
  (`scripts/run_analyst.py --redraw ID`).
- **A second redraw no longer loses "the agent asked for …".** A chart stores the window it drew
  as its spec; `charts.redraw` now draws from `window_clamped.asked` when there is one, so
  redrawing a clamped chart clamps it again instead of treating the drawn window as the request.

## [2.21.0] - 2026-09-23

**Feat: F-WP5 of the free-hand analyst — the card in the panel, and `explorer` by default.**

- **`analyst_mode` now defaults to `explorer`** (`config.yaml`, `run.sh`'s jq fallback and
  `settings._analyst_mode` say the same word; an empty or unreadable value is `explorer` too,
  logged). `audited` stays selectable. An install whose saved options already hold
  `analyst_mode: audited` keeps it — the Supervisor does not overwrite a saved option; set it in the
  add-on's Configuration tab. **What this means for what leaves the house:** the explorer sends the
  model the rows it asks the record for (watched entities only, within the tools' size caps), not
  only the aggregates the audited run sends. The design, the pairing guide and the option
  description do not say so yet — that is F-WP7. `analyst_enabled: false` still stops both runs.
- **The explorer's card.** A suggestion from the explorer run gets its own card in the Suggestions
  tab: the agent's finding, the chart drawn from your record when the idea was stored, the rule in
  the agent's words, the automation (behind a toggle, the YAML the approval is recorded against),
  and "What was sent to the model for this" — every record read of that run, as sent. All four
  chart types are drawn as inline SVG: timeline and overlay (a row per day, a lane per device,
  ▲ / ▽ where the rule would have turned it on / off), heatmap (weekday × hour) and daily count
  (bars, with a hatched strip for minutes with no data). "No data" (unknown / unavailable) is
  hatched in every chart. A window the chart had to clamp, markers the rule could not have drawn,
  and a chart the record could not draw at all are each said in a sentence from
  `core/artefacts.py` (`shown_detail`) — the panel draws the series, Python writes the claims.
- **Yes / No / No, because….** Both end on the existing `/rest/approve` with the suggestion's
  change id, exactly as Apply and Dismiss do: the gate still decides and executes. Before that, the
  panel records the answer with the new **`POST /rest/suggestions/feedback`**, which stores
  `{answer, reason, at}` in a new `feedback` column and nothing else — no state move, no journal
  row, no gate. If recording fails, nothing is sent to `/rest/approve`. The reason is kept on the
  suggestion; feeding it to the next run is F-WP6.
- Detector and audited cards are unchanged, and so is the chat text of every suggestion.
- Panel rebuilt (`dist/`). Chart colours are new tokens in `styles.css` (`--chart-1..3`,
  `--chart-heat`, `--chart-rest`, `--chart-gap`, `--chart-grid`), light and dark.

## [2.20.0] - 2026-09-22

**Feat: F-WP4 of the free-hand analyst — charts drawn from the record.** Only matters with
`analyst_mode: explorer`; the default `audited` run is unchanged. No panel change yet (the card is
F-WP5), so no `dist/` rebuild.

- **Charts are drawn at store time.** When the explorer stores an idea, `core/charts.py` draws the
  chart it asked for from the record before the row is written, and the row keeps the drawn chart:
  `{spec, series, markers, markers_note, error, drawn_at, window_clamped}`. Until now the row held
  only the request (`series: null`). The series comes from the same rows and the same
  run-builder (`core/timeline.py`) as the timeline tool the model used, so what the person is
  shown and what the model was told cannot disagree.
- **Four chart types.** `timeline` and `overlay`: each local day's runs per entity, in minutes
  since local midnight; a day the clocks go forward is 23 hours long and a day they go back is 25,
  and each day says how long it is. `heatmap`: changes into a state by weekday and hour, plus how
  many of each weekday the window covered. `daily_count`: changes into a state per day, plus how
  many minutes of each day had no data.
- **A window that does not fit is clamped, not refused** (field run 2: idea #15 asked for an
  overlay of 7.6 days against a cap of 7 and got no chart). Too long → the most recent span the
  type allows; too short → widened back from where it ends; ending in the future → ends now;
  starting before the record → starts where the record does. `window_clamped` says what was asked
  and what was drawn. Only a chart that cannot be drawn at all (unknown type, an unwatched or
  never-recorded entity, no rows in the window) carries an `error` — and the idea is still stored.
  The explorer prompt now says windows are clamped.
- **`unknown` and `unavailable` are drawn as gaps ("no data")**, never as the state before them,
  and the heatmap and daily count never count a change into or out of them. Field run 2's "466
  hours on with no motion" was a test light whose simulator was not running, sitting `on` until a
  Home Assistant restart made it `unavailable`; drawn this way, those days show as no data.
- **What the rule would have done — markers on timeline and overlay charts, for a supported
  subset only.** Evaluated against the record with Home Assistant's own rules for these shapes:
  state triggers (one entity or a list; `to`, `from`; `for` — fires only if the new state held for
  the whole duration); time triggers (`at`, every local day; a time the clock skips does not fire
  that day, a repeated time fires once, the first time); state conditions, time conditions
  (`after` / `before`, including windows across midnight, and `weekday`) and `and` of those;
  `turn_on` / `turn_off` / `toggle` on `light`, `switch`, `fan` and `input_boolean` targets named by
  `target.entity_id`; `mode: single`. A toggle is resolved from the target's state at that moment,
  and an `unavailable` target gets no marker (Home Assistant skips it). **Anything else anywhere in
  the rule** — templates, sun, numeric_state, delays, choose, other services, device triggers, area
  targets — draws no markers at all, with the note "the agent can't draw what this rule would have
  done" naming the first part it cannot read. Markers show what the rule would have done given
  what actually happened; they do not simulate the rule's own effect on later events.
- **Redrawing an old row.** `charts.redraw(id)` draws a row stored before this version again from
  its own request and config and writes it back through `suggestions.set_chart` (the row's state,
  place in the queue and `updated_at` do not change). From the container:
  `python3 run_analyst.py --redraw 15` — no model call, no key.

## [2.19.1] - 2026-09-22

**Fix: the explorer spends its budget on the house, not on orienting.** Only matters with
`analyst_mode: explorer`; the default `audited` run is unchanged. No panel change, so no `dist/`
rebuild.

- **What field run 1 showed** (2.19.0, on asi1-mini because `asi1_model_strong` is unset). 10 model
  calls, 5 record-tool calls, nothing submitted — the step budget ran out. The SQL was valid every
  time. About half the calls went on orientation: four separate `Get Home Assistant automation`
  calls, then the overview, the observed patterns and the suggestions. The queries then went to the
  power sensors the standby and schedule detectors already cover. The planted habits (hallway
  motion → light, bathroom fan left on, porch light ~19:00 on weekdays) were reached only in the
  last calls and never queried.
- **Orientation up front.** The run's first message now carries, through the disclosure boundary:
  the record overview (the overview tool's own answer), every existing automation as its alias
  plus the entities that trigger it and the entities it acts on (from the configs the run already
  read for the duplicate check — no extra Home Assistant call), the watched power sensors the
  detectors cover with the switch each pairs to, and the open suggestions and detector findings as
  before. The prompt says all of it is given and not to fetch it again; the tools stay available.
- **What the detectors cover, stated.** Standby power at night and idle-on-a-schedule power are the
  detectors' job. The list of sensors they cover is derived from the detectors' own pairing rule
  (a power sensor with exactly one switch on its device), never hardcoded; the prompt points the
  model at lights, motion/occupancy, switches with no power reading, habits between entities and
  things left on.
- **A budget warning inside the loop.** When 2 model calls remain, one message tells the model how
  many calls and record-tool calls remain and to submit with what it has. Once per run.
- **A turn past the query budget no longer ends the run.** Its calls are not run; each is answered
  with how many record-tool calls remain, and the run goes on (the model can still submit or ask
  for fewer). Running out of model calls still ends it with nothing stored.
- **Budget: 16 model calls per explorer run (was 10); record-tool calls stay at 20.** Field run 1
  used 5 of its 20 queries — it was the calls that ran short.
- **The strong-model daily cap does not fit two runs a day.** `ASI1_STRONG_DAILY_CAP` stays at 20.
  Once `asi1_model_strong` is set, one explorer run can take up to 16 of those 20, so a manual run
  (`run_analyst.py`) on the same day gets at most the 4 left and will most likely end with nothing
  submitted. Raise the cap in the add-on's options for a day of testing; it is not raised here.
  While the strong model is unset (the same as the normal one), the cap is not spent at all.
- **The prompt is rebuilt around a short plan** — read the given orientation, pick two or three
  candidate habits from what is not covered, one or two queries or a timeline per candidate,
  submit — and states its budget from the code's constants. The query tool gains a fourth worked
  example: the time something was first switched on each weekday.
- **New dev script `scripts/probe_asi1_models.py`**: asks ASI-1 which model names exist
  (`GET /v1/models`, else one 1-token call per candidate name), so `asi1_model_strong` can be set
  from an answer rather than from documentation. Never prints the key. Not run by this release.

## [2.19.0] - 2026-09-22

**Feat: the analyst's explorer run (free-hand analyst, F-WP3) — built, OFF by default.** A new
add-on option `analyst_mode` chooses what the daily analyst run does: `audited` (the default,
unchanged — cheap shortlist, strong author, every figure recounted) or `explorer`. Nothing changes
unless you set `explorer`. The panel cannot draw an explorer idea's chart yet (F-WP5), so the
default stays `audited` until it can. No panel change, so no `dist/` rebuild.

- **What `explorer` does.** One pass on the strong model (`asi1_model_strong`, or the normal model
  when that is unset) explores the behavioural record with the analyst's read tools plus two new
  ones — `Query the behavioural record` (one read-only SQL SELECT) and `Get entity timeline` — and
  submits one to three ideas. Each idea is a title, one sentence of what it saw, the rule in plain
  words, the automation config, and a request for a chart of the recorded data that shows it.
  Budget per run: 10 model calls and 20 calls to the two record tools; reaching either, or the
  strong-model daily cap, ends the run with nothing stored. Each model call waits at most 90 s
  (twice retried at most); each tool call at most 30 s.
- **What it sends out of the house when on (proposal F-3 — wider than `audited`).** Whatever the
  model asks the two record tools for goes to ASI1 as returned: raw state changes of WATCHED
  entities (entity id, local time, state, how long the previous state lasted), within the tools'
  caps (a query answers at most 500 rows / 32 KB; a timeline at most 5 entities, 21 days, 300
  runs each). Only watched entities are in the record, every answer goes through the disclosure
  boundary, and `analyst_enabled: false` still stops the run. Each stored idea keeps a copy of
  what was sent for it (`sent`: every query and the first 2 KB of its answer), so "what left for
  this" is a fact on the row. The design doc and the pairing/security guide say this in F-WP7.
- **What is kept from the audited run.** The allowlist (checked on every call), the submit call
  never answered, the house checks: an idea naming an entity or service Home Assistant does not
  have, calling a sensitive domain (locks, covers, alarms, …), or copying an existing
  automation is dropped and stores nothing. Asking you still goes through the morning review,
  one question a day.
- **How an idea is stored and shown.** A new state of evidence, `shown` ("suggested by the analyst
  — see the chart"): no recounted claims, no *not tested* warning. Chat, ASI1 and the digest show
  the title, the sentence, the rule under "What it would do (the agent's words)" and the YAML. The
  chart request is checked (type, entity count, window length, entities watched, the record
  reaching back to its start) and stored; a chart the record cannot draw keeps the idea and
  carries the reason. It ranks where *not tested* rows do, and is withdrawn when a charted entity
  stops being watched.
- New columns on `suggestions`: `finding`, `rule_text`, `chart`, `sent` (migrated in place).

## [2.18.2] - 2026-09-22

**Fix: a hung ledger query froze the whole agent for ~21 h.** At 13:42 on 2026-09-21 uagents'
hourly Almanac re-registration asked the Fetch.ai testnet ledger whether the agent is registered
and never got an answer. That query (cosmpy, synchronous gRPC, no deadline) runs inside an
`async def` on the agent's event loop, so the loop itself waited: no interval task (prune,
review, analyst), no REST call from the panel, no chat, until the add-on was restarted at ~10:05
on 09-22. The observation thread kept recording. uagents 0.25.5 has the same code. No panel
change, so no `dist/` rebuild.

- **Registration is bounded.** New `core/registration.py`, passed to the `Agent` as its
  `registration_policy`: the same two steps as uagents' default. The Almanac API step is
  unchanged. The ledger step runs on its own daemon thread with its own event loop; the agent
  waits at most 120 s for it, then logs `REGISTRATION: ledger step did not answer in 120s —
  skipped` and tries again in an hour. While a stuck thread is still alive no second one is
  started, so a dead node costs at most one thread. uagents' own error handling is kept
  (insufficient funds ignored, other errors retried in a minute). The ledger policy is also no
  longer built at import time — its constructor queried the ledger too.
- **The on-chain record is kept.** Senders find the agent through the Almanac API first and use
  the ledger record only as a fallback; it cannot be shown that ASI1/Agentverse never need it,
  so it stays (reasoning in `core/uagent.py`'s docstring).
- **A stalled loop now shows within minutes.** New `tasks/heartbeat.py`: a 60 s interval stamps a
  heartbeat; a watcher thread logs `LOOP: stalled for N s` once it is older than 180 s, and one
  line when it recovers.
- **The Supervisor can restart a stalled agent.** New `GET /health` on 8099, answered on the
  event loop, and `watchdog: "http://[HOST]:[PORT:8099]/health"` in `config.yaml`. With the add-on's
  Watchdog toggle on, the Supervisor polls it every 120 s (10 s timeout) and restarts the add-on
  on its second miss.
- Also in this build, not wired to anything yet: `tools/tool_record_query.py` (F-WP1 of the
  free-hand analyst proposal).

## [2.18.1] - 2026-09-21

**Fix: the analyst could not cite a figure the auditor recounts, so every candidate was refused.**
In the first field runs (2026-09-21) the analyst read the right devices and wrote sensible
automations, and the auditor refused all three — correctly. The entity profile the model reads
printed only every change of any state (`events`), the share of time on and the mean on-duration,
none of which is a figure a claim can state; the model copied the nearest number (294 changes
cited as 146 "on" transitions; a mean duration in seconds cited as hours on). No panel change,
so no `dist/` rebuild.

- **The profile prints the citable figures.** `Get entity behaviour profile` now also prints
  `changes_to_state` (changes INTO each state), `on_hours` (hours on, spans cut at the window's
  start) and the window they are true for, `window_start` → `window_end` ("now"). The window never
  starts before the entity's first recorded event, so it is one the auditor accepts. Both figures
  count live events only, as the auditor does. `events`, `on_fraction` and
  `mean_on_duration_seconds` are unchanged and marked "not citable as a claim". The figures are
  computed by the record's own SQL, not by the auditor, and a test pins the two to each other.
- **The analyst's prompt says which field each claim copies.** `transitions` →
  `changes_to_state[<to_state>]`, `on_hours` → `on_hours`, both from `window_start` to "now";
  never an hour/weekday filter (no tool prints a filtered count). An entity in the config (a
  trigger, a sun condition) need not be watched; one inside a claim must be.
- **A refused submission is logged with its raw arguments** (cut to 1500 characters), locally
  only. The first run's empty shortlist could not be diagnosed without them.
  `scripts/run_analyst.py --trace` prints every submission's arguments, accepted or not.

## [2.18.0] - 2026-09-18

**Phase D of `docs/PROPOSAL-MODEL-ANALYST.md` (WP-D1..D3): the analyst — once a day the model
reads the house and suggests new automations of its own, and only what the auditor passes is
kept.** Nothing reaches you by a new route: an idea it keeps waits in the same queue as the
detectors' suggestions, and the daily review still asks at most one question a day. No panel
change, so no `dist/` rebuild.

- **What it does (D2).** New module `core/analyst.py`. A first pass on the cheap model reads the
  house through read-only tools only (entities, areas, history, the record, observed patterns,
  existing automations, open suggestions) and shortlists up to three ideas. A second pass, one
  strong-tier call per idea, writes each as a new automation plus the figures it relies on
  ("the hall light followed motion 14 times"). Each goes to the auditor (2.17.0), which recounts
  those figures from the record and checks the config; a refusal is logged and stores nothing,
  and the model is never told why. Passing ideas are stored labelled *checked* or *untested*.
  The analyst never proposes anything itself, cannot call a service or write an automation, and
  over the strong-tier daily cap the remaining ideas wait for tomorrow — nothing falls back to
  the cheap model.
- **When it runs (D3).** Daily from 07:00 local, two hours before the review, so that morning's
  review can rank what it stored. The day's run is recorded on disk before it starts, so a
  restart never runs it twice; if that record cannot be written, it does not run. It skips,
  with one log line, when switched off, without an ASI1 key, with nothing watched, or when Home
  Assistant's registry, services or automations cannot be read.
- **The `analyst_enabled` option (D3), default on.** This is the first thing the agent sends to
  ASI1 unattended (design §10.1): once a day, without anyone asking, the analyst sends what the
  read tools return — entity names and states, history, the behavioural record's summaries,
  your automations — to the ASI1 model. Turn `analyst_enabled` off to stop it; chat and the
  detector-based suggestions are unaffected.
- **How analyst suggestions age (D1).** An analyst suggestion is withdrawn when a device its
  figures were counted from stops being watched, ranks behind any suggestion that claims an
  energy saving (then by label: *replayed*, *checked*, *untested*), and is retired if Home
  Assistant rejects its automation when it would be proposed. The same idea submitted again
  updates the existing suggestion instead of adding a second one.

## [2.17.0] - 2026-09-18

**Phase C of `docs/PROPOSAL-MODEL-ANALYST.md` (WP-C0..C2): the auditor — plain code, no
model, deciding whether a model-raised candidate is shown and with which label.** New module
`core/auditor.py`, imports nothing from `agent_runtime` and calls no model; nothing calls it
yet (Phase D). No panel change beyond C0's tag, so no further `dist/` rebuild.

- **Detector cards show their label too (C0).** `core/artefacts.py` prefixes every card's
  headline with its evidence label, detector rows included — B shipped the tag for analyst
  rows only, which left a detector card as the one place *replayed* was never said. The panel
  drops its analyst-only guard on the tag span to match.
- **The claim vocabulary (C1).** Three types, each recounted from `record.between` rows by a
  pure function and never read back from an aggregate the model was shown: `follows` (A-events
  followed by a B change within `record.RELATION_WINDOW_SECONDS`), `transitions` (changes into
  a state, filtered by local hour range and/or weekday), `on_hours` (time spent `on`, clipped
  to the window). Any other type, or params that fail that type's own shape, is `not_checkable`
  — stored, never rendered as evidence, never a refusal.
- **Match, contradict, or too weak (C2).** `matched` within `max(2, 10%)` of the recount;
  outside that, or a `follows`/`transitions` recount below `record.RELATION_MIN_SUPPORT`,
  refuses the whole candidate — never a downgraded label. A claim's window must be one the
  record covers: `start < end <= now`, span within `record.RETENTION_DAYS`, every named entity
  watched, and the window's anchor entity's own history reaching back to `start`.
- **Static checks (C4/C5/C6).** The config is normalised (`automations._normalize_automation_cfg`)
  and checked for alias/trigger/action, every referenced entity existing in the registry
  snapshot, every called service existing and none of them sensitive
  (`actions.is_sensitive_service`), and no duplicate of an existing automation's trigger and
  action signature.
- **`audit()` (C2).** The one I/O function — reads the record, the watchlist, the registry and
  Home Assistant's `/services`, only when not handed in — wiring the checks above into one
  verdict: `{"ok": True, "config", "claims", "evidence_level"}`, exactly what
  `suggestions.mark_audited` accepts, or `{"ok": False, "reason"}`. Every refusal is logged
  `AUDIT: refused — <reason>`.

## [2.16.0] - 2026-09-18

**Phase B of `docs/PROPOSAL-MODEL-ANALYST.md` (WP-B1..B3): a suggestion no detector raised is
a first-class row — stored, rendered in every §9 artefact, verified a week later — before
anything can raise one.** Nothing in this build creates an analyst row except tests. Detector
rows render and verify exactly as before.

- **Store (B1).** `suggestions` gains `source`, `evidence_level`, `model`, `title`,
  `rationale` and `claims`, added in place by `PRAGMA table_info` + `ALTER TABLE`; rows already
  on disk read as `detector` / `replayed`. `upsert_analyst_candidate` writes an
  `analyst:<digest>` row at `candidate`, *not tested*. `mark_audited` is the second entry to
  `validated` (B-6): it refuses `replayed` without a replay, `checked` over any claim that is
  not `matched`, a claim the auditor could not have stored, and a detector row.
  `mark_validated` is now detector-only.
- **Rendering (B2).** `core/artefacts.py` routes on `source`, never `kind` (B-1). An analyst
  card is headed `<title> (<label>)` — *replayed* / *checked against your history* / *not
  tested* — opens with design §15.3's fixed warning when not tested, prints evidence only from
  matched claims and only the **recounted** figure (B-3), puts the model's reasoning under
  *"Why the agent suggests this (its own reasoning, not checked)"*, and reads the config back
  as its alias plus YAML (B-4). Model-written text is printed, never parsed.
- **Verification (B2).** New outcome `not_measured` (B-5): an analyst row is reported at its
  first due date with *"The agent had nothing it could measure for this change."*, never
  retried, with Keep / Roll back offered as for any other. `review.settle` records
  `{"outcome": "not_measured"}` instead of measuring. Deleted, disabled and edited still
  apply; for a config the replay never graded, "edited" is a config comparison with Home
  Assistant's filled-in defaults normalised (`verify._same_config`).
- **Panel (B3).** Analyst cards show the evidence-level tag, the warning as a notice (new
  `--warning` / `--warning-bg` tokens, light and dark), the reasoning under its heading, and
  the config as an all-added diff (`useDiff.addedLines`). The empty-state notice no longer
  promises that every suggestion was replayed.

## [2.15.0] - 2026-09-18

**Phase A of `docs/PROPOSAL-MODEL-ANALYST.md` (WP-A1): model tiers, plumbing only, nothing
user-visible.** `core/models.py` is now the one place a chat model is constructed —
`agent_runtime._create_llm_with_tools` calls `models.chat_model` instead of building
`ChatOpenAI` itself, and chat's behaviour does not change: the session's chosen model is
still what runs. Two new options, `asi1_model_strong` and `asi1_strong_daily_cap`, exist for
Phase D and Phase F to spend against `models.take_strong_call`, which rations the strong tier
by a daily cap persisted in `DATA_DIR/model_budget.json` and **fails closed** — an unreadable
budget file reads as today already spent, never as an empty one. Nothing calls the strong
tier yet: an unset `asi1_model_strong` collapses it onto the cheap tier everywhere, which is
today's behaviour. `core/scheduler_llm.py` is untouched — the named, retiring exception.

## [2.14.15] - 2026-09-17

**This changes what households are shown: the schedule detector now picks its window from
`backed_windows(grid, couple_weekend=True)` instead of `schedules(grid)`.** On a device whose
weekdays are idle across slightly different hours, a household can now be asked about a
window the old picker could not name — a five-day 08:00-15:30 Mon-Fri instead of
07:30-17:00 on only the two weekdays whose edges happened to match. **The release is HELD**
until D-6's verification of `schedule:sensor.den_media_cabinet_power` on 2026-10-01 comes
back clean.

**D-2 was answered no.** The model proposer, `core/scheduler_llm.py`, is not being
admitted — not on every candidate, not on a narrowed trigger, not at all. WP4 and WP6 are
cancelled, D-1's opt-in option is never added, and nothing ever leaves a home unattended.
The reading is proposal §7.7's, over eight watched devices graded by replay exactly as the
review pass grades a live candidate: the model was preferred over both deterministic
proposers **0 times out of 8**. It lost to D1+ twice — on `desk_setup`, where D1+ reached
03:00-18:00 all week (333.0 device-hours a month), and on `server_rack`, where it dropped
the weekend on the words "server rack" and gave up 154 device-hours a month — and matched
everywhere else. Every window from every proposer graded with zero wrong firings.

The one behavioural change is that line in `find_candidate`. Both functions return the
same list shape, so the midnight skip, `measure` and the evidence blob read identically.
`schedules()` is unchanged and kept as the reference D1+ was measured against;
`core/scheduler_llm.py` and `core/schedule_cache.py` stay too, because retiring them is its
own package after this release, and doing it first would remove the comparison this change
rests on. `core/review.py`'s "No model, anywhere" stays literally true, now on purpose, and
§7.1's note that it would become transitively false is withdrawn.

Against the real record (`scripts/compare_proposers.py --dry-run`, no model called),
`sensor.den_media_cabinet_power` still gets 07:30-17:00 Mon-Fri, the window its applied
suggestion is soaking on. What D-6's soak now gates is shipping D1+, which is a fair
question: D1+ names more days than D1 on two of the eight devices, replay only catches a
window the record proves was in use, and every record it was measured on was planted.

**The harness's default D1 column now reports D1+.** `scripts/compare_proposers.py` takes
D1 from `find_candidate` rather than calling `schedules()`, so from this version its D1 and
D1+ columns agree on every device by construction — the dry run above shows exactly that.
Only `--stability` still calls `schedules()` by name. `scripts/` is unchanged here; a
comparison that needs the old D1 column has to be pointed at `schedules()` first.

## [2.14.14] - 2026-09-17

**The grid detector can now decline the idle Saturday.** `backed_windows` in
`core/detectors/schedule.py` gains `couple_weekend`, off by default. **Nothing calls
this code** — `backed_windows` itself is still uncalled by `find_candidate`, `scan` and
the review pass, and the argument is off even where it is — so no household-visible
behaviour changes, nothing new is written to any store, and no data leaves any home.

The rule exists for exactly one shape, and a real record has it. Over
`sensor.spare_room_desk_power_draw` the per-weekday idle runs are 08:00-16:30 Mon-Fri,
08:00-20:30 Saturday and nothing on Sunday. 2.14.13's reach ranks by hours x days and
proposes **08:00-16:30 Mon-Sat at 219 device-hours a month, against the Mon-Fri window's
182** — the highest score, and the answer proposal §3 stage D2 exists to refuse. The
weekday window's hours are set by a household leaving for work and coming back.
Saturday's idleness is not that, and next Saturday they may be home all day. With the
argument on, the same grid returns 08:00-16:30 Mon-Fri at 182.

The rule is a slack test, not "weekends are different". A window's hours are set by the
days that BOUND it — whose covering idle run stops exactly where the window stops. A
day's slack is how many cells its run reaches past the window's two edges; a weekend day
may be named only at zero slack, and only by a window that also names a working day. A
device idle 03:00-18:00 every day, whose other days' runs reach back to midnight, keeps
Saturday and Sunday because their runs end exactly on the window's edges. The allowance is
`WEEKEND_MAX_SLACK_SLOTS = 0` rather than a tuned number because every case in hand
separates at zero, and a non-zero value would be a number no record supplied.

It is weekend-only on purpose. `sensor.landing_media_hub_power_draw` is idle 08:00-17:00
Mon-Thu and 08:00-16:00 Friday, and the right answer, 08:00-16:00 Mon-Fri, leaves Mon-Thu
two cells of slack each; applied to working days the rule would throw that away and keep
Friday alone. What separates the weekend is a prior about human life, which the cells
cannot supply, and `window.WEEKENDS` is where it lives. A Saturday-and-Sunday window is
left alone for the matching reason: it has no weekday boundary to disagree with.

The cost is §7.5's bench row 5 stated honestly: a device idle 09:00-17:00 on weekdays and
08:00-18:00 at the weekend drops from all seven days at 240 to Mon-Fri at 171. The default
stays False so the harness can read the reach and the prior as two numbers; turning it on
is a decision for once that reading exists.

## [2.14.13] - 2026-09-17

**The grid detector can now reach the intersections it documents itself as unable to
name.** `core/detectors/schedule.py` gains `backed_windows(grid)` beside `schedules()`.
**Nothing calls it** — not `find_candidate`, not `scan`, not the review pass — so no
behaviour a household sees changes, nothing new is written to any store, and no data
leaves any home. It lands uncalled the way `window.py`, `grid_text` and
`window_is_backed` each did, so that the harness can measure it against the shipped
proposer before anything depends on it.

The gap it closes is measured rather than argued. `schedules()` merges a weekday's idle
runs into one window only when they are IDENTICAL, and its own docstring calls the
intersection of a Monday 07:00-17:00 and a Tuesday 07:30-17:00 "a window nothing in the
record picked out". On the commuter device planted in 2.14.12 — every weekday's edges
half an hour apart — that under-reach costs half the claim: `schedules()` can merge only
Monday with Thursday and proposes a two-day window worth **0.73 kWh/month against the
five-day intersection's 1.45**, which in the quantity it actually ranks by is 81
device-hours a month against 161. `backed_windows` returns 161 on that grid and 171 on
the near-miss shape where `schedules()` returns 154, matching row 1 and row 2 of
`docs/PROPOSAL-SCHEDULE-DETECTOR.md` §7.5's bench table exactly.

`schedules()`' second reason for declining — that choosing among intersections means
ranking a longer window on fewer days against a shorter one on more, and D1 "has no rule
for that which is not arbitrary" — is answered rather than overridden. `schedules()`
already ranks the runs it finds by `device_hours_per_month` and picks a first among them
on that rule alone; applying one rule to a larger candidate set is not a second rule.
What the intersections genuinely do need a prior for is §3 stage D2's own example, the
weekday pattern plus an idle Saturday where the highest score is the answer the proposal
says to refuse. That coupling is deliberately NOT in this release: it is its own work
package, so the reach and the prior can be read as two numbers instead of one.

Candidate ends are enumerated from run EDGES only — starts from the set of run starts,
ends from the set of run ends — never over all 48 x 48 pairs, and that is lossless
rather than a shortcut. For any fixed set of days the largest window all of them back is
[max of their run starts, min of their run ends], so every day set has its optimum at a
pair of edges, and any window that is not maximal for its own day set is dominated by one
that is. It also bounds the cost by the format instead of by the household: tens of
candidates per grid rather than thousands, which is what a daily pass over every watched
sensor can afford. Windows running through local midnight stay in the list exactly as
`schedules()` leaves them — `find_candidate` is the one place that refuses those, and a
second copy of the rule is a rule that drifts.

The output is `schedules()`' own shape, key for key and ranked the same way, so anything
downstream can read either. Every window it returns passes `window_is_backed` against the
grid it came from, asserted per row rather than on the winner — a wider candidate set is
only worth having if none of the extra members is a window the cells cannot evidence.

## [2.14.12] - 2026-09-17

**The device name is a second input to the model proposer, and D-4's cache now keys on
it.** Still nothing called in the shipped pass — WP4 is held behind the soak — so no
behaviour a household sees changes and no data leaves any home.

2.14.11's re-run left one thing unexplained. `sensor.lounge_tv_power_draw` and
`sensor.server_rack_power_draw` hold grids identical character for character, and in the
same run at `TEMPERATURE = 0` the first came back with all seven days and the second with
Mon-Fri. That was read on 2026-09-17 as D2 answering non-reproducibly. It is not. The
prompt carries two things, and only one of them is the grid: over the identical grid,
"Lounge TV Power" returns all seven days and "Server rack Power" returns Mon-Fri, three
times each. The model is reading "server rack" as a thing a household does not stop at the
weekend — a prior about human life, which is the whole of what stage D2 was admitted for,
and reproducible once the second input is held still.

The consequence is a defect in `core/schedule_cache.py`. D-4 keys an entry on the grid
fingerprint and the prompt version, and a rename moves neither — so a household renaming a
device would have gone on getting the answer to its old name for as long as the weekly
shape held, silently. `core/scheduler_llm.py` gains `name_print(name)`, a short digest of
`_clean_name`'s output, and `lookup` and `store` take it alongside the fingerprint. A
digest rather than the name itself because a cache is a store of shapes and ids and keeping
it that way costs one line; over the cleaned name rather than the raw one so that two names
the prompt cannot tell apart do not key differently. An entry written without the field is
dropped rather than defaulted — it cannot say which name it was decided against, and
guessing would be the exact silent staleness the field exists to prevent.

`scripts/` gains three schedule test devices whose idle edges differ weekday to weekday
(`schedule_devices.py`, `plant_schedule.py`, `simulate_schedule.py`) — the shapes
`schedules()` documents itself as unable to merge, which every existing fixture lacks. Over
the ragged one D2 proposes 08:00-15:30 Mon-Fri against D1's 07:30-17:00 Mon, Thu: 1.45
kWh/month against 0.73, 63 credited occurrences against 25, zero wrong firings either way.

## [2.14.11] - 2026-09-17

**The model proposer is shown clock times instead of the character grid, and starts
answering from the record rather than from a prior.** Nothing a user sees changes and
nothing calls this code in the shipped pass — WP4 is still held behind the soak
(`docs/PROPOSAL-SCHEDULE-DETECTOR.md` §7.3), so no data leaves any home as a result of
this release, the same as 2.14.8 through 2.14.10.

The first live comparison (WP5's harness, 2026-09-17, `asi1-mini`) had D2 agreeing with
D1 on **0 of 5** watched devices and refused or declined on 3 of them, answering
09:30-15:30 over grids with nothing in common including one that held no observations
at all. That read as a model reproducing the prompt's own worked example — "everybody
out between leaving for work and coming back" — as a fixed office window. The
measurement underneath it is narrower and worse: asked to do nothing but report the
longest run of digits out of `grid_text`'s Monday row, with no rules and no priors in
the question, the model answered slot 16 to slot 34 on a row whose run is slot 15 to
slot 33, and then rendered its own slot 34 as 18:00 rather than 17:30. **It cannot
index the grid.** A reader that cannot index the grid cannot check any rule stated in
the grid's terms either, and what it falls back on is what an idle window usually looks
like.

`core/detectors/schedule.py` gains `runs_text(grid)`: the same grid rendered as each
weekday's idle stretches in clock times, through `_runs_for` — so D1 and D2 now
disagree about which stretch to propose and never about which stretches exist. Nothing
legal is given up by the swap, and that is exact rather than approximate:
`window_is_backed` admits a window only where every cell it covers was idle on all of
its observations and was watched at least `MIN_OCCURRENCES` times, which is
`_runs_for`'s own predicate cell for cell, so every window that can ever validate is a
sub-range of a listed stretch on each of its days. The `MIN_RUN_SLOTS` filter keeps the
text bounded by FORMAT rather than by household — at most `SLOTS_PER_DAY //
MIN_RUN_SLOTS` stretches a day whatever the device does — which is the size objection
`GRID_TEXT_COUNT_CAP`'s comment raises against a run table, answered. A day with no
stretch says which of the two reasons applies, because "the device was in use" is
evidence and "nobody watched" is the absence of it.

`core/scheduler_llm.py`'s `prompt_for` is rewritten around that list and
`PROMPT_VERSION` moves to 2, which invalidates every WP3 cache entry cleanly. Three
things changed besides the input. The job is now named as the choice `schedules()`
documents itself as refusing to make — runs merge across weekdays only when they are
IDENTICAL, so a Monday 07:30-17:00 and a Tuesday 07:00-17:30 stay two windows and their
intersection is never proposed. The rules are stated against the stretch list rather
than against the grid's digits, which over a 90-day lookback were all 9 and stated a
rule no answer could fail and none could use. And the weekend rule became something
checkable: seven days with the SAME stretch is a fact about the device and the weekend
belongs, while a weekend whose stretch differs is the household happening to be out.
The old text's blanket "do not extend it to Saturday" cost a real window — over a grid
idle 00:00-18:00 on all seven days the model answered `{"window": null}` and gave up
what D1 had.

The same five devices, re-run: D2 now agrees with D1 on **3 of 5**, differs on one, and
declines on the one whose grid holds no observations. Nothing was refused. The device it
differs on is the better answer of the two — 03:00-18:00 Mon-Fri against D1's
00:00-18:00 Mon-Wed, Fri, which replay credits with 47 occurrences rather than 37 at
the same zero wrong firings, because starting after that device's 02:00 late-night uses
buys back the weekday D1 had to drop. Three consecutive runs over a fixed set of grids
returned identical answers, where the earlier prompt had returned a window for one grid
and `{"window": null}` for another grid identical to it in the same run.

`scripts/compare_proposers.py`'s `--dry-run` now prints `grid_text` and the prompt, as
its own docstring always said it did — it was printing the tri-state fingerprint, which
is a different thing and easy to misread as the grid. That is also what keeps the
character rendering exercised now that the prompt no longer carries it: a person reading
a disagreement wants the cells.

## [2.14.10] - 2026-09-16

**`core/scheduler_llm.py` imports on stdlib alone.** `from langchain_openai import
ChatOpenAI` moves from module scope into `_client()`, the one place a real client is
ever built. Nothing about the shipped behaviour changes — `propose` still refuses
rather than repairs, still makes exactly one call, and `client=None` still means "no
key configured" — this is purely about what importing the module now requires.

The reason is `scripts/compare_proposers.py` (WP5 of step 3,
`docs/PROPOSAL-SCHEDULE-DETECTOR.md` §7.3): the harness that runs D1 and D2 over the
same real record and grades both, which is the only way to find out whether the
free-form model proposer (decision 3) is any good, since its answers are not
reproducible run to run. It is not shipped, runs inside the add-on container like
its neighbours in `scripts/`, and calls its own stdlib `urllib` client rather than
langchain's, the same reason every other script there is stdlib only — which only
works because `propose(grid, client=...)` now accepts an injected client without
langchain importable at all, the property this amendment establishes and
`tests/test_scheduler_llm.py` now pins. No data leaves any home as a result of this
release, for the same reason as 2.14.8 and 2.14.9: the option and the wiring that
would call any of this in the shipped pass are WP4, held behind a production soak.

## [2.14.9] - 2026-09-16

**Groundwork: the cache that makes an unchanged grid free.** Nothing a user
sees changes in this release and nothing calls the new code — WP3 of step 3
(`docs/PROPOSAL-SCHEDULE-DETECTOR.md` §7.3), landing the same way WP1 and WP2
did. No data leaves any home as a result of this release, for the same reason
as 2.14.8: the option and the wiring that would call any of this are WP4, held
behind a production soak.

`core/detectors/schedule.py` gains `grid_fingerprint(grid)`: the grid's
336-cell shape reduced to one tri-state mark per cell — never observed, idle
on every observation, or active at least once — rather than a hash over its
raw observation counts. That is decision 1's trap in this module's own
costume: `scan`'s window slides one day every morning, so nearly every cell's
`observed` count moves by one daily while the shape it describes does not, and
a fingerprint over the counts would miss every single day while looking like
it worked. The tri-state is exactly the distinction `window_is_backed` already
tests and `grid_text` already renders, so two grids that fingerprint alike are
proven, not merely expected, to answer `window_is_backed` identically for
every window — asserted directly in `tests/test_schedule_detector.py`.

`core/schedule_cache.py` is the new store, in `core/watchlist.py`'s own shape:
one JSON file in `settings.DATA_DIR`, a tmp-file-plus-`os.replace` write, and
every failure logged and absorbed rather than raised. It is keyed per entity
on the fingerprint AND `core/scheduler_llm.PROMPT_VERSION` (now exposed for
exactly this) — a cached answer is only true of the prompt that produced it,
and a revised prompt must not go on serving yesterday's answer. A cached
refusal (`window: null`) is stored as deliberately as an accepted window: D-2
consults the model on every run-producing entity, and an uncached refusal
would be re-asked, identically, every single morning the grid stays the same
shape. `prune(keep)` takes the entities to retain as an argument rather than
importing `core/watchlist.py` itself, so the store never knows more than a
store should. No eviction by age: an entry whose fingerprint still matches is
still a true answer about a grid that has not moved.

## [2.14.8] - 2026-09-16

**Groundwork: the model proposer, and the validator that is the point of it.**
Nothing a user sees changes in this release and nothing calls the new code —
WP2 of step 3 (`docs/PROPOSAL-SCHEDULE-DETECTOR.md` §7.3), landing the same way
2.14.7's `grid_text` and 2.14.2's weekly automation shape each did. No data
leaves any home as a result of this release: §7.2's decision 1 makes that
egress an opt-in add-on option, and both the option and the wiring are WP4,
held behind a production soak.

`core/scheduler_llm.py` is stage D2 — hand the 336-cell grid to ASI1 and ask
where this device's idle window is. It is a **leaf module**: it does not import
`core/agent_runtime.py`, `core/review.py` or `core/suggestions.py` (decision 5),
and builds its own SYNCHRONOUS client, because `tasks/review.py` hands the
daily pass to an executor thread where there is no event loop for `ainvoke` to
run on. Its own timeout at 30 s rather than `settings.LLM_TIMEOUT`'s 60: that
number is sized for a person watching a chat reply, and this one is paid once
per candidate device by a thread with the morning's verifications and rollbacks
queued behind it. Temperature pinned to zero, which is decision 3's last
paragraph — WP5 has to diff this proposer against D1, and output that also
varies run to run is not something a harness can read.

**The validator refuses; it never repairs.** Decision 3 lets the model answer
free-form rather than pick from a list the detector enumerated, and that makes
this the safety boundary outright rather than a second opinion. §7.1 is why it
cannot be left to the grader: replay only catches a window the record PROVES
was in use, so a window laid across half-hours nobody watched passes grading
cleanly — `MAX_WRONG_NIGHTS = 0` is a floor on proven wrongness, not on
unexamined wrongness. Six checks, and a window failing any of them is discarded
whole. Nothing is clamped to the nearest legal window, trimmed to the cells
that pass, or stripped of the one weekday that failed.

- **the three about the grid** — every cell inside the window idle on every
  observation of it, every one of them clearing `MIN_OCCURRENCES` in its own
  right, and the named days observed at all — are
  `schedule.window_is_backed(grid, spec)`, pure and in the detector, because
  they are questions about the detector's own data and have to be testable
  against a literal grid with no client anywhere near them. The same split
  `core/observer.py` and `core/observation.py` already have
- **the three about the window's shape** are `scheduler_llm.refusal`, and it
  asks them in the order they depend on each other rather than in the order
  §7.2 lists them: no midnight span first, because a window running through
  midnight has no single weekday to index cells under; then the half-hour
  lattice, because a length in cells means nothing until both ends land on a
  cell boundary; then `MIN_RUN_SLOTS` and `MAX_RUN_SLOTS`. Each reason names
  the fault alone — `propose` prints the window's label once in front of
  whichever came back, so no reason carries a second copy of it
- **one call, and no nudge.** A refusal is logged with its reason and returned
  as None. It is never sent back with the reason attached and no corrective
  message is appended and the call repeated — a proposer that quietly
  self-corrects until something passes turns a bad prompt into a stream of
  plausible windows nobody reviewed, and the log line is the only signal left
  that the prompt is wrong

**Parsing tolerance is not repair**, and the two are kept apart deliberately.
The reply may arrive fenced or wrapped in prose and the object inside it is
still found — a fence needs no stripping step of its own, because a fenced
object is still a balanced run of braces. What is found is not then adjusted:
times go to `window.spec` as written, and weekday numbers must already be
integers rather than be cast from whatever turned up, since `int(0.5)` is a
repair wearing a cast's clothes.

**The prompt is code**, in this repo's sense, so it states the constraints
rather than leaving them to be caught afterwards — this module may not correct
anything, so a rule the model was never told is a wasted call and a day with no
suggestion. It states them in the grid's own terms ("a digit of at least 8"),
names §3 stage D2's target pattern and its counter-example (the weekday office
window, and declining to extend it to a Saturday that merely looks idle), and
offers `{"window": null}` as an explicit way to decline, because a model with
no legal answer and no way to say so invents one.

**Every failure is a None** — timeout, transport error, unreadable answer,
validator refusal, no API key configured. §11's rule is that the agent going
quiet is acceptable; an exception escaping here would be raised inside
`review.run_once` and would take that morning's other suggestions,
verifications and rollbacks with it. An unconfigured home is the ordinary case
and never builds a client at all.

What leaves the house, stated in the module because
`tests/test_disclosure.py`'s "no tool calls `json.dumps`" grep does not reach
`core/`: the rendered grid and the device's name, and nothing else. The grid is
an aggregate — a state and a single-digit count per half-hour over ninety days,
no rows, no timestamps, no entity_id, no watts — which is §10.1's own reason
for permitting it, and the name is `core/disclosure.py`'s deliberate
`NAME_FIELDS` hole. `Grid.dates` is not sent: nothing about the grid's shape
needs it, and it is the one part that could place a household on a calendar.

## [2.14.7] - 2026-09-16

**Groundwork: the grid, rendered for a model that does not exist yet.** Nothing
a user sees changes in this release and nothing calls the new code — WP1 of
step 3's model proposer (`docs/PROPOSAL-SCHEDULE-DETECTOR.md` §7.3), the same
way `core/window.py` and `core/detectors/levels.py` each shipped ahead of
their callers.

`core/detectors/schedule.py` gains `grid_text(grid)`, a pure rendering of
stage C's 336-cell weekly grid as ASCII text: one character per cell, carrying
both its state (idle on every observation, observed but sometimes active,
never observed) and a bucketed observation count, so a cell seen twice cannot
read the same as one seen thirteen times. Capping the count at
`GRID_TEXT_COUNT_CAP` (9) rather than carrying it in full is what keeps the
alphabet to one character per cell without losing anything a later reader
needs — nothing downstream asks a sharper question than whether a cell
cleared `MIN_OCCURRENCES`. At the full 336 cells this comes out under 700
bytes, inside §3's 1-2 KB estimate even with a count now attached to every
cell. The next work package, `core/scheduler_llm.py`, is what reads this text
and is not part of this release.

## [2.14.6] - 2026-09-15

**The rendering is no longer night-shaped, and 2.14.5's release gate is
lifted.** That release wired the schedule detector into the daily pass and
said not to put it in front of a household, because `core/artefacts.py` still
read the night's field names directly: a card about a 07:00-17:00 weekday
window said the device "draws power overnight without being used". §9's rules
are about what a rendering may CLAIM, and the wrong reason is the one thing it
forbids outright. A dozen sentences read those names — nine more than 2.14.5's
note listed, which called two of them correct and never looked at artefact 4 —
and every one of them now asks the module that owns the fact.

**Where the names come from, which is decision 7 with one more reader.** The
detector that wrote a blob declares what it calls the counts in it
(`EVIDENCE_FIELDS`, now the whole vocabulary rather than the two fields the
fingerprint took), the grading's names are core/replay.py's and are asked of
`fields_for`, and the renderer asks whichever owns the fact. There is still no
per-kind table in the renderer and there must not be one: adding a detector is
still adding it to one tuple. That tuple moved to
`core/detectors/__init__.py` — not a change of mind, a change of address. A
module that has to READ a blob needs to know which detector wrote it, and
core/artefacts.py cannot ask the pass, because the chain from the pass reaches
core/verify.py and that imports the renderer.

The rule that goes with it, in a renderer's terms: **a field this cannot name
means the clause is OMITTED.** Never printed with a blank in it, never with a
zero standing in for a count nobody has, and never with the night's word over
another shape's number. A suggestion whose kind this build has no detector for
now says less rather than saying something confident and wrong — and each
clause is guarded on its OWN field rather than the block on the vocabulary, so
the guard threshold, one number under one name in both shapes, survives a kind
nothing can name.

**The user-facing noun is OCCURRENCE, and that is a decision rather than a
lookup.** "Day" is what a person would say and is the one word this may not
use: the same card already prints "Measured over 90 days" and "90 of those 90
days had readable data" about the LOOKBACK, and a second meaning of the word
one line below turns "Days left out: 4" into a sentence about neither.
"Weekday" reads best of all and is false — the detector finds weekend runs
too, and proposes them. What is left is the grading's own word, and taking it
from `replay.fields_for` rather than inventing a warmer synonym is the point:
a household reading "13 occasions" beside a log line saying
`occurrences_saved: 13` has to work out that they are one count. Two
vocabularies for one unit is exactly the drift "rendered once" is written
against, so there is no noun declared in the renderer and no second one
anywhere.

What changed, sentence by sentence:

- **`why`, the actual bug.** The window and the counts now come off the blob
  under the raising detector's own names, so the card says the printer never
  dropped below 12 W between 07:00-17:00 local Mon-Fri on 63 of the 63
  occurrences examined. The fallback names no window and no hours, because it
  is reached exactly when there are none to name
- **`headline` and `_measured_on`, which 2.14.5 called correct and were not.**
  A schedule line degraded to a bare kWh with no observation clause and no
  window, and without a kWh it read "standby draw overnight". Both counts are
  now read in the vocabulary of the side that produced the figure — the
  detector's through its declaration, the grading's through `fields_for` — and
  `_measured_on` is still the only place `estimate.basis` is read. The window
  is what carries a digest holding two claims about one television, so a line
  that named neither would leave "yes" meaning either
- **`evidence_lines`.** "The nightly minimum" is now "the lowest reading on
  each occurrence" — the adjective had no form the other shape could borrow,
  and spelling the statistic out is what lets a reader check it against the
  figure beside it. The coverage line stopped dropping out: the schedule blob
  states `covered_days` under the name every shape uses, beside the `coverage`
  sub-blob that keeps decision 4's requested-versus-observed pair, one
  expression stated twice and never two counts. Both detectors count calendar
  DATES, and a window opening mid-afternoon touches one more than it spans, so
  the figure is capped at the window rather than printed as "91 of those 90" —
  and a stored figure nothing can read as a number drops the clause instead,
  since "0 of those 30 days had readable data" is a measurement saying the
  agent saw nothing, invented out of a field nobody could parse
- **`replay_lines`.** It kept only the guard line; it now reports the grading's
  counts under its own heading again, in the shape's own noun, and "cut power
  to a device that was used again before morning" is "before the power came
  back" — this rule's power comes back at five in the afternoon
- **`verification_windows`, `verification_evidence` and `_window_phrase`,
  which the release entry did not name.** The window phrase counted DAYS and
  called them nights; for a night the two are one number, and for a Mon-Fri
  rule they are ten occurrences over a fortnight. It now takes the count off
  the measurement in that measurement's own vocabulary, falling back to the
  span in days for a week nothing could be read in. "The week to X" is gone —
  2.14.4 made the after-length per suggestion, so seven days is an assumption
  now rather than a constant, and a fortnight reported as a week is a
  comparison window nobody measured. The same argument gives "the 14 days
  before the change was applied" where a night still reads "the week", and
  "in those 14 days" where a night still reads "in that week"
- **two more the release entry did not name.** `estimate_sentence` lost its
  whole breakdown on a schedule row — the four figures are named
  `hours_per_occurrence` and `occurrences_per_month` there — leaving the bare
  kWh §9 is written against. And `label_for` stripped the night's alias suffix
  by hand, so a schedule card called the device "Office printer off on a
  schedule"; the suffixes are core/builder.py's and `builder.label_of` knows
  all of them. `_VERDICTS` and the measured result said "overnight draw" about
  both windows and now say "in that window", which the sentence above them has
  already named

**Every assertion in `tests/test_artefacts.py` written before there was a
second shape passes untouched, which is the check this release is measured
by** — including "01:00-05:00 local", "21 of 30 nights", "Nights left out",
"the 7 nights to 2026-09-15" and "the week before the change was applied". The
22 new ones, 54 assertions, are driven off blobs the real pipeline produces
rather than literals: an office printer's ninety days of heartbeats, the detector finding
the window in them, core/builder.py writing the automation, core/replay.py
grading that config against the same rows, and core/verify.py measuring a
fortnight of it afterwards. A blob written by hand there would agree with
whatever the test file believed on the day, and what is being tested is that
the renderer reads what the pipeline actually wrote.

Four edges are worth knowing about, because each is a sentence that would
otherwise have read as machine-made or as a claim nobody measured. A count
between one and two is a PROJECTION rather than a tally — `nights_per_month`
on a device present for a fortnight of the window is 1.5 — so the plural turns
on exactly one rather than on truncating to one, or the card reads "1.5 night
a month". A suggestion row with NO kind is read as a night, which is
`verify.shape_of`'s own reading of it and has to be the same one: that module
will measure such a row, and a report of the week with its evidence omitted
would be a saving claimed with no observation behind it. A kind this build has
no detector for still omits, because those are two different cases and
guessing which shape an unknown one resembles is what the refusal is for. And
the digest's counts are guarded on the vocabulary like every sibling clause:
`_measured_on`'s replay branch answers off core/replay.py's table while the
noun beside it comes from the detector registry, and a kind in one and not the
other was a KeyError that took the whole listing rather than one line's
clause.

`builder.label_of` matches the suffix before trimming whitespace, because
every suffix opens with a space: an alias that is exactly one of them is a
device with no name, and trimming first would hand back "off overnight" as
what to call the thing.

One thing outside the renderer moved with it: `tools/tool_suggestions.py` told
the model to relay the digest "with their numbers and the nights they were
measured on", which is a prompt for one shape in front of a digest that now
holds both. The prompt is code, and it now says the line names which claim it
is.

`panel/dist` needs no rebuild: no panel source changed, and none should. The
panel is handed finished sentences for the same reason it always was — a rule
implemented in Python for ASI1 and again in JavaScript for the card is a rule
that drifts — and `grep -ri night panel/src/` is still empty.

## [2.14.5] - 2026-09-15

**The schedule detector is reachable.** `core/review.py` ran `standby.scan`
and nothing else, which is why 2.14.2, 2.14.3 and 2.14.4 could each say
"nothing a user sees changes" — the builder, the replay engine and the
verification all dispatched on `kind` and no `schedule` candidate ever reached
them. The daily pass now runs both detectors, so a home CAN be asked about a
daytime window. **Read the §9 note at the end of this entry before releasing
this to anyone**: the rendering has not been generalised yet and one sentence
on the card is wrong.

Four things in the pass were night-shaped, and a fifth nobody had checked.

- **where the per-kind answer lives, which is decision 7 and the reason the
  other four are one-liners.** `core/replay.py` holds a table (how to GRADE a
  config of this kind) and `core/verify.py` holds one (how to MEASURE a week
  of it). A third here would be a third place for a detector added tomorrow to
  be half-registered, so there is none: the pass holds `DETECTORS`, a tuple of
  MODULES, and asks each of them its kind, how far back it looks, the scan
  itself, and what the blob IT wrote calls the two fields the fingerprint
  reads. Every one of those is a fact about the detector and was already
  stated in the detector's own module — replay's table reads
  `standby.LOOKBACK_DAYS` off it by name for exactly that reason. The fifth
  thing, what the GRADING calls its counts, is replay's vocabulary and is
  asked of replay through the new `replay.fields_for` rather than restated.
  Adding a detector is adding it to one tuple
- **and the rule that goes with it: a blob whose field this cannot read is
  REFUSED, never read as zero.** That is what 2.14.3 fixed the latent half of
  in `disqualified`, and it applies to all four. `evidence_fingerprint`
  answers None for a kind with no detector rather than a shorter tuple,
  because two tuples missing the same fields compare EQUAL and "the evidence
  has not moved" is the one answer that must never be reached by failing to
  look. `disqualified` refuses a kind replay cannot name the fields of.
  `rank` sorts a grading it cannot read after every one it can, rather than
  reading it as zero and landing it among the genuinely thin. And an unknown
  kind's evidence is aged against the LONGEST lookback any detector has
- **the fingerprint (decision 1), which had collapsed.** It read
  `nights_per_month` and `night_window`; a schedule blob carries
  `occurrences_per_month` and `schedule_window`, so both came back None and ""
  and the tuple fell to (device, switch, watts). Two claims about one
  television — the night plateau and the daytime one — fingerprinted
  identically, and the window, the one field that tells them apart, was the
  half that vanished. Both are now read through the raising detector's
  `EVIDENCE_FIELDS`, declared beside the code that writes the blob, and the
  kind leads the tuple so the distinction survives even two windows measured
  at the same level and the same frequency. `FINGERPRINT_NIGHTS_STEP` is
  `FINGERPRINT_PER_MONTH_STEP` and keeps its value: it is one quantity on one
  scale, and the slack it leaves a ninety-day window errs towards holding a
  replay, which `EVIDENCE_HOLD_DAYS` already bounds at a week
- **the rank tiebreak, which is now across detectors rather than within one.**
  The estimate's `kwh_per_month` is the one figure both shapes write under one
  name — a night's kilowatt hours and a schedule's are the same quantity, and
  that is what makes a single ordering honest. How many firings were credited
  is not: `nights_saved` against `occurrences_saved`, read through
  `replay.fields_for`, or sixty credited occurrences would have sorted behind
  eleven credited nights
- **`PROPOSAL_MAX_EVIDENCE_AGE_DAYS` was one constant for two lookbacks.** It
  was `standby.LOOKBACK_DAYS`, and a schedule's window is ninety days. It is
  now `evidence_max_age_days(kind)` — the raising detector's own, since the
  timeout is stated as the point at which that detector's window stops
  overlapping today. One number for both would either withdraw a schedule
  proposal two months before its evidence stopped describing now, or leave a
  night one standing for three
- **`run_once(lookback_days=...)` is gone rather than generalised.** A single
  parameter for two windows is the bug `core/replay.py`'s docstring names:
  ninety days of claim graded over thirty days of record, a third of the
  occurrences credited, and the result compared against a floor set for the
  other window. There is no replacement parameter. Each scan is called with
  its own default and `replay.run` is left to resolve the same figure from the
  same detector, so the window the rows came from and the window they are
  graded over cannot disagree

**And the fifth thing: two detectors can both fire on one device, and nothing
stopped them.** Decision 5 claimed one open proposal per entity was structural
— "the store's partial unique index makes one live row per key, and the key is
derived from the entity". The key is `kind:entity`, so `standby:sensor.tv` and
`schedule:sensor.tv` are two rows, two replays and two questions; on a
television with a night plateau and a daytime one that is the ordinary case,
not a corner of it. `claimed_entities` restores the invariant, and it is
stated as three states rather than as a count because the reason is not
volume:

- §9 answers a suggestion in one word, and two questions about one television
  a line apart in a three-line digest are two things "yes" could mean
- they would be two automations cutting one switch, and §7.2 measures each by
  subtracting two weeks of the SAME sensor — a second rule applied inside the
  first one's week moves the after, and the report would credit the saving to
  whichever rule happened to be the one being measured
- the detector that has not been applied yet keeps reading a record the
  applied one is now changing. That much is honest, since the cut hours read
  `below_floor` and are excluded rather than counted twice, but only while one
  claim at a time is live

So a device is spoken for while a question about it is open, while a rule on
it is having its week measured, or while a measured result is waiting to be
kept or rolled back. Not by the agent's own bookkeeping: a candidate or a
validated row costs nothing and waits to be ranked. Not by a dismissal, which
is a cooldown on ONE key — a "no" to the night question is not a "no" to the
daytime one. And `kept` and `rolled_back` are ends, so the device is free
again and the next claim about it is measured over a record that already
includes whatever was kept.

**The cost is two reads of every watched sensor a day, thirty days and ninety,
and that is deliberate rather than tolerated.** Each detector's `scan` is the
whole of its own I/O and derives its own window, so a shared read would have
to be the longer one sliced by the pass — which is the pass choosing how far
back a detector looks, the thing decision 7 exists to stop. The record stays
READ ONLY with no transaction held, which `tests/test_review_pass.py` still
proves by running the whole pass while another connection holds the write
lock. Memory does not double: each scan drops the series of every entity that
produced no candidate where it read it, and the two run one after the other.
Home Assistant traffic does not double either — one registry round trip still
serves both detectors and the verification.

**§9's rendering has NOT been generalised, and one sentence on a schedule card
is wrong.** `core/artefacts.py` is untouched in this release on purpose — it
wants a real stored blob to build against, and this is the release that
produces one. Today a schedule proposal renders its headline, its estimate,
its digest line and its full readback correctly, including the sentence about
the daytime window being the riskier one. What is wrong is that `why` says the
device "draws power overnight without being used" about a 07:00-17:00 weekday
window, `evidence_lines` calls the figure "the nightly minimum" and drops the
line carrying the counts and the exclusions, and `replay_lines` loses the
grading's counts and keeps only the guard line. All three read the night's
field names directly. The approval itself is unaffected — the gate's YAML diff
is built from the config — but a card that states the wrong reason is exactly
what §9 forbids, so **do not put this in front of a household until the
artefacts package lands.**

Everything a standby suggestion does is unchanged, which is the check this
release is measured by: every assertion in `tests/test_review_pass.py` written
before the second detector existed passes untouched, and 33 new ones cover the
second, the refusals, and the two claims about one television end to end — the
record planted for real, both detectors reading it, and the questions counted.
`panel/dist` needs no rebuild: no panel source changed.

## [2.14.4] - 2026-09-15

**Groundwork: measuring a week of a rule whose window names its weekdays.**
Nothing a user sees changes in this release, and **no verification promise
already made to anyone moves**. Nothing builds a schedule candidate yet —
`core/review.py` still calls `standby.scan` and only that — so no home reaches
the code below; what lands is §7.2's verification for the config shape 2.14.2
writes and 2.14.3 grades.

`core/verify.py` had the night folded into it in seven places. Each of them is
now a parameter taken off the suggestion rather than a constant read from the
detector, and the night's own behaviour is unchanged down to the blob it
writes.

- **how long a week is, which is the one promise in this phase that could
  move.** `AFTER_DAYS = 7` gives a Mon-Fri rule about five occurrences to be
  measured on, which `docs/PROPOSAL-SCHEDULE-DETECTOR.md` §4 calls not
  defensible. So what is held constant is OCCURRENCES rather than days —
  seven of them — and `verify.days_for` turns that into however many whole
  weeks offer that many: a fortnight for Mon-Fri, four weeks for Sat-Sun, and
  seven days for a rule that happens every day. Whole weeks, because ten days
  over a Mon-Fri rule holds two Mondays and one Friday and a claim about a
  household's week should not weigh its Mondays twice. The proposal's flat 14
  fixes the weekday case and makes the weekend one worse than the five it
  already refused
- **and it is per suggestion, not a global bump, which is why no existing
  promise moves.** `review.settle` reads that length ONCE at apply time — when
  the kind is already known — and uses it for both the baseline's window and
  the week to wait, so the two ends of one comparison cannot disagree. Every
  suggestion already on disk is a standby one, gets seven, and is compared
  against a seven-day after exactly as it was. A reader of this release asking
  "does my pending verification now take a fortnight" gets no
- **the tail case is stated rather than capped.** A rule that happens on one
  weekday waits seven weeks. That is the honest answer — a rule that acts once
  a week has to be watched for seven weeks to be seen acting seven times, and
  reporting sooner on fewer occurrences is the thing §7.2 objects to — and
  nobody is made to wait for it, since §11 offers the rollback without waiting
  for the weekly report
- **"compare like periods" loses an assumption and gains a check.**
  `_like_periods` could say both windows were `AFTER_DAYS` long by
  construction. With a length derived per suggestion that is no longer true,
  and two weeks of different lengths compared as though they were alike is
  exactly the failure it exists to catch, so the length is now checked beside
  the hours. The hours are still compared as the LABEL — what the baseline
  stored and what §9 printed — and `window.label` names the weekdays of a
  window that has them, so `07:00-17:00 local Mon-Fri` is distinguishable from
  the same hours every day for nothing. `01:00-05:00 local` does not move by a
  character and is still asserted as a literal in two test files
- **the level a schedule week is judged against comes from what was stored,
  never from the week itself.** `schedule.intervals` needs the device's own
  active floor, and re-deriving it by histogramming the after week would
  histogram a device the automation now switches OFF for most of that window:
  a different threshold, so a different measurement, and a comparison between
  two things rather than two measurements of one thing. It is taken in the
  order `builder.readback` takes its three sources for the ceiling — by how
  well each knows THIS config — so the replay's `guard.below_w` first, since
  that is the threshold the automation that actually ran carries and the one
  the grading used, then the detector's own `active_floor` through
  `builder._floor_from`. There is no constant behind the two, so a week whose
  level cannot be recovered is a blob that says so rather than a guess
- **the window is the suggestion's own, and is validated the way it was
  built.** `standby.NIGHT` may be read off the detector because the night is a
  declaration about the house; a schedule's window is a finding about one
  device and is rebuilt from `evidence["schedule"]` through
  `builder._spec_from` — the same function that wrote the automation, so the
  hours measured and the hours the rule runs over cannot come apart. A
  truncated blob is refused rather than defaulted to all seven days, which
  would measure the two days the evidence never looked at
- **the field names, and the five functions that read them.** `readable`,
  `min_read`, `kwh_interval`, `floor_kwh` and `_unreadable_reason` read one
  vocabulary through one table, the way `replay._DETECTOR_FIELDS` already
  does. A schedule week says `occurrences_observed`, `occurrences_examined`,
  `occurrences`, `occurrences_per_month` and `hours_per_occurrence`; a standby
  week does not move, because those names are on disk in activity.db on live
  installs. Each measurement now records its own `kind`, and a blob without
  one is a night — every blob written before there was a second shape was
- **the trap that decides whether a success is legible is unchanged and is why
  the table exists.** The floor is on OBSERVED occurrences, never on
  qualifying ones, because a working automation produces a week in which
  nothing qualifies. `schedule.measure` counts the occurrences it MEASURED —
  the same number, for a run the detector proposes — so the observed count is
  taken from `schedule.OBSERVED` where the week is walked, or every successful
  verification would report itself unmeasurable
- **`MIN_NIGHTS_READ` stays derived.** A majority of what the week actually
  offered: four of seven for a night, six of ten for a weekday fortnight.
  Holding a fortnight to the night's four would accept a week over half of
  which was missing
- **`NOISE_FLOOR_WATTS` keeps its value and its second argument now reads for
  both detectors.** The record's own resolution is a fact about the record.
  The residual a successful week can hide is bounded by
  `standby.STANDBY_MIN_WATTS` for a night and by `schedule.IDLE_MIN_WATTS` for
  a window, and those are the same `2 x observer.NUMERIC_MIN_ABS_CHANGE` by
  construction rather than by coincidence — so the number is not recomputed
  per kind. Its size on the kWh axis is per measurement and generalises
  unchanged: a ten-hour window seen on 21 weekday occurrences gets a bigger
  floor than a four-hour one seen on 30 nights, because it is a bigger claim

**`verify._same_rule` now compares the weekdays**, which 2.14.3's entry named
as this release's line. A rule edited from Mon-Fri to every day cuts power on
two days nothing ever measured, and until now it verified as though it were
the rule that was graded. Compared as `days` rather than as `restricted`:
`plan_for` answers EVERY_DAY when nothing restricts the cut, so one expression
covers both shapes and the night's behaviour is unchanged by construction — a
night rule that gained a five-day restriction reads as an edit, and one whose
seven-day restriction was written out longhand does not, because it fires on
exactly the same dates.

The four decisions `core/verify.py` already owned are unchanged in meaning. A
week that could not be read is still re-measured over another like period, up
to `MAX_ATTEMPTS`, and then reported as unmeasured and never as "saved
nothing"; the rule is still checked before any number is claimed; keep and
roll back are still the user's answer through the one exit both surfaces
reach; and a verification is still not urgent. §7.2 still allows a range or
"too small to separate from noise" and no third form.

Everything a night suggestion does is unchanged, which is the check this
release is measured by. `tests/test_verify.py` asserts it directly: every
assertion written before the second window existed passes untouched, and 50
new ones cover the second. `panel/dist` needs no rebuild — no panel source
changed and the version is not embedded there.

## [2.14.3] - 2026-09-15

**Groundwork: grading a rule whose window names its weekdays.** Nothing a user
sees changes in this release. Nothing builds a schedule candidate yet —
`core/review.py` still calls `standby.scan` and only that — so no home reaches
the code below; what lands is stage E of
`docs/PROPOSAL-SCHEDULE-DETECTOR.md` for the config shape 2.14.2 started
writing. Until now `replay.plan_for` refused that config outright.

`core/replay.py` now grades two shapes, and every number that differs between
them comes from the detector that raised the candidate rather than from a
constant chosen in the grader.

- **the kind is an argument, and a required one.** It is not sniffed out of the
  config and it is not imported: `core/builder.py` knows the shapes and pulls
  `_preflight_automation` in from the Home Assistant tools, and this module
  having no I/O half at all is a property it has rather than a coincidence it
  enjoys. `review.py` passes `built["kind"]` — the kind of the config that was
  actually built. It is then CHECKED against that config the way `entity_id` is
  checked against the guard: a `standby` kind over a config restricting its cut
  to named weekdays, or a `schedule` kind over one that does not, is a refusal
  rather than a best guess. That is WP5's own argument from the other end — a
  night rule somebody hand-edited a `condition: time` into would otherwise be
  graded over five days rather than the seven it fires on
- **a `condition: time` naming weekdays is the whole of what the shape adds.**
  One on the cutting branch, never two, and never one on the restoring branch —
  a weekday restriction there is how a device cut on Friday stays cut until
  Monday, which is the same refusal a guard there has always met. A `time`
  condition carrying `after:` or `before:` is refused by the mechanism every
  unknown key is refused by: it would move the instants the grading is placed
  at away from the two triggers the whole span is derived from. A refusal
  discards the candidate; it never shows it with a weaker number
- **a Saturday under a Mon-Fri rule is not an occurrence the rule declined —
  it is not an occurrence.** It never enters the denominator the monthly figure
  is projected over, and the weekday filter runs BEFORE the trigger instants
  are placed. A date the clock ate the trigger instant on is the other case and
  stays the other case: that one IS an occurrence and IS counted as examined,
  under `OUTCOME_NO_TRIGGER`. Collapsing the two is how a weekday rule comes to
  report "credited on 10 of 90" for a rule that only ever had 64 chances
- **the level that means "in use" is this device's own**, for a schedule:
  `guard.below_w`, the threshold the card itself quotes, read back off the plan
  rather than fetched from a second constant. A device drawing 25 W inside its
  idle window is proof the rule would have been wrong even though 25 W is under
  the global standby ceiling a night rule is judged against
- **what credits an occurrence is the detector's own check, called rather than
  reimplemented.** A night must be a PLATEAU — `standby.assess_night`. A
  schedule window must have stayed under this device's active floor, which is a
  different claim and the one its card makes, so the answer is `schedule.py`'s
  own interval series and cell verdict asked about the occurrence's span. A
  span the grader credited and the detector excluded would be two components
  describing one window differently inside one suggestion
- **the evidence floor and the lookback are the detector's too** — eight
  occurrences over ninety days for a schedule where a night needs ten over
  thirty. One lookback applied to both would grade a ninety-day claim across
  thirty days of record, credit a third of the occurrences and then compare
  that count against a floor set for the other window
- **a Mon-Fri window is projected over weekdays**, not over a month: about 21
  occurrences rather than 30, which is `schedule.occurrences_per_month`'s own
  weekday fraction. For a rule naming all seven days the fraction is 1 and the
  arithmetic is what it always was

**The field names.** A schedule replay blob says `occurrences_examined`,
`occurrences_fired`, `occurrences_guard_failed`, `occurrences_wrong`,
`occurrences_saved`, `occurrences_per_month`, `hours_per_occurrence` and
`occurrences`, mirroring what 2.14.1 did for the detector's blob and for its
reason: "nights" is a lie about a window that happens at 11:00 on a Tuesday,
and a schedule blob's names have never existed anywhere. A standby blob does
not move by a character — its names are on disk on live installs, and
`review.disqualified` reads `nights_wrong` with an `or 0` behind it, so a
blanket rename would have read absent as zero and passed every candidate the
record says would have cut power to a device in use. `core/artefacts.py` (WP7)
and `core/review.py` (WP8) still read only the night names, which is why
nothing produces a schedule candidate yet; `verify._same_rule` still does not
compare the weekdays the plan now carries, and that is WP6's line to add.

Everything a night candidate does is unchanged, down to the blob it stores and
the constants it names. That is the check this release is measured by, and
`tests/test_replay.py` asserts it directly: every assertion written before the
second shape existed passes untouched.

## [2.14.2] - 2026-09-12

**Groundwork: the second automation shape.** Nothing a user sees changes in
this release. Nothing builds a schedule candidate yet — `core/review.py` still
calls `standby.scan` and only that — so no home can reach the code below; what
lands is the shape a schedule candidate will be written as when something does.

`core/builder.py` now dispatches on the candidate's `kind` and carries two
shapes rather than one. The night shape is unchanged, down to the ceiling it
writes.

- **the weekly shape** — the same one automation with two time triggers and a
  `choose` on the trigger id, plus a `condition: time` naming the weekdays. It
  goes in the CUTTING branch beside the idle guard, never at the top level: a
  top-level condition gates the restore too, and a rule that can leave a device
  off from Friday evening until Monday morning is not a rule whose saving
  anybody may compute. `replay.plan_for` refuses exactly that config from the
  other end, so the placement is the only one the grader accepts. Restricting
  the cut alone is complete rather than a simplification, and the guarantee is
  the detector's: `schedule.py`'s runs stop at local midnight, so both ends of
  every window it proposes fall on the same weekday
- **the guard is the device's own floor**, `active_floor`, not the global
  `STANDBY_MAX_WATTS` the night shape writes. That is the threshold every one
  of the candidate's 336 cells was actually judged against, and a guard at 30 W
  under a card claiming the device is idle below 8 W would be two claims about
  one rule answered with one word. The asymmetry with standby is deliberate and
  is the proposal's own: moving standby onto a per-device number would change
  automations users have already approved, so §5 files it there as an optional
  follow-on, and a shape with no installed base has no such debt. A candidate
  recording no level is refused rather than given the global constant, and so
  is one whose level does not survive the conversion into the sensor's units —
  `below: 0.0` preflights cleanly and is a condition that is never true
- **the window is rebuilt from `evidence["schedule"]`**, never from a constant.
  There is no `schedule.SCHEDULE` to import the way `standby.NIGHT` is
  imported: the night window is a declaration about the house and this one is a
  finding about one device, so a proposal raised last week has to be built from
  the blob stored with it. A blob that is missing, malformed, or describes a
  window `core/window.py` refuses is a refusal in a plain sentence. `days` is
  required rather than defaulted to all seven: defaulting is not a neutral
  reading of a truncated blob, it cuts the supply on the days the evidence
  never looked at. That includes a window crossing midnight, which that
  detector cannot produce and which is
  checked anyway, because `window.spec("00:00", "00:00")` raises and a
  traceback out of the daily pass takes every other suggestion, verification
  and rollback due that morning with it
- **its own trigger ids and alias suffix** (`schedule_start` / `schedule_end` /
  " off on a schedule"). The night shape's three are wrong English for a rule
  that cuts power at 07:00 on a Tuesday, and they are in people's
  `automations.yaml` where `_parts_of` matches them literally — so they are
  added beside, never reworded. `_parts_of` and `readback` now read either
  shape back out of a stored config and still return "" for a config this
  module did not write — and which shape it is comes from that pair of ids and
  from nothing else, so a night rule somebody hand-edited a `condition: time`
  into is still rendered as a night rule rather than losing its ceiling
- **the readback names the weekdays and says the harder thing.** §6 of the
  proposal is explicit that the daytime window is the riskier one — a wrong cut
  at 11:00 on a Tuesday meets somebody who is at home, where a wrong cut at
  03:00 usually does not — and that idle is observable where *needed* is not.
  So the sentence about the device not answering its remote is pushed harder
  here, not softened. The description left inside Home Assistant speaks in that
  detector's vocabulary (`occurrences`, `schedule_window`), because "nights"
  would be a lie about a window that happens at 11:00 on a Tuesday
- one device can carry both suggestions without collision: `schedule:<entity>`
  and `standby:<entity>` are different keys and hash to different automation ids
- `core/window.py`'s `_days_phrase` is now public as `days_phrase`. No output
  changes; the readback has weekday numbers rather than a spec and needs the
  same rendering, and a second implementation of "Mon-Fri" would be free to
  disagree in a place a user reads both

`core/replay.py` will REFUSE the config this writes — `_CONDITIONS` has no
`time` entry — and that is expected. Teaching the grader a per-weekday window
is the next work package, and bending the config shape to squeeze past the
current parser would be writing a rule to suit the grader rather than the
house.

## [2.14.1] - 2026-09-12

**Groundwork: the weekly grid.** Nothing a user sees changes in this release —
nothing calls the new detector yet, and the daily review still runs the standby
one and only that. What lands is stages B, C and D1 of
`docs/PROPOSAL-SCHEDULE-DETECTOR.md`: the machinery that reads the record as a
week rather than as a night, and finds the stretch a household is out of the
house. On the proposal's own example that is `07:00-17:00 local Mon-Fri` — about
2.6 kWh a month against the 1.4 the night window can see.

It ships ahead of its caller on purpose, the way `core/window.py` did in 2.13.1:
the four modules a candidate has to travel through (the builder, the replay
engine, the verification, the §9 artefacts) are all still written about a night,
and teaching them a second window at the same time would be a single change
nobody could review.

- New detector `core/detectors/schedule.py` (`KIND = "schedule"`), pure up to
  `find_candidate` and with `scan` as the only I/O, exactly as `standby.py` is
  split:
  - **stage B** — the record condensed to intervals. Four states, not two:
    idle, active, *off*, and *not observed* — the last spelled by which of
    three reasons it was, because that reason is what the evidence reports. A
    gap longer than the record's doubled heartbeat, an `unavailable` reading
    and a `source='statistics'` bucket mean all land as not observed. Reading
    any of them as flatness is what would produce a rock-solid all-idle run
    across hours nothing was watching
  - *off* is the one easiest to leave out and the one that costs most. A
    reading under `IDLE_MIN_WATTS` is a device the household already cuts — a
    timer, a deep sleep, or a sensor reporting export as a negative number —
    and counted as idle it lets a run extend across the dark hours, after
    which the per-occurrence minimum reports the OFF level as the standing
    draw. `standby.py` has always refused that night as `REASON_BELOW_FLOOR`;
    this refuses that half-hour, under the same name and for the same reason
  - **stage C** — 7 weekdays × 48 half-hours, each carrying how often it was
    idle and how often it was observed. A cell partly covered by unobserved
    time counts as neither, which is `window.windows`' whole-occurrence rule
    asked about half an hour instead of about a night
  - **stage D1** — maximal contiguous all-idle runs per weekday, merged across
    the weekdays that produced the *identical* run into one `window.Window`.
    A run covering the whole day is refused rather than built — that device
    wants a window spanning several *dates*, which the no-wrap rule already
    puts outside this module, and `window.spec` refuses to describe a
    twenty-four-hour one at all. Near misses are not merged and not
    intersected: that judgement is stage D2's, and §7.1 discards rather than
    showing a weak number
- Reads **90 days** where the standby detector reads 30, because the unit of
  this claim is a weekday — §2's table is blunt about it, ~4 samples of "Monday
  11:00" is a coincidence and ~13 is a pattern. Staleness is handled by a
  different rule instead: a cell has to be idle on *every* observation, so a
  household that changed three weeks ago has recent active occurrences and the
  run dies on them
- **Coverage is measured, never assumed.** `record.enforce_cap()` halves the
  retention window repeatedly on a home watching fast-reporting sensors, so a
  ninety-day read can come back with forty-five days. The evidence carries
  `window` (what was asked for) and `coverage` (what was actually there) as two
  separate fields, and a record that cannot reach eight occurrences of a
  weekday produces no candidate at all
- **The clock changes are handled in both directions, and both cost the claim
  rather than flattering it.** On a spring-forward date a row of cells never
  happened, so any run containing one is not an occurrence on that date. On a
  fall-back date the repeated hour is absorbed by one ninety-minute cell that
  has to be idle across all of it to count as idle. Both fall out of
  `core/window.py` rather than being special-cased here
- A **unimodal device still produces nothing**, and here it is structural
  rather than an extra test: `levels.two_levels` supplies the per-device floor
  a cell is judged idle against, so without two levels there is no grid to
  build. Note the deliberate asymmetry — this detector uses the device's own
  `active_floor` where `standby.py` still uses the global
  `STANDBY_MAX_WATTS`, which the proposal files as an optional follow-on rather
  than something to change under automations users have already approved
- The evidence blob **speaks in occurrences**, not nights: `occurrences`,
  `occurrences_observed`, `occurrences_examined`, `occurrences_per_month`,
  `hours_per_occurrence`, `schedule_window`. Standby's night vocabulary is
  untouched — those blobs are on disk on live installs — and no blob of this
  kind exists anywhere yet, so this one is free to be honest. The cost is named
  in the module docstring rather than papered over: `review.evidence_fingerprint`
  reads five fields and two of them (`nights_per_month`, `night_window`) do not
  exist here, so the work package that teaches the review pass a second
  detector has to generalise the fingerprint over them
- New leaf module `core/detectors/power.py`: `power_scale`,
  `controllable_switch`, `POWER_UNITS` and `CONTROLLABLE_DOMAINS` moved out of
  `standby.py`, along with the reading loop both detectors use to ask
  `levels.two_levels` its question. A detector importing another detector is
  backwards — they are siblings reading the same record, not layers — and
  `core/detectors/levels.py` already set the precedent for exactly this.
  `standby.py` re-exports all four names, so `core/builder.py`,
  `core/verify.py` and the existing tests are untouched

## [2.14.0] - 2026-09-07

**Stage A, properly: a histogram, not a median.** 2.13.0 shipped a stand-in for
the bimodal check — the median of the whole window as the idle level, three
times that as the floor — which does reject a router, but has a known false
negative: a device used more than half the window (a desktop, an always-on
media player) has its median land ON the active level, and is rejected even
though its night plateau is real. A histogram does not care which cluster is
bigger, and it is also what the next work package (the weekly schedule grid)
needs, so it is built once rather than twice.

- New pure, stdlib-only module `core/detectors/levels.py`: histograms an
  entity's readings on a bin scale with constant RELATIVE width, finds the
  modes, and answers with the device's own idle level and the floor above
  which it is in use — the lowest QUALIFYING mode, not the largest, which is
  the whole fix
- Readings below `IDLE_FLOOR_WATTS` never join a bin at all, which is also
  decision 5's answer to a device that is genuinely switched off for part of
  the window: the off cluster can never become a candidate mode, so it cannot
  be mistaken for the idle one
- `standby.device_levels` is now a caller of it rather than a second
  implementation. Its own four constants for this test
  (`ACTIVE_GAP_MULTIPLE`, `ACTIVE_GAP_WATTS`, `ACTIVE_MIN_READINGS`) are gone;
  their values and reasoning carry over unchanged into
  `levels.MODE_GAP_MULTIPLE` and `levels.MIN_MODE_READINGS`
- **One number moves.** `active_floor` was three times the idle level; it is
  now the histogram bin edge immediately above the idle cluster's own
  readings — a threshold rather than a multiple, still recorded and still
  used by nothing downstream
- No behaviour change for a device already found: a television idling at 12 W
  and watched at 120 W is still a candidate, a flat router is still rejected.
  What changes is which devices are found at all
- Review caught `VALLEY_MAX_OCCUPANCY_FRACTION` — the "how empty" half of the
  valley test alongside `VALLEY_MIN_WIDTH_BINS` — doing nothing: by
  construction the gap it measured was always empty, so it was a threshold
  that could never fire. Retired rather than kept for show; `MIN_MODE_READINGS`
  already catches the stray-mid-ramp-reading case it was meant for, argued in
  the comment above `VALLEY_MIN_WIDTH_BINS`
- `_median` picked up a third copy in `levels.py`; moved the one
  implementation to `core/observer.py` and pointed `levels.py` and
  `standby.py` at it instead (`replay.py`'s own copy is untouched — out of
  scope for this work package)

## [2.13.1] - 2026-09-07

**Groundwork: "a night" becomes a window you can pass.** Nothing a user sees
changes in this release. The period of the day the suggestion pipeline reasons
about was a pair of integer hours living inside the standby detector, and four
other modules reached in for them — so the period was not a thing anyone could
hand over, only a constant everybody knew. The next detector's window is
08:30-17:00 on weekdays, and without this it would have landed as seventy-odd
scattered edits across modules whose reasoning is written in prose.

- New leaf module `core/window.py`: a window is now **data** — two wall-clock
  times and the weekdays it happens on — and every operation over one (place it
  on a date, enumerate it over a range, print it, ask how long it is) lives
  there and nowhere else. It imports nothing from `core/`
- It can describe a window that names its weekdays and a window that crosses
  midnight, neither of which has a caller today. Both are built now because the
  cost of adding them later is not the code, it is re-opening every module the
  seam was extracted to close
- The standby detector declares `NIGHT` and hands it out; the builder writes its
  two ends into the automation, the verification measures the week over it, and
  the review pass fingerprints its rendered label. The stored evidence
  vocabulary is untouched — `night_window`, `hours_per_day`, `nights`,
  `nights_observed`, `nights_examined`, `nights_per_month` and `excluded` are on
  disk on live installs and renaming them is a migration, not a refactor
- **One behaviour change, and it is about the clock.** There were two answers to
  "what unix second is 01:00 on this date" and they disagreed: the detector took
  whatever `mktime` normalised a skipped hour to, while the replay round-tripped
  it and could tell that the time never happened. The strict answer is now the
  only one. On the morning the clocks go forward, the detector drops that night
  instead of measuring a three-hour one as four hours — one occurrence a year,
  out of a window that needs ten of thirty, failing towards measuring nothing
- The verification's `01:00-05:00 local` label is byte-for-byte what it was. A
  home mid-verification compares the string its baseline stored last week
  against the one measured today, and a label that gained so much as a full stop
  would report a real saving as unmeasurable

## [2.13.0] - 2026-09-07

**The detector stops offering to switch off the router.** Everything the standby
detector tested until now was *shape*: small, steady, all night, most nights. A
television on standby has that shape — and so does a router, a NAS, a CCTV
recorder, an aquarium pump and a relay behind a boiler, each of them quietly
doing the job it was installed for. Nothing further down the pipeline caught the
difference either: the automation's 30 W runtime guard never fires on a 15 W
router, and the replay looks for proof the device was *used* after a cut, which
for a router never appears because a router never leaves standby-shaped draw. The
only thing between that and a home losing its network at 01:00 was the person
reading the card.

- The detector now learns **the device's own two levels** before it will call
  anything standby, and rejects a device that only ever had one. The argument is
  short: you cannot call 15 W "standby" without having seen what "in use" looks
  like on that same device. A television idles at 12 W and plays at 120 W; a
  router lives its whole life between 12 and 18 W
- It reads the **whole thirty-day window, day as well as night** — the one rule
  here that does. The in-use level exists only in the daytime series, so a check
  restricted to the hours the claim is about could never see the thing that makes
  the claim true
- The gap has to be a real one, and the high side has to be occupied: a device
  that drifts between 12 W and 20 W has one level with a wobble on it, and two
  readings at 120 W are a meter glitch rather than a state. A DVR that idles at
  15 W and records at 25 W does not clear the gap, which is the right answer —
  §6 of the proposal it comes from names the DVR as exactly the device nobody
  should be offered a switch for
- Both levels are written into the **evidence blob** and printed in the
  suggestion's evidence, beside the nights and the exclusions, because the answer
  to "is this a television or a router" is an observation like any other. Nothing
  else uses them yet: the runtime guard and the replay's in-use verdict still
  turn on the global 30 W ceiling, and moving them onto a per-device threshold
  would change automations users have already approved
- Suggestions already open against a router need no migration. Once the detector
  stops finding them the existing withdrawal machinery retires them on its own

Both new numbers are derived from constants that were already here rather than
picked — the gap from the flatness allowance the plateau rule has always used —
and the floor below which the window cannot answer the question at all is the
smallest series the evidence floors can possibly have left, so it never fires in
practice. It falls the same way as the check it guards: "too little data to tell
whether this is a router" is not a weaker version of "this is a standby draw".

This is the first step of a longer proposal, and deliberately the narrow one.
Generalising "night" to "window" — the weekday-daytime waste that is roughly two
thirds of what a household actually leaves running — is a phase, not a patch, and
it comes later.

## [2.12.0] - 2026-08-31

**Phase 2: the loop closes.** Design §13 calls this the milestone that matters —
"a demo before Phase 2 shows a chatbot; a demo after it shows the actual
proposition" — and scopes it to one vertical slice: standby loads, observed →
replayed → proposed → applied → verified → rolled back. All seven stages of §7's
lifecycle now have code and run end to end. The agent finds waste nobody asked
it about, grades its own idea against the record, puts one question to a person,
writes the automation on a yes, and a week later reports what actually happened
with the rollback on the table whatever the number says.

Everything below is deterministic and none of it imports the model. §7.1 draws
that line — "the model proposes candidates; it does not get to grade them" —
and the reason is falsifiability rather than purity: a number a model produced
is a number nobody can reproduce, argue with, or explain the change in from
yesterday. Every threshold is a named constant with its argument written beside
it.

### The lifecycle store — `core/suggestions.py`

- One row per candidate, growing a column at a time from first sight to
  measured outcome, so a restart between any two stages loses nothing but the
  process
- A **third table in `activity.db`**, not a fourth file in `/data`. Proposing a
  suggestion writes the suggestion row *and* queues the outbox message
  announcing it, and a home where those two disagree is worse than either
  failing. One file, one transaction
- `key` is the load-bearing column — `"standby:sensor.tv_power"`, derived from
  the entity alone — because the review re-derives the same key from the same
  waste every morning. Dedupe here is not an optimisation, it is the difference
  between a product and a nuisance, and it is enforced by a partial `UNIQUE`
  index over non-retired rows rather than by a promise
- Illegal transitions are logged and refused, writing nothing. `applied` is the
  claim that something was written into a home and `verified` the claim that it
  was measured; evidence code can set by accident is not evidence
- **Never pruned**, unlike the behavioural record. §12 measures the product on
  acceptance, dismissal and rollback rates computed from exactly these rows, and
  a retention window would quietly delete the denominator

### An entry into the approval gate with no chat session — `gate.propose`

- A chat approval is answered in the next minute; **an agent-raised proposal may
  sit for days**, and session memory is in-process, evicted after an hour and
  gone on restart. So the sessionless pending is parked *nowhere*: the whole
  question is written to the `changes` row, and `gate.pending_for` rebuilds it
  whenever the answer arrives
- **`gate.apply` therefore needed no change at all, and cannot tell the two
  entries apart.** That is the invariant to protect. `/rest/approve` takes a
  `change_id` *or* a `session_id` for the same reason — a second endpoint would
  be a second place for "everything lands on apply" to stop being true
- `gate.apply` now claims the journal row atomically before executing, so a
  double-tapped Apply cannot execute twice, and **refuses on drift** rather than
  silently overwriting a hand-edit made during the days-long wait
- `activity.source_for` gains an honest third value for agent-raised changes
  rather than reporting a human click, and can no longer *derive* that source
  from any client-supplied string

### The detector — `core/detectors/standby.py`

- First of §8's four types, and first because its loop is honest end to end: the
  measure is watts, the saving is arithmetic, there is no comfort trade-off
- What it looks for is a **plateau** — a stretch of the night a power sensor
  never drops below, night after night, on a device nobody was using
- Shaped around the trap that **the record is not a time series**. A flat
  standby draw crosses no threshold and writes nothing; what makes the plateau
  visible is the fifteen-minute heartbeat, and *their sameness is the
  observation*. So absence of rows means two opposite things, and any gap
  materially longer than the heartbeat disqualifies the night it lands in rather
  than being interpolated across
- **Statistics rows are never evidence of a floor.** A bucket mean cannot show
  that a value never dropped below a level, and that is the detector's entire
  claim
- A power sensor with no controllable switch on the same device produces
  nothing — the record is not even read for it. §9 wants a suggestion answerable
  in one word, and there is no one-word answer to "your television draws 12 W
  and neither of us can do anything about it"
- `watts` is the median of the per-night minima, `hours_per_day` is the observed
  night window and nothing more, and `kwh_per_month` is projected over the
  nights the plateau was *actually seen on* — ten nights of thirty are credited
  with ten

### The builder — `core/builder.py`

- Turns a measurement into a change: cut the supply at 01:00 behind a guard on
  the sensor's own reading, restore it at 05:00 unconditionally
- **One automation, not two.** A pair would be two journal rows answered with
  one word, which §9 forbids, and two rollback operations where the journal
  recorded one. The branch is `choose` rather than a top-level condition because
  a top-level guard would gate the turn-*on* as well, and the device would never
  come back
- The hours and the ceiling are imported from the detector, never restated — if
  the two could disagree about when night is, the estimate would stop describing
  the automation printed beside it

### The replay engine — `core/replay.py`

- The grading step, and the only one that can prove a rule *wrong*: the
  candidate's triggers and conditions evaluated over the stored event log,
  exactly as Home Assistant would have evaluated them live
- **Pure, with no I/O half at all.** The caller has already read the days the
  detector was judged on, and reading them again here would grade a config
  against a different series than the measurement printed beside it
- **It refuses by default.** The supported subset is exactly what the builder
  emits, dispatched through tables the way `rollback.py` dispatches an inverse,
  and a refusal *discards the candidate* — §7.1 gives a candidate two fates and
  "partly evaluated" is not one
- The asymmetry is load-bearing: a device used later is *proof* the rule would
  have been wrong and is counted wherever it is visible; a device *not* used is
  *not* proof it would have been right
- **Replay may only tighten, never inflate.** If its recomputed saving comes out
  higher than the detector's it reports the detector's instead and says so in
  `estimate.basis`. Shipping the larger of two numbers that disagree about one
  house is the opposite of what §7.2 asks
- DST, the unit line and a required `entity_id` are all decided rather than left
  implicit — a config replayed against the wrong sensor's series produces a
  clean blob, a plausible saving and no symptom anywhere downstream

### The daily pass — `core/review.py`

- Once a day after 09:00 local, the caller the three modules above were written
  against: scan, build, replay over the same rows, rank, and put **one** question
  to a person
- Six decisions it owns, each deferred to it by name and each argued where the
  code makes it: what counts as the evidence having CHANGED (a fingerprint over
  the claim, not over the window that slides every morning); how many
  would-have-been-wrong nights disqualify (`MAX_WRONG_NIGHTS` is 0, because
  replay's count is a floor rather than an estimate); create rather than update
  (an update against an automation that does not exist is journalled `one_way` —
  reversible advertised as irreversible); which series replay gets; cadence, rank
  and volume (one new question a day, at most three open, best first, withdrawn
  when its evidence stops describing now); and cost — the record is read only,
  no transaction, and everything written goes to a different file from the one
  the observation thread writes
- `review.settle` is the seam `core/gate.py` deliberately leaves open. The gate
  hands back a `change_id` and does not know what a suggestion is; this tells the
  suggestion what became of it — applied with the baseline the verification will
  measure against, dismissed on a "no", retired if the write never landed. It
  reads the JOURNAL ROW rather than the tool result

### The surfaces — `core/artefacts.py`, the Suggestions tab, two chat tools

- §9's artefacts rendered **once, in Python, for both surfaces**, because §9's
  rules are about what a rendering may CLAIM and a rule implemented in Python
  for ASI1 and again in JavaScript for the panel is a rule that drifts
- `estimate.basis` is read in exactly one place, so every number carries the
  counts belonging to whichever side produced it, and the replay's own figures
  are reported under their own heading rather than merged into the estimate
- The automation readback is rebuilt from the STORED CONFIG, so a proposal
  raised last week still describes the automation it would actually write
- **The chat path applies nothing.** `Show an agent suggestion` returns a
  QUESTION; the agent loop recognises it in the tool RESULT — which the model
  cannot forge, unlike an argument — the session parks it in its own slot, and
  `parse_approval_decision` reads the user's own next message without the LLM
- Panel Accept/Reject and an ASI1 "yes" both post to the existing
  `/rest/approve`. One exit, two surfaces — which is what makes a chat "no" the
  same thirty-day cooldown a panel "Dismiss" is
- The disclosure boundary was withholding every suggestion: `01:00-05:00` is
  four hex-ish groups joined by three separators, which matched the IPv6/MAC
  rule. That rule now requires **one** separator throughout — no address mixes
  them, and every time range this agent renders does

### Verify — §7.2, the honest part — `core/verify.py`

- **A week after applying, the agent measures the same thing again and reports
  it**, then offers the rollback whatever the number says
- Both weeks go through `standby.measure_nights` at the same scale over the same
  length of window. Never a second implementation: a baseline computed by a
  second reading of "the median of the per-night minima" would be free to drift,
  and the difference between two such numbers would be a difference in the
  arithmetic rather than in the home
- **The claim is a range, or the sentence saying the effect is too small to
  separate from noise. There is no third form.** `NOISE_FLOOR_WATTS` is the line
  between them, and two independent arguments land on the same value: the
  record's own resolution (`NUMERIC_MIN_ABS_CHANGE`, carried twice because a
  difference has two ends) and the residual a successful week can still be
  hiding (a night excluded as `below_floor` dropped *under* the standby floor,
  not to zero). On the kWh axis the floor is sized to the claim it guards, with
  the measurement's own hours and nights
- The report **names whose estimate it is comparing against**. `estimate.basis`
  was carried this far for exactly this: "the replay said 1.4, the measurement
  says 1.1" over a detector-sourced figure would give back everything §7.1 spent
  a module protecting
- **A week that could not be read is never "it saved nothing".** An unreadable
  *after* may be readable next week, so it is measured again — up to three
  attempts, then reported as unmeasured with the rollback still offered. An
  unreadable *before* never will be, because the automation has been running
  over the record ever since, so that is said at once. What separates a
  successful week from an unreadable one is `nights_observed`: a working
  automation leaves a week in which no night qualifies, and every one of them
  was read
- **A week the rule was not running is not a verification.** The automation is
  checked before any number is claimed, in the register `rollback._check` and
  `gate._drift_refusal` established. The edit check is `replay.plan_for` over
  what Home Assistant holds now against the stored config — the same parser that
  graded the rule, so what is compared is what the grading depended on; a dict
  comparison would report an edit every time. "Deleted" is claimed only when two
  independent reads agree, and a deleted or already-rolled-back change retires
  the suggestion with a message rather than parking a question with no meaning
- **Keep or roll back is the user's answer, not a state the code sets.** The
  report is delivered at `verified`; `kept` and `rolled_back` come from a
  person — panel buttons or a one-word chat reply, both landing on
  `verify.answer`. The undo is `rollback.roll_back` and nothing else: no second
  rollback path, and no re-gating, because the operation was pre-authorised when
  the change was approved and only the selection was ever in question. A failed
  rollback is relayed verbatim and leaves the report open
- A weekly report is **not urgent**. §11 reserves the Home Assistant push for
  what cannot wait, and `core/delivery.py` decides the channel, not the producer
- It runs from the daily pass that already exists rather than a second schedule,
  and the pass now takes **one registry round trip** and hands the same snapshot
  to the detector and to the verification

### Also

- New prompt section 10, "What became of a change the agent made"; the old
  sections 10 and 11 become 11 and 12
- Three new LLM tools: `List agent suggestions`, `Show an agent suggestion`,
  `Show a measured result`. None is in `DESTRUCTIVE_TOOLS` and none reaches
  `gate.apply`
- Four markers now exist (`gate.APPROVAL_MARKER`, `rollback.CONFIRMATION_MARKER`,
  `artefacts.QUESTION_MARKER`, `artefacts.VERDICT_MARKER`) because four code
  paths answer them, and a conversation may hold only one outstanding question
- `/rest/suggestions` reads the open proposals and the measured results;
  `/rest/suggestions/verdict` answers one report. There is still deliberately no
  approve endpoint there

## [2.11.0] - 2026-08-27
- **Rollback is reachable from chat.** It shipped in 2.8.0 behind a button in
  the panel, which is the failure design §9 names: "if it cannot be read and
  decided in a chat message, it does not ship." A user in ASI1 or the panel's
  chat box can now say "undo that" and get it undone
- Two tools: `List agent changes` (the journal, with rollback's own verdict on
  what can still be undone and why not) and `Roll back a change`
- **The operation stays pre-authorised; the SELECTION is confirmed.** Those are
  two decisions and core/rollback.py now keeps them apart. In the panel the user
  clicks a row, so nothing is inferred. In chat a model picks a change id by
  matching fuzzy text against the journal — the operation is still the one
  computed at approval time, but WHICH change to undo is a fresh decision made
  by an LLM, and getting it wrong undoes something the user never meant
- So `Roll back a change` never rolls anything back. It plans one and returns a
  question naming the change and what undoing it will do; the agent loop
  recognises that in the tool RESULT — which the model cannot forge, unlike an
  argument — and the session holds it until the user answers in their own words,
  read by `parse_approval_decision`, which does not use the LLM. A reply that is
  not a decision ("no, the other one") re-asks rather than guessing
- None of this goes through the approval gate. The rollback tool is not in
  `DESTRUCTIVE_TOOLS`, the question is parked in its own session slot, and
  nothing on the path calls `gate.apply` — putting an LLM turn in front of the
  escape hatch is what the gate's own docstring forbids
- Refusals and failures are relayed verbatim. `roll_back`'s message names the
  automation and what Home Assistant said; §11 requires that to arrive
  actionable rather than summarised into "that did not work"
- A chat-initiated rollback is journalled with the real session id, so
  `source` reads 'asi1' rather than 'panel'
- `gate.describe_change` is now the single describer for a journal row: the
  panel, the chat listing and the notification cannot name the same event
  differently
- The "no recorded inverse" refusal says which case it is — a sensitive service
  call, which has no undo by design, or a prior config that could not be read.
  It is relayed to users now, not just shown beside a row they can see
- New prompt section 8, "Undoing what you changed"; the old sections 8 and 9
  become 9 and 10

## [2.10.1] - 2026-08-27
- **`/rest/approve` now honours the same approval TTL as chat.** `approval_expired`
  (`APPROVAL_TTL_SECONDS`, 10 minutes) already guarded the ASI1 chat path in
  `handlers/chat_handlers.py`, but a panel session could sit open for the full
  one-hour eviction window and still fire a stale approval. An expired approval
  is now closed the same way a restart-orphaned one is: marked `expired` in the
  journal, its outbox notice withdrawn, and the panel told plainly rather than
  executing the tool
- **`asi1_model` is now a real add-on option.** `run.sh` already read it out of
  `options.json`, but `config.yaml` declared neither the option nor its schema
  entry, so the Supervisor never wrote the key and the setting could never take
  effect. It is also no longer shadowed at the point of use: a session's model
  defaulted to a hardcoded `"asi1-mini"` regardless of this setting, so it only
  ever reached ASI1 chat-protocol sessions, which never send a model of their
  own — a panel session always sends one and continues to win there

## [2.10.0] - 2026-08-27
- **The record now has the cap §6.2 always promised.** `RETENTION_DAYS` bounds
  the record in time, and on a home whose sensors report every few seconds that
  bounded nothing — which is what 2.7.0 and 2.7.1 demonstrated. `record.enforce_cap()`
  is the missing size-driven trigger: as the record approaches its ceiling it
  re-runs the same collapse with a shorter window, halving from the configured
  retention until the record fits
- Nothing is discarded that was not first collapsed. The cap calls `prune()`,
  the same collapse-then-delete transaction, only with a shorter cutoff — the
  price of an early collapse is sub-day resolution over the stretch that
  collapsed, and profiles are never dropped
- `profile()`'s `includes_collapsed_history` stays honest for free: it already
  compares the requested window against the persisted collapse boundary rather
  than against the retention setting, so moving that boundary forward moves the
  flag with it. `Get behavioural record overview` says the same thing in prose
  when the cap is what is deciding how far back the record reaches
- It measures used pages, not the file size. SQLite frees pages to a freelist
  and reuses them rather than shrinking the file, so a cap read off
  `os.path.getsize()` would ratchet the window down to its floor chasing a
  number that was never going to move — and the alternative, VACUUM, is exactly
  the long exclusive lock this store may not take
- `CAP_MIN_RETENTION_DAYS` (7 days) is the floor. A cap nothing can satisfy is
  not a cap that empties the record: the floor holds, and the shortfall is
  logged, because the fixes are a larger cap or fewer watched entities and both
  are the user's to make
- **New add-on option `record_max_mb`** (default 256, 0 disables). The first of
  the record's numbers to become an option rather than an env var, because it
  is the only one that depends on the machine rather than on the data: /data on
  an SD-card Pi and /data on an SSD differ by orders of magnitude, and only the
  user knows which they have

## [2.9.0] - 2026-08-27
- **The disclosure boundary.** Design §10.1 has always tabulated what leaves
  the house and nothing in the code named it, enforced it or tested it.
  `core/disclosure.py` now does — one module, one stated rule, standing to
  disclosure exactly as `core/watchlist.py` stands to recording
- The rule: a tool answer may carry names, states, times, counts, aggregates
  and automation configs. It may not carry a credential, a network address, a
  geographic coordinate, or an unbounded structure. Names are the deliberate
  exception and are never inspected — "Martin's office lamp" reaching the model
  intact is the product, and mangling it would be solving the wrong problem
- Enforced at the one place data leaves: every tool returns through
  `disclosure.emit()` rather than `json.dumps`, which is an invariant a test
  holds with a grep rather than a convention a reviewer has to remember
- Two holes closed. `Get Home Assistant entity details` handed over Home
  Assistant's whole state object, and an attributes blob is where a home's
  coordinates, camera tokens, MAC addresses and configuration URLs actually
  live; it now names the fields it keeps and filters the attributes. And an
  automation's `webhook_id` is a bearer token by another name, left in every
  config the model read
- Withholding a credential from a config would break the read-modify-write
  round trip the model performs on every edit, so `update_automation` restores
  what was withheld from the config Home Assistant actually holds — and refuses
  the write rather than saving a config nobody can reconstruct
- Smaller ones on the way past: the listener's `last_error` is a raw exception
  string carrying the URL it failed against, an entity whose STATE is an IP
  address is network layout whoever asked, and the services catalogue's 50 kB
  cap sliced the JSON mid-string and handed the model a document it could not
  parse
- Design §10.1's table corrected to describe what the code does, and its Open
  resolved. The claim is not "raw data never leaves" — the agent needs recent
  transitions to answer "when did the door last open", which is Phase 1's
  day-one value. It is that the home's STORES stay in the house, and that what
  leaves is bounded, named and free of secrets

## [2.8.0] - 2026-08-27
- **Rollback.** The inverse of every approved change has been recorded since
  2.6.0 and nothing executed one; `core/rollback.py` now does. Reversibility
  stops being a property of the data and becomes something the user can press
- Automation create / update / delete are integration-level functions in
  `tools/tool_homeassistant/automations.py`, with the LLM tools as wrappers
  over them. Undoing a change never goes near the tool registry or the agent
  loop, so it does not depend on the model being available, or willing
- A rollback is journalled as its own change, linked to the one it reverses by
  `undo_of`. The original is never rewritten — it was applied, and no later
  event makes that untrue — it only gains an `undone_by` pointer once the undo
  has actually succeeded. That leaves somewhere honest to record a rollback
  that FAILED, which a status field on the original does not have
- A rollback is not put through the approval gate. It runs the operation
  decided at approval time, unchanged, with no argument for anyone to fill in;
  asking again would be asking permission to withdraw permission, and would
  put an LLM turn in front of the one path that has to work when the LLM does
  not. It is still journalled with its own inverse, like every other write —
  so an undo can itself be undone
- A failed rollback records itself as failed, leaves the change eligible to be
  tried again, and pushes an urgent notification naming the automation and
  what Home Assistant said. The panel shows it in its own notice that the
  fifteen-second refresh cannot wipe
- A rollback a restart caught mid-flight is closed at startup and reported —
  nobody can say whether it landed, and the safe reading of that is the loud one
- The Activity tab's "undoable" label is a button. The rollback rate design §12
  calls "the one that matters" is counted and shown
- **Fix: `tasks/delivery.py` was never imported.** `main.py` listed it and did
  not load it, so the outbox drain and the startup expiry of orphaned approvals
  had never run — urgent messages were queued and never delivered

## [2.7.1] - 2026-08-27
- **Fix: the record stopped accepting writes on a home with fast-reporting
  sensors.** Every observation failed with `database is locked`, continuously
  and for every entity. The hourly relations scan was a SQL self-join whose
  condition (`b.entity != a.entity AND b.ts BETWEEN ...`) could not use the
  only index there was — its leading column is the one constrained by `!=` —
  so SQLite scanned the whole events table once per row. Quadratic is
  invisible on the few thousand rows a handful of lights produce and does not
  finish at all on the million a dozen energy sensors do, and all of it ran
  inside the write transaction `DELETE FROM relations` had already opened. The
  scan is now one ordered pass with a sliding window, holding only the events
  inside it: 1.8M rows in under 10 seconds, against a self-join that did not
  finish in two minutes
- The write lock is now taken only for the two statements that replace the
  relations table, after all the reading is done. Nothing that scans the
  record holds it any more
- An index on `events(ts)`, which both whole-record passes needed and neither
  had: the relations scan's ordered read and `prune()`'s `DELETE`
- Writes retry briefly when they meet a lock rather than dropping the
  observation on the first `SQLITE_BUSY`, and the busy timeout is a named
  constant rather than a literal
- The relations scan and the retention prune no longer start on the same tick.
  Both are whole-record passes, and SQLite takes one writer at a time
- `RELATION_SCAN_DAYS` narrows the relations scan below the retention window
  for a home that wants it. Unset, the scan still covers everything the record
  holds

## [2.7.0] - 2026-08-25
- **Fast-reporting entities can be watched at all.** An integration that pushes
  a state every second or two used to make backfill impossible: the recorder
  was asked for twenty entities over a day at once — hundreds of thousands of
  rows — which Home Assistant could not answer inside the timeout. The request
  was retried on every reconnect, failed every time, and those entities were
  never warmed. History is now streamed rather than held in memory, and each
  request is sized to how often the entity actually reports, measured against
  the recorder itself rather than assumed
- Entities too dense to read in full get a request budget, spent on the newest
  end where the questions are, and the stretch behind it is filled from Home
  Assistant's own five-minute and hourly statistics. Those rows are marked
  `statistics` in the record, so a mean is never mistaken for an observed
  crossing. The hourly pass is only made where the five-minute buckets came up
  short, which also means a home on a short `purge_keep_days` is now warmed
  with months of shape it could not reach before
- A jitter floor for numeric entities (`RECORD_NUMERIC_MIN_INTERVAL`, 60s):
  before this the record had no upper bound at all — a power reading crosses
  the 5% threshold on noise alone, which is 86,400 rows a day per entity rather
  than the ~96 the heartbeat budgets for. A change of 25% or more is still
  recorded at the second it happened, so a kettle switching on stays a crossing
  and keeps its place in the two-minute relation window
- The entity history tool no longer reads an unbounded window to report 200
  transitions, and says so when it covered less than it was asked for
- Fix: an entity's behaviour profile no longer reports "0% of the time on" for
  a cover, a climate entity or a sensor — anything that never uses the literal
  state `on`. That reading was indistinguishable from a light that really is
  always off; now it reports `on_fraction` as unknown instead of zero when the
  entity's whole history never shows an on/off concept at all
- **relations**: the last of design §6.1's four record layers. The record now
  notices when one watched entity tends to change shortly after another —
  "hall motion → kitchen light within 2 minutes, 43 times" — rebuilt hourly
  from the event log, and self-pruning: a pattern that stops recurring fades
  out on its own once its evidence ages past retention, with nothing extra to
  clean up
- a new tool, `Get observed patterns`, so the agent can answer "what usually
  happens after X" or check whether one thing tends to trigger another before
  proposing an automation from the user's own habits, instead of guessing
- the Record tab shows the strongest patterns across the whole record, and an
  entity's detail view shows what usually follows it

## [2.6.1] - 2026-08-25
- Fix: the behavioural record now collapses old events into a per-entity
  aggregate instead of quietly deleting them at 90 days. Habit questions that
  reach past retention ("how do I usually use this in the evening") still get
  an answer — the model is told when that answer is a running total rather
  than an exact window, since a collapsed period has no day-by-day resolution
- "how far back does the record reach" (the Record tab, `Get behavioural
  record overview`) no longer creeps forward by itself every hour as pruning
  runs — it now reports the entity's true first-ever observation

## [2.6.0] - 2026-08-25
- a new **Activity** tab: every change the agent proposed, who asked for it,
  what was decided and what was written — kept on disk, so it survives restarts
  and does not depend on the connection it is auditing
- nothing reaches Home Assistant any more without being written down first. The
  row exists before you are asked, so a request raised in ASI1 is visible in the
  panel while it is still open, and an approval the add-on restarted through is
  marked expired instead of vanishing
- **the operation that would undo each change is recorded with it** — captured
  while the previous configuration is still there to read. Nothing executes
  those inverses yet; the tab labels what could be undone and no more
- the agent can now reach you when you are not talking to it: urgent messages
  become a Home Assistant notification. Set `notify_service` to a companion-app
  target for phone push; left empty it uses a persistent notification, which
  every install has
- a watched device disappearing from Home Assistant is the first thing sent that
  way — until now it just quietly stopped being recorded
- ASI1 can ask "anything new?" and get whatever has been queued, once
- one approval gate: deciding, executing and recording a change now happen in
  one component instead of at four call sites that had to agree

## [2.5.0] - 2026-08-25
- the agent can now look at the **past**, not only the present — three new
  read-only tools it can call while answering
- **entity history** reads Home Assistant's own recorder for any entity: what it
  did over a window, how long each state was held, and min/max/mean for numeric
  sensors. Roughly ten days, depending on the user's recorder settings
- **record overview** reports what this add-on has itself observed — which
  entities are watched, how much is stored for each, how far back it reaches,
  and whether the listener is connected, so "the record is empty" can be
  explained rather than just stated
- **entity behaviour profile** answers the habit questions from that record:
  when an entity is usually switched on, how often, how much of the time it is
  on, from weeks or months of observation
- these tools only read. They cannot change what is watched — the panel's
  Watching tab stays the only place that is decided

## [2.4.2] - 2026-08-24
- pairing has moved out of the strip at the top of the panel and into its own
  **ASI1** tab, with step-by-step instructions for pairing a device
- the tab shows how many devices are paired, and marks itself when none are — the
  agent ignores ASI1 messages until at least one is
- the agent's address is shown in full there with a copy button, next to an
  explanation of why pairing exists at all: the address is public, so anyone who
  knows it could otherwise send this agent instructions
- every screen gained the vertical space the strip used to take

## [2.4.1] - 2026-08-24
- fixed: recovering history from Home Assistant's recorder failed every time —
  the request carried a malformed end time, so nothing was ever recovered and
  newly watched entities started from empty
- the record now also reaches **backwards**: history from before the agent was
  watching is picked up, not just the gap since it last ran. Previously an entity
  could only ever be filled forward from its newest stored change, so history
  that appeared for an earlier period stayed invisible
- both together mean selecting an entity now gives you up to ten days of its past
  immediately, as originally intended

## [2.4.0] - 2026-08-24
- the agent now keeps its own record of how your home behaves, so it can later
  reason about patterns rather than only about what is happening right now
- **nothing is watched until you say so** — the new **Watching** tab lets you pick
  whole devices or individual entities, and an entity you have not selected is
  never observed and never stored
- the new **Record** tab shows what has actually been gathered: how many
  transitions per entity, when each was last seen, and a per-entity view with its
  recent changes and the hours of the day it is usually active
- when you select something, its recent history is recovered from Home Assistant's
  own recorder straight away — so a newly watched entity has data immediately
  instead of in a week
- the same recovery fills the gap after the add-on has been stopped, restarted or
  updated, as far back as your recorder still holds
- the record is kept in the add-on's own storage and survives restarts and
  rebuilds; events older than 90 days are dropped
- only meaningful changes are stored: repeated readings, attribute-only updates
  and flapping are filtered out, and numeric sensors are recorded when they cross
  a threshold rather than on every reading
- the Record tab shows whether the agent is actually connected to Home Assistant,
  so an empty record is never ambiguous
- the panel is now three tabs — Chat, Watching, Record — and your conversation is
  kept when you switch between them

## [2.3.0] - 2026-08-21
- the agent is now paired to its home: it only acts on messages from uAgent
  identities you have bound from the add-on panel
- previously any address that knew the agent's address could control the home and
  create, change or delete automations — the YES/NO confirmation was no defence,
  since the same sender supplied it
- unpaired senders get a short instruction to pair and nothing else: no reasoning,
  no tools, and no ASI1 tokens spent on them
- pair from the panel: **Pair a device** shows a six-digit code, valid for 10
  minutes and single-use, which you send to the agent from ASI1
- several household members can pair, and any of them can approve changes
- bindings are listed in the panel and can be removed there; unpairing also
  discards that identity's conversation and any confirmation it had waiting
- bindings are stored in /data, so they survive add-on restarts and rebuilds
- the panel shows the agent's own address with a **Copy** button, so it can be pasted
  into ASI1 without digging through the log
- the agent logs its address and how many identities are paired at every start

## [2.2.0] - 2026-08-21
- panel rewritten: the Material Dashboard theme and `vue-material` are gone, replaced by a
  minimalist chat UI with no third-party UI framework — the built panel dropped from 1.1 MB
  to ~121 kB
- panel follows the system light/dark preference instead of always rendering white
- panel no longer loads fonts or icons from CDNs, so it works on a Home Assistant host with
  no internet access
- agent replies render as documents rather than chat bubbles, so YAML and lists stay readable
- message box accepts multi-line input (Enter sends, Shift+Enter for a newline)
- panel migrated from Vue 2.7 (end of life) to Vue 3
- add-on, agent and panel versions aligned — the agent previously reported "2.10"

## [2.1.0] - 2026-08-13
- area (room) awareness — "the kitchen light" resolves to the entity without asking
- direct control of entities (turn on/off, set brightness, temperature, ...)

## [2.0.0] - 2026-08-13
- rework of the code

## [1.0.0] - 2026-04-28

### Added
- Initial release as Home Assistant add-on
