"""LLM tool for discovering Home Assistant services (read-only — describes
actions but cannot call them)."""
from typing import Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import disclosure
from ._client import _ha_get


# How much of one domain's catalogue may be handed over. Home Assistant
# describes every parameter of every service it knows, and a large integration
# runs to hundreds of kilobytes — more of the model's context than any question
# about lights is worth.
MAX_CHARS = 50000


class _HAServicesInput(BaseModel):
    """Input schema for the services tool: optional domain filter."""
    domain: str = Field(
        description="Optional domain to filter to (e.g. 'light', 'switch', 'climate'). "
                    "When provided, returns only that domain's services with full details "
                    "(name, description, parameter fields). When omitted, returns a compact list "
                    "of all domains and their service names.",
        default="",
    )


class tool_ha_list_services(BaseTool):
    """LLM tool that lists HA service domains, or one domain's services in detail."""
    name: str = "List Home Assistant services"
    description: str = (
        "Discover what actions (services) exist in Home Assistant. Read-only — describes actions "
        "but cannot execute them. Pass a domain (e.g. 'light', 'switch', 'climate') to get the "
        "full details for that domain's services including parameter fields. Pass nothing to get "
        "a compact list of all domains and their service names. Use this when the user asks "
        "'what can I do with my lights?' or similar capability questions."
    )
    args_schema: Type[BaseModel] = _HAServicesInput

    def _run(self, domain: str = "") -> str:
        """Returns full service details for the given domain, or a compact
        list of all domains' service names if no domain is given."""
        ok, data = _ha_get("/services")
        if not ok:
            return disclosure.emit({"error": data})
        if not isinstance(data, list):
            return disclosure.emit({"error": "Unexpected response shape from HA /services"})

        if domain:
            for entry in data:
                if entry.get("domain") == domain:
                    # Bounded by dropping whole services, not by cutting the
                    # JSON in half — the old cap sliced mid-string and handed
                    # the model a document it could not parse, which is worse
                    # than a short one.
                    services = dict(entry.get("services") or {})
                    dropped = []
                    while len(disclosure.emit({"domain": domain, "services": services})) > MAX_CHARS \
                            and len(services) > 1:
                        dropped.append(services.popitem()[0])
                    payload = {"domain": domain, "services": services}
                    if dropped:
                        payload["_truncated"] = True
                        payload["omitted_services"] = sorted(dropped)
                    return disclosure.emit(payload)
            return disclosure.emit({
                "error": f"Domain '{domain}' not found",
                "available_domains": [e.get("domain") for e in data],
            })

        compact = [
            {"domain": e.get("domain"), "services": list((e.get("services") or {}).keys())}
            for e in data
        ]
        return disclosure.emit({"domains": compact})
