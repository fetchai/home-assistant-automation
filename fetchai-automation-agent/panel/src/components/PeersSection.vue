<template>
  <!-- Other agents, below the people who pair from ASI1. Kept apart on
       purpose: a paired person can approve changes, an agent never can, and
       what an agent says about itself is only ever shown as its own claim. -->
  <section class="peers">
    <div class="heading-row">
      <h2 class="heading">Agents</h2>
      <span v-if="pending.length" class="pill">{{ pending.length }} waiting</span>
    </div>
    <p class="lede">
      Other Fetch.ai agents can ask to work with this home — any agent, just by writing to it in chat.
      Until you approve one here it gets nothing. <strong>Read</strong> lets it ask about the devices
      you watch; the agent answers it from those alone, and every exchange is kept here.
      <strong>Read + propose</strong> also lets it suggest a change to those devices — a new
      automation or turning one on or off — which waits here for your answer. An agent can never
      approve anything, and never reaches a device you do not watch.
    </p>

    <p v-if="error" class="notice error">{{ error }}</p>
    <p v-if="outcome" class="notice outcome">{{ outcome }}</p>

    <ul v-if="proposals.length" class="cards">
      <li v-for="p in proposals" :key="p.change_id" class="card waiting">
        <div class="who">
          <strong>{{ p.title || "A proposed change" }}</strong>
          <span class="muted small">from {{ p.name || "an agent" }} · {{ since(p.proposed_at) }}</span>
        </div>
        <p class="what">It would {{ p.summary }}.</p>
        <p v-if="p.reason" class="purpose">The agent says: “{{ p.reason }}”</p>
        <details v-if="p.new_config" class="yaml">
          <summary class="muted small">The automation, exactly as it would be written</summary>
          <pre>{{ p.new_config }}</pre>
        </details>
        <p class="muted small addr-line" :title="p.address">{{ p.address }}</p>
        <div class="actions">
          <button type="button" class="btn btn-accent" :disabled="busy"
                  @click="answer(p.change_id, true)">
            Approve
          </button>
          <button type="button" class="btn btn-ghost" :disabled="busy"
                  @click="answer(p.change_id, false)">
            Reject
          </button>
        </div>
      </li>
    </ul>

    <ul v-if="pending.length" class="cards">
      <li v-for="p in pending" :key="p.address" class="card waiting">
        <div class="who">
          <strong>{{ p.name || "Unnamed agent" }}</strong>
          <span class="muted small">says it is</span>
        </div>
        <code class="addr" :title="p.address">{{ p.address }}</code>
        <p v-if="p.purpose" class="purpose">
          <span v-if="p.via === 'chat'" class="muted small">wrote in chat: </span>“{{ p.purpose }}”
        </p>
        <p class="muted small">
          Asks for <strong>{{ p.requested_level || "read" }}</strong> access ·
          {{ since(p.requested_at) }} · expires unless answered within a week
        </p>
        <div class="actions">
          <button type="button" class="btn btn-accent" :disabled="busy"
                  @click="decide(p.address, 'approve', 'read')">
            Approve — read
          </button>
          <button type="button" class="btn btn-ghost" :disabled="busy"
                  @click="decide(p.address, 'approve', 'propose')">
            Approve — read + propose
          </button>
          <button type="button" class="btn btn-ghost" :disabled="busy"
                  @click="decide(p.address, 'deny')">
            Deny
          </button>
          <button type="button" class="link danger" :disabled="busy"
                  @click="decide(p.address, 'block')">
            Block
          </button>
        </div>
      </li>
    </ul>

    <ul v-if="approved.length" class="list">
      <li v-for="p in approved" :key="p.address" class="ask-row">
        <div class="ask-line">
        <span class="dot dot-on" aria-hidden="true"></span>
        <span class="name">{{ p.name || "Unnamed agent" }}</span>
        <code class="addr small" :title="p.address">{{ shorten(p.address) }}</code>
        <span class="muted small grow">{{ levelLabel(p.level) }} · {{ seen(p) }}</span>
        <button v-if="p.level === 'read'" type="button" class="link" :disabled="busy"
                @click="decide(p.address, 'approve', 'propose')">
          Allow proposals
        </button>
        <button v-else type="button" class="link" :disabled="busy"
                @click="decide(p.address, 'approve', 'read')">
          Read only
        </button>
        <button type="button" class="link" :disabled="busy" @click="removePeer(p.address)">
          Remove
        </button>
        <button type="button" class="link danger" :disabled="busy" @click="decide(p.address, 'block')">
          Block
        </button>
        </div>
        <details v-if="p.chats && p.chats.length" class="asked">
          <summary class="muted small">{{ p.chats.length }} chat message(s)</summary>
          <div v-for="c in [...p.chats].reverse()" :key="c.at" class="qa">
            <p class="muted small">{{ when(c.at) }}</p>
            <p><strong>It wrote:</strong> {{ c.message }}</p>
            <p><strong>This home replied:</strong> {{ c.reply }}</p>
          </div>
        </details>
      </li>
    </ul>

    <details v-if="declined.length" class="declined">
      <summary class="muted small">{{ declined.length }} denied or blocked</summary>
      <ul class="list">
        <li v-for="p in declined" :key="p.address">
          <span class="dot dot-off" aria-hidden="true"></span>
          <span class="name">{{ p.name || "Unnamed agent" }}</span>
          <code class="addr small" :title="p.address">{{ shorten(p.address) }}</code>
          <span class="muted small grow">{{ p.state }}</span>
          <button type="button" class="link" :disabled="busy"
                  @click="decide(p.address, 'approve', 'read')">
            Approve — read
          </button>
          <button type="button" class="link" :disabled="busy" @click="removePeer(p.address)">
            Forget
          </button>
        </li>
      </ul>
    </details>

    <p v-if="!pending.length && !approved.length && !declined.length" class="notice">
      No agent has asked yet. Another agent connects by writing to this agent's address above in
      chat (or, for a structured link, sending a <code class="inline">ConnectRequest</code> over the
      <code class="inline">CEELABS-HomePeer 0.2.0</code> protocol); its request then appears here.
    </p>

    <div class="heading-row ask-heading">
      <h2 class="heading">Agents this home can ask</h2>
    </div>
    <p class="lede">
      Agents the agent may put questions to in chat — a tariff or weather service, another home
      service. It can ask only the ones listed here, and uses your description to decide when. Each
      question leaves the house through the same filter as everything else (no passwords, network
      addresses or location), and every question and answer is kept below.
    </p>

    <form class="ask-form" @submit.prevent="submitAsk">
      <input v-model.trim="askAddress" class="field wide" placeholder="Agent address (agent1…)"
             :disabled="busy" />
      <input v-model.trim="askName" class="field" placeholder="Name, e.g. Tariff agent"
             :disabled="busy" />
      <input v-model.trim="askPurpose" class="field wide"
             placeholder="What to ask it for, e.g. tomorrow's electricity prices" :disabled="busy" />
      <label class="check">
        <input v-model="askConfirm" type="checkbox" :disabled="busy" />
        Ask me before each question — for an agent that can act (buy, pay, change things)
      </label>
      <button type="submit" class="btn btn-accent" :disabled="busy || !askAddress || !askName">
        Add
      </button>
    </form>

    <ul v-if="askable.length" class="list">
      <li v-for="p in askable" :key="p.address" class="ask-row">
        <div class="ask-line">
          <span class="name">{{ p.ask.name }}</span>
          <code class="addr small" :title="p.address">{{ shorten(p.address) }}</code>
          <span class="muted small grow">{{ p.ask.purpose }}</span>
          <span class="muted small">{{ confirms(p) ? "you confirm each question" : "asked freely" }}</span>
          <button type="button" class="link" :disabled="busy"
                  @click="setConfirm(p.address, !confirms(p))">
            {{ confirms(p) ? "Ask freely" : "Confirm each question" }}
          </button>
          <button type="button" class="link" :disabled="busy" @click="removeAsk(p.address)">
            Stop asking
          </button>
        </div>
        <details v-if="p.asked && p.asked.length" class="asked">
          <summary class="muted small">{{ p.asked.length }} question(s) sent</summary>
          <div v-for="q in [...p.asked].reverse()" :key="q.at" class="qa">
            <p class="muted small">{{ when(q.at) }}</p>
            <p><strong>Sent:</strong> {{ q.question }}</p>
            <p v-if="q.error" class="error"><strong>No answer:</strong> {{ q.error }}</p>
            <p v-else><strong>Answer:</strong> {{ q.answer }}</p>
          </div>
        </details>
      </li>
    </ul>
  </section>
