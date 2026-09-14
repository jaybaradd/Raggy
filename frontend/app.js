'use strict';

const API = '';

// ── State ─────────────────────────────────────────────────────────────────────
let currentSessionId = null;
let currentProjectId = null;
let isStreaming = false;
let pendingFile = null;       // File object waiting to be uploaded on send
let detectedYtUrl = null;     // YouTube URL detected in textarea
const shownConflictIds = new Set();

// ── DOM refs ──────────────────────────────────────────────────────────────────
const newChatBtn         = document.getElementById('newChatBtn');
const sessionList        = document.getElementById('sessionList');
const chatTitle          = document.getElementById('chatTitle');
const projectScopeInput  = document.getElementById('projectScopeInput');
const messageThread      = document.getElementById('messageThread');
let emptyState           = document.getElementById('emptyState');
const emptyStateTemplate = emptyState.cloneNode(true);
const messageInput       = document.getElementById('messageInput');
const sendBtn            = document.getElementById('sendBtn');
const fileInput          = document.getElementById('fileInput');
const uploadStatus       = document.getElementById('uploadStatus');
const uploadStatusIcon   = document.getElementById('uploadStatusIcon');
const uploadStatusText   = document.getElementById('uploadStatusText');
const toastClose         = document.getElementById('toastClose');
const attachmentPreview  = document.getElementById('attachmentPreview');
const attachmentName     = document.getElementById('attachmentName');
const attachmentIcon     = document.getElementById('attachmentIcon');
const attachmentRemove   = document.getElementById('attachmentRemove');
const indexFileCheckbox  = document.getElementById('indexFileCheckbox');
const knowledgeBaseToggle = document.getElementById('knowledgeBaseToggle');
const ytPrompt           = document.getElementById('ytPrompt');
const ytConfirm          = document.getElementById('ytConfirm');
const ytDismiss          = document.getElementById('ytDismiss');
const memoryBrowserBtn   = document.getElementById('memoryBrowserBtn');
const memoryDrawer       = document.getElementById('memoryDrawer');
const memoryDrawerClose  = document.getElementById('memoryDrawerClose');
const memoryDrawerProject = document.getElementById('memoryDrawerProject');
const memoryStatusFilter = document.getElementById('memoryStatusFilter');
const memoryScopeFilter  = document.getElementById('memoryScopeFilter');
const memoryBrowserList  = document.getElementById('memoryBrowserList');
const memoryBrowserDetail = document.getElementById('memoryBrowserDetail');

// ── Init ──────────────────────────────────────────────────────────────────────
(async function init() {
  await loadSessions();
  bindEvents();
})();

// ── Events ────────────────────────────────────────────────────────────────────
function bindEvents() {
  newChatBtn.addEventListener('click', createNewSession);
  projectScopeInput.addEventListener('change', saveCurrentSessionProject);
  sendBtn.addEventListener('click', sendMessage);
  memoryBrowserBtn.addEventListener('click', openMemoryBrowser);
  memoryDrawerClose.addEventListener('click', () => { memoryDrawer.hidden = true; });
  memoryStatusFilter.addEventListener('change', loadBrowserMemories);
  memoryScopeFilter.addEventListener('change', loadBrowserMemories);
  toastClose.addEventListener('click', () => { uploadStatus.hidden = true; });

  // Attachment file picker
  fileInput.addEventListener('change', () => {
    if (fileInput.files.length > 0) setPendingFile(fileInput.files[0]);
    fileInput.value = '';
  });

  attachmentRemove.addEventListener('click', clearPendingFile);

  // YouTube prompt buttons
  ytConfirm.addEventListener('click', () => ingestYouTube(detectedYtUrl));
  ytDismiss.addEventListener('click', () => { ytPrompt.hidden = true; detectedYtUrl = null; });

  messageInput.addEventListener('input', () => {
    autoGrow(messageInput);
    checkForYouTubeUrl(messageInput.value);
    updateSendBtn();
  });

  messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!sendBtn.disabled) sendMessage();
    }
  });

  document.querySelectorAll('.chip').forEach(chip => {
    chip.addEventListener('click', () => {
      messageInput.value = chip.dataset.prompt;
      messageInput.dispatchEvent(new Event('input'));
      sendMessage();
    });
  });
}

