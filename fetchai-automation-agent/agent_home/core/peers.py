"""Which other uAgents this home works with, and how far each is trusted.

A person reaches this agent through ASI1 chat and is bound with a code from the
panel (core/pairing.py). Another AGENT is a different kind of caller and is kept
in a different list, on purpose: a paired person can answer YES to a change, and
an agent must never be able to — it would be approving its own request. So the
two lists never share an address, an agent never reaches the chat path's model
or tools, and the only way an agent is trusted at all is a person approving its
request in the panel, behind Home Assistant's login.

An address is a real identity here: uagents verifies the signature of every
envelope from an `agent1…` address before dispatching it (uagents/mailbox.py,
`env.verify()`). It does NOT verify `user1…` senders, so this module refuses
them outright — an unsigned address could be anyone's.

Levels are what an approved agent may do. `read` answers a fixed set of
questions about WATCHED entities only (the same bound the analyst has), by code,
never by the model. `propose` adds proposing a change — a new automation or a
service call on watched entities — which goes through core/gate.py's `propose`
and is decided by a person in the panel like any other question. An agent at
any level never approves anything.

Each proposal's change_id is kept on its agent until the agent has been told
how it ended; the journal (core/activity.py) holds the change itself and is
what the limits on proposals are counted from.

The other direction (phase 3): an agent THIS home may ask. Only a person adds
one, in the panel, with a name and what it is for (`ask`, their own words — the
model reads them to choose whom to ask). Asking is independent of the agent's
own access to this home: an entry may carry `ask`, an inbound `state`, or both.
Every question sent and answer received is kept on the entry (`asked`, the
newest QUESTIONS_KEPT), which is what the panel shows as the record of what
left the house.

An agent reaches this home either way (2.28.0): the typed protocol, or plain
chat — what any Agentverse agent speaks. `via` records which. A chat request is
any unknown sender's non-code message; a person on ASI1 looks the same, so a
PENDING request never stops that person pairing with a code
(`blocks_pairing`). An approved agent's chat is answered by core/peer_chat.py,
and each exchange is kept on the entry (`chats`, the newest CHATS_KEPT).

State lives in /data like pairing.json and fails closed the same way: a missing
or corrupt file means no agent is trusted, never that all are.
"""
from datetime import datetime, timezone
import json
import logging
import os

from core import settings

logger = logging.getLogger(__name__)

STORE_PATH = os.path.join(settings.DATA_DIR, "peers.json")

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
BLOCKED = "blocked"

#: What an approved agent may be granted, least first. Each includes the one
#: before it.
LEVELS = ("read", "propose")

# At most this many of one agent's proposals wait for a person at once, and at
# most this many a day: a person is asked, and asking is not free.
MAX_OPEN_PROPOSALS = 3
MAX_PROPOSALS_PER_DAY = 5

# Proposals whose outcome has been told are kept for the panel's history of
# that agent, the newest this many.
_KEEP_REPORTED = 20

# A request is someone knocking, not a relationship: it is dropped if nobody
# answers it within a week, and at most this many wait at once, so an agent
# spraying requests from fresh addresses cannot bury the one that matters.
PENDING_TTL_SECONDS = 7 * 86400
MAX_PENDING = 20

# What an agent says about itself is shown to a person and nothing else; it is
# cut to a length a panel row can hold.
NAME_MAX = 60
PURPOSE_MAX = 300

_USER_PREFIX = "user"

ASK_PURPOSE_MAX = 300
QUESTIONS_KEPT = 20
CHATS_KEPT = 20

_state = None


def _now():
    return int(datetime.now(tz=timezone.utc).timestamp())


def _empty_state():
    return {"version": 1, "peers": []}


def _load():
    """The cached state, read from disk on first use. Unreadable means empty:
    losing the list must leave every agent untrusted (the person re-approves),
    never trusted."""
    global _state
    if _state is not None:
        return _state
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("peers"), list):
            raise ValueError("unexpected structure")
        data["peers"] = [p for p in data["peers"]
                         if isinstance(p, dict) and isinstance(p.get("address"), str)]
        _state = data
    except FileNotFoundError:
        _state = _empty_state()
    except Exception as e:
        logger.warning(f"PEERS: could not read {STORE_PATH} ({e}) — no agent is trusted")
        _state = _empty_state()
    return _state


