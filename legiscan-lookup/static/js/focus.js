/* The two halves of not losing the user's place, in one file because
 * they are the same job seen from two sides: a modal that opens must
 * take focus and give it back, and a region that redraws itself must
 * hand focus back to the control the user was standing on.
 *
 * Both existed here already, in exactly one place each. The drawer in
 * app_shell() traps and returns focus correctly (it is the only thing
 * in the app that did), and nothing anywhere restored focus across a
 * re-render. So this is an extraction, not an invention: the drawer's
 * pattern lifted out so the three modals — the flag dialog, quick-add
 * client, and the shared destructive-confirm — can call it instead of
 * each growing their own copy, and the missing counterpart written
 * once for the two filter rails that rebuild themselves wholesale.
 *
 * Loaded from page() rather than per-template, next to
 * page_progress.js: it is shell infrastructure, the shell's own drawer
 * is one of its callers, and being in <body> above {app_shell(...)}
 * is what guarantees it is defined before any page's inline script or
 * the shell's own runs.
 */

/* .focus() on a display:none element silently no-ops, so a collapsed
 * nav group or a closed account menu inside the trapped region used to
 * make first and last unreachable and break the wrap-around.
 * offsetParent is null for anything display:none'd, itself or by an
 * ancestor. Wider than the drawer's old 'a, button': a modal traps a
 * form, and the quick-add panel is six inputs before it is a button. */
function focusablesIn(el) {
  const selector = 'a[href], button, input, select, textarea, summary, [tabindex]:not([tabindex="-1"])';
  return Array.from(el.querySelectorAll(selector)).filter(
    (n) => n.offsetParent !== null && !n.disabled);
}

/* Move focus into `panel`, keep Tab inside it, and give focus back to
 * whatever had it when this was called. Returns the release function —
 * call it when the panel closes, however it closes (button, Escape,
 * backdrop click, or a successful submit).
 *
 * aria-modal="true" was already set on all three modals and does none
 * of this: it tells a screen reader the rest of the page is inert
 * while leaving the browser's tab order exactly as it was, so Tab
 * walked out into the page behind a backdrop that still blocked the
 * mouse.
 *
 * The listener is registered in the capture phase so a page's own
 * keydown handler can't consume Tab first, and the focusable list is
 * recomputed per keystroke rather than cached on open, since a panel
 * can enable, disable or reveal a control while it is open (the flag
 * modal disables its client <select> until /api/clients answers). */
function trapFocus(panel, initialFocus) {
  const restoreTo = document.activeElement;
  const onKeydown = (e) => {
    if (e.key !== 'Tab') return;
    const focusable = focusablesIn(panel);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    const inside = panel.contains(document.activeElement);
    if (e.shiftKey && (!inside || document.activeElement === first)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (!inside || document.activeElement === last)) {
      e.preventDefault();
      first.focus();
    }
  };
  document.addEventListener('keydown', onKeydown, true);
  const target = initialFocus || focusablesIn(panel)[0];
  if (target) target.focus();
  return function releaseFocus() {
    document.removeEventListener('keydown', onKeydown, true);
    // Only if it's still in the document and still rendered: the
    // trigger is often a row control the close itself just re-rendered
    // away, and .focus() on a detached node moves focus to <body>,
    // which is the very thing this is here to avoid. getClientRects()
    // rather than offsetParent here because a trigger can legitimately
    // be position:fixed (the topbar's own buttons), which reads as
    // null offsetParent while being perfectly visible.
    if (restoreTo && document.contains(restoreTo) && restoreTo.getClientRects().length) {
      restoreTo.focus();
    }
  };
}

/* Run `render`, which is expected to replace everything inside
 * `container`, and put focus back on the control the user was standing
 * on. Every candidate carries data-focus-key (see focusKeyAttr) —
 * matching on identity rather than position, since the rails reorder
 * and drop options as counts change.
 *
 * The fallbacks matter as much as the hit: removing an active-filter
 * chip destroys the very button that was focused, so a chip falls back
 * to its own filter tab, which is the same filter in its other form
 * and is always present. Anything else falls back to the first
 * focusable thing in the region — not ideal, but the alternative is
 * <body>, and re-tabbing from the top of the document after every
 * filter is what made the faceted rail unusable without a mouse. */
function keepFocus(container, render) {
  const restore = rememberFocus(container);
  render();
  restore();
}

/* keepFocus split in two, for the case where the redraw isn't one
 * synchronous call: /lookup empties its results the moment a search
 * starts and refills them a network round trip later, so the control
 * that was focused is already gone by the time anything renders.
 * Remember before the emptying, restore after the refill. */
function rememberFocus(container) {
  const active = document.activeElement;
  const key = active && container.contains(active) ? active.dataset.focusKey : null;
  return function restoreFocus() {
    if (!key) return;
    const find = (k) => container.querySelector('[data-focus-key="' + CSS.escape(k) + '"]');
    const back = find(key)
      || (key.startsWith('chip:') ? find('tab:' + key.slice('chip:'.length)) : null)
      || focusablesIn(container)[0];
    if (back) back.focus();
  };
}

/* The whole attribute, escaped, rather than a bare string: these keys
 * are built from client names and committee labels. This used to carry
 * its own copy of the escape, because the page-local escapeText()
 * helpers didn't escape quotes and a key lands in an attribute value;
 * escape_text.js does, so there is nothing left to hand-roll. */
function focusKeyAttr() {
  return 'data-focus-key="' + escapeText(Array.from(arguments).join(':')) + '"';
}
