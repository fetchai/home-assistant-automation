<template>
  <div v-if="show" class="overlay" @click.self="$emit('close')">
    <div class="box" role="dialog" aria-modal="true" aria-label="YAML diff preview">
      <div class="box-header">
        <span>YAML diff preview</span>
        <button type="button" class="close" aria-label="Close" @click="$emit('close')">
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path
              d="M6 6l12 12M18 6L6 18"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
            />
          </svg>
        </button>
      </div>
      <div class="box-content">
        <div class="diff" v-if="hasApproval">
          <div
            v-for="(line, i) in diffLines"
            :key="i"
            :class="['diff-line', line.type]"
          >
            <span class="diff-sign">{{ line.sign }}</span>
            <span class="diff-text">{{ line.text }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { watch, onBeforeUnmount } from "vue";

const props = defineProps({
  show: { type: Boolean, default: false },
  hasApproval: { type: Boolean, default: false },
  diffLines: { type: Array, default: () => [] },
});

const emit = defineEmits(["close"]);

function onKeydown(e) {
  if (e.key === "Escape") emit("close");
}

// Lock body scroll and listen for Escape only while the modal is open.
watch(
  () => props.show,
  (visible) => {
    document.body.style.overflow = visible ? "hidden" : "";
    if (visible) window.addEventListener("keydown", onKeydown);
    else window.removeEventListener("keydown", onKeydown);
  }
);

onBeforeUnmount(() => {
  document.body.style.overflow = "";
  window.removeEventListener("keydown", onKeydown);
});
</script>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  z-index: 100;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.box {
  width: 100%;
  max-width: 760px;
  max-height: 82vh;
  display: flex;
  flex-direction: column;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.28);
  overflow: hidden;
}

.box-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 14px;
  font-weight: 600;
}

.close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--text-muted);
  transition: background 0.15s, color 0.15s;
}

.close:hover {
  background: var(--surface-strong);
  color: var(--text);
}

.box-content {
  flex: 1;
  overflow: auto;
  padding: 12px 8px;
}

.diff {
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.65;
  white-space: pre;
  min-width: min-content;
}

.diff-line {
  display: flex;
  gap: 8px;
  padding: 0 8px;
  border-left: 2px solid transparent;
}

.diff-line.added {
  background: var(--added-bg);
  border-left-color: var(--added);
  color: var(--added);
}

.diff-line.removed {
  background: var(--removed-bg);
  border-left-color: var(--removed);
  color: var(--removed);
}

.diff-line.context {
  color: var(--text-muted);
}

.diff-sign {
  width: 10px;
  flex-shrink: 0;
  opacity: 0.7;
}

.diff-text {
  flex: 1;
}
</style>
