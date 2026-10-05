"""The protocol other uAgents use to work with this home: typed messages, not
chat.

Chat is how a PERSON talks to the agent, and chat text goes to the model with
tools behind it. An agent's messages never do: every message here is a typed
model answered by code, so nothing another agent sends can reach a prompt, and
there is no instruction in it for anyone to follow. That is the whole reason
this is a protocol of its own rather than chat between agents.

Published to Agentverse with its manifest, so another developer can implement
the other side from the digest alone.

    ConnectRequest -> ConnectStatus   ask to be trusted; the answer is a state
    Query          -> QueryResult     an approved agent's read-level question
    ActionProposal -> ActionStatus    a propose-level agent's change, put to a
                                      person; later ActionStatus messages say how
                                      it ended (tasks/peer_outcomes.py)

No message model here has a docstring, on purpose (0.2.0). uagents matches a
message to its handler by `Model.build_schema_digest` — sha256 of the model's
JSON schema — and pydantic puts a class docstring into that schema as its
`description`. With one, every other implementation would have had to copy our
docstring character for character or have its messages go unrecognised; 0.1.0
did. Now the digest is the class name and its fields, which is the contract.
What each message means is in the comment above it.

`answer` is the pure half of a query: which entities may be told about, what is
said about each, and what is refused — tested over literals, with the record
and the names handed in. `vet` is the pure half of a proposal: whether it may be
put to a person at all, and as which gate tool with which arguments.
"""
import json
import re

from uagents import Model, Protocol

from core import auditor
from tools.tool_homeassistant import automations as automations_mod
from tools.tool_homeassistant.actions import is_sensitive_service

PROTOCOL_NAME = "Fetchai-HomePeer"
PROTOCOL_VERSION = "0.2.0"

#: The questions a `read` agent may ask.
QUERY_KINDS = ("entities", "state", "profile")

MAX_ENTITY_IDS = 50
MAX_PROFILE_DAYS = 30


# `name` and `purpose` are what the agent says about itself — shown to a
# person as exactly that. `level` is what it asks for; a person grants.
class ConnectRequest(Model):
    name: str = ""
    purpose: str = ""
    level: str = "read"


# `state`: pending, approved, denied, refused or full. `level` is set only
# when approved.
class ConnectStatus(Model):
    state: str
    level: str = ""
    detail: str = ""


# `kind` is one of QUERY_KINDS. `entity_ids` names what `state` and
# `profile` are about (`profile` reads the first); `days` bounds `profile`.
class Query(Model):
    kind: str
    entity_ids: list[str] = []
    days: int = 7


# `data` is JSON through core/disclosure.emit — the same rule every answer
# that leaves the house passes.
class QueryResult(Model):
    ok: bool
    kind: str = ""
    data: str = ""
    error: str = ""


# A change for a person to decide. `action` is `create_automation`
# (`config_json`: the automation, as Home Assistant takes it) or
# `call_service` (`service` as domain.service, `entity_id` one or several
# comma-separated, `data_json` optional). `title` and `reason` are shown to
# the person deciding as the agent's own words, and go nowhere else.
class ActionProposal(Model):
    action: str
    title: str = ""
    reason: str = ""
    config_json: str = ""
    service: str = ""
    entity_id: str = ""
    data_json: str = ""


# `state`: asked (waiting for a person; `change_id` names it), refused,
# applied, failed, rejected or withdrawn.
class ActionStatus(Model):
    state: str
    change_id: int = 0
    detail: str = ""


peer_proto = Protocol(name=PROTOCOL_NAME, version=PROTOCOL_VERSION)


_PROFILE_FIELDS = ("by_hour", "by_day", "events", "on_fraction", "mean_on_duration", "days")


def _about(entity_id, last_state, name_for):
    last = last_state(entity_id) or {}
    return {"entity_id": entity_id, "name": name_for(entity_id),
            "state": last.get("state"), "since": last.get("ts")}


