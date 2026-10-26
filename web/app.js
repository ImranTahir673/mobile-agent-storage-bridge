/**
 * Storage Copilot — Phase 8 Mobile Web UI Controller
 * Features:
 * - Real-time Server-Sent Events (SSE) telemetry via EventSource (/stream?token=...)
 * - Live micro-batch progress bar & in-flight cancellation (POST /cancel_execution)
 * - Interactive Action Plan Diff Cards with selective per-action checkboxes
 * - Phase 6 Deep Deduplication (Exact SHA-256 + 3-Gram Jaccard Near-Duplicate toggle)
 * - Phase 6 Forgotten Files Finder (>90d stale downloads, .tmp files, orphaned screenshots)
 * - Persistent 1-Tap Undo Banner wired to SQLite WAL (/recent_batch & /rollback_batch)
 */

const state = {
  conversationHistory: [],
  activeBatchId: null,
  lastCompletedBatchId: null,
  sseSource: null,
  bearerToken: localStorage.getItem("copilot_bearer_token") || "",
};

const dom = {
  chatContainer: document.getElementById("chat-container"),
  promptForm: document.getElementById("prompt-form"),
  promptInput: document.getElementById("prompt-input"),
  targetFolderSelect: document.getElementById("target-folder-select"),
  quickChipsBar: document.getElementById("quick-chips-bar"),
  sseStatusPill: document.getElementById("sse-status-pill"),
  aiStatusPill: document.getElementById("ai-status-pill"),
  versionBadge: document.getElementById("version-badge"),
  undoBanner: document.getElementById("undo-banner"),
  undoBannerTitle: document.getElementById("undo-banner-title"),
  undoBannerSubtitle: document.getElementById("undo-banner-subtitle"),
  btnUndoBatch: document.getElementById("btn-undo-batch"),
  progressPanel: document.getElementById("live-progress-panel"),
  progressPhaseBadge: document.getElementById("progress-phase-badge"),
  progressStatusText: document.getElementById("progress-status-text"),
  progressPercentText: document.getElementById("progress-percent-text"),
  progressBarFill: document.getElementById("progress-bar-fill"),
  btnCancelExecution: document.getElementById("btn-cancel-execution"),
  btnSeedFixtures: document.getElementById("btn-seed-fixtures"),
  chkSeedDuplicates: document.getElementById("chk-seed-duplicates"),
  btnScanDuplicates: document.getElementById("btn-scan-duplicates"),
  chkIncludeNear: document.getElementById("chk-include-near"),
  btnScanForgotten: document.getElementById("btn-scan-forgotten"),
  btnViewFolders: document.getElementById("btn-view-folders"),
  settingsModal: document.getElementById("settings-modal"),
  btnOpenSettings: document.getElementById("btn-open-settings"),
  btnCloseSettings: document.getElementById("btn-close-settings"),
  btnSaveSettings: document.getElementById("btn-save-settings"),
  inputApiKey: document.getElementById("input-api-key"),
  inputBearerToken: document.getElementById("input-bearer-token"),
};

function buildHeaders() {
  const headers = { "Content-Type": "application/json" };
  if (state.bearerToken) {
    headers["Authorization"] = `Bearer ${state.bearerToken}`;
  }
  return headers;
}

function formatMarkdownSimple(text) {
  if (!text) return "";
  const lines = text.split("\n");
  let html = "";
  let inList = false;

  for (let rawLine of lines) {
    let line = rawLine.trim();
    if (!line) {
      if (inList) { html += "</ul>"; inList = false; }
      continue;
    }

    let escaped = line
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    escaped = escaped.replace(/`([^`]+)`/g, "<code>$1</code>");
    escaped = escaped.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    escaped = escaped.replace(/\*([^*]+)\*/g, "<em>$1</em>");

    if (escaped.startsWith("#### ")) {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<h4 class="msg-subheading">${escaped.slice(5)}</h4>`;
    } else if (escaped.startsWith("### ")) {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<h3 class="msg-heading">${escaped.slice(4)}</h3>`;
    } else if (escaped.startsWith("- ") || escaped.startsWith("* ")) {
      if (!inList) { html += "<ul class='msg-list'>"; inList = true; }
      html += `<li>${escaped.slice(2)}</li>`;
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<p>${escaped}</p>`;
    }
  }
  if (inList) html += "</ul>";
  return html;
}

