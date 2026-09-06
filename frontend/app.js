'use strict';

const API = '';

// ── State ─────────────────────────────────────────────────────────────────────
let currentSessionId = null;
let isStreaming = false;
let pendingFile = null;       // File object waiting to be uploaded on send
let detectedYtUrl = null;     // YouTube URL detected in textarea

// ── DOM refs ──────────────────────────────────────────────────────────────────
const newChatBtn         = document.getElementById('newChatBtn');
const sessionList        = document.getElementById('sessionList');
const chatTitle          = document.getElementById('chatTitle');
const messageThread      = document.getElementById('messageThread');
const emptyState         = document.getElementById('emptyState');
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
const ytPrompt           = document.getElementById('ytPrompt');
const ytConfirm          = document.getElementById('ytConfirm');
const ytDismiss          = document.getElementById('ytDismiss');

// ── Init ──────────────────────────────────────────────────────────────────────
(async function init() {
  await loadSessions();
  bindEvents();
})();

// ── Events ────────────────────────────────────────────────────────────────────
function bindEvents() {
  newChatBtn.addEventListener('click', createNewSession);
  sendBtn.addEventListener('click', sendMessage);
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
    showToast('✅', `Ingested ${data.chunk_count} transcript chunks.`);
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
  sessions.forEach(session => {
    const el = document.createElement('div');
    el.className = 'session-item' + (session.session_id === currentSessionId ? ' active' : '');
    el.textContent = session.title;
    el.dataset.id = session.session_id;
    el.addEventListener('click', () => switchSession(session.session_id, session.title));
    sessionList.appendChild(el);
  });
}

async function createNewSession() {
  try {
    const res = await fetch(`${API}/api/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'New chat' }),
    });
    const session = await res.json();
    currentSessionId = session.session_id;
    chatTitle.textContent = session.title;
    clearThread();
    await loadSessions();
  } catch (err) {
    console.error('Failed to create session:', err);
  }
}

function switchSession(sessionId, title) {
  currentSessionId = sessionId;
  chatTitle.textContent = title;
  clearThread();
  loadSessions();
}

// ── Send message ──────────────────────────────────────────────────────────────
async function sendMessage() {
  const content = messageInput.value.trim();
  if ((!content && !pendingFile) || isStreaming) return;
  if (!currentSessionId) await createNewSession();

  // Parse the attached file inline — don't ingest into Qdrant yet.
  // The parsed text becomes inline_context for this chat turn.
  let inlineContext = '';
  if (pendingFile) {
    const file = pendingFile;
    clearPendingFile();
    inlineContext = await parseFile(file);
  }

  if (!content) return;   // file-only send with no text — nothing to ask

  appendBubble('user', content);
  messageInput.value = '';
  messageInput.style.height = 'auto';
  isStreaming = true;
  updateSendBtn();

  if (emptyState) emptyState.style.display = 'none';

  const botBubble = appendBubble('bot', '');
  const cursor = document.createElement('span');
  cursor.className = 'cursor';
  botBubble.appendChild(cursor);

  try {
    const res = await fetch(`${API}/api/sessions/${currentSessionId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, inline_context: inlineContext }),
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
          const token = JSON.parse(payload);
          if (token.error) { botBubble.textContent = `⚠ Error: ${token.error}`; return; }
          botText += token;
          botBubble.textContent = botText;
          botBubble.appendChild(cursor);
          scrollToBottom();
        } catch (_) { /* non-JSON line */ }
      }
    }
    cursor.remove();
    scrollToBottom();

  } catch (err) {
    cursor.remove();
    botBubble.textContent = `⚠ Network error: ${err.message}`;
  } finally {
    isStreaming = false;
    updateSendBtn();
  }
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
    if (!res.ok) { showToast('❌', `Upload failed: ${data.detail || 'Unknown error'}`); return; }
    showToast('✅', `Indexed ${data.chunk_count} chunks from "${file.name}"`);
    setTimeout(() => { uploadStatus.hidden = true; }, 5000);
  } catch (err) {
    showToast('❌', `Upload error: ${err.message}`);
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function appendBubble(role, text) {
  const row = document.createElement('div');
  row.className = `message-row ${role}`;
  const avatar = document.createElement('div');
  avatar.className = `avatar ${role}`;
  avatar.textContent = role === 'user' ? 'U' : '⬡';
  const bubble = document.createElement('div');
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;
  row.appendChild(avatar);
  row.appendChild(bubble);
  messageThread.appendChild(row);
  scrollToBottom();
  return bubble;
}

function clearThread() {
  messageThread.innerHTML = '';
  if (emptyState) {
    const clone = emptyState.cloneNode(true);
    clone.style.display = '';
    messageThread.appendChild(clone);
    clone.querySelectorAll('.chip').forEach(chip => {
      chip.addEventListener('click', () => {
        messageInput.value = chip.dataset.prompt;
        messageInput.dispatchEvent(new Event('input'));
        sendMessage();
      });
    });
  }
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
