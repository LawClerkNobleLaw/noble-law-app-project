/* Shared history/amendment/hearing/vote row-rendering JS, used by both
 * LOOKUP_BODY (live LegiScan data from /api/bill) and REPORT_BODY (the
 * same table shapes from db.get_bill_report(), see legiscan_client.py's
 * shape_bill()) — interpolated into both pages' own <script> blocks via
 * app.py's BILL_TABLES_JS rather than hand-duplicated, so a future
 * change to any of these four tables has one place to edit instead of
 * two that have to be kept in sync by hand.
 *
 * Each function takes the already-extracted array (d.hearings vs
 * r.upcoming_hearings, etc.), not the whole bill/report object, since
 * the two pages don't even use the same field name for hearings.
 */

// A history table is easy to skim by date/chamber but hard to skim by
// WHAT HAPPENED — every row reads the same until you actually read it.
// This buckets each row into one of three real legislative milestones
// by keyword match on its own action text (LegiScan's own wording, not
// a separate field) so the colored left edge is scannable down the
// column instead. Keyword match, not a lookup table, because LegiScan's
// action text is free-form and these three phrasings cover the cases
// that matter here — a row that matches none of them just renders
// unstyled, same as before.
function milestoneClass(action) {
  const a = (action || '').toLowerCase();
  if (a.includes('introduced')) return 'milestone-intro';
  if (a.includes('chaptered') || a.includes('approved by the governor') || a.includes('passed')) return 'milestone-passed';
  if (a.includes('amended')) return 'milestone-amended';
  return '';
}

function historyRowsHtml(history) {
  return (history || []).slice().sort((a, b) => (b.date || '').localeCompare(a.date || '')).map(h =>
    `<tr class="${milestoneClass(h.action)}"><td class="date">${h.date || ''}</td><td class="chamber">${h.chamber || ''}</td><td>${h.action || ''}</td></tr>`
  ).join('');
}

function amendmentRowsHtml(amendments) {
  return (amendments || []).map(a => `
    <tr>
      <td class="date">${a.date || ''}</td>
      <td class="chamber">${a.chamber || ''}</td>
      <td>${a.title || a.description || ''}${a.adopted ? ' <span class="tag">Adopted</span>' : ''}${a.url ? ` — <a href="${a.url}" target="_blank" rel="noopener">View amended text →</a>` : ''}</td>
    </tr>
  `).join('');
}

function hearingRowsHtml(hearings) {
  return (hearings || []).map(h => `
    <tr>
      <td class="date">${h.date || ''}${h.time ? ' ' + h.time : ''}</td>
      <td class="chamber">${h.event_type || ''}</td>
      <td>${h.description || ''}${h.location ? ` — ${h.location}` : ''}</td>
    </tr>
  `).join('');
}

// No page had a Votes panel until Phase 1 added it here — LegiScan's
// own per-bill vote index (roll_call_id, chamber, tally), already
// broken out by shape_bill(), just wasn't surfaced in the UI before.
function voteRowsHtml(votes) {
  return (votes || []).map(v => `
    <tr>
      <td class="date">${v.date || ''}</td>
      <td class="chamber">${v.chamber || ''}</td>
      <td>
        ${v.description || ''}${v.passed ? ' <span class="tag">Passed</span>' : ''}
        <div class="sub" style="margin:0.2rem 0 0;font-size:0.78rem">
          Yea ${v.yea || 0} · Nay ${v.nay || 0} · NV ${v.nv || 0} · Absent ${v.absent || 0}
        </div>
      </td>
    </tr>
  `).join('');
}
