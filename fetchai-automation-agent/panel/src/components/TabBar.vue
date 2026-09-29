<template>
  <!-- Three surfaces, one screen. No router: the chat is kept alive behind the
       other tabs rather than remounted, so switching away and back does not
       throw a conversation away. -->
  <nav class="tabs" role="tablist" aria-label="Panel sections">
    <button
      v-for="tab in tabs"
      :key="tab.id"
      type="button"
      role="tab"
      :class="['tab', { active: modelValue === tab.id }]"
      :aria-selected="modelValue === tab.id"
      @click="$emit('update:modelValue', tab.id)"
    >
      {{ tab.label }}
      <span v-if="tab.badge" class="badge">{{ tab.badge }}</span>
      <!-- Something needs attention on a tab you are not looking at. The
           pairing strip used to say this from every screen; with it gone, this
           dot is what stops "nothing is paired" going unnoticed. -->
      <span v-else-if="tab.alert" class="alert" :title="tab.alertTitle" aria-hidden="true"></span>
    </button>
  </nav>
</template>

<script setup>
defineProps({
  modelValue: { type: String, default: "chat" },
  tabs: { type: Array, default: () => [] },
});

defineEmits(["update:modelValue"]);
</script>

<style scoped>
.tabs {
  display: flex;
  gap: 2px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  flex: none;
}

.tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 14px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: none;
  color: var(--text-muted);
  font-size: 14px;
}

.tab:hover:not(.active) {
  color: var(--text);
}

.tab.active {
  color: var(--text);
  border-bottom-color: var(--accent);
}

.badge {
  padding: 0 6px;
  border-radius: 999px;
  background: var(--surface-strong);
  color: var(--text-muted);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.tab.active .badge {
  background: var(--accent);
  color: var(--accent-fg);
}

.alert {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--danger);
}

@media (max-width: 600px) {
  .tabs {
    padding: 0 8px;
  }

  .tab {
    padding: 9px 10px;
  }
}
</style>
