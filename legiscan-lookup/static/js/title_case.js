/* Small display-only helper for showing client/org names consistently.
 * c.name (or b.name — same field, different call sites) holds two very
 * different kinds of value with nothing in the data telling them apart:
 * a name typed in by hand, already well-cased ("Anthropic PBC",
 * "O'Brien Consulting"), and a name autofilled from CAL-ACCESS, which
 * comes through exactly as the source record has it — typically
 * ALL-CAPS ("BRICKLAYERS, TILESETTERS & ALLIED CRAFTWORKERS LOCAL 3
 * UNION"). Reshaping every name unconditionally would fix the second
 * case but wreck the first ("O'Brien" -> "O'brien", "McDonald Group" ->
 * "Mcdonald Group"), so titleCaseName() gates the whole transform on
 * the input being ALL-CAPS already (letters only — digits/punctuation/
 * whitespace have no case to check); anything not already shouting —
 * including a name that's merely got a lowercase letter somewhere in
 * it — passes through completely untouched. This is deliberately a
 * display-only cosmetic fix: call it only where a name is rendered as
 * a label, never on the stored value, an editable field's value, or
 * anything used as a lookup/link key (see each call site's own comment
 * for why that spot is safe).
 */

const TITLE_CASE_LOWER = new Set(['AND', 'OF', 'THE']);
const TITLE_CASE_KEEP_UPPER = new Set(['LLC', 'PBC', 'LLP', 'LLLP', 'INC', 'CORP', 'CO', 'LP', 'PAC', 'USA']);

// Title-cases one "word" — really just a whitespace-delimited chunk,
// so it may carry attached punctuation like the comma in
// "BRICKLAYERS," or the ampersand standing alone as "&". `core` strips
// that off (letters only) so it can be checked against the acronym
// and connector-word lists without the punctuation getting in the
// way, but the transform itself runs on `raw` so anything non-letter
// stays exactly where it was — toUpperCase()/toLowerCase() are no-ops
// on digits/punctuation, which is what makes "LOCAL 3 UNION" ->
// "Local 3 Union" and "&" -> "&" work with no special-casing. Hyphens
// split into their own segments ("SMITH-JONES" -> "Smith-Jones");
// apostrophes deliberately do NOT ("O'BRIEN" -> "O'brien", not
// "O'Brien" — there's no reliable way to tell a name-particle
// apostrophe from any other kind without a name-specific exception
// list, so this keeps the simple, safe rule instead of guessing).
function titleCaseWord(raw, isEdgeWord) {
  const core = raw.replace(/[^A-Za-z]/g, '');
  if (TITLE_CASE_KEEP_UPPER.has(core)) return raw.toUpperCase();
  if (!isEdgeWord && TITLE_CASE_LOWER.has(core)) return raw.toLowerCase();
  return raw.split('-').map((segment) => {
    let seenLetter = false;
    return segment.replace(/[A-Za-z]/g, (ch) => {
      if (seenLetter) return ch.toLowerCase();
      seenLetter = true;
      return ch.toUpperCase();
    });
  }).join('-');
}

// See the comment above this block's own TITLE_CASE_JS constant for
// why this only touches a name that's ALL-CAPS already, and only ever
// as a display label.
function titleCaseName(name) {
  if (!name) return name;
  // Any lowercase letter at all means this isn't a shouting
  // CAL-ACCESS name to begin with (or already got fixed by hand) —
  // leave it alone. No uppercase letter either means there's nothing
  // with a case to reshape (pure digits/punctuation, or empty).
  if (!/[A-Z]/.test(name) || /[a-z]/.test(name)) return name;
  // Split on whitespace but keep the whitespace runs themselves (the
  // odd-indexed entries) so original spacing survives exactly, rather
  // than being normalized to single spaces.
  const tokens = name.split(/(\\s+)/);
  const wordIdxs = [];
  tokens.forEach((t, i) => { if (i % 2 === 0 && t !== '') wordIdxs.push(i); });
  const firstIdx = wordIdxs[0];
  const lastIdx = wordIdxs[wordIdxs.length - 1];
  return tokens.map((t, i) => {
    if (i % 2 !== 0 || t === '') return t;
    return titleCaseWord(t, i === firstIdx || i === lastIdx);
  }).join('');
}
