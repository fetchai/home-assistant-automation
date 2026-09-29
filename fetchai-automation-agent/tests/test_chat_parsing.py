"""Reading a chat reply as a decision, and rendering an approval for chat clients.

parse_approval_decision is the load-bearing one: it is what turns a message into
consent to delete something, and it deliberately does not involve the LLM. It
matches the whole normalised message, so anything with extra words is NOT an
answer and must come back None so the caller re-asks.
"""
import json

import pytest

from core.agent_runtime import (
    APPROVAL_MARKER,
    format_approval_for_chat,
    parse_approval_decision,
)


def _approval_response(friendly="Please confirm.", **payload):
    """Builds the string handle_post returns for a pending approval."""
    return f"{friendly}\n\n{APPROVAL_MARKER}{json.dumps(payload)}"


class TestParseApprovalDecision:
    @pytest.mark.parametrize("raw", [
        "yes", "YES", "  yes  ", "yes!", "yes.", "y", "yeah", "yep", "ok",
        "sure", "confirm", "approve", "proceed", "go ahead", "go   ahead",
        "do it", "yes please", "@agent1qxyz yes", "@a @b go ahead",
    ])
    def test_confirmations(self, raw):
        assert parse_approval_decision(raw) is True

    @pytest.mark.parametrize("raw", [
        "no", "NO", "n", "nope!", "cancel", "reject", "deny", "stop", "abort",
        "no thanks", "do not", "don't", "Don't.", "@agent1qxyz no",
    ])
    def test_rejections(self, raw):
        assert parse_approval_decision(raw) is False

    @pytest.mark.parametrize("raw", [
        "no, use the other light",     # a correction, not a rejection
        "yes, but only the kitchen one",
        "yes please turn it off",
        "I said no",
        "Yes, please",                 # interior comma survives .strip(".!?,")
        "maybe",
        "what does that do?",
        "",
        None,
        "@agent1qxyz",                 # a bare mention with nothing after it
    ])
    def test_anything_that_is_not_an_answer_returns_none(self, raw):
        """None means 'hold the approval and re-ask'. Widening any of these
        would widen what counts as consent to a destructive action."""
        assert parse_approval_decision(raw) is None


class TestFormatApprovalForChat:
    @pytest.mark.parametrize("text", ["", None, "just a normal reply", "no marker here"])
    def test_text_without_the_marker_is_returned_unchanged(self, text):
        assert format_approval_for_chat(text) == text

    def test_update_renders_both_configurations_in_order(self):
        out = format_approval_for_chat(_approval_response(
            old_yaml="alias: Old\n", new_yaml="alias: New\n",
        ))
        assert "**Current configuration:**" in out
        assert "**New configuration:**" in out
        assert out.index("**Current configuration:**") < out.index("**New configuration:**")
        assert out.count("```yaml") == 2
        assert out.endswith("_Reply **YES** to confirm or **NO** to cancel._")

    def test_create_renders_only_the_new_configuration(self):
        out = format_approval_for_chat(_approval_response(old_yaml="", new_yaml="alias: New\n"))
        assert out.count("```yaml") == 1
        assert "**Current configuration:**" not in out
        assert "**New configuration:**" in out

    def test_payload_with_no_yaml_renders_no_fence(self):
        out = format_approval_for_chat(_approval_response(old_yaml="", new_yaml=""))
        assert "```" not in out
        assert out.startswith("Please confirm.")
        assert out.endswith("_Reply **YES** to confirm or **NO** to cancel._")

    def test_whitespace_only_yaml_renders_no_fence(self):
        out = format_approval_for_chat(_approval_response(old_yaml="   \n", new_yaml=""))
        assert "```" not in out

    def test_an_unparseable_payload_never_reaches_the_user(self):
        """'Never leak a half-parsed payload' — the user gets the friendly half
        and nothing else, not a stray marker or a JSON fragment."""
        out = format_approval_for_chat(f"Please confirm.  \n\n{APPROVAL_MARKER}{{not json")
        assert out == "Please confirm."
        assert APPROVAL_MARKER not in out
        assert "{" not in out
        assert "_Reply" not in out
