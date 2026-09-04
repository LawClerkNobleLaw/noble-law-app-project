/* page_progress.js — P2-32's "immediate pressed state on navigation".
 *
 * This app is a plain multi-page app on purpose (see app.py's own module
 * docstring: no framework, no client-side router) — every link click is
 * a real browser navigation, and nothing can paint DURING the network
 * round trip that follows, however long a cold Render instance takes to
 * answer it. What this CAN do: confirm the click landed, instantly, so
 * "a blank screen after clicking Report is indistinguishable from a
 * click that didn't register" (the audit's own words) stops being true.
 *
 * The bar starts filling the instant a same-tab internal link is
 * clicked — before the browser has done anything with that click — and
 * a sessionStorage flag carries the "still navigating" state across the
 * gap, so the NEXT page's own copy of this script picks the bar up
 * already partway full instead of starting over from zero. It clears
 * itself on that next page's 'load' event, or after a 4s safety timeout
 * if 'load' never fires (a download, a page that redirects itself, ...).
 *
 * Deliberately scoped to <a> clicks only, not form submits: every real
 * form in this app already shows its own inline pending state (a
 * disabled button reading "Saving…", see confirmFlagBill() and friends)
 * before its fetch() call — re-triggering this bar on the same submit
 * would either double up on that or, worse, arm the "still navigating"
 * flag for a fetch-based submit that never actually navigates, leaving
 * it to misfire on whatever link gets clicked next.
 */
(function () {
  var FLAG = 'page-progress-nav';
  var bar = document.getElementById('page-progress');
  if (!bar) return;

  function setWidth(pct, animated) {
    bar.style.transition = animated ? 'width 0.4s ease-out, opacity 0.15s ease-out' : 'none';
    bar.style.opacity = '1';
    bar.style.width = pct + '%';
  }

  function finish() {
    if (bar.style.opacity === '0') return; // already hidden — nothing to finish
    setWidth(100, true);
    setTimeout(function () {
      bar.style.transition = 'opacity 0.25s ease-out';
      bar.style.opacity = '0';
      setTimeout(function () { bar.style.width = '0%'; }, 260);
    }, 150);
  }

  var resuming = false;
  try {
    resuming = sessionStorage.getItem(FLAG) === '1';
    if (resuming) sessionStorage.removeItem(FLAG);
  } catch (err) {
    // Private-window sessionStorage can throw on read/write — treat it
    // the same as "nothing to resume" rather than letting this crash
    // page load for a purely cosmetic feature.
  }
  if (resuming) {
    setWidth(35, false); // pre-filled with no transition, so it reads as continuous
    requestAnimationFrame(function () { setWidth(80, true); });
  }

  window.addEventListener('load', finish);
  // Safety net: if 'load' never fires as expected, don't leave the bar
  // stuck full-width on this page forever.
  setTimeout(finish, 4000);
  // A page restored from the back/forward cache never re-runs this
  // script or fires 'load' again — finish() defensively either way.
  window.addEventListener('pageshow', function (e) { if (e.persisted) finish(); });

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // opening in a new tab/window
    var a = e.target.closest('a[href]');
    if (!a || a.target === '_blank' || a.hasAttribute('download')) return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' || href.indexOf('javascript:') === 0 || href.indexOf('mailto:') === 0) return;
    var url;
    try {
      url = new URL(href, window.location.href);
    } catch (err) {
      return;
    }
    if (url.origin !== window.location.origin) return;
    // A same-page hash link (or a link back to exactly where we are)
    // isn't a navigation this bar needs to announce.
    if (url.pathname === window.location.pathname && url.search === window.location.search) return;
    try {
      sessionStorage.setItem(FLAG, '1');
    } catch (err) {
      // Can't persist the flag — the bar just won't resume on the next
      // page, which is a smaller miss than crashing this click handler.
    }
    setWidth(20, true);
  }, true);
})();
