"""LLM tool for listing Home Assistant devices from the device registry,
together with the area each of them belongs to and their entities."""

from langchain.tools import BaseTool

from core import disclosure
from ._client import _ha_get, _ha_ws_calls
from .registry import entities_by_device


class tool_ha_list_devices(BaseTool):
    """LLM tool that lists HA devices with their area and entities."""
    name: str = "List Home Assistant devices"
    description: str = (
        "List the physical devices known to Home Assistant, from the device registry. Returns a JSON "
        "array of {device_id, name, manufacturer, model, area, entities}. Useful for understanding "
        "what hardware exists in the user's home and which room each device is in. To find the "
        "entities of a room, prefer 'List entities in Home Assistant area'."
    )

    def _run(self) -> str:
        """Reads the device and entity registries and joins them with the area names."""
        ok, results = _ha_ws_calls([
            {"type": "config/area_registry/list"},
            {"type": "config/device_registry/list"},
            {"type": "config/entity_registry/list"},
        ])
        if not ok:
            return disclosure.emit({"error": results})
        area_rows, device_rows, entity_rows = results

        area_names = {
            a.get("area_id"): a.get("name", "")
            for a in (area_rows or []) if isinstance(a, dict)
        }

        # Shared with the observation picker and the watchlist, so "this
        # device's entities" means the same set everywhere.
        by_device = entities_by_device(entity_rows)

        devices = []
        for d in (device_rows or []):
            if not isinstance(d, dict) or not d.get("id"):
                continue
            devices.append({
                "device_id": d.get("id"),
                "name": d.get("name_by_user") or d.get("name", ""),
                "manufacturer": d.get("manufacturer", ""),
                "model": d.get("model", ""),
                "area": area_names.get(d.get("area_id"), ""),
                "entities": by_device.get(d.get("id"), []),
            })
        devices.sort(key=lambda d: (d["area"].lower(), str(d["name"]).lower()))

        total = len(devices)
        truncated = False
        if len(devices) > 200:
            devices = devices[:200]
            truncated = True
        return disclosure.emit({"devices": devices, "_truncated": truncated, "total": total})
