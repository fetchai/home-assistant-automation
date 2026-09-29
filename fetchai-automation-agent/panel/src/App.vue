<template>
  <!-- Six surfaces over one agent: the conversation, what the agent is
       allowed to observe, what it has observed so far, what it has raised by
       itself and is waiting on, what it has done and still has to say, and who
       may reach it from ASI1 — and which other agents may work with it. Pairing
       was a strip above all of them; it earns a tab of its own because the part
       people actually need is the instructions, and a strip is no place for
       those. -->
  <TabBar v-model="activeTab" :tabs="tabs" />

  <!-- v-show, not v-if: switching tabs must not throw away a conversation, its
       scroll position, or a parked approval waiting for an answer. -->
  <ChatPanel
    v-show="activeTab === 'chat'"
    :messages="messages"
    :loading="loading"
    :pendingApproval="pendingApproval"
    :sessions="sessions"
    :activeSessionId="activeSessionId"
    :activeSessionTitle="activeSessionTitle"
    :model="activeModel"
    @send="sendMessage"
    @approve="approveAction"
    @open-diff="openDiff"
    @create-session="createSession"
    @select-session="selectSession"
    @rename-session="renameSession"
    @delete-session="deleteSession"
    @update-model="setModel"
  />

  <EntityPicker
    v-if="activeTab === 'watching'"
    :areas="visibleAreas"
    :filter="filter"
    :watchedCount="watchedCount"
    :hiddenCount="hiddenCount"
    :dirty="dirty"
    :loading="observationLoading"
    :saving="saving"
    :error="observationError"
    :isEntityWatched="isEntityWatched"
    :deviceState="deviceState"
    @update:filter="filter = $event"
    @toggle-device="toggleDevice"
    @toggle-entity="toggleEntity"
    @save="saveSelection"
    @discard="discard"
  />

  <PairingPanel
    v-if="activeTab === 'asi1'"
    :agentAddress="agentAddress"
    :paired="paired"
    :code="code"
    :codeLive="codeLive"
    :countdown="countdown"
    :busy="pairingBusy"
    :error="pairingError"
    @issue-code="issueCode"
    @unpair="unpair"
  >
    <PeersSection
      :peers="peers"
      :pending="pendingPeers"
      :proposals="peerProposals"
      :outcome="peerOutcome"
      :approved="approvedPeers"
      :declined="declinedPeers"
      :askable="askablePeers"
      :busy="peersBusy"
      :error="peersError"
      @decide="decidePeer"
      @remove="removePeer"
      @answer="answerProposal"
      @add-ask="addAskable"
      @remove-ask="removeAskable"
      @set-confirm="setConfirm"
    />
  </PairingPanel>

  <RecordView
    v-if="activeTab === 'record'"
    :status="status"
    :rows="rows"
    :totals="totals"
    :relations="relations"
    :detail="detail"
    :error="observationError"
    @open="openEntity"
    @close="closeEntity"
  />

  <SuggestionsView
    v-if="activeTab === 'suggestions'"
    :open="openSuggestions"
    :verified="verifiedSuggestions"
    :totals="suggestionTotals"
    :error="suggestionError"
    :answering="answering"
    :deciding="deciding"
    :outcome="suggestionOutcome"
    @answer="answerSuggestion"
    @answer-idea="answerIdea"
    @decide="decideVerification"
    @dismiss-outcome="dismissOutcome"
  />

  <ActivityView
    v-if="activeTab === 'activity'"
    :changes="changes"
    :messages="outboxMessages"
    :totals="activityTotals"
    :error="activityError"
    :rollingBack="rollingBack"
    :rollbackError="rollbackError"
    @roll-back="rollBackChange"
    @dismiss-rollback-error="dismissRollbackError"
  />

  <DiffModal
    :show="showDiffModal"
    :hasApproval="!!pendingApproval"
    :diffLines="computedDiffLines"
    @close="closeDiff"
  />
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";

import ActivityView from "@/components/ActivityView.vue";
import ChatPanel from "@/components/ChatPanel.vue";
import EntityPicker from "@/components/EntityPicker.vue";
import PairingPanel from "@/components/PairingPanel.vue";
import PeersSection from "@/components/PeersSection.vue";
import RecordView from "@/components/RecordView.vue";
import SuggestionsView from "@/components/SuggestionsView.vue";
import TabBar from "@/components/TabBar.vue";
import DiffModal from "@/components/DiffModal.vue";

import { useActivity } from "@/composables/useActivity";
import { useChat } from "@/composables/useChat";
import { useObservation } from "@/composables/useObservation";
import { usePairing } from "@/composables/usePairing";
import { usePeers } from "@/composables/usePeers";
import { useSuggestions } from "@/composables/useSuggestions";

const {
  sessions, activeSessionId, activeSessionTitle, activeModel, messages, loading, pendingApproval, showDiffModal, computedDiffLines,
  initSession, createSession, selectSession, setModel, renameSession, deleteSession, sendMessage, approveAction,
} = useChat();

const {
  visibleAreas, filter, hiddenCount, watchedCount, dirty,
  status, rows, totals, relations, detail,
  loading: observationLoading, saving, error: observationError,
  isEntityWatched, deviceState, toggleDevice, toggleEntity, discard,
  loadRegistry, saveSelection, loadRecord, openEntity, closeEntity,
} = useObservation();

