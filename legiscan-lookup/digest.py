"""
digest.py — builds and sends the daily "what changed" email.

Called once a day by refresh_watchlist.py, right after it finishes
refreshing every watched bill and has collected a
{bill_id: [change description, ...]} map via
db.snapshot_bill_state()/db.diff_bill_state(). This module's only job is
turning that map into one email per affected user — it doesn't fetch
anything from LegiScan or touch bill data itself; see mailer.py for how
the email actually gets sent.

One digest per user per day, and only for users who actually have a
change on one of THEIR flagged bills — someone with zero relevant
changes gets nothing at all, not an empty "no changes today" email.
"""

import html
import os

import db
import mailer

# Where the "View action report" links point — set this to the real
# hosted URL (e.g. https://legiscan-lookup.onrender.com) when deploying;
# defaults to the local dev server so links still work when testing by
# hand.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8420").rstrip("/")


def _report_url(bill_id):
    return f"{APP_BASE_URL}/report?bill_id={bill_id}"


def build_user_digest(conn, user_id, changes_by_bill):
    """Returns {"subject", "text", "html"} for this user's digest, or
    None if none of their flagged bills have a change today."""
    flagged_ids = db.list_flagged_bill_ids_for_user(conn, user_id)
    relevant_bill_ids = sorted(bid for bid in flagged_ids if changes_by_bill.get(bid))
    if not relevant_bill_ids:
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
            # prose rather than a run-on.
            "summary": " ".join(changes_by_bill[bill_id]),
            "url": _report_url(bill_id),
        })
    if not rows:
        return None

    plural = "s" if len(rows) != 1 else ""
    subject = f"Bill Search: {len(rows)} update{plural} on your flagged bills"

    text_lines = [f"{len(rows)} of your flagged bills changed today:", ""]
    for r in rows:
        text_lines.append(f"{r['label']} — {r['title']}")
        text_lines.append(f"  {r['summary']}")
        text_lines.append(f"  {r['url']}")
        text_lines.append("")
    text_body = "\n".join(text_lines).rstrip() + "\n"

    html_rows = "".join(
        f"""
      <tr>
        <td style="padding:14px 0;border-bottom:1px solid #dcded3">
          <div style="font-family:ui-monospace,monospace;font-size:12px;color:#2f5d8a;margin-bottom:2px">{html.escape(r['label'])}</div>
          <div style="font-size:15px;font-weight:700;color:#1c2333;margin-bottom:6px">{html.escape(r['title'])}</div>
          <div style="font-size:14px;color:#5a6272;margin-bottom:8px">{html.escape(r['summary'])}</div>
          <a href="{r['url']}" style="font-size:13px;color:#2f5d8a;text-decoration:none;font-weight:600">View action report &rarr;</a>
        </td>
      </tr>"""
        for r in rows
    )
    html_body = f"""<!doctype html>
<html><body style="margin:0;background:#f4f5f2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1c2333">
  <div style="max-width:36rem;margin:0 auto;padding:2rem 1.25rem">
    <div style="font-size:13px;color:#5a6272;text-transform:uppercase;letter-spacing:0.03em;font-weight:700;margin-bottom:0.5rem">Bill Search — Daily Digest</div>
    <h1 style="font-size:20px;margin:0 0 1.25rem">{len(rows)} update{plural} on your flagged bills</h1>
    <table style="width:100%;border-collapse:collapse">{html_rows}</table>
    <p style="font-size:12px;color:#5a6272;margin-top:2rem">
      You're getting this because you have bills flagged in Bill Search.
      Manage what you're tracking at
      <a href="{APP_BASE_URL}/flagged" style="color:#2f5d8a">{APP_BASE_URL}/flagged</a>.
    </p>
  </div>
</body></html>"""

    return {"subject": subject, "text": text_body, "html": html_body}


def send_all_digests(conn, changes_by_bill):
    """Runs once, at the end of the daily refresh (see
    refresh_watchlist.py). Returns a summary dict for the caller to log:
    sent (actually delivered), not_configured (would-have-sent but SMTP
    isn't set up — see mailer.py), skipped (no relevant changes), and
    errors (a real send failure for that one recipient)."""
    summary = {"sent": 0, "not_configured": 0, "skipped": 0, "errors": 0}
    if not changes_by_bill:
        return summary

    for user_id, email in db.list_users_with_flagged_bills(conn):
        digest = build_user_digest(conn, user_id, changes_by_bill)
        if not digest:
            summary["skipped"] += 1
            continue
        try:
            delivered = mailer.send_email(email, digest["subject"], digest["text"], digest["html"])
            summary["sent" if delivered else "not_configured"] += 1
        except Exception:
            summary["errors"] += 1
    return summary