def answer(kind, entity_ids, days, *, watched, last_state, profile, name_for) -> dict:
    """What a `read` agent is told. Returns {"ok", "data"} or {"ok": False,
    "error"}. Only WATCHED entities are ever described — the household's opt-in
    in the Watching tab is the whole of what another agent may learn about —
    and an entity asked for that is not watched is named back under
    `not_shared`, never looked up."""
    watched = set(watched or ())
    if kind not in QUERY_KINDS:
        return {"ok": False, "error": f"unknown kind; one of {', '.join(QUERY_KINDS)}"}
    asked = [e for e in (entity_ids or []) if isinstance(e, str)][:MAX_ENTITY_IDS]

    if kind == "entities":
        return {"ok": True, "data": {"entities": [
            _about(e, last_state, name_for) for e in sorted(watched)]}}

    if not asked:
        return {"ok": False, "error": "entity_ids is empty"}
    shared = [e for e in asked if e in watched]
    not_shared = [e for e in asked if e not in watched]

    if kind == "state":
        return {"ok": True, "data": {"entities": [_about(e, last_state, name_for) for e in shared],
                                     "not_shared": not_shared}}

    # profile: the first entity asked for, if it may be told about.
    if asked[0] not in watched:
        return {"ok": True, "data": {"profile": None, "not_shared": [asked[0]]}}
    try:
        span = max(1, min(MAX_PROFILE_DAYS, int(days)))
    except (TypeError, ValueError):
        span = 7
    full = profile(asked[0], span) or {}
    return {"ok": True, "data": {"entity_id": asked[0], "name": name_for(asked[0]),
                                 "profile": {k: full.get(k) for k in _PROFILE_FIELDS}}}


# --------------------------------------------------------------------------
# proposals
# --------------------------------------------------------------------------
_CREATE_TOOL = "Create Home Assistant automation"
_SERVICE_TOOL = "Call Home Assistant service"
ACTIONS = {"create_automation": _CREATE_TOOL, "call_service": _SERVICE_TOOL}

_SERVICE_NAME = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

# Keys that aim an action at something other than a named entity. A target by
# area, device, floor or label reaches devices nobody watches, so a proposal
# that uses one is refused rather than resolved.
_WIDE_TARGETS = frozenset({"area_id", "device_id", "floor_id", "label_id"})


def _walk_keys(node, visit):
    if isinstance(node, dict):
        for k, v in node.items():
            visit(k, v)
            _walk_keys(v, visit)
    elif isinstance(node, list):
        for item in node:
            _walk_keys(item, visit)


def _called(cfg) -> set:
    """Every domain.service an automation calls — under `service` AND under an
    action step's `action`, the key Home Assistant has used since 2024.8.
    auditor._service_names reads `service` only; a check that read one key
    could be walked past with the other."""
    found = set()

    def visit(k, v):
        if k in ("service", "action") and isinstance(v, str) and _SERVICE_NAME.match(v):
            found.add(v)
    _walk_keys(cfg, visit)
    return found


