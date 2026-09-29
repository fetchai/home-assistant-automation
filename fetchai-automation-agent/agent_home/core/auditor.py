"""What is left of the auditor: plain checks of an analyst idea against the
HOUSE, before it is stored.

`docs/PROPOSAL-MODEL-ANALYST.md` §5A built this module as a claim auditor —
the model stated figures (`follows`, `transitions`, `on_hours`), this module
recounted each from the record, refused a figure that did not recount and
labelled the row `checked` or `untested`. Free-hand decision F-9
(`docs/PROPOSAL-FREE-HAND-ANALYST.md`) retired that half once the explorer run
had its field run (F-WP8, 2.23.0): the explorer's evidence is a chart code
draws from the record (core/charts.py), which a person judges, not a figure a
recount can vouch for, and the claim path's labels proved weaker than they read
(#12 "checked" on 30 ons, its time of day unclaimed). The retired rows are
withdrawn by `core/review.py`; nothing writes a claim any more.

What stays is the half that never depended on the model's words: C-4 / C-5
(`check_config` — every entity and service the config names exists, and no
sensitive domain is called) and C-6 (`duplicate_of` — not a copy of an
existing automation's triggers and targets), which `core/analyst.run_explorer`
calls on every idea before anything is stored, plus `signature`, which
`analyst.analyst_key` derives a row's key from (D-8). Nothing here calls an
LLM, imports `agent_runtime`, or writes anywhere: the checks answer a reason or
None, and the caller decides.

Pure except `_fetch_automation_configs`, which the explorer's preconditions
call once per run so every idea is checked against the same list.
"""
import logging

from tools.tool_homeassistant import automations as automations_mod
from tools.tool_homeassistant.actions import is_sensitive_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure — no I/O, no module state.
# ---------------------------------------------------------------------------

def _entity_ids(value) -> set:
    """Every value found under a key literally named `entity_id`, string or
    list — the one shape Home Assistant's own trigger/condition/action/target
    schema uses to name an entity, in a config that may nest it inside a
    choose/if branch at any depth."""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "entity_id":
                    if isinstance(v, str):
                        found.add(v)
                    elif isinstance(v, list):
                        found.update(x for x in v if isinstance(x, str))
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def _service_names(value) -> set:
    """Every string found under a key literally named `service`, at any
    depth — the action step's own service call, wherever it sits."""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "service" and isinstance(v, str):
                    found.add(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def _known_services(services) -> frozenset:
    """{"domain.service", ...} from the raw shape `_ha_get("/services")`
    returns — a list of {"domain", "services": {name: {...}}}."""
    out = set()
    for entry in (services or []):
        if not isinstance(entry, dict):
            continue
        domain = entry.get("domain")
        for name in (entry.get("services") or {}):
            out.add(f"{domain}.{name}")
    return frozenset(out)


def check_config(cfg: dict, states: dict, services):
    """C-4, C-5: the static half. `cfg` is assumed already normalised
    (`automations._normalize_automation_cfg`) — this validates the pure part
    of `_preflight_automation` (alias/trigger/action present, both arrays)
    and adds what that function does not: every entity id it names exists in
    `states`, every service it calls exists in `services`, and none of those
    services is in `is_sensitive_service`'s domains (C-5). The HA test-create
    `_preflight_automation` also runs is NOT repeated here — `gate.propose`
    runs it before anything is journaled, and an analyst row whose config
    fails it there is retired (D-7)."""
    if not isinstance(cfg, dict) or "alias" not in cfg or "trigger" not in cfg or "action" not in cfg:
        return "config must include alias, trigger, and action"
    if not isinstance(cfg.get("trigger"), list) or not isinstance(cfg.get("action"), list):
        return "trigger and action must be arrays"

    entities = _entity_ids(cfg)
    missing = sorted(e for e in entities if e not in (states or {}))
    if missing:
        return f"references entities Home Assistant does not have: {', '.join(missing)}"

    calls = _service_names(cfg.get("action"))
    known = _known_services(services)
    unknown = sorted(s for s in calls if s not in known)
    if unknown:
        return f"calls services Home Assistant does not have: {', '.join(unknown)}"

    sensitive = sorted(s for s in calls if is_sensitive_service(s))
    if sensitive:
        return f"calls a sensitive service the analyst may not use: {', '.join(sensitive)}"
    return None


def _trigger_pairs(triggers) -> frozenset:
    out = set()
    for t in (triggers or []):
        if not isinstance(t, dict):
            continue
        platform = t.get("platform")
        ents = t.get("entity_id")
        if isinstance(ents, str):
            ents = [ents]
        elif not isinstance(ents, list):
            ents = []
        if ents:
            out.update((platform, e) for e in ents)
        else:
            out.add((platform, None))
    return frozenset(out)


def _action_pairs(actions) -> frozenset:
    out = set()
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        service = a.get("service")
        ents = _entity_ids(a)
        if ents:
            out.update((service, e) for e in ents)
        else:
            out.add((service, None))
    return frozenset(out)


def signature(cfg: dict):
    """C-6: the sorted set of (trigger platform, trigger entity) pairs plus
    the sorted set of (service, target entity) pairs — hashable, so two
    configs collide exactly when `duplicate_of` says they should."""
    return (_trigger_pairs((cfg or {}).get("trigger")), _action_pairs((cfg or {}).get("action")))


def duplicate_of(cfg: dict, automations):
    """C-6: the alias of an existing automation whose signature matches
    `cfg`'s, or None. `automations` is a list of full automation configs (the
    shape `automations.read_automation` returns), not the summary
    `list_automations` gives — a signature needs the trigger/action bodies a
    summary does not carry."""
    sig = signature(cfg)
    for existing in (automations or []):
        if isinstance(existing, dict) and signature(existing) == sig:
            return existing.get("alias") or "an existing automation"
    return None


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _fetch_automation_configs():
    """Every automation's full config, as (ok, [config, ...]) — the shape
    `duplicate_of` needs. Mirrors `automations.find_dependent_automations`'s
    fetch-then-read-each pattern: any single read failing fails the whole
    lookup rather than silently checking against a partial list."""
    ok, summaries = automations_mod.list_automations()
    if not ok:
        return False, summaries
    configs = []
    for summary in summaries:
        ok_cfg, cfg = automations_mod.read_automation(summary["automation_id"])
        if not ok_cfg:
            return False, cfg
        if isinstance(cfg, dict):
            configs.append(cfg)
    return True, configs