function scrollToBottom() {
  dom.chatContainer.scrollTop = dom.chatContainer.scrollHeight;
}

// ============================================================================
// 1. Live Server-Sent Events (SSE) Connection (/stream)
// ============================================================================
function connectSSEStream() {
  if (state.sseSource) {
    state.sseSource.close();
  }
  const tokenParam = state.bearerToken ? `?token=${encodeURIComponent(state.bearerToken)}` : "";
  const streamUrl = `/stream${tokenParam}`;
  const es = new EventSource(streamUrl);
  state.sseSource = es;

  es.addEventListener("connected", () => {
    dom.sseStatusPill.textContent = "● Live SSE Connected";
    dom.sseStatusPill.className = "status-pill status-online";
  });

  es.addEventListener("classifier_intent", (evt) => {
    try {
      const data = JSON.parse(evt.data);
      showProgressBanner(
        data.intent || "CLASSIFYING",
        `Intent routed via ${data.fast_path_matched ? "0ms Fast-Path Gate" : "Micro-LLM Router"} (${data.sub_intent || "standard"})`,
        15,
        false
      );
    } catch (_) {}
  });

  es.addEventListener("scan_progress", (evt) => {
    try {
      const data = JSON.parse(evt.data);
      if (data.phase === "scanning_metadata") {
        showProgressBanner("SCANNING", `Scanning ${data.target_folder}/ (${data.total_files} files)...`, 35, false);
      } else if (data.phase === "scan_complete") {
        showProgressBanner("INDEXED", `Indexed ${data.total_files} files (${data.hydrated_snippets || 0} snippets hydrated)`, 70, false);
        setTimeout(() => hideProgressBannerIfIdle(), 900);
      }
    } catch (_) {}
  });

  es.addEventListener("plan_synthesized", () => {
    setTimeout(() => hideProgressBannerIfIdle(), 600);
  });

  es.addEventListener("execution_progress", (evt) => {
    try {
      const data = JSON.parse(evt.data);
      state.activeBatchId = data.batch_id || state.activeBatchId;
      const pct = typeof data.percent === "number" ? data.percent : 0;

      if (data.phase === "started" || data.phase === "chunk_committed") {
        const chunkStr = data.total_chunks ? `Chunk ${data.current_chunk}/${data.total_chunks}` : "Executing";
        showProgressBanner(
          "EXECUTING",
          `${chunkStr} — ${data.actions_completed || data.executed || 0} of ${data.total || 0} actions committed`,
          pct,
          true
        );
      } else if (data.phase === "completed") {
        showProgressBanner("COMPLETED", `Completed ${data.executed || 0} actions (100%)`, 100, false);
        state.activeBatchId = null;
        refreshRecentBatchBanner();
        setTimeout(() => hideProgressBannerIfIdle(), 1500);
      } else if (data.phase === "cancelled") {
        showProgressBanner("CANCELLED", `Stopped cleanly after ${data.actions_completed || 0} actions`, pct, false);
        state.activeBatchId = null;
        refreshRecentBatchBanner();
        setTimeout(() => hideProgressBannerIfIdle(), 2000);
      } else if (data.phase === "partial_failure" || data.phase === "rolled_back") {
        showProgressBanner("HALTED", data.error || "Execution encountered an error", pct, false);
        state.activeBatchId = null;
        refreshRecentBatchBanner();
      }
    } catch (_) {}
  });

  es.onerror = () => {
    dom.sseStatusPill.textContent = "● SSE Reconnecting...";
    dom.sseStatusPill.className = "status-pill status-connecting";
  };
}

function showProgressBanner(phase, text, percent, showCancelBtn) {
  dom.progressPanel.classList.remove("hidden");
  dom.progressPhaseBadge.textContent = phase;
  dom.progressStatusText.textContent = text;
  dom.progressPercentText.textContent = `${Math.round(percent)}%`;
  dom.progressBarFill.style.width = `${Math.min(100, Math.max(0, percent))}%`;
  if (showCancelBtn) {
    dom.btnCancelExecution.classList.remove("hidden");
  } else {
    dom.btnCancelExecution.classList.add("hidden");
  }
}

function hideProgressBannerIfIdle() {
  if (!state.activeBatchId) {
    dom.progressPanel.classList.add("hidden");
  }
}

