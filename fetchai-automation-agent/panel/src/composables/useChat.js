import { computed, ref } from "vue";

import { diffLines, diffStats } from "@/composables/useDiff";

const agentBaseUrl = ".";
const SESSION_STORAGE_KEY = "fetchai-automation-agent.chat-sessions.v1";
const WELCOME_MESSAGE = "I'm your Fetch.ai Automation Agent. Tell me what you would like your home to do — I can switch things on and off for you, and propose, create or change your Home Assistant automations.";

function _attachDiffStats(approval) {
  if (!approval) return approval;
  const stats = diffStats(approval.old_yaml || "", approval.new_yaml || "");
  approval._diffAdded = stats.added;
  approval._diffRemoved = stats.removed;
  return approval;
}

function _newSession() {
  const id = "session_" + Math.random().toString(36).slice(2, 11);
  const now = Date.now();
  return {
    id,
    title: "New chat",
    messages: [{ sender: "bot", text: WELCOME_MESSAGE, session_id: id }],
    model: "asi1-mini",
    pendingApproval: null,
    loading: false,
    createdAt: now,
    updatedAt: now,
  };
}

function _sessionTitle(text) {
  const compact = text.replace(/\s+/g, " ").trim();
  return compact.length > 42 ? `${compact.slice(0, 42)}…` : compact;
}

function _parseApproval(text) {
  return text?.match(/\[APPROVAL_REQUIRED\](\{[\s\S]*\})\s*$/);
}

