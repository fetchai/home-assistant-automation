<template>
  <!-- Design §9's artefacts 1 to 4. The digest is the header line, the detail
       is each card, the readback is the sentence above the buttons, and the
       verification report is the section that comes first. Every sentence here
       arrives already written from core/artefacts.py — the panel lays them
       out, it does not decide what they may claim. -->
  <div class="suggestions">
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

        <!-- §9: "every approval is echoed back with what was actually done,
             because a one-word reply carries no record of what it agreed to".
             This is the agent's own readback, not a summary of it, and it
             stays until dismissed. -->
        <p v-if="outcome" class="notice echo">
          <span>{{ outcome }}</span>
          <button type="button" class="dismiss" aria-label="Dismiss" @click="$emit('dismiss-outcome')">×</button>
        </p>

        <!-- No promise here that every suggestion was replayed: from the
             analyst on (design §15.3) some are checked against the history
             and some are not tested at all, and each card says which. -->
        <p v-if="!open.length && !verified.length" class="notice">
          The agent has not raised any suggestions yet. It looks at the entities you watch once a
          day. Every suggestion it raises says what stands behind it — replayed against what
          actually happened, checked against your history, a chart of your recorded history to
          judge for yourself, or not tested — so you know how far to trust it.
        </p>

        <!-- §9 artefact 4, first because it is about a change already running
             in the home. Its own section and never merged with the proposals
             below: those are changes nobody has said yes to, these are ones
             that have been running for a week, and one list of the two is how
             a click comes to mean the opposite of what was intended. -->
        <template v-if="verified.length">
          <h2 class="section">A week later</h2>
          <article v-for="row in verified" :key="`v${row.suggestion_id}`" class="card report">
            <header class="card-head">
              <h3 class="title">{{ row.label }}</h3>
              <span v-if="row.basis" class="tag" :title="basisTitle(row)">{{ basisLabel(row) }}</span>
            </header>

            <!-- The claim, and the only form it may take: a range, or the
                 sentence saying the effect is too small to separate from
                 noise. core/verify.py decided which; this prints what it
                 decided. -->
            <p class="estimate">{{ row.result }}</p>

            <!-- An analyst change has no measurement (proposal §5, B-5):
                 no windows, no evidence, and no heading promising either. -->
            <template v-if="row.source !== 'analyst'">
              <!-- §7.2: compare like periods, and state the comparison
                   window. -->
              <p class="why">{{ row.windows }}</p>

              <h4 class="sub">What was measured</h4>
              <ul class="lines">
                <li v-for="(line, i) in row.evidence" :key="`ve${i}`">{{ line }}</li>
              </ul>
            </template>

            <!-- Whose estimate the measurement is being compared against. The
                 replay may report the detector's figures, and a report naming
                 the wrong side is the failure §7.1 spent a module preventing. -->
            <p v-if="row.estimate" class="lines estimate-note">{{ row.estimate }}</p>

            <footer class="actions">
              <!-- Keep is the plain button and Roll back is the accented one,
                   deliberately. §7.2 offers the rollback whatever the number
                   says, and a layout that made keeping the obvious choice
                   would be the agent grading its own homework. -->
              <button
                type="button"
                class="apply"
                :disabled="!row.undoable || !!deciding"
                @click="decide(row, false)"
              >
                {{ deciding === row.suggestion_id ? "rolling back…" : "Roll back" }}
              </button>
              <button
                type="button"
                class="dismiss-btn"
                :disabled="!!deciding"
                @click="decide(row, true)"
              >
                Keep it
              </button>
              <span class="muted small note">{{ row.choice }}</span>
            </footer>
          </article>
        </template>

        <h2 v-if="verified.length && open.length" class="section">Waiting for a decision</h2>

        <template v-for="row in open" :key="row.suggestion_id">
          <!-- An explorer idea (free-hand F-WP5) has its own card: a chart drawn
               from the record where the other cards carry measurements, and
               Yes / No / No, because… — which still end on /rest/approve. -->
          <IdeaCard
            v-if="row.evidence_level === 'shown'"
            :row="row"
            :answering="answering"
            @answer="(...args) => $emit('answer-idea', ...args)"
          />
          <article v-else class="card">
            <header class="card-head">
              <h3 class="title">{{ row.label }}</h3>
              <!-- Which side the number came from. core/replay.py reports the
                   detector's figures whenever its own came out higher, and a
                   suggestion that hid that would be quoting a measurement it
                   discarded. -->
              <span v-if="row.basis" class="tag" :title="basisTitle(row)">{{ basisLabel(row) }}</span>
              <!-- Design §15.3: every card carries exactly one evidence label,
                   detector rows included (§5A.1) — the auditor's word, in
                   core/artefacts.py's words. -->
              <span
                :class="['tag', 'level', row.evidence_level]"
                :title="levelTitle(row)"
              >{{ row.evidence_label }}</span>
            </header>

            <!-- §15.3's fixed sentence, above everything the model wrote, so no
                 reading of the card reaches the reasoning without passing it. -->
            <p v-if="row.warning" class="warning">{{ row.warning }}</p>

            <p class="why">{{ row.why }}</p>

            <template v-if="row.source === 'analyst'">
              <!-- Only claims the core recounted and found matching, each with
                   the recounted figure (B-3). None at all is an ordinary card. -->
              <template v-if="row.evidence.length">
                <h4 class="sub">Checked against your history</h4>
                <ul class="lines">
                  <li v-for="(line, i) in row.evidence" :key="`ae${i}`">{{ line }}</li>
                </ul>
              </template>

              <template v-if="row.reasoning">
                <h4 class="sub">{{ row.reasoning_heading }}</h4>
                <p class="reasoning">{{ row.reasoning }}</p>
              </template>

              <!-- B-4: the readback is the config itself, shown whole rather
                   than behind a toggle, because there is no plain-language
                   sentence standing in for it. -->
              <h4 class="sub">What would be written</h4>
              <p class="readback">{{ row.label }}</p>
              <div class="diff">
                <div v-for="(line, i) in row.readbackDiff" :key="i" :class="['diff-line', line.type]">
                  <span class="diff-sign">{{ line.sign }}</span>
                  <span class="diff-text">{{ line.text }}</span>
                </div>
              </div>
            </template>

            <template v-else>
              <p class="estimate">{{ row.estimate }}</p>

              <!-- Two headings, not one list. What the detector measured and what
                   the replay graded are two different observations, and merging
                   them is exactly the misreading core/artefacts.py's _measured_on
                   exists to prevent — most of all when the estimate above came
                   from the detector because the replay's own figure was discarded.
                   The chat rendering keeps them apart as separate paragraphs; this
                   has to make the same claim, differing only in layout. -->
              <h4 class="sub">What was measured</h4>
              <ul class="lines">
                <li v-for="(line, i) in row.evidence" :key="`e${i}`">{{ line }}</li>
              </ul>

              <template v-if="row.replay.length">
                <h4 class="sub">What the replay found</h4>
                <ul class="lines">
                  <li v-for="(line, i) in row.replay" :key="`r${i}`">{{ line }}</li>
                </ul>
              </template>

              <h4 class="sub">What would be written</h4>
              <p class="readback">{{ row.readback }}</p>

              <p class="config-toggle">
                <button type="button" class="link" @click="toggle(row.suggestion_id)">
                  {{ expanded === row.suggestion_id ? "Hide" : "Show" }} the automation
                  <span class="muted small">({{ addedCount(row) }} lines)</span>
                </button>
              </p>

              <div v-if="expanded === row.suggestion_id" class="diff">
                <div v-for="(line, i) in row.diff" :key="i" :class="['diff-line', line.type]">
                  <span class="diff-sign">{{ line.sign }}</span>
                  <span class="diff-text">{{ line.text }}</span>
                </div>
              </div>
            </template>

            <footer class="actions">
              <!-- Both post the change_id to /rest/approve, which is where an
                   ASI1 "yes" lands too. There is no suggestion-shaped approval
                   endpoint, on purpose. -->
              <button
                type="button"
                class="apply"
                :disabled="!row.answerable || !!answering"
                @click="ask(row, true)"
              >
                {{ answering === row.change_id ? "applying…" : "Apply" }}
              </button>
              <button
                type="button"
                class="dismiss-btn"
                :disabled="!row.answerable || !!answering"
                @click="ask(row, false)"
              >
                Dismiss
              </button>
              <span class="muted small note">
                <template v-if="row.answerable">
                  Dismissing is not permanent — the agent will leave it alone for 30 days.
                </template>
                <template v-else>
                  This one was already answered elsewhere ({{ row.decision || "unknown" }}).
                </template>
              </span>
            </footer>
          </article>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";