// ============================================================================
// 2. Chat & Interactive Diff Plan Rendering (Selective Checkboxes)
// ============================================================================
function appendUserMessage(text) {
  const div = document.createElement("div");
  div.className = "chat-message user-message";
  div.textContent = text;
  dom.chatContainer.appendChild(div);
  scrollToBottom();
}

function showThinkingIndicator(promptText) {
  removeThinkingIndicator();
  const div = document.createElement("div");
  div.className = "chat-message assistant-message thinking-bubble";
  div.id = "activeThinkingBubble";
  div.innerHTML = `
    <div class="msg-header">
      <span class="msg-author">Storage Copilot</span>
      <span class="msg-badge pulse-badge">ANALYZING...</span>
    </div>
    <div class="msg-body thinking-body">
      <div class="typing-indicator">
        <span></span><span></span><span></span>
      </div>
      <span class="thinking-text">Auditing storage & evaluating request...</span>
    </div>
  `;
  dom.chatContainer.appendChild(div);
  scrollToBottom();
}

function removeThinkingIndicator() {
  const el = document.getElementById("activeThinkingBubble");
  if (el) {
    el.remove();
  }
}

function appendAssistantResponse(data) {
  const div = document.createElement("div");
  div.className = "chat-message assistant-message";

  const intentLabel = data.intent || (data.type || "conversation").toUpperCase();
  const badgesHtml = (data.badges || [intentLabel])
    .map((b) => `<span class="msg-badge">${b}</span>`)
    .join(" ");

  div.innerHTML = `
    <div class="msg-header">
      <span class="msg-author">Storage Copilot</span>
      ${badgesHtml}
    </div>
    <div class="msg-body">${formatMarkdownSimple(data.message || data.analysis || "")}</div>
  `;

  // If this response is a Duplicate Audit with duplicates, add 1-click Clean Duplicates button
  if (data.duplicate_summary && data.duplicate_summary.total_duplicates > 0 && data.type !== "plan") {
    const actionBox = document.createElement("div");
    actionBox.className = "quick-audit-action";
    actionBox.innerHTML = `
      <button type="button" class="btn-action-trigger">
        🧹 Clean ${data.duplicate_summary.total_duplicates} Redundant Copy(ies) (${data.duplicate_summary.reclaimable_human})
      </button>
    `;
    actionBox.querySelector("button").addEventListener("click", () => {
      submitPrompt("Clean duplicate downloads");
    });
    div.appendChild(actionBox);
  }

  // If this response is a Forgotten Files Audit with items, add 1-click Clean Forgotten Files button
  if (data.forgotten_summary && data.forgotten_summary.total_forgotten > 0 && data.type !== "plan") {
    const actionBox = document.createElement("div");
    actionBox.className = "quick-audit-action";
    actionBox.innerHTML = `
      <button type="button" class="btn-action-trigger">
        🗑️ Clean ${data.forgotten_summary.total_forgotten} Forgotten File(s) (${data.forgotten_summary.reclaimable_human})
      </button>
    `;
    actionBox.querySelector("button").addEventListener("click", () => {
      submitPrompt("Clean forgotten files");
    });
    div.appendChild(actionBox);
  }

  // If this response contains a proposed Action Plan, render Selective Diff Cards
  const actions = data.actions || (data.plan && data.plan.actions) || [];
  if (data.type === "plan" && actions.length > 0) {
    const planObj = data.plan || {
      plan_id: `plan-${Date.now()}`,
      description: data.message || "Proposed Action Plan",
      actions: actions,
    };
    const reviewBox = buildSelectivePlanReviewCard(planObj);
    div.appendChild(reviewBox);
  }

  dom.chatContainer.appendChild(div);

  if (Array.isArray(data.quick_chips) && data.quick_chips.length > 0) {
    renderQuickChips(data.quick_chips);
  }
  scrollToBottom();
}

