<template>
  <!-- What the agent has actually gathered. The status line above the table is
       load-bearing: an empty record because nothing has happened and an empty
       record because the listener is down look identical without it. -->
  <div class="record">
    <div class="header">
      <div class="header-inner">
        <span :class="['dot', status.connected ? 'dot-on' : 'dot-off']" aria-hidden="true"></span>
        <span class="status">{{ statusText }}</span>
        <span class="spacer"></span>
        <span class="muted small">{{ totalsText }}</span>
      </div>
      <div v-if="provenance" class="header-inner">
        <span class="muted small">{{ provenance }}</span>
      </div>
      <div v-if="status.last_error && !status.connected" class="header-inner">
        <span class="small error">{{ status.last_error }}</span>
      </div>
    </div>

    <div class="body">
      <div class="body-inner">
        <p v-if="error" class="notice error">{{ error }}</p>

        <p v-if="!rows.length" class="notice">
          Nothing is being recorded yet. Choose what the agent should watch in the
          <strong>Watching</strong> tab.
        </p>

        <table v-else class="table">
          <thead>
            <tr>
              <th>Entity</th>
              <th class="num">Events</th>
              <th>Last seen</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in rows"
              :key="row.entity_id"
              :class="{ withdrawn: !row.watched }"
              tabindex="0"
              role="button"
              @click="$emit('open', row.entity_id)"
              @keydown.enter="$emit('open', row.entity_id)"
            >
              <td>
                <span class="label">{{ row.name }}</span>
                <code class="muted small id">{{ row.entity_id }}</code>
                <span v-if="!row.watched" class="tag">no longer watched</span>
              </td>
              <td class="num">
                {{ row.events }}
                <span v-if="row.backfilled" class="muted small" :title="`${row.backfilled} recovered from Home Assistant's recorder`">
                  ({{ row.backfilled }}&nbsp;h)
                </span>
              </td>
              <td class="muted small">{{ row.events ? ago(row.last_seen) : "waiting…" }}</td>
              <td class="small">{{ row.last_state }}</td>
            </tr>
          </tbody>
        </table>

        <p v-if="rows.length" class="notice muted small">
          Counts marked <strong>h</strong> were recovered from Home Assistant's own recorder rather
          than watched live.
        </p>

        <!-- Observed co-occurrences: one watched entity tending to change
             shortly after another. Gated on rows.length so a wholly-empty
             record doesn't grow a second empty-state heading. -->
        <template v-if="rows.length">
          <h3 class="section">Patterns</h3>
          <p v-if="!relations.length" class="notice muted small">
            No recurring pattern has been noticed yet.
          </p>
          <ul v-else class="patterns">
            <li v-for="r in relations" :key="`${r.entity_a}>${r.entity_b}`">
              <span class="label">{{ r.entity_a_name }}</span>
              <span class="arrow">→</span>
              <span class="label">{{ r.entity_b_name }}</span>
              <span class="muted small">{{ r.count }}×</span>
            </li>
          </ul>
        </template>
      </div>
    </div>

    <!-- One entity's detail: the transitions themselves, and the shape they
         make over a day. -->
    <div v-if="detail" class="overlay" @click.self="$emit('close')">
      <div class="box" role="dialog" aria-modal="true" :aria-label="`Record for ${detail.name || detail.entity_id}`">
        <div class="box-header">
          <span>{{ detail.name || detail.entity_id }}</span>
          <button type="button" class="close" aria-label="Close" @click="$emit('close')">
            <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2"
                    stroke-linecap="round" />
            </svg>
          </button>
        </div>

        <div class="box-content">
          <p v-if="detail.loading" class="notice">Reading the record…</p>

          <template v-else>
            <div class="facts">
              <div class="fact">
                <span class="fact-value">{{ detail.profile?.events ?? 0 }}</span>
                <span class="muted small">transitions / {{ detail.profile?.days ?? 30 }}d</span>
              </div>
              <div class="fact">
                <span class="fact-value">{{ percent(detail.profile?.on_fraction) }}</span>
                <span class="muted small">of the time on</span>
              </div>
              <div class="fact">
                <span class="fact-value">{{ duration(detail.profile?.mean_on_duration) }}</span>
                <span class="muted small">average when on</span>
              </div>
            </div>

            <h3 class="section">By hour of day</h3>
            <div class="hours" role="img" :aria-label="hoursLabel">
              <div v-for="(n, hour) in detail.profile?.by_hour || []" :key="hour" class="hour">
                <div class="bar" :style="{ height: barHeight(n) }" :title="`${hour}:00 — ${n}`"></div>
                <span v-if="hour % 6 === 0" class="muted hour-label">{{ hour }}</span>
              </div>
            </div>

            <h3 class="section">Recent transitions</h3>
            <p v-if="!detail.recent?.length" class="notice">Nothing recorded for this entity yet.</p>
            <table v-else class="table">
              <tbody>
                <tr v-for="(row, i) in detail.recent" :key="i">
                  <td class="muted small">{{ when(row.ts) }}</td>
                  <td class="small">
                    <span class="muted">{{ row.prev_state || "—" }}</span>
                    <span class="arrow">→</span>
                    <strong>{{ row.state }}</strong>
                  </td>
                  <td class="muted small">{{ row.prev_duration != null ? `after ${duration(row.prev_duration)}` : "" }}</td>
                  <td class="muted small">{{ row.source === "history" ? "recovered" : "" }}</td>
                </tr>
              </tbody>
            </table>

            <template v-if="detail.relations?.length">
              <h3 class="section">Usually followed by</h3>
              <ul class="patterns">
                <li v-for="r in detail.relations" :key="r.entity_b">
                  <span class="label">{{ r.entity_b_name }}</span>
                  <span class="muted small">{{ r.count }}×</span>
                </li>
              </ul>
            </template>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  status: { type: Object, default: () => ({}) },
  rows: { type: Array, default: () => [] },
  totals: { type: Object, default: () => ({}) },
  relations: { type: Array, default: () => [] },
  detail: { type: Object, default: null },
  error: { type: String, default: "" },
});