// Chat sessions are stored in browser storage so the sidebar and visible
// transcript survive panel reloads. Each session keeps its own agent session
// id, which keeps histories and parked approvals isolated on the backend.
export function useChat({ afterAction } = {}) {
  const sessions = ref([]);
  const activeSessionId = ref("");
  const showDiffModal = ref(false);

  const activeSession = computed(() =>
    sessions.value.find(session => session.id === activeSessionId.value) || null
  );
  const messages = computed(() => activeSession.value?.messages || []);
  const loading = computed(() => !!activeSession.value?.loading);
  const pendingApproval = computed(() => activeSession.value?.pendingApproval || null);
  const sessionId = computed(() => activeSession.value?.id || "");
  const activeModel = computed(() => activeSession.value?.model || "asi1-mini");
  const activeSessionTitle = computed(() => activeSession.value?.title || "New chat");

  const computedDiffLines = computed(() =>
    pendingApproval.value
      ? diffLines(pendingApproval.value.old_yaml, pendingApproval.value.new_yaml)
      : []);

  function persistSessions() {
    try {
      const stored = sessions.value.map(({ loading: _loading, ...session }) => session);
      localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({
        activeSessionId: activeSessionId.value,
        sessions: stored,
      }));
    } catch (error) {
      console.warn("Unable to save chat sessions", error);
    }
  }

  function initSession() {
    try {
      const stored = JSON.parse(localStorage.getItem(SESSION_STORAGE_KEY) || "null");
      if (Array.isArray(stored?.sessions) && stored.sessions.length) {
        sessions.value = stored.sessions
          .filter(session => session?.id && Array.isArray(session.messages))
          .map(session => ({
            ...session,
            model: session.model || "asi1-mini",
            loading: false,
            pendingApproval: session.pendingApproval || null,
          }));
        if (sessions.value.length) {
          activeSessionId.value = sessions.value.some(session => session.id === stored.activeSessionId)
            ? stored.activeSessionId
            : sessions.value[0].id;
        }
      }
    } catch (error) {
      console.warn("Unable to restore chat sessions", error);
    }

    if (!sessions.value.length) createSession();
  }

  function createSession() {
    const session = _newSession();
    sessions.value.unshift(session);
    activeSessionId.value = session.id;
    showDiffModal.value = false;
    persistSessions();
  }

  function selectSession(id) {
    if (!sessions.value.some(session => session.id === id)) return;
    activeSessionId.value = id;
    showDiffModal.value = false;
    persistSessions();
  }

  function setModel(model) {
    const session = activeSession.value;
    if (!session || !model) return;
    session.model = model;
    session.updatedAt = Date.now();
    persistSessions();
  }

  function renameSession(id, title) {
    const session = sessions.value.find(item => item.id === id);
    const name = title?.trim();
    if (!session || !name) return;
    session.title = name.slice(0, 80);
    session.updatedAt = Date.now();
    persistSessions();
  }

  async function deleteSession(id) {
    const index = sessions.value.findIndex(session => session.id === id);
    if (index < 0 || sessions.value[index].loading) return;
    const [deleted] = sessions.value.splice(index, 1);
    if (activeSessionId.value === id) {
      activeSessionId.value = sessions.value[0]?.id || "";
      showDiffModal.value = false;
    }
    if (!sessions.value.length) createSession();
    else persistSessions();

    // Removing a panel session must also discard any server-side context and
    // a parked approval that could otherwise remain answerable.
    try {
      await fetch(`${agentBaseUrl}/rest/session/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: deleted.id }),
      });
    } catch (error) {
      console.warn("Unable to discard server-side chat session", error);
    }
  }

  function addMessage(session, sender, text) {
    session.messages.push({ sender, text, session_id: session.id });
    session.updatedAt = Date.now();
  }

  async function sendMessage(text) {
    const userMsg = (text != null ? text : "").trim();
    const session = activeSession.value;
    if (!userMsg || !session || session.loading) return;
    addMessage(session, "user", userMsg);
    if (session.title === "New chat") session.title = _sessionTitle(userMsg);
    session.loading = true;
    persistSessions();
    try {
      const res = await fetch(`${agentBaseUrl}/rest/post`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: userMsg,
          session_id: session.id,
          model: session.model || "asi1-mini",
        }),
      });
      const data = await res.json();
      const approvalMatch = _parseApproval(data.text);
      if (approvalMatch) {
        const friendlyText = data.text.replace(/\s*\[APPROVAL_REQUIRED\]\{[\s\S]*\}/, "").trim();
        addMessage(session, "bot", friendlyText);
        try {
          session.pendingApproval = _attachDiffStats(JSON.parse(approvalMatch[1]));
        } catch (error) {
          console.error("Failed to parse approval JSON:", approvalMatch[1], error);
        }
      } else {
        addMessage(session, "bot", data.text || "[No response text]");
        session.pendingApproval = null;
      }
      if (afterAction) afterAction();
    } catch (error) {
      addMessage(session, "bot", "[Error: Automation Agent is not responding...]");
    } finally {
      session.loading = false;
      persistSessions();
    }
  }

  async function approveAction(approved) {
    const session = activeSession.value;
    if (!session || session.loading || !session.pendingApproval) return;
    session.loading = true;
    session.pendingApproval = null;
    persistSessions();
    try {
      const res = await fetch(`${agentBaseUrl}/rest/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: session.id, approved }),
      });
      const data = await res.json();
      const approvalMatch = _parseApproval(data.text);
      if (approvalMatch) {
        const friendlyText = data.text.replace(/\s*\[APPROVAL_REQUIRED\]\{[\s\S]*\}/, "").trim();
        if (friendlyText) addMessage(session, "bot", friendlyText);
        try {
          session.pendingApproval = _attachDiffStats(JSON.parse(approvalMatch[1]));
        } catch (error) {
          console.error("Failed to parse approval JSON:", approvalMatch[1], error);
        }
      } else if (data.text && !data.text.trim().startsWith("{")) {
        addMessage(session, "bot", data.text);
      }
      if (afterAction) afterAction();
    } catch (error) {
      addMessage(session, "bot", "[Error processing approval]");
    } finally {
      session.loading = false;
      persistSessions();
    }
  }

  return {
    sessions, activeSessionId, activeSessionTitle, activeModel, messages, loading, pendingApproval, showDiffModal, sessionId, computedDiffLines,
    initSession, createSession, selectSession, setModel, renameSession, deleteSession, sendMessage, approveAction,
  };
}