function buildSelectivePlanReviewCard(plan) {
  const box = document.createElement("div");
  box.className = "plan-review-box";

  const actions = plan.actions || [];
  const selectedIndices = new Set(actions.map((_, idx) => idx));

  const header = document.createElement("div");
  header.className = "plan-review-header";

  const countLabel = document.createElement("span");
  countLabel.style.fontSize = "12px";
  countLabel.style.fontWeight = "700";

  const controls = document.createElement("div");
  controls.className = "plan-select-controls";

  const btnSelectAll = document.createElement("button");
  btnSelectAll.type = "button";
  btnSelectAll.className = "link-btn";
  btnSelectAll.textContent = "Select All";

  const btnClearAll = document.createElement("button");
  btnClearAll.type = "button";
  btnClearAll.className = "link-btn";
  btnClearAll.textContent = "Clear All";

  controls.appendChild(btnSelectAll);
  controls.appendChild(btnClearAll);
  header.appendChild(countLabel);
  header.appendChild(controls);

  const listEl = document.createElement("div");
  listEl.className = "diff-list";

  const execBtn = document.createElement("button");
  execBtn.type = "button";
  execBtn.className = "btn-execute-plan";

  function updateSelectionUI() {
    const count = selectedIndices.size;
    countLabel.textContent = `${count} of ${actions.length} actions selected`;
    execBtn.textContent = `✅ Execute Selected (${count} Action${count === 1 ? "" : "s"})`;
    execBtn.disabled = count === 0;
  }

  const checkboxes = [];
  actions.forEach((act, idx) => {
    const card = document.createElement("div");
    card.className = "diff-card";

    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.className = "diff-checkbox";
    chk.checked = true;
    chk.addEventListener("change", () => {
      if (chk.checked) selectedIndices.add(idx);
      else selectedIndices.delete(idx);
      updateSelectionUI();
    });
    checkboxes.push(chk);

    const badgeType = act.badge || (act.type || "MOVE").toUpperCase();
    const srcPath = act.source || act.path || "";
    const dstPath = act.destination || (act.type === "trash" ? ".agent_trash/" : "");
    const piiBadge = act.ai_inspected
      ? `<span class="pii-badge">🛡️ PII-Sanitized Inspect</span>`
      : "";

    const content = document.createElement("div");
    content.className = "diff-content";
    content.innerHTML = `
      <div class="diff-badges">
        <span class="op-badge op-${badgeType}">${badgeType}</span>
        ${piiBadge}
      </div>
      <div class="diff-paths">${srcPath}</div>
      ${dstPath ? `<div class="diff-paths diff-dest">➔ ${dstPath}</div>` : ""}
      ${act.rationale ? `<div class="diff-rationale">${act.rationale}</div>` : ""}
    `;

    card.appendChild(chk);
    card.appendChild(content);
    listEl.appendChild(card);
  });

  btnSelectAll.addEventListener("click", () => {
    checkboxes.forEach((c, i) => {
      c.checked = true;
      selectedIndices.add(i);
    });
    updateSelectionUI();
  });

  btnClearAll.addEventListener("click", () => {
    checkboxes.forEach((c) => (c.checked = false));
    selectedIndices.clear();
    updateSelectionUI();
  });

  execBtn.addEventListener("click", async () => {
    const filteredActions = actions.filter((_, idx) => selectedIndices.has(idx));
    if (filteredActions.length === 0) return;
    execBtn.disabled = true;
    execBtn.textContent = "⏳ Executing Micro-Batch...";

    const filteredPlan = {
      ...plan,
      actions: filteredActions,
    };
    await executeSelectedPlan(filteredPlan, execBtn);
  });

  const footer = document.createElement("div");
  footer.className = "plan-review-footer";
  footer.appendChild(execBtn);

  updateSelectionUI();
  box.appendChild(header);
  box.appendChild(listEl);
  box.appendChild(footer);
  return box;
}

// ============================================================================
// 3. API Interactions (/propose_plan, /execute_plan, /cancel_execution, /rollback_batch)
// ============================================================================
async function submitPrompt(promptText) {
  const cleanPrompt = (promptText || "").trim();
  if (!cleanPrompt) return;

  appendUserMessage(cleanPrompt);
  dom.promptInput.value = "";
  const targetFolder = dom.targetFolderSelect.value || "Download";

  showThinkingIndicator(cleanPrompt);

  try {
    const resp = await fetch("/propose_plan", {
      method: "POST",
      headers: buildHeaders(),
      body: JSON.stringify({
        prompt: cleanPrompt,
        target_folder: targetFolder,
        conversation_history: state.conversationHistory.slice(-6),
      }),
    });
    const data = await resp.json();
    removeThinkingIndicator();
    state.conversationHistory.push({ role: "user", content: cleanPrompt });
    state.conversationHistory.push({ role: "assistant", content: data.message || "" });
    appendAssistantResponse(data);
  } catch (err) {
    removeThinkingIndicator();
    appendAssistantResponse({
      type: "conversation",
      intent: "CONVERSATION",
      message: `⚠️ Network error communicating with Storage Copilot: ${err.message}`,
    });
  }
}

