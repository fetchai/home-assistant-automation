"""Automation writes, and the LLM tools that expose them.

Two layers, deliberately. The bottom one — create_automation,
update_automation, delete_automation — is plain functions taking an id and a
config dict: no BaseTool, no schema, no registry. The top one is the tools the
model is given, and each of them is now a thin wrapper around the function
below it.

The split exists because of core/gate.py: it records the inverse of every write
as an integration-level operation ("restore_automation"), never a tool name,
"deliberately — undoing a change must never depend on the model being
available, or willing". An inverse that could only be run by looking a tool up
in the registry and calling its _run would put the model straight back into the
rollback path. core/rollback.py calls the functions; the tools call the same
ones, so there is one implementation of each write rather than two that can
drift.

Also here: the validation helpers used before a destructive change is put to
the user."""
import json
import time
from typing import Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import disclosure
from ._client import _ha_get, _ha_post, _ha_delete


def _normalize_automation_cfg(cfg: dict) -> dict:
    """Accept both plural (triggers/conditions/actions) and singular HA keys."""
    for plural, singular in (("triggers", "trigger"), ("conditions", "condition"), ("actions", "action")):
        if plural in cfg and singular not in cfg:
            cfg[singular] = cfg.pop(plural)
    return cfg


def _validate_by_test_create(cfg: dict) -> tuple:
    """POST cfg as a temporary automation to HA to validate schema. Deletes it on success."""
    temp_id = new_automation_id()
    ok, data = _ha_post(f"/config/automation/config/{temp_id}", cfg)
    if not ok:
        return False, str(data)
    _ha_delete(f"/config/automation/config/{temp_id}")
    return True, temp_id


def _preflight_automation(config_json: str):
    """Parse, normalise and test-create a config. Returns error string if invalid, None if ok."""
    try:
        cfg = json.loads(config_json)
    except Exception as e:
        return f"Invalid JSON in config_json: {e}"
    if not isinstance(cfg, dict):
        return "config_json must be a JSON object"
    cfg = _normalize_automation_cfg(cfg)
    if "alias" not in cfg or "trigger" not in cfg or "action" not in cfg:
        return "config must include alias, trigger, and action"
    if not isinstance(cfg.get("trigger"), list) or not isinstance(cfg.get("action"), list):
        return "trigger and action must be arrays"
    cfg.setdefault("mode", "single")
    cfg.setdefault("description", "")
    cfg.setdefault("condition", [])
    ok, err = _validate_by_test_create(cfg)
    if not ok:
        # Home Assistant's rejection is fed straight back to the model, so it
        # leaves the house like a tool answer does and goes through the same
        # boundary. A request failure's text carries the URL it was made
        # against, which is the case that makes this worth doing.
        return disclosure.text(f"HA rejected this config: {err}. Fix the config and retry.")
    return None


# --------------------------------------------------------------------------
# The writes themselves — no tool, no model, no registry.
#
# All three answer (ok, detail). On failure `detail` is the error text; on
# success it is a warning, or "" when there is nothing to warn about — a config
# that was written but whose reload failed is neither a success to report
# plainly nor a failure to roll back, so it comes back as both.
# --------------------------------------------------------------------------

def new_automation_id() -> str:
    """A fresh automation id, minted the way Home Assistant's own editor mints
    one: milliseconds since the epoch."""
    return str(int(time.time() * 1000))


def _write_automation(automation_id: str, cfg: dict) -> tuple:
    """POSTs a config at an id and reloads so it takes effect.

    Home Assistant's automation config endpoint is the same POST whether the id
    already exists or not, which is why creating and updating share a body — and
    why restoring a deleted automation is not a special case at all.
    """
    if not automation_id:
        return False, "automation_id is required"
    if not isinstance(cfg, dict):
        return False, "config must be a JSON object"
    cfg = _normalize_automation_cfg(cfg)
    cfg.setdefault("mode", "single")
    cfg.setdefault("description", "")
    cfg.setdefault("condition", [])

    ok, data = _ha_post(f"/config/automation/config/{automation_id}", cfg)
    if not ok:
        return False, str(data)

    ok_reload, reload_data = _ha_post("/services/automation/reload", {})
    if not ok_reload:
        return True, f"reload failed — the change may not be active until Home Assistant reloads: {reload_data}"
    return True, ""


