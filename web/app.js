/**
 * Agent Storage Bridge - Mobile Copilot Front-End Controller
 * Clean Vanilla ES6+ - Zero External Dependencies
 */

document.addEventListener('DOMContentLoaded', () => {
  // ==============================================================================
  // State Management
  // ==============================================================================
  const state = {
    engineOnline: false,
    baseDir: '',
    userProfile: null,
    lastBatchId: null,
    lastBatchActions: 0,
    activePlan: null,
    selectedActionIds: new Set()
  };

  // ==============================================================================
  // DOM Elements
  // ==============================================================================
  const elements = {
    engineStatusBadge: document.getElementById('engine-status-badge'),
    engineStatusText: document.getElementById('engine-status-text'),
    userDisplayName: document.getElementById('user-display-name'),
    userIdTag: document.getElementById('user-id-tag'),
    userOrg: document.getElementById('user-org'),
    storageBasePath: document.getElementById('storage-base-path'),
    copyPathBtn: document.getElementById('copy-path-btn'),
    seedBtn: document.getElementById('seed-btn'),
    peerPillsList: document.getElementById('peer-pills-list'),
    peerCountBadge: document.getElementById('peer-count-badge'),
    undoBanner: document.getElementById('undo-banner'),
    undoTitle: document.getElementById('undo-title'),
    undoMeta: document.getElementById('undo-meta'),
    undoBtn: document.getElementById('undo-btn'),
    conversationStream: document.getElementById('conversation-stream'),
    contentScroll: document.getElementById('content-scroll'),
    chipsContainer: document.getElementById('chips-container'),
    promptForm: document.getElementById('prompt-form'),
    promptInput: document.getElementById('prompt-input'),
    sendBtn: document.getElementById('send-btn'),
    toast: document.getElementById('toast'),
    toastMessage: document.getElementById('toast-message')
  };

  // ==============================================================================
  // Toast Notifications
  // ==============================================================================
  let toastTimer = null;
  function showToast(message, type = 'info', duration = 3200) {
    if (!elements.toast) return;
    clearTimeout(toastTimer);
    elements.toastMessage.textContent = message;
    elements.toast.className = `toast-snackbar show ${type}`;
    toastTimer = setTimeout(() => {
      elements.toast.className = 'toast-snackbar';
    }, duration);
  }

  const EXPECTED_VERSION = 'v0.4-live';

  async function fetchHealth() {
    try {
      const resp = await fetch('/health', {
        headers: { 'Accept': 'application/json' }
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      
      state.engineOnline = data.status === 'running';
      state.baseDir = data.base_dir || '';
      state.version = data.version || '';

      const versionBadge = document.getElementById('bridge-version');
      if (versionBadge) {
        versionBadge.textContent = `Android Bridge ${state.version || EXPECTED_VERSION}`;
      }

      if (data.version && data.version !== EXPECTED_VERSION) {
        console.warn(`[Version Mismatch] Expected ${EXPECTED_VERSION}, got ${data.version}`);
      } else {
        console.log(`[Version Verified] Running ${EXPECTED_VERSION}`);
      }

      elements.engineStatusBadge.className = 'engine-badge online';
      elements.engineStatusText.textContent = 'Engine Online';
      elements.storageBasePath.textContent = state.baseDir || 'Connected Root';
      elements.storageBasePath.title = state.baseDir;
      return data;
    } catch (err) {
      console.warn('Bridge health check failed:', err);
      state.engineOnline = false;
      elements.engineStatusBadge.className = 'engine-badge offline';
      elements.engineStatusText.textContent = 'Bridge Offline';
      elements.storageBasePath.textContent = 'Offline (Check server)';
      return null;
    }
  }

  async function fetchUserProfile() {
    try {
      const resp = await fetch('/user_profile');
      if (!resp.ok) return;
      const data = await resp.json();
      const profile = data.profile || {};
      state.userProfile = profile;

      // Update Owner Details
      const identity = profile.user_identity || {};
      const primaryName = identity.primary_name || 'Imran Tahir';
      elements.userDisplayName.textContent = primaryName;

      const ids = identity.identifiers || [];
      elements.userIdTag.textContent = ids.length > 0 ? ids[0] : 'BSAI-182';
      elements.userOrg.textContent = identity.organization ? `${identity.organization} • Storage Profile` : 'University Storage Profile';

      // Update Registered Peers
      const peers = profile.known_peers || [];
      if (peers.length > 0) {
        elements.peerCountBadge.textContent = `${peers.length} Peers`;
        elements.peerPillsList.innerHTML = peers.map(peer => {
          const initial = (peer.name || 'P')[0].toUpperCase();
          return `
            <span class="peer-pill" title="Routing: ${peer.designated_folder || 'Documents/Peers/' + peer.name}">
              <span class="peer-avatar">${initial}</span>
              ${peer.name}
            </span>
          `;
        }).join('');
      }
    } catch (err) {
      console.warn('Could not load user profile:', err);
    }
  }

  async function fetchRecentBatch() {
    try {
      const resp = await fetch('/recent_batch');
      if (!resp.ok) return;
      const data = await resp.json();
      const batch = data.batch;
      if (batch && batch.batch_id) {
        state.lastBatchId = batch.batch_id;
        state.lastBatchActions = batch.executed_actions || batch.total_actions || 0;
        updateUndoBanner(batch.batch_id, batch.status, state.lastBatchActions);
      }
    } catch (err) {
      console.warn('Could not fetch recent batch:', err);
    }
  }

  async function seedDemoFixtures() {
    try {
      elements.seedBtn.disabled = true;
      elements.seedBtn.style.opacity = '0.6';
      showToast('Seeding fixture files in Download/...', 'info');

      const resp = await fetch('/seed_fixtures', { method: 'POST' });
      const data = await resp.json();

      if (data.status === 'success') {
        showToast(`✓ Fixtures ready: ${data.files.length} test files available`, 'success');
      } else {
        showToast('Seed finished with note', 'info');
      }
    } catch (err) {
      showToast(`Seed failed: ${err.message}`, 'error');
    } finally {
      elements.seedBtn.disabled = false;
      elements.seedBtn.style.opacity = '1';
    }
  }

  // ==============================================================================
  // Undo Banner Controller
  // ==============================================================================
  function updateUndoBanner(batchId, status = 'COMPLETED', actionCount = 0) {
    if (!batchId) {
      elements.undoBanner.style.display = 'none';
      return;
    }

    elements.undoBanner.style.display = 'flex';
    const shortId = batchId.length > 10 ? `${batchId.substring(0, 8)}...` : batchId;

    if (status === 'ROLLED_BACK') {
      elements.undoTitle.textContent = `Batch #${shortId} (Reverted)`;
      elements.undoMeta.textContent = `Rolled back via inverted ledger`;
      elements.undoBtn.disabled = true;
      elements.undoBtn.style.opacity = '0.4';
      elements.undoBtn.style.cursor = 'not-allowed';
    } else {
      elements.undoTitle.textContent = `Batch #${shortId} Active`;
      elements.undoMeta.textContent = `${actionCount} filesystem operations executed`;
      elements.undoBtn.disabled = false;
      elements.undoBtn.style.opacity = '1';
      elements.undoBtn.style.cursor = 'pointer';
    }
  }

  async function handleRollback() {
    if (!state.lastBatchId) return;

    try {
      elements.undoBtn.disabled = true;
      elements.undoBtn.innerHTML = `<span>Reverting...</span>`;

      const resp = await fetch('/rollback_batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ batch_id: state.lastBatchId })
      });

      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || 'Failed to rollback batch');
      }

      showToast(`✓ Rollback complete! ${data.reverted_actions || 0} operations reverted`, 'success', 4000);
      updateUndoBanner(state.lastBatchId, 'ROLLED_BACK', data.reverted_actions);

      // Append notice to chat timeline
      appendSystemNotice(`⟲ <strong>Rollback Executed:</strong> Batch <code>${state.lastBatchId.substring(0, 8)}</code> was reverted. All original paths restored.`);
    } catch (err) {
      showToast(`Undo Error: ${err.message}`, 'error', 4500);
      elements.undoBtn.disabled = false;
      elements.undoBtn.innerHTML = `<span>Undo</span><kbd>1-Tap</kbd>`;
    }
  }

  // ==============================================================================
  // Chat Timeline Rendering
  // ==============================================================================
  function scrollToBottom() {
    setTimeout(() => {
      elements.contentScroll.scrollTop = elements.contentScroll.scrollHeight;
    }, 60);
  }

  function appendUserMessage(text) {
    const card = document.createElement('div');
    card.className = 'message-card user';
    card.innerHTML = `
      <div class="message-header">
        <div class="message-sender">You</div>
        <span class="message-time">Just now</span>
      </div>
      <div class="message-body">
        <p>${escapeHtml(text)}</p>
      </div>
    `;
    elements.conversationStream.appendChild(card);
    scrollToBottom();
  }

  function appendAssistantMessage(text) {
    const card = document.createElement('div');
    card.className = 'message-card assistant';
    card.innerHTML = `
      <div class="message-header">
        <div class="assistant-avatar">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
        </div>
        <div class="message-sender">Storage Copilot</div>
        <span class="message-time">Just now</span>
      </div>
      <div class="message-body">
        <p>${text.replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>')}</p>
      </div>
    `;
    elements.conversationStream.appendChild(card);
    scrollToBottom();
  }

  function appendSystemNotice(html) {
    const card = document.createElement('div');
    card.className = 'message-card assistant';
    card.style.borderColor = 'rgba(239, 68, 68, 0.35)';
    card.style.backgroundColor = 'rgba(239, 68, 68, 0.08)';
    card.innerHTML = `
      <div class="message-body" style="color: #FCA5A5;">
        <p>${html}</p>
      </div>
    `;
    elements.conversationStream.appendChild(card);
    scrollToBottom();
  }

  function showTypingIndicator() {
    const bubble = document.createElement('div');
    bubble.className = 'message-card assistant';
    bubble.id = 'typing-indicator';
    bubble.innerHTML = `
      <div class="typing-bubble">
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
      </div>
    `;
    elements.conversationStream.appendChild(bubble);
    scrollToBottom();
    return bubble;
  }

  function removeTypingIndicator() {
    const bubble = document.getElementById('typing-indicator');
    if (bubble) bubble.remove();
  }

  // ==============================================================================
  // Interactive Diff Review Cards Rendering & Execution
  // ==============================================================================
  function renderDiffReview(plan) {
    state.activePlan = plan;
    const actions = plan.actions || [];
    
    // Select all actions by default
    state.selectedActionIds.clear();
    actions.forEach(act => state.selectedActionIds.add(act.action_id));

    const container = document.createElement('div');
    container.className = 'diff-review-container';
    container.id = `diff-review-${plan.plan_id}`;

    // Header
    const header = document.createElement('div');
    header.className = 'diff-header';
    header.innerHTML = `
      <div class="diff-title-row">
        <div class="diff-title">
          <span>Action Plan Proposal</span>
          <span class="diff-count-badge" id="diff-badge-count">${actions.length} Actions</span>
        </div>
      </div>
      <div class="diff-desc">${escapeHtml(plan.description || 'Filesystem reorganization')}</div>
    `;
    container.appendChild(header);

    // Diff Cards List
    const list = document.createElement('div');
    list.className = 'diff-cards-list';

    actions.forEach(act => {
      const card = createDiffCardElement(act);
      list.appendChild(card);
    });
    container.appendChild(list);

    // Action Bar (Approve Selected & Reject All)
    const actionBar = document.createElement('div');
    actionBar.className = 'diff-action-bar';
    actionBar.innerHTML = `
      <button class="approve-btn" id="approve-plan-btn">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px;">
          <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" />
        </svg>
        <span id="approve-btn-label">Approve Selected (${actions.length})</span>
      </button>
      <button class="reject-btn" id="reject-plan-btn">
        <span>Reject All</span>
      </button>
    `;
    container.appendChild(actionBar);

    elements.conversationStream.appendChild(container);
    scrollToBottom();

    // Event Listeners for Action Bar
    const approveBtn = actionBar.querySelector('#approve-plan-btn');
    const rejectBtn = actionBar.querySelector('#reject-plan-btn');

    approveBtn.addEventListener('click', () => executeApprovedActions(container, plan));
    rejectBtn.addEventListener('click', () => rejectPlan(container, plan));
  }

  function createDiffCardElement(act) {
    const card = document.createElement('div');
    card.className = 'diff-card selected';
    card.id = `card-${act.action_id}`;
    card.dataset.actionId = act.action_id;

    // Determine Badge
    const badgeType = act.badge || act.type.toUpperCase();
    const badgeLabel = act.badge_label || badgeType.replace('_', ' ');

    let mutationHtml = '';
    if (act.type === 'make_dir') {
      mutationHtml = `
        <div class="mutation-diff">
          <div class="path-node">
            <span class="node-label dst">DIR</span>
            <span class="node-path" title="${act.path}">${escapeHtml(act.path)}</span>
          </div>
        </div>
      `;
    } else if (act.type === 'trash') {
      mutationHtml = `
        <div class="mutation-diff">
          <div class="path-node">
            <span class="node-label src">FILE</span>
            <span class="node-path" title="${act.path}">${escapeHtml(act.path)}</span>
          </div>
          <div class="diff-connector">↓ soft-delete</div>
          <div class="path-node">
            <span class="node-label dst">VAULT</span>
            <span class="node-path">.agent_trash (Soft Delete)</span>
          </div>
        </div>
      `;
    } else {
      // move or copy
      mutationHtml = `
        <div class="mutation-diff">
          <div class="path-node">
            <span class="node-label src">SRC</span>
            <span class="node-path" title="${act.source}">${escapeHtml(act.source)}</span>
          </div>
          <div class="diff-connector">↓ destination</div>
          <div class="path-node">
            <span class="node-label dst">DST</span>
            <span class="node-path" title="${act.destination}">${escapeHtml(act.destination)}</span>
          </div>
        </div>
      `;
    }

    card.innerHTML = `
      <div class="card-top-bar">
        <div class="card-left-group">
          <div class="custom-checkbox">
            <svg class="check-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
              <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" />
            </svg>
          </div>
          <span class="action-badge badge-${badgeType}">${badgeLabel}</span>
          ${act.peer_name ? `<span class="peer-pill" style="font-size:10px;padding:2px 7px;"><span class="peer-avatar">${act.peer_name[0]}</span> ${act.peer_name}</span>` : ''}
        </div>
        <span class="step-id">${act.action_id}</span>
      </div>
      ${mutationHtml}
      ${act.rationale ? `
        <div class="card-rationale">
          <svg class="rationale-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span>${escapeHtml(act.rationale)}</span>
        </div>
      ` : ''}
    `;

    // Toggle Checkbox / Selection on Click
    card.addEventListener('click', (e) => {
      const isSelected = state.selectedActionIds.has(act.action_id);
      if (isSelected) {
        state.selectedActionIds.delete(act.action_id);
        card.classList.remove('selected');
        card.classList.add('excluded');
      } else {
        state.selectedActionIds.add(act.action_id);
        card.classList.remove('excluded');
        card.classList.add('selected');
      }
      updateDiffActionBarCount();
    });

    return card;
  }

  function updateDiffActionBarCount() {
    const total = (state.activePlan && state.activePlan.actions) ? state.activePlan.actions.length : 0;
    const selected = state.selectedActionIds.size;
    
    const approveLabel = document.getElementById('approve-btn-label');
    const badgeCount = document.getElementById('diff-badge-count');
    const approveBtn = document.getElementById('approve-plan-btn');

    if (approveLabel) approveLabel.textContent = `Approve Selected (${selected} of ${total})`;
    if (badgeCount) badgeCount.textContent = `${selected} of ${total} Selected`;

    if (approveBtn) {
      approveBtn.disabled = selected === 0;
      approveBtn.style.opacity = selected === 0 ? '0.5' : '1';
    }
  }

  function rejectPlan(container, plan) {
    container.innerHTML = `
      <div style="padding: 10px; text-align: center; color: var(--text-secondary);">
        <p>✕ Plan <code>${plan.plan_id.substring(0, 8)}</code> was rejected by user. No mutations were applied.</p>
      </div>
    `;
    state.activePlan = null;
    showToast('Action Plan cancelled', 'info');
  }

  async function executeApprovedActions(container, plan) {
    const selectedActions = (plan.actions || []).filter(act => state.selectedActionIds.has(act.action_id));
    if (selectedActions.length === 0) {
      showToast('No actions selected for execution', 'error');
      return;
    }

    const approveBtn = container.querySelector('#approve-plan-btn');
    const rejectBtn = container.querySelector('#reject-plan-btn');
    if (approveBtn) {
      approveBtn.disabled = true;
      approveBtn.innerHTML = `<span>Executing atomically...</span>`;
    }
    if (rejectBtn) rejectBtn.style.display = 'none';

    try {
      const planPayload = {
        plan_id: plan.plan_id,
        description: plan.description,
        collision_strategy: plan.collision_strategy || 'RENAME_NUMERIC',
        actions: selectedActions
      };

      const resp = await fetch('/execute_plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(planPayload)
      });

      const result = await resp.json();
      if (!resp.ok) {
        throw new Error(result.error || 'Batch execution failed');
      }

      // Success Display
      container.innerHTML = `
        <div style="padding: 14px; display: flex; flex-direction: column; gap: 8px; text-align: center;">
          <div style="display: flex; align-items: center; justify-content: center; gap: 8px; color: #34D399; font-weight: 800; font-size: 14px;">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width:20px;height:20px;">
              <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <span>Batch Executed Successfully</span>
          </div>
          <p style="font-size: 12px; color: var(--text-secondary);">
            Batch <code>#${result.batch_id.substring(0, 8)}</code> applied ${result.executed_actions} atomic changes. Pre-logged to SQLite WAL ledger.
          </p>
        </div>
      `;

      state.lastBatchId = result.batch_id;
      state.lastBatchActions = result.executed_actions;
      updateUndoBanner(result.batch_id, 'COMPLETED', result.executed_actions);

      showToast(`✓ Batch #${result.batch_id.substring(0, 8)} executed! (1-Tap Undo active)`, 'success', 4000);
    } catch (err) {
      showToast(`Execution Error: ${err.message}`, 'error', 5000);
      if (approveBtn) {
        approveBtn.disabled = false;
        approveBtn.innerHTML = `<span>Retry Execution</span>`;
      }
      if (rejectBtn) rejectBtn.style.display = 'flex';
    }
  }

  // ==============================================================================
  // User Prompt Submission & Conversational Dispatch
  // ==============================================================================
  async function submitPrompt(promptText) {
    const text = (promptText || '').trim();
    if (!text) return;

    elements.promptInput.value = '';
    appendUserMessage(text);
    const typingBubble = showTypingIndicator();

    try {
      const resp = await fetch('/propose_plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: text, target_folder: 'Download' })
      });

      const data = await resp.json();
      removeTypingIndicator();

      if (!resp.ok) {
        throw new Error(data.error || 'Failed to analyze prompt');
      }

      if (data.type === 'conversation') {
        // Casual greetings or guidance
        appendAssistantMessage(data.message);
      } else if (data.type === 'plan' && data.plan) {
        // Synthesized Action Plan
        appendAssistantMessage(data.message || 'I have analyzed your request and formulated the following Action Plan:');
        renderDiffReview(data.plan);
      } else {
        appendAssistantMessage(data.message || 'I processed your request.');
      }
    } catch (err) {
      removeTypingIndicator();
      appendAssistantMessage(`⚠️ <strong>Bridge Error:</strong> ${err.message}`);
      showToast(`Error: ${err.message}`, 'error');
    }
  }

  // ==============================================================================
  // Event Bindings
  // ==============================================================================
  // 1. Form Submit
  elements.promptForm.addEventListener('submit', (e) => {
    e.preventDefault();
    submitPrompt(elements.promptInput.value);
  });

  // 2. Quick Task Chips Click
  elements.chipsContainer.addEventListener('click', (e) => {
    const chip = e.target.closest('.task-chip');
    if (!chip) return;
    const taskName = chip.dataset.task;
    if (taskName) {
      submitPrompt(taskName);
    }
  });

  // 3. Persistent Undo Button
  elements.undoBtn.addEventListener('click', handleRollback);

  // 4. Seed Fixtures Button
  elements.seedBtn.addEventListener('click', seedDemoFixtures);

  // 5. Copy Base Path Button
  elements.copyPathBtn.addEventListener('click', () => {
    if (!state.baseDir) return;
    navigator.clipboard.writeText(state.baseDir).then(() => {
      showToast('✓ Storage base path copied to clipboard', 'info');
    }).catch(() => {
      showToast(state.baseDir, 'info');
    });
  });

  // ==============================================================================
  // Helper Utilities
  // ==============================================================================
  function escapeHtml(str) {
    if (!str) return '';
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // ==============================================================================
  // Startup Initialization (initApp)
  // ==============================================================================
  async function initApp() {
    const healthData = await fetchHealth();
    if (healthData && healthData.version && healthData.version !== EXPECTED_VERSION) {
      console.warn(`[initApp] Bridge version mismatch: server reported ${healthData.version}, expected ${EXPECTED_VERSION}`);
      const versionEl = document.getElementById('bridge-version');
      if (versionEl) {
        versionEl.textContent = `Android Bridge ${healthData.version}`;
      }
    }
    await fetchUserProfile();
    await fetchRecentBatch();
  }

  initApp();
});
