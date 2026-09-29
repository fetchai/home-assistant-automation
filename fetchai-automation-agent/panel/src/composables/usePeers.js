import { ref, computed } from "vue";

const agentBaseUrl = ".";

// POST like every other endpoint: proxy.py answers GET from the built panel
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

// Other agents that asked to work with this home. This panel is the only place
// one becomes trusted: it is behind Home Assistant's login, which is what makes
// an approval here a person's decision (core/peers.py).
export function usePeers() {
  const peers = ref([]);
  const levels = ref([]);
  const proposals = ref([]);
  const outcome = ref("");
  const busy = ref(false);
  const error = ref("");

  const pending = computed(() => peers.value.filter((p) => p.state === "pending"));
  const approved = computed(() => peers.value.filter((p) => p.state === "approved"));
  const declined = computed(() =>
    peers.value.filter((p) => p.state === "denied" || p.state === "blocked")
  );
  // The other direction: agents this home may ask (phase 3), whatever access
  // they have to it themselves.
  const askable = computed(() => peers.value.filter((p) => p.ask && p.state !== "blocked"));

  async function _call(path, body) {
    busy.value = true;
    error.value = "";
    try {
      const data = await _post(path, body);
      peers.value = data.peers || [];
      levels.value = data.levels || [];
      proposals.value = data.proposals || [];
      error.value = data.error || "";
    } catch (e) {
      error.value = "Could not reach the agent.";
    } finally {
      busy.value = false;
    }
  }

  const loadPeers = () => _call("/rest/peers/list");
  const decide = (address, action, level = "read") =>
    _call("/rest/peers/decide", { address, action, level });
  const removePeer = (address) => _call("/rest/peers/remove", { address });
  const addAskable = (address, name, purpose, confirm) =>
    _call("/rest/peers/ask/add", { address, name, purpose, confirm });
  const setConfirm = (address, confirm) => _call("/rest/peers/ask/confirm", { address, confirm });
  const removeAskable = (address) => _call("/rest/peers/ask/remove", { address });

  // An agent's proposal is answered like every question nobody asked in a
  // conversation: its change_id to /rest/approve, which lands on the gate's
  // apply. The reply is the gate's readback of what was actually done.
  async function answerProposal(changeId, approved) {
    busy.value = true;
    outcome.value = "";
    try {
      const data = await _post("/rest/approve", { change_id: changeId, approved });
      outcome.value = data.text || "";
    } catch (e) {
      outcome.value =
        "The answer did not reach the agent, so nothing is known to have changed. " +
        "Check the Activity tab before answering again.";
    } finally {
      busy.value = false;
    }
    await loadPeers();
  }

  return {
    peers, levels, proposals, outcome, pending, approved, declined, askable, busy, error,
    loadPeers, decide, removePeer, answerProposal, addAskable, removeAskable, setConfirm,
  };
}