// ── Memory browser ──────────────────────────────────────────────────────────
async function openMemoryBrowser() {
  const project = projectScopeInput.value.trim();
  memoryDrawerProject.textContent = project ? ` · ${project}` : ' · all memories';
  memoryDrawer.hidden = false;
  memoryBrowserDetail.innerHTML = '<p>Select a memory to inspect its details and history.</p>';
  await loadBrowserMemories();
}

async function loadBrowserMemories() {
  const params = new URLSearchParams({ status: memoryStatusFilter.value, limit: '100' });
  if (memoryScopeFilter.value) params.set('scope', memoryScopeFilter.value);
  const project = projectScopeInput.value.trim();
  if (currentProjectId) params.set('project_id', currentProjectId);
  else if (project) params.set('project_scope', project);
  memoryBrowserList.innerHTML = '<p class="memory-browser-empty">Loading…</p>';
  try {
    const res = await fetch(`${API}/api/memories?${params}`);
    const data = await res.json();
    const memories = data.memories || [];
    memoryBrowserList.innerHTML = '';
    if (!memories.length) {
      memoryBrowserList.innerHTML = '<p class="memory-browser-empty">No memories match these filters.</p>';
      return;
    }
    memories.forEach(memory => {
      const row = document.createElement('button');
      row.className = `memory-browser-row status-${memory.status}`;
      const summary = memory.payload?.summary || memory.payload?.preferred_behavior ||
        memory.payload?.subject || memory.payload?.canonical_name || memory.memory_id;
      row.innerHTML = `<span class="memory-browser-row-title"></span><span class="memory-browser-row-meta"></span>`;
      row.querySelector('.memory-browser-row-title').textContent = summary;
      row.querySelector('.memory-browser-row-meta').textContent = `${memory.kind} · ${memory.status}`;
      row.addEventListener('click', () => showMemoryDetail(memory.memory_id));
      memoryBrowserList.appendChild(row);
    });
  } catch (err) {
    memoryBrowserList.innerHTML = `<p class="memory-browser-empty">Could not load memories: ${err.message}</p>`;
  }
}

async function showMemoryDetail(memoryId) {
  memoryBrowserDetail.innerHTML = '<p>Loading memory details…</p>';
  try {
    const [memoryRes, auditRes, accessRes] = await Promise.all([
      fetch(`${API}/api/memories/${memoryId}`),
      fetch(`${API}/api/memories/${memoryId}/audit`),
      fetch(`${API}/api/memories/${memoryId}/access-events`),
    ]);
    if (!memoryRes.ok) throw new Error('Memory is unavailable');
    const memory = await memoryRes.json();
    const audit = auditRes.ok ? (await auditRes.json()).events || [] : [];
    const access = accessRes.ok ? (await accessRes.json()).events || [] : [];
    renderMemoryDetail(memory, audit, access);
  } catch (err) {
    memoryBrowserDetail.innerHTML = `<p>Could not load details: ${err.message}</p>`;
  }
}

