/* Whole-row navigation for entity tables whose rows map to a detail page
 * — the bill search results (LOOKUP_BODY) and the client list
 * (CLIENTS_BODY). Opening a bill or a client is one of the most frequent
 * actions in the app, and only the small name/number link was ever the
 * target: the big bold title looked clickable but wasn't.
 *
 * The keyboard story is NOT this file's job. Each of these tables already
 * puts a real <a> in its primary cell, so the row is reachable by Tab and
 * activatable with Enter with an accessible name, exactly as it was — this
 * only widens the MOUSE target to the rest of the row. That division is
 * deliberate: a tr isn't a natural tab stop, and giving it role/tabindex
 * on top of the anchor it contains would announce every row twice.
 *
 * Delegation on a stable container, not a listener per row: both tables
 * re-render their <tbody> wholesale (a flag toggle, a filter, an edit),
 * so a per-row binding would be dropped on every repaint. The container
 * element itself persists, so one listener on it survives every redraw.
 * Opt a row in with class="link-row" and data-href="<detail url>".
 */
function enableRowNavigation(container) {
  if (!container || container._rowNavBound) return;
  container._rowNavBound = true;
  container.addEventListener('click', (e) => {
    // A click that lands on the row's own controls does that control's
    // job, never the row's: the checkbox, the Flag button, the
    // bill-number/title links (which navigate themselves), the row menu
    // and its Edit button. Anything interactive stays independent.
    if (e.target.closest('a, button, input, select, textarea, label, [role="button"]')) return;
    const row = e.target.closest('tr.link-row[data-href]');
    if (!row || !container.contains(row)) return;
    // A drag that happens to end inside the row is someone selecting
    // text, not asking to leave the page.
    const sel = window.getSelection && window.getSelection();
    if (sel && String(sel).length) return;
    const href = row.dataset.href;
    // Cmd/Ctrl/Shift/middle-click open a new tab on a real link; match
    // that here so the widened target behaves like the anchor it wraps.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) {
      window.open(href, '_blank', 'noopener');
    } else {
      window.location.href = href;
    }
  });
}
