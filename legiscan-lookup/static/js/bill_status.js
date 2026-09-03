// bill_status.js — one status vocabulary for every screen that prints
// where a bill stands.
//
// There used to be two. The flagged list, the bill report and a client's
// bill table all print bills.status_label, which is LegiScan's coded
// getBill status run through STATUS_LABELS in legiscan_client.py:
// Prefiled / Introduced / Engrossed / Enrolled / Passed / Vetoed /
// Failed. The search page couldn't — getSearch's rows carry no status
// code at all (checked against a live response; see _shape_search_rows)
// — so it had grown its own two-word vocabulary out of the last-action
// text: everything was "In progress" or "Passed". An enrolled bill and a
// bill still in its first committee read identically, which made the
// coarse label useless, and the same bill saying "In progress" here and
// "Enrolled" one click away undermined both.
//
// So: one vocabulary, defined once, below. Where the coded status is
// available it is used directly. Where only the action text exists, that
// text is mapped into these same words and the badge says out loud that
// it was inferred — a dotted underline and a tooltip — rather than
// quietly presenting a guess in the same register as a known fact.
//
// The definitions matter as much as the words. Engrossed versus enrolled
// is a genuine trap even for people who work in the building, and this
// app had no glossary for it while Organization Search happily defines
// Firm / Employer / Coalition inline. Same treatment here.

const BILL_STATUS_DEFINITIONS = {
  'Prefiled': 'Filed before the session formally opened. Not yet introduced.',
  'Introduced': 'Formally introduced and waiting on its first policy committee.',
  'Engrossed': 'Passed its house of origin and moved to the second house.',
  'Enrolled': 'Passed both houses. Sent to the Governor to sign or veto.',
  'Passed': 'Signed or allowed to become law, and chaptered.',
  'Vetoed': 'Rejected by the Governor. A veto can still be overridden.',
  'Failed': 'Dead — in committee, on the floor, or at a session deadline.',
  'In progress': 'Somewhere between introduction and a floor vote. Open the bill for its exact stage.',
};

// The order these read in the legend: the path a bill takes, then the
// three ways it can end.
const BILL_STATUS_ORDER = [
  'Prefiled', 'Introduced', 'Engrossed', 'Enrolled', 'Passed', 'Vetoed', 'Failed', 'In progress',
];

// Last-action text -> the vocabulary above. Ordered most-final first: a
// chaptered bill's action line often still mentions the house it came
// from, so the endings have to win over the way-points.
//
// These are California's own action phrasings. "In Assembly. Read first
// time. Held at Desk." is what a Senate bill's action line says the day
// after it clears the Senate, which is exactly engrossment; the word
// "engrossed" itself rarely appears in the text.
const ACTION_PATTERNS = [
  [/chaptered|approved by the governor|signed by the governor/, 'Passed'],
  [/vetoed/, 'Vetoed'],
  [/died|failed|held under submission|withdrawn from/, 'Failed'],
  [/enrolled|presented to the governor/, 'Enrolled'],
  [/in assembly|in senate|ordered to the assembly|ordered to the senate/, 'Engrossed'],
  [/introduced|read first time|from printer/, 'Introduced'],
];

// Returns { label, inferred }. inferred=true means this came from the
// action text rather than from a status field, and the badge should say
// so. An unmatched action falls back to 'In progress' — deliberately
// still in the vocabulary, and defined in the glossary as the
// don't-know it actually is, rather than being upgraded to a specific
// stage the text doesn't support.
function billStatusFromAction(lastAction) {
  const text = (lastAction || '').toLowerCase();
  for (const [pattern, label] of ACTION_PATTERNS) {
    if (pattern.test(text)) return { label, inferred: true };
  }
  return { label: 'In progress', inferred: true };
}

function escapeAttr(text) {
  return String(text == null ? '' : text).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

// The one status pill. `inferred` adds the dotted underline and says so
// in the tooltip; every pill gets the glossary definition as its title,
// which is the hover explanation the terms never had.
function statusBadgeHtml(label, options) {
  if (!label) return '';
  const inferred = !!(options && options.inferred);
  const definition = BILL_STATUS_DEFINITIONS[label] || '';
  const note = inferred ? ' Inferred from the bill’s latest action — open the bill for its recorded status.' : '';
  const title = escapeAttr((definition + note).trim());
  return `<span class="status-badge${inferred ? ' inferred' : ''}"${title ? ` title="${title}"` : ''}>${escapeAttr(label)}</span>`;
}

// A legend at the foot of any list that uses the pills. Only the labels
// actually on screen, so a list of three enrolled bills doesn't explain
// four words nobody saw. `labels` is any iterable of status strings.
function statusLegendHtml(labels) {
  const present = new Set(Array.from(labels || []).filter(Boolean));
  const shown = BILL_STATUS_ORDER.filter(label => present.has(label));
  if (!shown.length) return '';
  const items = shown.map(label => `
    <div class="status-legend-item"><span class="status-legend-term">${escapeAttr(label)}</span>${escapeAttr(BILL_STATUS_DEFINITIONS[label])}</div>
  `).join('');
  return `<div class="status-legend"><div class="status-legend-head">What these mean</div>${items}</div>`;
}
