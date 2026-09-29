"""The analyst's submit tool — a schema the model fills in, and nothing else.
`docs/PROPOSAL-FREE-HAND-ANALYST.md` F-5/F-6: `Submit ideas`, the explorer
run's one way to finish. (`Submit shortlist` and `Submit automation
candidate`, the audited run's two, went with it when F-9 retired that run,
2.23.0.)

`core/analyst.py` runs ONE strong pass that reads the house and ends by
calling `Submit ideas`. The loop intercepts it BY NAME, captures the arguments
and stops. So `_run` below is never what decides anything — the tool class
exists so a schema can be bound, and `_run` answers a fixed acknowledgement in
case something executes it anyway.

WHY IT WRITES NOTHING, AND WHY CHAT NEVER HOLDS IT
----------------------------------------------------
It is not in `agent_runtime.tools`, and a test holds that. A submission is not
a suggestion — nothing has checked it — and the one thing that may turn it
into one is `core/analyst.py`, after the pass has ended: the house checks
(`auditor.check_config`, `duplicate_of`) and a chart drawn from the record. A
tool that stored or proposed from inside the model's own loop would put the
model's words in front of the user unchecked, which is the exact failure
design §15.2 exists to prevent. For the same reason the model never sees a
verdict: the loop appends no ToolMessage for it (refuse, never repair — the
refusal goes to the log, not back to the model).

Not in tools/tool_homeassistant/: it does not talk to Home Assistant, and
there is no export to add — like tool_record and tool_suggestions, the one
importer (`core/analyst.py`) reaches the module directly.

The chart's times are strings, `YYYY-MM-DD HH:MM:SS` or 'now', because every
read tool prints local wall-clock time and the model copies what it was shown;
core/charts.py converts them (`window.local_epoch`).
"""
from typing import Any, Dict, List, Optional, Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import disclosure


_RECEIVED = {"received": True}


class _ChartRequestInput(BaseModel):
    """What the explorer asks code to draw (F-7). The rules per type are the
    explorer prompt's (`settings.analyst_explorer_message`); `core/analyst.py`
    checks them and stores a spec it cannot draw with its error rather than
    dropping the idea."""
    type: str = Field(description="One of 'timeline', 'heatmap', 'overlay', 'daily_count'.")
    entities: List[str] = Field(description="The watched entity_ids to draw, exactly as a tool "
                                            "showed them.")
    state: Optional[str] = Field(default=None,
                                 description="The state to count, for 'heatmap' and "
                                             "'daily_count' (e.g. 'on').")
    start: str = Field(description="Local time 'YYYY-MM-DD HH:MM:SS' as the tools print it.")
    end: str = Field(description="Local time 'YYYY-MM-DD HH:MM:SS', or 'now'.")


class _ExplorerIdeaInput(BaseModel):
    """One idea from the explorer run (F-6): a sentence, a rule, a config and
    the chart that shows it."""
    title: str = Field(description="A short name for the automation, at most 80 characters, "
                                   "as the user will see it.")
    finding: str = Field(description="ONE sentence: what you saw in the record.")
    rule_text: str = Field(description="The automation in plain words: when it runs and what "
                                       "it does.")
    config: Dict[str, Any] = Field(description="The full Home Assistant automation config: "
                                               "alias, trigger, optional condition, action.")
    chart: _ChartRequestInput = Field(description="The chart of the recorded data that SHOWS the "
                                                  "finding to the person who will judge it.")


class _IdeasInput(BaseModel):
    """Input schema for `Submit ideas`."""
    ideas: List[_ExplorerIdeaInput] = Field(
        description="One to three ideas for NEW automations, best first. Submitting ends the run."
    )


class tool_submit_ideas(BaseTool):
    """The explorer run's one way to finish."""
    name: str = "Submit ideas"
    description: str = (
        "Submit one to three ideas for NEW Home Assistant automations, best first. Each has a "
        "title, a one-sentence finding, the rule in plain words, the full config, and the chart "
        "request that shows the finding. Call it exactly once. Calling it ends the run; nothing "
        "is returned to you."
    )
    args_schema: Type[BaseModel] = _IdeasInput

    def _run(self, ideas=None) -> str:
        # The loop intercepts this call by name and never executes it (D-3).
        return disclosure.emit(_RECEIVED)
