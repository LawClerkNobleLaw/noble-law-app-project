// hearing_time.js — one convention for printing a hearing's time, shared
// by the hearing calendar and the Upcoming hearings table on the bill
// report. Both used to print bill_hearings.time exactly as stored.
//
// That column is TEXT straight from LegiScan, which sends a 24-hour
// 'HH:MM' when it knows the time and '00:00' when it only knows the date.
// Printed literally, the second case rendered as "Hearing · 00:00" — a
// midnight Appropriations hearing, stated as fact, on the screen a user
// plans a Capitol trip from. The Legislature does not sit at midnight, so
// exactly 00:00 is read here as "not announced yet" rather than as a time.
//
// 12-hour because that's what every agenda the user reads says ("1:30
// p.m."), and what the app's own timestamps already use elsewhere. The
// zone is labelled once per page (the calendar subtitle, the report's
// column header) rather than repeated on every row — every hearing in
// this product is in California.

function hearingTimeLabel(time) {
  if (!time) return 'Time TBA';
  const match = /^(\d{1,2}):(\d{2})/.exec(time);
  if (!match) return time;  // Unrecognized shape — show what we were given.

  const hours = Number(match[1]);
  const minutes = match[2];
  // Only exactly midnight is treated as absent. 00:30 would be a real, if
  // implausible, time and is left to speak for itself.
  if (hours === 0 && minutes === '00') return 'Time TBA';

  const suffix = hours < 12 ? 'AM' : 'PM';
  const hour12 = hours % 12 === 0 ? 12 : hours % 12;
  return `${hour12}:${minutes} ${suffix}`;
}