function renderMemoryDetail(memory, audit, access) {
  memoryBrowserDetail.innerHTML = '';
  const title = document.createElement('h3');
  title.textContent = `${memory.kind} · ${memory.status}`;
  const metadata = document.createElement('p');
  metadata.className = 'memory-detail-meta';
  metadata.textContent = `${memory.scope}${memory.project_scope ? ` · ${memory.project_scope}` : ''} · expires ${memory.valid_to || 'never'}`;
  const payload = document.createElement('pre');
  payload.className = 'memory-detail-payload';
  payload.textContent = JSON.stringify(memory.payload, null, 2);
  const actions = document.createElement('div');
  actions.className = 'memory-detail-actions';
  addMemoryActions(actions, memory);
  const timeline = document.createElement('div');
  timeline.className = 'memory-detail-timeline';
  timeline.innerHTML = '<h4>Audit history</h4>';
  if (!audit.length) timeline.append(Object.assign(document.createElement('p'), { textContent: 'No audit events.' }));
  audit.forEach(event => {
    const item = document.createElement('p');
    item.textContent = `${event.event_type} · ${event.actor_id || 'system'} · ${event.created_at}`;
    timeline.appendChild(item);
  });
  const usage = document.createElement('p');
  usage.className = 'memory-detail-usage';
  usage.textContent = `Retrieved/injected ${access.length} time${access.length === 1 ? '' : 's'}.`;
  memoryBrowserDetail.append(title, metadata, payload, actions, timeline, usage);
}

