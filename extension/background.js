const HOST_NAME = 'com.localdrop.live';
const PROTOCOL_VERSION = 1;
const APP_URL = chrome.runtime.getURL('app.html');

let nativePort = null;
let nativeConnecting = false;
let reconnectTimer = null;
let idleDisconnectTimer = null;
let hostWanted = false;
let intentionalDisconnect = false;
const appPorts = new Set();

chrome.storage.local.get('hostWanted').then((stored) => { hostWanted = Boolean(stored.hostWanted); });

chrome.action.onClicked.addListener(async () => {
  const { appTabId } = await chrome.storage.local.get('appTabId');
  if (Number.isInteger(appTabId)) {
    try {
      const tab = await chrome.tabs.get(appTabId);
      await chrome.tabs.update(appTabId, { active: true });
      if (tab.windowId) await chrome.windows.update(tab.windowId, { focused: true });
      return;
    } catch {
      await chrome.storage.local.remove('appTabId');
    }
  }
  const tab = await chrome.tabs.create({ url: APP_URL });
  if (tab.id) await chrome.storage.local.set({ appTabId: tab.id });
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const { appTabId } = await chrome.storage.local.get('appTabId');
  if (tabId === appTabId) await chrome.storage.local.remove('appTabId');
});

chrome.runtime.onInstalled.addListener(() => {
  hostWanted = false;
  chrome.storage.local.set({ hostWanted: false, lastNativeError: null });
});

chrome.runtime.onStartup.addListener(async () => {
  const stored = await chrome.storage.local.get('hostWanted');
  hostWanted = Boolean(stored.hostWanted);
  if (hostWanted) connectNative();
});

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'localdrop-app') return;
  appPorts.add(port);
  port.onMessage.addListener((message) => onAppMessage(port, message));
  port.onDisconnect.addListener(() => appPorts.delete(port));
  port.postMessage({ v: PROTOCOL_VERSION, type: 'event', event: 'bridge_ready' });
});

async function onAppMessage(appPort, message) {
  if (!isEnvelope(message)) {
    appPort.postMessage(errorResponse(message?.id, 'InvalidEnvelope', 'Invalid extension message'));
    return;
  }

  if (message.type === 'create_session') {
    hostWanted = true;
    clearIdleDisconnect();
    await chrome.storage.local.set({ hostWanted: true });
  } else if (message.type === 'clear_session') {
    hostWanted = false;
    await chrome.storage.local.set({ hostWanted: false });
  }

  const port = connectNative();
  if (!port) {
    appPort.postMessage(errorResponse(message.id, 'NativeHostUnavailable', 'Native companion is unavailable. Run the installer, then retry.'));
    return;
  }

  try {
    port.postMessage(message);
  } catch (error) {
    appPort.postMessage(errorResponse(message.id, 'NativePostFailed', error.message || String(error)));
  }
}

function connectNative() {
  if (nativePort) return nativePort;
  if (nativeConnecting) return null;
  nativeConnecting = true;

  try {
    const port = chrome.runtime.connectNative(HOST_NAME);
    nativePort = port;
    port.onMessage.addListener((message) => {
      chrome.storage.local.set({ lastNativeError: null });
      broadcast(message);
      if (!hostWanted) scheduleIdleDisconnect();
    });
    port.onDisconnect.addListener(async () => {
      const wasIntentional = intentionalDisconnect;
      intentionalDisconnect = false;
      const detail = chrome.runtime.lastError?.message || 'Native companion disconnected';
      if (nativePort === port) nativePort = null;
      nativeConnecting = false;
      if (wasIntentional) return;
      await chrome.storage.local.set({ lastNativeError: detail });
      broadcast({ v: PROTOCOL_VERSION, type: 'event', event: 'native_disconnected', message: detail });
      if (hostWanted && !detail.toLowerCase().includes('not found')) scheduleReconnect();
    });
    nativeConnecting = false;
    broadcast({ v: PROTOCOL_VERSION, type: 'event', event: 'native_connecting' });
    return port;
  } catch (error) {
    nativeConnecting = false;
    chrome.storage.local.set({ lastNativeError: error.message || String(error) });
    return null;
  }
}

function scheduleIdleDisconnect() {
  clearIdleDisconnect();
  idleDisconnectTimer = setTimeout(() => {
    idleDisconnectTimer = null;
    if (!hostWanted && nativePort) {
      const port = nativePort;
      nativePort = null;
      intentionalDisconnect = true;
      try { port.disconnect(); } catch { intentionalDisconnect = false; }
    }
  }, 30000);
}

function clearIdleDisconnect() {
  if (!idleDisconnectTimer) return;
  clearTimeout(idleDisconnectTimer);
  idleDisconnectTimer = null;
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectNative();
  }, 1000);
}

function broadcast(message) {
  for (const port of [...appPorts]) {
    try {
      port.postMessage(message);
    } catch {
      appPorts.delete(port);
    }
  }
}

function isEnvelope(message) {
  return Boolean(
    message &&
    message.v === PROTOCOL_VERSION &&
    typeof message.id === 'string' &&
    typeof message.type === 'string' &&
    message.payload &&
    typeof message.payload === 'object'
  );
}

function errorResponse(id, code, message) {
  return { v: PROTOCOL_VERSION, id, ok: false, error: { code, message } };
}
