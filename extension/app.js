const PROTOCOL_VERSION = 1;
const CHUNK_BYTES = 384 * 1024;
const MAX_VISIBLE_FILES = 100;

const elements = Object.fromEntries(
  [...document.querySelectorAll('[id]')].map((element) => [element.id, element])
);

const state = {
  port: null,
  pending: new Map(),
  files: [],
  totalBytes: 0,
  uploadedBytes: 0,
  activeFileId: null,
  cancelled: false,
  hostReady: false,
  cloudflared: null,
  session: null,
  publicUrl: null,
  logs: [],
  bridgeReconnectTimer: null,
};

connectBridge();
bindEvents();

function connectBridge() {
  clearTimeout(state.bridgeReconnectTimer);
  state.bridgeReconnectTimer = null;
  state.port = chrome.runtime.connect({ name: 'localdrop-app' });
  state.port.onMessage.addListener(onBridgeMessage);
  state.port.onDisconnect.addListener(() => {
    state.port = null;
    state.hostReady = false;
    setHostState('', 'Reconnecting extension bridge…');
    rejectAllPending('Extension bridge disconnected');
    state.bridgeReconnectTimer = setTimeout(connectBridge, 500);
  });
  setTimeout(checkHost, 30);
}

function bindEvents() {
  elements.chooseFiles.addEventListener('click', (event) => { event.stopPropagation(); elements.fileInput.click(); });
  elements.chooseFolder.addEventListener('click', (event) => { event.stopPropagation(); elements.folderInput.click(); });
  elements.fileInput.addEventListener('change', () => setFiles([...elements.fileInput.files].map((file) => ({ file, path: file.name }))));
  elements.folderInput.addEventListener('change', () => setFiles([...elements.folderInput.files].map((file) => ({ file, path: file.webkitRelativePath || file.name }))));
  elements.dropZone.addEventListener('click', () => elements.fileInput.click());
  elements.dropZone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); elements.fileInput.click(); }
  });
  for (const type of ['dragenter', 'dragover']) {
    elements.dropZone.addEventListener(type, (event) => { event.preventDefault(); elements.dropZone.classList.add('dragging'); });
  }
  for (const type of ['dragleave', 'drop']) {
    elements.dropZone.addEventListener(type, (event) => { event.preventDefault(); elements.dropZone.classList.remove('dragging'); });
  }
  elements.dropZone.addEventListener('drop', async (event) => {
    try {
      const files = await filesFromDrop(event.dataTransfer);
      setFiles(files);
    } catch (error) {
      showToast(error.message || String(error), true);
    }
  });
  elements.clearSelection.addEventListener('click', clearSelection);
  elements.serveButton.addEventListener('click', startServing);
  elements.cancelUpload.addEventListener('click', cancelUpload);
  elements.retryHost.addEventListener('click', checkHost);
  elements.copyLocal.addEventListener('click', () => copyText(elements.localUrl.value));
  elements.copyPublic.addEventListener('click', () => copyText(elements.publicUrl.value));
  elements.openLocal.addEventListener('click', () => openUrl(elements.localUrl.value));
  elements.openPublic.addEventListener('click', () => openUrl(elements.publicUrl.value));
  elements.startTunnel.addEventListener('click', startTunnel);
  elements.stopTunnel.addEventListener('click', stopTunnel);
  elements.stopServer.addEventListener('click', stopServer);
  elements.newSession.addEventListener('click', replaceFiles);
  elements.clearLogs.addEventListener('click', () => { state.logs = []; renderLogs(); });
  document.addEventListener('click', (event) => {
    const target = event.target.closest('[data-copy]');
    if (target) copyText(target.dataset.copy);
  });
}

async function checkHost() {
  setHostState('', 'Checking companion…');
  elements.setupCard.classList.add('hidden');
  try {
    const hello = await request('hello');
    state.hostReady = true;
    state.cloudflared = hello.cloudflared;
    setHostState('ready', `Companion ready · Python ${hello.python}`);
    const status = await request('status');
    applyStatus(status);
  } catch (error) {
    state.hostReady = false;
    setHostState('error', 'Companion unavailable');
    elements.setupCard.classList.remove('hidden');
    showToast(error.message || String(error), true);
  }
}