function addMemoryActions(actions, memory) {
  const action = (label, method, suffix = '', body = null, confirmText = '') => {
    const button = document.createElement('button');
    button.textContent = label;
    button.addEventListener('click', async () => {
      if (confirmText && !window.confirm(confirmText)) return;
      try {
        const res = await fetch(`${API}/api/memories/${memory.memory_id}${suffix}`, {
          method, headers: body ? { 'Content-Type': 'application/json' } : {},
          body: body ? JSON.stringify(body) : undefined,
        });
        if (!res.ok) throw new Error((await res.json()).detail || 'Action failed');
        await loadBrowserMemories();
        await showMemoryDetail(memory.memory_id);
      } catch (err) { showToast('❌', err.message); }
    });
    actions.appendChild(button);
  };
  if (memory.status === 'candidate') {
    action('Confirm', 'POST', '/confirm');
    action('Reject', 'POST', '/reject');
  }
  if (memory.status === 'active') action('Expire', 'POST', '/expire', null, 'Expire this memory?');
  if (memory.scope === 'session' && projectScopeInput.value.trim()) {
    action('Promote to project', 'POST', '/promote', { scope: 'project', project_scope: projectScopeInput.value.trim() });
  }
  if (memory.status !== 'deleted') action('Forget', 'DELETE', '', null, 'Forget this memory? It will remain in the audit trail.');
  if (['deleted', 'expired', 'superseded'].includes(memory.status)) return;
  const edit = document.createElement('button');
  edit.textContent = 'Edit payload';
  edit.addEventListener('click', async () => {
    const text = window.prompt('Edit the JSON payload:', JSON.stringify(memory.payload, null, 2));
    if (text === null) return;
    try {
      const res = await fetch(`${API}/api/memories/${memory.memory_id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: JSON.parse(text) }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || 'Edit failed');
      await loadBrowserMemories();
      await showMemoryDetail(memory.memory_id);
    } catch (err) { showToast('❌', `Edit failed: ${err.message}`); }
  });
  actions.appendChild(edit);
}

// ── YouTube URL detection ─────────────────────────────────────────────────────
const YT_REGEX = /(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)[\w-]+/;

function checkForYouTubeUrl(text) {
  const match = text.match(YT_REGEX);
  if (match) {
    detectedYtUrl = match[0].startsWith('http') ? match[0] : 'https://' + match[0];
    ytPrompt.hidden = false;
  } else {
    if (!ytPrompt.hidden) ytPrompt.hidden = true;
    detectedYtUrl = null;
  }
}

async function ingestYouTube(url) {
  ytPrompt.hidden = true;
  detectedYtUrl = null;
  // Clean URL from input box if present
  if (url && messageInput.value.includes(url)) {
    messageInput.value = messageInput.value.replace(url, '').trim();
    autoGrow(messageInput);
    updateSendBtn();
  }
  showToast('⏳', `Ingesting YouTube video…`);
  try {
    const res = await fetch(`${API}/api/documents/youtube`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) { showToast('❌', `Failed: ${data.detail || 'Unknown error'}`); return; }
    const status = data.status === 'processing'
      ? await waitForDocument(data.doc_id, 'YouTube transcript')
      : data;
    if (status.status === 'error') {
      showToast('❌', `Ingestion failed: ${status.message || 'Unknown error'}`);
      return;
    }
    showToast('✅', `Ingested ${status.chunk_count} transcript chunks.`);
    setTimeout(() => { uploadStatus.hidden = true; }, 5000);
  } catch (err) {
    showToast('❌', `Error: ${err.message}`);
  }
}

// ── Attachment management ─────────────────────────────────────────────────────
const MODALITY_ICONS = {
  pdf: '📄', docx: '📝', pptx: '📊', xlsx: '📊', csv: '📊',
  jpg: '🖼️', jpeg: '🖼️', png: '🖼️', webp: '🖼️', gif: '🖼️',
  mp4: '🎬', mov: '🎬', avi: '🎬', webm: '🎬',
};

function setPendingFile(file) {
  pendingFile = file;
  const ext = file.name.split('.').pop().toLowerCase();
  attachmentIcon.textContent = MODALITY_ICONS[ext] || '📎';
  attachmentName.textContent = file.name;
  attachmentPreview.hidden = false;
  updateSendBtn();
}

function clearPendingFile() {
  pendingFile = null;
  indexFileCheckbox.checked = false;
  attachmentPreview.hidden = true;
  updateSendBtn();
}

function updateSendBtn() {
  const hasText = messageInput.value.trim() !== '';
  sendBtn.disabled = (!hasText && !pendingFile) || isStreaming;
}

// ── Sessions ──────────────────────────────────────────────────────────────────
async function loadSessions() {
  try {
    const res = await fetch(`${API}/api/sessions`);
    const data = await res.json();
    renderSessionList(data.sessions || []);
  } catch (err) {
    console.error('Failed to load sessions:', err);
  }
}

function renderSessionList(sessions) {
  sessionList.innerHTML = '';
  if (sessions.length === 0) {
    sessionList.innerHTML = `<p style="color:var(--text-muted);font-size:12px;padding:8px 12px;">No sessions yet</p>`;
    return;
  }
  const groups = new Map();
  sessions.forEach(session => {
    const project = session.project_scope || '';
    if (!groups.has(project)) groups.set(project, []);
    groups.get(project).push(session);
  });

  [...groups.entries()]
    .sort(([a], [b]) => (a || 'zzzz').localeCompare(b || 'zzzz'))
    .forEach(([project, projectSessions]) => {
      const heading = document.createElement('div');
      heading.className = 'project-group-heading';
      heading.textContent = project ? `Project · ${project}` : 'Personal chats';
      sessionList.appendChild(heading);

      projectSessions.forEach(session => {
        const el = document.createElement('div');
        el.className = 'session-item' + (session.session_id === currentSessionId ? ' active' : '');
        el.textContent = session.title;
        el.dataset.id = session.session_id;
        el.addEventListener('click', () => switchSession(session.session_id, session.title, session.project_scope, session.project_id));
        sessionList.appendChild(el);
      });
    });
}

async function createNewSession() {
  try {
    const res = await fetch(`${API}/api/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: 'New chat',
        project_scope: projectScopeInput.value.trim() || null,
        project_id: currentProjectId,
      }),
    });
    const session = await res.json();
    currentSessionId = session.session_id;
    currentProjectId = session.project_id || null;
    chatTitle.textContent = session.project_scope
      ? `${session.title} · ${session.project_scope}`
      : session.title;
    clearThread();
    await loadSessions();
  } catch (err) {
    console.error('Failed to create session:', err);
  }
}

