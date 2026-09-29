"""The in-process session store.

Nothing here survives a restart by design, but while it lives it holds the
conversation history and — more importantly — any parked destructive approval.
"""
from core import agent_runtime as rt
from core import settings


def test_a_new_session_starts_empty():
    memory = rt.get_memory("session-a")
    assert set(memory) == {"timestamp", "messages", "pending_approval", "model"}
    assert memory["messages"] == []
    assert memory["pending_approval"] is None
    assert memory["model"] == "asi1-mini"
    assert isinstance(memory["timestamp"], int)


def test_the_same_session_gets_the_same_object_back():
    """Callers mutate the returned dict in place and expect it to stick."""
    first = rt.get_memory("session-a")
    first["messages"].append("hello")
    first["pending_approval"] = {"tool_name": "Delete Home Assistant automation"}

    second = rt.get_memory("session-a")
    assert second is first
    assert second["messages"] == ["hello"]
    assert second["pending_approval"]["tool_name"] == "Delete Home Assistant automation"


def test_sessions_are_independent():
    rt.get_memory("session-a")["messages"].append("only mine")
    assert rt.get_memory("session-b")["messages"] == []


def test_dropping_a_session_reports_whether_there_was_one():
    rt.get_memory("session-a")
    assert rt.drop_session("session-a") is True
    assert rt.drop_session("session-a") is False
    assert rt.drop_session("never-seen") is False


def test_a_new_session_defaults_to_the_configured_asi1_model(monkeypatch):
    """A session that never picks a model (an ASI1 chat session — the panel
    always sends one) must fall back to the add-on's asi1_model option, not a
    hardcoded 'asi1-mini' that would make the option unreachable."""
    monkeypatch.setattr(settings, "ASI1_MODEL", "asi1-fast")

    memory = rt.get_memory("session-configured-model")

    assert memory["model"] == "asi1-fast"


def test_dropping_a_session_discards_a_parked_approval():
    """Called when an identity is unpaired: a confirmation the user is no longer
    willing to grant must not stay answerable, and a re-paired address must not
    resume the old conversation."""
    memory = rt.get_memory("session-a")
    memory["messages"].append("earlier conversation")
    memory["pending_approval"] = {"tool_name": "Delete Home Assistant automation"}

    rt.drop_session("session-a")

    fresh = rt.get_memory("session-a")
    assert fresh["pending_approval"] is None
    assert fresh["messages"] == []