def read_automation(automation_id: str) -> tuple:
    """The stored config of one automation, as (ok, config-or-error).

    Not a tool — tool_ha_get_automation is the tool. This is what a caller that
    must not go through the model uses: core/rollback.py reads the config that
    is there NOW before overwriting it, so the undo it is about to perform is
    itself journalled with an inverse, like every other write.
    """
    if not automation_id:
        return False, "automation_id is required"
    ok, data = _ha_get(f"/config/automation/config/{automation_id}")
    return (True, data) if ok else (False, str(data))


def create_automation(automation_id: str, cfg: dict) -> tuple:
    """Writes cfg at a given id, creating the automation.

    This is also what restoring a deleted one is: putting it back at the id it
    had, with the config that was read before it went. gate.py's
    `restore_automation` inverse dispatches here for exactly that reason — there
    is no separate "undelete" in Home Assistant, and inventing one would only
    have hidden that.
    """
    return _write_automation(automation_id, cfg)


def update_automation(automation_id: str, cfg: dict) -> tuple:
    """Replaces an existing automation's whole config.

    The same HTTP call as create_automation, kept under its own name because the
    journal distinguishes them: an inverse recorded as "update_automation" has
    to dispatch to something that means update, whatever the wire happens to do.

    It is also where the read-modify-write round trip is closed. An update is
    the model handing back a whole config it read through core.disclosure, so
    anything the boundary withheld on the way out — a webhook_id is a bearer
    token by another name — would come back as the marker and overwrite the
    real value. The config Home Assistant actually holds is what puts those
    back; a marker that cannot be resolved fails the write rather than saving a
    config nobody can reconstruct. Rollback never trips this: it writes a config
    read straight from HA, which was never redacted.
    """
    if disclosure.has_withheld(cfg):
        ok, current = read_automation(automation_id)
        if not ok:
            return False, (f"this config still contains values withheld from the model, and the "
                           f"stored automation could not be read to restore them: {current}")
        ok, cfg = disclosure.restore(cfg, current)
        if not ok:
            return False, cfg
    return _write_automation(automation_id, cfg)


def delete_automation(automation_id: str) -> tuple:
    """Removes an automation. Home Assistant reloads on its own for a delete,
    so there is no second call here."""
    if not automation_id:
        return False, "automation_id is required"
    ok, result = _ha_delete(f"/config/automation/config/{automation_id}")
    if not ok:
        return False, str(result)
    return True, ""


def list_automations() -> tuple:
    """Every automation as (ok, [{automation_id, entity_id, alias, state}]),
    or (False, error).

    Not a tool — tool_ha_list_automations is the tool and now wraps this. Kept
    as a plain function for the same reason read_automation is: a caller that
    must not go through the model — tasks/observation.py, checking what
    depends on an entity that just disappeared — needs the same listing.
    """
    ok, data = _ha_get("/states")
    if not ok:
        return False, str(data)
    automations = [
        {
            "automation_id": (e.get("attributes") or {}).get("id", e.get("entity_id", "").removeprefix("automation.")),
            "entity_id": e.get("entity_id", ""),
            "alias": (e.get("attributes") or {}).get("friendly_name", ""),
            "state": e.get("state", ""),
        }
        for e in data
        if isinstance(e, dict) and e.get("entity_id", "").startswith("automation.")
        and e.get("state") != "unavailable"
    ]
    return True, automations


def _references_entity(value, entity_id: str) -> bool:
    """True if entity_id appears anywhere inside an automation config — an
    entity_id can sit in a trigger, a condition, an action's target block, or
    nested inside a choose/if branch, so this walks the structure rather than
    assuming any particular shape."""
    if isinstance(value, str):
        return value == entity_id
    if isinstance(value, dict):
        return any(_references_entity(v, entity_id) for v in value.values())
    if isinstance(value, list):
        return any(_references_entity(v, entity_id) for v in value)
    return False


