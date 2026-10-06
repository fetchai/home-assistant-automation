"""Agent-to-agent, phase 1 (2.25.0): core/peers.py, core/peer_protocol.answer,
handlers/peer_handlers.py and rest/rest_peers.py.

An agent is trusted only after a person approves it in the panel, never answers
YES to anything, is never the same address as a paired person, and learns only
about watched entities — by code, never through the model."""
import asyncio
import json

import pytest

from core import activity
from core import pairing
from core import peers
from core import protocols
from core import watchlist
from core.peer_protocol import (
    ConnectRequest, ConnectStatus, Disconnect, Query, QueryResult, answer, MAX_PROFILE_DAYS,
)
from handlers import peer_handlers
from rest import rest_peers

AGENT = "agent1qenergyagent0000000000000000000000000000000000000000000000"
OTHER = "agent1qsomeoneelse000000000000000000000000000000000000000000000"
USER = "user1qunsignedsender00000000000000000000000000000000000000000000"
T0 = 1_790_000_000


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(peers, "STORE_PATH", str(tmp_path / "peers.json"))
    monkeypatch.setattr(peers, "_state", None)
    monkeypatch.setattr(pairing, "STORE_PATH", str(tmp_path / "pairing.json"))
    monkeypatch.setattr(pairing, "_state", None)
    monkeypatch.setattr(peer_handlers, "_recent", {})
    queued = []
    monkeypatch.setattr(activity, "queue", lambda kind, **kw: queued.append((kind, kw)))
    return queued


def _run(coro):
    return asyncio.run(coro)


class _Ctx:
    """What a handler sends, in order, instead of the network."""

    def __init__(self):
        self.sent = []

    async def send(self, to, msg):
        self.sent.append((to, msg))


# --------------------------------------------------------------------------
class TestTheStore:
    def test_a_first_request_is_pending(self):
        assert peers.request(AGENT, "Energy Agent", "tariffs", "read", now=T0) == peers.PENDING
        assert peers.level_of(AGENT) is None

    def test_approval_grants_the_level(self):
        peers.request(AGENT, now=T0)

        assert peers.decide(AGENT, "approve", "read") == (True, peers.APPROVED)
        assert peers.level_of(AGENT) == "read"

    @pytest.mark.parametrize("level", ["admin", "approve", ""])
    def test_only_a_known_level_can_be_granted(self, level):
        peers.request(AGENT, now=T0)

        assert peers.decide(AGENT, "approve", level) == (False, "bad_level")
        assert peers.level_of(AGENT) is None

    def test_an_unsigned_address_is_refused(self):
        """uagents verifies agent1… envelopes only; user1… could be anyone."""
        assert peers.request(USER, now=T0) == "refused"
        assert not peers.is_known(USER)

    def test_a_person_cannot_also_be_an_agent(self):
        assert peers.request(AGENT, is_person=True, now=T0) == "refused"
        peers.request(OTHER, now=T0)
        assert peers.decide(OTHER, "approve", "read", is_person=True) == (False, "is_person")

    def test_asking_again_changes_no_decision(self):
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "deny")

        assert peers.request(AGENT, "new name", now=T0 + 10) == peers.DENIED
        assert peers.request(OTHER, now=T0) == peers.PENDING
        peers.decide(OTHER, "block")
        assert peers.request(OTHER, now=T0 + 10) == peers.BLOCKED

    def test_a_repeated_request_refreshes_one_row(self):
        peers.request(AGENT, "old", now=T0)
        peers.request(AGENT, "new", now=T0 + 100)

        rows = peers.list_peers(now=T0 + 100)
        assert len(rows) == 1
        assert rows[0]["name"] == "new"

    def test_an_unanswered_request_expires(self):
        peers.request(AGENT, now=T0)

        assert peers.list_peers(now=T0 + peers.PENDING_TTL_SECONDS) == []

    def test_but_an_approval_does_not(self):
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "approve", "read", now=T0)

        assert peers.list_peers(now=T0 + 10 * peers.PENDING_TTL_SECONDS)[0]["state"] == peers.APPROVED

    def test_waiting_requests_are_capped(self):
        for i in range(peers.MAX_PENDING):
            assert peers.request(f"agent1q{i:060d}", now=T0) == peers.PENDING

        assert peers.request(AGENT, now=T0) == "full"

    def test_what_an_agent_says_about_itself_is_clipped(self):
        peers.request(AGENT, "x" * 500, "y\n" * 500, now=T0)

        row = peers.list_peers(now=T0)[0]
        assert len(row["name"]) == peers.NAME_MAX
        assert len(row["purpose"]) == peers.PURPOSE_MAX
        assert "\n" not in row["purpose"]

    def test_removal_ends_access_and_allows_asking_again(self):
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "approve", "read")

        assert peers.remove(AGENT)
        assert peers.level_of(AGENT) is None
        assert peers.request(AGENT, now=T0) == peers.PENDING

    @pytest.mark.parametrize("approve", [True, False])
    def test_an_agent_can_end_its_own_access(self, approve):
        peers.request(AGENT, now=T0)
        if approve:
            peers.decide(AGENT, "approve", "propose")

        assert peers.leave(AGENT) == "disconnected"
        assert not peers.is_known(AGENT)
        assert peers.level_of(AGENT) is None

    @pytest.mark.parametrize("decision", ["deny", "block"])
    def test_but_cannot_wipe_a_household_decision(self, decision):
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, decision)

        assert peers.leave(AGENT) == peers.state_of(AGENT)
        assert peers.state_of(AGENT) in (peers.DENIED, peers.BLOCKED)

    def test_leaving_keeps_the_households_own_entry(self):
        peers.add_askable(AGENT, "Energy Agent", "tariffs")
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "approve", "read")

        assert peers.leave(AGENT) == "disconnected"

        assert peers.level_of(AGENT) is None
        assert peers.state_of(AGENT) is None
        assert peers.may_be_asked(AGENT)

    def test_leaving_unknown_is_already_done_and_strangers_are_refused(self):
        assert peers.leave(AGENT) == "disconnected"
        assert peers.leave(USER) == "refused"
        assert peers.leave(AGENT, is_person=True) == "refused"
        assert not peers.is_known(AGENT)

    def test_a_corrupt_store_trusts_nobody(self, tmp_path):
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "approve", "read")
        (tmp_path / "peers.json").write_text("{not json")
        peers._state = None

        assert peers.level_of(AGENT) is None
        assert peers.list_peers(now=T0) == []

    def test_the_decision_survives_a_restart(self):
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "approve", "read")
        peers._state = None

        assert peers.level_of(AGENT) == "read"


