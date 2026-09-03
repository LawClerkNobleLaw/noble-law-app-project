"""
Tests for saved searches (P1-12) — the one part of this product that can
tell a user about a bill nobody has flagged.

The load-bearing behaviour is the diff: a saved search for "artificial
intelligence" matches the same 119 bills every morning, and what the user
needs to hear about is the one that wasn't there yesterday. Everything
here is about that, about not reporting the same bill twice, and about
not losing news when the email can't go out.

No LegiScan calls anywhere — record_saved_search_matches takes the result
rows, so the tests hand it rows directly.
"""

import db
import digest
from conftest import insert_bill, insert_user


def _rows(*numbers):
    return [
        {"bill_id": 1000 + n, "bill_number": f"SB{n}", "title": f"Bill {n}",
         "last_action": "Introduced."}
        for n in numbers
    ]


def test_first_run_reports_everything_it_matched(conn):
    user_id = insert_user(conn)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "artificial intelligence")

    new = db.record_saved_search_matches(conn, search_id, _rows(1, 2, 3))

    assert [m["bill_number"] for m in new] == ["SB1", "SB2", "SB3"]


def test_second_run_reports_only_what_is_new(conn):
    user_id = insert_user(conn)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "artificial intelligence")
    db.record_saved_search_matches(conn, search_id, _rows(1, 2))

    new = db.record_saved_search_matches(conn, search_id, _rows(1, 2, 3))

    assert [m["bill_number"] for m in new] == ["SB3"]


def test_a_bill_dropping_out_and_returning_is_not_new_again(conn):
    """Relevance ordering moves. A bill falling off page one and coming
    back is not news, and re-reporting it would train the user to ignore
    the section."""
    user_id = insert_user(conn)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "ai")
    db.record_saved_search_matches(conn, search_id, _rows(1, 2))
    db.record_saved_search_matches(conn, search_id, _rows(1))

    assert db.record_saved_search_matches(conn, search_id, _rows(1, 2)) == []


def test_running_a_search_stamps_when_it_last_ran(conn):
    user_id = insert_user(conn)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "ai")
    db.record_saved_search_matches(conn, search_id, _rows(1), seen_at="2026-09-03T06:00:00Z")

    saved = db.list_saved_searches(conn, user_id)[0]
    assert saved["last_run_at"] == "2026-09-03T06:00:00Z"
    assert saved["new_match_count"] == 1


def test_duplicate_names_are_refused(conn):
    user_id = insert_user(conn)
    db.create_saved_search(conn, user_id, "AI bills", "ai")

    try:
        db.create_saved_search(conn, user_id, "AI bills", "something else")
    except ValueError as e:
        assert "already have a saved search" in str(e)
    else:
        raise AssertionError("expected a ValueError for a duplicate name")


def test_a_client_that_is_not_yours_is_refused(conn):
    user_id = insert_user(conn)
    other = insert_user(conn, email="other@example.com")
    their_client = db.create_client(conn, other, {"name": "Someone else's client"})

    try:
        db.create_saved_search(conn, user_id, "AI bills", "ai", client_id=their_client)
    except ValueError as e:
        assert "doesn't belong to your account" in str(e)
    else:
        raise AssertionError("expected a ValueError for another user's client")


def test_deleting_the_client_keeps_the_search_running(conn):
    """The query is the user's; only the auto-assign target went away."""
    user_id = insert_user(conn)
    client_id = db.create_client(conn, user_id, {"name": "Anthropic PBC"})
    db.create_saved_search(conn, user_id, "AI bills", "ai", client_id=client_id)

    db.delete_client(conn, user_id, client_id)
    conn.commit()

    saved = db.list_saved_searches(conn, user_id)
    assert len(saved) == 1
    assert saved[0]["client_id"] is None


def test_deleting_the_search_takes_its_matches(conn):
    """Unlike position_history, these rows only ever answered "what's new
    for this search" — they mean nothing once it's gone."""
    user_id = insert_user(conn)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "ai")
    db.record_saved_search_matches(conn, search_id, _rows(1, 2))

    assert db.delete_saved_search(conn, user_id, search_id) is True
    assert conn.execute("SELECT COUNT(*) FROM saved_search_matches").fetchone()[0] == 0


def test_another_user_cannot_delete_your_search(conn):
    user_id = insert_user(conn)
    other = insert_user(conn, email="other@example.com")
    search_id = db.create_saved_search(conn, user_id, "AI bills", "ai")

    assert db.delete_saved_search(conn, other, search_id) is False
    assert len(db.list_saved_searches(conn, user_id)) == 1


def test_opening_a_search_clears_its_count(conn):
    user_id = insert_user(conn)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "ai")
    db.record_saved_search_matches(conn, search_id, _rows(1, 2))

    db.mark_search_seen(conn, user_id, search_id)

    assert db.list_saved_searches(conn, user_id)[0]["new_match_count"] == 0


def test_digest_goes_out_for_matches_with_no_flagged_bill_changes(conn):
    """The account saved searches are most useful to is the one with
    nothing flagged yet, and the old digest skipped it entirely."""
    user_id = insert_user(conn)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "artificial intelligence")
    db.record_saved_search_matches(conn, search_id, _rows(1))
    conn.commit()

    built = digest.build_user_digest(conn, user_id, {})
    assert built is not None
    assert "matching your saved searches" in built["subject"]
    assert "SB1" in built["text"]
    assert built["match_ids"]


def test_digest_carries_both_halves_when_both_have_news(conn):
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=1, bill_number="SB1159")
    db.flag_bill(conn, user_id, 1)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "ai")
    db.record_saved_search_matches(conn, search_id, _rows(7))
    conn.commit()

    changes = {1: [{"change_type": "status", "summary": "Enrolled",
                    "description": "Status moved to Enrolled.", "event_date": None}]}
    built = digest.build_user_digest(conn, user_id, changes)

    assert "flagged bills" in built["subject"] and "matching your searches" in built["subject"]
    assert "SB1159" in built["text"] and "SB7" in built["text"]


def test_a_user_with_no_news_still_gets_nothing(conn):
    user_id = insert_user(conn)
    db.create_saved_search(conn, user_id, "AI bills", "ai")
    conn.commit()

    assert digest.build_user_digest(conn, user_id, {}) is None


def test_matches_stay_unreported_until_the_mail_actually_goes(conn):
    """mailer degrades to logging when SMTP isn't configured. If that
    counted as reported, a local run would quietly consume the news."""
    user_id = insert_user(conn)
    search_id = db.create_saved_search(conn, user_id, "AI bills", "ai")
    db.record_saved_search_matches(conn, search_id, _rows(1))
    conn.commit()

    summary = digest.send_all_digests(conn, {})

    assert summary["not_configured"] == 1
    assert db.list_saved_searches(conn, user_id)[0]["new_match_count"] == 1