def find_dependent_automations(entity_id: str) -> tuple:
    """Automations whose config references entity_id, as (ok, [alias, ...]).

    Fetches every automation's full config — one call per automation — so this
    must only run on an event that actually happened (an entity disappearing),
    never on a fixed schedule. Any single automation's config failing to load
    fails the whole lookup rather than silently under-reporting: a caller that
    cannot tell "found none" from "checked none" must be told which one it got.
    """
    ok, automations = list_automations()
    if not ok:
        return False, automations
    names = []
    for a in automations:
        ok_cfg, cfg = read_automation(a["automation_id"])
        if not ok_cfg:
            return False, cfg
        if _references_entity(cfg, entity_id):
            names.append((cfg or {}).get("alias") or a.get("alias") or a.get("automation_id"))
    return True, names


class _HACreateAutomationInput(BaseModel):
    """Input schema for creating an automation: its full config as a JSON string."""
    config_json: str = Field(
        description=(
            "Full automation config as a JSON string. Required keys: alias (string), trigger (array), "
            "action (array). Optional keys: description (string), condition (array), mode "
            "(single|restart|queued|parallel, default 'single'). "
            "Example: '{\"alias\":\"Porch at sunset\",\"trigger\":[{\"platform\":\"sun\",\"event\":\"sunset\"}],"
            "\"action\":[{\"service\":\"light.turn_on\",\"target\":{\"entity_id\":\"light.porch\"}}]}'. "
            "Use the 'List Home Assistant entities' and 'List Home Assistant services' tools first to find "
            "correct entity_ids and service names."
        ),
        default="",
    )


class tool_ha_create_automation(BaseTool):
    """LLM tool that creates and activates a new Home Assistant automation."""
    name: str = "Create Home Assistant automation"
    description: str = (
        "Create a new automation in Home Assistant. Pass the full automation config as a JSON string "
        "(alias, trigger, action, optional condition/description/mode). Returns the new automation_id and "
        "entity_id on success. The automation is activated immediately via reload. Always use "
        "'List Home Assistant entities' and 'List Home Assistant services' first to verify entity_ids "
        "and service names before creating, otherwise the automation will reference missing entities."
    )
    args_schema: Type[BaseModel] = _HACreateAutomationInput

    def _run(self, config_json: str = "") -> str:
        """Parses the config and hands it to create_automation, which mints the
        id, writes it and reloads."""
        try:
            cfg = json.loads(config_json)
        except Exception as e:
            return disclosure.emit({"error": f"Invalid JSON: {e}"})

        automation_id = new_automation_id()
        ok, detail = create_automation(automation_id, cfg)
        if not ok:
            return disclosure.emit({"error": f"Create failed: {detail}"})

        result = {
            "created": True,
            "automation_id": automation_id,
            "entity_id": f"automation.{automation_id}",
            "alias": cfg.get("alias"),
        }
        if detail:
            result["warning"] = f"Created but {detail}"
        return disclosure.emit(result)


class tool_ha_list_automations(BaseTool):
    """LLM tool that lists all automations and their basic info."""
    name: str = "List Home Assistant automations"
    description: str = (
        "List all automations in Home Assistant. Returns a compact JSON array of "
        "{automation_id, alias, state} for each automation. Use this to discover "
        "existing automations and get their IDs before fetching full details."
    )

    def _run(self) -> str:
        """Fetches all states and filters/maps the automation.* entities to a compact list."""
        ok, automations = list_automations()
        if not ok:
            return disclosure.emit({"error": automations})
        return disclosure.emit({"automations": automations, "total": len(automations)})


class _HAGetAutomationInput(BaseModel):
    """Input schema for fetching one automation by id."""
    automation_id: str = Field(
        description=(
            "The automation_id (e.g. '1748000000000') or full entity_id "
            "(e.g. 'automation.1748000000000'). Get it from 'List Home Assistant "
            "automations' first if you don't know it."
        ),
        default="",
    )


