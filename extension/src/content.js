// Content script for IRCTC pages. Injects a panel; never submits, never reads availability.
(async function () {
  if (window.__rmContentLoaded) return;
  window.__rmContentLoaded = true;

  const RM = window.RM;
  if (!RM) { console.warn('RailMind: selectors not loaded'); return; }

  await RM.api.loadBaseUrl();

  // Dismiss the language chooser if present (it covers the form on a first visit).
  try {
    for (const el of document.querySelectorAll('button, a, span, div')) {
      if ((el.innerText || '').trim() === 'English' && el.offsetParent !== null) { el.click(); break; }
    }
  } catch (e) { console.debug('RailMind: no language chooser', e); }

  // Only show panel on a page that actually has the booking form.
  const hasForm = !!document.querySelector('[formcontrolname="origin"]');
  if (!hasForm) {
    // Still inject a tiny badge? No. Exit.
    return;
  }

  let last = null;
  try {
    const r = await chrome.storage.local.get(['railmindLast']);
    last = (r && r.railmindLast) || null;
  } catch (e) {}

  // Build panel
  const panel = document.createElement('div');
  panel.id = 'rm-panel';
  panel.innerHTML = `
    <div class="rm-header">
      <span class="rm-title">RailMind Companion</span>
      <button class="rm-close" id="rm-close" title="Hide">&times;</button>
    </div>
    <div class="rm-status" id="rm-status">Backend: ${escapeHtml(RM.api.baseUrl)}</div>
    <div id="rm-body"></div>
  `;
  document.body.appendChild(panel);

  const body = panel.querySelector('#rm-body');
  const status = panel.querySelector('#rm-status');
  panel.querySelector('#rm-close').addEventListener('click', () => { panel.style.display = 'none'; });

  function render() {
    const parts = [];
    if (!last) {
      parts.push('<div class="rm-card">No RailMind result stored yet. Run a search in the popup, pick a train, then come back.</div>');
      parts.push('<button class="rm-btn rm-btn-secondary" id="rm-refresh">Reload stored result</button>');
      body.innerHTML = parts.join('');
      bindRefresh();
      return;
    }
    const train = last.trainName ? `${last.trainName} (${last.trainNumber || ''})` : (last.trainNumber || 'selected train');
    const riskCls = 'rm-' + (last.riskBand || 'unknown');
    parts.push(`
      <div class="rm-card">
        <div class="rm-row"><span class="rm-label">Train</span><span class="rm-value">${escapeHtml(train)}</span></div>
        <div class="rm-row"><span class="rm-label">Route</span><span class="rm-value">${escapeHtml(last.fromCode || '')} → ${escapeHtml(last.toCode || '')}</span></div>
        <div class="rm-row"><span class="rm-label">Date</span><span class="rm-value">${escapeHtml(last.journeyDateISO || '')}</span></div>
        <div class="rm-row"><span class="rm-label">Class</span><span class="rm-value">${escapeHtml(last.preferredClass || '')}</span></div>
        <div class="rm-row"><span class="rm-label">Fare</span><span class="rm-value">${last.fare ? (last.fare.currency + ' ' + (last.fare.amount_minor/100).toFixed(2)) : 'n/a'}</span></div>
        <div class="rm-row"><span class="rm-label">Risk</span><span class="rm-badge ${riskCls}">${escapeHtml(last.riskBand || 'unknown')}</span></div>
        ${last.riskReasons && last.riskReasons.length ? '<div class="rm-note">' + last.riskReasons.map(escapeHtml).join(' ') + '</div>' : ''}
      </div>
    `);
    parts.push('<button class="rm-btn rm-btn-primary" id="rm-fill">Fill form from RailMind</button>');
    parts.push('<button class="rm-btn rm-btn-secondary" id="rm-open">Open IRCTC booking page again</button>');
    parts.push('<div class="rm-note">RailMind fills the fields only. It never submits, never logs in, never reads availability. Review every field, then you click Search.</div>');
    if (last.warnings && last.warnings.length) {
      parts.push('<div class="rm-note">' + last.warnings.map(escapeHtml).join(' ') + '</div>');
    }
    body.innerHTML = parts.join('');
    bindActions();
  }

  function bindRefresh() {
    const b = body.querySelector('#rm-refresh');
    if (b) b.addEventListener('click', async () => {
      const r = await chrome.storage.local.get(['railmindLast']);
      last = (r && r.railmindLast) || null;
      render();
    });
  }

  function bindActions() {
    const fillBtn = body.querySelector('#rm-fill');
    if (fillBtn) fillBtn.addEventListener('click', () => fillFromLast());
    const openBtn = body.querySelector('#rm-open');
    if (openBtn) openBtn.addEventListener('click', () => {
      if (last && last.handoffUrl) window.open(last.handoffUrl, '_blank');
    });
  }

  async function fillFromLast() {
    if (!last) return;
    try {
      await fillIrctc(last);
      status.textContent = 'Filled. Review and click Search Trains yourself.';
    } catch (e) {
      status.textContent = 'Fill failed: ' + (e && e.message ? e.message : e);
      console.error('RailMind fill error', e);
    }
  }

  async function fillIrctc(data) {
    // origin
    await fillAutocomplete('origin', data.fromCode || data.fromStation, data.fromCode);
    await RM.sleep(300);
    await fillAutocomplete('destination', data.toCode || data.toStation, data.toCode);
    await RM.sleep(300);
    // date
    const dateInput = document.querySelector('[formcontrolname="journeyDate"] input');
    if (dateInput) {
      const d = RM.isoToIrctcDate(data.journeyDateISO || data.journey_date);
      dateInput.focus();
      dateInput.value = '';
      dateInput.value = d;
      dateInput.dispatchEvent(new Event('input', { bubbles: true }));
      dateInput.dispatchEvent(new Event('change', { bubbles: true }));
      dateInput.dispatchEvent(new Event('blur', { bubbles: true }));
    }
    await RM.sleep(200);
    // class
    if (data.preferredClass) {
      await pickDropdown('journeyClass', data.preferredClass);
    }
    await RM.sleep(200);
    // quota
    const quota = (data.quota || 'general').toUpperCase();
    const quotaMap = { GENERAL:'GENERAL', TATKAL:'TATKAL', LADIES:'LADIES' };
    await pickDropdown('journeyQuota', quotaMap[quota] || quota);
  }

  async function fillAutocomplete(fc, query, code) {
    const wrap = document.querySelector(`[formcontrolname="${fc}"]`);
    const input = wrap ? wrap.querySelector('input') : null;
    if (!input) throw new Error('missing input for ' + fc);
    input.focus();
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    // type char by char to trigger PrimeNG autocomplete
    for (const ch of String(query || '')) {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
      input.value += ch;
      input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: ch }));
      input.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
      await RM.sleep(30);
    }
    // wait for panel
    for (let i = 0; i < 30; i++) {
      const items = document.querySelectorAll('.ui-autocomplete-panel li');
      if (items.length) break;
      await RM.sleep(100);
    }
    await RM.sleep(200);
    const items = [...document.querySelectorAll('.ui-autocomplete-panel li')];
    const target = RM.findStationItem(items, code);
    if (target) target.click();
    await RM.sleep(400);
  }

  async function pickDropdown(fc, optionText) {
    const wrap = document.querySelector(`[formcontrolname="${fc}"]`);
    if (!wrap) return;
    wrap.click();
    await RM.sleep(900);
    const opts = [...document.querySelectorAll('.ui-dropdown-panel .ui-dropdown-item')];
    let target = opts.find(o => (o.innerText || '').toUpperCase().includes(String(optionText).toUpperCase()));
    // if optionText is a code like 3A, also match "(3A)"
    if (!target) target = opts.find(o => (o.innerText || '').includes('(' + optionText + ')'));
    if (target) target.click();
    await RM.sleep(400);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  render();
})();