async function switchSession(sessionId, title, projectScope, projectId = null) {
  currentSessionId = sessionId;
  currentProjectId = projectId;
  projectScopeInput.value = projectScope || '';
  chatTitle.textContent = projectScope ? `${title} · ${projectScope}` : title;
  clearThread();
  await Promise.all([loadSessionMessages(sessionId), loadSessions()]);
}

async function loadSessionMessages(sessionId) {
  try {
    const res = await fetch(`${API}/api/sessions/${sessionId}/messages`);
    if (!res.ok) throw new Error('Could not load chat history');
    const data = await res.json();
    // A user can click another chat while this request is in flight.
    if (currentSessionId !== sessionId) return;

    const messages = data.messages || [];
    if (messages.length === 0) {
      clearThread();
      return;
    }
    messageThread.innerHTML = '';
    emptyState = null;
    messages.forEach(message => {
      appendBubble(message.role === 'assistant' ? 'bot' : 'user', message.content, message.attachments || []);
    });
    scrollToBottom();
  } catch (err) {
    console.error('Failed to load chat history:', err);
    if (currentSessionId === sessionId) clearThread();
  }
}

async function saveCurrentSessionProject() {
  if (!currentSessionId) return;
  const projectScope = projectScopeInput.value.trim() || null;
  // Typing a new name intentionally lets the backend resolve-or-create it.
  currentProjectId = null;
  try {
    const res = await fetch(`${API}/api/sessions/${currentSessionId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_scope: projectScope, project_id: currentProjectId }),
    });
    if (!res.ok) throw new Error('Could not update project');
    const session = await res.json();
    projectScopeInput.value = session.project_scope || '';
    currentProjectId = session.project_id || null;
    chatTitle.textContent = session.project_scope
      ? `${session.title} · ${session.project_scope}`
      : session.title;
    await loadSessions();
  } catch (err) {
    console.error('Failed to update project:', err);
  }
}

// ── Send message ──────────────────────────────────────────────────────────────
async function sendMessage() {
  const content = messageInput.value.trim();
  if ((!content && !pendingFile) || isStreaming) return;
  if (!currentSessionId) await createNewSession();

  // Attachments can be used once (inline) or saved to the knowledge base as
  // well. Inline context keeps the current question fast and grounded; the
  // optional upload makes the document available to future retrievals.
  let inlineContext = '';
  let attachments = [];
  if (pendingFile) {
    const file = pendingFile;
    const saveToKnowledgeBase = indexFileCheckbox.checked;
    clearPendingFile();
    if (saveToKnowledgeBase) {
      // Docling uses native conversion components that can crash when the
      // same document is parsed concurrently by /documents and /documents/parse.
      // The indexed chunks will also serve the current question, so there is
      // no need to parse the file a second time for inline context.
      const indexed = await uploadFile(file);
      if (indexed) {
        knowledgeBaseToggle.checked = true;
        attachments = [buildAttachmentMetadata(file, 'knowledge_base', { document_id: indexed.doc_id })];
      } else {
        inlineContext = await parseFile(file);
        attachments = [buildAttachmentMetadata(file, 'inline')];
      }
    } else {
      inlineContext = await parseFile(file);
      attachments = [buildAttachmentMetadata(file, 'inline')];
    }
  }

  if (!content) return;   // file-only send with no text — nothing to ask

  appendBubble('user', content, attachments);
  messageInput.value = '';
  messageInput.style.height = 'auto';
  isStreaming = true;
  updateSendBtn();

  if (emptyState) emptyState.style.display = 'none';

  const botBubble = appendBubble('bot', '');
  const requestStartedAt = Date.now();
  const cursor = document.createElement('span');
  cursor.className = 'cursor';
  botBubble.appendChild(cursor);

  try {
    const res = await fetch(`${API}/api/sessions/${currentSessionId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content,
        inline_context: inlineContext,
        use_knowledge_base: knowledgeBaseToggle.checked,
        attachments,
      }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
      botBubble.textContent = `⚠ Error: ${err.detail}`;
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let botText = '';
    let sourceMetadata = [];
    let memoryMetadata = [];

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6).trim();
        if (payload === '[DONE]') break;
        try {
          const parsed = JSON.parse(payload);
          if (parsed && parsed.type === 'sources') {
            sourceMetadata = parsed.sources || [];
            continue;
          }
          if (parsed && parsed.type === 'memories') {
            memoryMetadata = parsed.memories || [];
            continue;
          }
          if (parsed && parsed.error) { botBubble.textContent = `⚠ Error: ${parsed.error}`; return; }
          const token = typeof parsed === 'string' ? parsed : (parsed.token || '');
          botText += token;
          botBubble.textContent = botText;
          botBubble.appendChild(cursor);
          scrollToBottom();
        } catch (_) { /* non-JSON line */ }
      }
    }
    cursor.remove();
    renderSources(botBubble, sourceMetadata);
    renderMemories(botBubble, memoryMetadata);
    scheduleConflictCheck(botBubble, currentProjectId, projectScopeInput.value.trim() || null);
    scrollToBottom();

  } catch (err) {
    cursor.remove();
    botBubble.textContent = `⚠ Network error: ${err.message}`;
  } finally {
    isStreaming = false;
    updateSendBtn();
  }
}

