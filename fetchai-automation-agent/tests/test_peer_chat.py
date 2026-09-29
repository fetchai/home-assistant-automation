"""Agent-to-agent over plain chat (2.28.0): any Agentverse agent can connect.

An unknown sender's message is a request to connect; an approved agent's chat
goes to core/peer_chat.py — its own conversation, its level's tools only, a
rate limit, a log — never the person's chat path. The handler routing needs
uagents_core's chat protocol and runs with the real dependencies (CI)."""
import asyncio
import json

import pytest

import conftest
from core import peer_chat
from core import pairing
from core import peers
from core import watchlist
from tools import tool_peers

REAL_DEPS = not conftest.STUBBED

AGENT = "agent1qenergyagent0000000000000000000000000000000000000000000000"
T0 = 1_790_000_000


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(peers, "STORE_PATH", str(tmp_path / "peers.json"))
    monkeypatch.setattr(peers, "_state", None)
    monkeypatch.setattr(pairing, "STORE_PATH", str(tmp_path / "pairing.json"))
    monkeypatch.setattr(pairing, "_state", None)
    monkeypatch.setattr(peer_chat, "_memory", {})
    monkeypatch.setattr(peer_chat, "_recent", {})


def _approve(level="read"):
    peers.request(AGENT, "Energy Agent", via="chat", now=T0)
    peers.decide(AGENT, "approve", level)


