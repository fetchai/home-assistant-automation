<template>
  <!-- A heatmap (free-hand §4.2): changes into one state, by local weekday
       (Monday first, as core/charts.py counts them) and hour. One hue,
       darker is more. A weekday the window never touched is hatched: its
       zeros are no data, not "never". -->
  <svg
    class="chart"
    :width="width"
    :height="height"
    :viewBox="`0 0 ${width} ${height}`"
    role="img"
    :aria-label="`Changes to ${series.state} by weekday and hour`"
  >
    <defs>
      <pattern :id="gapId" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <line class="gap-stroke" x1="2.5" y1="0" x2="2.5" y2="5" />
      </pattern>
    </defs>

    <g v-for="(row, d) in counts" :key="d">
      <text class="label" x="0" :y="y(d) + CELL_H - 5">{{ WEEKDAYS[d] }} ×{{ daysOn(d) }}</text>
      <rect
        v-for="(n, h) in row"
        :key="h"
        :class="cellClass(d, n)"
        :x="x(h) + 1"
        :y="y(d) + 1"
        :width="cellW - 2"
        :height="CELL_H - 2"
        rx="2"
        :fill="daysOn(d) ? null : `url(#${gapId})`"
        :style="daysOn(d) && n ? { fillOpacity: shade(n) } : null"
      ><title>{{ cellTitle(d, h, n) }}</title></rect>
    </g>

    <text
      v-for="h in hourTicks"
      :key="`h${h}`"
      class="label"
      :x="x(h) + cellW / 2"
      :y="height - 4"
      text-anchor="middle"
    >{{ String(h).padStart(2, "0") }}</text>
  </svg>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  series: { type: Object, required: true },
  width: { type: Number, default: 600 },
});

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const LABEL_W = 56;
const CELL_H = 20;
const AXIS_H = 18;

const gapId = `gap-${Math.random().toString(36).slice(2, 9)}`;

const counts = computed(() => props.series.counts || []);
const max = computed(() => Math.max(1, ...counts.value.flat()));
const cellW = computed(() => Math.max(6, (props.width - LABEL_W) / 24));
const height = computed(() => counts.value.length * CELL_H + AXIS_H);
const hourTicks = computed(() => {
  const step = cellW.value < 14 ? 6 : 3;
  return Array.from({ length: 24 / step }, (_, i) => i * step);
});

function x(hour) {
  return LABEL_W + hour * cellW.value;
}

function y(day) {
  return day * CELL_H;
}

function daysOn(d) {
  return (props.series.days_per_weekday || [])[d] || 0;
}

// Opacity steps of the one hue, never under a fifth so a single change is
// still visibly not a zero.
function shade(n) {
  return (0.2 + 0.8 * (n / max.value)).toFixed(2);
}

function cellClass(d, n) {
  if (!daysOn(d)) return "cell";
  return n ? "cell heat" : "cell zero";
}

function cellTitle(d, h, n) {
  const hours = `${String(h).padStart(2, "0")}:00–${String(h + 1).padStart(2, "0")}:00`;
  if (!daysOn(d)) return `${WEEKDAYS[d]} ${hours}: no ${WEEKDAYS[d]} in the window`;
  const days = daysOn(d);
  return `${WEEKDAYS[d]} ${hours}: ${n} change${n === 1 ? "" : "s"} to ${props.series.state}`
    + ` over ${days} ${WEEKDAYS[d]}${days === 1 ? "" : "s"}`;
}
</script>

<style scoped>
.chart { display: block; }
.label { fill: var(--text-muted); font-size: 11px; font-family: var(--font); }
.gap-stroke { stroke: var(--chart-gap); stroke-width: 1.5; }
.heat { fill: var(--chart-heat); }
.zero { fill: var(--surface-strong); }
</style>
