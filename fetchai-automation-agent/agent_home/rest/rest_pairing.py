"""REST handlers backing the panel's pairing strip: issue a code, read the
current bindings, remove one.

These are the in-home half of the pairing flow — they are reachable only
through Home Assistant ingress, where the user is already authenticated, which
is what makes a code shown here trustworthy as proof that whoever sends it back
over ASI1 is in the house. They are POST even where GET would read more
naturally: proxy.py answers GET from the built panel directory and only
forwards POST to the agent.
"""
import logging

from core import protocols
from core import pairing
from core.agent_runtime import drop_session
from core.uagent import agent_1

from uagents import Context

logger = logging.getLogger(__name__)


def _status() -> protocols.PairingStatusResponse:
    """Builds the reply every pairing endpoint returns, so the panel gets fresh
    state from whichever one it called."""
    live = pairing.current_code()
    return protocols.PairingStatusResponse(
        agent_address=agent_1.address,
        paired=pairing.list_paired(),
        code=live["code"] if live else "",
        code_expires_at=live["expires_at"] if live else 0,
    )


@agent_1.on_rest_post("/rest/pairing/status", protocols.PairingRequest, protocols.PairingStatusResponse)
async def handle_pairing_status(ctx: Context, req: protocols.PairingRequest) -> protocols.PairingStatusResponse:
    """Returns the bound addresses and any code still awaiting redemption."""
    return _status()


@agent_1.on_rest_post("/rest/pairing/code", protocols.PairingRequest, protocols.PairingStatusResponse)
async def handle_pairing_code(ctx: Context, req: protocols.PairingRequest) -> protocols.PairingStatusResponse:
    """Issues a fresh pairing code, invalidating any code already outstanding."""
    pairing.issue_code()
    return _status()


@agent_1.on_rest_post("/rest/pairing/unpair", protocols.PairingRequest, protocols.PairingStatusResponse)
async def handle_pairing_unpair(ctx: Context, req: protocols.PairingRequest) -> protocols.PairingStatusResponse:
    """Removes one binding and forgets that identity's conversation.

    Dropping the session matters as much as dropping the binding: it is where a
    parked destructive approval lives, and an approval the user has just
    revoked access for must not stay answerable."""
    if pairing.unpair(req.address):
        drop_session(req.address)
    return _status()
