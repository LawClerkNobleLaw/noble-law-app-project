/* Shared "⋮" row-menu behavior (open/close/Escape/click-outside) for
 * every row-menu-dropdown in the app (Flagged Bills, Clients, Client
 * detail, Disclosures — each has a table of rows plus, on Client
 * detail, one menu that isn't in a table at all). Used to be pasted
 * four times, byte-for-byte identical, one copy per page body — the
 * same "same shared page-chrome behavior, one definition" reasoning
 * confirm_delete.js and account_widget()/app_shell() already follow,
 * just not applied here until now.
 *
 * A tempting-looking "fix" was tried and deliberately reverted here: on
 * a very short table (as few as one row), flipping .open-up can push
 * the menu above the table's own top edge, floating it over the
 * panel-head — misplaced-looking, but still fully visible/clickable
 * (confirmed: .panel's overflow:hidden never actually clips it, it just
 * renders above the header row instead of below it). Adding a check to
 * fall back to opening downward instead, whenever upward would do that,
 * seemed like the fix — but downward is exactly what triggered the
 * upward flip in the first place, so falling back to it doesn't avoid a
 * problem, it un-avoids the ORIGINAL one: the table's overflow-x:auto
 * wrapper clips overflow-y too (see .row-menu-dropdown.open-up in
 * static/style.css), and reverting to "down" on a table too short to
 * clear either direction lands the menu fully outside the wrapper's
 * clipped box — invisible and unusable, confirmed by screenshot.
 * Misplaced-but-usable beats invisible, so the flip stays unconditional
 * on bottom-overflow.
 */

function closeRowMenus() {
  document.querySelectorAll('.row-menu-dropdown.show').forEach(m => {
    // Whether focus was inside the menu decides whether closing it owes
    // the user a place to stand. Closing because they clicked something
    // else on the page must not yank focus off whatever they clicked;
    // closing a menu they were standing in must not drop them on <body>.
    const heldFocus = m.contains(document.activeElement);
    m.classList.remove('show', 'open-up');
    const openBtn = document.querySelector(`[aria-controls="${m.id}"]`);
    if (!openBtn) return;
    openBtn.setAttribute('aria-expanded', 'false');
    if (heldFocus) openBtn.focus();
  });
}
function toggleRowMenu(e, key) {
  e.stopPropagation();
  const menu = document.getElementById(`row-menu-${key}`);
  const wasOpen = menu.classList.contains('show');
  closeRowMenus();
  if (!wasOpen) {
    menu.classList.add('show');
    e.currentTarget.setAttribute('aria-expanded', 'true');
    // See .row-menu-dropdown.open-up in STYLE — flip upward instead of
    // down whenever opening downward would get clipped by the table's
    // own overflow-x:auto wrapper, which the last row always would.
    const table = e.currentTarget.closest('table');
    if (table && menu.getBoundingClientRect().bottom > table.getBoundingClientRect().bottom) {
      menu.classList.add('open-up');
    }
    // Focus moves into the menu, so Tab walks its items rather than the
    // rest of the row. On /flagged the ⋮ sits inside the Bill cell (a
    // deliberate placement — see FLAGGED_BODY), which put "Bill report"
    // and "LegiScan ↗" between the trigger and the menu it just opened.
    // closeRowMenus() above is the other half: it hands focus back.
    const firstItem = menu.querySelector('a, button');
    if (firstItem) firstItem.focus();
  }
}
document.addEventListener('click', closeRowMenus);
// Escape closes whichever row menu is open; closeRowMenus() returns
// focus to its trigger, matching the standard disclosure-menu keyboard
// pattern.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!document.querySelector('.row-menu-dropdown.show')) return;
  closeRowMenus();
});