# --------------------------------------------------------------------------
LAST = {"light.hall": {"state": "on", "ts": T0}, "sensor.power": {"state": "12.5", "ts": T0 - 60}}


def _answer(kind, ids=(), days=7, watched=("light.hall", "sensor.power")):
    return answer(kind, list(ids), days, watched=watched,
                  last_state=LAST.get,
                  profile=lambda e, d: {"by_hour": [1] * 24, "by_day": [2] * 7, "events": 9,
                                        "on_fraction": 0.4, "mean_on_duration": 600, "days": d,
                                        "changes_to_state": {"on": 5}},
                  name_for=lambda e: f"name of {e}")


class TestWhatAnApprovedAgentIsTold:
    def test_entities_lists_the_watched_ones_only(self):
        result = _answer("entities")

        assert [e["entity_id"] for e in result["data"]["entities"]] == ["light.hall", "sensor.power"]
        assert result["data"]["entities"][0] == {
            "entity_id": "light.hall", "name": "name of light.hall", "state": "on", "since": T0}

    def test_an_unwatched_entity_is_named_back_and_never_looked_up(self):
        looked = []
        result = answer("state", ["light.hall", "lock.front_door"], 7,
                        watched={"light.hall"},
                        last_state=lambda e: looked.append(e) or LAST.get(e),
                        profile=None, name_for=str)

        assert result["data"]["not_shared"] == ["lock.front_door"]
        assert looked == ["light.hall"]

    def test_a_profile_is_bounded_and_trimmed(self):
        result = _answer("profile", ["light.hall"], days=400)

        profile = result["data"]["profile"]
        assert profile["days"] == MAX_PROFILE_DAYS
        assert "changes_to_state" not in profile

    def test_a_profile_of_an_unwatched_entity_is_nothing(self):
        result = _answer("profile", ["lock.front_door"])

        assert result["data"] == {"profile": None, "not_shared": ["lock.front_door"]}

    @pytest.mark.parametrize("kind,ids,error", [
        ("services", (), "unknown kind"), ("state", (), "entity_ids is empty")])
    def test_bad_questions_are_errors(self, kind, ids, error):
        result = _answer(kind, ids)

        assert result["ok"] is False
        assert error in result["error"]

    def test_nothing_is_told_when_nothing_is_watched(self):
        assert _answer("entities", watched=())["data"]["entities"] == []