function onBridgeMessage(message) {
  if (!message || message.v !== PROTOCOL_VERSION) return;
  if (message.type === 'event') {
    onHostEvent(message);
    return;
  }
  const pending = state.pending.get(message.id);
  if (!pending) return;
  state.pending.delete(message.id);
  clearTimeout(pending.timeout);
  if (message.ok) pending.resolve(message.result);
  else pending.reject(new Error(message.error?.message || 'Native companion request failed'));
}

function onHostEvent(message) {
  switch (message.event) {
    case 'native_disconnected':
      state.hostReady = false;
      setHostState('error', 'Companion disconnected');
      rejectAllPending(message.message || 'Companion disconnected');
      break;
    case 'request':
      state.logs.unshift(message);
      state.logs = state.logs.slice(0, 100);
      renderLogs();
      break;
    case 'tunnel_started':
      state.publicUrl = message.publicUrl;
      renderPublicLink();
      break;
    case 'tunnel_stopped':
      state.publicUrl = null;
      renderPublicLink();
      break;
    case 'session_cleared':
      resetToDrop();
      break;
    default:
      break;
  }
}

function request(type, payload = {}, timeoutMs = 45000) {
  if (!state.port) return Promise.reject(new Error('Extension bridge is unavailable'));
  const id = crypto.randomUUID();
  const envelope = { v: PROTOCOL_VERSION, id, type, payload };
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      state.pending.delete(id);
      reject(new Error(`Request timed out: ${type}`));
    }, timeoutMs);
    state.pending.set(id, { resolve, reject, timeout });
    state.port.postMessage(envelope);
  });
}

function rejectAllPending(message) {
  for (const [id, pending] of state.pending) {
    clearTimeout(pending.timeout);
    pending.reject(new Error(message));
    state.pending.delete(id);
  }
}

async function filesFromDrop(dataTransfer) {
  const items = [...(dataTransfer?.items || [])].filter((item) => item.kind === 'file');
  if (!items.length) return [];
  const entries = items.map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) {
    return [...dataTransfer.files].map((file) => ({ file, path: file.name }));
  }
  const output = [];
  for (const entry of entries) await walkEntry(entry, '', output);
  return output;
}

async function walkEntry(entry, prefix, output) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    output.push({ file, path: `${prefix}${file.name}` });
    return;
  }
  if (!entry.isDirectory) return;
  const directoryPrefix = `${prefix}${entry.name}/`;
  const reader = entry.createReader();
  while (true) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) break;
    for (const child of batch) await walkEntry(child, directoryPrefix, output);
  }
}

function setFiles(entries) {
  const seen = new Map();
  for (const entry of entries) {
    if (!entry.file || !entry.path) continue;
    const path = normalizePath(entry.path);
    if (!path) continue;
    seen.set(path, { file: entry.file, path });
  }
  state.files = [...seen.values()].sort((a, b) => a.path.localeCompare(b.path));
  state.totalBytes = state.files.reduce((sum, item) => sum + item.file.size, 0);
  if (!state.files.length) {
    showToast('No files found in the drop.', true);
    clearSelection();
    return;
  }
  renderSelection();
}

function normalizePath(path) {
  return path.replaceAll('\\', '/').replace(/^\/+/, '').split('/').filter((part) => part && part !== '.' && part !== '..').join('/');
}

function renderSelection() {
  elements.selection.classList.remove('hidden');
  elements.selectionSummary.textContent = `${state.files.length.toLocaleString()} file${state.files.length === 1 ? '' : 's'}`;
  elements.selectionDetail.textContent = ` · ${formatBytes(state.totalBytes)}`;
  elements.fileList.replaceChildren();
  for (const item of state.files.slice(0, MAX_VISIBLE_FILES)) {
    const row = document.createElement('div');
    row.className = 'file-item';
    const path = document.createElement('span');
    path.className = 'file-path';
    path.textContent = item.path;
    path.title = item.path;
    const size = document.createElement('span');
    size.className = 'file-size';
    size.textContent = formatBytes(item.file.size);
    row.append(path, size);
    elements.fileList.append(row);
  }
  if (state.files.length > MAX_VISIBLE_FILES) {
    const row = document.createElement('div');
    row.className = 'file-item muted';
    row.textContent = `…and ${(state.files.length - MAX_VISIBLE_FILES).toLocaleString()} more`;
    elements.fileList.append(row);
  }
}

