<template>
  <!-- A daily count (free-hand §4.2): changes into one state per local day.
       Under the axis, a hatched strip per day as deep as that day's share of
       minutes with no data — a zero above a full strip is a dead device, not
       a quiet day. -->
  <svg
    class="chart"
    :width="width"
    :height="height"
    :viewBox="`0 0 ${width} ${height}`"
    role="img"
    :aria-label="`Changes to ${series.state} per day`"
  >
    <defs>
      <pattern :id="gapId" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <line class="gap-stroke" x1="2.5" y1="0" x2="2.5" y2="5" />
      </pattern>
    </defs>

    <g v-for="t in yTicks" :key="`y${t}`">
      <line class="grid" :x1="LABEL_W" :x2="width" :y1="yOf(t)" :y2="yOf(t)" />
      <text class="label" :x="LABEL_W - 6" :y="yOf(t) + 4" text-anchor="end">{{ t }}</text>
    </g>

    <g v-for="(day, i) in days" :key="day.date">
      <rect
        v-if="day.count"
        class="bar"
        :x="bandX(i) + (band - barW) / 2"
        :y="yOf(day.count)"
        :width="barW"
        :height="PLOT_H - yOf(day.count) + TOP"
        rx="2"
      ><title>{{ dayTitle(day) }}</title></rect>
      <rect
        v-if="day.no_data_minutes"
        class="strip"
        :x="bandX(i) + (band - barW) / 2"
        :y="TOP + PLOT_H + 3"
        :width="barW"
        :height="Math.max(3, STRIP_H * Math.min(1, day.no_data_minutes / 1440))"
        :fill="`url(#${gapId})`"
      ><title>{{ dayTitle(day) }}</title></rect>
      <!-- A hit target the whole band tall, so a zero day has a tooltip too. -->
      <rect class="hit" :x="bandX(i)" :y="TOP" :width="band" :height="PLOT_H + STRIP_H + 3">
        <title>{{ dayTitle(day) }}</title>
      </rect>
      <text
        v-if="i % labelEvery === 0"
        class="label"
        :x="bandX(i) + band / 2"
        :y="height - 4"
        text-anchor="middle"
      >{{ day.date.slice(5) }}</text>
    </g>
  </svg>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  series: { type: Object, required: true },
  width: { type: Number, default: 600 },
});

const LABEL_W = 28;
const TOP = 6;
const PLOT_H = 110;
const STRIP_H = 14;
const AXIS_H = 18;

const gapId = `gap-${Math.random().toString(36).slice(2, 9)}`;

const days = computed(() => props.series.days || []);
const max = computed(() => Math.max(1, ...days.value.map(d => d.count || 0)));
const height = computed(() => TOP + PLOT_H + 3 + STRIP_H + AXIS_H);
const band = computed(() => (props.width - LABEL_W) / Math.max(1, days.value.length));
const barW = computed(() => Math.max(2, Math.min(22, band.value - 2)));
// Every nth date, so labels of about 40 px never touch.
const labelEvery = computed(() => Math.max(1, Math.ceil(42 / band.value)));
const yTicks = computed(() => {
  const top = max.value;
  return top <= 1 ? [0, 1] : [0, Math.round(top / 2), top];
});

function bandX(i) {
  return LABEL_W + i * band.value;
}

function yOf(n) {
  return TOP + PLOT_H - (n / max.value) * PLOT_H;
}

function dayTitle(day) {
  let text = `${day.date}: ${day.count} change${day.count === 1 ? "" : "s"} to ${props.series.state}`;
  if (day.no_data_minutes) text += `, ${day.no_data_minutes} minutes with no data`;
  return text;
}
</script>

<style scoped>
.chart { display: block; }
.grid { stroke: var(--chart-grid); stroke-width: 1; }
.label { fill: var(--text-muted); font-size: 11px; font-family: var(--font); }
.bar { fill: var(--chart-1); }
.gap-stroke { stroke: var(--chart-gap); stroke-width: 1.5; }
.strip { stroke: var(--chart-gap); stroke-width: 1; }
.hit { fill: transparent; }
</style>
