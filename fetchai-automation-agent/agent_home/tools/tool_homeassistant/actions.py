"""LLM tool that calls a Home Assistant service — the agent's way of actually
turning things on and off, rather than only writing automations.

Calls in a sensitive domain (locks, alarm, covers, notifications, Home
Assistant itself) are routed through the approval flow in
core.agent_runtime; everyday domains such as light or switch run straight
away, since they are trivially reversible.
"""
import json
from typing import Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import disclosure
from ._client import _ha_post

# Domains where a wrong call is not trivially undone (unlocking a door,
# disarming an alarm, messaging somebody, restarting HA). Kept here because it
# describes these services, and imported by core.agent_runtime for approvals.
SENSITIVE_SERVICE_DOMAINS = {
    "alarm_control_panel",
    "cover",
    "hassio",
    "homeassistant",
    "lock",
    "notify",
    "shell_command",
    "valve",
}


def is_sensitive_service(service: str) -> bool:
    """True if this service belongs to a domain that needs user approval."""
    return str(service or "").split(".")[0] in SENSITIVE_SERVICE_DOMAINS


class _HACallServiceInput(BaseModel):
    """Input schema for calling a service on one or more entities."""
    service: str = Field(
        description="Full service name in 'domain.service' form, e.g. 'light.turn_on', "
                    "'switch.toggle', 'climate.set_temperature', 'media_player.media_pause'. "
                    "Use 'List Home Assistant services' to discover what a domain supports.",
        default="",
    )
    entity_id: str = Field(
        description="Target entity_id, or several separated by commas, e.g. "
                    "'light.kitchen_ceiling' or 'light.kitchen_ceiling,light.kitchen_counter'. "
                    "Resolve the entity_ids first — with 'List entities in Home Assistant area' "
                    "when the user named a room, or 'List Home Assistant entities' otherwise. "
                    "May be left empty for services that take no target.",
        default="",
    )
    data_json: str = Field(
        description="Optional extra service parameters as a JSON object string, e.g. "
                    "'{\"brightness_pct\": 40}' or '{\"temperature\": 21.5}'. Omit when the "
                    "service needs no parameters beyond the target.",
        default="",
    )


class tool_ha_call_service(BaseTool):
    """LLM tool that executes a Home Assistant service call on given entities."""
    name: str = "Call Home Assistant service"
    description: str = (
        "Control the home right now: turn entities on or off, toggle them, set brightness, "
        "temperature, media volume and so on, by calling a Home Assistant service. "
        "Pass the service as 'domain.service' (e.g. 'light.turn_on'), the target entity_id(s), "
        "and any extra parameters as a JSON object string. Use this for immediate actions the user "
        "asks for ('turn off the kitchen light'). For things that should happen on a schedule or "
        "in reaction to an event, create an automation instead. Always resolve the exact entity_ids "
        "first — by area when the user named a room. Returns the entities whose state changed."
    )
    args_schema: Type[BaseModel] = _HACallServiceInput

    def _run(self, service: str = "", entity_id: str = "", data_json: str = "") -> str:
        """Posts the service call to HA and reports the states it changed."""
        if not service:
            return disclosure.emit({"error": "service is required, in 'domain.service' form"})
        if "." not in service:
            return disclosure.emit({"error": f"'{service}' is not a valid service — use 'domain.service', e.g. 'light.turn_on'"})
        domain, _, name = service.partition(".")

        body = {}
        if data_json:
            try:
                parsed = json.loads(data_json)
            except Exception as e:
                return disclosure.emit({"error": f"Invalid JSON in data_json: {e}"})
            if not isinstance(parsed, dict):
                return disclosure.emit({"error": "data_json must be a JSON object"})
            body.update(parsed)

        targets = [e.strip() for e in entity_id.split(",") if e.strip()]
        if targets:
            body["entity_id"] = targets

        ok, data = _ha_post(f"/services/{domain}/{name}", body)
        if not ok:
            return disclosure.emit({"error": f"Service call failed: {data}"})

        # HA answers with the states it changed; report them so the model can
        # confirm what actually happened instead of assuming success.
        changed = []
        if isinstance(data, list):
            changed = [
                {"entity_id": s.get("entity_id", ""), "state": s.get("state", "")}
                for s in data if isinstance(s, dict)
            ]
        return disclosure.emit({
            "called": service,
            "entity_id": targets,
            "changed_states": changed,
        })
