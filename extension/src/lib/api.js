// Backend client. All network calls go through here.
// Runs in content scripts, popup, and (duplicated logic) the service worker.
window.RM = window.RM || {};
window.RM.api = {
  baseUrl: 'http://127.0.0.1:8000',

  setBaseUrl(url) {
    this.baseUrl = (url || 'http://127.0.0.1:8000').replace(/\/+$/, '');
  },

  async loadBaseUrl() {
    try {
      const r = await chrome.storage.local.get(['backendUrl']);
      if (r && r.backendUrl) this.setBaseUrl(r.backendUrl);
    } catch (e) { /* storage unavailable */ }
  },

  async _fetch(path, options = {}) {
    await this.loadBaseUrl();
    const url = this.baseUrl + path;
    const res = await fetch(url, {
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
  },

  async intent(text, passengers) {
    const body = { text, locale: 'en-IN', use_llm: false };
    if (passengers) body.passengers = passengers;
    return this._fetch('/intent', { method: 'POST', body: JSON.stringify(body) });
  },

  async search(intentId, limit = 8) {
    return this._fetch('/search', { method: 'POST', body: JSON.stringify({ intent_id: intentId, limit, deadline_ms: 1800 }) });
  },

  async handoff(itineraryId) {
    return this._fetch('/handoff', { method: 'POST', body: JSON.stringify({ itinerary_id: itineraryId }) });
  },

  async health() {
    return this._fetch('/health', { method: 'GET' });
  },
};
