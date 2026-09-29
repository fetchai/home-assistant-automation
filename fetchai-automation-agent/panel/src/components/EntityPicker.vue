<template>
  <!-- What the agent is allowed to observe. Nothing here is watched until it is
       ticked and saved: the record is opt-in, and this screen is the opt-in. -->
  <div class="picker">
    <div class="toolbar">
      <div class="toolbar-inner">
        <label class="search">
          <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
            <circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2" />
            <path d="M16.5 16.5L21 21" fill="none" stroke="currentColor" stroke-width="2"
                  stroke-linecap="round" />
          </svg>
          <input
            :value="filter"
            type="search"
            placeholder="Search devices and entities…"
            aria-label="Search devices and entities"
            @input="$emit('update:filter', $event.target.value)"
          />
        </label>

        <span class="spacer"></span>

        <span class="count">{{ watchedCount }} watched</span>
        <button type="button" class="btn btn-ghost" :disabled="!dirty || saving" @click="$emit('discard')">
          Discard
        </button>
        <button type="button" class="btn btn-accent" :disabled="!dirty || saving" @click="$emit('save')">
          {{ saving ? "Saving…" : "Save" }}
        </button>
      </div>
    </div>

    <div class="body">
      <div class="body-inner">
        <p v-if="error" class="notice error">{{ error }}</p>

        <p v-if="loading && !areas.length" class="notice">Reading your Home Assistant registry…</p>

        <p v-else-if="!areas.length" class="notice">
          No observable entities found.
        </p>

        <section v-for="area in areas" :key="area.area_id || 'none'" class="area">
          <h2 class="area-name">{{ area.name }}</h2>

          <div v-for="device in area.devices" :key="device.device_id" class="device">
            <button
              type="button"
              class="row row-device"
              :aria-pressed="deviceState(device) === 'all'"
              @click="$emit('toggle-device', device)"
            >
              <span :class="['box', deviceState(device)]" aria-hidden="true">
                <svg v-if="deviceState(device) === 'all'" viewBox="0 0 24 24" width="12" height="12">
                  <path d="M5 13l4 4L19 7" fill="none" stroke="currentColor" stroke-width="3"
                        stroke-linecap="round" stroke-linejoin="round" />
                </svg>
                <span v-else-if="deviceState(device) === 'some'" class="dash"></span>
              </span>
              <span class="label">{{ device.name }}</span>
              <span class="muted small">{{ device.entities.length }}</span>
            </button>

            <button
              v-for="entity in device.entities"
              :key="entity.entity_id"
              type="button"
              class="row row-entity"
              :aria-pressed="isEntityWatched(entity, device.device_id)"
              @click="$emit('toggle-entity', entity, device)"
            >
              <span :class="['box', isEntityWatched(entity, device.device_id) ? 'all' : 'none']" aria-hidden="true">
                <svg v-if="isEntityWatched(entity, device.device_id)" viewBox="0 0 24 24" width="12" height="12">
                  <path d="M5 13l4 4L19 7" fill="none" stroke="currentColor" stroke-width="3"
                        stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </span>
              <span class="label">{{ entity.name }}</span>
              <code class="muted small id">{{ entity.entity_id }}</code>
              <span class="muted small state">{{ entity.state }}</span>
            </button>
          </div>

          <!-- Entities with no device of their own — helpers and template
               sensors, which are often exactly what someone wants watched. -->
          <button
            v-for="entity in area.entities"
            :key="entity.entity_id"
            type="button"
            class="row row-loose"
            :aria-pressed="isEntityWatched(entity, null)"
            @click="$emit('toggle-entity', entity, null)"
          >
            <span :class="['box', isEntityWatched(entity, null) ? 'all' : 'none']" aria-hidden="true">
              <svg v-if="isEntityWatched(entity, null)" viewBox="0 0 24 24" width="12" height="12">
                <path d="M5 13l4 4L19 7" fill="none" stroke="currentColor" stroke-width="3"
                      stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
            <span class="label">{{ entity.name }}</span>
            <code class="muted small id">{{ entity.entity_id }}</code>
            <span class="muted small state">{{ entity.state }}</span>
          </button>
        </section>

        <p v-if="hiddenCount" class="notice muted">
          {{ hiddenCount }} entities are not shown — automations, scripts, scenes and other things
          the agent produces rather than observes.
        </p>
      </div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  areas: { type: Array, default: () => [] },
  filter: { type: String, default: "" },
  watchedCount: { type: Number, default: 0 },
  hiddenCount: { type: Number, default: 0 },
  dirty: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  saving: { type: Boolean, default: false },
  error: { type: String, default: "" },
  isEntityWatched: { type: Function, required: true },
  deviceState: { type: Function, required: true },
});

defineEmits(["toggle-device", "toggle-entity", "save", "discard", "update:filter"]);
</script>

<style scoped>
.picker {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.toolbar {
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  flex: none;
}

.toolbar-inner,
.body-inner {
  max-width: 820px;
  margin: 0 auto;
  padding: 0 20px;
}

.toolbar-inner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 10px;
  padding-bottom: 10px;
}

.search {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex: 1;
  min-width: 0;
  max-width: 320px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
}

.search:focus-within {
  border-color: var(--accent);
}

.search input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: none;
  background: none;
  color: var(--text);
  font: inherit;
  font-size: 14px;
}

.spacer {
  flex: 1;
}

.count {
  color: var(--text-muted);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.body {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.body-inner {
  padding-top: 16px;
  padding-bottom: 24px;
}

.area + .area {
  margin-top: 22px;
}

.area-name {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.device {
  margin-bottom: 6px;
}

.row {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 5px 8px;
  border: 0;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--text);
  font-size: 14px;
  text-align: left;
}

.row:hover {
  background: var(--surface);
}

.row-device {
  font-weight: 600;
}

.row-entity {
  padding-left: 30px;
}

.box {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border: 1.5px solid var(--border);
  border-radius: 4px;
  color: var(--accent-fg);
}

.box.all {
  background: var(--accent);
  border-color: var(--accent);
}

.box.some {
  border-color: var(--accent);
}

.dash {
  width: 8px;
  height: 2px;
  border-radius: 1px;
  background: var(--accent);
}

.label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.muted {
  color: var(--text-muted);
}

.small {
  font-size: 12px;
}

.id {
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.state {
  margin-left: auto;
  padding-left: 8px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.notice {
  margin: 10px 0;
  color: var(--text-muted);
  font-size: 13.5px;
}

.error {
  color: var(--danger);
}

.btn {
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  font-size: 14px;
  white-space: nowrap;
  transition: background 0.15s, border-color 0.15s;
}

.btn:disabled {
  opacity: 0.45;
}

.btn-ghost {
  background: none;
  border-color: var(--border);
  color: var(--text);
}

.btn-ghost:hover:not(:disabled) {
  background: var(--surface-strong);
}

.btn-accent {
  background: var(--accent);
  color: var(--accent-fg);
}

.btn-accent:hover:not(:disabled) {
  background: var(--accent-hover);
}

@media (max-width: 600px) {
  .toolbar-inner,
  .body-inner {
    padding-left: 14px;
    padding-right: 14px;
  }

  /* The entity id is the first thing to go: the friendly name and the state
     are what a phone-sized screen has room to be useful with. */
  .id {
    display: none;
  }
}
</style>
