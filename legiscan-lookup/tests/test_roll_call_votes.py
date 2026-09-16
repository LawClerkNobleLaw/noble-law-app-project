"""
Tests for the per-legislator roll-call layer added on top of the
chamber-level `votes` tally:

  * db-side storage/read — store_roll_call_votes, store_legiscan_members,
    roll_calls_with_detail, legiscan_members_fresh, roll_call_detail, and
    upsert_bill now carrying a sponsor's people_id.
  * refresh_watchlist.sync_member_votes — the ingest orchestration that
    keeps the quota near zero (only uncached roll calls fetched, the
    session roster pulled at most once while fresh) and stays
    best-effort (a LegiScan error is swallowed, not fatal).

The roll_call_votes / legiscan_members tables are a public, org-unscoped
cache, so none of these need a user or an org.
"""

import db
import refresh_watchlist
from conftest import insert_bill


# ── db storage + read ───────────────────────────────────────────────

def test_store_and_read_roll_call_detail_resolves_members(conn):
    db.store_legiscan_members(conn, [
        {"people_id": 101, "name": "A. Senator", "party": "D", "role": "Sen",
         "district": "SD-1", "chamber": "Senate"},
        {"people_id": 102, "name": "B. Senator", "party": "R", "role": "Sen",
         "district": "SD-2", "chamber": "Senate"},
    ], session_id=2172)
    db.store_roll_call_votes(conn, 555, [
        {"people_id": 102, "vote_id": 2, "vote_text": "Nay"},
        {"people_id": 101, "vote_id": 1, "vote_text": "Yea"},
        {"people_id": 999, "vote_id": 4, "vote_text": "Absent"},  # unknown member
    ])
    conn.commit()

    detail = db.roll_call_detail(conn, 555)

    # Ordered Yea -> Nay -> other, so the two sides read as blocks.
    assert [d["vote_text"] for d in detail] == ["Yea", "Nay", "Absent"]
    assert detail[0]["people_id"] == 101 and detail[0]["party"] == "D"
    # A ballot whose member isn't in legiscan_members still appears (name None),
    # so a roll call is never silently short a vote.
    assert detail[2]["people_id"] == 999 and detail[2]["name"] is None


def test_store_roll_call_votes_is_idempotent_and_skips_null_people(conn):
    db.store_roll_call_votes(conn, 42, [
        {"people_id": 1, "vote_id": 1, "vote_text": "Yea"},
        {"people_id": None, "vote_id": 1, "vote_text": "Yea"},  # no PK half -> skipped
    ])
    db.store_roll_call_votes(conn, 42, [
        {"people_id": 1, "vote_id": 2, "vote_text": "Nay"},  # a re-fetch replaces cleanly
    ])
    conn.commit()

    rows = conn.execute("SELECT people_id, vote_text FROM roll_call_votes WHERE roll_call_id = 42").fetchall()
    assert [tuple(r) for r in rows] == [(1, "Nay")]


def test_roll_calls_with_detail_reports_only_cached_ids(conn):
    db.store_roll_call_votes(conn, 10, [{"people_id": 1, "vote_id": 1, "vote_text": "Yea"}])
    conn.commit()
    assert db.roll_calls_with_detail(conn, [10, 20, None]) == {10}
    assert db.roll_calls_with_detail(conn, []) == set()


def test_legislators_fresh_tracks_recent_sync(conn):
    assert db.legiscan_members_fresh(conn, 2172) is False  # never synced
    db.store_legiscan_members(conn, [{"people_id": 1, "name": "X"}], session_id=2172)
    conn.commit()
    assert db.legiscan_members_fresh(conn, 2172) is True
    # A sync older than the window is stale again.
    conn.execute("UPDATE legiscan_members SET synced_at = datetime('now', '-30 days') WHERE session_id = 2172")
    conn.commit()
    assert db.legiscan_members_fresh(conn, 2172, within_days=7) is False


def test_upsert_bill_stores_sponsor_people_id(conn):
    db.upsert_bill(conn, {
        "id": 7, "state": "CA", "bill_number": "SB1", "status_label": "Introduced",
        "sponsors": [{"people_id": 101, "name": "A. Senator", "party": "D", "role": "Primary"}],
    })
    conn.commit()
    row = conn.execute("SELECT people_id, name FROM bill_sponsors WHERE bill_id = 7").fetchone()
    assert row["people_id"] == 101 and row["name"] == "A. Senator"


# ── refresh_watchlist.sync_member_votes orchestration ───────────────

def _bill(votes, session_id=2172, bill_id=7, bill_number="SB1"):
    return {
        "id": bill_id, "bill_number": bill_number, "session_id": session_id,
        "votes": [{"roll_call_id": rc} for rc in votes],
    }


def _fake_roll_call(rc):
    return {"roll_call_id": rc, "votes": [{"people_id": 101, "vote_id": 1, "vote_text": "Yea"}]}


def _fake_people(session_id):
    return [{"people_id": 101, "name": "A. Senator", "party": "D", "role": "Sen",
             "district": "SD-1", "chamber": "Senate"}]


