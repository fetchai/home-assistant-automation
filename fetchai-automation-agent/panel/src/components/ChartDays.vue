<template>
  <!-- A timeline or overlay (free-hand §4.2): one row per local day, one lane
       per entity, drawn from the series core/charts.py stored — elapsed local
       minutes, so a 23- or 25-hour day is a shorter or longer row, not a
       folded one. Nothing here computes a run; it lays out the stored ones. -->
  <svg
    class="chart"
    :width="width"
    :height="height"
    :viewBox="`0 0 ${width} ${height}`"
    role="img"
    :aria-label="ariaLabel"
  >
    <defs>
      <pattern :id="gapId" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <line class="gap-stroke" x1="2.5" y1="0" x2="2.5" y2="5" />
      </pattern>
    </defs>

    <!-- Hours since local midnight, on top: every row shares the axis. -->
    <g class="axis">
      <text
        v-for="tick in ticks"
        :key="`t${tick}`"
        class="tick"
        :x="x(tick)"
        :y="AXIS_H - 6"
        text-anchor="middle"
      >{{ hourLabel(tick) }}</text>
    </g>

    <g v-for="(day, d) in days" :key="day.date" :transform="`translate(0, ${rowTop(d)})`">
      <!-- Days are ruled apart like the panel's table rows. Outside the
           window nothing is drawn; inside it every minute is a bar, the
           resting line or hatched as no data, so blank never means "no
           data". -->
      <line v-if="d > 0" class="rule" :x1="0" :x2="width" y1="0" y2="0" />
      <text class="day" x="0" :y="laneY(0) + LANE_H - 1">{{ dayLabel(day) }}</text>
      <line
        v-for="tick in ticks"
        :key="`g${tick}`"
        class="grid"
        :x1="x(tick)"
        :x2="x(tick)"
        :y1="1"
        :y2="rowH - 1"
      />
      <g v-for="(entity, e) in entities" :key="entity">
        <template v-for="(seg, i) in day.entities[entity] || []" :key="i">
          <rect
            v-if="seg[2] === null"
            :x="x(seg[0])"
            :y="laneY(e)"
            :width="w(seg)"
            :height="LANE_H"
            :fill="`url(#${gapId})`"
          ><title>{{ names[entity] || entity }}: no data {{ clock(seg[0]) }}–{{ clock(seg[1]) }}</title></rect>
          <rect
            v-else-if="isRest(seg[2])"
            class="rest"
            :x="x(seg[0])"
            :y="laneY(e) + LANE_H / 2 - 1"
            :width="w(seg)"
            height="2"
          ><title>{{ names[entity] || entity }}: {{ seg[2] }} {{ clock(seg[0]) }}–{{ clock(seg[1]) }}</title></rect>
          <rect
            v-else
            :class="['on', `slot-${e + 1}`]"
            :x="x(seg[0])"
            :y="laneY(e)"
            :width="w(seg)"
            :height="LANE_H"
            rx="2"
          ><title>{{ names[entity] || entity }}: {{ seg[2] }} {{ clock(seg[0]) }}–{{ clock(seg[1]) }}</title></rect>
        </template>
      </g>
      <!-- F-8: what the rule would have done. Drawn in the text colour, the
           shape carrying the action, so no marker is read as an entity. -->
      <g v-for="(m, i) in markersFor(day.date)" :key="`m${i}`" class="marker">
        <path :d="markerPath(m)" :class="m.action" />
        <title>The rule would have run {{ m.action.replace("_", " ") }} on {{ names[m.entity] || m.entity }} at {{ clock(m.minute) }}</title>
      </g>
    </g>
  </svg>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  series: { type: Object, required: true },
  entities: { type: Array, required: true },
  markers: { type: Array, default: null },
  names: { type: Object, default: () => ({}) },
  width: { type: Number, default: 600 },
});

const LABEL_W = 68;
const AXIS_H = 20;
const LANE_H = 10;
const LANE_GAP = 3;
const ROW_PAD = 7;
const RIGHT_PAD = 6;

// States a lane draws as a thin line rather than a filled bar: the "nothing
// is happening" side of the common HA entities. Everything else is filled in
// the entity's colour, its state in the tooltip; unknown / unavailable arrive
// as null and are hatched.
const REST_STATES = new Set(["off", "closed", "idle", "standby", "not_home"]);

const gapId = `gap-${Math.random().toString(36).slice(2, 9)}`;

const days = computed(() => props.series.days || []);
const xMax = computed(() => Math.max(1440, ...days.value.map(d => d.minutes || 0)));
const plotW = computed(() => Math.max(60, props.width - LABEL_W - RIGHT_PAD));
const rowH = computed(() => props.entities.length * (LANE_H + LANE_GAP) - LANE_GAP + ROW_PAD * 2);
const height = computed(() => AXIS_H + days.value.length * rowH.value);
const ticks = computed(() => {
  const step = plotW.value < 260 ? 720 : 360;
  const out = [];
  for (let m = 0; m <= xMax.value; m += step) out.push(m);
  return out;
});

const byDay = computed(() => {
  const out = {};
  for (const m of props.markers || []) (out[m.day] ||= []).push(m);
  return out;
});

const ariaLabel = computed(() =>
  `${days.value.length} days of ${props.entities.join(", ")}`);

function x(minute) {
  return LABEL_W + (minute / xMax.value) * plotW.value;
}

function w(seg) {
  return Math.max(1, x(seg[1]) - x(seg[0]));
}

function rowTop(d) {
  return AXIS_H + d * rowH.value;
}

function laneY(e) {
  return ROW_PAD + e * (LANE_H + LANE_GAP);
}

function isRest(state) {
  return REST_STATES.has(String(state).toLowerCase());
}

function markersFor(date) {
  return (byDay.value[date] || []).filter(m => props.entities.includes(m.entity));
}

// ▲ for a turn on, ▽ for a turn off, over the lane of the entity it acts on,
// ringed in the surface colour so it reads over a filled bar.
function markerPath(m) {
  const cx = x(m.minute);
  const top = laneY(props.entities.indexOf(m.entity)) - 1;
  const bottom = top + LANE_H + 2;
  return m.action === "turn_on"
    ? `M${cx - 5},${bottom} L${cx + 5},${bottom} L${cx},${top} Z`
    : `M${cx - 5},${top} L${cx + 5},${top} L${cx},${bottom} Z`;
}

// Elapsed minutes since local midnight as a clock reading. On a day the
// clocks change the two differ by an hour after the change; the day's own
// length is the row's, so nothing is folded.
function clock(minutes) {
  const total = Math.floor(minutes);
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function hourLabel(minutes) {
  return String(Math.round(minutes / 60)).padStart(2, "0");
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function dayLabel(day) {
  const [y, m, d] = day.date.split("-").map(Number);
  const weekday = WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
  return `${weekday} ${day.date.slice(5)}`;
}
</script>

<style scoped>
.chart { display: block; overflow: visible; }
.grid { stroke: var(--chart-grid); stroke-width: 1; }
.rule { stroke: var(--border); stroke-width: 1; }
.tick,
.day { fill: var(--text-muted); font-size: 11px; font-family: var(--font); }
.gap-stroke { stroke: var(--chart-gap); stroke-width: 1.5; }
.rest { fill: var(--chart-rest); }
.slot-1 { fill: var(--chart-1); }
.slot-2 { fill: var(--chart-2); }
.slot-3 { fill: var(--chart-3); }
.marker .turn_on { fill: var(--text); stroke: var(--bg); stroke-width: 1.5; }
.marker .turn_off { fill: var(--bg); stroke: var(--text); stroke-width: 1.5; }
</style>
