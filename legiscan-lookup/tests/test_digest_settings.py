"""
Tests for the digest's settings (P2-30) — frequency, which change types
count as news, extra recipients, and per-bill mutes.

The digest was unconfigurable, which made these tests mostly about one
question: does a setting actually change what leaves the building? So
almost everything here goes through digest.send_all_digests with a fake
mailer rather than asserting on the prefs row — a stored preference the
send path ignores is exactly the bug worth catching.

No SMTP anywhere: mailer.send_email is monkeypatched to record its
arguments, which is also what proves the cc list reaches it.
"""

import db
import digest
import mailer
from conftest import insert_bill, insert_user


def _flagged(conn, user_id, bill_id=1, number="SB1"):
    insert_bill(conn, bill_id=bill_id, bill_number=number)
    db.flag_bill(conn, user_id, bill_id)
    return bill_id


def _change(change_type="status", description="Status changed."):
    return {"change_type": change_type, "summary": "Enrolled",
            "description": description, "event_date": "2026-09-01"}


class _Outbox:
    """Stands in for mailer.send_email, reporting a successful send."""

    def __init__(self):
        self.sent = []

    def __call__(self, to_addr, subject, text_body, html_body=None):
        self.sent.append({"to": to_addr, "subject": subject,
                          "text": text_body, "html": html_body})
        return True


def _outbox(monkeypatch):
    box = _Outbox()
    monkeypatch.setattr(digest.mailer, "send_email", box)
    return box


# ── Defaults: an account that has never seen the settings ──────────────

def test_defaults_match_the_behaviour_the_digest_always_had(conn):
    user_id = insert_user(conn)

    prefs = db.get_notification_prefs(conn, user_id)

    assert prefs["frequency"] == "daily"
    assert prefs["event_types"] == ["status", "amendment", "hearing", "vote"]
    assert prefs["include_matches"] is True
    assert prefs["extra_recipients"] == []


def test_no_prefs_row_still_sends(conn, monkeypatch):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    box = _outbox(monkeypatch)

    summary = digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-02")

    assert summary["sent"] == 1
    assert len(box.sent) == 1


# ── Frequency ──────────────────────────────────────────────────────────

def test_paused_sends_nothing_and_is_counted_separately(conn, monkeypatch):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    db.save_notification_prefs(conn, user_id, {"frequency": "off"})
    box = _outbox(monkeypatch)

    summary = digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-02")

    assert box.sent == []
    # "off" rather than "skipped": there was news, it just wasn't a send
    # day, and the daily log line should be able to tell those apart.
    assert summary == {"sent": 0, "not_configured": 0, "skipped": 0, "off": 1, "errors": 0}


def test_weekdays_skips_the_weekend(conn, monkeypatch):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    db.save_notification_prefs(conn, user_id, {
        "frequency": "weekdays", "event_types": list(db.CHANGE_TYPES)})
    box = _outbox(monkeypatch)

    # 2026-09-05 is a Saturday, 2026-09-07 a Monday.
    assert digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-05")["off"] == 1
    assert digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-07")["sent"] == 1
    assert len(box.sent) == 1


def test_weekly_sends_on_monday_only(conn, monkeypatch):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    db.save_notification_prefs(conn, user_id, {
        "frequency": "weekly", "event_types": list(db.CHANGE_TYPES)})
    # A weekly recipient's news comes out of bill_change_events, not out
    # of what the refresh job just handed over — see the roll-up test
    # below for why.
    db.record_bill_changes(conn, bill_id, [_change()], detected_at="2026-09-03")
    conn.commit()
    _outbox(monkeypatch)

    assert digest.send_all_digests(conn, {}, today="2026-09-09")["off"] == 1
    assert digest.send_all_digests(conn, {}, today="2026-09-07")["sent"] == 1