def _save() -> bool:
    """Atomic write, as pairing.py does it."""
    tmp = f"{STORE_PATH}.tmp"
    try:
        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_load(), f, indent=2)
        os.replace(tmp, STORE_PATH)
        return True
    except OSError as e:
        logger.error(f"PEERS: failed to persist {STORE_PATH}: {e}")
        return False


def _find(address):
    for peer in _load()["peers"]:
        if peer["address"] == address:
            return peer
    return None


def _clip(value, limit) -> str:
    return " ".join(str(value or "").split())[:limit]


def _expire(now) -> bool:
    """Drops pending requests nobody answered in time. True if any went."""
    peers = _load()["peers"]
    kept = [p for p in peers
            if p.get("state") != PENDING
            or now - int(p.get("requested_at") or 0) < PENDING_TTL_SECONDS]
    if len(kept) == len(peers):
        return False
    _load()["peers"] = kept
    return True


def is_signed_address(address) -> bool:
    """Whether uagents verified this sender's signature — an agent address,
    not a user one."""
    return isinstance(address, str) and bool(address) and not address.startswith(_USER_PREFIX)


def request(address, name="", purpose="", level="", *, is_person=False, via="protocol",
            now=None) -> str:
    """Records an agent's request to connect. Returns the state to tell it:

    `pending` (new, or refreshed), `approved`, `denied`, `blocked` (the caller
    stays silent), `refused` (unsigned address, or an address paired as a
    person) or `full` (too many requests already waiting).

    A request from an approved or denied agent changes nothing — a decision is
    only undone from the panel. A repeated pending request refreshes what it
    says about itself and its expiry, so knocking twice is not two rows.
    """
    now = int(now if now is not None else _now())
    if not is_signed_address(address) or is_person:
        return "refused"
    changed = _expire(now)
    peer = _find(address)
    if peer and peer.get("state") in (APPROVED, DENIED, BLOCKED):
        if changed:
            _save()
        return peer["state"]
    if peer is None or not peer.get("state"):
        waiting = sum(1 for p in _load()["peers"] if p.get("state") == PENDING)
        if waiting >= MAX_PENDING:
            if changed:
                _save()
            return "full"
        if peer is None:
            peer = {"address": address}
            _load()["peers"].append(peer)
        # An agent this home only ASKS (added in the panel) that now asks for
        # access itself starts at pending like any other.
        peer.update(state=PENDING, level="")
    peer.update(name=_clip(name, NAME_MAX), purpose=_clip(purpose, PURPOSE_MAX),
                requested_level=_clip(level, 20), requested_at=now,
                via="chat" if via == "chat" else "protocol")
    _save()
    return PENDING


def was_new(address) -> bool:
    """Whether a pending request has not been announced to the household yet.
    A refreshed request is not news, so a person hears about each agent once."""
    peer = _find(address)
    return bool(peer) and peer.get("state") == PENDING and "notified_at" not in peer


def mark_notified(address, now=None):
    peer = _find(address)
    if peer is not None:
        peer["notified_at"] = int(now if now is not None else _now())
        _save()


def decide(address, action, level="read", *, is_person=False, now=None) -> tuple:
    """A person's answer from the panel: `approve` (at `level`), `deny` or
    `block`. Returns (ok, reason). Works on a known address in any state, so a
    denied agent can later be approved and an approved one blocked."""
    now = int(now if now is not None else _now())
    peer = _find(address)
    if peer is None:
        return False, "unknown"
    if action == "approve":
        if level not in LEVELS:
            return False, "bad_level"
        if is_person:
            return False, "is_person"
        peer.update(state=APPROVED, level=level, decided_at=now)
    elif action == "deny":
        peer.update(state=DENIED, level="", decided_at=now)
    elif action == "block":
        peer.update(state=BLOCKED, level="", decided_at=now)
    else:
        return False, "bad_action"
    _save()
    return True, peer["state"]


def remove(address) -> bool:
    """Forgets an agent entirely; it may ask again. True if it was known."""
    peers = _load()["peers"]
    kept = [p for p in peers if p["address"] != address]
    if len(kept) == len(peers):
        return False
    _load()["peers"] = kept
    _save()
    return True


