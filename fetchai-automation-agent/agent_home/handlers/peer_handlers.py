"""Handlers for the agent-to-agent protocol (core/peer_protocol.py).

Every decision about another agent is made here in code, from the sender's
verified address and core/peers.py — never by the model, which never sees these
messages. A request is recorded and announced to the household once; nothing is
answered for an agent a person has not approved; and a blocked agent is not
answered at all.

A proposal (`propose` level) is vetted here (`peer_protocol.vet`: watched
entities only, their domains' non-sensitive services, no wide targets), then
raised through core/gate.py's `propose` like any other sessionless question —
the gate test-creates it, journals it and puts it to a person. The agent learns
the outcome from tasks/peer_outcomes.py. Nothing an agent writes goes into the
outbox: the notice is built from the checked service and entity ids alone.
"""
import asyncio
import logging
import time

from uagents import Context

from core import activity
from core import auditor
from core import disclosure
from core import gate
from core import observation
from core import pairing
from core import peers
from core import record
from core import watchlist
from core.peer_protocol import (
    peer_proto, ConnectRequest, ConnectStatus, Query, QueryResult, ActionProposal, ActionStatus,
    Disconnect, answer, notice, vet,
)
from rest import rest_peers
from tools.tool_homeassistant import registry
from tools.tool_homeassistant._client import _ha_get

logger = logging.getLogger(__name__)

# Queries per approved agent per rolling hour. In memory on purpose: it only
# has to stop a runaway loop, and a restart that forgets it costs one hour's
# budget, not a security property.
MAX_QUERIES_PER_HOUR = 60
_recent = {}


def _over_budget(address, now) -> bool:
    stamps = [t for t in _recent.get(address, []) if now - t < 3600]
    over = len(stamps) >= MAX_QUERIES_PER_HOUR
    if not over:
        stamps.append(now)
    _recent[address] = stamps
    return over


def _short(address) -> str:
    return f"{address[:14]}…{address[-6:]}" if len(address) > 24 else address


def announce_request(address):
    """One outbox message per agent that asks, for whichever channel carries it.

    Only the address: the name and purpose the agent gave are its own words,
    and the outbox is what ASI1's "what's new" hands to the model. The panel
    shows them, as the agent's claims."""
    body = (f"An agent ({_short(address)}) asks to connect to this home. Its request, in its "
            f"own words, is in the panel's Connections tab — approve, deny or block it there.")
    activity.queue("peer_request", title="An agent asks to connect", body=body,
                   payload={"address": address})


@peer_proto.on_message(ConnectRequest, replies={ConnectStatus})
async def handle_connect(ctx: Context, sender: str, msg: ConnectRequest):
    state = peers.request(sender, msg.name, msg.purpose, msg.level,
                          is_person=pairing.is_paired(sender))
    logger.info(f"PEERS: connect request from {sender} -> {state}")
    if state == peers.BLOCKED:
        return
    if state == peers.PENDING and peers.was_new(sender):
        announce_request(sender)
        peers.mark_notified(sender)
    detail = {
        "refused": "This address cannot connect as an agent.",
        "full": "Too many requests are waiting; try again later.",
        peers.PENDING: "Waiting for the household to approve.",
    }.get(state, "")
    await ctx.send(sender, ConnectStatus(state=state, level=peers.level_of(sender) or "",
                                         detail=detail))


@peer_proto.on_message(Disconnect, replies={ConnectStatus})
async def handle_disconnect(ctx: Context, sender: str, msg: Disconnect):
    """The panel's remove, asked for by the agent itself — which is why only a
    pending or approved agent can do it (core/peers.leave). An approved one
    leaving is news to the household, from the address alone."""
    was = peers.state_of(sender)
    if was == peers.APPROVED:
        await asyncio.to_thread(rest_peers.withdraw_open, sender,
                                "the agent ended its connection")
    state = peers.leave(sender, is_person=pairing.is_paired(sender))
    logger.info(f"PEERS: {sender} disconnects ({was} -> {state})")
    if state == peers.BLOCKED:
        return
    if was == peers.APPROVED and state == "disconnected":
        activity.queue("peer_left", title="An agent ended its connection",
                       body=(f"An agent ({_short(sender)}) ended its own connection to this home. "
                             f"It can no longer ask or propose anything; to come back it would "
                             f"have to ask again, and be approved again."),
                       payload={"address": sender})
    detail = {
        "disconnected": "No access to this home remains. A new ConnectRequest would need approving.",
        "refused": "This address cannot connect as an agent.",
        peers.DENIED: "The household denied this agent; only the household can change that.",
    }.get(state, "")
    await ctx.send(sender, ConnectStatus(state=state, detail=detail))