def test_weekly_reports_the_week_the_daily_job_stayed_quiet_about(conn, monkeypatch):
    """The point of the weekly option. The refresh job hands the digest
    only what changed in the last few minutes; a Monday roll-up has to
    cover the six days it already ran and said nothing."""
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    db.save_notification_prefs(conn, user_id, {
        "frequency": "weekly", "event_types": list(db.CHANGE_TYPES)})
    db.record_bill_changes(conn, bill_id, [_change(description="Amended on Wednesday.")],
                           detected_at="2026-09-02")
    conn.commit()
    box = _outbox(monkeypatch)

    # Nothing changed overnight, so the daily map is empty — and the
    # weekly recipient still has news.
    summary = digest.send_all_digests(conn, {}, today="2026-09-07")

    assert summary["sent"] == 1
    assert "Amended on Wednesday." in box.sent[0]["text"]


def test_weekly_does_not_reach_back_past_the_previous_send(conn, monkeypatch):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    db.save_notification_prefs(conn, user_id, {
        "frequency": "weekly", "event_types": list(db.CHANGE_TYPES)})
    # 2026-08-31 is the Monday before, already covered by that send.
    db.record_bill_changes(conn, bill_id, [_change(description="Old news.")],
                           detected_at="2026-08-31")
    conn.commit()
    _outbox(monkeypatch)

    assert digest.send_all_digests(conn, {}, today="2026-09-07")["skipped"] == 1


# ── Which change types count as news ───────────────────────────────────

def test_unchecked_change_types_are_not_news(conn, monkeypatch):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    db.save_notification_prefs(conn, user_id, {
        "frequency": "daily", "event_types": ["hearing", "amendment"]})
    box = _outbox(monkeypatch)

    summary = digest.send_all_digests(
        conn, {bill_id: [_change("status", "Status changed.")]}, today="2026-09-02")

    assert box.sent == []
    assert summary["skipped"] == 1


def test_a_wanted_change_still_arrives_when_others_are_filtered_out(conn, monkeypatch):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    db.save_notification_prefs(conn, user_id, {
        "frequency": "daily", "event_types": ["hearing"]})
    box = _outbox(monkeypatch)

    digest.send_all_digests(conn, {bill_id: [
        _change("status", "Status changed."),
        _change("hearing", "Hearing scheduled for 2026-09-10."),
    ]}, today="2026-09-02")

    text = box.sent[0]["text"]
    assert "Hearing scheduled" in text
    assert "Status changed" not in text


def test_turning_every_change_type_off_still_leaves_saved_search_matches(conn, monkeypatch):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    db.save_notification_prefs(conn, user_id, {"frequency": "daily", "event_types": []})
    search_id = db.create_saved_search(conn, user_id, "AI bills", "artificial intelligence")
    db.record_saved_search_matches(conn, search_id, [
        {"bill_id": 900, "bill_number": "AB900", "title": "An AI bill", "last_action": "Introduced."}])
    box = _outbox(monkeypatch)

    digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-02")

    assert "AB900" in box.sent[0]["text"]
    assert "Status changed" not in box.sent[0]["text"]


def test_matches_can_be_turned_off_on_their_own(conn, monkeypatch):
    user_id = insert_user(conn)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "artificial intelligence")
    db.record_saved_search_matches(conn, search_id, [
        {"bill_id": 900, "bill_number": "AB900", "title": "An AI bill", "last_action": "Introduced."}])
    db.save_notification_prefs(conn, user_id, {
        "frequency": "daily", "event_types": list(db.CHANGE_TYPES), "include_matches": False})
    box = _outbox(monkeypatch)

    assert digest.send_all_digests(conn, {}, today="2026-09-02")["skipped"] == 1
    assert box.sent == []


