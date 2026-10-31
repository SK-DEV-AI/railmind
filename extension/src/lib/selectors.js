// IRCTC DOM selectors — single source of truth.
// Verified against the live old-site booking form on 2026-10-31 (see ../shots/probe-filled.png).
// PrimeNG components: p-autocomplete, p-calendar, p-dropdown.
// The formcontrolname sits on the wrapper component; the fillable input is inside it.

window.RM = window.RM || {};
window.RM.sel = {
  origin: '[formcontrolname="origin"]',
  destination: '[formcontrolname="destination"]',
  date: '[formcontrolname="journeyDate"]',
  class: '[formcontrolname="journeyClass"]',
  quota: '[formcontrolname="journeyQuota"]',
  searchBtn: 'button.search_btn, button:has-text("Search Trains")',
  autocompletePanel: '.ui-autocomplete-panel li, .ui-autocomplete-items li',
  dropdownPanel: '.ui-dropdown-panel .ui-dropdown-item, .ui-dropdown-items li',
  langEnglish: 'text=English',
  betaExplore: 'text=Explore beta now',
};

// Helpers
window.RM.q = (sel, root = document) => root.querySelector(sel);
window.RM.qa = (sel, root = document) => [...root.querySelectorAll(sel)];
window.RM.sleep = (ms) => new Promise(r => setTimeout(r, ms));

// Convert ISO (YYYY-MM-DD) to IRCTC's dd/mm/yyyy.
window.RM.isoToIrctcDate = (iso) => {
  if (!iso) return null;
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return iso;
  return `${m[3]}/${m[2]}/${m[1]}`;
};

// Find a station suggestion whose text contains the given code in parentheses.
window.RM.findStationItem = (items, code) => {
  const c = (code || '').toUpperCase();
  for (const it of items) {
    const t = (it.innerText || '').trim();
    if (c && t.includes('(' + c + ')')) return it;
  }
  // fallback: first item that looks like a station (has parentheses)
  for (const it of items) {
    const t = (it.innerText || '').trim();
    if (t.includes('(') && t.includes(')')) return it;
  }
  return items[0] || null;
};