# --------------------------------------------------------------------------
class TestTheHandlers:
    def test_a_request_is_pending_and_announced_once(self, isolated):
        ctx = _Ctx()
        msg = ConnectRequest(name="Energy Agent", purpose="tariffs", level="read")

        _run(peer_handlers.handle_connect(ctx, AGENT, msg))
        _run(peer_handlers.handle_connect(ctx, AGENT, msg))

        assert [m.state for _, m in ctx.sent] == [peers.PENDING, peers.PENDING]
        assert [kind for kind, _ in isolated] == ["peer_request"]
        # The outbox reaches the model through ASI1's "what's new": the agent's
        # own words stay in the panel.
        body = isolated[0][1]["body"]
        assert "Energy Agent" not in body and "tariffs" not in body
        assert AGENT[:14] in body

    def test_a_blocked_agent_hears_nothing(self):
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "block")
        ctx = _Ctx()

        _run(peer_handlers.handle_connect(ctx, AGENT, ConnectRequest()))
        _run(peer_handlers.handle_query(ctx, AGENT, Query(kind="entities")))
        _run(peer_handlers.handle_disconnect(ctx, AGENT, Disconnect()))

        assert ctx.sent == []
        assert peers.state_of(AGENT) == peers.BLOCKED

    def test_an_approved_agent_disconnects(self, isolated, monkeypatch):
        peers.request(AGENT, "Energy Agent", "tariffs", now=T0)
        peers.decide(AGENT, "approve", "propose")
        withdrawn = []
        monkeypatch.setattr(rest_peers, "withdraw_open", lambda a, why: withdrawn.append(a))
        isolated.clear()
        ctx = _Ctx()

        _run(peer_handlers.handle_disconnect(ctx, AGENT, Disconnect()))

        assert ctx.sent[0][1].state == "disconnected"
        assert withdrawn == [AGENT]
        assert not peers.is_known(AGENT)
        # Announced from the address alone, like a request.
        assert [kind for kind, _ in isolated] == ["peer_left"]
        body = isolated[0][1]["body"]
        assert "Energy Agent" not in body and "tariffs" not in body
        assert AGENT[:14] in body

    def test_a_pending_agent_withdraws_quietly(self, isolated):
        peers.request(AGENT, now=T0)
        isolated.clear()
        ctx = _Ctx()

        _run(peer_handlers.handle_disconnect(ctx, AGENT, Disconnect()))

        assert ctx.sent[0][1].state == "disconnected"
        assert isolated == []

    def test_a_denied_agent_stays_denied(self):
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "deny")
        ctx = _Ctx()

        _run(peer_handlers.handle_disconnect(ctx, AGENT, Disconnect()))

        assert ctx.sent[0][1].state == peers.DENIED
        assert peers.state_of(AGENT) == peers.DENIED

    def test_a_paired_person_cannot_connect_as_an_agent(self, monkeypatch):
        monkeypatch.setattr(pairing, "is_paired", lambda a: a == AGENT)
        ctx = _Ctx()

        _run(peer_handlers.handle_connect(ctx, AGENT, ConnectRequest()))

        assert ctx.sent[0][1].state == "refused"
        assert not peers.is_known(AGENT)

    def test_a_query_before_approval_is_refused(self):
        peers.request(AGENT, now=T0)
        ctx = _Ctx()

        _run(peer_handlers.handle_query(ctx, AGENT, Query(kind="entities")))

        assert isinstance(ctx.sent[0][1], ConnectStatus)
        assert ctx.sent[0][1].state == peers.PENDING

    def test_an_approved_agent_is_answered_through_disclosure(self, monkeypatch):
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "approve", "read")
        monkeypatch.setattr(watchlist, "watched_entities", lambda: ["light.hall"])
        monkeypatch.setattr(peer_handlers.record, "last_state", LAST.get)
        monkeypatch.setattr(peer_handlers.observation, "name_for", lambda e: "Hall light")
        emitted = []
        real_emit = peer_handlers.disclosure.emit
        monkeypatch.setattr(peer_handlers.disclosure, "emit",
                            lambda payload: emitted.append(payload) or real_emit(payload))
        ctx = _Ctx()

        _run(peer_handlers.handle_query(ctx, AGENT, Query(kind="entities")))

        result = ctx.sent[0][1]
        assert isinstance(result, QueryResult) and result.ok
        assert json.loads(result.data)["entities"][0]["name"] == "Hall light"
        assert len(emitted) == 1

    def test_queries_are_rate_limited(self, monkeypatch):
        peers.request(AGENT, now=T0)
        peers.decide(AGENT, "approve", "read")
        monkeypatch.setattr(watchlist, "watched_entities", lambda: [])
        monkeypatch.setattr(peer_handlers, "MAX_QUERIES_PER_HOUR", 2)
        ctx = _Ctx()

        for _ in range(3):
            _run(peer_handlers.handle_query(ctx, AGENT, Query(kind="entities")))

        assert [m.ok for _, m in ctx.sent] == [True, True, False]


