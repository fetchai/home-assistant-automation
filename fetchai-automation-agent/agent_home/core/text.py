"""Chat-text normalisation shared by the approval gate and the pairing gate.

Both gates decide something security-relevant from what the user typed, and both
have to undo the same ASI1 quirk first. This module holds that shared parsing so
neither has to import the other: core.agent_runtime pulls in the whole LLM stack,
and core.pairing — reached from core.uagent — must stay light and dependency-free.
Nothing here imports anything but the standard library, which is also what makes
it testable without constructing the uAgent.
"""
import re

# ASI1 prefixes every message with the agent's @address mention, so a bare "yes"
# arrives as "@agent1q… yes".
_LEADING_MENTIONS = re.compile(r"^\s*(?:@\S+\s+)+")

# A code typed into ASI1 arrives with the agent's @address in front of it and
# possibly some politeness around it, so accept "pair 123456" as well as a bare
# six-digit message. The panel displays the code as two space-separated groups
# of three digits for readability ("417 311"), so that split is accepted too —
# whether the sender types it as shown or squashes it back together.
_PAIRING_CODE = re.compile(r"^(?:pair[\s:]+)?(\d{3})\s?(\d{3})$")


def strip_leading_mentions(text: str) -> str:
    """Removes the @address prefixes ASI1 puts in front of every message, so
    the rest of the code can match on what the user actually typed."""
    return _LEADING_MENTIONS.sub("", text or "")


def parse_pairing_code(text: str):
    """Reads a chat message as a pairing code, or None if it is not one.

    Matches the whole normalised message rather than searching inside it, for
    the same reason parse_approval_decision does — "call me on 555123" is not
    an attempt to pair."""
    normalized = " ".join(strip_leading_mentions(text).strip().lower().split())
    match = _PAIRING_CODE.match(normalized)
    return f"{match.group(1)}{match.group(2)}" if match else None
