<template>
  <!-- An explorer idea (free-hand F-6, F-10, F-11): the agent's finding, the
       chart drawn from the record that is its evidence, the rule in the
       agent's words, the automation, what was sent, and Yes / No / No,
       because…. Every sentence arrives from core/artefacts.py. -->
  <article class="card">
    <header class="card-head">
      <h3 class="title">{{ row.label }}</h3>
      <!-- F-10: no checked / not tested — the chart is the label. -->
      <span class="tag" :title="LEVEL_TITLE">{{ row.evidence_label }}</span>
    </header>

    <p class="why">{{ row.why }}</p>

    <SuggestionChart :row="row" />

    <template v-if="row.reasoning">
      <h4 class="sub">{{ row.reasoning_heading }}</h4>
      <p class="reasoning">{{ row.reasoning }}</p>
    </template>

    <!-- The YAML the gate recorded when it raised the change — the text the
         approval is recorded against — behind the same toggle a detector
         card's automation is. -->
    <p class="toggle">
      <button type="button" class="link" :aria-expanded="showYaml" @click="showYaml = !showYaml">
        {{ showYaml ? "Hide" : "Show" }} the automation
        <span class="muted small">({{ addedCount }} lines)</span>
      </button>
    </p>
    <div v-if="showYaml" class="diff">
      <div v-for="(line, i) in yamlLines" :key="i" :class="['diff-line', line.type]">
        <span class="diff-sign">{{ line.sign }}</span>
        <span class="diff-text">{{ line.text }}</span>
      </div>
    </div>

    <!-- F-6: what left the house for this idea, as a fact on the card. -->
    <details class="sent">
      <summary>
        {{ row.sent_heading }}
        <span class="muted small">({{ sent.length }} {{ sent.length === 1 ? "read" : "reads" }})</span>
      </summary>
      <p class="muted small">{{ row.sent_note }}</p>
      <details v-for="(entry, i) in sent" :key="i" class="sent-entry">
        <summary>
          {{ entry.tool }}<span v-if="entry.rows !== null && entry.rows !== undefined" class="muted small">
            · {{ entry.rows }} {{ entry.rows === 1 ? "row" : "rows" }}</span><span
            v-if="entry.truncated" class="muted small"> · excerpt cut</span>
        </summary>
        <pre class="code">{{ queryText(entry.query) }}</pre>
        <pre class="code">{{ entry.answer_excerpt }}</pre>
      </details>
    </details>

    <p v-if="row.feedback_line" class="feedback">{{ row.feedback_line }}</p>

    <footer class="actions">
      <!-- Yes and No end on /rest/approve with the change_id, as Apply and
           Dismiss do; the answer is recorded first and approves nothing. -->
      <button type="button" class="apply" :disabled="busy" @click="answer('yes')">
        {{ answering === row.change_id ? "applying…" : "Yes" }}
      </button>
      <button type="button" class="plain" :disabled="busy" @click="answer('no')">No</button>
      <button
        type="button"
        class="plain"
        :disabled="busy"
        :aria-expanded="asking"
        @click="asking = !asking"
      >No, because…</button>
      <span class="muted small note">
        <template v-if="row.answerable">
          No is not permanent — the agent will leave it alone for 30 days. Your answer, and
          any reason, is read by the agent before it next looks for ideas.
        </template>
        <template v-else>
          This one was already answered elsewhere ({{ row.decision || "unknown" }}).
        </template>
      </span>
    </footer>

    <form v-if="asking" class="because" @submit.prevent="answer('no', reason)">
      <!-- Kept on the row (F-11) and read by the analyst's next run (F-WP6):
           the last 20 answers the change journal backs go into its first
           message, so the label says the agent reads it — not that it obeys. -->
      <label :for="`why-${row.suggestion_id}`" class="small muted">Why not? The agent reads your reason before it next looks for ideas.</label>
      <textarea
        :id="`why-${row.suggestion_id}`"
        v-model="reason"
        rows="2"
        :maxlength="REASON_MAX"
        placeholder="e.g. the light is left on for the dog at night"
      ></textarea>
      <div>
        <button type="submit" class="plain" :disabled="busy || !reason.trim()">Send “No”</button>
      </div>
    </form>
  </article>
