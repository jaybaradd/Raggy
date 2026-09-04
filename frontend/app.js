/**
 * app.js — Raggy Phase 0 frontend logic
 *
 * Responsibilities:
 *  - Session management (create, list, switch)
 *  - PDF upload with status toast
 *  - Send message → SSE stream → render tokens in real time
 *  - Auto-grow textarea, keyboard shortcuts
 *
 * No frameworks, no bundler. Pure vanilla JS (ES2022).
 * All API calls go to the same origin so no CORS issues in production.
 */

'use strict';

// ── Constants ─────────────────────────────────────────────────────────────────
const API = '';   // same-origin; prefix all paths with /api/...

// ── State ─────────────────────────────────────────────────────────────────────
let currentSessionId = null;
let isStreaming = false;

// ── DOM refs ──────────────────────────────────────────────────────────────────
const newChatBtn     = document.getElementById('newChatBtn');
const sessionList    = document.getElementById('sessionList');
const chatTitle      = document.getElementById('chatTitle');
const messageThread  = document.getElementById('messageThread');
const emptyState     = document.getElementById('emptyState');
const messageInput   = document.getElementById('messageInput');
const sendBtn        = document.getElementById('sendBtn');
const fileInput      = document.getElementById('fileInput');
const uploadStatus   = document.getElementById('uploadStatus');
const uploadStatusIcon = document.getElementById('uploadStatusIcon');
const uploadStatusText = document.getElementById('uploadStatusText');
const toastClose     = document.getElementById('toastClose');

// ── Initialization ────────────────────────────────────────────────────────────
(async function init() {
  await loadSessions();
  bindEvents();
})();

// ── Event bindings ────────────────────────────────────────────────────────────
function bindEvents() {
  newChatBtn.addEventListener('click', createNewSession);
  sendBtn.addEventListener('click', sendMessage);
  toastClose.addEventListener('click', () => { uploadStatus.hidden = true; });

  fileInput.addEventListener('change', () => {
    if (fileInput.files.length > 0) uploadFile(fileInput.files[0]);
  });

  messageInput.addEventListener('input', () => {
    autoGrow(messageInput);
    sendBtn.disabled = messageInput.value.trim() === '' || isStreaming;
  });

  messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!sendBtn.disabled) sendMessage();
    }
  });

  // Suggestion chips
  document.querySelectorAll('.chip').forEach(chip => {
    chip.addEventListener('click', () => {
      messageInput.value = chip.dataset.prompt;
      messageInput.dispatchEvent(new Event('input'));
      sendMessage();
    });
  });
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
  loadSessions();   // refresh active highlight
}

// ── Messages ──────────────────────────────────────────────────────────────────
async function sendMessage() {
  const content = messageInput.value.trim();
  if (!content || isStreaming) return;

  // Ensure we have a session
  if (!currentSessionId) await createNewSession();

  // Render user bubble immediately
  appendBubble('user', content);
  messageInput.value = '';
  messageInput.style.height = 'auto';
  sendBtn.disabled = true;
  isStreaming = true;

  // Hide empty state
  if (emptyState) emptyState.style.display = 'none';

  // Create bot bubble with typing cursor
  const botBubble = appendBubble('bot', '');
  const cursor = document.createElement('span');
  cursor.className = 'cursor';
  botBubble.appendChild(cursor);

  try {
    const res = await fetch(`${API}/api/sessions/${currentSessionId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
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
      buffer = lines.pop();   // keep incomplete line in buffer

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6).trim();
        if (payload === '[DONE]') break;

        try {
          const token = JSON.parse(payload);
          if (token.error) {
            botBubble.textContent = `⚠ Error: ${token.error}`;
            return;
          }
          botText += token;
          // Render text without the cursor node, then re-add cursor at end
          botBubble.textContent = botText;
          botBubble.appendChild(cursor);
          scrollToBottom();
        } catch (_) { /* non-JSON line — skip */ }
      }
    }

    // Remove cursor on completion
    cursor.remove();
    scrollToBottom();

  } catch (err) {
    cursor.remove();
    botBubble.textContent = `⚠ Network error: ${err.message}`;
  } finally {
    isStreaming = false;
    sendBtn.disabled = messageInput.value.trim() === '';
  }
}

// ── Upload ────────────────────────────────────────────────────────────────────
async function uploadFile(file) {
  showToast('⏳', `Uploading ${file.name}…`);

  const formData = new FormData();
  formData.append('file', file);

  // Reset file input so the same file can be re-uploaded if needed
  fileInput.value = '';

  try {
    const res = await fetch(`${API}/api/documents`, {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();

    if (!res.ok) {
      showToast('❌', `Upload failed: ${data.detail || 'Unknown error'}`);
      return;
    }

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
    // Re-bind chips on the cloned node
    clone.querySelectorAll('.chip').forEach(chip => {
      chip.addEventListener('click', () => {
        messageInput.value = chip.dataset.prompt;
        messageInput.dispatchEvent(new Event('input'));
        sendMessage();
      });
    });
  }
}

function scrollToBottom() {
  messageThread.scrollTop = messageThread.scrollHeight;
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 180) + 'px';
}

function showToast(icon, text) {
  uploadStatusIcon.textContent = icon;
  uploadStatusText.textContent = text;
  uploadStatus.hidden = false;
}