def leave(address, *, is_person=False) -> str:
    """An agent ends its own access (`Disconnect`, protocol 0.3.0). Returns the
    state to tell it: `disconnected` (its request or approval is forgotten, or
    it had neither), `denied` or `blocked` (unchanged; the caller stays silent
    to a blocked one) or `refused` (unsigned, or an address paired as a person).

    It only ever narrows access, so no person is asked. But a denial or a block
    is the household's decision and is undone only from the panel — otherwise
    an agent could leave to wipe its record and knock again as news. What the
    household set up itself, the agent's place on the "this home can ask" list,
    stays; an entry with nothing else on it goes, as in `remove_askable`. The
    caller withdraws open proposals first: they go with the row."""
    if not is_signed_address(address) or is_person:
        return "refused"
    peer = _find(address)
    if peer and peer.get("state") in (DENIED, BLOCKED):
        return peer["state"]
    if peer is None or not peer.get("state"):
        return "disconnected"
    if "ask" in peer:
        kept = {key: peer[key] for key in ("address", "ask", "asked") if key in peer}
        peer.clear()
        peer.update(kept)
    else:
        _load()["peers"] = [p for p in _load()["peers"] if p["address"] != address]
    _save()
    return "disconnected"


def state_of(address):
    peer = _find(address)
    return peer.get("state") if peer else None


def level_of(address):
    """The approved level, or None: the one question every handler asks."""
    peer = _find(address)
    if peer and peer.get("state") == APPROVED and peer.get("level") in LEVELS:
        return peer["level"]
    return None


def approved_addresses() -> list:
    return [p["address"] for p in _load()["peers"] if p.get("state") == APPROVED]


def with_unreported() -> list:
    """Every agent — whatever its state now — with an outcome still to tell.
    A blocked or removed agent is never told; the caller skips blocked ones."""
    return [p["address"] for p in _load()["peers"]
            if any(not e.get("reported") for e in p.get("proposals", []))]


def may_read(level) -> bool:
    return level in LEVELS


def may_propose(level) -> bool:
    return level == "propose"


def add_proposal(address, change_id, title="", reason="", now=None) -> bool:
    """Links a change the gate raised to the agent that proposed it, with what
    it said about it — shown to the person deciding, never sent anywhere."""
    peer = _find(address)
    if peer is None:
        return False
    peer.setdefault("proposals", []).append({
        "change_id": int(change_id), "title": _clip(title, NAME_MAX),
        "reason": _clip(reason, PURPOSE_MAX),
        "proposed_at": int(now if now is not None else _now()), "reported": "",
    })
    return _save()


def unreported(address) -> list:
    """Change ids whose outcome this agent has not been told yet."""
    peer = _find(address) or {}
    return [p["change_id"] for p in peer.get("proposals", []) if not p.get("reported")]


def mark_reported(address, change_id, state) -> bool:
    peer = _find(address)
    if peer is None:
        return False
    for entry in peer.get("proposals", []):
        if entry["change_id"] == int(change_id):
            entry["reported"] = str(state)
    done = [p for p in peer.get("proposals", []) if p.get("reported")]
    waiting = [p for p in peer.get("proposals", []) if not p.get("reported")]
    peer["proposals"] = done[-_KEEP_REPORTED:] + waiting
    return _save()


def proposal(address, change_id):
    """The stored entry for one of this agent's proposals, or None."""
    for entry in (_find(address) or {}).get("proposals", []):
        if entry["change_id"] == int(change_id):
            return dict(entry)
    return None


def is_known(address) -> bool:
    return _find(address) is not None


def touch(address, now=None):
    """Stamps when an approved agent last asked something — at most once a
    minute, so a chatty agent does not turn every query into a file write."""
    now = int(now if now is not None else _now())
    peer = _find(address)
    if peer is not None and now - int(peer.get("last_seen") or 0) >= 60:
        peer["last_seen"] = now
        _save()