async function executeSelectedPlan(planPayload, buttonEl) {
  state.activeBatchId = planPayload.plan_id;
  showProgressBanner("STARTING", `Starting execution of ${planPayload.actions.length} action(s)...`, 5, true);

  try {
    const resp = await fetch("/execute_plan", {
      method: "POST",
      headers: buildHeaders(),
      body: JSON.stringify(planPayload),
    });
    const data = await resp.json();
    state.activeBatchId = null;

    if (resp.ok && (data.status === "success" || data.status === "cancelled" || data.status === "partial_failure")) {
      state.lastCompletedBatchId = data.batch_id;
      buttonEl.textContent = data.cancelled
        ? `⏹ Cancelled (${data.executed_actions} Executed)`
        : `✓ Executed (${data.executed_actions} Actions)`;
      appendAssistantResponse({
        type: "conversation",
        intent: "FILE_OPERATION",
        badges: [data.status === "success" ? "COMPLETED" : data.status.toUpperCase()],
        message:
          data.status === "success"
            ? `✅ **Batch Execution Complete!** Successfully executed **${data.executed_actions} action(s)** across **${data.chunks_processed || 1} micro-batch chunk(s)** (Batch ID: \`${data.batch_id}\`). You can revert this entire batch at any time with 1-Tap Undo.`
            : data.cancelled
            ? `⏹ **Batch Execution Cancelled.** Safely committed **${data.executed_actions} of ${data.total_actions} action(s)** before stopping. Tap **1-Tap Undo Batch** above if you wish to restore them.`
            : `⚠️ **Partial Failure Recovery:** ${data.error}`,
      });
      await refreshRecentBatchBanner();
    } else {
      buttonEl.disabled = false;
      buttonEl.textContent = "Retry Execution";
      appendAssistantResponse({
        type: "conversation",
        intent: "CONVERSATION",
        badges: ["ERROR"],
        message: `⚠️ **Execution Error:** ${data.error || "Unknown error"}`,
      });
    }
  } catch (err) {
    state.activeBatchId = null;
    buttonEl.disabled = false;
    buttonEl.textContent = "Retry Execution";
  }
}

async function cancelActiveExecution() {
  try {
    await fetch("/cancel_execution", {
      method: "POST",
      headers: buildHeaders(),
      body: JSON.stringify({
        batch_id: state.activeBatchId,
        reason: "User clicked Stop Execution in Web UI",
      }),
    });
  } catch (_) {}
}

async function refreshRecentBatchBanner() {
  try {
    const resp = await fetch("/recent_batch");
    const data = await resp.json();
    if (data.status === "success" && data.batch && data.batch.status !== "ROLLED_BACK" && data.batch.executed_actions > 0) {
      state.lastCompletedBatchId = data.batch.batch_id;
      dom.undoBanner.classList.remove("hidden");
      dom.undoBannerTitle.textContent = `Recent Batch: ${data.batch.executed_actions} action(s) executed`;
      dom.undoBannerSubtitle.textContent = `Batch ${data.batch.batch_id.slice(0, 8)}... (${data.batch.status}) — Protected by SQLite WAL`;
    } else {
      dom.undoBanner.classList.add("hidden");
    }
  } catch (_) {}
}

async function triggerRollbackLastBatch() {
  if (!state.lastCompletedBatchId) return;
  dom.btnUndoBatch.disabled = true;
  dom.btnUndoBatch.textContent = "Reverting...";
  try {
    const resp = await fetch("/rollback_batch", {
      method: "POST",
      headers: buildHeaders(),
      body: JSON.stringify({ batch_id: state.lastCompletedBatchId }),
    });
    const data = await resp.json();
    if (resp.ok && data.status === "rolled_back") {
      appendAssistantResponse({
        type: "conversation",
        intent: "FILE_OPERATION",
        badges: ["ROLLED BACK", `${data.reverted_actions} Reverted`],
        message: `⏪ **1-Tap Atomic Rollback Complete!** Restored **${data.reverted_actions} action(s)** from Batch \`${data.batch_id}\` back to their original locations with 100% byte-for-byte fidelity.`,
      });
      dom.undoBanner.classList.add("hidden");
    }
  } finally {
    dom.btnUndoBatch.disabled = false;
    dom.btnUndoBatch.textContent = "1-Tap Undo Batch";
  }
}