import IdeaCard from "@/components/IdeaCard.vue";

const props = defineProps({
  open: { type: Array, default: () => [] },
  verified: { type: Array, default: () => [] },
  totals: { type: Object, default: () => ({}) },
  error: { type: String, default: "" },
  answering: { type: Number, default: 0 },
  deciding: { type: Number, default: 0 },
  outcome: { type: String, default: "" },
});

const emit = defineEmits(["answer", "answer-idea", "decide", "dismiss-outcome"]);

// One card open at a time: the diff is the long part of a card and two of them
// expanded turns a page of three suggestions into a page of YAML.
const expanded = ref(0);

function toggle(id) {
  expanded.value = expanded.value === id ? 0 : id;
}

function addedCount(row) {
  return (row.diff || []).filter(line => line.type === "added").length;
}

const BASIS_LABELS = {
  replay: "replayed",
  detector: "measured",
};

const BASIS_TITLES = {
  replay: "The saving is what replaying this rule over the record produced.",
  detector: "The replay's own figure came out higher than the measurement, so the "
    + "smaller one — the measurement's — is the one quoted.",
};

function basisLabel(row) {
  return BASIS_LABELS[row.basis] || row.basis || "";
}

function basisTitle(row) {
  return BASIS_TITLES[row.basis] || "";
}

