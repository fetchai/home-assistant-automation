import { ref } from "vue";

const agentBaseUrl = ".";

// POST like everything else: proxy.py answers GET out of the built panel
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

// What the agent did, and what it is still holding. This reads local SQLite
// and touches no Home Assistant API, which is the point: it is the audit
// surface, so it has to keep working when the thing being audited does not.
export function useActivity() {
  const changes = ref([]);
  const messages = ref([]);
  const totals = ref({});
  const loading = ref(false);
  const error = ref("");
  // Which change is being undone right now, so one row can show it without
  // every other row's button greying out.
  const rollingBack = ref(0);
  // Kept apart from `error`, which every listing response overwrites. The tab
  // refreshes itself every fifteen seconds, so a failed rollback reported in
  // the shared field would erase itself before the user finished reading it —
  // and §11 is explicit that a failed rollback is never quietly swallowed.
  const rollbackError = ref("");

  function _apply(data) {
    changes.value = data.changes || [];
    messages.value = data.messages || [];
    totals.value = data.totals || {};
    error.value = data.error || "";
  }

  async function loadActivity() {
    loading.value = true;
    try {
      _apply(await _post("/rest/activity"));
    } catch (e) {
      error.value = "Could not read the activity log.";
    } finally {
      loading.value = false;
    }
  }

  // Opening the tab is what marks messages seen — and only for the panel. A
  // message read here is still waiting for ASI1, because the person at the
  // panel and the person in the chat are not necessarily the same person.
  async function openActivity() {
    loading.value = true;
    try {
      _apply(await _post("/rest/activity/seen"));
    } catch (e) {
      error.value = "Could not read the activity log.";
    } finally {
      loading.value = false;
    }
  }

  // Undoing a recorded change. There is no approval round-trip: the agent
  // treats a rollback as pre-authorised by the approval that let the original
  // change through, and the reasoning lives in the agent's core/rollback.py so
  // that every surface agrees with one argument rather than each making its own.
  //
  // The endpoint answers with the whole refreshed journal, failure included, so
  // there is nothing to reconcile here — whatever comes back IS the record.
  async function rollBackChange(changeId) {
    rollingBack.value = changeId;
    rollbackError.value = "";
    try {
      const data = await _post("/rest/activity/rollback", { change_id: changeId });
      rollbackError.value = data.error || "";
      _apply({ ...data, error: "" });
    } catch (e) {
      rollbackError.value =
        "The rollback request did not reach the agent, so nothing is known to have changed. " +
        "Check the Changes list below before trying again.";
    } finally {
      rollingBack.value = 0;
    }
  }

  return {
    changes, messages, totals, loading, error, rollingBack, rollbackError,
    loadActivity, openActivity, rollBackChange,
    dismissRollbackError: () => { rollbackError.value = ""; },
  };
}
