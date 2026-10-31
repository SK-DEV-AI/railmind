// Popup logic. Uses the service worker via chrome.runtime.sendMessage.
(async function () {
  const RM = window.RM;
  await RM.api.loadBaseUrl();

  const backendInput = document.getElementById('backend');
  const healthEl = document.getElementById('health');
  const queryInput = document.getElementById('query');
  const statusEl = document.getElementById('status');
  const resultsEl = document.getElementById('results');

  // load stored backend
  const r = await chrome.storage.local.get(['backendUrl']);
  if (r && r.backendUrl) {
    backendInput.value = r.backendUrl;
    RM.api.setBaseUrl(r.backendUrl);
  } else {
    backendInput.value = RM.api.baseUrl;
  }

  async function msg(m) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(m, (resp) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else if (!resp || !resp.ok) reject(new Error((resp && resp.error) || 'failed'));
        else resolve(resp.data);
      });
    });
  }

  async function checkHealth() {
    try {
      const h = await msg({ type: 'health' });
      healthEl.textContent = 'Backend: ' + (h.status || 'ok') + ' (v' + (h.version || '?') + ')';
    } catch (e) {
      healthEl.textContent = 'Backend unreachable: ' + e.message;
    }
  }
  await checkHealth();

  document.getElementById('saveBackend').addEventListener('click', async () => {
    const url = backendInput.value.trim();
    RM.api.setBaseUrl(url);
    await msg({ type: 'setBackend', url });
    await checkHealth();
  });

  document.getElementById('clearBtn').addEventListener('click', async () => {
    await msg({ type: 'clearLast' });
    statusEl.textContent = 'Stored result cleared.';
  });

  document.getElementById('searchBtn').addEventListener('click', async () => {
    const text = queryInput.value.trim();
    if (!text) { statusEl.textContent = 'Enter a journey.'; return; }
    statusEl.textContent = 'Parsing intent...';
    resultsEl.innerHTML = '';
    try {
      const intentResp = await msg({ type: 'intent', text });
      const intentId = intentResp.intent.intent_id;
      statusEl.textContent = 'Searching...';
      const searchResp = await msg({ type: 'search', intentId, limit: 8 });
      const its = searchResp.itineraries || [];
      statusEl.textContent = its.length + ' options. Click one to prepare IRCTC handoff.';
      resultsEl.innerHTML = its.map(it => {
        const svc = (it.legs && it.legs[0] && it.legs[0].service) ? it.legs[0].service.name : 'train';
        const num = (it.legs && it.legs[0] && it.legs[0].service) ? it.legs[0].service.service_id : '';
        const dep = it.depart_at ? new Date(it.depart_at).toLocaleString('en-IN', { hour:'2-digit', minute:'2-digit', day:'2-digit', month:'short' }) : '';
        const price = it.total_price ? (it.total_price.currency + ' ' + (it.total_price.amount_minor/100).toFixed(0)) : 'n/a';
        const risk = it.risk_band || 'unknown';
        return `<div class="card it" data-id="${escapeHtml(it.itinerary_id)}" data-num="${escapeHtml(num)}" data-name="${escapeHtml(svc)}" data-risk="${escapeHtml(risk)}" data-price='${escapeHtml(JSON.stringify(it.total_price))}' data-breakup='${escapeHtml(JSON.stringify(it.price_breakup || []))}' data-reasons='${escapeHtml(JSON.stringify(it.risk_reasons || []))}' data-warnings='${escapeHtml(JSON.stringify(it.warnings || []))}' data-dep="${escapeHtml(it.depart_at)}" data-arr="${escapeHtml(it.arrive_at)}">
          <div style="font-weight:600;">#${it.rank} ${escapeHtml(svc)} (${escapeHtml(num)})</div>
          <div class="muted">${escapeHtml(dep)} · ${escapeHtml(price)} · <span class="badge rm-${escapeHtml(risk)}">${escapeHtml(risk)}</span></div>
        </div>`;
      }).join('');

      // store intent details for handoff prefill extraction
      window.__rmLastIntent = intentResp.intent;

      resultsEl.querySelectorAll('.it').forEach(card => {
        card.addEventListener('click', async () => {
          const iid = card.dataset.id;
          statusEl.textContent = 'Building handoff...';
          try {
            const h = await msg({ type: 'handoff', itineraryId: iid });
            const intent = window.__rmLastIntent || {};
            const fromCode = (intent.origin && intent.origin.code) || (h.prefill && h.prefill.from_station) || '';
            const toCode = (intent.destination && intent.destination.code) || (h.prefill && h.prefill.to_station) || '';
            const journeyDateISO = (h.prefill && h.prefill.journey_date) || '';
            const preferredClass = (h.prefill && h.prefill.preferred_class) || (intent.class_preferences && intent.class_preferences[0]) || '';
            const quota = (intent.quota || 'general');
            const payload = {
              itineraryId: iid,
              trainNumber: h.prefill && h.prefill.train_number || card.dataset.num,
              trainName: card.dataset.name,
              fromCode, toCode,
              journeyDateISO,
              preferredClass,
              quota,
              fare: h && null, // fill from card dataset
              riskBand: card.dataset.risk,
              riskReasons: safeParse(card.dataset.reasons, []),
              warnings: safeParse(card.dataset.warnings, []).concat(h.warnings || []),
              handoffUrl: h.url,
              steps: h.steps,
              createdAt: Date.now(),
            };
            // fare from card dataset
            try { payload.fare = JSON.parse(card.dataset.price); } catch (e) { payload.fare = null; }
            try { payload.priceBreakup = JSON.parse(card.dataset.breakup); } catch (e) { payload.priceBreakup = []; }
            payload.riskReasons = safeParse(card.dataset.reasons, []);
            if (h.warnings) payload.warnings = (payload.warnings || []).concat(h.warnings);

            await msg({ type: 'storeLast', payload });
            statusEl.textContent = 'Stored. Opening IRCTC...';
            await msg({ type: 'openIrctc', url: h.url });
            statusEl.textContent = 'Opened IRCTC. Click "Fill form" in the panel.';
          } catch (e) {
            statusEl.textContent = 'Handoff failed: ' + e.message;
          }
        });
      });
    } catch (e) {
      statusEl.textContent = 'Error: ' + e.message;
    }
  });

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function safeParse(s, fallback) {
    try { return JSON.parse(s); } catch (e) { return fallback; }
  }
})();