def test_sync_member_votes_fetches_each_roll_call_and_roster_once(conn, monkeypatch):
    insert_bill(conn, bill_id=7)
    rc_calls, people_calls = [], []
    monkeypatch.setattr(refresh_watchlist, "get_roll_call",
                        lambda rc: rc_calls.append(rc) or _fake_roll_call(rc))
    monkeypatch.setattr(refresh_watchlist, "get_session_people",
                        lambda sid: people_calls.append(sid) or _fake_people(sid))

    refresh_watchlist.sync_member_votes(conn, _bill([1, 2]))

    assert sorted(rc_calls) == [1, 2]
    assert people_calls == [2172]
    assert db.roll_calls_with_detail(conn, [1, 2]) == {1, 2}
    assert db.legiscan_members_fresh(conn, 2172) is True


def test_sync_member_votes_skips_cached_roll_calls_and_fresh_roster(conn, monkeypatch):
    insert_bill(conn, bill_id=7)
    # Pre-seed: roll call 1 already cached, roster already fresh.
    db.store_roll_call_votes(conn, 1, [{"people_id": 101, "vote_id": 1, "vote_text": "Yea"}])
    db.store_legiscan_members(conn, _fake_people(2172), session_id=2172)
    conn.commit()

    rc_calls, people_calls = [], []
    monkeypatch.setattr(refresh_watchlist, "get_roll_call",
                        lambda rc: rc_calls.append(rc) or _fake_roll_call(rc))
    monkeypatch.setattr(refresh_watchlist, "get_session_people",
                        lambda sid: people_calls.append(sid) or _fake_people(sid))

    refresh_watchlist.sync_member_votes(conn, _bill([1, 2]))

    assert rc_calls == [2]      # only the uncached roll call
    assert people_calls == []   # roster still fresh -> no getSessionPeople


def test_sync_member_votes_makes_no_calls_when_nothing_missing(conn, monkeypatch):
    insert_bill(conn, bill_id=7)
    db.store_roll_call_votes(conn, 1, [{"people_id": 101, "vote_id": 1, "vote_text": "Yea"}])
    db.store_legiscan_members(conn, _fake_people(2172), session_id=2172)
    conn.commit()

    def boom(*a, **k):
        raise AssertionError("should not call LegiScan when nothing is missing")

    monkeypatch.setattr(refresh_watchlist, "get_roll_call", boom)
    monkeypatch.setattr(refresh_watchlist, "get_session_people", boom)

    refresh_watchlist.sync_member_votes(conn, _bill([1]))  # returns without a call


def test_sync_member_votes_swallows_legiscan_errors(conn, monkeypatch):
    insert_bill(conn, bill_id=7)
    monkeypatch.setattr(refresh_watchlist, "get_session_people", _fake_people)

    def boom(rc):
        raise RuntimeError("LegiScan getRollCall failed")

    monkeypatch.setattr(refresh_watchlist, "get_roll_call", boom)
    monkeypatch.setattr(refresh_watchlist, "log", lambda *a, **k: None)

    # No exception escapes, and the failed batch leaves nothing half-written.
    refresh_watchlist.sync_member_votes(conn, _bill([1]))
    assert db.roll_calls_with_detail(conn, [1]) == set()


# ── roll_call_detail_for_flagged: the org-scoped whip-count read ─────

def _vote_row(conn, roll_call_id, bill_id):
    conn.execute(
        "INSERT INTO votes (id, bill_id, chamber, description, yea, nay, nv, absent, total, passed) "
        "VALUES (?, ?, 'Senate', 'Third reading', 1, 0, 0, 0, 1, 1)",
        (roll_call_id, bill_id),
    )


def test_roll_call_detail_for_flagged_returns_detail_for_your_bill(conn):
    from conftest import insert_user
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=7)
    db.flag_bill(conn, user_id, 7)
    _vote_row(conn, 500, 7)
    db.store_legiscan_members(conn, [{"people_id": 101, "name": "A. Senator", "party": "D",
                                      "chamber": "Senate"}], session_id=2172)
    db.store_roll_call_votes(conn, 500, [{"people_id": 101, "vote_id": 1, "vote_text": "Yea"}])
    conn.commit()

    detail = db.roll_call_detail_for_flagged(conn, user_id, 500)
    assert detail and detail[0]["people_id"] == 101 and detail[0]["party"] == "D"


def test_roll_call_detail_for_flagged_rejects_a_roll_call_not_on_your_bills(conn):
    from conftest import insert_user
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=8)   # present but NOT flagged by this user
    _vote_row(conn, 600, 8)
    db.store_roll_call_votes(conn, 600, [{"people_id": 101, "vote_id": 1, "vote_text": "Yea"}])
    conn.commit()

    # None (route 404s), even though the detail exists — it isn't their bill.
    assert db.roll_call_detail_for_flagged(conn, user_id, 600) is None


def test_roll_call_detail_for_flagged_empty_when_detail_not_ingested(conn):
    from conftest import insert_user
    user_id = insert_user(conn)
    insert_bill(conn, bill_id=9)
    db.flag_bill(conn, user_id, 9)
    _vote_row(conn, 700, 9)        # a known roll call, but no roll_call_votes yet
    conn.commit()

    # Empty list, not None: the roll call is theirs, the ballots just
    # haven't been fetched yet.
    assert db.roll_call_detail_for_flagged(conn, user_id, 700) == []
