import { ref, computed, onUnmounted } from "vue";

const agentBaseUrl = ".";

// Every pairing endpoint is POST, including the read-only one: proxy.py answers
// GET out of the built panel directory and only forwards POST to the agent.
async function _post(path, body = {}) {
  const res = await fetch(`${agentBaseUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}

function _nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

// Which uAgent identities may talk to this home. The panel is never gated —
// it is behind Home Assistant ingress, and that is exactly what makes a code
// shown here proof that whoever sends it back from ASI1 is in the house.
export function usePairing() {
  const agentAddress = ref("");
  const paired = ref([]);
  const code = ref("");
  const codeExpiresAt = ref(0);
  const busy = ref(false);
  const error = ref("");
  const now = ref(_nowSeconds());

  let watchTimer = null;

  const secondsLeft = computed(() =>
    codeExpiresAt.value ? Math.max(0, codeExpiresAt.value - now.value) : 0
  );
  const codeLive = computed(() => !!code.value && secondsLeft.value > 0);
  const countdown = computed(() => {
    const s = secondsLeft.value;
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  });

  function _apply(data) {
    agentAddress.value = data.agent_address || "";
    paired.value = data.paired || [];
    code.value = data.code || "";
    codeExpiresAt.value = data.code_expires_at || 0;
    now.value = _nowSeconds();
  }

  function _stopWatch() {
    if (watchTimer) {
      clearInterval(watchTimer);
      watchTimer = null;
    }
  }

  // Pairing finishes in ASI1, not here, so while a code is outstanding the
  // strip polls: it has no other way to notice that someone redeemed it. The
  // same ticker runs the countdown, and both stop as soon as the code is gone.
  function _startWatch() {
    if (watchTimer) return;
    let ticks = 0;
    watchTimer = setInterval(async () => {
      now.value = _nowSeconds();
      if (!codeLive.value) {
        _stopWatch();
        await loadStatus();
        return;
      }
      if (++ticks % 5 === 0) await loadStatus();
    }, 1000);
  }

  async function _call(path, body) {
    busy.value = true;
    error.value = "";
    try {
      _apply(await _post(path, body));
      if (codeLive.value) _startWatch();
    } catch (e) {
      error.value = "Could not reach the agent.";
    } finally {
      busy.value = false;
    }
  }

  const loadStatus = () => _call("/rest/pairing/status");
  const issueCode = () => _call("/rest/pairing/code");
  const unpair = (address) => _call("/rest/pairing/unpair", { address });

  onUnmounted(_stopWatch);

  return {
    agentAddress, paired, code, busy, error, secondsLeft, codeLive, countdown,
    loadStatus, issueCode, unpair,
  };
}
