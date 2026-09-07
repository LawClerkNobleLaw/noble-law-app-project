/* position_history.js — rendering the append-only record of a client's
 * position on a bill (see position_history in schema.sql).
 *
 * Its own file rather than part of bill_clients.js because the two
 * places that show this history want different things: the bill report
 * also carries the live assignment control, while the client record just
 * wants the list. Making the client page load the whole assignment
 * control — and the quick-add panel behind it — to render a read-only
 * list would be paying for a dependency it never uses.
 *
 * Depends only on title_case.js (titleCaseName).
 */

// The one place support/oppose/watch turn into words for display. Kept
// here rather than in client_quickadd.js's POSITIONS (which is an
// ordered list built for <option> rendering) because two of the three
// pages that need the label don't need the picker.
const POSITION_LABELS = { support: 'Support', oppose: 'Oppose', watch: 'Watch' };

function positionLabel(value) {
  return POSITION_LABELS[value] || value;
}

/* ── The record, as opposed to the current answer ──────────────────────
 *
 * position_history is append-only (see schema.sql): bill_client_links
 * says what the position is, this says how it got there. Rendered on the
 * bill report (one bill, every client) and on the client record (one
 * client, every bill), which is why it takes a flag for which of the two
 * columns to leave out rather than being written twice.
 *
 * Rows survive the thing they describe. A client taken off a bill still
 * appears here, and so does a client deleted afterwards — the join that
 * fetches these is a LEFT JOIN for exactly that reason, so a missing
 * name degrades to "A deleted client" instead of dropping the row.
 */
function positionHistoryHtml(rows, options) {
  const showBill = !!(options && options.showBill);
  if (!rows || !rows.length) return '<p class="empty">No position changes recorded yet.</p>';
  return `<ol class="position-history">${rows.map(row => {
    const subject = showBill
      ? escapeText(`${row.state || ''} ${row.bill_number || 'a removed bill'}`.trim())
      : escapeText(titleCaseName(row.client_name || 'A deleted client'));
    const to = row.to_position
      ? `<span class="position-badge ${row.to_position}">${positionLabel(row.to_position)}</span>`
      : '<span class="position-badge removed">Removed from the bill</span>';
    // No arrow on the first entry: "→ Watch" with nothing on the left of
    // it reads as a missing value rather than as the beginning.
    const from = row.from_position
      ? `<span class="position-badge ${row.from_position} was">${positionLabel(row.from_position)}</span><span class="position-arrow" aria-hidden="true">→</span>`
      : '';
    const effective = row.effective_date ? `In force since ${fmtHistoryDate(row.effective_date)}` : '';
    const when = `Recorded ${fmtHistoryStamp(row.changed_at)}`;
    const who = row.changed_by_email ? ` by ${escapeText(row.changed_by_email)}` : '';
    return `
      <li class="position-history-row">
        <div class="position-history-what"><span class="position-history-subject">${subject}</span>${from}${to}</div>
        <div class="position-history-when">${[effective, when + who].filter(Boolean).join(' · ')}</div>
      </li>
    `;
  }).join('')}</ol>`;
}

// effective_date is a plain California date, and changed_at a UTC
// instant — the always-year date and the timestamp, both from dates.js
// (loaded globally by page() before this file). fmtHistoryStamp keeps
// its own missing-value wording so "changed <stamp>" still reads as a
// sentence when the stamp is absent.
function fmtHistoryDate(dateStr) {
  return fmtDate(dateStr);
}

function fmtHistoryStamp(iso) {
  return iso ? fmtStamp(iso) : 'at an unknown time';
}
