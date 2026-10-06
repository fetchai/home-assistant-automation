"""REST handlers behind the panel's Connections tab: which other agents may work
with this home.

This is the only place an agent becomes trusted, and it is reachable only
through Home Assistant ingress (proxy.py answers the Supervisor alone), so every
approval here is a person who logged into Home Assistant — the same reason the
pairing code for people is issued from the panel. The agent is told the outcome
at once when it can be reached; if the send fails it learns on its next message,
which is answered from the same store.

The other direction lives here too: the agents THIS home may ask (phase 3),
added and removed only from the panel.

An agent's proposals are answered through /rest/approve by change_id, like
every sessionless question; this module only lists them with what the agent
said about each. Lowering an agent below `propose` — read only, deny, block,
remove — withdraws the proposals it still has open: a question from someone
the household no longer lets ask must not stay answerable.
"""
import asyncio
import logging

from uagents import Context

from core import activity
from core import gate
from core import pairing
from core import peers
from core import protocols
from core.peer_protocol import ConnectStatus
from core.uagent import agent_1

logger = logging.getLogger(__name__)

_ERRORS = {
    "not_agent": "That is not an agent address (agent1…).",
    "self": "That is this agent's own address.",
    "no_name": "Give the agent a name, so you and the agent can tell it apart.",
    "unknown": "That agent is not in the list any more.",
    "bad_level": "That level cannot be granted.",
    "bad_action": "Unknown action.",
    "is_person": "That address is paired as a person, so it cannot also be an agent.",
}


def _open_proposals() -> list:
    """Each agent's proposals still waiting for a person, oldest first, with
    what the agent said (its words, for the person deciding) and what the gate
    recorded it would do."""
    out = []
    for peer in peers.list_peers():
        for change_id in peers.unreported(peer["address"]):
            row = activity.change(change_id) or {}
            if row.get("decision") != "proposed":
                continue
            entry = peers.proposal(peer["address"], change_id) or {}
            out.append({
                "change_id": change_id, "address": peer["address"], "name": peer.get("name", ""),
                "title": entry.get("title", ""), "reason": entry.get("reason", ""),
                "summary": gate._describe(row.get("tool", ""), row.get("args") or {}),
                "new_config": row.get("new_config") or "",
                "proposed_at": entry.get("proposed_at") or row.get("proposed_at"),
            })
    return sorted(out, key=lambda p: p["proposed_at"] or 0)


def withdraw_open(address, why) -> int:
    """Withdraws an agent's open proposals. Returns how many. Also the path
    when an agent disconnects itself (handlers/peer_handlers.handle_disconnect)."""
    withdrawn = 0
    for change_id in peers.unreported(address):
        if (activity.change(change_id) or {}).get("decision") == "proposed":
            withdrawn += gate.withdraw(change_id, why)
    return withdrawn


def _response(error="") -> protocols.PeersResponse:
    return protocols.PeersResponse(peers=peers.list_peers(), levels=list(peers.LEVELS),
                                   proposals=_open_proposals(), error=error)


@agent_1.on_rest_post("/rest/peers/list", protocols.PeersRequest, protocols.PeersResponse)
async def handle_peers_list(ctx: Context, req: protocols.PeersRequest) -> protocols.PeersResponse:
    return _response()


@agent_1.on_rest_post("/rest/peers/decide", protocols.PeersRequest, protocols.PeersResponse)
async def handle_peers_decide(ctx: Context, req: protocols.PeersRequest) -> protocols.PeersResponse:
    """Approve at a level, deny, or block. A blocked agent is not told."""
    ok, reason = peers.decide(req.address, req.action, req.level,
                              is_person=pairing.is_paired(req.address))
    logger.info(f"PEERS: {req.action} {req.address} -> {reason}")
    if not ok:
        return _response(_ERRORS.get(reason, reason))
    if not peers.may_propose(peers.level_of(req.address)):
        await asyncio.to_thread(withdraw_open, req.address,
                                "the household no longer lets that agent propose changes")
    if reason != peers.BLOCKED and ctx is not None:
        try:
            await ctx.send(req.address, ConnectStatus(state=reason,
                                                      level=peers.level_of(req.address) or ""))
        except Exception as e:
            logger.warning(f"PEERS: could not tell {req.address} it was {reason}: {e}")
    return _response()


@agent_1.on_rest_post("/rest/peers/remove", protocols.PeersRequest, protocols.PeersResponse)
async def handle_peers_remove(ctx: Context, req: protocols.PeersRequest) -> protocols.PeersResponse:
    """Forgets an agent: its access ends now, its open proposals are withdrawn,
    and it may ask again."""
    await asyncio.to_thread(withdraw_open, req.address, "the household removed that agent")
    peers.remove(req.address)
    return _response()


@agent_1.on_rest_post("/rest/peers/ask/add", protocols.PeersRequest, protocols.PeersResponse)
async def handle_peers_ask_add(ctx: Context, req: protocols.PeersRequest) -> protocols.PeersResponse:
    """Lets this home ask an agent: the one place one is added. `name` and
    `purpose` are the person's words; the model reads them to choose whom to
    ask ('Ask another agent')."""
    ok, reason = peers.add_askable(req.address.strip(), req.name, req.purpose,
                                   confirm=req.confirm, own_address=agent_1.address,
                                   is_person=pairing.is_paired(req.address.strip()))
    logger.info(f"PEERS: ask/add {req.address} -> {reason}")
    return _response("" if ok else _ERRORS.get(reason, reason))


@agent_1.on_rest_post("/rest/peers/ask/remove", protocols.PeersRequest, protocols.PeersResponse)
async def handle_peers_ask_remove(ctx: Context, req: protocols.PeersRequest) -> protocols.PeersResponse:
    """Stops this home asking an agent. Its access to this home, if any, stays."""
    peers.remove_askable(req.address)
    return _response()


@agent_1.on_rest_post("/rest/peers/ask/confirm", protocols.PeersRequest, protocols.PeersResponse)
async def handle_peers_ask_confirm(ctx: Context, req: protocols.PeersRequest) -> protocols.PeersResponse:
    """Whether each question to this agent waits for the person's YES."""
    peers.set_confirm(req.address, req.confirm)
    logger.info(f"PEERS: ask/confirm {req.address} -> {req.confirm}")
    return _response()
