"""
Tests for P2-25 — the "Sponsors & votes" scorecard: db._bill_position_verdict
(with_us/against_us/pending, and why "watch" gets neither) and
db.list_sponsor_vote_rollup's aggregation of sponsors + votes + the
firm's own client positions.
"""

import db
from conftest import insert_bill, insert_user


def _set_status(conn, bill_id, status_label):
    conn.execute("UPDATE bills SET status_label = ? WHERE id = ?", (status_label, bill_id))
    conn.commit()


def _add_sponsor(conn, bill_id, name="Akilah Weber Pierson", party="D", role="Sponsor"):
    conn.execute(
        "INSERT INTO bill_sponsors (bill_id, name, party, role) VALUES (?, ?, ?, ?)",
        (bill_id, name, party, role),
    )
    conn.commit()


def _add_vote(conn, bill_id, yea=0, nay=0, nv=0, absent=0, passed=False, date="2026-06-01"):
    conn.execute(
        """INSERT INTO votes (bill_id, date, chamber, description, yea, nay, nv, absent, total, passed)
           VALUES (?, ?, 'Senate', 'Third reading', ?, ?, ?, ?, ?, ?)""",
        (bill_id, date, yea, nay, nv, absent, yea + nay + nv + absent, int(passed)),
    )
    conn.commit()


# ── _bill_position_verdict ──────────────────────────────────────────

def test_verdict_watch_is_never_a_win_or_a_loss():
    assert db._bill_position_verdict("Passed", "watch") is None
    assert db._bill_position_verdict("Failed", "watch") is None


def test_verdict_pending_while_the_bill_is_still_moving():
    for status in ("Introduced", "Engrossed", "Enrolled", None, ""):
        assert db._bill_position_verdict(status, "support") == "pending"
        assert db._bill_position_verdict(status, "oppose") == "pending"


def test_verdict_support_aligns_with_becoming_law():
    assert db._bill_position_verdict("Passed", "support") == "with_us"
    assert db._bill_position_verdict("Failed", "support") == "against_us"
    assert db._bill_position_verdict("Vetoed", "support") == "against_us"


def test_verdict_oppose_aligns_with_not_becoming_law():
    assert db._bill_position_verdict("Passed", "oppose") == "against_us"
    assert db._bill_position_verdict("Failed", "oppose") == "with_us"
    assert db._bill_position_verdict("Vetoed", "oppose") == "with_us"


def test_verdict_status_matching_is_case_insensitive():
    # bills.status_label is a small closed vocabulary but this shouldn't
    # silently start reading "pending" if a caller passes different case.
    assert db._bill_position_verdict("PASSED", "support") == "with_us"


# ── list_sponsor_vote_rollup ─────────────────────────────────────────

def test_rollup_includes_client_position_and_verdict(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    client_id = db.create_client(conn, user_id, {"name": "Sierra Club CA"})
    conn.commit()
    db.link_bill_to_client(conn, user_id, bill_id, client_id, "oppose")
    _add_sponsor(conn, bill_id)
    _set_status(conn, bill_id, "Passed")
    conn.commit()

    rollup = db.list_sponsor_vote_rollup(conn, user_id)

    assert len(rollup) == 1
    sponsor = rollup[0]
    assert sponsor["name"] == "Akilah Weber Pierson"
    assert sponsor["bill_count"] == 1
    bill = sponsor["bills"][0]
    assert bill["status_label"] == "Passed"
    assert len(bill["positions"]) == 1
    assert bill["positions"][0]["position"] == "oppose"
    assert bill["positions"][0]["verdict"] == "against_us"


def test_rollup_marks_unanimous_votes_so_the_page_can_collapse_them(conn):
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    _add_sponsor(conn, bill_id)
    conn.commit()
    _add_vote(conn, bill_id, yea=12, nay=0, nv=0, absent=1, passed=True, date="2026-05-01")
    _add_vote(conn, bill_id, yea=7, nay=5, nv=0, absent=1, passed=True, date="2026-06-01")

    rollup = db.list_sponsor_vote_rollup(conn, user_id)

    votes = rollup[0]["bills"][0]["votes"]
    assert [v["unanimous"] for v in sorted(votes, key=lambda v: v["date"])] == [True, False]


def test_rollup_omits_verdict_for_unassigned_bills(conn):
    # A sponsored bill nobody's assigned a client to yet has nothing to
    # compare a verdict against — an empty positions list, not a guess.
    user_id = insert_user(conn)
    bill_id = insert_bill(conn)
    db.flag_bill(conn, user_id, bill_id)
    _add_sponsor(conn, bill_id)
    conn.commit()

    bill = db.list_sponsor_vote_rollup(conn, user_id)[0]["bills"][0]
    assert bill["positions"] == []
