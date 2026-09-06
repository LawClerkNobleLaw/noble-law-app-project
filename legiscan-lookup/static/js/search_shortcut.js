/* One keystroke to a search box, from anywhere in the shell.
 *
 * There was a global search box in the topbar once, and it was removed
 * as redundant: /lookup is one click away in the sidebar and does the
 * same job better, so a second always-visible box competing with it was
 * furniture. That reasoning still holds, and this doesn't undo it —
 * what was missing wasn't a box, it was a way in that doesn't cost a
 * trip to the mouse. The topbar affordance here is a LINK shaped like a
 * search field (see app_shell), not an input: it navigates, it can be
 * middle-clicked, and it works with this file blocked.
 *
 * Two bindings, both what people already have in their fingers:
 *
 *   ⌘K / Ctrl-K  — works even while typing, since it is modified and
 *                  can't be mistaken for text.
 *   /            — only when the caret is not in a field, or it would
 *                  eat the slash out of "Health and Safety 39617.2".
 *
 * Where it lands is the page's own search box if the page has one,
 * because "search from here" means this page's search on a page that
 * searches something — /lobbying searches lobbying disclosures and
 * /directory searches Capitol staff, and sending either of those to
 * /lookup would be the shortcut arguing with the page.
 *
 * The selectors are listed here rather than marked with an attribute in
 * each template on purpose: four templates edited for one keybinding is
 * four merge conflicts waiting for whichever branch lands second, and
 * this list is one line to extend when a fifth search page appears.
 */
const PAGE_SEARCH_SELECTOR = '#q, #flagged-search';

function pageSearchBox() {
  return document.querySelector(PAGE_SEARCH_SELECTOR);
}

/* A shortcut that steals "/" mid-sentence is worse than no shortcut.
 * contentEditable is checked first because a rich-text host is a <div>,
 * so the tag test below would miss it. */
function isTypingIn(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
}

document.addEventListener('keydown', (e) => {
  const commandK = (e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k';
  const slash = e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey && !isTypingIn(document.activeElement);
  if (!commandK && !slash) return;
  const box = pageSearchBox();
  if (box) {
    e.preventDefault();
    box.focus();
    // Selected, not just focused: the fast second search is a different
    // question, and having to clear the last one first is the friction
    // this shortcut exists to remove.
    box.select();
    return;
  }
  e.preventDefault();
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
