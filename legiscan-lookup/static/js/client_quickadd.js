/* Shared client-picking JS, used by the flag-confirmation modal in
 * LOOKUP_BODY and by clientCell()'s per-row "Assign to client" dropdown
 * in FLAGGED_BODY — same reasoning as bill_tables.js (one place to
 * edit, not two hand-kept-in-sync copies), interpolated into both
 * pages' own <script> blocks via app.py's CLIENT_QUICKADD_JS.
 *
 * Covers two things every client <select> in this app now needs:
 *   - POSITIONS / clientOptionsHtml(): the shared support/oppose/watch
 *     list and a client <option> list that always ends in "+ Add new
 *     client", so every picker offers the same escape hatch instead of
 *     dead-ending at "no clients yet" (see clientCell()'s old fallback
 *     for what that used to look like).
 *   - The "+ Add new client" quick-add panel itself — a small modal,
 *     built lazily on first use and reused by both callers rather than
 *     duplicated, since a page can only ever have one open at a time.
 *     A trimmed copy of CLIENTS_BODY's own form: same fields minus the
 *     "for disclosure forms" section (effective_date/contract_period/
 *     agencies_lobbied), which stays editable later on the full
 *     /clients page — this is a quick add, not the whole form.
 */

const POSITIONS = [['watch', 'Watch'], ['support', 'Support'], ['oppose', 'Oppose']];

const ADD_NEW_CLIENT_VALUE = '__add_new_client__';

function clientOptionsHtml(available) {
  const opts = (available || []).map(c => `<option value="${c.id}">${c.name}</option>`).join('');
  return opts + `<option value="${ADD_NEW_CLIENT_VALUE}">+ Add new client</option>`;
}

// { existingClients, onCreated, onCancel } while the quick-add panel is
// open — null the rest of the time. existingClients is only used to
// tell which row of the POST /api/clients response (the endpoint
// returns the caller's whole client list, not just the new one) is the
// one just created: whichever id wasn't already in existingClients.
let quickAddClientState = null;

function ensureQuickAddClientModal() {
  if (document.getElementById('quick-add-client-backdrop')) return;
  const backdrop = document.createElement('div');
  backdrop.id = 'quick-add-client-backdrop';
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = `
    <div class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="qac-title">
      <div class="modal-head">
        <div>
          <div class="title" id="qac-title">Add new client</div>
          <div class="sub">The rest of the form (effective date, agencies lobbied, etc.) can be filled in later on /clients.</div>
        </div>
        <button type="button" class="icon-btn" id="qac-close" aria-label="Close">×</button>
      </div>
      <form id="qac-form">
        <label style="position:relative">
          <div class="sub" style="margin:0 0 0.3rem">Client / employer name</div>
          <input id="qac-name" required autocomplete="off">
          <div id="qac-name-autofill-dropdown" class="autofill-dropdown" style="top:100%;left:0;right:0;width:auto"></div>
        </label>
        <p class="sub" id="qac-name-autofill-note" style="margin:0.2rem 0 0;display:none">Prefilled from CAL-ACCESS — edit anything below if it looks wrong.</p>
        <div style="display:flex;gap:0.5rem;flex-wrap:wrap">
          <input id="qac-bus_addr1" placeholder="Street address" style="flex:1 1 100%">
          <input id="qac-bus_city" placeholder="City" style="flex:2">
          <input id="qac-bus_st" placeholder="State" maxlength="2" style="flex:1;text-transform:uppercase">
          <input id="qac-bus_zip4" placeholder="ZIP" style="flex:1">
        </div>
        <label>
          <div class="sub" style="margin:0 0 0.3rem">Description of the client's industry or interests</div>
          <textarea id="qac-interests" rows="2"></textarea>
        </label>
        <label>
          <div class="sub" style="margin:0 0 0.3rem">California Secretary of State filer ID <span style="font-weight:400">(optional)</span></div>
          <input id="qac-existing_filer_id" placeholder="e.g. 1486088">
        </label>
        <div id="qac-error" role="alert" aria-live="assertive"></div>
        <div class="modal-actions">
          <button type="submit" id="qac-submit">Add client →</button>
          <button type="button" class="secondary" id="qac-cancel">Cancel</button>
        </div>
      </form>
    </div>
  `;
  document.body.appendChild(backdrop);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) cancelQuickAddClient(); });
  document.getElementById('qac-close').addEventListener('click', cancelQuickAddClient);
  document.getElementById('qac-cancel').addEventListener('click', cancelQuickAddClient);
  document.getElementById('qac-form').addEventListener('submit', submitQuickAddClient);
  wireQuickAddNameAutofill();
}

// Same live CAL-ACCESS name autofill as the full /clients form (see that
// page's runNameAutofillSearch/applyEntityAutofill) — duplicated here
// rather than shared because this modal's JS ships as its own constant,
// included verbatim into other pages' <script> blocks that never load
// the /clients page's script at all. Employer matches sort first and
// firm/coalition matches render de-emphasized, same reasoning as there:
// a "client" means an employer, but a de-emphasized match beats hiding
// a name the user might actually have meant.
let qacAutofillTimer = null;