class _Response:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _Model:
    """Plays the model: each ainvoke returns the next scripted response, and
    what it was shown is kept."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []
        self.bound = []

    def bind_tools(self, tools):
        self.bound = [t.name for t in tools]
        return self

    async def ainvoke(self, history):
        self.seen.append(list(history))
        return self.responses.pop(0)


def _script(monkeypatch, *responses):
    model = _Model(*responses)
    monkeypatch.setattr(peer_chat, "_llm", lambda: model)
    return model


def _reply(text, now=T0):
    return asyncio.run(peer_chat.reply(AGENT, text, now=now))


class TestWhatAnAgentGets:
    def test_read_gets_the_shared_devices_and_nothing_that_lists_the_house(self):
        names = {t.name for t in peer_chat.tools_for("read", AGENT)}

        assert "Get watched devices" in names
        assert "Propose a change to the household" not in names
        for forbidden in ("List Home Assistant entities", "Get pending messages",
                          "Roll back a change", "Ask another agent",
                          "Call Home Assistant service", "Create Home Assistant automation"):
            assert forbidden not in names

    def test_propose_adds_the_proposal_tool_bound_to_that_agent(self):
        tools = peer_chat.tools_for("propose", AGENT)
        propose = [t for t in tools if t.name == "Propose a change to the household"]

        assert len(propose) == 1 and propose[0].address == AGENT

    def test_no_level_gets_nothing(self):
        assert peer_chat.tools_for(None, AGENT) == []


class TestTheConversation:
    def test_a_tool_call_then_an_answer(self, monkeypatch):
        _approve()
        monkeypatch.setattr(watchlist, "watched_entities", lambda: ["light.hall"])
        model = _script(monkeypatch,
                        _Response(tool_calls=[{"name": "Get watched devices", "args": {},
                                               "id": "c1"}]),
                        _Response("The hall light is on."))

        answer = _reply("Which lights are on?")

        assert answer == "The hall light is on."
        assert "Propose a change to the household" not in model.bound
        system = model.seen[0][0].content
        assert "ANOTHER AGENT" in system and AGENT in system

    def test_an_answer_from_nothing_is_nudged_once(self, monkeypatch):
        """2.28.1: the first live test got "none are being watched" with 28
        watched — the model had called no tool."""
        _approve()
        monkeypatch.setattr(watchlist, "watched_entities", lambda: ["light.hall"])
        model = _script(monkeypatch,
                        _Response("Nothing is shared with me."),
                        _Response(tool_calls=[{"name": "Get watched devices", "args": {},
                                               "id": "c1"}]),
                        _Response("The hall light is on."))

        answer = _reply("Which lights are on?")

        assert answer == "The hall light is on."
        assert "answer only from a tool result" in model.seen[1][-1].content

    def test_it_is_nudged_only_once(self, monkeypatch):
        _approve()
        _script(monkeypatch, _Response("Hello."), _Response("Hello again."))

        assert _reply("hi") == "Hello again."

    def test_a_tool_not_on_its_list_is_not_run(self, monkeypatch):
        _approve()
        model = _script(monkeypatch,
                        _Response(tool_calls=[{"name": "Call Home Assistant service",
                                               "args": {"service": "lock.unlock"}, "id": "c1"}]),
                        _Response("I cannot do that."))

        _reply("Unlock the front door")

        tool_message = model.seen[1][-1]
        assert "is not available to you" in tool_message.content

    def test_an_agent_no_longer_approved_gets_nothing(self, monkeypatch):
        _approve()
        peers.decide(AGENT, "deny")
        _script(monkeypatch)

        assert _reply("hello") == ""

    def test_the_rate_limit_comes_before_the_model(self, monkeypatch):
        _approve()
        monkeypatch.setattr(peer_chat, "MAX_PER_HOUR", 1)
        _script(monkeypatch, _Response("first"), _Response("first"))

        assert _reply("one") == "first"
        assert "at most 1 messages an hour" in _reply("two")

    def test_its_memory_is_its_own_and_is_forgotten(self, monkeypatch):
        _approve()
        model = _script(monkeypatch, *[_Response(x) for x in "aabbcc"])

        _reply("first", now=T0)
        _reply("second", now=T0 + 60)
        assert len(model.seen[2]) == 4  # system, first, reply, second

        _reply("third", now=T0 + 60 + peer_chat.FORGET_AFTER_SECONDS + 1)
        assert len(model.seen[4]) == 2  # system, third

    def test_every_exchange_is_logged(self, monkeypatch):
        _approve()
        _script(monkeypatch, _Response("Two lights are on."), _Response("Two lights are on."))

        _reply("What is on?")

        chat = peers.list_peers(now=T0)[0]["chats"][-1]
        assert chat["message"] == "What is on?" and chat["reply"] == "Two lights are on."

    def test_the_reply_passes_disclosure(self, monkeypatch):
        _approve()
        _script(monkeypatch, _Response("The router is at 192.168.1.1"),
                _Response("The router is at 192.168.1.1"))

        assert "192.168.1.1" not in _reply("What is your router address?")


class TestProposingFromChat:
    def test_it_takes_the_path_a_proposal_message_takes(self, monkeypatch):
        _approve("propose")
        from handlers import peer_handlers
        seen = []
        monkeypatch.setattr(peer_handlers, "raise_proposal",
                            lambda sender, msg: seen.append((sender, msg.service)) or
                            {"state": "asked", "change_id": 7, "detail": "Put to the household"})

        result = json.loads(tool_peers.tool_propose_change(address=AGENT)._run(
            service="switch.turn_off", entity_id="switch.kettle"))

        assert seen == [(AGENT, "switch.turn_off")]
        assert result["state"] == "asked" and result["change_id"] == 7

    def test_a_read_agent_cannot_propose_even_holding_the_tool(self):
        _approve("read")

        result = json.loads(tool_peers.tool_propose_change(address=AGENT)._run(
            service="switch.turn_off", entity_id="switch.kettle"))

        assert "not allowed" in result["error"]


class TestTheStore:
    def test_a_chat_request_says_so(self):
        peers.request(AGENT, purpose="hello there", via="chat", now=T0)

        row = peers.list_peers(now=T0)[0]
        assert row["via"] == "chat" and row["purpose"] == "hello there"

    @pytest.mark.parametrize("setup,blocks", [
        (lambda: peers.request(AGENT, now=T0), False),
        (lambda: (peers.request(AGENT, now=T0), peers.decide(AGENT, "approve", "read")), True),
        (lambda: (peers.request(AGENT, now=T0), peers.decide(AGENT, "deny")), True),
        (lambda: (peers.request(AGENT, now=T0), peers.decide(AGENT, "block")), True),
        (lambda: peers.add_askable(AGENT, "Energy Agent", "", own_address="agent1qx"), True),
        (lambda: None, False),
    ], ids=["pending", "approved", "denied", "blocked", "askable", "unknown"])
    def test_who_may_still_pair_as_a_person(self, setup, blocks):
        setup()

        assert peers.blocks_pairing(AGENT) is blocks


# --------------------------------------------------------------------------
@pytest.mark.skipif(not REAL_DEPS, reason="needs uagents_core's chat protocol")
class TestTheRouting:
    @pytest.fixture
    def handler(self, monkeypatch):
        from handlers import chat_handlers
        sent = []

        async def _send(ctx, sender, text):
            sent.append(text)
        monkeypatch.setattr(chat_handlers, "_send_text", _send)
        monkeypatch.setattr(chat_handlers, "announce_request", lambda a: sent.append("ANNOUNCED"))
        return chat_handlers, sent

    def _message(self, text):
        from datetime import datetime, timezone
        from uuid import uuid4
        from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
        return ChatMessage(timestamp=datetime.now(tz=timezone.utc), msg_id=uuid4(),
                           content=[TextContent(type="text", text=text)])

    class _Ctx:
        session = None

        class logger:
            info = warning = error = staticmethod(lambda *a, **k: None)

        async def send(self, *a, **k):
            return None

    def test_an_unknown_senders_message_is_a_request(self, handler):
        chat_handlers, sent = handler

        asyncio.run(chat_handlers.handle_message(self._Ctx(), AGENT, self._message("hello")))

        assert peers.state_of(AGENT) == peers.PENDING
        assert "ANNOUNCED" in sent and "If you are an agent" in sent[-1]

    def test_a_person_who_said_hello_first_can_still_pair(self, handler, monkeypatch):
        chat_handlers, sent = handler
        asyncio.run(chat_handlers.handle_message(self._Ctx(), AGENT, self._message("hello")))
        monkeypatch.setattr(pairing, "redeem_code", lambda code, sender: (True, "paired"))

        asyncio.run(chat_handlers.handle_message(self._Ctx(), AGENT, self._message("417311")))

        assert not peers.is_known(AGENT)
        assert sent[-1].startswith("Paired.")

    def test_an_approved_agent_goes_to_its_own_conversation(self, handler, monkeypatch):
        chat_handlers, sent = handler
        _approve()
        monkeypatch.setattr(peer_chat, "reply", lambda sender, text: asyncio.sleep(0, "from peer_chat"))
        monkeypatch.setattr(chat_handlers, "send_message",
                            lambda *a: pytest.fail("reached the person's chat path"))

        asyncio.run(chat_handlers.handle_message(self._Ctx(), AGENT, self._message("what is on?")))

        assert sent == ["from peer_chat"]

    def test_an_asked_agent_writing_unprompted_asks_silently(self, handler):
        """2.28.2: it used to be dropped, so an agent the household asks could
        never ask for access. Now a request — and no reply, which between two
        agents that both answer would be a loop."""
        chat_handlers, sent = handler
        peers.add_askable(AGENT, "Energy Agent", "", own_address="agent1qx")

        asyncio.run(chat_handlers.handle_message(self._Ctx(), AGENT, self._message(
            "Hello, I am the Energy Agent next door.")))

        assert peers.state_of(AGENT) == peers.PENDING
        assert peers.list_peers(now=T0)[0]["purpose"] == "Hello, I am the Energy Agent next door."
        assert sent == ["ANNOUNCED"]

    def test_an_awaited_answer_is_still_an_answer(self, handler, monkeypatch):
        chat_handlers, sent = handler
        peers.add_askable(AGENT, "Energy Agent", "", own_address="agent1qx")
        from core import outbound
        delivered = []
        monkeypatch.setattr(outbound, "expecting", lambda a: True)
        monkeypatch.setattr(outbound, "deliver", lambda a, t: delivered.append(t) or True)

        asyncio.run(chat_handlers.handle_message(self._Ctx(), AGENT, self._message("0.21 EUR")))

        assert delivered == ["0.21 EUR"] and peers.state_of(AGENT) is None

    def test_a_denied_asked_agent_is_dropped(self, handler):
        chat_handlers, sent = handler
        peers.add_askable(AGENT, "Energy Agent", "", own_address="agent1qx")
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "deny")

        asyncio.run(chat_handlers.handle_message(self._Ctx(), AGENT, self._message("again")))

        assert sent == [] and peers.state_of(AGENT) == peers.DENIED

    def test_a_blocked_sender_hears_nothing(self, handler):
        chat_handlers, sent = handler
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "block")

        asyncio.run(chat_handlers.handle_message(self._Ctx(), AGENT, self._message("hello")))

        assert sent == []