function scheduleConflictCheck(botBubble, projectId, projectScope) {
  // Extraction runs after the streamed reply, so retry briefly rather than
  // making the user send another message or open a terminal.
  [0, 1500, 4000, 8000].forEach(delay => {
    setTimeout(() => loadOpenConflicts(botBubble, projectId, projectScope), delay);
  });
}

async function loadOpenConflicts(botBubble, projectId, projectScope) {
  try {
    const params = new URLSearchParams();
    if (projectId) params.set('project_id', projectId);
    else if (projectScope) params.set('project_scope', projectScope);
    const res = await fetch(`${API}/api/memories/conflicts?${params}`);
    if (!res.ok) return;
    const data = await res.json();
    (data.conflicts || [])
      .filter(conflict => !shownConflictIds.has(conflict.conflict_id))
      .forEach(conflict => {
        shownConflictIds.add(conflict.conflict_id);
        renderConflictReview(botBubble, conflict);
      });
  } catch (err) {
    console.error('Failed to load memory conflicts:', err);
  }
}

function renderConflictReview(botBubble, conflict) {
  const temporal = conflict.details?.differences?.temporal_scope;
  const existingValue = temporal?.existing || 'the existing event';
  const incomingValue = temporal?.incoming || 'the update';
  const card = document.createElement('div');
  card.className = 'conflict-review-card';

  const title = document.createElement('div');
  title.className = 'conflict-review-title';
  title.textContent = 'Memory update needs confirmation';
  const summary = document.createElement('div');
  summary.className = 'conflict-review-summary';
  summary.textContent = `The existing event says ${existingValue}; your update says ${incomingValue}.`;
  const actions = document.createElement('div');
  actions.className = 'conflict-review-actions';

  const accept = document.createElement('button');
  accept.className = 'conflict-action primary';
  accept.textContent = `Accept ${incomingValue}`;
  accept.addEventListener('click', () => resolveConflict(conflict, 'supersede_existing', card, actions));
  const keep = document.createElement('button');
  keep.className = 'conflict-action';
  keep.textContent = `Keep ${existingValue}`;
  keep.addEventListener('click', () => resolveConflict(conflict, 'keep_existing', card, actions));
  const later = document.createElement('button');
  later.className = 'conflict-action quiet';
  later.textContent = 'Review later';
  later.addEventListener('click', () => card.remove());
  actions.append(accept, keep, later);
  card.append(title, summary, actions);
  botBubble.appendChild(card);
  scrollToBottom();
}

