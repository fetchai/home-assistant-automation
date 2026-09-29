"""Home Assistant tools subpackage: read-only entity/device/service/area
discovery, recorder history, service calls that control the home, and automation
create/list/get/update/delete tools, re-exported for the agent's tool registry."""
from .entities import tool_ha_list_entities, tool_ha_get_entity
from .devices import tool_ha_list_devices
from .services import tool_ha_list_services
from .areas import tool_ha_list_areas, tool_ha_list_area_entities
from .actions import tool_ha_call_service
from .history import tool_ha_entity_history
from .automations import (
    tool_ha_create_automation, tool_ha_list_automations, tool_ha_get_automation,
    tool_ha_update_automation, tool_ha_delete_automation,
)

__all__ = [
    "tool_ha_list_entities",
    "tool_ha_get_entity",
    "tool_ha_list_devices",
    "tool_ha_list_services",
    "tool_ha_list_areas",
    "tool_ha_list_area_entities",
    "tool_ha_call_service",
    "tool_ha_entity_history",
    "tool_ha_create_automation",
    "tool_ha_list_automations",
    "tool_ha_get_automation",
    "tool_ha_update_automation",
    "tool_ha_delete_automation",
]