def test_declining_matches_does_not_consume_them(conn, monkeypatch):
    """A user who turns matches off and back on should still hear about
    the bill that matched while they weren't listening — the reported
    flag is about delivery, not about having been offered."""
    user_id = insert_user(conn)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "artificial intelligence")
    db.record_saved_search_matches(conn, search_id, [
        {"bill_id": 900, "bill_number": "AB900", "title": "An AI bill", "last_action": "Introduced."}])
    db.save_notification_prefs(conn, user_id, {
        "frequency": "daily", "event_types": [], "include_matches": False})
    box = _outbox(monkeypatch)
    digest.send_all_digests(conn, {}, today="2026-09-02")

    db.save_notification_prefs(conn, user_id, {
        "frequency": "daily", "event_types": [], "include_matches": True})
    digest.send_all_digests(conn, {}, today="2026-09-03")

    assert len(box.sent) == 1
    assert "AB900" in box.sent[0]["text"]


# ── Muting one bill ────────────────────────────────────────────────────

def test_muting_a_bill_stops_its_mail_without_unflagging_it(conn, monkeypatch):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    db.set_digest_muted(conn, user_id, bill_id, True)
    box = _outbox(monkeypatch)

    summary = digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-02")

    assert box.sent == []
    assert summary["skipped"] == 1
    # Still tracked — muting is not unflagging.
    assert bill_id in db.list_flagged_bill_ids_for_user(conn, user_id)


def test_muting_one_bill_leaves_the_others_alone(conn, monkeypatch):
    user_id = insert_user(conn)
    quiet = _flagged(conn, user_id, bill_id=1, number="SB1")
    loud = _flagged(conn, user_id, bill_id=2, number="SB2")
    db.set_digest_muted(conn, user_id, quiet, True)
    box = _outbox(monkeypatch)

    digest.send_all_digests(conn, {quiet: [_change()], loud: [_change()]}, today="2026-09-02")

    text = box.sent[0]["text"]
    assert "SB2" in text
    assert "SB1" not in text


def test_unmuting_puts_the_bill_back(conn, monkeypatch):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    db.set_digest_muted(conn, user_id, bill_id, True)
    db.set_digest_muted(conn, user_id, bill_id, False)
    box = _outbox(monkeypatch)

    digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-02")

    assert len(box.sent) == 1


def test_a_mute_is_one_persons_not_the_firms(conn, monkeypatch):
    """The flag belongs to the organization; who wants mail about it does
    not. Same split as bill_views."""
    org_id = db.create_organization(conn, "Noble Law")
    mine = insert_user(conn, email="a@firm.com")
    theirs = insert_user(conn, email="b@firm.com")
    for uid in (mine, theirs):
        conn.execute("UPDATE users SET org_id = ? WHERE id = ?", (org_id, uid))
    conn.commit()
    bill_id = _flagged(conn, mine)
    db.set_digest_muted(conn, mine, bill_id, True)
    box = _outbox(monkeypatch)

    digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-02")

    assert [s["to"] for s in box.sent] == [["b@firm.com"]]


def test_muted_bills_can_be_found_again(conn):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id, number="SB1159")
    db.set_digest_muted(conn, user_id, bill_id, True)

    muted = db.list_digest_mutes(conn, user_id)

    assert [(m["state"], m["bill_number"]) for m in muted] == [("CA", "SB1159")]


def test_the_report_says_whether_this_bill_is_muted(conn):
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)

    assert db.get_bill_report(conn, user_id, bill_id)["digest_muted"] is False
    db.set_digest_muted(conn, user_id, bill_id, True)
    assert db.get_bill_report(conn, user_id, bill_id)["digest_muted"] is True


# ── Extra recipients ───────────────────────────────────────────────────

def test_extra_recipients_are_copied_on_the_same_email(conn, monkeypatch):
    user_id = insert_user(conn, email="lobbyist@firm.com")
    bill_id = _flagged(conn, user_id)
    db.save_notification_prefs(conn, user_id, {
        "frequency": "daily", "event_types": list(db.CHANGE_TYPES),
        "extra_recipients": "assistant@firm.com, associate@firm.com"})
    box = _outbox(monkeypatch)

    digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-02")

    assert box.sent[0]["to"] == ["lobbyist@firm.com", "assistant@firm.com", "associate@firm.com"]


