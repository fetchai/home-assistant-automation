<template>
  <div class="chat">
    <aside class="session-sidebar" aria-label="Chat sessions">
      <div class="session-sidebar-header">
        <span>Chats</span>
        <button type="button" class="new-session" title="New chat" @click="$emit('create-session')">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
          <span>New</span>
        </button>
      </div>

      <div class="session-list">
        <div
          v-for="session in sessions"
          :key="session.id"
          :class="['session-row', { active: session.id === activeSessionId }]"
          @click="$emit('select-session', session.id)"
        >
          <input
            v-if="editingSessionId === session.id"
            v-model="editingTitle"
            ref="renameInput"
            class="session-name-input"
            aria-label="Chat name"
            maxlength="80"
            @click.stop
            @keydown.enter.prevent="commitRename(session.id)"
            @keydown.esc.prevent="cancelRename"
            @blur="commitRename(session.id)"
          />
          <button
            v-else
            type="button"
            class="session-name"
            :title="session.title"
            @dblclick.stop="startRename(session)"
          >
            {{ session.title }}
          </button>
          <div class="session-actions">
            <button
              type="button"
              class="session-action"
              title="Rename chat"
              @click.stop="startRename(session)"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 16.5-.5 4 4-.5L19 8.5 15.5 5 4 16.5Zm10.8-10.8 3.5 3.5" /></svg>
            </button>
            <button
              type="button"
              class="session-action session-delete"
              title="Remove chat"
              :disabled="session.loading"
              @click.stop="removeSession(session)"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6m4-6v6M9 7l1-2h4l1 2m-9 0 1 14h10l1-14" /></svg>
            </button>
          </div>
        </div>
      </div>
    </aside>

    <div class="conversation">
      <div class="conversation-header">
        <div class="conversation-title" :title="activeSessionTitle">
          {{ activeSessionTitle }}
        </div>
        <div class="model-picker">
          <label for="model-select" class="model-label">Model</label>
          <select
            id="model-select"
            class="model-select"
            :value="model"
            :disabled="loading"
            @change="$emit('update-model', $event.target.value)"
          >
            <option value="asi1-mini">asi1-mini (Fast)</option>
            <option value="asi1">asi1 (Standard)</option>
            <option value="asi1-ultra">asi1-ultra (Deep)</option>
          </select>
        </div>
      </div>

      <div class="messages" ref="messagesContainer" aria-live="polite">
      <div class="messages-inner">
        <div
          v-for="(msg, index) in messages"
          :key="index"
          :class="['turn', msg.sender === 'user' ? 'turn-user' : 'turn-agent']"
        >
          <div v-if="msg.sender === 'user'" class="bubble">{{ msg.text }}</div>
          <div v-else class="markdown" v-html="parseMarkdown(msg.text)"></div>
        </div>

        <div v-if="loading" class="turn turn-agent">
          <div class="typing" aria-label="Agent is replying">
            <span></span><span></span><span></span>
          </div>
        </div>
      </div>
    </div>

      <div class="composer-area">
      <div class="composer-inner">
        <!-- While an approval is parked, the composer is replaced rather than
             sitting next to it: the only meaningful reply is accept or reject. -->
        <div v-if="pendingApproval" class="approval">
          <div class="approval-label">
            <span>Waiting for your decision</span>
            <button
              v-if="pendingApproval._diffAdded || pendingApproval._diffRemoved"
              type="button"
              class="diff-badge"
              title="Preview the YAML changes"
              @click="$emit('open-diff')"
            >
              <span class="diff-added">+{{ pendingApproval._diffAdded }}</span>
              <span class="diff-removed">&minus;{{ pendingApproval._diffRemoved }}</span>
            </button>
          </div>
          <div class="approval-actions">
            <button
              type="button"
              class="btn btn-ghost"
              :disabled="loading"
              @click="$emit('approve', false)"
            >
              Reject
            </button>
            <button
              type="button"
              class="btn btn-accent"
              :disabled="loading"
              @click="$emit('approve', true)"
            >
              Accept
            </button>
          </div>
        </div>

        <form v-else class="composer" @submit.prevent="onSubmit">
          <textarea
            v-model="newMessage"
            ref="messageInput"
            rows="1"
            placeholder="Tell the agent what to do…"
            :disabled="loading"
            @input="autoGrow"
            @keydown.enter.exact.prevent="onSubmit"
          ></textarea>
          <button
            type="submit"
            class="send"
            :disabled="loading || !newMessage.trim()"
            aria-label="Send message"
          >
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
              <path
                d="M12 19V5M12 5l-6 6M12 5l6 6"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </button>
        </form>
      </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick, onMounted } from "vue";