</template>

<script setup>
import { ref } from "vue";

defineProps({
  peers: { type: Array, default: () => [] },
  pending: { type: Array, default: () => [] },
  proposals: { type: Array, default: () => [] },
  outcome: { type: String, default: "" },
  approved: { type: Array, default: () => [] },
  declined: { type: Array, default: () => [] },
  askable: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
  error: { type: String, default: "" },
});

const emit = defineEmits(["decide", "remove", "answer", "add-ask", "remove-ask", "set-confirm"]);

const askAddress = ref("");
const askName = ref("");
const askPurpose = ref("");
// On by default: the safe side for an agent nobody has checked yet.
const askConfirm = ref(true);

function submitAsk() {
  emit("add-ask", askAddress.value, askName.value, askPurpose.value, askConfirm.value);
  askAddress.value = "";
  askName.value = "";
  askPurpose.value = "";
  askConfirm.value = true;
}

// An entry from before 2.27.1 has no flag and is treated as confirming,
// exactly as core/peers.askable reads it.
const confirms = (p) => p.ask.confirm !== false;
const setConfirm = (address, confirm) => emit("set-confirm", address, confirm);

const removeAsk = (address) => emit("remove-ask", address);

const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");

const decide = (address, action, level) => emit("decide", address, action, level);
const removePeer = (address) => emit("remove", address);
const answer = (changeId, approved) => emit("answer", changeId, approved);