function clearSelection() {
  state.files = [];
  state.totalBytes = 0;
  elements.fileInput.value = '';
  elements.folderInput.value = '';
  elements.selection.classList.add('hidden');
  elements.fileList.replaceChildren();
}

async function startServing() {
  if (!state.files.length) return;
  if (!state.hostReady) {
    await checkHost();
    if (!state.hostReady) return;
  }

  state.cancelled = false;
  state.uploadedBytes = 0;
  showProgress('Preparing local server…', 'Creating a temporary session', 0);
  try {
    await request('create_session', {
      spaFallback: elements.spaFallback.checked,
      allowCors: elements.allowCors.checked,
    });

    for (let index = 0; index < state.files.length; index += 1) {
      if (state.cancelled) throw new Error('Upload cancelled');
      const item = state.files[index];
      const fileId = crypto.randomUUID();
      state.activeFileId = fileId;
      updateProgress(`Copying ${index + 1} of ${state.files.length}`, item.path);
      await request('begin_file', { fileId, path: item.path, size: item.file.size });
      let offset = 0;
      let sequence = 0;
      while (offset < item.file.size) {
        if (state.cancelled) {
          await request('cancel_file', { fileId }).catch(() => {});
          throw new Error('Upload cancelled');
        }
        const next = Math.min(offset + CHUNK_BYTES, item.file.size);
        const bytes = new Uint8Array(await item.file.slice(offset, next).arrayBuffer());
        await request('file_chunk', { fileId, sequence, data: bytesToBase64(bytes) });
        const transferred = next - offset;
        offset = next;
        sequence += 1;
        state.uploadedBytes += transferred;
        updateProgress(`Copying ${index + 1} of ${state.files.length}`, item.path);
      }
      await request('end_file', { fileId });
      state.activeFileId = null;
    }

    const session = await request('finalize', {
      spaFallback: elements.spaFallback.checked,
      allowCors: elements.allowCors.checked,
    });
    state.session = session;
    state.publicUrl = null;
    renderLive();
    showToast('Local server started.');
  } catch (error) {
    await request('clear_session').catch(() => {});
    resetToDrop();
    showToast(error.message || String(error), true);
  } finally {
    state.activeFileId = null;
    elements.progressCard.classList.add('hidden');
  }
}

async function cancelUpload() {
  state.cancelled = true;
  elements.cancelUpload.disabled = true;
  if (state.activeFileId) await request('cancel_file', { fileId: state.activeFileId }).catch(() => {});
  await request('clear_session').catch(() => {});
  elements.cancelUpload.disabled = false;
}

function showProgress(title, detail, percent) {
  elements.dropCard.classList.add('hidden');
  elements.liveCard.classList.add('hidden');
  elements.progressCard.classList.remove('hidden');
  elements.progressTitle.textContent = title;
  elements.progressDetail.textContent = detail;
  setProgress(percent);
}

function updateProgress(title, path) {
  const percent = state.totalBytes ? (state.uploadedBytes / state.totalBytes) * 100 : 100;
  elements.progressTitle.textContent = title;
  elements.progressDetail.textContent = `${path} · ${formatBytes(state.uploadedBytes)} of ${formatBytes(state.totalBytes)}`;
  setProgress(percent);
}

function setProgress(percent) {
  const rounded = Math.max(0, Math.min(100, percent));
  elements.progressBar.style.width = `${rounded}%`;
  elements.progressPercent.textContent = `${Math.floor(rounded)}%`;
}

function bytesToBase64(bytes) {
  let binary = '';
  const stride = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += stride) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + stride));
  }
  return btoa(binary);
}

function applyStatus(status) {
  state.cloudflared = status.cloudflared;
  state.session = status.session;
  state.publicUrl = status.tunnel?.publicUrl || null;
  if (state.session) renderLive();
  else resetToDrop();
}