// Held here rather than inside the pairing tab: a code is redeemed in ASI1,
// minutes after it was asked for, and the polling that notices must survive
// switching to another tab in the meantime.
const {
  agentAddress, paired, code, busy: pairingBusy, error: pairingError, codeLive, countdown,
  loadStatus, issueCode, unpair,
} = usePairing();

// Other agents. Loaded on mount like the badges beside it: an agent waiting
// for an answer is something to see before choosing a tab.
const {
  peers, pending: pendingPeers, approved: approvedPeers, declined: declinedPeers,
  proposals: peerProposals, outcome: peerOutcome, askable: askablePeers,
  addAskable, removeAskable, setConfirm,
  busy: peersBusy, error: peersError, loadPeers, decide: decidePeer, removePeer,
  answerProposal: _answerProposal,
} = usePeers();

// Answering an agent's proposal writes a change into the journal the Activity
// tab shows, so that is refreshed too — as with a suggestion's answer.
async function answerProposal(changeId, approved) {
  await _answerProposal(changeId, approved);
  loadActivity();
}

// Loaded on mount for the badge alone: an approval raised in ASI1 and never
// answered is the one thing a user opening the panel needs to see before they
// choose a tab. It is a local SQLite read, so it costs nothing.
const {
  changes, messages: outboxMessages, totals: activityTotals,
  error: activityError, rollingBack, rollbackError,
  loadActivity, openActivity, rollBackChange, dismissRollbackError,
} = useActivity();

// Loaded on mount for the badge, like the activity totals beside it: a
// suggestion the agent raised is something the user should see they have,
// before they choose a tab. Also a local SQLite read.
const {
  open: openSuggestions, verified: verifiedSuggestions, totals: suggestionTotals,
  error: suggestionError, answering, deciding, outcome: suggestionOutcome,
  count: suggestionCount, loadSuggestions, answerSuggestion: _answerSuggestion,
  answerIdea: _answerIdea, decideVerification: _decideVerification, dismissOutcome,
} = useSuggestions();

// The panel answers a suggestion through the same /rest/approve the chat path
// reaches; the only thing this adds is refreshing the Activity tab afterwards,
// because applying one writes a change into the journal that tab is showing.
async function answerSuggestion(changeId, approved) {
  await _answerSuggestion(changeId, approved);
  loadActivity();
}

// An explorer idea's answer ends on that same /rest/approve (after its
// feedback is recorded), so it refreshes the journal for the same reason.
async function answerIdea(suggestionId, changeId, answer, reason) {
  await _answerIdea(suggestionId, changeId, answer, reason);
  loadActivity();
}

// Answering a measured result refreshes the journal for the same reason, and
// with more force: rolling one back writes a rollback row into it.
async function decideVerification(suggestionId, keep) {
  await _decideVerification(suggestionId, keep);
  loadActivity();
}

const activeTab = ref("chat");

const tabs = computed(() => [
  { id: "chat", label: "Chat" },
  { id: "watching", label: "Watching", badge: watchedCount.value || null },
  { id: "record", label: "Record" },
  {
    id: "suggestions",
    label: "Suggestions",
    badge: suggestionCount.value || null,
  },
  {
    id: "activity",
    label: "Activity",
    badge: activityTotals.value.unseen || null,
    alert: !!activityTotals.value.awaiting,
    alertTitle: "A change is waiting for your answer",
  },
  {
    id: "asi1",
    label: "Connections",
    badge: paired.value.length || null,
    alert: !paired.value.length || !!pendingPeers.value.length || !!peerProposals.value.length,
    alertTitle: peerProposals.value.length
      ? "An agent's proposal is waiting for your answer"
      : pendingPeers.value.length
        ? "An agent is asking to connect"
        : "No devices paired — the agent will not act on ASI1 messages",
  },
]);

// The registry is a websocket round trip and the record is a database read, so
// neither is fetched until its tab is actually opened. The Record tab then
// refreshes on a timer, because it is the one screen whose content changes
// while you are looking at it.
let recordTimer = null;

function stopRecordPolling() {
  if (recordTimer) {
    clearInterval(recordTimer);
    recordTimer = null;
  }
}

watch(activeTab, (tab) => {
  stopRecordPolling();
  if (tab === "watching") {
    loadRegistry();
  } else if (tab === "record") {
    loadRecord();
    recordTimer = setInterval(loadRecord, 15000);
  } else if (tab === "suggestions") {
    // Read fresh on open and then left alone: unlike the record, this changes
    // once a day at REVIEW_HOUR, and a card refreshing under a user who is
    // reading it would be worse than a stale count.
    loadSuggestions();
  } else if (tab === "activity") {
    // Opening the tab is what marks messages seen, so the first call is the
    // one that stamps them; the refresh afterwards only reads.
    openActivity();
    recordTimer = setInterval(loadActivity, 15000);
  } else if (tab === "asi1") {
    loadPeers();
  }
});

const openDiff = () => { showDiffModal.value = true; };
const closeDiff = () => { showDiffModal.value = false; };

onMounted(() => {
  initSession();
  loadStatus();
  // Only for the tab badge — the count is the one piece of observation state
  // worth knowing before the tab is opened, since "0 watched" is the thing a
  // new user needs to notice.
  loadRegistry();
  loadActivity();
  loadSuggestions();
  loadPeers();
});

onUnmounted(stopRecordPolling);
</script>
