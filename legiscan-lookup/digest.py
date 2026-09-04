"""
digest.py — builds and sends the daily "what changed" email.

Called once a day by refresh_watchlist.py, right after it finishes
refreshing every watched bill and has collected a
{bill_id: [change description, ...]} map via
db.snapshot_bill_state()/db.diff_bill_state(). This module's only job is
turning that map into one email per affected user — it doesn't fetch
anything from LegiScan or touch bill data itself; see mailer.py for how
the email actually gets sent.

One digest per user per day, and only for users who actually have news:
a change on one of THEIR flagged bills, or a bill that started matching
one of their saved searches. Someone with neither gets nothing at all,
not an empty "no changes today" email.

The two halves answer different questions. The flagged-bill half is
"what moved on the bills you're watching"; the saved-search half is
"here is a bill you weren't watching" — the only part of this product
that can tell a user about a bill nobody has flagged yet.

Both halves are now subject to the recipient's own settings
(db.get_notification_prefs): how often, which of the four change types,
whether the saved-search half is wanted at all, which bills are muted,
and who else gets cc'd. Those settings live on Profile and are linked
from the footer of every email this module sends, which is the only
place a recipient who isn't the account holder can find them.

The defaults reproduce this module's pre-settings behaviour exactly, so
an account that never touches Profile gets the same mail it always did.
"""

import html
from datetime import date, timedelta

import config
import db
import mailer

# Where the "View action report" links point — set APP_BASE_URL to the
# real hosted URL (e.g. https://legiscan-lookup.onrender.com) when
# deploying; defaults to the local dev server so links still work when
# testing by hand.
APP_BASE_URL = config.APP_BASE_URL


def _report_url(bill_id):
    return f"{APP_BASE_URL}/report?bill_id={bill_id}"

SETTINGS_URL = f"{APP_BASE_URL}/profile#notifications"

# How many days back a weekly roll-up reaches. Six, not seven: the send
# lands on a Monday and the previous send was the Monday before, so
# Tuesday-through-today is exactly the gap with no day counted twice.
WEEKLY_LOOKBACK_DAYS = 6


def _is_send_day(frequency, today):
    """Is `today` (ISO 'YYYY-MM-DD', California — see
    db.today_in_california) a day this frequency sends on?

    The refresh job runs every day regardless; this is the only thing
    that decides whether a given recipient hears from it. 'weekly' sends
    on Monday, which is when the week's hearings and deadlines are
    actually being planned around.

    An unparseable date falls through to sending, deliberately: a
    malformed clock should not silently stop somebody's compliance mail.
    """
    if frequency == "off":
        return False
    if frequency == "daily":
        return True
    try:
        weekday = date.fromisoformat(today).weekday()   # Monday == 0
    except (TypeError, ValueError):
        return True
    if frequency == "weekdays":
        return weekday < 5
    if frequency == "weekly":
        return weekday == 0
    return True


def _weekly_window_start(today):
    try:
        return (date.fromisoformat(today) - timedelta(days=WEEKLY_LOOKBACK_DAYS)).isoformat()
    except (TypeError, ValueError):
        return today