def add_askable(address, name, purpose, *, confirm=True, own_address="", is_person=False,
                now=None) -> tuple:
    """A person lets this home ask an agent. Returns (ok, reason).

    `confirm` (2.27.1): every question to this agent waits for the person's YES
    in chat before it is sent. For an agent that can ACT — buy, pay, change a
    home — a question is also a command, and this home's approval gate only
    guards changes to this home. On by default; a person turns it off for an
    agent that only answers."""
    if not is_signed_address(address) or not str(address).startswith("agent1"):
        return False, "not_agent"
    if address == own_address:
        return False, "self"
    if is_person:
        return False, "is_person"
    name = _clip(name, NAME_MAX)
    if not name:
        return False, "no_name"
    peer = _find(address)
    if peer is None:
        peer = {"address": address}
        _load()["peers"].append(peer)
    peer["ask"] = {"name": name, "purpose": _clip(purpose, ASK_PURPOSE_MAX),
                   "confirm": bool(confirm),
                   "added_at": int(now if now is not None else _now())}
    _save()
    return True, "added"


def remove_askable(address) -> bool:
    """Stops this home asking an agent; an entry with nothing else on it goes."""
    peer = _find(address)
    if peer is None or "ask" not in peer:
        return False
    del peer["ask"]
    if not peer.get("state"):
        _load()["peers"] = [p for p in _load()["peers"] if p["address"] != address]
    _save()
    return True


def set_confirm(address, confirm) -> bool:
    peer = _find(address)
    if peer is None or "ask" not in peer:
        return False
    peer["ask"]["confirm"] = bool(confirm)
    return _save()


def askable() -> list:
    """[{address, name, purpose, confirm}] of every agent a person has let this
    home ask — blocked ones excluded: blocking an agent stops the conversation
    both ways. An entry from before `confirm` existed reads as confirm: the
    side that asks the person."""
    return [{"address": p["address"], "name": p["ask"]["name"], "purpose": p["ask"]["purpose"],
             "confirm": p["ask"].get("confirm", True)}
            for p in _load()["peers"] if "ask" in p and p.get("state") != BLOCKED]


def find_askable(agent):
    """The askable agent a name or address refers to, or None. A name matches
    whole and case-insensitively; a model asking for "energy" does not get the
    Energy Agent by guess."""
    wanted = " ".join(str(agent or "").split()).casefold()
    for entry in askable():
        if wanted in (entry["address"].casefold(), entry["name"].casefold()):
            return entry
    return None


def blocks_pairing(address) -> bool:
    """Whether a pairing code from this address is refused: it is an agent the
    household has decided about (approved, denied, blocked) or asks. A PENDING
    request does not count — anyone's first chat message makes one, including
    a person about to pair."""
    peer = _find(address)
    if peer is None:
        return False
    return peer.get("state") in (APPROVED, DENIED, BLOCKED) or "ask" in peer


def log_chat(address, message, reply, now=None) -> bool:
    """Keeps an approved agent's chat message and this home's reply."""
    peer = _find(address)
    if peer is None:
        return False
    log = peer.setdefault("chats", [])
    log.append({"at": int(now if now is not None else _now()), "message": str(message),
                "reply": str(reply or "")})
    peer["chats"] = log[-CHATS_KEPT:]
    return _save()


def may_be_asked(address) -> bool:
    return any(e["address"] == address for e in askable())


def log_question(address, question, answer, error="", now=None) -> bool:
    """Keeps what was sent to an agent and what came back, for the panel."""
    peer = _find(address)
    if peer is None:
        return False
    log = peer.setdefault("asked", [])
    log.append({"at": int(now if now is not None else _now()), "question": str(question),
                "answer": str(answer or ""), "error": str(error or "")})
    peer["asked"] = log[-QUESTIONS_KEPT:]
    return _save()


def list_peers(now=None) -> list:
    """Every known agent for the panel: pending first (they are waiting on a
    person), then by most recent activity."""
    now = int(now if now is not None else _now())
    if _expire(now):
        _save()
    order = {PENDING: 0, APPROVED: 1, DENIED: 2, BLOCKED: 3, None: 4}
    peers = [dict(p) for p in _load()["peers"]]
    return sorted(peers, key=lambda p: (order.get(p.get("state"), 9),
                                        -int(p.get("last_seen") or p.get("requested_at") or 0)))