@peer_proto.on_message(Query, replies={QueryResult, ConnectStatus})
async def handle_query(ctx: Context, sender: str, msg: Query):
    level = peers.level_of(sender)
    if not peers.may_read(level):
        state = peers.state_of(sender)
        logger.info(f"PEERS: refused a query from {sender} (state {state})")
        if state != peers.BLOCKED:
            await ctx.send(sender, ConnectStatus(
                state=state or "unknown",
                detail="Not approved. Send a ConnectRequest and wait for the household."))
        return
    now = int(time.time())
    if _over_budget(sender, now):
        await ctx.send(sender, QueryResult(ok=False, kind=msg.kind,
                                           error=f"over {MAX_QUERIES_PER_HOUR} queries an hour"))
        return
    peers.touch(sender, now)

    result = answer(msg.kind, msg.entity_ids, msg.days,
                    watched=watchlist.watched_entities(),
                    last_state=record.last_state,
                    profile=lambda entity_id, days: record.profile(entity_id, days=days),
                    name_for=observation.name_for)
    logger.info(f"PEERS: {sender} asked {msg.kind} -> ok={result['ok']}")
    if result["ok"]:
        await ctx.send(sender, QueryResult(ok=True, kind=msg.kind,
                                           data=disclosure.emit(result["data"])))
    else:
        await ctx.send(sender, QueryResult(ok=False, kind=msg.kind, error=result["error"]))


def _house():
    """Home Assistant's states, services and automations — what `vet` checks a
    proposal against. Blocking; run off the loop. (None, reason) on failure."""
    ok, snapshot = registry.fetch_registry()
    if not ok:
        return None, f"could not read Home Assistant ({snapshot})"
    ok, services = _ha_get("/services")
    if not ok:
        return None, f"could not read Home Assistant's services ({services})"
    ok, automations = auditor._fetch_automation_configs()
    if not ok:
        return None, f"could not read the existing automations ({automations})"
    return ((snapshot.get("states") or {}), services, automations), ""


def _start_of_day(now) -> int:
    local = time.localtime(now)
    return int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)))


def raise_proposal(sender, msg, now=None) -> dict:
    """An approved `propose` agent's change, put to the household — the one
    path both ways in share: an ActionProposal message and the chat tool
    'Propose a change to the household' (core/peer_chat.py). Blocking (Home
    Assistant is read, the gate test-creates): run it off the loop. Returns
    {"state": "asked"|"refused", "change_id", "detail"}. The caller has checked
    the level; this checks everything else."""
    now = int(now if now is not None else time.time())
    counts = activity.peer_proposal_counts(sender, _start_of_day(now))
    if counts["open"] >= peers.MAX_OPEN_PROPOSALS:
        return {"state": "refused", "change_id": 0,
                "detail": f"{peers.MAX_OPEN_PROPOSALS} proposals already wait for an answer."}
    if counts["since"] >= peers.MAX_PROPOSALS_PER_DAY:
        return {"state": "refused", "change_id": 0,
                "detail": f"over {peers.MAX_PROPOSALS_PER_DAY} proposals today."}

    house, why = _house()
    if house is None:
        logger.warning(f"PEERS: could not vet a proposal from {sender}: {why}")
        return {"state": "refused", "change_id": 0, "detail": "The home could not check it now."}
    states, services, automations = house
    vetted = vet(msg, watched=watchlist.watched_entities(), states=states,
                 services=services, automations=automations)
    if not vetted["ok"]:
        logger.info(f"PEERS: refused a proposal from {sender} — {vetted['error']}")
        return {"state": "refused", "change_id": 0, "detail": disclosure.text(vetted["error"])}

    raised = gate.propose(vetted["tool_name"], vetted["tool_args"],
                          source=activity.PEER_SOURCE, actor=sender,
                          notice=notice(sender, vetted["tool_name"], vetted["tool_args"],
                                        vetted["entities"]))
    if not raised.get("ok"):
        logger.info(f"PEERS: the gate refused a proposal from {sender} — {str(raised.get('error'))[:200]}")
        return {"state": "refused", "change_id": 0,
                "detail": disclosure.text(str(raised.get("error"))[:300])}
    change_id = raised["change_id"]
    peers.add_proposal(sender, change_id, msg.title, msg.reason, now=now)
    logger.info(f"PEERS: {sender} proposed change {change_id}")
    return {"state": "asked", "change_id": change_id,
            "detail": "Put to the household; you will be told the outcome."}


@peer_proto.on_message(ActionProposal, replies={ActionStatus, ConnectStatus})
async def handle_action(ctx: Context, sender: str, msg: ActionProposal):
    level = peers.level_of(sender)
    if not peers.may_propose(level):
        state = peers.state_of(sender)
        logger.info(f"PEERS: refused a proposal from {sender} (state {state}, level {level})")
        if state != peers.BLOCKED:
            await ctx.send(sender, ActionStatus(
                state="refused", detail="Not approved to propose changes to this home."))
        return
    result = await asyncio.to_thread(raise_proposal, sender, msg)
    await ctx.send(sender, ActionStatus(**result))