// What each evidence label means, for the tag's tooltip — design §15.3's
// table, in the household's words. The label itself arrives from Python.
const LEVEL_TITLES = {
  replayed: "Simulated over the recorded history.",
  checked: "Every fact it cites was recounted from your history and matched. "
    + "The change itself was not simulated.",
  untested: "Nothing about it could be tested against your history.",
};

function levelTitle(row) {
  return LEVEL_TITLES[row.evidence_level] || "";
}

// Confirmed in the browser. Not a second approval — the click IS the approval,
// and the agent gates on it — but Apply writes an automation into the house and
// a button is an easy thing to hit by accident.
//
// An analyst suggestion's readback is fenced for the chat's markdown; the
// dialog is plain text, so it is given the alias and the YAML without fences.
function ask(row, approved) {
  const written = row.source === "analyst"
    ? `${row.label}\n\n${row.readback_yaml}`
    : row.readback;
  const what = approved
    ? `Apply this suggestion?\n\n${written}`
    : `Dismiss this suggestion?\n\n${row.label}\n\nThe agent will not raise it again for 30 days.`;
  if (window.confirm(what)) emit("answer", row.change_id, approved);
}

// Confirmed in the browser, like Apply above and for the same reason: a
// rollback is pre-authorised and the click IS the answer, but removing an
// automation from a running home is an easy thing to hit by accident.
function decide(row, keep) {
  if (keep) {
    emit("decide", row.suggestion_id, true);
    return;
  }
  const what = `Roll back this change?\n\n${row.label}\n\n`
    + "The agent will remove the automation it added.";
  if (window.confirm(what)) emit("decide", row.suggestion_id, false);
}

const headline = computed(() => {
  const waiting = props.open.length;
  const measured = props.verified.length;
  const parts = [];
  if (measured) parts.push(`${measured} measured result${measured > 1 ? "s" : ""}`);
  if (waiting) parts.push(`${waiting} suggestion${waiting > 1 ? "s" : ""}`);
  if (!parts.length) return "Nothing waiting for a decision";
  return `${parts.join(" and ")} waiting for your decision`;
});

const totalsText = computed(() => {
  const t = props.totals;
  if (!t.suggestions) return "";
  const parts = [`${t.suggestions} found`];
  // §12 measures the product on acceptance, dismissal and rollback rates, and
  // all three are ratios over exactly these counts. The rollback rate is the
  // one that matters, so it is shown even when it is the only one that is not
  // zero.
  if (t.applied) parts.push(`${t.applied} applied`);
  if (t.dismissed) parts.push(`${t.dismissed} dismissed`);
  if (t.kept) parts.push(`${t.kept} kept`);
  if (t.rolled_back) parts.push(`${t.rolled_back} rolled back`);
  return parts.join(" · ");
});
</script>

<style scoped>
.suggestions {
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
  margin: 4px 0 10px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.card {
  padding: 14px 16px;
  margin-bottom: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.card-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
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
  white-space: nowrap;
}

.why {
  margin: 8px 0 0;
  font-size: 13.5px;
}

.warning {
  margin: 10px 0 0;
  padding: 8px 12px;
  border-left: 3px solid var(--warning);
  border-radius: var(--radius-sm);
  background: var(--warning-bg);
  color: var(--text);
  font-size: 13.5px;
}

.level.untested {
  border-color: var(--warning);
  color: var(--warning);
}

/* The model's own words, kept as it wrote them — line breaks included — and
   set in the muted colour of everything on the card that is not a finding. */
.reasoning {
  margin: 0;
  font-size: 13px;
  color: var(--text-muted);
  white-space: pre-wrap;
}

.estimate {
  margin: 10px 0 0;
  font-size: 13.5px;
  font-weight: 600;
}

.sub {
  margin: 14px 0 4px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.lines {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  color: var(--text-muted);
}

.lines li { margin: 2px 0; }

.readback {
  margin: 0;
  font-size: 13.5px;
}

/* A measured result is about a change already in the home, so it is set apart
   from the proposals rather than looking like one more of them. */
.report { border-left: 3px solid var(--accent); }

.estimate-note {
  margin: 10px 0 0;
  padding-left: 0;
  list-style: none;
}

.config-toggle { margin: 10px 0 0; }

.link {
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font-size: 12.5px;
}

.diff {
  margin-top: 8px;
  padding: 8px;
  max-height: 320px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  font-family: var(--font-mono);
  font-size: 12px;
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
.diff-text { flex: 1; }

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

.dismiss-btn {
  padding: 5px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: none;
  color: var(--text);
  font-size: 13px;
}

.apply:disabled,
.dismiss-btn:disabled { opacity: 0.5; }

.note { flex: 1; min-width: 200px; }

.echo {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--accent);
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

.muted { color: var(--text-muted); }
.small { font-size: 12px; }
.error { color: var(--danger); }

.notice {
  margin: 10px 0;
  color: var(--text-muted);
  font-size: 13.5px;
}
</style>