def test_the_account_address_is_never_copied_twice(conn, monkeypatch):
    user_id = insert_user(conn, email="lobbyist@firm.com")
    bill_id = _flagged(conn, user_id)
    db.save_notification_prefs(conn, user_id, {
        "frequency": "daily", "event_types": list(db.CHANGE_TYPES),
        "extra_recipients": "LOBBYIST@firm.com, assistant@firm.com"})
    box = _outbox(monkeypatch)

    digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-02")

    assert box.sent[0]["to"] == ["lobbyist@firm.com", "assistant@firm.com"]


def test_recipients_are_accepted_however_they_are_typed(conn):
    user_id = insert_user(conn)

    prefs = db.save_notification_prefs(conn, user_id, {
        "frequency": "daily", "extra_recipients": "a@firm.com; b@firm.com\n c@firm.com"})

    assert prefs["extra_recipients"] == ["a@firm.com", "b@firm.com", "c@firm.com"]


def test_a_bad_address_is_named_rather_than_stored(conn):
    user_id = insert_user(conn)

    try:
        db.save_notification_prefs(conn, user_id, {
            "frequency": "daily", "extra_recipients": "fine@firm.com, sam@"})
    except ValueError as err:
        assert "sam@" in str(err)
    else:
        raise AssertionError("expected a ValueError naming the bad address")

    assert db.get_notification_prefs(conn, user_id)["extra_recipients"] == []


def test_too_many_recipients_is_refused(conn):
    user_id = insert_user(conn)
    many = ", ".join(f"a{n}@firm.com" for n in range(db.MAX_EXTRA_RECIPIENTS + 1))

    try:
        db.save_notification_prefs(conn, user_id, {"frequency": "daily", "extra_recipients": many})
    except ValueError as err:
        assert str(db.MAX_EXTRA_RECIPIENTS) in str(err)
    else:
        raise AssertionError("expected a ValueError")


def test_a_frequency_the_app_does_not_have_is_refused(conn):
    user_id = insert_user(conn)

    try:
        db.save_notification_prefs(conn, user_id, {"frequency": "hourly"})
    except ValueError:
        pass
    else:
        raise AssertionError("expected a ValueError")


def test_an_unknown_change_type_is_dropped_rather_than_stored(conn):
    user_id = insert_user(conn)

    prefs = db.save_notification_prefs(conn, user_id, {
        "frequency": "daily", "event_types": ["status", "sponsor-change"]})

    assert prefs["event_types"] == ["status"]


# ── The settings link every email carries ──────────────────────────────

def test_every_digest_says_where_the_settings_are(conn, monkeypatch):
    """A cc'd assistant has no account to log into, so the link in the
    footer is the only route they have to these controls."""
    user_id = insert_user(conn)
    bill_id = _flagged(conn, user_id)
    box = _outbox(monkeypatch)

    digest.send_all_digests(conn, {bill_id: [_change()]}, today="2026-09-02")

    assert digest.SETTINGS_URL in box.sent[0]["text"]
    assert digest.SETTINGS_URL in box.sent[0]["html"]
    assert digest.SETTINGS_URL.endswith("/profile#notifications")


# ── mailer: one message, several recipients ────────────────────────────

def test_mailer_takes_one_address_or_a_list(monkeypatch, capsys):
    monkeypatch.setattr(mailer, "SMTP_HOST", "")

    assert mailer.send_email("one@firm.com", "s", "t") is False
    assert mailer.send_email(["one@firm.com", "two@firm.com"], "s", "t") is False
    # An empty list is not an error and not a send — it's nobody to tell.
    assert mailer.send_email([], "s", "t") is False

    logged = capsys.readouterr().out
    assert "one@firm.com, two@firm.com" in logged
