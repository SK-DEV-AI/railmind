// Service worker. Message router + storage + (optional) Tatkal alarm.
const DEFAULT_BACKEND = 'http://127.0.0.1:8000';

async function getBackend() {
  const r = await chrome.storage.local.get(['backendUrl']);
  return (r && r.backendUrl) || DEFAULT_BACKEND;
}

async function apiFetch(path, options = {}) {
  const base = await getBackend();
  const res = await fetch(base.replace(/\/+$/, '') + path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
  if (!res.ok) {
    const err = new Error((data && (data.detail || data.title)) || ('HTTP ' + res.status));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      switch (msg && msg.type) {
        case 'intent': {
          const body = { text: msg.text, locale: 'en-IN', use_llm: false };
          if (msg.passengers) body.passengers = msg.passengers;
          const data = await apiFetch('/intent', { method: 'POST', body: JSON.stringify(body) });
          sendResponse({ ok: true, data });
          break;
        }
        case 'search': {
          const data = await apiFetch('/search', { method: 'POST', body: JSON.stringify({ intent_id: msg.intentId, limit: msg.limit || 8, deadline_ms: 1800 }) });
          sendResponse({ ok: true, data });
          break;
        }
        case 'handoff': {
          const data = await apiFetch('/handoff', { method: 'POST', body: JSON.stringify({ itinerary_id: msg.itineraryId }) });
          sendResponse({ ok: true, data });
          break;
        }
        case 'health': {
          const data = await apiFetch('/health', { method: 'GET' });
          sendResponse({ ok: true, data });
          break;
        }
        case 'setBackend': {
          await chrome.storage.local.set({ backendUrl: msg.url });
          sendResponse({ ok: true });
          break;
        }
        case 'storeLast': {
          await chrome.storage.local.set({ railmindLast: msg.payload });
          sendResponse({ ok: true });
          break;
        }
        case 'getLast': {
          const r = await chrome.storage.local.get(['railmindLast']);
          sendResponse({ ok: true, data: (r && r.railmindLast) || null });
          break;
        }
        case 'clearLast': {
          await chrome.storage.local.remove(['railmindLast']);
          sendResponse({ ok: true });
          break;
        }
        case 'openIrctc': {
          const tab = await chrome.tabs.create({ url: msg.url });
          sendResponse({ ok: true, tabId: tab.id });
          break;
        }
        default:
          sendResponse({ ok: false, error: 'unknown message type' });
      }
    } catch (e) {
      sendResponse({ ok: false, error: (e && e.message) || String(e), status: e && e.status });
    }
  })();
  return true; // keep channel open
});

// Tatkal alarm (optional convenience). Fires a notification at the configured time.
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm && alarm.name === 'rm-tatkal') {
    chrome.notifications.create('rm-tatkal', {
      type: 'basic',
      iconUrl: 'src/icons/icon128.png',
      title: 'RailMind: Tatkal window',
      message: 'Tatkal booking window is open. Open IRCTC and finish the booking yourself.',
    });
  }
});

chrome.runtime.onInstalled.addListener(() => {
  // default backend
  chrome.storage.local.get(['backendUrl']).then(r => {
    if (!r || !r.backendUrl) chrome.storage.local.set({ backendUrl: DEFAULT_BACKEND });
  });
});