class tool_ha_get_automation(BaseTool):
    """LLM tool that fetches one automation's full config by id."""
    name: str = "Get Home Assistant automation"
    description: str = (
        "Get the full config (alias, trigger, condition, action, mode) for a single "
        "Home Assistant automation. Pass the automation_id or full entity_id. "
        "Call 'List Home Assistant automations' first to find the ID."
    )
    args_schema: Type[BaseModel] = _HAGetAutomationInput

    def _run(self, automation_id: str = "") -> str:
        """Fetches the full config for the given automation id."""
        if not automation_id:
            return disclosure.emit({"error": "automation_id is required"})
        ok, data = read_automation(automation_id.removeprefix("automation."))
        if not ok:
            return disclosure.emit({"error": data})
        return disclosure.emit(data)


class _HAUpdateAutomationInput(BaseModel):
    """Input schema for replacing an automation's config: its id and the full new config JSON."""
    automation_id: str = Field(
        description=(
            "The automation_id (e.g. '1748000000000') or full entity_id "
            "(e.g. 'automation.1748000000000'). Get it from 'List Home Assistant automations'."
        ),
        default="",
    )
    config_json: str = Field(
        description=(
            "Full replacement automation config as a JSON string. Required keys: alias, trigger, action. "
            "Optional: description, condition, mode. The entire existing config is replaced — include all "
            "fields you want to keep, not just the changed ones. "
            "Example: '{\"alias\":\"Updated name\",\"trigger\":[...],\"action\":[...]}'"
        ),
        default="",
    )


class tool_ha_update_automation(BaseTool):
    """LLM tool that replaces an existing automation's full config."""
    name: str = "Update Home Assistant automation"
    description: str = (
        "Replace the full config of an existing Home Assistant automation. Pass the automation_id "
        "(or entity_id) and the complete new config as a JSON string. This overwrites the entire "
        "automation — include all fields you want to keep. The automation is reloaded immediately "
        "after update. Call 'Get Home Assistant automation' first to retrieve the current config "
        "before making changes."
    )
    args_schema: Type[BaseModel] = _HAUpdateAutomationInput

    def _run(self, automation_id: str = "", config_json: str = "") -> str:
        """Parses the replacement config and hands it to update_automation."""
        if not automation_id:
            return disclosure.emit({"error": "automation_id is required"})
        try:
            cfg = json.loads(config_json)
        except Exception as e:
            return disclosure.emit({"error": f"Invalid JSON: {e}"})

        aid = automation_id.removeprefix("automation.")
        ok, detail = update_automation(aid, cfg)
        if not ok:
            return disclosure.emit({"error": f"Update failed: {detail}"})

        result = {"updated": True, "automation_id": aid, "alias": cfg.get("alias")}
        if detail:
            result["warning"] = f"Updated but {detail}"
        return disclosure.emit(result)


class _HADeleteAutomationInput(BaseModel):
    """Input schema for deleting an automation by id."""
    automation_id: str = Field(
        description=(
            "The automation_id (e.g. '1748000000000') or full entity_id "
            "(e.g. 'automation.1748000000000'). Get it from 'List Home Assistant automations'."
        ),
        default="",
    )


class tool_ha_delete_automation(BaseTool):
    """LLM tool that deletes an existing automation by id."""
    name: str = "Delete Home Assistant automation"
    description: str = (
        "Delete an existing Home Assistant automation. Pass the automation_id or entity_id. "
        "Call 'List Home Assistant automations' first to confirm the ID. This action is irreversible."
    )
    args_schema: Type[BaseModel] = _HADeleteAutomationInput

    def _run(self, automation_id: str = "") -> str:
        """Deletes the automation with the given id."""
        if not automation_id:
            return disclosure.emit({"error": "automation_id is required"})
        aid = automation_id.removeprefix("automation.")
        ok, detail = delete_automation(aid)
        if not ok:
            return disclosure.emit({"error": f"Delete failed: {detail}"})
        return disclosure.emit({"deleted": True, "automation_id": aid})