defineEmits(["open", "close"]);

function when(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function day(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function ago(ts) {
  if (!ts) return "";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

function duration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

function percent(fraction) {
  // null means nothing to divide — "no data" and "never on" must not both
  // render as 0%.
  return fraction == null ? "—" : `${Math.round(fraction * 100)}%`;
}

const statusText = computed(() => {
  const s = props.status;
  if (s.connected) {
    const seen = s.seen ?? 0;
    const recorded = s.recorded ?? 0;
    return `Watching live — ${recorded} of ${seen} events recorded this session`;
  }
  if (s.running) return "Reconnecting to Home Assistant…";
  return "Not listening";
});

const provenance = computed(() => {
  const s = props.status;
  const parts = [];
  if (s.backfilled_to) parts.push(`recovered back to ${day(s.backfilled_to)}`);
  if (s.live_since) parts.push(`live since ${when(s.live_since)}`);
  return parts.join(" · ");
});

const totalsText = computed(() => {
  const t = props.totals;
  if (!t.events) return "";
  const size = t.bytes > 1048576
    ? `${(t.bytes / 1048576).toFixed(1)} MB`
    : `${Math.max(1, Math.round((t.bytes || 0) / 1024))} kB`;
  return `${t.events} events · ${t.entities} entities · ${size} · kept ${t.retention_days}d`;
});

const busiestHour = computed(() =>
  Math.max(1, ...(props.detail?.profile?.by_hour || [0]))
);

function barHeight(n) {
  // Always a sliver for a non-zero hour, so "rare" is visibly different from
  // "never" at a glance.
  return n ? `${Math.max(6, Math.round((n / busiestHour.value) * 100))}%` : "0";
}

const hoursLabel = computed(() => {
  const by = props.detail?.profile?.by_hour || [];
  const peak = by.indexOf(Math.max(...by));
  return by.length ? `Transitions by hour of day, busiest around ${peak}:00` : "";
});
</script>

<style scoped>
.record {
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

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
}

.dot-on { background: var(--success); }
.dot-off { background: var(--text-muted); }

.status {
  font-size: 13px;
}

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

tbody tr[role="button"]:hover {
  background: var(--surface);
  cursor: pointer;
}

.withdrawn {
  opacity: 0.6;
}

.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.label {
  margin-right: 8px;
}

.id {
  display: inline-block;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
  font-family: var(--font-mono);
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

.muted { color: var(--text-muted); }
.small { font-size: 12px; }
.error { color: var(--danger); }

.notice {
  margin: 10px 0;
  color: var(--text-muted);
  font-size: 13.5px;
}

.overlay {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(0, 0, 0, 0.45);
  z-index: 10;
}

.box {
  display: flex;
  flex-direction: column;
  width: min(680px, 100%);
  max-height: 84vh;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg);
  overflow: hidden;
}

.box-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  font-weight: 600;
}

.close {
  display: inline-flex;
  padding: 4px;
  border: 0;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--text-muted);
}

.close:hover {
  background: var(--surface-strong);
  color: var(--text);
}

.box-content {
  padding: 14px 16px 18px;
  overflow-y: auto;
}

.facts {
  display: flex;
  gap: 22px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.fact {
  display: flex;
  flex-direction: column;
}

.fact-value {
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}

.section {
  margin: 18px 0 8px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.hours {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 84px;
}

.hour {
  position: relative;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  align-items: center;
  flex: 1;
  height: 100%;
}

.bar {
  width: 100%;
  border-radius: 2px 2px 0 0;
  background: var(--accent);
  min-height: 0;
}

.hour-label {
  position: absolute;
  bottom: -16px;
  font-size: 10.5px;
}

.hours + .section {
  margin-top: 28px;
}

.arrow {
  margin: 0 6px;
  color: var(--text-muted);
}

.patterns {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.patterns li {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 13.5px;
}

.patterns .muted {
  margin-left: auto;
}

@media (max-width: 600px) {
  .header-inner,
  .body-inner {
    padding-left: 14px;
    padding-right: 14px;
  }

  .id {
    display: none;
  }
}
</style>
