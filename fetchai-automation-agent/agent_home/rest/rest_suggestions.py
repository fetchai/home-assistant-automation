"""REST handlers backing the panel's Suggestions tab: what the agent has raised
by itself, what became of it, and nothing else.

Design §9's artefacts 1 to 4, for the surface that can show them richly. The
rendering itself is core/artefacts.py's, not this module's and not the panel's:
§9's rules are about what a rendering may CLAIM — every number carrying its
observation, an estimate never printed as the replay's when the replay's own
figure was the one discarded, a saving never stated more confidently than the
range it was measured to — and a rule implemented once in Python and again in
JavaScript is a rule with two implementations. So this reads rows, joins each
to its journal entry, and hands over sentences.

**There is no approve endpoint here, deliberately.** The panel's Accept and
Reject buttons post the suggestion's `change_id` to the EXISTING /rest/approve,
which rebuilds the pending from the journal row (gate.pending_for), executes it
through gate.apply and settles the suggestion through review.settle. A second
endpoint would be a second place for "everything lands on apply" to stop being
true — the reason /rest/approve took a change_id in the first place — and a
second place for a rejection to forget to become a cooldown.

**The verdict endpoint is not that endpoint, and the difference is the whole
of why it may exist here.** Keep and Roll back answer a change that was
approved a week ago: "keep" writes nothing at all, and "roll back" runs the
inverse core/gate.py computed at approval time, through core/rollback.py, which
is the only thing that executes one. Neither reaches gate.apply, so neither is
a second way to make a write the user has not approved — and putting it here
rather than beside /rest/activity/rollback is what keeps the verdict and the
undo one act instead of two the panel would have to sequence.

The diff comes from the journal rather than from the suggestion row. The
suggestion holds the config as a structure, because the replay engine and the
gate read it that way; `changes.new_config` holds the YAML a human is shown,
written by the gate at propose time. Rendering the structure again here would
be a second YAML of one config, and the one the user reads should be the one
the approval was recorded against.

**The feedback endpoint is not an approve endpoint either.** An explorer
idea's card (free-hand F-11) offers Yes / No / No, because…; the panel posts
the answer HERE first, which only stores `{answer, reason, at}` on the row
(suggestions.set_feedback — no state move, no journal row, no gate), and then
posts Yes or No with the change_id to /rest/approve exactly as Accept and
Reject do. So the answer is recorded once and the change is decided once, by
the gate. A refused feedback stops the panel before /rest/approve: a "No,
because…" whose reason was lost is the thing F-11 exists to keep.

An explorer idea's entry carries its stored chart, `sent` and feedback AS
THEY ARE — the chart was drawn from the record at store time (§9: evidence
travels with the suggestion), so nothing here redraws it — and the sentences
around them from artefacts.shown_detail.

POST like every other endpoint here, because proxy.py answers GET from the
built panel directory and only forwards POST to the agent.
"""
import asyncio
import logging

from core import activity
from core import artefacts
from core import observation
from core import protocols
from core import rollback
from core import suggestions
from core import verify
from core.uagent import agent_1

from uagents import Context

logger = logging.getLogger(__name__)


def _entry(row) -> dict:
    """One open proposal: the artefact, and what the journal holds about it.

    `answerable` is the panel's guard against showing buttons that cannot work.
    It is read off the journal row rather than off the suggestion's state,
    because the journal is what /rest/approve will consult — a change answered
    from ASI1 thirty seconds ago is already decided there while this row still
    says `proposed`, and offering Accept for it would produce a refusal the
    user did not deserve.
    """
    change = activity.change(row.get("change_id")) or {}
    entry = {
        **artefacts.detail(row),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "entities": row.get("entities") or [],
        # The YAML the gate wrote when it raised the change, so the panel's
        # diff is over the same text the approval was recorded against.
        "old_yaml": change.get("old_config") or "",
        "new_yaml": change.get("new_config") or "",
        "answerable": change.get("decision") == "proposed",
        "decision": change.get("decision") or "",
    }
    chart = row.get("chart") if isinstance(row.get("chart"), dict) else None
    spec = chart.get("spec") if chart and isinstance(chart.get("spec"), dict) else {}
    # The household's names for the charted entities, from the cache the
    # Record tab reads (tasks/observation.py refreshes it); an id with no
    # name yet is its own label, never a blank.
    names = {e: observation.name_for(e)
             for e in spec.get("entities") or [] if isinstance(e, str)}
    shown = artefacts.shown_detail(row, names)
    if shown:
        # An explorer idea (F-WP5): the stored chart, sent and feedback as
        # they are, beside the sentences about them. Detector and audited
        # rows carry none of these keys.
        entry.update(shown)
        entry["chart"] = chart
        entry["chart_names"] = names
        entry["sent"] = row.get("sent") if isinstance(row.get("sent"), list) else []
        entry["feedback"] = row.get("feedback") if isinstance(row.get("feedback"), dict) else None
    return entry


