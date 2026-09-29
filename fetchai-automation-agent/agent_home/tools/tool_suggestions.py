"""LLM tools over the agent's own suggestions: seeing them, and answering one.

Design §4 makes ASI1 the primary interface, and §9 states the rule these exist
for: "The plain-text rendering is the design constraint, not the fallback. If
it cannot be read and decided in a chat message, it does not ship." Before
these, core/review.py could raise a proposal through `gate.propose` and there
was no way at all for a chat user to see it, let alone answer it.

Not in tools/tool_homeassistant/: no tool here calls a Home Assistant API. They
read the add-on's own suggestion store and hand the rendering to
core/artefacts.py, which owns every rule about what a suggestion may claim.

Three tools, and TWO QUESTIONS THAT MUST NOT BE CONFUSED
--------------------------------------------------------

`List agent suggestions` shows both, in two separate lists. The other two each
put ONE thing to the user:

    Show an agent suggestion   a change nobody has approved — apply or dismiss
    Show a measured result     a change that has been running for a week —
                               keep or roll back

A single numbered list of the two would be how a "yes" comes to mean the
opposite of what the user thought, so they are never merged, and each tool
refuses a row in the other's state rather than rendering it.

The two-step contract
---------------------

The same shape as tools/tool_changes.py, and for a stricter reason on the
proposal side. There the operation was pre-authorised and only the SELECTION
needed confirming; a proposal is exactly the write nobody has said yes to — so
that is the harder case, not the softer one.

Neither showing tool applies, undoes or writes anything. Each returns the
artefact as a QUESTION, the agent loop recognises that in the tool RESULT, and
`rest_chat` parks it in its own session slot to be answered by the user's own
next message through `parse_approval_decision`, which does not use the LLM.
There is deliberately no `approved` or `keep` argument on either: a tool result
is built by this process and cannot be forged by the model, while an argument
is written by the model — and "the user said yes" is the one claim it must
never be able to make.

No tool here is in DESTRUCTIVE_TOOLS and none reaches `gate.apply`. Where the
two answers go differs, because they are different acts. A proposal's answer
lands on /rest/approve's change_id branch, the same one the panel's Accept and
Reject post to, so that a "no" reaches `review.settle` and becomes a thirty-day
cooldown rather than disappearing. A measured result's answer lands on
`verify.answer`, which records the verdict and — for a "no" — runs the inverse
core/gate.py computed when the change was approved, through the one module that
executes one. Neither is re-gated and neither invents a second rollback path.

What leaves the house
---------------------

Rendered sentences, the numbers inside them, and the ids needed to name one
suggestion. Not the automation config as a structure and not the raw replay or
verdict blobs: the config's plain-language readback is what §9 artefact 3 asks
for, the report is what artefact 4 asks for, and the blobs behind them are
several hundred fields of night-by-night grading that would be the model's
whole context spent on something nobody asked to see.
"""
from typing import Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core import activity
from core import artefacts
from core import disclosure
from core import rollback
from core import suggestions


