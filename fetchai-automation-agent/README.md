# Fetch.ai Automation Agent — Home Assistant Add-on

A Home Assistant add-on that adds a natural-language assistant to your home. You describe in plain
language what you want — *"turn off the kitchen light"*, *"turn the porch light on at sunset and off at
midnight"* — and the agent resolves which entities you meant, acts on them right away, or proposes an
automation and creates it for you after you confirm.

The agent knows about your **areas (rooms)**, so *"the kitchen light"* is resolved to the actual entity
when there is only one light in that room — it asks which one you meant only when there really are several.

The agent is built on the [Fetch.ai uAgents framework](https://docs.fetch.ai/uagents/) and reasons with the
[ASI-1](https://asi1.ai) LLM. It registers itself on [Agentverse](https://agentverse.ai), so you can talk to
it both from the built-in panel in Home Assistant and from any ASI1/Agentverse chat client.

## What it can do

- **Control your devices** — turn entities on, off or toggle them, set brightness, temperature, volume and
  any other Home Assistant service, immediately
- **Understand rooms** — resolves "the kitchen light", "the bedroom" to real entities through the Home
  Assistant area registry, including entities that inherit their room from their device
- **Propose automations** — describe a goal ("make the house look occupied in the evening") and the agent
  suggests concrete automations in plain language before writing anything
- **Create automations** — builds a valid Home Assistant automation config and activates it immediately
- **Modify automations** — fetches an existing automation, changes what you asked for, and writes it back
- **Delete automations** — removes automations you no longer want
- **Explain your home** — lists areas, entities, devices and services, and explains what an existing
  automation does
- **Confirm before risky changes** — every automation create/update/delete is shown to you first, with a
  YAML diff of what will change, and only runs after you accept
- **Answer only to you** — the agent acts on messages from ASI1 only after you have paired that identity
  from the panel; see [Pairing](#pairing)

Everyday actions (lights, switches, media, climate) run straight away, because asking twice for "turn off
the kitchen light" is more annoying than useful. Actions that are not trivially undone — locks, alarm,
covers/valves, notifications and Home Assistant itself — go through the same confirmation step as
automation changes.

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**
2. Click the menu (⋮) in the top right and select **Repositories**
3. Add the repository URL and click **Add**
4. Find **Fetch.ai Automation Agent** in the store and click **Install**

## Configuration

| Option | Required | Description |
|--------|----------|-------------|
| `asi1_api_key` | Yes | API key for the ASI-1 LLM the agent reasons with — the agent registers to ASI1 with it |
| `asi1_model` | No | ASI-1 model identifier: `asi1-mini`, `asi1`, or `asi1-ultra` (default: `asi1-mini`) |
| `agent_name` | No | Internal agent identifier used for registration (default: `HAAutomationAgent`) |
| `agent_display_name` | No | A display name for the agent (default: `HA Automation Agent`) — not used by the agent at present |
| `agentverse_api_key` | No | Agentverse mailbox API key — set it to reach the agent from ASI1/Agentverse chat |
| `analyst_enabled` | No | The daily analyst (default: on) — see [What leaves your home](#what-leaves-your-home) |
| `digest_day` | No | The day of the week the analyst's ideas arrive, up to three at a time (default: `saturday`) |

Every option is also described on the add-on's **Configuration** tab.

The add-on uses the Home Assistant timezone; there is nothing else to configure.

## Panel

Once the add-on is running, open it from the **Fetch.ai Automation Agent** item in the Home Assistant
sidebar (or the **Open Web UI** button on the add-on page). The panel is a single chat dialog with the
agent. When the agent wants to create, change or delete an automation, the message box is replaced by a
confirmation bar with a `+/-` badge — click it to review the full YAML diff — and **Accept** / **Reject**
buttons.

The panel follows your system's light or dark preference, and loads nothing from the internet, so it works
on a Home Assistant host with no external network access.

## Pairing

An agent's address on Agentverse is public, so the agent will not act on a chat message until you have
bound the sender to your home. This is done from the panel, because Home Assistant is the one place you are
already signed in.

1. Open the panel. The strip at the top shows **No devices paired**, along with the agent's own address
   and a **Copy** button — that address is what you send messages to in ASI1.
2. Click **Pair a device** — a six-digit code appears, valid for 10 minutes.
3. Send that code to the agent from ASI1 (just the six digits).
4. The agent confirms, and the strip switches to **1 device paired**.

Until then, an ASI1 message gets nothing back but an instruction to pair — the agent does no reasoning and
touches nothing in your home. A code can only be used once, and it is cancelled after five wrong attempts.

Several household members can pair, and any paired member can approve automation changes. The panel lists
the paired identities and can remove any of them; unpairing also discards that identity's conversation and
any confirmation it had waiting. Pairings are stored in the add-on's `/data` directory, so they survive
restarts and rebuilds.

**The panel itself is never gated** — it is already behind Home Assistant's own authentication, and works
whether or not anything is paired.

The agent's address is also written to the add-on log at every start, next to the number of paired
identities, so `ha addons logs local_fetchai-automation-agent` is enough to tell whether the agent is
reachable and whether anyone is allowed to use it.

## Other agents

Other Fetch.ai agents can ask to work with your home — for example another add-on that wants
to know what your devices are doing. They are kept apart from the people who pair from ASI1:

- **Any agent can ask, just by writing to this agent in chat** — the Fetch.ai chat protocol every
  Agentverse agent speaks; nothing needs building on its side. Its first message becomes a request in
  the panel's **Connections** tab (with that message, as the agent's own words), and you get a message.
  A person on ASI1 who has not paired yet looks the same from here, so they are also told how to pair
  with a code, and pairing clears their request. An agent that wants a structured link can instead send
  a `ConnectRequest` over the `HomePeer 0.3.0` protocol. Until you approve it, an agent gets
  nothing. A request nobody answers expires after a week.
- You **approve** it at one of two levels, **deny** it or **block** it there. Approval is only possible
  in the panel, behind your Home Assistant login. Removing an approved agent ends its access at once.
  An agent can also end its own access with a `Disconnect` message; you get a message if it was
  approved. A denied or blocked agent cannot use it to clear your decision.
- **Read:** the agent can ask about the state and activity of the devices you watch — nothing else,
  and never a device you do not watch. In chat, this agent answers it with a model that has only those
  read tools, in a conversation kept apart from yours; every exchange is kept in the tab. At most 20
  messages an hour.
- **Read + propose:** it can also propose a change to those devices — a new automation, or a service
  call such as turning a switch off. Each proposal waits in the Connections tab, with what the agent
  says about it and exactly what would be written, until you approve or reject it; the agent is told
  the outcome. A proposal may only touch devices you watch, only with those devices' own services, never
  a lock, alarm, cover, valve, notification or Home Assistant itself, and never a whole area or device.
  An agent may have 3 proposals waiting and make 5 a day. Lowering its level, blocking or removing it
  withdraws what it still has waiting. Proposals are answered in the panel only.
- An agent can never use your conversation or your tools, and never approve anything. Nothing it
  writes is put in the messages the agent hands to ASI1.
- **The risk you accept with chat:** an approved agent's text reaches the model, so it may try to talk
  it into something (prompt injection). What limits it is not the model's good sense but what that
  conversation can do: read the devices you share, and — at read + propose — put a proposal to you,
  which you decide. Messages over the `HomePeer` protocol are answered by fixed code and never
  reach the model at all.

**Asking other agents.** The other direction: in the same tab, under **Agents this home can ask**, you
add an agent by its address, a name and what it is for — a tariff or weather agent, say. In chat the
agent may then put a question to it (`Ask another agent`) when that helps with what you asked, and tells
you what it answered and which agent said it. It can ask only the agents you listed; each question
leaves through the same filter as everything else and is at most 1,000 characters; the answer is treated
as information, never as an instruction; and every question and answer is kept in the tab. The other
agent is asked over the Fetch.ai chat protocol, and its reply counts only while the agent is waiting for
it — anything else it sends is dropped. Blocking an agent stops asking it too.

**Ask me before each question** — on by default when you add an agent, and switchable in its row. Use it
for an agent that can act: one that buys energy, pays, or changes another home acts on what it is asked,
and this agent's own approval step only guards changes to your home. With it on, the agent shows you the
exact question in chat and sends it only after you reply **YES**; the other agent's answer then comes
straight to you. The reply is read by fixed code, never the model, and a question not answered within
10 minutes is dropped. Turn it off for an agent that only answers (weather, prices).
- Only agents with a signed `agent1…` address can connect, and an address paired as a person cannot
  also connect as an agent.

## What leaves your home

The agent thinks with ASI-1, a model that runs outside your home at [asi1.ai](https://asi1.ai). Whatever
the agent reads in order to answer is sent there. Home Assistant's history and the agent's own record are
never uploaded, copied or synced — what leaves are the answers read from them.

**In chat**, when you ask something: your messages; the names, rooms, current states and details of the
devices it looks at; your automations when it reads or changes them. For a question about the past it
reads either Home Assistant's own history of that device — up to 200 changes over up to 10 days per
question — or what the agent has recorded about the devices you chose to watch.

**The daily analyst** runs once a day after 07:00, with nobody in the conversation, and is on by
default. It looks for habits an automation could take over and keeps its ideas for a weekly
digest: on `digest_day`, up to three of them are put to you in the panel.

- It sees the house's **inventory** the way chat does — every device's name, room and current state, and
  your automations — so that its ideas name real devices.
- The **history** it reads is only that of the devices you chose in the panel's Watching tab, from the
  agent's own record. It does not read Home Assistant's history. A device you stop watching is not shown
  to it, though its past stays in the record until it ages out (after 90 days by default).
- The model **searches that record itself**, and the answers are sent as they come back — individual changes with their times, not only summaries. One search
  returns at most 500 rows and 32 KB (100 rows unless it asks for more); one timeline covers at most 5
  devices over at most 21 days. A run makes at most 20 searches and timelines together, at most 48 tool
  calls in all, and at most 16 calls to the model. It starts from an overview of the record (the watched devices, how many changes
  each has and its last state), your existing automations and the ideas already open.
- It also sends your last 20 answers to its earlier ideas (at most 8 KB of them): each idea's
  title, whether you said yes, no, or applied it and rolled it back, and the reason you typed after
  **No, because…** — so that it stops raising what you turned down. An answer that did not take effect
  is not sent.
- It can also read ready-made summaries of each watched device — which hours and days it changes, how
  long it stays on — with its recent changes (up to 200 per device), and which devices tend to change
  together.
- Each idea's card in the panel has a **What was sent** list: every answer the agent received
  in that run — searches, timelines, summaries, device lists — with what it asked and the start of what
  came back (up to 2 KB of each). The opening overview and your automations were sent as well, and the
  card says so.

**To an approved agent** (see [Other agents](#other-agents)): the names, current states and activity
profiles of watched devices it asks about, through the same filter.

**To an agent you added for asking:** the questions the agent writes to it in chat, through the same
filter — listed, with the answers, in the Connections tab.

**Never sent:** passwords, tokens and keys; network addresses (IPs, MACs, hostnames,
URLs); your home's coordinates; and large unfiltered attribute data. Every answer passes through one
filter before it can leave. Names are sent as you wrote them — a device called "Anna's room" arrives as
"Anna's room".

**To stop it:** set `analyst_enabled: false`, and nothing is sent unless you are chatting with the
agent.

## Example requests

- "What lights do I have in the living room?"
- "Turn off the kitchen light."
- "Dim the living room lamps to 30%."
- "What rooms do I have?"
- "Turn on the porch light at sunset and off at 23:00."
- "Every weekday at 6:30 open the bedroom blinds, but only if nobody is asleep."
- "List my automations."
- "What does the 'Morning routine' automation do?"
- "Change the morning routine to start at 7:00 instead of 6:30."
- "Delete the automation that turns off the heating."

## Architecture

```
HA Ingress (port 8098 — answers the Supervisor only)
    └── proxy.py
            ├── GET  /*       → serves the built Vue panel (panel/dist)
            ├── GET  /health  → the watchdog's check, forwarded to the agent
            └── POST /rest/*  → forwards to the agent (port 8099)

Agent (127.0.0.1:8099 — reachable only from inside the add-on, uAgents framework)
    ├── /rest/post, /rest/approve  → chat + approval flow used by the panel
    ├── /rest/pairing/*            → issue a code, list and remove paired identities
    ├── chat protocol              → same flow for ASI1 / Agentverse clients,
    │                                after the sender has been paired
    └── LLM tool loop (ASI-1)
            ├── read-only:  list areas / entities in an area (HA websocket registries)
            ├── read-only:  list entities / entity details / devices / services / automations
            ├── read-only:  current datetime
            ├── direct:     call service on entities (light.turn_on, switch.toggle, ...)
            └── confirmed:  create / update / delete automation, sensitive service calls
```

The agent's REST endpoints carry no credentials of their own, so the add-on publishes no port: the
only way in is Home Assistant ingress, behind your Home Assistant login. Inside the host, the agent
listens on loopback only and the proxy refuses every caller but the Supervisor, so other add-ons cannot
reach it either.

Areas come from Home Assistant's area, device and entity registries, which are only reachable over the
websocket API — an entity's room is taken from the entity itself when set, otherwise from its device.

Every automation config is preflight-validated by test-creating it in Home Assistant before you are asked
to approve it, so an invalid config is corrected by the agent instead of reaching you.

## Requirements

- Home Assistant OS or Supervised installation
- An ASI-1 API key (from [asi1.ai](https://asi1.ai))
- An Agentverse API key (from [agentverse.ai](https://agentverse.ai)) if you want to chat with the agent
  from outside Home Assistant

## License

Released under the [PolyForm Noncommercial License 1.0.0](https://github.com/fetchai/home-assistant-automation/blob/main/LICENSE.md):
free for personal and other noncommercial use; commercial use needs a separate license.
