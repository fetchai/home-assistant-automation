<template>
  <!-- Who may reach this agent from ASI1, and how to grant it. The panel
       itself is never gated: it is behind Home Assistant ingress, and that is
       exactly what makes a code shown here proof that whoever sends it back
       from ASI1 is in the house. -->
  <div class="pairing">
    <div class="body-inner">
      <p v-if="error" class="notice error">{{ error }}</p>

      <section class="block">
        <h2 class="heading">Your agent's address</h2>
        <p class="lede">
          This is what you talk to in ASI1. It is public — anyone who knows it can send this agent a
          message, which is why pairing exists.
        </p>
        <div class="addr-row">
          <code ref="addrEl" class="addr">{{ agentAddress || "…" }}</code>
          <button type="button" class="btn btn-ghost" :disabled="!agentAddress" @click="copyAddress">
            {{ copyLabel }}
          </button>
        </div>
      </section>

      <section class="block">
        <div class="heading-row">
          <h2 class="heading">Paired devices</h2>
          <button type="button" class="btn btn-accent" :disabled="busy" @click="issueCode">
            {{ paired.length ? "Pair another" : "Pair a device" }}
          </button>
        </div>

        <!-- A live code: the one time-critical thing on this screen. -->
        <div v-if="codeLive" class="code-box">
          <code class="code">{{ spacedCode }}</code>
          <div class="code-text">
            <span>Send this code to the agent from ASI1.</span>
            <span class="muted">Expires in {{ countdown }} · usable once</span>
          </div>
        </div>

        <ul v-if="paired.length" class="list">
          <li v-for="p in paired" :key="p.address">
            <span :class="['dot', 'dot-on']" aria-hidden="true"></span>
            <code class="addr small" :title="p.address">{{ shorten(p.address) }}</code>
            <span class="muted small since">paired {{ since(p.paired_at) }}</span>
            <button type="button" class="link danger" :disabled="busy" @click="unpair(p.address)">
              Unpair
            </button>
          </li>
        </ul>
        <p v-else class="notice">
          No devices paired yet. Until one is, the agent replies to ASI1 messages with instructions to
          pair and does nothing else — no reasoning, no tools, and no tokens spent.
        </p>
      </section>

      <section class="block">
        <h2 class="heading">How to pair from ASI1</h2>
        <ol class="steps">
          <li>
            <strong>Copy the agent's address</strong> above, and start a chat with it in ASI1.
          </li>
          <li>
            <strong>Press “Pair a device”</strong> here. A six-digit code appears, good for ten minutes
            and usable once.
          </li>
          <li>
            <strong>Send the code to the agent</strong> in that ASI1 chat. Any of these work:
            <code class="inline">417311</code>, <code class="inline">417 311</code> or
            <code class="inline">pair 417311</code> — and the <code class="inline">@address</code> ASI1
            puts in front of your message is ignored.
          </li>
          <li>
            The agent replies <em>“Paired. This home is now bound to your address”</em>, and the device
            appears in the list above. Now ask it for things.
          </li>
        </ol>
        <p class="notice muted">
          Five wrong codes cancels the code — ask for a new one. Several people can pair, and any of
          them can approve changes. Unpairing takes effect at once and also discards that identity's
          conversation, including any confirmation it had waiting.
        </p>
      </section>

      <!-- Other agents (PeersSection), in this tab's one scroll area. -->
      <slot />
    </div>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref } from "vue";

const props = defineProps({
  agentAddress: { type: String, default: "" },
  paired: { type: Array, default: () => [] },
  code: { type: String, default: "" },
  codeLive: { type: Boolean, default: false },
  countdown: { type: String, default: "" },
  busy: { type: Boolean, default: false },
  error: { type: String, default: "" },
});

const emit = defineEmits(["issue-code", "unpair"]);

const issueCode = () => emit("issue-code");
const unpair = (address) => emit("unpair", address);

const addrEl = ref(null);
const copyState = ref("");
let copyTimer = null;

const copyLabel = computed(() =>
  copyState.value === "copied" ? "Copied" : copyState.value === "failed" ? "Press Ctrl+C" : "Copy"
);

