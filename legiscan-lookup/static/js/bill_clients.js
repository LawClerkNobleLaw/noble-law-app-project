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
 * openQuickAddClient), title_case.js (titleCaseName), toast.js
 * (showToast) and position_history.js (positionLabel), so load all four
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
  // "CA SB1159" for the confirmations and toasts. A removal that says
  // only "remove this client" is a destructive action described without
  // naming either of the two things it destroys the link between; pages
  // that know the bill supply it, and the wording degrades to "this
  // bill" for any that don't.
  billLabel: () => '',
};

function initBillClients(hooks) {
  billClientsHooks = Object.assign({}, billClientsHooks, hooks);
}

function positionSelectHtml(billId, client) {
  const position = client.position || 'watch';
  const options = POSITIONS.map(([value, label]) =>
    `<option value="${value}" ${position === value ? 'selected' : ''}>${label}</option>`
  ).join('');
  // data-effective carries the date the current position took effect, so
  // an undo can put back both halves of what it replaced — reverting the
  // stance but leaving the new date behind would be a third state that
  // was never true.
  return `<select class="position-select ${position}" data-saved="${position}" data-effective="${client.effective_date || ''}" data-bill="${billId}" data-client="${client.id}" onchange="setPosition(${billId}, ${client.id}, this)" style="font-size:0.78rem;padding:0.3rem 0.5rem;font-weight:600">${options}</select>`;
}