def _steps(cfg) -> list:
    """(service, entity ids it targets) for every service-calling step, at any
    depth. A step's targets are what sits under its own `target`, `data` or
    `entity_id` — not under a nested step's."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            call = node.get("service") if isinstance(node.get("service"), str) else node.get("action")
            if isinstance(call, str) and _SERVICE_NAME.match(call):
                found.append((call, auditor._entity_ids(
                    {k: node.get(k) for k in ("target", "data", "entity_id") if k in node})))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(cfg)
    return found


def _mismatch(steps) -> str:
    """Every call names what it acts on, and only entities of its own domain —
    so `light.turn_off` is a light's, never a switch's, and a step with no
    target (which Home Assistant may read as "all of them") never passes."""
    for service, entities in steps:
        if not entities:
            return f"{service} names no entity to act on"
        domain = service.split(".")[0]
        foreign = sorted(e for e in entities if e.split(".")[0] != domain)
        if foreign:
            return f"{service} acts on another domain's entities: {', '.join(foreign)}"
    return ""


def _wide(cfg) -> set:
    found = set()
    _walk_keys(cfg, lambda k, v: found.add(k) if k in _WIDE_TARGETS else None)
    return found


def _bounds(entities, calls, watched):
    """The rule every proposal meets: it names at least one entity, only
    watched ones, and calls only non-sensitive services of the domains those
    watched entities belong to — so `script.*`, `homeassistant.*` and
    `shell_command.*` are out unless the household watches such an entity."""
    if not entities:
        return "it names no entity; a proposal must act on devices you watch"
    unwatched = sorted(e for e in entities if e not in watched)
    if unwatched:
        return f"it acts on devices you do not watch: {', '.join(unwatched)}"
    sensitive = sorted(c for c in calls if is_sensitive_service(c))
    if sensitive:
        return f"it calls a sensitive service: {', '.join(sensitive)}"
    domains = {e.split(".")[0] for e in watched}
    foreign = sorted(c for c in calls if c.split(".")[0] not in domains)
    if foreign:
        return f"it calls services outside the watched devices' domains: {', '.join(foreign)}"
    return None


def vet(msg, *, watched, states, services, automations) -> dict:
    """Whether a proposal may be put to a person. Returns {"ok": True,
    "tool_name", "tool_args", "entities"} or {"ok": False, "error"}.

    Pure: Home Assistant's states, services and automations are handed in. The
    gate still test-creates an automation afterwards (_preflight_automation);
    this is the part the gate does not know — who is asking, and what they may
    reach."""
    watched = set(watched or ())
    tool_name = ACTIONS.get(getattr(msg, "action", ""))
    if tool_name is None:
        return {"ok": False, "error": f"unknown action; one of {', '.join(ACTIONS)}"}
    known = auditor._known_services(services)

    if tool_name == _CREATE_TOOL:
        try:
            cfg = json.loads(msg.config_json or "")
        except ValueError:
            return {"ok": False, "error": "config_json is not JSON"}
        if not isinstance(cfg, dict):
            return {"ok": False, "error": "config_json is not an object"}
        cfg = automations_mod._normalize_automation_cfg(cfg)
        # Ids are this home's to assign, and the description is free text no
        # person asked for; the alias is kept, cut to a length a list can hold.
        cfg.pop("id", None)
        cfg.pop("description", None)
        if isinstance(cfg.get("alias"), str):
            cfg["alias"] = " ".join(cfg["alias"].split())[:60]
        wide = _wide(cfg)
        if wide:
            return {"ok": False, "error": f"it targets by {', '.join(sorted(wide))}; name entities"}
        why = auditor.check_config(cfg, states, services)
        if why:
            return {"ok": False, "error": why}
        calls = _called(cfg.get("action"))
        unknown = sorted(c for c in calls if c not in known)
        if unknown:
            return {"ok": False, "error": f"calls services Home Assistant does not have: {', '.join(unknown)}"}
        entities = auditor._entity_ids(cfg)
        why = _bounds(entities, calls, watched) or _mismatch(_steps(cfg.get("action")))
        if why:
            return {"ok": False, "error": why}
        duplicate = auditor.duplicate_of(cfg, automations)
        if duplicate:
            return {"ok": False, "error": f"duplicates the automation {duplicate!r}"}
        return {"ok": True, "tool_name": tool_name,
                "tool_args": {"config_json": json.dumps(cfg)}, "entities": sorted(entities)}

    service = str(msg.service or "").strip()
    if not _SERVICE_NAME.match(service):
        return {"ok": False, "error": "service must be domain.service"}
    if service not in known:
        return {"ok": False, "error": f"Home Assistant has no service {service}"}
    entities = {e.strip() for e in str(msg.entity_id or "").split(",") if e.strip()}
    missing = sorted(e for e in entities if e not in (states or {}))
    if missing:
        return {"ok": False, "error": f"Home Assistant has no entity {', '.join(missing)}"}
    data = {}
    if (msg.data_json or "").strip():
        try:
            data = json.loads(msg.data_json)
        except ValueError:
            return {"ok": False, "error": "data_json is not JSON"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "data_json is not an object"}
        smuggled = sorted(k for k in data if k in _WIDE_TARGETS | {"entity_id", "target"})
        if smuggled:
            return {"ok": False, "error": f"data_json may not target anything ({', '.join(smuggled)})"}
    why = _bounds(entities, {service}, watched) or _mismatch([(service, entities)])
    if why:
        return {"ok": False, "error": why}
    return {"ok": True, "tool_name": tool_name,
            "tool_args": {"service": service, "entity_id": ",".join(sorted(entities)),
                          "data_json": json.dumps(data) if data else ""},
            "entities": sorted(entities)}


def notice(address, tool_name, tool_args, entities) -> str:
    """The outbox text for a proposal, built only from checked facts — the
    service name and entity ids Home Assistant has, never the alias, title or
    reason the agent wrote: the outbox is what ASI1's "what's new" hands the
    model, and nothing another agent writes may reach it."""
    who = f"{address[:14]}…{address[-6:]}" if len(address) > 24 else address
    if tool_name == _SERVICE_TOOL:
        what = f"call {tool_args.get('service')} on {', '.join(entities)}"
    else:
        what = f"create an automation acting on {', '.join(entities)}"
    return (f"An agent you connected ({who}) proposes to {what}. "
            f"Answer it in the panel's Connections tab.")
