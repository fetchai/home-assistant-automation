"""Consistency between the tool registry, the approval gate and the prompt.

Tool names are written out in four places: the `tools` list in agent_runtime, the
DESTRUCTIVE_TOOLS set, SERVICE_CALL_TOOL, and the prose of settings.system_message.
Nothing links them — they are matched by string. Renaming a tool in one place and
not the others silently breaks either the prompt or the approval gate, with no
error anywhere. These tests are the link.
"""
import re

import pytest

from core import agent_runtime as rt
from core import settings
from tools import tool_homeassistant as ha
from tools.tool_homeassistant import actions


REGISTERED = [tool.name for tool in rt.tools]
GATED = sorted(rt.DESTRUCTIVE_TOOLS | {rt.SERVICE_CALL_TOOL})

# The prompt is built from adjacent string literals, so a sentence that wraps
# across two of them carries the second line's indentation with it. Assert
# against the collapsed text rather than pinning incidental whitespace.
PROMPT = " ".join(settings.system_message.split())


def test_registry_is_not_empty():
    assert len(rt.tools) >= 17


def test_tool_names_are_unique():
    """_execute_tool_sync returns on the FIRST name match, so a duplicate name
    silently shadows whichever tool is registered later."""
    duplicates = {name for name in REGISTERED if REGISTERED.count(name) > 1}
    assert not duplicates, f"duplicate tool names shadow each other: {sorted(duplicates)}"


@pytest.mark.parametrize("tool_name", sorted(rt.DESTRUCTIVE_TOOLS))
def test_destructive_tools_are_registered(tool_name):
    """A name in DESTRUCTIVE_TOOLS that no tool answers to gates nothing."""
    assert tool_name in REGISTERED


def test_service_call_tool_is_registered():
    assert rt.SERVICE_CALL_TOOL in REGISTERED


def test_every_tool_name_appears_in_the_system_message():
    """The prompt names each tool in prose. A rename that misses settings.py
    leaves the model calling a tool the prompt never mentions."""
    missing = [name for name in REGISTERED if name not in PROMPT]
    assert not missing, (
        f"tool names absent from settings.system_message: {missing}. "
        "Renaming a tool means editing the prompt text too."
    )


@pytest.mark.parametrize("class_name", sorted(ha.__all__))
def test_every_exported_ha_tool_is_in_the_registry(class_name):
    """Catches the 'a new tool needs three edits' trap: class written and
    exported, but never added to the tools list, so the LLM never sees it."""
    tool_class = getattr(ha, class_name)
    assert any(isinstance(tool, tool_class) for tool in rt.tools), (
        f"{class_name} is exported from tool_homeassistant but not in agent_runtime.tools"
    )


@pytest.mark.parametrize("tool_name", GATED)
def test_approval_message_is_specific_for_every_gated_tool(tool_name):
    """_format_approval_message spells the tool names out a fourth time. If one
    drifts, the user gets the generic fallback instead of a real description of
    what they are about to approve."""
    generic = f"⚠️ **Confirm action:** {tool_name}"
    assert rt._format_approval_message(tool_name, {}) != generic


def test_gated_tools_are_the_ones_the_approval_gate_actually_catches():
    """Ties the sets to the predicate rather than trusting them to agree."""
    for tool_name in rt.DESTRUCTIVE_TOOLS:
        assert rt._is_destructive(tool_name, {}) is True
    assert rt._is_destructive(rt.SERVICE_CALL_TOOL, {"service": "lock.unlock"}) is True


def test_system_message_explains_the_confirmation_step():
    """The prompt owes the user an explanation of why some actions pause for
    confirmation; without it the model apologises for the approval flow or
    refuses to make the call at all."""
    assert "are shown to the user for confirmation before they run" in PROMPT


@pytest.mark.parametrize("domain", sorted(actions.SENSITIVE_SERVICE_DOMAINS))
def test_system_message_names_every_sensitive_domain(domain):
    """The fifth place the safety boundary is written out. The prompt tells the
    model which domains pause for confirmation; a domain the gate catches but
    the prompt never names is one the model warns about or refuses instead of
    just calling. Matched on word boundaries so 'cover' is not satisfied by
    'discovery'."""
    assert re.search(rf"\b{re.escape(domain)}\b", PROMPT), (
        f"{domain!r} is in SENSITIVE_SERVICE_DOMAINS but the prompt never names it. "
        "Adding a sensitive domain means editing settings.system_message too."
    )


def test_system_message_keeps_its_critical_rules_block():
    """The prompt is code — this block is what stops the model inventing
    entity_ids and claiming actions it never performed."""
    assert "CRITICAL RULES — NEVER VIOLATE" in PROMPT
