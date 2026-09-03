/* Shared "which of my clients is this bill assigned to, and what's our
 * position on it" control — the client chips, the support/oppose/watch
 * <select>s, the remove button, and the "+ Add client" picker that ends
 * in the quick-add panel.
 *
 * Extracted from FLAGGED_BODY, where it was the whole reason the flagged
 * list could do something the bill report couldn't. The report page is
 * the one a digest email links to, which makes it the most likely entry
 * point in the product, and it displayed the same assignments as dead
 * text — so changing a position meant navigating back to the list and
 * finding the row again. Same reasoning as row_menu.js and
 * client_quickadd.js: one definition, not two hand-kept-in-sync copies.
 *
 * Depends on client_quickadd.js (POSITIONS, clientOptionsHtml,
 * openQuickAddClient) and title_case.js (titleCaseName), so load both
 * before this one.
 *
 * Pages differ in where they keep their state and what "reload" means,
 * so this takes those as hooks rather than reaching for page globals —
 * the same way openQuickAddClient() takes its client list as an
 * argument instead of assuming one.
 */

let billClientsHooks = {
  // Every client this user has, for the "+ Add client" options.
  allClients: () => [],
  // The clients already on this bill — used to exclude them from the add
  // list, and to name one in the removal confirmation.
  assignedClients: () => [],
  // Show a failure to the user. Called with '' at the start of every
  // mutation to clear a previous one, so a stale error can't sit above
  // a change that has since succeeded.
  onError: () => {},
  // Re-fetch after a successful write. Each page reloads its own shape.
  onChanged: () => {},
};

function initBillClients(hooks) {
  billClientsHooks = Object.assign({}, billClientsHooks, hooks);
}

function positionSelectHtml(billId, client) {
  const position = client.position || 'watch';
  const options = POSITIONS.map(([value, label]) =>
    `<option value="${value}" ${position === value ? 'selected' : ''}>${label}</option>`
  ).join('');
  return `<select class="position-select ${position}" data-saved="${position}" onchange="setPosition(${billId}, ${client.id}, this)" style="font-size:0.78rem;padding:0.3rem 0.5rem;font-weight:600">${options}</select>`;
}

// Two different jobs live in this cell: the chips change an *existing*
// assignment's position (or remove it); the select below them *adds a
// new* client. They used to look like two competing ways to do the same
// thing — see .client-chip/.add-client-select in STYLE for the visual
// fix.
function billClientCellHtml(billId) {
  const assigned = billClientsHooks.assignedClients(billId) || [];
  const allClients = billClientsHooks.allClients() || [];

  const chips = assigned.map(c => `
    <div class="client-chip">
      <a href="/clients/detail?id=${c.id}">${titleCaseName(c.name)}</a>
      ${positionSelectHtml(billId, c)}
      <button type="button" class="icon-btn" onclick="unassignClient(${billId}, ${c.id}, this)" aria-label="Remove client from this bill" title="Remove client" style="height:1.5rem;width:1.5rem;color:var(--slate)">×</button>
    </div>
  `).join('');

  const assignedIds = new Set(assigned.map(c => c.id));
  const available = allClients.filter(c => !assignedIds.has(c.id));
  const placeholder = allClients.length ? (available.length ? '+ Add client' : 'All clients assigned') : 'No clients yet…';
  return `
    <div>${chips}</div>
    <select class="add-client-select" onchange="handleClientCellSelect(${billId}, this)" style="margin-top:0.2rem">
      <option value="">${placeholder}</option>
      ${clientOptionsHtml(available)}
    </select>
  `;
}

async function setPosition(billId, clientId, selectEl) {
  billClientsHooks.onError('');
  const newPosition = selectEl.value;
  const savedPosition = selectEl.dataset.saved;
  // Optimistic repaint so the picked color shows right away — reverted
  // in the catch below if the server rejects the change, so a failed
  // save can no longer look identical to a successful one.
  selectEl.className = 'position-select ' + newPosition;
  selectEl.disabled = true;
  try {
    const res = await fetch('/api/bill-clients', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bill_id: billId, client_id: clientId, position: newPosition }),
    });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error || 'Could not update position');
    }
    selectEl.dataset.saved = newPosition;
    billClientsHooks.onChanged();
  } catch (err) {
    selectEl.value = savedPosition;
    selectEl.className = 'position-select ' + savedPosition;
    selectEl.disabled = false;
    billClientsHooks.onError(err.message);
  }
}

async function assignClient(billId, selectEl) {
  const clientId = selectEl.value;
  if (!clientId) return;
  billClientsHooks.onError('');
  selectEl.disabled = true;
  try {
    const res = await fetch('/api/bill-clients', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bill_id: billId, client_id: Number(clientId) }),
    });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error || 'Could not assign client');
    }
    billClientsHooks.onChanged();
  } catch (err) {
    selectEl.disabled = false;
    billClientsHooks.onError(err.message);
  }
}

async function unassignClient(billId, clientId, btnEl) {
  // Same confirmDelete() dialog as removeClient() (see CONFIRM_DELETE_JS).
  const client = (billClientsHooks.assignedClients(billId) || []).find(c => c.id === clientId);
  const ok = await confirmDelete('Remove client?', `Remove ${client ? client.name : 'this client'} from this bill? This can't be undone.`);
  if (!ok) return;
  billClientsHooks.onError('');
  btnEl.disabled = true;
  try {
    const res = await fetch(`/api/bill-clients?bill_id=${billId}&client_id=${clientId}`, { method: 'DELETE' });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error || 'Could not remove assignment');
    }
    billClientsHooks.onChanged();
  } catch (err) {
    btnEl.disabled = false;
    billClientsHooks.onError(err.message);
  }
}

// Selecting an existing client assigns it right away — selecting "+ Add
// new client" (always the last option, see clientOptionsHtml() in
// client_quickadd.js) opens the quick-add panel instead, then assigns
// whatever it creates to this bill, since that's the whole reason to
// reach the quick-add form from here rather than from /clients directly.
function handleClientCellSelect(billId, selectEl) {
  const value = selectEl.value;
  if (value !== ADD_NEW_CLIENT_VALUE) {
    // Reset right away rather than waiting for the network round trip —
    // assignClient() below is async and isn't awaited here.
    assignClient(billId, selectEl);
    selectEl.value = '';
    return;
  }
  openQuickAddClient(billClientsHooks.allClients(), (updatedClients, created) => {
    assignNewClientToBill(billId, created.id, updatedClients);
  }, () => {
    selectEl.value = '';
  });
}

async function assignNewClientToBill(billId, clientId, updatedClients) {
  billClientsHooks.onError('');
  try {
    const res = await fetch('/api/bill-clients', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bill_id: billId, client_id: clientId }),
    });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error || 'Client was added, but could not be assigned to this bill.');
    }
  } catch (err) {
    billClientsHooks.onError(err.message);
  } finally {
    // Refresh either way — the new client should show up in every
    // picker from here on even if assigning it to *this* bill failed.
    billClientsHooks.onChanged(updatedClients);
  }
}
