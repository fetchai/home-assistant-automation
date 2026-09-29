<template>
  <!-- The audit surface. Everything the agent proposed and everything it has
       to say, read from local storage — this is the screen that has to work
       when the connection being audited does not. -->
  <div class="activity">
    <div class="header">
      <div class="header-inner">
        <span class="status">{{ headline }}</span>
        <span class="spacer"></span>
        <span class="muted small">{{ totalsText }}</span>
      </div>
    </div>

    <div class="body">
      <div class="body-inner">
        <p v-if="error" class="notice error">{{ error }}</p>

        <!-- Its own notice, above the table, and it stays until dismissed. A
             rollback that did not work is the one thing on this screen the
             user must not have to go looking for. -->
        <p v-if="rollbackError" class="notice failure">
          <span>{{ rollbackError }}</span>
          <button type="button" class="dismiss" aria-label="Dismiss" @click="$emit('dismiss-rollback-error')">×</button>
        </p>

        <h3 class="section">Changes</h3>
        <p v-if="!changes.length" class="notice">
          The agent has not proposed any changes yet. Everything it writes into Home Assistant
          is recorded here first, whether you approve it or not.
        </p>

        <table v-else class="table">
          <thead>
            <tr>
              <th>Change</th>
              <th>Asked from</th>
              <th>When</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in changes" :key="row.id">
              <td>
                <span class="label">{{ capitalise(row.summary) }}</span>
                <code v-if="row.target" class="muted small id">{{ row.target }}</code>
              </td>
              <td class="small muted">{{ sourceLabel(row.source) }}</td>
              <td class="muted small">{{ ago(row.decided_at || row.proposed_at) }}</td>
              <td class="small">
                <span :class="['pill', pillClass(row)]">{{ outcome(row) }}</span>
                <!-- It used to say "undoable" and do nothing, because the
                     inverse was recorded and nothing executed it. It executes
                     one now, so the label is a button. There is no approval
                     step behind it: the agent treats a rollback as
                     pre-authorised by the approval that let the change through,
                     and core/rollback.py carries the argument. The confirm
                     below is a misclick guard, not a second approval. -->
                <button
                  v-if="undoable(row)"
                  type="button"
                  class="undo"
                  :disabled="!!rollingBack"
                  :title="`Run the recorded inverse of change #${row.id}`"
                  @click="askRollBack(row)"
                >
                  {{ rollingBack === row.id ? "undoing…" : "undo" }}
                </button>
                <span v-else-if="row.undone_by" class="tag" :title="`Reversed by change #${row.undone_by}`">
                  rolled back
                </span>
              </td>
            </tr>
          </tbody>
        </table>

        <h3 class="section">Messages</h3>
        <p v-if="!messages.length" class="notice">
          Nothing has been queued for you.
        </p>

        <ul v-else class="messages">
          <li v-for="message in messages" :key="message.id" :class="{ urgent: message.urgency === 'urgent' }">
            <div class="message-head">
              <span class="label">{{ message.title }}</span>
              <span class="muted small">{{ ago(message.created_at) }}</span>
            </div>
            <p class="message-body">{{ message.body }}</p>
            <p class="muted small delivery">{{ deliveryText(message) }}</p>
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  changes: { type: Array, default: () => [] },
  messages: { type: Array, default: () => [] },
  totals: { type: Object, default: () => ({}) },
  error: { type: String, default: "" },
  rollingBack: { type: Number, default: 0 },
  rollbackError: { type: String, default: "" },
});

const emit = defineEmits(["roll-back", "dismiss-rollback-error"]);

// Confirmed in the browser, not by the agent. A rollback needs no approval —
// see the comment on the button — but it is still a write into the house, and
// a table row is an easy thing to click by accident.
function askRollBack(row) {
  const what = capitalise(row.summary);
  if (window.confirm(`Undo this change?\n\n${what}\n\nThe agent will run the inverse it recorded when you approved it.`)) {
    emit("roll-back", row.id);
  }
}