async function resolveConflict(conflict, action, card, actions) {
  actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
  try {
    const res = await fetch(`${API}/api/memories/conflicts/${conflict.conflict_id}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    if (!res.ok) {
      const error = await res.json().catch(() => ({}));
      throw new Error(error.detail || 'Could not resolve this memory update');
    }
    card.classList.add('resolved');
    actions.textContent = action === 'keep_existing'
      ? 'Kept the existing memory.'
      : 'Accepted the update. Future responses will use it.';
  } catch (err) {
    actions.textContent = `Could not resolve: ${err.message}`;
    actions.classList.add('error');
  }
}

function renderMemories(botBubble, memories) {
  if (!memories || memories.length === 0) return;
  const container = document.createElement('div');
  container.className = 'source-list memory-list';
  const heading = document.createElement('div');
  heading.className = 'source-list-heading';
  heading.textContent = 'Memory used';
  container.appendChild(heading);
  memories.forEach((memory) => {
    const card = document.createElement('div');
    card.className = 'source-card memory-card';
    const title = document.createElement('div');
    title.className = 'source-card-title';
    title.textContent = `${memory.kind || 'memory'} · ${memory.scope || 'session'}`;
    const details = document.createElement('div');
    details.className = 'source-card-details';
    details.textContent = `confidence ${(Number(memory.confidence || 0) * 100).toFixed(0)}% · ${memory.memory_id || ''}`;
    card.append(title, details);
    container.appendChild(card);
  });
  botBubble.appendChild(container);
}

// ── File helpers ──────────────────────────────────────────────────────────────

/** Parse a file inline and return its text — does NOT store in Qdrant. */
async function parseFile(file) {
  showToast('⏳', `Reading ${file.name}…`);
  const formData = new FormData();
  formData.append('file', file);
  try {
    const res = await fetch(`${API}/api/documents/parse`, { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) {
      showToast('❌', `Parse failed: ${data.detail || 'Unknown error'}`);
      return '';
    }
    showToast('✅', `Read "${file.name}" — asking about it now…`);
    setTimeout(() => { uploadStatus.hidden = true; }, 3000);
    return data.parsed_text || '';
  } catch (err) {
    showToast('❌', `Parse error: ${err.message}`);
    return '';
  }
}

/** Upload a file into the knowledge base (Qdrant). Used for explicit KB ingestion. */
async function uploadFile(file) {
  showToast('⏳', `Uploading ${file.name} to knowledge base…`);
  const formData = new FormData();
  formData.append('file', file);
  try {
    const res = await fetch(`${API}/api/documents`, { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) { showToast('❌', `Upload failed: ${data.detail || 'Unknown error'}`); return null; }
    const status = data.status === 'processing'
      ? await waitForDocument(data.doc_id, file.name)
      : data;
    if (status.status === 'error') {
      showToast('❌', `Indexing failed: ${status.message || 'Unknown error'}`);
      return null;
    }
    showToast('✅', `Indexed ${status.chunk_count} chunks from "${file.name}"`);
    setTimeout(() => { uploadStatus.hidden = true; }, 5000);
    return status;
  } catch (err) {
    showToast('❌', `Upload error: ${err.message}`);
    return null;
  }
}

/** Poll the asynchronous ingestion job until it reaches a terminal state. */
async function waitForDocument(docId, label) {
  const maxAttempts = 1200; // 20 minutes at one request per second
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    const res = await fetch(`${API}/api/documents/${docId}/status`);
    const status = await res.json();
    if (!res.ok) throw new Error(status.detail || `Could not read ${label} status`);
    if (status.status === 'processing') {
      showToast('⏳', `${label}: still processing…`);
      continue;
    }
    return status;
  }
  throw new Error(`${label} ingestion timed out`);
}

function renderSources(botBubble, sources) {
  if (!sources || sources.length === 0) return;

  const container = document.createElement('div');
  container.className = 'source-list';
  const heading = document.createElement('div');
  heading.className = 'source-list-heading';
  heading.textContent = 'Sources';
  container.appendChild(heading);

  sources.forEach(source => {
    const card = document.createElement('div');
    card.className = 'source-card';
    const title = document.createElement('span');
    title.className = 'source-card-title';
    title.textContent = `[${source.source_index}] ${source.filename || source.doc_id || 'Source'}`;
    card.appendChild(title);

    const details = document.createElement('span');
    details.className = 'source-card-details';
    details.textContent = [
      source.representation,
      source.page ? `page ${source.page}` : '',
      source.cell_range || '',
      source.time_range ? formatTimeRange(source.time_range) : '',
      source.bbox ? 'image region' : '',
    ].filter(Boolean).join(' · ');
    card.appendChild(details);

    if (source.source_url) {
      const link = document.createElement('a');
      link.href = source.source_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'open';
      card.appendChild(link);
    }
    container.appendChild(card);
  });
  botBubble.appendChild(container);
}

function formatTimeRange(range) {
  if (!Array.isArray(range) || range.length < 2) return '';
  return `${formatSeconds(range[0])}–${formatSeconds(range[1])}`;
}

function formatSeconds(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return '';
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.floor(seconds % 60).toString().padStart(2, '0');
  return `${minutes}:${remainder}`;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function attachmentId() {
  return globalThis.crypto?.randomUUID?.() ||
    '00000000-0000-4000-8000-000000000000'.replace(/[018]/g, value =>
      (Number(value) ^ (Math.random() * 16 >> Number(value) / 4)).toString(16));
}

function buildAttachmentMetadata(file, sourceMode, references = {}) {
  return {
    attachment_id: attachmentId(),
    filename: file.name || 'attachment',
    mime_type: file.type || 'application/octet-stream',
    size_bytes: Number(file.size) || 0,
    source_mode: sourceMode,
    ...references,
  };
}

function attachmentIconFor(filename) {
  const ext = String(filename || '').split('.').pop().toLowerCase();
  return MODALITY_ICONS[ext] || '📎';
}

function renderAttachmentChips(bubble, attachments) {
  if (!attachments?.length) return;
  const container = document.createElement('div');
  container.className = 'message-attachments';
  attachments.forEach(attachment => {
    const chip = document.createElement('span');
    chip.className = 'message-attachment-chip';
    const mode = attachment.source_mode === 'knowledge_base' ? 'saved to knowledge base' : 'inline';
    chip.textContent = `${attachmentIconFor(attachment.filename)} ${attachment.filename} · ${mode}`;
    chip.title = `${attachment.mime_type || 'unknown type'} · ${attachment.size_bytes ?? 0} bytes`;
    container.appendChild(chip);
  });
  bubble.appendChild(container);
}

function appendBubble(role, text, attachments = []) {
  const row = document.createElement('div');
  row.className = `message-row ${role}`;
  const avatar = document.createElement('div');
  avatar.className = `avatar ${role}`;
  avatar.textContent = role === 'user' ? 'U' : '⬡';
  const bubble = document.createElement('div');
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;
  renderAttachmentChips(bubble, attachments);
  row.appendChild(avatar);
  row.appendChild(bubble);
  messageThread.appendChild(row);
  scrollToBottom();
  return bubble;
}

function clearThread() {
  messageThread.innerHTML = '';
  const clone = emptyStateTemplate.cloneNode(true);
  clone.style.display = '';
  messageThread.appendChild(clone);
  emptyState = clone;
  clone.querySelectorAll('.chip').forEach(chip => {
    chip.addEventListener('click', () => {
      messageInput.value = chip.dataset.prompt;
      messageInput.dispatchEvent(new Event('input'));
      sendMessage();
    });
  });
}

function scrollToBottom() { messageThread.scrollTop = messageThread.scrollHeight; }

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 180) + 'px';
}

function showToast(icon, text) {
  uploadStatusIcon.textContent = icon;
  uploadStatusText.textContent = text;
  uploadStatus.hidden = false;
}
