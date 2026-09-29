"""Which uAgent identities this home has bound itself to.

A uAgent address is public and discoverable, and this agent is registered on
Agentverse, so anyone who knows the address can send it a chat message. An
agent that writes automations into someone's house cannot take instructions
from whoever turns up: the chat handler checks every sender against the
bindings kept here, and the approval gate never even sees an unpaired one.

The binding is established from inside the home — the panel issues a short
code over Home Assistant ingress (where the user is already authenticated) and
the user sends that code from ASI1. Nothing outside the house can start a
pairing.

State lives in /data, not in `ctx.storage`: the uAgent store writes to the
process working directory (/app), which is part of the image and is wiped by
every add-on rebuild. /data is the persistent volume — the same reason run.sh
keeps the agent seed there.
"""
from datetime import datetime, timezone
import json
import logging
import os
import secrets

from core import settings

logger = logging.getLogger(__name__)

STORE_PATH = os.path.join(settings.DATA_DIR, "pairing.json")

# A code is read out of the panel and typed into another app, so it is short.
# Six digits only stays safe because it expires and because _MAX_ATTEMPTS
# destroys it long before the keyspace can be walked.
CODE_TTL_SECONDS = 600
_MAX_ATTEMPTS = 5

# Loaded once and written through on every change. This process is the only
# writer, so the file never has to be re-read.
_state = None


def _now():
    return int(datetime.now(tz=timezone.utc).timestamp())


def _empty_state():
    return {"version": 1, "paired": [], "pending_code": None}


def _load():
    """Returns the cached state, reading it from disk on first use.

    A missing, unreadable or corrupt file yields an empty state rather than an
    exception: losing the bindings must fail closed — nobody paired, and the
    user re-pairs from the panel — never open."""
    global _state
    if _state is not None:
        return _state
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("paired"), list):
            raise ValueError("unexpected structure")
        data.setdefault("version", 1)
        data.setdefault("pending_code", None)
        _state = data
    except FileNotFoundError:
        _state = _empty_state()
    except Exception as e:
        logger.warning(f"PAIRING: could not read {STORE_PATH} ({e}) — starting with no bindings")
        _state = _empty_state()
    return _state


def _save():
    """Writes the cached state to disk atomically, so an interrupted write
    cannot leave a half-written file that the next start would discard."""
    state = _load()
    tmp = f"{STORE_PATH}.tmp"
    try:
        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STORE_PATH)
    except Exception as e:
        logger.error(f"PAIRING: failed to persist {STORE_PATH}: {e}")


def _live_code(state):
    """Returns the pending code entry if one exists and has not expired."""
    pending = state.get("pending_code")
    if not pending:
        return None
    if _now() >= pending.get("expires_at", 0):
        return None
    return pending


def is_paired(address: str) -> bool:
    """True if this uAgent address has been bound to this home."""
    if not address:
        return False
    return any(p.get("address") == address for p in _load()["paired"])


def list_paired() -> list:
    """Returns the bound addresses, newest first, for the panel to display."""
    return sorted(_load()["paired"], key=lambda p: p.get("paired_at", 0), reverse=True)


def issue_code() -> dict:
    """Generates a fresh pairing code, replacing any code already outstanding,
    and returns it as {code, expires_at}."""
    state = _load()
    code = f"{secrets.randbelow(1000000):06d}"
    state["pending_code"] = {
        "code": code,
        "expires_at": _now() + CODE_TTL_SECONDS,
        "attempts": 0,
    }
    _save()
    logger.info(f"PAIRING: issued a new pairing code, valid for {CODE_TTL_SECONDS}s")
    return {"code": code, "expires_at": state["pending_code"]["expires_at"]}


def current_code():
    """Returns the outstanding {code, expires_at} if one is still valid, else
    None. Lets the panel redisplay a live code instead of issuing a new one
    every time it refreshes."""
    pending = _live_code(_load())
    if not pending:
        return None
    return {"code": pending["code"], "expires_at": pending["expires_at"]}


def redeem_code(code: str, address: str) -> tuple:
    """Binds `address` to this home if `code` is the outstanding pairing code.

    Returns (ok, reason) where reason is one of "paired", "already_paired",
    "no_code", "expired", "wrong" or "locked_out" — the caller turns that into
    something the user reads.

    The code is single-use: a successful redemption clears it, and so does
    running out of attempts, which is what keeps a six-digit secret from being
    guessed over the chat protocol."""
    if not address:
        return False, "wrong"
    if is_paired(address):
        return True, "already_paired"

    state = _load()
    pending = _live_code(state)
    if not pending:
        # Distinguishing "expired" from "never issued" tells the user whether to
        # look at the panel again or ask for a code in the first place.
        reason = "expired" if state.get("pending_code") else "no_code"
        state["pending_code"] = None
        _save()
        return False, reason

    if not secrets.compare_digest(str(code), pending["code"]):
        pending["attempts"] = pending.get("attempts", 0) + 1
        remaining = _MAX_ATTEMPTS - pending["attempts"]
        if remaining <= 0:
            state["pending_code"] = None
            _save()
            logger.warning(f"PAIRING: code cancelled after {_MAX_ATTEMPTS} failed attempts")
            return False, "locked_out"
        _save()
        logger.warning(f"PAIRING: wrong code from {address} ({remaining} attempts left)")
        return False, "wrong"

    state["paired"].append({"address": address, "paired_at": _now()})
    state["pending_code"] = None
    _save()
    logger.info(f"PAIRING: bound {address} to this home")
    return True, "paired"


def unpair(address: str) -> bool:
    """Removes a binding. True if the address was bound."""
    state = _load()
    remaining = [p for p in state["paired"] if p.get("address") != address]
    if len(remaining) == len(state["paired"]):
        return False
    state["paired"] = remaining
    _save()
    logger.info(f"PAIRING: unbound {address}")
    return True