function ago(ts) {
  if (!ts) return "";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

function capitalise(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

// The one place "asked from" is rendered, so the WP2 acceptance criterion —
// an agent-raised change must visibly differ from a panel click — has exactly
// one ternary to hold rather than two that can drift apart. Mirrors
// core/activity.py's SOURCE_LABELS; kept here rather than fetched, because a
// three-entry map does not need a round trip to stay in sync with three
// possible values of one column.
const SOURCE_LABELS = { asi1: "ASI1 chat", panel: "the panel", agent: "the agent itself" };

function sourceLabel(source) {
  return SOURCE_LABELS[source] || SOURCE_LABELS.panel;
}

function outcome(row) {
  if (row.decision === "proposed") return "awaiting you";
  if (row.decision === "rejected") return "rejected";
  if (row.decision === "expired") return "expired";
  // FIX 3: the gate refused at apply time — drift or a stale config, not the
  // user saying no. Distinct from "failed", which is Home Assistant refusing
  // a write that was actually attempted.
  if (row.decision === "refused") return "refused before writing";
  // A rollback row is written before it runs, so it can be read with no
  // outcome yet — from a second browser tab, or after a restart caught it
  // mid-flight and before startup settled it.
  if (!row.status) return "in flight";
  return row.status === "applied" ? "applied" : "failed";
}

function pillClass(row) {
  if (row.decision === "proposed") return "pill-open";
  if (row.decision === "refused") return "pill-bad";
  if (row.status === "applied") return "pill-done";
  if (row.status === "failed") return "pill-bad";
  return "pill-muted";
}

// Only a change that actually happened, and left an inverse behind, can be
// undone. A rejected one never touched the house, and one already reversed has
// nothing left to reverse. Rollback rows qualify like any other: undoing an
// undo is a redo, and the agent records the inverse for it exactly the same way.
function undoable(row) {
  return row.status === "applied"
    && row.reversibility === "reversible"
    && !!row.inverse
    && !row.undone_by;
}

function deliveryText(message) {
  const parts = [];
  if (message.urgency === "urgent") {
    parts.push(message.pushed_at ? "notified" : "waiting to notify");
  }
  parts.push(message.collected_at ? "collected in chat" : "not yet collected in chat");
  return parts.join(" · ");
}

const headline = computed(() => {
  const awaiting = props.totals.awaiting || 0;
  if (awaiting) return `${awaiting} change${awaiting > 1 ? "s" : ""} waiting for your answer`;
  const applied = props.totals.applied || 0;
  return applied ? `${applied} change${applied > 1 ? "s" : ""} applied` : "Nothing written yet";
});

const totalsText = computed(() => {
  const t = props.totals;
  if (!t.changes && !t.messages) return "";
  const parts = [`${t.changes || 0} proposed`];
  // Shown whenever it is not zero: design §12 calls the rollback rate "the one
  // that matters", and a number nobody can see is not a measurement.
  if (t.rolled_back) parts.push(`${t.rolled_back} rolled back`);
  parts.push(`${t.messages || 0} messages`);
  return parts.join(" · ");
});
</script>

<style scoped>
.activity {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.header {
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  flex: none;
  padding: 8px 0;
}

.header-inner,
.body-inner {
  max-width: 820px;
  margin: 0 auto;
  padding: 0 20px;
}

.header-inner {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status { font-size: 13px; }
.spacer { flex: 1; }

.body {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.body-inner {
  padding-top: 16px;
  padding-bottom: 24px;
}

.section {
  margin: 18px 0 8px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.section:first-of-type { margin-top: 0; }

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

.table th {
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
  text-align: left;
  letter-spacing: 0.03em;
  text-transform: uppercase;
}

.table td {
  padding: 7px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: baseline;
}

.label { margin-right: 8px; }

.id { font-family: var(--font-mono); }

.pill {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  white-space: nowrap;
}

.pill-open { background: var(--surface-strong); color: var(--text); }
.pill-done { background: var(--added-bg); color: var(--added); }
.pill-bad { background: var(--removed-bg); color: var(--removed); }
.pill-muted { background: var(--surface); color: var(--text-muted); }

.failure {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--danger);
  border-radius: var(--radius);
  color: var(--text);
}

.dismiss {
  margin-left: auto;
  padding: 0 4px;
  border: 0;
  background: none;
  color: var(--text-muted);
  font-size: 16px;
  line-height: 1;
}

.undo {
  margin-left: 8px;
  padding: 1px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: none;
  color: var(--accent);
  font-size: 11px;
  white-space: nowrap;
}

.undo:hover:not(:disabled) {
  border-color: var(--accent);
}

.undo:disabled {
  color: var(--text-muted);
  opacity: 0.6;
}

.tag {
  margin-left: 8px;
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
}

.messages {
  margin: 0;
  padding: 0;
  list-style: none;
}

.messages li {
  padding: 10px 12px;
  margin-bottom: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.messages li.urgent {
  border-color: var(--danger);
}

.message-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.message-body {
  margin: 4px 0 0;
  font-size: 13.5px;
}

.delivery { margin: 6px 0 0; }

.muted { color: var(--text-muted); }
.small { font-size: 12px; }
.error { color: var(--danger); }

.notice {
  margin: 10px 0;
  color: var(--text-muted);
  font-size: 13.5px;
}
</style>