def build_user_digest(conn, user_id, changes_by_bill, prefs=None):
    """Returns {"subject", "text", "html", "match_ids"} for this user's
    digest, or None if they have no news today.

    match_ids is what the caller marks reported once the send actually
    succeeded — see send_all_digests. Kept out of the email body and
    returned alongside it so an SMTP outage postpones the news instead of
    losing it.

    Three of the recipient's settings apply here rather than at the send:
    which change types count as news, whether a bill is muted, and
    whether the saved-search half is wanted. All three can empty the
    digest out entirely, which returns None — the same "no news, no
    email" answer as having nothing move at all. Muting a bill therefore
    really does stop the mail, instead of producing a shorter email that
    still arrives."""
    if prefs is None:
        prefs = db.get_notification_prefs(conn, user_id)
    allowed_types = set(prefs["event_types"])
    muted_bill_ids = db.list_digest_muted_bill_ids(conn, user_id)

    flagged_ids = db.list_flagged_bill_ids_for_user(conn, user_id)
    kept_changes = {}
    for bill_id in flagged_ids:
        if bill_id in muted_bill_ids:
            continue
        kept = [c for c in (changes_by_bill.get(bill_id) or [])
                if c["change_type"] in allowed_types]
        if kept:
            kept_changes[bill_id] = kept
    relevant_bill_ids = sorted(kept_changes)

    new_matches = db.list_unreported_matches(conn, user_id) if prefs["include_matches"] else []
    if not relevant_bill_ids and not new_matches:
        return None

    rows = []
    for bill_id in relevant_bill_ids:
        bill = db.get_bill_basic(conn, bill_id)
        if not bill:
            continue
        rows.append({
            "label": f"{bill['state']} {bill['bill_number']}",
            "title": bill.get("title") or "",
            # One line per bill, even if multiple change types fired —
            # each individual change from diff_bill_state() is already a
            # full sentence, so joining with a space reads as continuous
            # prose rather than a run-on. Those changes are dicts now (the
            # flagged list needs the parts separately); the email still
            # only wants the sentence.
            "summary": " ".join(c["description"] for c in kept_changes[bill_id]),
            "url": _report_url(bill_id),
        })
    if not rows and not new_matches:
        return None

    match_count = sum(len(g["matches"]) for g in new_matches)
    match_ids = [m["id"] for g in new_matches for m in g["matches"]]

    plural = "s" if len(rows) != 1 else ""
    if rows and match_count:
        subject = (f"Rotunda: {len(rows)} update{plural} on your flagged bills, "
                   f"{match_count} new bill{'s' if match_count != 1 else ''} matching your searches")
    elif rows:
        subject = f"Rotunda: {len(rows)} update{plural} on your flagged bills"
    else:
        subject = (f"Rotunda: {match_count} new bill{'s' if match_count != 1 else ''} "
                   "matching your saved searches")

    text_lines = []
    if rows:
        text_lines += [f"{len(rows)} of your flagged bills changed today:", ""]
        for r in rows:
            text_lines.append(f"{r['label']} — {r['title']}")
            text_lines.append(f"  {r['summary']}")
            text_lines.append(f"  {r['url']}")
            text_lines.append("")
    for group in new_matches:
        heading = f"New bills matching \u201c{group['name']}\u201d"
        if group["client_name"]:
            heading += f" (for {group['client_name']})"
        text_lines += [heading + ":", ""]
        for match in group["matches"]:
            text_lines.append(f"{match['bill_number'] or 'Bill'} — {match['title'] or ''}")
            if match["last_action"]:
                text_lines.append(f"  {match['last_action']}")
            text_lines.append(f"  {_report_url(match['bill_id'])}")
            text_lines.append("")
    # Every email says where the settings are, in both parts. A cc'd
    # assistant who wants off this list has no account to log into and
    # no other way to find out that this is adjustable at all.
    text_lines += ["", "—", f"Change what this email covers, or turn it off: {SETTINGS_URL}"]
    text_body = "\n".join(text_lines).rstrip() + "\n"

    html_rows = "".join(
        f"""
      <tr>
        <td style="padding:14px 0;border-bottom:1px solid #e5e5e5">
          <div style="font-family:ui-monospace,monospace;font-size:12px;color:#6b6b6b;margin-bottom:2px">{html.escape(r['label'])}</div>
          <div style="font-size:15px;font-weight:700;color:#171717;margin-bottom:6px">{html.escape(r['title'])}</div>
          <div style="font-size:14px;color:#6b6b6b;margin-bottom:8px">{html.escape(r['summary'])}</div>
          <a href="{r['url']}" style="font-size:13px;color:#6b6b6b;text-decoration:none;font-weight:600">View action report &rarr;</a>
        </td>
      </tr>"""
        for r in rows
    )
    # The saved-search half, as its own section per search. Visually the
    # same row shape as the flagged-bill rows above — the difference that
    # matters is stated in the heading ("New bills matching ..."), not in
    # a second visual language.
    match_sections = ""
    for group in new_matches:
        heading = f"New bills matching \u201c{html.escape(group['name'])}\u201d"
        if group["client_name"]:
            heading += f" &middot; for {html.escape(group['client_name'])}"
        section_rows = "".join(
            f"""
      <tr>
        <td style="padding:14px 0;border-bottom:1px solid #e5e5e5">
          <div style="font-family:ui-monospace,monospace;font-size:12px;color:#6b6b6b;margin-bottom:2px">{html.escape(match['bill_number'] or '')}</div>
          <div style="font-size:15px;font-weight:700;color:#171717;margin-bottom:6px">{html.escape(match['title'] or '')}</div>
          {f'<div style="font-size:14px;color:#6b6b6b;margin-bottom:8px">{html.escape(match["last_action"])}</div>' if match['last_action'] else ''}
          <a href="{_report_url(match['bill_id'])}" style="font-size:13px;color:#6b6b6b;text-decoration:none;font-weight:600">Open the bill report &rarr;</a>
        </td>
      </tr>"""
            for match in group["matches"]
        )
        match_sections += f"""
    <h2 style="font-size:15px;margin:2rem 0 0.25rem">{heading}</h2>
    <table style="width:100%;border-collapse:collapse">{section_rows}</table>"""

    flagged_section = f"""
    <h1 style="font-size:20px;margin:0 0 1.25rem">{len(rows)} update{plural} on your flagged bills</h1>
    <table style="width:100%;border-collapse:collapse">{html_rows}</table>""" if rows else ""

    html_body = f"""<!doctype html>
<html><body style="margin:0;background:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#171717">
  <div style="max-width:36rem;margin:0 auto;padding:2rem 1.25rem">
    <div style="font-size:13px;color:#6b6b6b;text-transform:uppercase;letter-spacing:0.03em;font-weight:700;margin-bottom:0.5rem">Rotunda — Daily Digest</div>
    {flagged_section}
    {match_sections}
    <p style="font-size:12px;color:#6b6b6b;margin-top:2rem">
      You're getting this because you have bills flagged or searches saved in Rotunda.
      Manage what you're tracking at
      <a href="{APP_BASE_URL}/flagged" style="color:#6b6b6b">{APP_BASE_URL}/flagged</a>
      and <a href="{APP_BASE_URL}/lookup" style="color:#6b6b6b">{APP_BASE_URL}/lookup</a>.
    </p>
    <p style="font-size:12px;color:#6b6b6b;margin-top:0.5rem">
      <a href="{SETTINGS_URL}" style="color:#6b6b6b;font-weight:600">Digest settings</a>
      &mdash; change the frequency, pick which kinds of change count as news,
      mute a bill, or turn this email off.
    </p>
  </div>
</body></html>"""

    return {"subject": subject, "text": text_body, "html": html_body, "match_ids": match_ids}


