"""F-WP7: README "What leaves your home" states the egress bounds in numbers a
household reads. Each is read from the constant that sets it, so a changed
cap fails here until the page says the new figure.
"""
import os

from core import analyst
from core import record
from tools import tool_record_query as trq
from tools import tool_record_timeline as trt
from tools import tool_record
from tools.tool_homeassistant import history

_README = os.path.join(os.path.dirname(__file__), "..", "README.md")


def _section():
    with open(_README, encoding="utf-8") as f:
        text = f.read()
    start = text.index("## What leaves your home")
    # Wrapped lines are one sentence to a reader: compare with spaces.
    return " ".join(text[start:text.index("\n## ", start + 1)].split())


def test_every_number_is_the_codes():
    s = _section()
    assert f"up to {history.MAX_TRANSITIONS} changes over up to {history.MAX_HISTORY_HOURS // 24} days" in s
    assert f"after {record.RETENTION_DAYS} days by default" in s
    assert f"at most {trq.QUERY_MAX_ROWS} rows and {trq.QUERY_MAX_BYTES // 1024} KB" in s
    assert f"({trq.DEFAULT_MAX_ROWS} rows unless it asks for more)" in s
    assert f"at most {trt.MAX_ENTITIES} devices over at most {trt.MAX_DAYS} days" in s
    assert f"at most {analyst.EXPLORER_MAX_QUERIES} searches and timelines together" in s
    assert f"at most {analyst.EXPLORER_MAX_STEPS} calls to the model" in s
    assert f"at most {analyst.EXPLORER_MAX_CALLS} tool calls in all" in s
    assert f"(up to {tool_record.MAX_RECENT} per device)" in s
    assert f"up to {analyst.SENT_ANSWER_MAX_CHARS // 1024} KB of each" in s
    assert f"after {analyst.ANALYST_HOUR:02d}:00" in s
    assert (f"your last {analyst.FEEDBACK_PROMPT_MAX} answers to its earlier ideas "
            f"(at most {analyst.FEEDBACK_PROMPT_MAX_BYTES // 1024} KB of them)") in s


def test_the_card_list_is_described_as_the_card_says_it():
    """2.21.6: `sent` holds every tool answer, and the card's note and this
    page say so in the same words."""
    from core import artefacts
    phrase = "every answer the agent received"
    assert phrase in _section()
    assert phrase in artefacts.SENT_NOTE.lower()
    assert "overview" in artefacts.SENT_NOTE


def test_it_names_the_way_to_stop_it():
    s = _section()
    assert "`analyst_enabled: false`" in s
    assert "analyst_mode" not in s, "retired with the audited run (free-hand F-9)"