// Two different jobs live in this cell: the chips change an *existing*
// assignment's position (or remove it); the select below them *adds a
// new* client. They used to look like two competing ways to do the same
// thing — see .client-chip/.add-client-select in STYLE for the visual
// fix.
// `options.showEffectiveDate` adds the "in force since" date under each
// chip, editable. Off by default because the flagged list is a dense
// table where the cell is already three controls wide; on for the bill
// report, which is the page someone is actually on when they think about
// when a position started.
function billClientCellHtml(billId, options) {
  const showEffectiveDate = !!(options && options.showEffectiveDate);
  const assigned = billClientsHooks.assignedClients(billId) || [];
  const allClients = billClientsHooks.allClients() || [];

  // The date sits under the chip, not inside it: the chip is a pill
  // (name, dropdown, remove ×) and a fourth control in the same row
  // squeezed the client's name onto two lines.
  const chips = assigned.map(c => `
    <div class="client-assignment">
      <div class="client-chip">
        <a href="/clients/detail?id=${c.id}">${titleCaseName(c.name)}</a>
        ${positionSelectHtml(billId, c)}
        <button type="button" class="icon-btn" onclick="unassignClient(${billId}, ${c.id}, this)" aria-label="Remove client from this bill" title="Remove client" style="height:1.5rem;width:1.5rem;color:var(--slate)">×</button>
      </div>
      ${showEffectiveDate ? effectiveDateHtml(billId, c) : ''}
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

// `undoOf` is the position this call is putting back, set only when
// setPosition is called from a toast's Undo. It suppresses the toast
// that would otherwise offer to undo the undo, and changes the wording
// to say what happened rather than offering a way out of it.
// When this position took effect, as opposed to when someone got round
// to entering it. Usually the same day; occasionally not, and "what was
// our position when we testified in June" is a question about the
// former. Set automatically on every change (see
// db.link_bill_to_client), editable here.
function effectiveDateHtml(billId, client) {
  const id = `effective-${billId}-${client.id}`;
  return `
    <span class="chip-effective">
      <label for="${id}">In force since</label>
      <input type="date" id="${id}" value="${client.effective_date || ''}"
             onchange="setEffectiveDate(${billId}, ${client.id}, this)">
    </span>
  `;
}

async function setEffectiveDate(billId, clientId, inputEl) {
  billClientsHooks.onError('');
  const client = (billClientsHooks.assignedClients(billId) || []).find(c => c.id === clientId);
  if (!client) return;
  const previous = inputEl.defaultValue;
  inputEl.disabled = true;
  try {
    const res = await fetch('/api/bill-clients', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bill_id: billId, client_id: clientId,
        position: client.position || 'watch',
        effective_date: inputEl.value,
      }),
    });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error || 'Could not save that date');
    }
    inputEl.defaultValue = inputEl.value;
    inputEl.disabled = false;
    showToast(`${titleCaseName(client.name)} — position in force since ${inputEl.value || 'no date'}.`);
    billClientsHooks.onChanged();
  } catch (err) {
    inputEl.value = previous;
    inputEl.disabled = false;
    billClientsHooks.onError(err.message);
  }
}

async function setPosition(billId, clientId, selectEl, undoOf) {
  billClientsHooks.onError('');
  const newPosition = selectEl.value;
  const savedPosition = selectEl.dataset.saved;
  const savedEffective = selectEl.dataset.effective || '';
  // Optimistic repaint so the picked color shows right away — reverted
  // in the catch below if the server rejects the change, so a failed
  // save can no longer look identical to a successful one.
  selectEl.className = 'position-select ' + newPosition;
  selectEl.disabled = true;
  try {
    const res = await fetch('/api/bill-clients', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bill_id: billId, client_id: clientId, position: newPosition,
        // Only sent by an undo, which is restoring a date rather than
        // setting one. A normal change leaves this out and lets the
        // server date it (see db.link_bill_to_client).
        effective_date: undoOf ? (undoOf.effectiveDate || '') : undefined,
      }),
    });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error || 'Could not update position');
    }
    selectEl.dataset.saved = newPosition;
    announcePositionChange(billId, clientId, savedPosition, savedEffective, newPosition, undoOf);
    billClientsHooks.onChanged();
  } catch (err) {
    selectEl.value = savedPosition;
    selectEl.className = 'position-select ' + savedPosition;
    selectEl.disabled = false;
    billClientsHooks.onError(err.message);
  }
}

// Say what just happened, and leave a way back for as long as anyone
// would plausibly notice. A position change used to be silent — no
// confirmation, no undo — on the control sitting an inch from an × that
// removes the client from the bill outright.
//
// The undo is a real change, recorded in position_history like any
// other. That's the honest record: the position genuinely was Oppose for
// twenty seconds, and a log that quietly erases it is a log that can be
// argued with.
function announcePositionChange(billId, clientId, fromPosition, fromEffective, toPosition, undoOf) {
  const client = (billClientsHooks.assignedClients(billId) || []).find(c => c.id === clientId);
  const name = client ? titleCaseName(client.name) : 'Client';
  const bill = billClientsHooks.billLabel(billId);
  const on = bill ? ` on ${bill}` : '';

  if (undoOf) {
    showToast(`Put back: ${name} is ${positionLabel(toPosition)}${on}.`);
    return;
  }
  showToast(`${name} set to ${positionLabel(toPosition)}${on}.`, {
    actionLabel: 'Undo',
    onAction: () => {
      // Re-read the select rather than closing over the element: the
      // page re-rendered after the change, so the node this ran from is
      // gone by now.
      const el = document.querySelector(`select.position-select[data-bill="${billId}"][data-client="${clientId}"]`);
      if (!el) return;
      el.value = fromPosition;
      setPosition(billId, clientId, el, { effectiveDate: fromEffective });
    },
  });
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
  const name = client ? titleCaseName(client.name) : 'this client';
  const bill = billClientsHooks.billLabel(billId) || 'this bill';
  // The cost, stated: which client, which bill, and what stance is being
  // dropped. "Remove this client from this bill?" named neither of the
  // two things whose link it was about to delete.
  const stance = client && client.position ? ` This drops their ${positionLabel(client.position)} position.` : '';
  const ok = await confirmDelete(
    'Remove client?',
    `Remove ${name} from ${bill}?${stance} The change stays in the position history.`
  );
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
