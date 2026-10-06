"""core/uagent.py — what the startup registration writes to Agentverse.

The call writes the agent's whole profile, and whatever it leaves out is
overwritten rather than kept. Two parts were lost that way: the readme
(`readme or ""` blanked the page on every start, 2.28.3) and every protocol but
chat (`register_chat_agent` hard-codes the chat digest, and Agentverse lists
the agent with what this call says, not the Almanac attestation — HomePeer was
missing from the agent's page, 2.29.0). The call is replaced here by a
recorder — the real one talks to Agentverse."""
import asyncio
import logging
import types

import pytest

from core import settings
from core import uagent

# Well-formed digests (uagents_core checks the shape when it is real).
CHAT = "proto:" + "a" * 64
HOMEPEER = "proto:" + "b" * 64


@pytest.fixture
def registered(monkeypatch):
    calls = []
    monkeypatch.setattr(uagent, "register_agent",
                        lambda request, config, credentials: calls.append(request))
    monkeypatch.setattr(uagent.agent_1, "protocols", {CHAT: object(), HOMEPEER: object()})
    monkeypatch.setattr(settings, "AGENTVERSE_API_KEY", "test-key")
    monkeypatch.setattr(settings, "system_message", settings.system_message)
    monkeypatch.setattr(settings, "agentCtx", getattr(settings, "agentCtx", None))
    ctx = types.SimpleNamespace(logger=logging.getLogger("test_agentverse_profile"))

    asyncio.run(uagent.process_startup(ctx))

    assert len(calls) == 1
    return calls[0]


def test_the_registration_sends_the_readme_file(registered):
    with open(uagent._README_PATH, encoding="utf-8") as f:
        expected = f.read()
    assert expected.strip()
    assert registered.readme == expected


def test_the_registration_lists_every_included_protocol(registered):
    assert registered.protocols == [CHAT, HOMEPEER]
