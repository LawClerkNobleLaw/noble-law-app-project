/* Keystrokes into search, from anywhere in the shell.
 *
 * There was a global search box in the topbar once, and it was removed
 * as redundant with /lookup. That reasoning still holds: the topbar
 * affordance (see app_shell) is a LINK shaped like a search field, not
 * an input — it navigates, it can be middle-clicked, and it works with
 * this file blocked. What was missing wasn't a box, it was a way in that
 * doesn't cost a trip to the mouse.
 *
 * Two bindings, and they mean two different scopes on purpose, so the
 * question "where do I search bills from here?" has one answer on every
 * page:
 *
 *   ⌘K / Ctrl-K  — the GLOBAL bill search, from every page. On /lookup
 *                  that box is right here, so focus it; anywhere else
 *                  (including /lobbying and /directory, which have their
 *                  own scoped boxes) go to it. Works even while typing,
 *                  since it is modified and can't be mistaken for text.
 *   /            — "search THIS page": the page's own scoped box if it
 *                  has one (/lobbying disclosures, /directory staff,
 *                  /flagged bills), else the global search as a fallback.
 *                  Only when the caret is not already in a field, or it
 *                  would eat the slash out of "Health and Safety 39617.2".
 *
 * The selectors are listed here rather than marked with an attribute in
 * each template on purpose: four templates edited for one keybinding is
 * four merge conflicts waiting for whichever branch lands second, and
 * this list is one line to extend when a fifth search page appears.
 */
const PAGE_SEARCH_SELECTOR = '#q, #flagged-search';
/* The global bill search only lives on /lookup; its box there is #q.
 * Gated on the path because #q is also the id of the *scoped* box on
 * /lobbying and /directory, and ⌘K must not mistake one of those for the
 * global search. */
const GLOBAL_SEARCH_PATH = '/lookup';

function pageSearchBox() {
  return document.querySelector(PAGE_SEARCH_SELECTOR);
}

function globalSearchBox() {
  return window.location.pathname === GLOBAL_SEARCH_PATH
    ? document.querySelector('#q')
    : null;
}

/* A shortcut that steals "/" mid-sentence is worse than no shortcut.
 * contentEditable is checked first because a rich-text host is a <div>,
 * so the tag test below would miss it. */
function isTypingIn(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
}

// Focus and select, not just focus: the fast second search is a different
// question, and having to clear the last one first is the friction these
// shortcuts exist to remove.
function focusSearch(box) {
  box.focus();
  box.select();
}

document.addEventListener('keydown', (e) => {
  const commandK = (e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k';
  const slash = e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey && !isTypingIn(document.activeElement);
  if (!commandK && !slash) return;
  e.preventDefault();

  if (commandK) {
    const global = globalSearchBox();
    if (global) { focusSearch(global); return; }
    window.location.href = '/lookup';
    return;
  }

  // slash: this page's own search, or the global one if it has none.
  const box = pageSearchBox();
  if (box) { focusSearch(box); return; }
  window.location.href = '/lookup';
});

/* Arriving at a search page with an empty box: the only thing to do
 * here is type, so start there. Not when the box already holds a query
 * — that page is showing an answer, and stealing focus to the question
 * would scroll it out from under the reader on a short screen. */
window.addEventListener('DOMContentLoaded', () => {
  const box = pageSearchBox();
  if (box && !box.value) box.focus();

  // The hint is rendered as the Mac key because the server can't know
  // which keyboard is reading it, and a hint that names a key the
  // visitor doesn't have is worse than none. Corrected here, where the
  // platform is knowable. userAgentData first, since `platform` is
  // deprecated and already lies in some browsers.
  const hint = document.querySelector('.topbar-search kbd');
  if (!hint) return;
  const platform = (navigator.userAgentData && navigator.userAgentData.platform)
    || navigator.platform || '';
  if (!/mac/i.test(platform)) hint.textContent = 'Ctrl K';
});