def _report(row) -> dict:
    """One measured result: §9 artefact 4, with the rollback offer checked.

    `why_not` is asked of core/rollback.py's own `_check`, the same rules the
    Activity tab's undo button and the chat tool ask, so the panel never offers
    a Roll back that would come back refused — and never invents a reason of
    its own for one it cannot offer.
    """
    change = activity.change(row.get("change_id")) or {}
    return {
        **artefacts.verification(row, rollback.why_not(change)),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "entities": row.get("entities") or [],
    }


def _snapshot(limit: int, error: str = "", outcome: str = "") -> protocols.SuggestionsResponse:
    """The open proposals and the delivered reports, rendered.

    `error` and `outcome` carry the verdict endpoint's answer back beside a
    listing that already reflects it, the way rest_activity's rollback handler
    does: whatever happened, the row that says so exists by the time this
    returns, so the panel cannot show an outcome that disagrees with the record
    it was read from.
    """
    limit = max(1, int(limit or 1))
    rows = suggestions.proposed()[:limit]
    reports = [r for r in suggestions.active() if r.get("state") == "verified"][:limit]
    return protocols.SuggestionsResponse(
        digest=artefacts.digest(rows)["suggestions"],
        open=[_entry(row) for row in rows],
        verified=[_report(row) for row in reports],
        totals=suggestions.stats(),
        error=error,
        outcome=outcome,
    )


@agent_1.on_rest_post("/rest/suggestions", protocols.SuggestionsRequest,
                      protocols.SuggestionsResponse)
async def handle_suggestions(ctx: Context,
                             req: protocols.SuggestionsRequest) -> protocols.SuggestionsResponse:
    """Returns the open proposals, best first, already rendered.

    A local SQLite read and no Home Assistant call, like the Activity tab it
    sits beside: this is the surface that has to keep working when the
    connection being audited does not.
    """
    return await asyncio.get_running_loop().run_in_executor(None, _snapshot, req.limit)


@agent_1.on_rest_post("/rest/suggestions/verdict", protocols.SuggestionsRequest,
                      protocols.SuggestionsResponse)
async def handle_verdict(ctx: Context,
                         req: protocols.SuggestionsRequest) -> protocols.SuggestionsResponse:
    """Records the user's answer to a measured result, and answers with the
    refreshed listing.

    §7.2's keep-or-roll-back, from the surface where it is two buttons. The
    decision itself is core/verify.py's `answer` — the same call an ASI1 "yes"
    reaches — so the verdict is recorded once, the rollback runs through the
    one module that executes an inverse, and a failed undo leaves the report
    open rather than a row claiming a home was changed when it was not.

    Unlike the listing above this can reach Home Assistant, so the executor is
    doing more than keeping SQLite off the event loop.
    """
    def _answer_and_read():
        answered = verify.answer(req.suggestion_id, req.keep, session_id="panel")
        return _snapshot(
            req.limit,
            error="" if answered.get("ok") else answered.get("message", ""),
            outcome=answered.get("message", "") if answered.get("ok") else "",
        )

    response = await asyncio.get_running_loop().run_in_executor(None, _answer_and_read)
    logger.info(f"SUGGESTIONS: verdict on suggestion {req.suggestion_id} "
                f"(keep={req.keep}) from the panel")
    return response


@agent_1.on_rest_post("/rest/suggestions/feedback", protocols.SuggestionsRequest,
                      protocols.SuggestionsResponse)
async def handle_feedback(ctx: Context,
                          req: protocols.SuggestionsRequest) -> protocols.SuggestionsResponse:
    """Records the household's answer to an explorer idea — Yes, No or No,
    because… — and answers with the refreshed listing (free-hand F-11).

    It RECORDS and does nothing else: suggestions.set_feedback writes the
    `feedback` column and nothing reaches the gate, the journal, rollback or
    the suggestion's state. The change itself is decided by /rest/approve,
    which the panel calls next with the row's change_id — see the module
    docstring. A local SQLite write, off the event loop like the listing.
    """
    def _record_and_read():
        stored = suggestions.set_feedback(req.suggestion_id, req.answer, req.reason)
        return _snapshot(
            req.limit,
            error="" if stored else "The agent could not record that answer, so nothing "
                                    "was sent on. Answer again from the refreshed card.",
        )

    response = await asyncio.get_running_loop().run_in_executor(None, _record_and_read)
    logger.info(f"SUGGESTIONS: feedback {req.answer!r} on suggestion {req.suggestion_id}"
                f" from the panel{'' if not response.error else ' — refused'}")
    return response