import { marked } from "marked";

const props = defineProps({
  messages: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  pendingApproval: { type: Object, default: null },
  sessions: { type: Array, default: () => [] },
  activeSessionId: { type: String, default: "" },
  activeSessionTitle: { type: String, default: "Chat" },
  model: { type: String, default: "asi1-mini" },
});

const emit = defineEmits(["send", "approve", "open-diff", "create-session", "select-session", "rename-session", "delete-session", "update-model"]);

const newMessage = ref("");
const messagesContainer = ref(null);
const messageInput = ref(null);
const editingSessionId = ref("");
const editingTitle = ref("");
const renameInput = ref(null);

function parseMarkdown(text) {
  return marked.parse(text || "");
}

function scrollToBottom() {
  const el = messagesContainer.value;
  if (el) el.scrollTop = el.scrollHeight;
}

// Grow the input with its content, up to the max-height set in CSS. An empty
// input falls back to its one-row height: scrollHeight counts a wrapped
// placeholder, which would otherwise leave the box two rows tall on a phone.
function autoGrow() {
  const el = messageInput.value;
  if (!el) return;
  el.style.height = "";
  if (el.value) el.style.height = `${el.scrollHeight}px`;
}

function onSubmit() {
  const text = newMessage.value.trim();
  if (!text || props.loading) return;
  emit("send", text);
  newMessage.value = "";
  nextTick(autoGrow);
}

function startRename(session) {
  editingSessionId.value = session.id;
  editingTitle.value = session.title;
  nextTick(() => {
    const input = Array.isArray(renameInput.value) ? renameInput.value[0] : renameInput.value;
    input?.focus();
  });
}

function commitRename(id) {
  const title = editingTitle.value.trim();
  if (title) emit("rename-session", id, title);
  cancelRename();
}

function cancelRename() {
  editingSessionId.value = "";
  editingTitle.value = "";
}

function removeSession(session) {
  if (window.confirm(`Remove “${session.title}”? This also discards its agent context.`)) {
    emit("delete-session", session.id);
  }
}

// Scroll to the latest message whenever the list grows, and while the typing
// indicator appears.
watch(() => props.messages.length, () => nextTick(scrollToBottom));
watch(() => props.loading, () => nextTick(scrollToBottom));
watch(() => props.activeSessionId, () => nextTick(scrollToBottom));

// Refocus the input once a request finishes and no approval is pending.
watch(
  () => props.loading,
  (isLoading, was) => {
    if (was && !isLoading && !props.pendingApproval) {
      nextTick(() => messageInput.value?.focus());
    }
  }
);

onMounted(() => nextTick(scrollToBottom));
</script>

<style scoped>
.chat {
  display: flex;
  flex: 1;
  min-height: 0;
}

.session-sidebar {
  display: flex;
  flex: 0 0 236px;
  flex-direction: column;
  min-width: 0;
  border-right: 1px solid var(--border);
  background: var(--surface);
}

.session-sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 12px 10px 16px;
  color: var(--text-muted);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}

.new-session {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 7px;
  border: 0;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--accent);
  font-size: 13px;
  font-weight: 600;
}

.new-session:hover { background: var(--surface-strong); }
.new-session svg, .session-action svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }

.session-list {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
  padding: 0 8px 10px;
}