def send_all_digests(conn, changes_by_bill, today=None):
    """Runs once, at the end of the daily refresh (see
    refresh_watchlist.py). Returns a summary dict for the caller to log:
    sent (actually delivered), not_configured (would-have-sent but SMTP
    isn't set up — see mailer.py), skipped (no news), off (not a send day
    for that recipient's chosen frequency), and errors (a real send
    failure for that one recipient).

    No longer returns early on an empty changes_by_bill: a user can have
    nothing move on their flagged bills and still have a new saved-search
    match, which is the case saved searches exist for.

    changes_by_bill is what the refresh job just found, which is the
    right window for a daily or weekdays recipient and the wrong one for
    a weekly recipient — the job already ran on the six days it stayed
    quiet for them. Those recipients get a window read back out of
    bill_change_events instead, computed once and shared, since it is the
    same week for everybody on that frequency.

    A match is marked reported only once the email is actually out the
    door, and not at all when SMTP isn't configured — otherwise a local
    run with no mail set up would quietly consume the news."""
    summary = {"sent": 0, "not_configured": 0, "skipped": 0, "off": 0, "errors": 0}
    today = today or db.today_in_california()
    weekly_changes = None

    for user_id, email in db.list_recipients(conn):
        prefs = db.get_notification_prefs(conn, user_id)
        if not _is_send_day(prefs["frequency"], today):
            summary["off"] += 1
            continue

        if prefs["frequency"] == "weekly":
            if weekly_changes is None:
                weekly_changes = db.changes_by_bill_since(conn, _weekly_window_start(today))
            source = weekly_changes
        else:
            source = changes_by_bill

        digest = build_user_digest(conn, user_id, source, prefs)
        if not digest:
            summary["skipped"] += 1
            continue

        # The account holder first, then anyone they cc'd, de-duplicated
        # case-insensitively so adding your own address to the extras box
        # doesn't send you two copies.
        recipients = [email] + [a for a in prefs["extra_recipients"]
                                if a.lower() != (email or "").lower()]
        try:
            delivered = mailer.send_email(recipients, digest["subject"],
                                          digest["text"], digest["html"])
        except Exception:
            summary["errors"] += 1
            continue
        if delivered:
            db.mark_matches_reported(conn, digest["match_ids"])
            conn.commit()
        summary["sent" if delivered else "not_configured"] += 1
    return summary
