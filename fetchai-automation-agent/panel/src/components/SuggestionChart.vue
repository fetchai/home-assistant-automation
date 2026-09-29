<template>
  <!-- An explorer idea's evidence (free-hand F-7, F-8): the chart core/charts.py
       drew from the record when the idea was stored, drawn here as inline SVG.
       Every sentence around it — the caption, a chart the record could not
       draw, a clamped window, the markers or why there are none — arrives
       written from core/artefacts.py; this lays them out. -->
  <figure ref="root" class="figure">
    <p v-if="row.chart_error" class="missing">{{ row.chart_error }}</p>

    <template v-else-if="kind">
      <figcaption v-if="row.chart_caption" class="caption">{{ row.chart_caption }}</figcaption>

      <div class="plot">
        <ChartDays
          v-if="kind === 'timeline' || kind === 'overlay'"
          :series="chart.series"
          :entities="entities"
          :markers="chart.markers"
          :names="names"
          :width="width"
        />
        <ChartHeatmap v-else-if="kind === 'heatmap'" :series="chart.series" :width="width" />
        <ChartDailyCount v-else :series="chart.series" :width="width" />
      </div>

      <!-- Identity is never colour alone: each lane's colour is named here
           by the household's name for its entity (the id on hover), and the
           gap shape and each marker shape the chart holds are spelled out. -->
      <ul class="legend">
        <template v-if="kind === 'timeline' || kind === 'overlay'">
          <li v-for="(entity, e) in entities" :key="entity">
            <span :class="['swatch', `slot-${e + 1}`]"></span><span :title="entity">{{ names[entity] || entity }}</span>
          </li>
          <li><span class="swatch rest"></span>off / closed / idle</li>
          <li v-if="actions.has('turn_on')"><span class="glyph">▲</span>rule would turn on</li>
          <li v-if="actions.has('turn_off')"><span class="glyph">▽</span>rule would turn off</li>
        </template>
        <template v-else>
          <li><span class="swatch heat"></span>changes to “{{ chart.series.state }}”</li>
        </template>
        <li><span class="swatch gap"></span>no data</li>
      </ul>

      <p v-if="row.chart_clamped" class="note">{{ row.chart_clamped }}</p>
      <p v-if="row.chart_markers" class="note">{{ row.chart_markers }}</p>
      <p v-if="row.chart_truncated" class="note">{{ row.chart_truncated }}</p>
    </template>
  </figure>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import ChartDailyCount from "@/components/ChartDailyCount.vue";
import ChartDays from "@/components/ChartDays.vue";
import ChartHeatmap from "@/components/ChartHeatmap.vue";

const props = defineProps({
  row: { type: Object, required: true },
});

const TYPES = new Set(["timeline", "overlay", "heatmap", "daily_count"]);

const chart = computed(() => props.row.chart || {});
// A type this build does not know draws nothing rather than a guess.
const kind = computed(() => {
  const type = chart.value.spec?.type;
  return TYPES.has(type) && chart.value.series ? type : "";
});
const entities = computed(() => (chart.value.spec?.entities || []).slice(0, 3));
const names = computed(() => props.row.chart_names || {});
// The marker shapes this chart actually draws; a legend entry for one it
// does not would describe a mark nobody can find.
const actions = computed(() => new Set((chart.value.markers || []).map((m) => m.action)));

// Drawn in real pixels at the frame's inner width, so text stays 11 px on a
// phone instead of shrinking with a scaled viewBox. PLOT_INSET is .plot's
// padding and border, both sides.
const PLOT_INSET = 2 * (12 + 1);
const root = ref(null);
const width = ref(600);
let observer = null;

onMounted(() => {
  const measure = () => {
    if (root.value) width.value = Math.max(240, Math.floor(root.value.clientWidth) - PLOT_INSET);
  };
  measure();
  if (typeof ResizeObserver !== "undefined") {
    observer = new ResizeObserver(measure);
    observer.observe(root.value);
  }
});

onBeforeUnmount(() => {
  if (observer) observer.disconnect();
});
</script>

<style scoped>
.figure {
  margin: 10px 0 0;
  min-width: 0;
}

.caption {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}

/* Framed like every other inset on a card (the automation's YAML, what was
   sent): the page background, a hairline border, the small radius. */
.plot {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  overflow-x: auto;
}

.missing {
  margin: 0;
  padding: 8px 12px;
  border-left: 3px solid var(--warning);
  border-radius: var(--radius-sm);
  background: var(--warning-bg);
  font-size: 13px;
}

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
  color: var(--text-muted);
}

.legend li {
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-wrap: anywhere;
}

.swatch {
  flex: none;
  width: 14px;
  height: 10px;
  border-radius: 2px;
}

.slot-1 { background: var(--chart-1); }
.slot-2 { background: var(--chart-2); }
.slot-3 { background: var(--chart-3); }
.heat { background: var(--chart-heat); }
.rest { height: 2px; background: var(--chart-rest); }

.gap {
  background: repeating-linear-gradient(
    45deg, var(--chart-gap) 0 1.5px, transparent 1.5px 5px);
}

.glyph {
  width: 14px;
  color: var(--text);
  font-size: 11px;
  text-align: center;
}

.note {
  margin: 6px 0 0;
  font-size: 12.5px;
  color: var(--text-muted);
}
</style>
