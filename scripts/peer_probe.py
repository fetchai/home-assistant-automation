#!/usr/bin/env python3
"""Play another agent against the live add-on's agent-to-agent protocol
(core/peer_protocol.py): ask to connect, then ask read-level questions.

WHY INSIDE THE CONTAINER
------------------------
The agent listens on 127.0.0.1 only (2.24.2), so the probe runs in the add-on's
own container and delivers straight to 127.0.0.1:8099/submit, synchronously —
uagents' `send_sync_message` returns the agent's reply in the same HTTP call, so
the probe needs no mailbox, no Almanac registration and no endpoint of its own.
It imports the message models from /app, so the digests are the add-on's own.

The probe is a real signed identity (an `agent1…` address from --seed), which is
what the agent checks; a different seed is a different agent.

    docker exec -i app_local_fetchai-automation-agent python3 - connect  < scripts/peer_probe.py
    # approve it in the panel's Connections tab, then:
    docker exec -i app_local_fetchai-automation-agent python3 - query entities < scripts/peer_probe.py
    docker exec -i app_local_fetchai-automation-agent python3 - query state light.x < scripts/peer_probe.py
    # with "read + propose": a service call, put to the household as a question
    docker exec -i app_local_fetchai-automation-agent python3 - propose light.turn_off light.x < scripts/peer_probe.py
    # plain chat, as any Agentverse agent speaks (2.28.0): the first message is a
    # request to connect; once approved, messages are answered by core/peer_chat.
    # The sync reply is only the acknowledgement — read the answer in the panel
    # (the agent's chat log) or the add-on log.
    docker exec -i app_local_fetchai-automation-agent python3 - chat "Which lights are on?" < scripts/peer_probe.py

Not part of the add-on; stdlib plus the uagents the image already has.
"""
import argparse
import asyncio
import json
import sys
import urllib.request

sys.path.insert(0, "/app")

from uagents.communication import send_sync_message  # noqa: E402
from uagents.crypto import Identity  # noqa: E402
from uagents.resolver import RulesBasedResolver  # noqa: E402

from core.peer_protocol import ActionProposal, ConnectRequest, Query  # noqa: E402

AGENT = "http://127.0.0.1:8099"


def _agent_address() -> str:
    req = urllib.request.Request(f"{AGENT}/rest/pairing/status", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)["agent_address"]


async def _send(identity, message):
    target = _agent_address()
    resolver = RulesBasedResolver({target: f"{AGENT}/submit"})
    return await send_sync_message(target, message, sender=identity, resolver=resolver, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", default="ceelabs peer probe — not a real agent")
    parser.add_argument("--name", default="Peer probe")
    parser.add_argument("--purpose", default="Testing the agent-to-agent protocol.")
    parser.add_argument("command", choices=("connect", "query", "propose", "chat", "address"))
    parser.add_argument("kind", nargs="?", default="entities",
                        help="query: entities|state|profile; propose: the domain.service")
    parser.add_argument("entity_ids", nargs="*")
    args = parser.parse_args()

    identity = Identity.from_seed(args.seed, 0)
    print(f"probe address: {identity.address}")
    if args.command == "address":
        return
    if args.command == "connect":
        message = ConnectRequest(name=args.name, purpose=args.purpose, level="propose")
    elif args.command == "chat":
        from datetime import datetime, timezone
        from uuid import uuid4
        from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
        message = ChatMessage(timestamp=datetime.now(tz=timezone.utc), msg_id=uuid4(),
                              content=[TextContent(type="text", text=args.kind)])
    elif args.command == "propose":
        message = ActionProposal(action="call_service", service=args.kind,
                                 entity_id=",".join(args.entity_ids),
                                 title="Probe proposal", reason="Testing phase 2.")
    else:
        message = Query(kind=args.kind, entity_ids=args.entity_ids)
    reply = asyncio.run(_send(identity, message))
    print(f"reply: {reply}")


if __name__ == "__main__":
    main()
