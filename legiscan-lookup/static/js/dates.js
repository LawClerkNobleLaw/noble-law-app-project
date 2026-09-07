// dates.js — the three date formats this app uses, each defined once so
// the same kind of value reads the same on every page. Loaded globally
// by page() (before any template's own <script>, and before the shared
// position_history.js), so every page can call these.
//
//   fmtStamp     an instant — "Sep 6, 1:00 PM". Stored UTC (a trailing
//                Z, or a SQLite datetime('now') without one), shown in
//                the reader's own clock, so the Z is supplied when the
//                string doesn't already carry a zone.
//   fmtDate      a calendar date with the year always shown —
//                "Sep 3, 2025". For the dates worth being unambiguous
//                about: filing deadlines, and position history that can
//                span sessions.
//   fmtShortDate a calendar date with the year shown only when it isn't
//                the current one — "Aug 13" this year, "Aug 13, 2025"
//                otherwise. For current-session bill-action dates, where
//                the year is almost always the obvious one and only
//                costs width. The deliberate contrast fmtDate exists to
//                make ("unlike the flagged list").
//
// A bare date (YYYY-MM-DD) is parsed at local midnight, not UTC, so it
// can't slip to the day before for anyone west of Greenwich. A missing
// or unparseable value comes back as itself rather than "Invalid Date".

function fmtStamp(iso) {
  if (!iso) return '';
  const s = String(iso);
  const d = new Date(s.includes('Z') || s.includes('+') ? s : s + 'Z');
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function fmtDate(dateStr) {
  if (!dateStr) return '';
  const s = String(dateStr);
  const d = new Date(s.includes('T') ? s : s + 'T00:00:00');
  if (isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function fmtShortDate(dateStr) {
  if (!dateStr) return '';
  const s = String(dateStr);
  const d = new Date(s.includes('T') ? s : s + 'T00:00:00');
  if (isNaN(d.getTime())) return dateStr;
  const opts = { month: 'short', day: 'numeric' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString('en-US', opts);
}
