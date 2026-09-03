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
"""

import html

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


def build_user_digest(conn, user_id, changes_by_bill):
    """Returns {"subject", "text", "html", "match_ids"} for this user's
    digest, or None if they have no news today.

    match_ids is what the caller marks reported once the send actually
    succeeded — see send_all_digests. Kept out of the email body and
    returned alongside it so an SMTP outage postpones the news instead of
    losing it."""
    flagged_ids = db.list_flagged_bill_ids_for_user(conn, user_id)
    relevant_bill_ids = sorted(bid for bid in flagged_ids if changes_by_bill.get(bid))
    new_matches = db.list_unreported_matches(conn, user_id)
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
            "summary": " ".join(c["description"] for c in changes_by_bill[bill_id]),
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
  </div>
</body></html>"""

    return {"subject": subject, "text": text_body, "html": html_body, "match_ids": match_ids}


def send_all_digests(conn, changes_by_bill):
    """Runs once, at the end of the daily refresh (see
    refresh_watchlist.py). Returns a summary dict for the caller to log:
    sent (actually delivered), not_configured (would-have-sent but SMTP
    isn't set up — see mailer.py), skipped (no news), and errors (a real
    send failure for that one recipient).

    No longer returns early on an empty changes_by_bill: a user can have
    nothing move on their flagged bills and still have a new saved-search
    match, which is the case saved searches exist for.

    A match is marked reported only once the email is actually out the
    door, and not at all when SMTP isn't configured — otherwise a local
    run with no mail set up would quietly consume the news."""
    summary = {"sent": 0, "not_configured": 0, "skipped": 0, "errors": 0}

    for user_id, email in db.list_recipients(conn):
        digest = build_user_digest(conn, user_id, changes_by_bill)
        if not digest:
            summary["skipped"] += 1
            continue
        try:
            delivered = mailer.send_email(email, digest["subject"], digest["text"], digest["html"])
        except Exception:
            summary["errors"] += 1
            continue
        if delivered:
            db.mark_matches_reported(conn, digest["match_ids"])
            conn.commit()
        summary["sent" if delivered else "not_configured"] += 1
    return summary