function wireQuickAddNameAutofill() {
  const nameInput = document.getElementById('qac-name');
  const dropdown = document.getElementById('qac-name-autofill-dropdown');
  const note = document.getElementById('qac-name-autofill-note');

  function closeDropdown() {
    dropdown.classList.remove('show');
    dropdown.innerHTML = '';
  }

  async function search(q) {
    try {
      const res = await fetch(`/api/lobbying/search?q=${encodeURIComponent(q)}`);
      const data = await res.json();
      if (!res.ok) return;
      if (nameInput.value.trim() !== q) return;
      const matches = (data.results || []).filter(r => r.kind === 'entity').slice(0, 6);
      if (!matches.length) { closeDropdown(); return; }
      const sorted = [...matches].sort((a, b) =>
        (a.entity_type === 'employer' ? 0 : 1) - (b.entity_type === 'employer' ? 0 : 1));
      dropdown.innerHTML = sorted.map(r => `
        <button type="button" class="${r.entity_type === 'employer' ? '' : 'non-employer-match'}" data-id="${r.id}">
          ${r.name}
          ${r.entity_type && r.entity_type !== 'employer' ? ` <span class="tag">${r.entity_type}</span>` : ''}
          ${r.city || r.state ? ` <span class="sub" style="margin:0">— ${[r.city, r.state].filter(Boolean).join(', ')}</span>` : ''}
        </button>
      `).join('');
      dropdown.classList.add('show');
    } catch (err) {
      // A failed suggestion lookup shouldn't block typing a name by
      // hand — just don't offer any suggestions this time.
    }
  }

  dropdown.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-id]');
    if (!btn) return;
    closeDropdown();
    try {
      const res = await fetch(`/api/lobbying/detail?id=${encodeURIComponent(btn.dataset.id)}`);
      const data = await res.json();
      if (!res.ok || !data.entity) return;
      const entity = data.entity;
      nameInput.value = entity.name || nameInput.value;
      document.getElementById('qac-bus_addr1').value = entity.address || '';
      document.getElementById('qac-bus_city').value = entity.city || '';
      document.getElementById('qac-bus_st').value = entity.state || '';
      document.getElementById('qac-bus_zip4').value = entity.zip || '';
      if (entity.filer_id) document.getElementById('qac-existing_filer_id').value = entity.filer_id;
      note.style.display = '';
    } catch (err) {
      // Same reasoning as search()'s catch — leave the name as typed
      // rather than blocking on this.
    }
  });

  nameInput.addEventListener('input', () => {
    note.style.display = 'none';
    clearTimeout(qacAutofillTimer);
    const q = nameInput.value.trim();
    if (q.length < 2) { closeDropdown(); return; }
    qacAutofillTimer = setTimeout(() => search(q), 300);
  });
  document.addEventListener('click', (e) => {
    if (!dropdown.contains(e.target) && e.target !== nameInput) closeDropdown();
  });
  nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDropdown();
  });
}

function openQuickAddClient(existingClients, onCreated, onCancel) {
  ensureQuickAddClientModal();
  quickAddClientState = { existingClients: existingClients || [], onCreated, onCancel };
  document.getElementById('qac-form').reset();
  document.getElementById('qac-error').className = '';
  document.getElementById('qac-name-autofill-note').style.display = 'none';
  document.getElementById('qac-name-autofill-dropdown').classList.remove('show');
  document.getElementById('quick-add-client-backdrop').classList.add('show');
  document.getElementById('qac-name').focus();
}

function closeQuickAddClientModal() {
  const backdrop = document.getElementById('quick-add-client-backdrop');
  if (backdrop) backdrop.classList.remove('show');
}

function cancelQuickAddClient() {
  const state = quickAddClientState;
  quickAddClientState = null;
  closeQuickAddClientModal();
  if (state && state.onCancel) state.onCancel();
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const backdrop = document.getElementById('quick-add-client-backdrop');
  if (backdrop && backdrop.classList.contains('show')) cancelQuickAddClient();
});

async function submitQuickAddClient(e) {
  e.preventDefault();
  const state = quickAddClientState;
  if (!state) return;
  const errorEl = document.getElementById('qac-error');
  errorEl.className = '';
  const name = document.getElementById('qac-name').value.trim();
  if (!name) {
    errorEl.textContent = 'Client / employer name is required.';
    errorEl.className = 'show';
    return;
  }
  const body = {
    name,
    bus_addr1: document.getElementById('qac-bus_addr1').value.trim(),
    bus_city: document.getElementById('qac-bus_city').value.trim(),
    bus_st: document.getElementById('qac-bus_st').value.trim(),
    bus_zip4: document.getElementById('qac-bus_zip4').value.trim(),
    interests: document.getElementById('qac-interests').value.trim(),
    existing_filer_id: document.getElementById('qac-existing_filer_id').value.trim(),
  };
  const submitBtn = document.getElementById('qac-submit');
  submitBtn.disabled = true;
  try {
    const beforeIds = new Set(state.existingClients.map(c => c.id));
    const res = await fetch('/api/clients', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const rows = await res.json();
    if (!res.ok) throw new Error((rows && rows.error) || 'Could not add client');
    const created = rows.find(c => !beforeIds.has(c.id)) || rows[rows.length - 1];
    quickAddClientState = null;
    closeQuickAddClientModal();
    if (state.onCreated) state.onCreated(rows, created);
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.className = 'show';
  } finally {
    submitBtn.disabled = false;
  }
}
