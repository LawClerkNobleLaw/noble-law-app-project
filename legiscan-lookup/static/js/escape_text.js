/* One definition of "put this string into HTML without letting it
 * become HTML", for the whole app.
 *
 * There were nine, byte-for-byte identical, one per page that
 * remembered — and eight pages that build rows with innerHTML and
 * didn't. Those eight interpolate LegiScan titles and committee
 * descriptions and user-typed client names straight into markup, so a
 * client called "Smith & Jones" renders as "Smith &amp;amp; Jones" or
 * worse, and a name with a "<" in it silently eats the rest of the
 * row. On a compliance product this is a display-integrity problem
 * before it is anything else: the whole point of the flagged list is
 * that it says exactly what the record says.
 *
 * Quotes are escaped here and weren't in the nine copies. Every one of
 * them was written for text between tags, but the same values also
 * land in title=, aria-label= and data- attributes all over these
 * pages, where an unescaped " ends the attribute early. In a text node
 * &quot; renders as " anyway, so escaping it costs nothing and closes
 * the case the copies didn't cover.
 *
 * Loaded from page() next to focus.js and page_progress.js rather than
 * per template: every page that renders a row needs it, and being in
 * <body> above app_shell()'s output is what guarantees it is defined
 * before any page's own inline script runs.
 */

function escapeText(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