.session-row {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  padding: 4px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.session-row:hover, .session-row.active { background: var(--surface-strong); }

.session-name, .session-name-input {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  border: 0;
  background: none;
  color: var(--text);
  font: inherit;
  line-height: 1.35;
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-name { padding: 5px 6px; }
.session-name-input { padding: 4px 5px; border: 1px solid var(--accent); border-radius: 4px; outline: 0; }

.session-actions { display: flex; opacity: 0; }
.session-row:hover .session-actions, .session-row.active .session-actions, .session-row:focus-within .session-actions { opacity: 1; }

.session-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: none;
  color: var(--text-muted);
}

.session-action:hover:not(:disabled) { background: var(--bg); color: var(--text); }
.session-delete:hover:not(:disabled) { color: var(--danger); }
.session-action:disabled { opacity: 0.35; }

.conversation {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.conversation-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  flex-shrink: 0;
}

.conversation-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.model-picker {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.model-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.model-select {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text);
  font-size: 13px;
  font-weight: 500;
  font-family: inherit;
  outline: none;
  cursor: pointer;
  transition: border-color 0.15s;
}

.model-select:hover:not(:disabled) {
  border-color: var(--accent);
}

.model-select:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.model-select:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.messages {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.messages-inner,
.composer-inner {
  max-width: 820px;
  margin: 0 auto;
  padding: 0 20px;
}

.messages-inner {
  padding-top: 24px;
  padding-bottom: 8px;
}

.turn {
  display: flex;
  margin-bottom: 18px;
}

.turn-user {
  justify-content: flex-end;
}

.turn-agent {
  justify-content: flex-start;
}

.bubble {
  max-width: 78%;
  padding: 10px 14px;
  background: var(--accent);
  color: var(--accent-fg);
  border-radius: var(--radius-lg);
  border-bottom-right-radius: 4px;
  overflow-wrap: anywhere;
}

/* Agent replies get no bubble — they carry lists, YAML and fenced code that a
   narrow tinted box makes harder to read, not easier. */
.turn-agent > .markdown {
  max-width: 100%;
  min-width: 0;
  overflow-wrap: anywhere;
}

.typing {
  display: flex;
  gap: 4px;
  padding: 8px 2px;
}

.typing span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-muted);
  animation: typing-blink 1.2s infinite ease-in-out;
}

.typing span:nth-child(2) {
  animation-delay: 0.18s;
}

.typing span:nth-child(3) {
  animation-delay: 0.36s;
}

@keyframes typing-blink {
  0%, 60%, 100% { opacity: 0.25; }
  30% { opacity: 1; }
}

@media (prefers-reduced-motion: reduce) {
  .typing span { animation: none; opacity: 0.5; }
}

.composer-area {
  border-top: 1px solid var(--border);
  background: var(--bg);
}

.composer-inner {
  padding-top: 14px;
  padding-bottom: 14px;
}

.composer {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 6px 6px 6px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg);
  transition: border-color 0.15s;
}

.composer:focus-within {
  border-color: var(--accent);
}

.composer textarea {
  flex: 1;
  border: 0;
  outline: none;
  resize: none;
  background: none;
  color: var(--text);
  font: inherit;
  line-height: 1.5;
  max-height: 9em;
  padding: 6px 0;
}

.composer textarea::placeholder {
  color: var(--text-muted);
}

.send {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: 0;
  border-radius: 50%;
  background: var(--accent);
  color: var(--accent-fg);
  transition: background 0.15s, opacity 0.15s;
}

.send:hover:not(:disabled) {
  background: var(--accent-hover);
}

.send:disabled {
  opacity: 0.35;
}

.approval {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 10px 10px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.approval-label {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text-muted);
  font-size: 14px;
}

.diff-badge {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  font-family: var(--font-mono);
  font-size: 12.5px;
  transition: background 0.15s;
}

.diff-badge:hover {
  background: var(--surface-strong);
}

.diff-added { color: var(--added); font-weight: 600; }
.diff-removed { color: var(--removed); font-weight: 600; }

.approval-actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
}

.btn {
  padding: 7px 16px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  font-size: 14px;
  transition: background 0.15s, border-color 0.15s;
}

.btn:disabled {
  opacity: 0.5;
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
  .session-sidebar { flex-basis: 172px; }
  .session-sidebar-header { padding-left: 10px; }
  .new-session span { display: none; }
  .session-list { padding-left: 5px; padding-right: 5px; }

  .conversation-header { padding-left: 12px; padding-right: 12px; }
  .model-label { display: none; }

  .messages-inner,
  .composer-inner {
    padding-left: 14px;
    padding-right: 14px;
  }

  .bubble {
    max-width: 88%;
  }
}
</style>