class tool_list_suggestions(BaseTool):
    """LLM tool listing what the agent has proposed and is waiting on."""
    name: str = "List agent suggestions"
    description: str = (
        "List the suggestions this agent has raised by itself and is waiting for the user to "
        "answer — at most three energy-saving ones and three of the analyst's ideas, one line "
        "each. There is "
        "no input parameter. Call it when the user asks what you have found, what you are "
        "suggesting, whether anything needs their decision, or asks about saving energy. Each "
        "entry has a suggestion_id; pass one to 'Show an agent suggestion' to put it to the user. "
        "These are the agent's OWN proposals, not the user's automations and not the change "
        "journal."
    )
    # No args_schema, like every other no-argument tool in the registry
    # (tool_time, tool_record_overview, tool_pending_messages): there is one
    # set of open proposals and nothing to ask for.

    def _run(self) -> str:
        """The digest, §9 artefact 1, plus any measured results waiting on a word.

        Two lists, kept apart. A proposal is a change nobody has said yes to; a
        report is a change that has been running for a week and is now waiting
        to be kept or undone. They are answered by different tools and by
        different code paths, and merging them into one numbered list is how a
        "yes" ends up meaning the opposite of what the user thought.
        """
        rows = suggestions.proposed()
        payload = artefacts.digest(rows)
        # §9 artefact 4's one line each. Not run through `digest`, which is
        # artefact 1 and bounded at DIGEST_LINES because that is what a digest may
        # carry; a measured result the user is already living with is not a
        # question the agent chose to ask, so nothing is truncated away.
        reports = [row for row in suggestions.active() if row.get("state") == "verified"]
        payload["verified"] = [
            {
                "suggestion_id": row.get("id"),
                "change_id": row.get("change_id"),
                "line": artefacts.verification_headline(row),
            }
            for row in reports
        ]
        if not payload["count"] and not reports:
            payload["note"] = (
                "Nothing is waiting for an answer. Say exactly that — do not invent a "
                "suggestion, and do not offer to create an automation instead."
            )
        else:
            payload["note"] = (
                "Relay these lines as they are, with their numbers and the observation each "
                "one carries — a night window and a daytime one are two different claims, and "
                "the line says which. To put one to the user, call 'Show an agent suggestion' "
                "with its "
                "suggestion_id — one at a time, never several in one question. Anything under "
                "'verified' is a change already made and measured a week later: use 'Show a "
                "measured result' for those instead, never 'Show an agent suggestion'."
            )
        return disclosure.emit(payload)


class _ShowInput(BaseModel):
    """Input schema for putting one suggestion to the user."""
    suggestion_id: int = Field(
        description="The suggestion_id to show, from 'List agent suggestions'. Never guess it — "
                    "list the suggestions first and use the id of the one the user meant.",
        default=0,
    )


class tool_show_suggestion(BaseTool):
    """LLM tool that puts one suggestion to the user for a decision."""
    name: str = "Show an agent suggestion"
    description: str = (
        "Show one of the agent's own suggestions in full — what was observed, over what window, "
        "what automation would be written, and what it is estimated to save — and ask the user to "
        "decide. Pass the suggestion_id from 'List agent suggestions'. It applies NOTHING by "
        "itself: the user is shown the whole suggestion and must answer in their own words, and "
        "that answer is read outside you. Use it when the user wants to hear more about a "
        "suggestion, or says they want to accept or reject one — show it and let them confirm."
    )
    args_schema: Type[BaseModel] = _ShowInput

    def _run(self, suggestion_id: int = 0) -> str:
        """Renders the artefact and asks. Applies nothing, writes nothing.

        A refusal comes back in its own words — a suggestion already applied or
        already dismissed is a thing the user should be told about rather than
        a boolean the model has to phrase for itself.
        """
        return disclosure.emit(artefacts.question(suggestions.suggestion(suggestion_id)))


class _ResultInput(BaseModel):
    """Input schema for putting one measured result to the user."""
    suggestion_id: int = Field(
        description="The suggestion_id to report on, from the 'verified' list in "
                    "'List agent suggestions'. Never guess it — list them first and use the id "
                    "of the one the user meant.",
        default=0,
    )


class tool_show_verification(BaseTool):
    """LLM tool that puts one verification report to the user for a decision."""
    name: str = "Show a measured result"
    description: str = (
        "Show what actually happened a week after one of the agent's own suggestions was "
        "applied — the two weeks compared, what was measured, what had been estimated, and the "
        "choice to keep the automation or roll it back. Pass the suggestion_id from the "
        "'verified' list in 'List agent suggestions'. It changes NOTHING by itself: the user is "
        "shown the whole report and must answer in their own words, and that answer is read "
        "outside you. Use it when the user asks whether a change the agent made actually "
        "worked, or says they want to keep or undo one."
    )
    args_schema: Type[BaseModel] = _ResultInput

    def _run(self, suggestion_id: int = 0) -> str:
        """Renders §9 artefact 4 and asks. Applies nothing, undoes nothing.

        The rollback offer is checked against core/rollback.py's own rules
        rather than assumed, so the user is never offered an undo that would
        then be refused — and when there is none, the refusal is rollback's own
        sentence, which says what can be done instead.
        """
        row = suggestions.suggestion(suggestion_id)
        why_not = rollback.why_not(activity.change((row or {}).get("change_id"))) if row else ""
        return disclosure.emit(artefacts.verdict_question(row, why_not))
