import { computed, ref } from "vue";

import { addedLines, diffLines } from "@/composables/useDiff";

const agentBaseUrl = ".";

// POST like everything else: proxy.py answers GET from the built panel
// directory and only forwards POST to the agent.
async function _post(path, body = {}) {
  const res = await fetch(`${agentBaseUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}

// What the agent found on its own and is waiting to be told about — and what
// became of the ones already applied.
//
// Two things this composable deliberately does NOT do. It does not render a
// suggestion or a report — every sentence arrives from core/artefacts.py
// already written, because design §9's rules are about what a rendering may
// claim and a rule implemented twice is a rule that drifts. And it does not
// have an endpoint of its own for an APPROVAL: Accept and Reject post the
// suggestion's change_id to /rest/approve, the same one an ASI1 "yes" reaches,
// so both surfaces land on the gate's apply and both tell the suggestion store
// what became of it.
//
// The verdict endpoint it does post to is the other side of that boundary.
// Keep writes nothing; Roll back runs the inverse the gate recorded when the
// change was approved a week ago. Neither is an approval, and neither reaches
// gate.apply.
export function useSuggestions() {
  const open = ref([]);
  // §9 artefact 4: the measured results waiting to be kept or rolled back.
  // Its own list, never merged with `open` — a proposal is a change nobody has
  // said yes to and a report is one that has been running for a week, and one
  // list of the two is how a click comes to mean the opposite of what was
  // intended.
  const verified = ref([]);
  const totals = ref({});
  const loading = ref(false);
  const error = ref("");
  // Which change is being answered right now, so one card can say so without
  // the others' buttons going dead.
  const answering = ref(0);
  // ...and which report. Kept apart from `answering` because they address
  // different things: one names a change_id, the other a suggestion_id.
  const deciding = ref(0);
  // The agent's own words about what actually happened — gate.readback or
  // rollback's own message, not a summary of either. Kept apart from `error`,
  // which the next listing overwrites: §9 asks for every approval to be echoed
  // back with what was done, and an echo erased by a refresh is no echo.
  const outcome = ref("");

  const count = computed(() => open.value.length + verified.value.length);

  function _apply(data) {
    open.value = (data.open || []).map(row => ({
      ...row,
      // Computed once here rather than in the template, so a card that is
      // expanded and collapsed does not recompute it on every toggle.
      diff: diffLines(row.old_yaml, row.new_yaml),
      // An analyst suggestion's readback IS its config (proposal §5, B-4), so
      // it is laid out as the diff of a new automation — every line added —
      // from the YAML core/artefacts.py rendered for the chat as well.
      readbackDiff: row.source === "analyst" ? addedLines(row.readback_yaml) : [],
    }));
    verified.value = data.verified || [];
    totals.value = data.totals || {};
    error.value = data.error || "";
    if (data.outcome) outcome.value = data.outcome;
  }

  async function loadSuggestions() {
    loading.value = true;
    try {
      _apply(await _post("/rest/suggestions"));
    } catch (e) {
      error.value = "Could not read the agent's suggestions.";
    } finally {
      loading.value = false;
    }
  }

  // The existing approval endpoint, addressed by change_id. There is no
  // suggestion-shaped variant of it on purpose: a second entry would be a
  // second place for "every write lands on gate.apply" to stop being true, and
  // a second place for a rejection to forget to become a cooldown.
  async function answerSuggestion(changeId, approved) {
    answering.value = changeId;
    outcome.value = "";
    try {
      const data = await _post("/rest/approve", { change_id: changeId, approved });
      outcome.value = data.text || "";
    } catch (e) {
      outcome.value =
        "The answer did not reach the agent, so nothing is known to have changed. " +
        "Check the Activity tab before answering again.";
    } finally {
      answering.value = 0;
      // Whatever happened, the listing is read back from the store rather than
      // patched here: an answered suggestion leaves this list because the
      // journal says it was answered, not because a button was clicked.
      await loadSuggestions();
    }
  }

  // An explorer idea's Yes / No / No, because… (free-hand F-11). Two posts, in
  // this order. The first only RECORDS what the household said and why on the
  // suggestion row — it approves and rejects nothing. The second is the same
  // /rest/approve with the change_id Apply and Dismiss post, so the gate is
  // still the one thing that decides and executes. A refused or lost first
  // post stops here: a "No, because…" whose reason did not reach the agent is
  // worth answering again, and nothing has been decided yet.
  async function answerIdea(suggestionId, changeId, answer, reason = "") {
    answering.value = changeId;
    outcome.value = "";
    let recorded = false;
    try {
      const data = await _post("/rest/suggestions/feedback",
        { suggestion_id: suggestionId, answer, reason });
      recorded = !data.error;
      if (!recorded) outcome.value = data.error;
    } catch (e) {
      outcome.value =
        "The answer did not reach the agent, so nothing was decided. Answer again.";
    }
    if (!recorded) {
      answering.value = 0;
      await loadSuggestions();
      return;
    }
    await answerSuggestion(changeId, answer === "yes");
  }

  // §7.2's keep-or-roll-back. A different endpoint from the one above and
  // deliberately so: this answers a change the user approved a week ago, so
  // "keep" writes nothing at all and "roll back" runs the inverse the gate
  // recorded at the time, through the one module that executes one. The
  // response carries the refreshed listing with it, so the card leaves this
  // list because the store says it was answered rather than because a button
  // was clicked.
  async function decideVerification(suggestionId, keep) {
    deciding.value = suggestionId;
    outcome.value = "";
    try {
      _apply(await _post("/rest/suggestions/verdict", { suggestion_id: suggestionId, keep }));
    } catch (e) {
      // `outcome`, not `error`: the refresh below re-reads the listing and
      // _apply overwrites `error` with whatever the fresh response carries,
      // which would wipe this the instant it was set. §11 wants a rollback
      // whose outcome is unknown said out loud, and an echo erased by a
      // refresh is no echo — the same reason answerSuggestion uses this slot.
      outcome.value =
        "The answer did not reach the agent, so it is not known whether anything was " +
        "removed. Check the Activity tab before answering again.";
      await loadSuggestions();
    } finally {
      deciding.value = 0;
    }
  }

  return {
    open, verified, totals, loading, error, answering, deciding, outcome, count,
    loadSuggestions, answerSuggestion, answerIdea, decideVerification,
    dismissOutcome: () => { outcome.value = ""; },
  };
}