function renderLive() {
  elements.setupCard.classList.add('hidden');
  elements.dropCard.classList.add('hidden');
  elements.progressCard.classList.add('hidden');
  elements.liveCard.classList.remove('hidden');
  elements.activityCard.classList.remove('hidden');
  elements.localUrl.value = state.session.localUrl;
  elements.liveStats.textContent = `${state.session.fileCount.toLocaleString()} files · ${formatBytes(state.session.totalBytes)} · localhost:${state.session.port}`;
  elements.tunnelUnavailable.classList.toggle('hidden', Boolean(state.cloudflared));
  elements.startTunnel.classList.toggle('hidden', !state.cloudflared || Boolean(state.publicUrl));
  renderPublicLink();
}

function renderPublicLink() {
  const isPublic = Boolean(state.publicUrl);
  elements.publicLinkWrap.classList.toggle('hidden', !isPublic);
  elements.startTunnel.classList.toggle('hidden', isPublic || !state.cloudflared);
  elements.publicUrl.value = state.publicUrl || '';
  elements.publicBadge.textContent = isPublic ? 'Public tunnel active' : 'Local only';
  elements.publicBadge.className = `badge ${isPublic ? 'public' : 'local'}`;
}

async function startTunnel() {
  elements.startTunnel.disabled = true;
  elements.startTunnel.textContent = 'Creating public link…';
  try {
    const result = await request('start_tunnel', {}, 35000);
    state.publicUrl = result.publicUrl;
    renderPublicLink();
    showToast('Temporary public link created.');
  } catch (error) {
    showToast(error.message || String(error), true);
  } finally {
    elements.startTunnel.disabled = false;
    elements.startTunnel.textContent = 'Create temporary public link';
  }
}

async function stopTunnel() {
  elements.stopTunnel.disabled = true;
  try {
    await request('stop_tunnel');
    state.publicUrl = null;
    renderPublicLink();
    showToast('Public link stopped.');
  } catch (error) {
    showToast(error.message || String(error), true);
  } finally {
    elements.stopTunnel.disabled = false;
  }
}

async function stopServer() {
  elements.stopServer.disabled = true;
  try {
    await request('clear_session');
    resetToDrop();
    clearSelection();
    showToast('Server stopped and temporary files deleted.');
  } catch (error) {
    showToast(error.message || String(error), true);
  } finally {
    elements.stopServer.disabled = false;
  }
}

async function replaceFiles() {
  try {
    await request('clear_session');
    resetToDrop();
    clearSelection();
  } catch (error) {
    showToast(error.message || String(error), true);
  }
}

function resetToDrop() {
  state.session = null;
  state.publicUrl = null;
  state.logs = [];
  elements.progressCard.classList.add('hidden');
  elements.liveCard.classList.add('hidden');
  elements.activityCard.classList.add('hidden');
  elements.dropCard.classList.remove('hidden');
  renderLogs();
}

function renderLogs() {
  elements.activityLog.replaceChildren();
  elements.activityEmpty.classList.toggle('hidden', state.logs.length > 0);
  for (const log of state.logs) {
    const row = document.createElement('div');
    row.className = 'activity-row';
    const time = document.createElement('span');
    time.textContent = new Date(log.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const method = document.createElement('span');
    method.textContent = log.method;
    const status = document.createElement('span');
    status.textContent = String(log.status);
    status.className = log.status < 400 ? 'status-ok' : 'status-error';
    const path = document.createElement('span');
    path.className = 'activity-path';
    path.textContent = log.path;
    path.title = log.path;
    const bytes = document.createElement('span');
    bytes.className = 'bytes';
    bytes.textContent = formatBytes(log.bytes || 0);
    row.append(time, method, status, path, bytes);
    elements.activityLog.append(row);
  }
}

function setHostState(kind, label) {
  elements.hostDot.className = `dot${kind ? ` ${kind}` : ''}`;
  elements.hostLabel.textContent = label;
}

async function copyText(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showToast('Copied.');
  } catch {
    const input = document.createElement('textarea');
    input.value = text;
    document.body.append(input);
    input.select();
    document.execCommand('copy');
    input.remove();
    showToast('Copied.');
  }
}

function openUrl(url) {
  if (url) chrome.tabs.create({ url });
}

let toastTimer;
function showToast(message, error = false) {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.className = `toast show${error ? ' error' : ''}`;
  toastTimer = setTimeout(() => { elements.toast.className = 'toast'; }, 3500);
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / (1024 ** index);
  return `${value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}