# --------------------------------------------------------------------------
class TestThePanel:
    def test_approve_tells_the_agent(self):
        peers.request(AGENT, now=T0)
        ctx = _Ctx()

        res = _run(rest_peers.handle_peers_decide(
            ctx, protocols.PeersRequest(address=AGENT, action="approve", level="read")))

        assert res.error == ""
        assert res.peers[0]["state"] == peers.APPROVED
        assert ctx.sent[0][1].state == peers.APPROVED
        assert ctx.sent[0][1].level == "read"

    def test_block_is_silent(self):
        peers.request(AGENT, now=T0)
        ctx = _Ctx()

        _run(rest_peers.handle_peers_decide(ctx, protocols.PeersRequest(address=AGENT,
                                                                        action="block")))

        assert ctx.sent == []

    def test_an_unreachable_agent_does_not_undo_the_decision(self):
        peers.request(AGENT, now=T0)

        class Down:
            async def send(self, to, msg):
                raise ConnectionError("mailbox unreachable")

        res = _run(rest_peers.handle_peers_decide(
            Down(), protocols.PeersRequest(address=AGENT, action="approve", level="read")))

        assert res.error == ""
        assert peers.level_of(AGENT) == "read"

    def test_errors_are_said_in_words(self):
        res = _run(rest_peers.handle_peers_decide(
            _Ctx(), protocols.PeersRequest(address=OTHER, action="approve", level="read")))

        assert "not in the list" in res.error

    def test_remove(self):
        peers.request(AGENT, now=T0)

        res = _run(rest_peers.handle_peers_remove(None, protocols.PeersRequest(address=AGENT)))

        assert res.peers == []
        assert res.levels == list(peers.LEVELS)


# --------------------------------------------------------------------------
import conftest  # noqa: E402

REAL_DEPS = not conftest.STUBBED


@pytest.mark.skipif(not REAL_DEPS, reason="needs the real uagents package")
def test_the_protocol_publishes_a_manifest_with_both_exchanges():
    """What Agentverse gets when main.py includes it with publish_manifest."""
    from core.peer_protocol import peer_proto

    manifest = peer_proto.manifest()

    assert manifest["metadata"]["name"] == "HomePeer"
    assert manifest["metadata"]["version"] == "0.3.0"
    names = {m["schema"]["title"] for m in manifest["models"]}
    assert {"ConnectRequest", "ConnectStatus", "Query", "QueryResult", "Disconnect"} <= names


@pytest.mark.skipif(not REAL_DEPS, reason="needs the real uagents package")
def test_the_protocol_does_not_have_the_digest_agentverse_named_before():
    """Agentverse keeps the first name it was given for a digest, and 0.2.0's
    is stored under the old one — a digest it has never seen is the only way
    the name reaches it. Any change to the interactions that lands back on this
    digest brings the old name back."""
    from core.peer_protocol import peer_proto

    assert peer_proto.digest != (
        "proto:274f774225efe4e0ae26e675c5f46b979fee7a6bacdd052596438fb59397942a")


@pytest.mark.skipif(not REAL_DEPS, reason="needs the real uagents package")
def test_no_message_carries_a_description_into_its_digest():
    """uagents matches a message by the sha256 of its JSON schema, and pydantic
    puts a class docstring there. 0.1.0 had them, so another implementation had
    to copy our docstrings verbatim; 0.2.0 has none."""
    from core import peer_protocol as pp

    for model in (pp.ConnectRequest, pp.ConnectStatus, pp.Query, pp.QueryResult,
                  pp.ActionProposal, pp.ActionStatus, pp.Disconnect):
        assert "description" not in model.schema(), model.__name__


@pytest.mark.skipif(not REAL_DEPS, reason="needs the real uagents package")
def test_another_implementation_needs_only_the_name_and_the_fields():
    """What someone writing the other side declares — no docstring — digests
    the same as ours."""
    from uagents import Model
    from core import peer_protocol as pp

    theirs = type("ConnectRequest", (Model,), {
        "__annotations__": {"name": str, "purpose": str, "level": str},
        "name": "", "purpose": "", "level": "read", "__module__": "someone_else"})

    assert Model.build_schema_digest(theirs) == Model.build_schema_digest(pp.ConnectRequest)