const levelLabel = (level) => (level === "propose" ? "read + propose" : "read");

const shorten = (a) => (a && a.length > 26 ? `${a.slice(0, 14)}…${a.slice(-8)}` : a);

function since(ts) {
  if (!ts) return "";
  const days = Math.floor((Date.now() / 1000 - ts) / 86400);
  if (days < 1) return "today";
  return days === 1 ? "yesterday" : `${days} days ago`;
}

const seen = (p) => (p.last_seen ? `last asked ${since(p.last_seen)}` : "has not asked anything yet");
</script>

<style scoped>
.peers {
  margin-top: 28px;
  padding-top: 22px;
  border-top: 1px solid var(--border);
}

.heading-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.heading {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.pill {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: var(--radius-lg);
  background: var(--warning-bg);
  color: var(--warning);
}

.lede {
  margin: 0 0 10px;
  font-size: 13.5px;
  color: var(--text-muted);
}

.cards {
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.card {
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  font-size: 14px;
}

.card.waiting { border-color: var(--warning); }

.who {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.card .addr {
  display: block;
  margin: 4px 0;
  overflow-wrap: anywhere;
}

.purpose {
  margin: 6px 0;
  font-style: italic;
}

.what { margin: 6px 0; }

.addr-line {
  font-family: var(--font-mono);
  overflow-wrap: anywhere;
}

.yaml pre {
  margin: 6px 0 0;
  padding: 8px 10px;
  font-family: var(--font-mono);
  font-size: 12.5px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow-x: auto;
}

.yaml summary { cursor: pointer; }

.outcome { color: var(--text); }

.ask-heading {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px solid var(--border);
}

.ask-form {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 8px 0;
}

.field {
  flex: 1 1 160px;
  min-width: 0;
  padding: 6px 10px;
  font: inherit;
  font-size: 14px;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.field.wide { flex-basis: 280px; }

.check {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-basis: 100%;
  font-size: 13.5px;
  color: var(--text-muted);
}

.list li.ask-row {
  flex-direction: column;
  align-items: stretch;
}

.ask-line {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.asked { margin-top: 6px; }
.asked summary { cursor: pointer; }

.qa {
  margin: 8px 0 0;
  padding: 8px 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: 13.5px;
}

.qa p { margin: 2px 0; overflow-wrap: anywhere; }

.card p { margin: 4px 0; }

.actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 8px;
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
  flex-wrap: wrap;
}

.name { font-weight: 500; }
.grow { flex: 1; }

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
}

.dot-on { background: var(--success); }
.dot-off { background: var(--text-muted); }

.declined { margin-top: 12px; }
.declined summary { cursor: pointer; }

.addr {
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--text);
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
</style>