</template>

<script setup>
import { computed, ref } from "vue";

import SuggestionChart from "@/components/SuggestionChart.vue";

const props = defineProps({
  row: { type: Object, required: true },
  answering: { type: Number, default: 0 },
});

const emit = defineEmits(["answer"]);

// suggestions.FEEDBACK_REASON_MAX_CHARS — the store cuts at the same length.
const REASON_MAX = 500;

const LEVEL_TITLE = "The agent's own idea. Nothing about it was recounted or replayed; the "
  + "chart is drawn from your recorded history, so judge it by the chart.";

const showYaml = ref(false);
const asking = ref(false);
const reason = ref("");

const busy = computed(() => !props.row.answerable || !!props.answering);
const sent = computed(() => props.row.sent || []);
const yamlLines = computed(() => props.row.diff || []);
const addedCount = computed(() => yamlLines.value.filter(l => l.type === "added").length);

// What the model asked a record tool: the SQL as written, anything else as
// the arguments it sent.
function queryText(query) {
  if (query && typeof query.sql === "string") return query.sql;
  return JSON.stringify(query || {}, null, 1);
}

// Confirmed in the browser, like Apply and Dismiss: the click IS the answer.
function answer(word, because = "") {
  const text = because.trim();
  const what = word === "yes"
    ? `Apply this suggestion?\n\n${props.row.label}\n\n${props.row.readback_yaml}`
    : `Say no to this suggestion?\n\n${props.row.label}\n\n`
      + (text ? `Because: ${text}\n\n` : "")
      + "The agent will not raise it again for 30 days.";
  if (!window.confirm(what)) return;
  emit("answer", props.row.suggestion_id, props.row.change_id, word, text);
  asking.value = false;
}
</script>

<style scoped>
.card {
  padding: 14px 16px;
  margin-bottom: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  min-width: 0;
}

.card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 10px;
}

.title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.tag {
  padding: 1px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-muted);
  font-size: 11px;
}

.why {
  margin: 8px 0 0;
  font-size: 13.5px;
}

.sub {
  margin: 14px 0 4px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--text-muted);
}

/* The model's own words, kept as it wrote them. */
.reasoning {
  margin: 0;
  font-size: 13px;
  color: var(--text-muted);
  white-space: pre-wrap;
}

.toggle { margin: 10px 0 0; }

.link {
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font-size: 12.5px;
}

.diff,
.code {
  margin: 8px 0 0;
  padding: 8px;
  max-height: 320px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  font-family: var(--font-mono);
  font-size: 12px;
}

.code {
  max-height: 200px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.diff-line {
  display: flex;
  gap: 6px;
  padding: 0 4px;
  white-space: pre-wrap;
}

.diff-line.added { background: var(--added-bg); color: var(--added); }
.diff-line.removed { background: var(--removed-bg); color: var(--removed); }
.diff-line.context { color: var(--text-muted); }
.diff-sign { flex: none; }
.diff-text { flex: 1; min-width: 0; overflow-wrap: anywhere; }

.sent {
  margin: 12px 0 0;
  font-size: 12.5px;
}

.sent > summary {
  color: var(--accent);
  cursor: pointer;
}

.sent-entry {
  margin: 6px 0 0 10px;
}

.sent-entry > summary {
  cursor: pointer;
  overflow-wrap: anywhere;
}

.feedback {
  margin: 12px 0 0;
  padding-left: 10px;
  border-left: 2px solid var(--border);
  font-size: 13px;
  color: var(--text-muted);
}

.actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

.apply {
  padding: 5px 14px;
  border: 0;
  border-radius: var(--radius-sm);
  background: var(--accent);
  color: var(--accent-fg);
  font-size: 13px;
}

.apply:hover:not(:disabled) { background: var(--accent-hover); }

.plain {
  padding: 5px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: none;
  color: var(--text);
  font-size: 13px;
}

.apply:disabled,
.plain:disabled { opacity: 0.5; }

.note { flex: 1 1 200px; }

.because {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 10px;
}

.because textarea {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text);
  font: inherit;
  font-size: 13px;
  resize: vertical;
}

.muted { color: var(--text-muted); }
.small { font-size: 12px; }
</style>