function renderQuickChips(chips) {
  dom.quickChipsBar.innerHTML = "";
  chips.forEach((chipText) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip-btn";
    btn.textContent = chipText;
    const cleanPrompt = chipText.replace(/^[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}✅⏩🧹📋👥🏷️🗑️📂💡📚🧾🕰️❌]+\s*/u, "");
    btn.addEventListener("click", () => submitPrompt(cleanPrompt));
    dom.quickChipsBar.appendChild(btn);
  });
}

// ============================================================================
// 4. Toolbar & Initialization Event Listeners
// ============================================================================
async function checkApiStatus() {
  try {
    const resp = await fetch("/api_status");
    const data = await resp.json();
    if (data.app_version) {
      dom.versionBadge.textContent = data.app_version;
    }
    if (data.gemini_configured && !data.quota_limited) {
      dom.aiStatusPill.textContent = `⚡ Gemini Online (${data.masked_key || "Active"})`;
      dom.aiStatusPill.className = "status-pill status-online";
    } else {
      dom.aiStatusPill.textContent = "🛡️ Local Offline Engine";
      dom.aiStatusPill.className = "status-pill status-offline";
    }
  } catch (_) {}
}

dom.promptForm.addEventListener("submit", (e) => {
  e.preventDefault();
  submitPrompt(dom.promptInput.value);
});

dom.promptInput.addEventListener("focus", () => {
  setTimeout(() => {
    scrollToBottom();
  }, 250);
});

document.querySelectorAll(".chip-btn[data-prompt]").forEach((btn) => {
  btn.addEventListener("click", () => submitPrompt(btn.getAttribute("data-prompt")));
});

dom.btnCancelExecution.addEventListener("click", cancelActiveExecution);
dom.btnUndoBatch.addEventListener("click", triggerRollbackLastBatch);

dom.btnSeedFixtures.addEventListener("click", async () => {
  const withDups = dom.chkSeedDuplicates.checked;
  const resp = await fetch("/seed_fixtures", {
    method: "POST",
    headers: buildHeaders(),
    body: JSON.stringify({ duplicates: withDups }),
  });
  const data = await resp.json();
  if (resp.ok) {
    appendAssistantResponse({
      type: "conversation",
      intent: "CONVERSATION",
      badges: ["FIXTURES SEEDED", `${(data.files || []).length} Files`],
      message: `🌱 **Seeded ${(data.files || []).length} sample mobile files into \`Download/\`!**\n\nFiles ready for testing: \`${(data.files || []).join("`, `")}\`.`,
    });
  }
});

dom.btnScanDuplicates.addEventListener("click", () => {
  const includeNear = dom.chkIncludeNear.checked;
  if (includeNear) {
    submitPrompt("Find near duplicate files in Download");
  } else {
    submitPrompt("Find duplicate files in Download");
  }
});

dom.btnScanForgotten.addEventListener("click", () => {
  submitPrompt("Find forgotten files in Download");
});

dom.btnViewFolders.addEventListener("click", () => {
  submitPrompt("View organized folders");
});

dom.btnOpenSettings.addEventListener("click", () => {
  dom.inputBearerToken.value = state.bearerToken;
  dom.settingsModal.classList.remove("hidden");
});

dom.btnCloseSettings.addEventListener("click", () => {
  dom.settingsModal.classList.add("hidden");
});

dom.btnSaveSettings.addEventListener("click", async () => {
  const newToken = dom.inputBearerToken.value.trim();
  state.bearerToken = newToken;
  localStorage.setItem("copilot_bearer_token", newToken);

  const newKey = dom.inputApiKey.value.trim();
  if (newKey) {
    await fetch("/configure_api_key", {
      method: "POST",
      headers: buildHeaders(),
      body: JSON.stringify({ api_key: newKey }),
    });
  }
  dom.settingsModal.classList.add("hidden");
  checkApiStatus();
  connectSSEStream();
});

// Initialize on page load
checkApiStatus();
connectSSEStream();
refreshRecentBatchBanner();
