"""
mailer.py — the one place that knows how to actually send an email.

Stdlib only (smtplib + email.message) — consistent with the rest of this
app having no dependencies beyond Python itself. Configured entirely via
environment variables:

  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, EMAIL_FROM

Works with any standard SMTP provider (Gmail, SendGrid's SMTP relay,
Postmark, AWS SES's SMTP interface, etc.) — this app doesn't pick one for
you, since that's a real account/credentials decision only whoever
deploys this can make.

If SMTP_HOST/SMTP_USER/SMTP_PASSWORD/EMAIL_FROM aren't all set (the
local/default case), send_email() doesn't raise — it logs what it WOULD
have sent and returns False, the same "degrade gracefully when
unconfigured" pattern LEGISCAN_API_KEY/REFRESH_SECRET already use
elsewhere in this app. A real send that fails (bad credentials, SMTP
server unreachable, etc.) DOES raise — that's an actual error the caller
(digest.py) should catch and count per-recipient, not silently swallow.
"""

import os
import smtplib
from email.message import EmailMessage

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_FROM = os.environ.get("EMAIL_FROM")


def is_configured():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and EMAIL_FROM)


def send_email(to_addr, subject, text_body, html_body=None):
    """Returns True if actually sent, False if skipped because SMTP isn't
    configured. Raises on a real send failure — the caller decides how
    to log/count that per recipient."""
    if not is_configured():
        print(f"[mailer] SMTP not configured — would have sent to {to_addr}: {subject}", flush=True)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = to_addr
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    return True
