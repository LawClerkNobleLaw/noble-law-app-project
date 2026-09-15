/* tooltip.js — one floating label for the app's icon-only controls,
 * shown on hover AND on keyboard focus.
 *
 * The buttons that need it (delete ×, the ⋮ row menus, modal Close)
 * already carry an aria-label, so a screen reader has always named
 * them; this is the sighted-but-uncertain case — the bare "×" on a
 * letter row that could read as either "delete" or "dismiss". The
 * native `title` attribute solves half of it and only half: browsers
 * pop a title on mouse hover but never on keyboard focus, so a
 * Tab-only user got nothing. This shows the same text both ways.
 *
 * Sourced from `data-tooltip` if present, else the element's own
 * `aria-label` — so every already-labelled icon button is covered with
 * no per-button markup, and the tooltip can never silently disagree
 * with the accessible name. `title` is deliberately dropped from those
 * buttons in the templates: leaving it would stack the OS tooltip on
 * top of this one on hover.
 *
 * One element, reused, appended to <body> and position:fixed — the same
 * reason toast.js lives on <body> rather than inside the page. These
 * buttons sit in table cells whose overflow-x:auto wrapper also clips
 * overflow-y (see .row-menu-dropdown.open-up in STYLE, and the clipping
 * note in row_menu.js); a tooltip rendered inside that box would be cut
 * off. Fixed-positioned off <body>, it clears every wrapper.
 *
 * Loaded once from page() next to focus.js/toast.js rather than
 * per-template: it is shell infrastructure that watches the whole
 * document through delegated listeners, and being defined before any
 * page's inline script is what lets it cover rows those scripts render
 * later.
 */

const TOOLTIP_SELECTOR = '.icon-btn, .row-menu-btn, [data-tooltip]';

let tooltipEl = null;
let tooltipFor = null;   // the control the tooltip currently describes

function tooltipText(el) {
  const t = el.getAttribute('data-tooltip') || el.getAttribute('aria-label') || '';
  return t.trim();
}

function ensureTooltipEl() {
  if (tooltipEl) return tooltipEl;
  tooltipEl = document.createElement('div');
  tooltipEl.className = 'app-tooltip';
  // Presentational only. The control it points at is already named for
  // assistive tech by its own aria-label, so announcing this too would
  // just double every label; role="tooltip"/aria-describedby would do
  // exactly that here and buys nothing.
  tooltipEl.setAttribute('aria-hidden', 'true');
  tooltipEl.hidden = true;
  document.body.appendChild(tooltipEl);
  return tooltipEl;
}

function hideTooltip() {
  tooltipFor = null;
  if (tooltipEl) {
    tooltipEl.hidden = true;
    tooltipEl.classList.remove('show', 'below');
  }
}

function showTooltipFor(el) {
  const text = tooltipText(el);
  if (!text) return;
  const tip = ensureTooltipEl();
  tooltipFor = el;
  tip.textContent = text;
  tip.hidden = false;

  // Measure, then place centered above the control; drop below when
  // there isn't room above (a button near the top of the viewport).
  const r = el.getBoundingClientRect();
  const gap = 8;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let top = r.top - th - gap;
  const below = top < 4;
  if (below) top = r.bottom + gap;
  let left = r.left + r.width / 2 - tw / 2;
  // Keep it on screen horizontally rather than letting a right-edge
  // button push it off the viewport.
  left = Math.max(4, Math.min(left, window.innerWidth - tw - 4));
  tip.style.top = `${Math.round(top)}px`;
  tip.style.left = `${Math.round(left)}px`;
  tip.classList.toggle('below', below);
  tip.classList.add('show');
}

/* Hover. mouseover/mouseout bubble (mouseenter/leave don't), so one
 * pair of delegated listeners on the document covers every row a page
 * renders now or later. */
document.addEventListener('mouseover', (e) => {
  const el = e.target.closest && e.target.closest(TOOLTIP_SELECTOR);
  if (el && el !== tooltipFor) showTooltipFor(el);
});
document.addEventListener('mouseout', (e) => {
  const el = e.target.closest && e.target.closest(TOOLTIP_SELECTOR);
  // Moving within the same control (onto its glyph) isn't a leave.
  if (el && el === tooltipFor && !el.contains(e.relatedTarget)) hideTooltip();
});

/* Keyboard focus — the half native title never did. Guarded by
 * :focus-visible so a mouse click, which also focuses the button and
 * then usually moves focus into a dialog, doesn't leave a tooltip
 * hanging; where :focus-visible isn't supported, showing on plain
 * focus is the safe fallback. */
document.addEventListener('focusin', (e) => {
  const el = e.target.closest && e.target.closest(TOOLTIP_SELECTOR);
  if (!el) return;
  let keyboard = true;
  try { keyboard = el.matches(':focus-visible'); } catch (_) { keyboard = true; }
  if (keyboard) showTooltipFor(el);
});
document.addEventListener('focusout', (e) => {
  const el = e.target.closest && e.target.closest(TOOLTIP_SELECTOR);
  if (el && el === tooltipFor) hideTooltip();
});

// A tooltip pinned to a viewport coordinate goes stale the moment the
// page scrolls or resizes under it, and it should never outlive an
// Escape. Capture-phase scroll so it catches scrolling inside any
// container, not just the window.
document.addEventListener('scroll', hideTooltip, true);
window.addEventListener('resize', hideTooltip);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideTooltip(); });