// Home Assistant is usually reached over plain http on the LAN, which is not a
// secure context, so navigator.clipboard is frequently undefined here and the
// old execCommand path is the one that actually runs. If both fail, select the
// address so the keyboard shortcut still works.
function _copy(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => true, () => _copyFallback(text));
  }
  return Promise.resolve(_copyFallback(text));
}

function _copyFallback(text) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

function _selectAddress() {
  if (!addrEl.value || !window.getSelection) return;
  const range = document.createRange();
  range.selectNodeContents(addrEl.value);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

async function copyAddress() {
  if (!props.agentAddress) return;
  const ok = await _copy(props.agentAddress);
  if (!ok) _selectAddress();
  copyState.value = ok ? "copied" : "failed";
  clearTimeout(copyTimer);
  copyTimer = setTimeout(() => (copyState.value = ""), 2500);
}

onUnmounted(() => clearTimeout(copyTimer));

// Grouped so a six-digit code can be read off the screen and typed into
// another app without losing your place.
const spacedCode = computed(() => `${props.code.slice(0, 3)} ${props.code.slice(3)}`);

// Paired identities are only being identified, not copied, so they are
// shortened to their distinctive ends with the full value in the title. The
// agent's own address above is shown in full — that one exists to be copied.
const shorten = (a) => (a && a.length > 26 ? `${a.slice(0, 14)}…${a.slice(-8)}` : a);

function since(ts) {
  if (!ts) return "";
  const days = Math.floor((Date.now() / 1000 - ts) / 86400);
  if (days < 1) return "today";
  return days === 1 ? "yesterday" : `${days} days ago`;
}
</script>

<style scoped>
.pairing {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.body-inner {
  max-width: 820px;
  margin: 0 auto;
  padding: 20px;
}

.block + .block {
  margin-top: 28px;
  padding-top: 22px;
  border-top: 1px solid var(--border);
}

.heading {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.heading-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.heading-row .heading {
  margin: 0;
}

.lede {
  margin: 0 0 10px;
  font-size: 13.5px;
  color: var(--text-muted);
}

.addr-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.addr {
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--text);
  /* The address is ~65 characters and the whole point of showing it is that it
     can be copied, so it is never truncated — it wraps instead. */
  overflow-wrap: anywhere;
  user-select: all;
  flex: 1;
  min-width: 240px;
}

.code-box {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin: 10px 0;
  padding: 12px 16px;
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  background: var(--surface);
}

.code {
  font-family: var(--font-mono);
  font-size: 26px;
  letter-spacing: 0.08em;
  color: var(--text);
}

.code-text {
  display: flex;
  flex-direction: column;
  font-size: 13px;
}

.list {
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}

.list li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px solid var(--border);
  font-size: 14px;
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
}

.dot-on { background: var(--success); }

.since { flex: 1; }

.steps {
  margin: 0;
  padding-left: 1.3em;
  font-size: 14px;
}

.steps li {
  margin-bottom: 10px;
}

.inline {
  font-family: var(--font-mono);
  font-size: 0.87em;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.1em 0.35em;
}

.notice {
  margin: 10px 0 0;
  color: var(--text-muted);
  font-size: 13.5px;
}

.muted { color: var(--text-muted); }
.small { font-size: 12.5px; }
.error { color: var(--danger); }

.link {
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font-size: 13px;
}

.link:hover:not(:disabled) { text-decoration: underline; }
.link:disabled { opacity: 0.5; }
.danger { color: var(--danger); }

.btn {
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  font-size: 14px;
  white-space: nowrap;
  transition: background 0.15s, border-color 0.15s;
}

.btn:disabled { opacity: 0.45; }

.btn-ghost {
  background: none;
  border-color: var(--border);
  color: var(--text);
}

.btn-ghost:hover:not(:disabled) { background: var(--surface-strong); }

.btn-accent {
  background: var(--accent);
  color: var(--accent-fg);
}

.btn-accent:hover:not(:disabled) { background: var(--accent-hover); }

@media (max-width: 600px) {
  .body-inner { padding: 16px 14px; }
  .code { font-size: 22px; }
}
</style>
