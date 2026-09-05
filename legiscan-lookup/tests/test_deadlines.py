"""
The Legislature's own deadline calendar (US-B3).

The phrasing in these tests is the shape the published tentative
calendar uses. Most of the risk here is in classification: several of
these sentences are substrings of each other, and a category read wrong
tells a firm its bill is safe for another two months when it has nine
days.
"""

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import accounts  # noqa: E402
import db  # noqa: E402
import deadlines  # noqa: E402


# ── Reading a date off a line ───────────────────────────────────────

@pytest.mark.parametrize("line,expected", [
    ("Jan. 1 Statutes take effect.", "2026-01-01"),
    ("January 1 Statutes take effect.", "2026-01-01"),
    ("Feb. 20 Last day for bills to be introduced.", "2026-02-20"),
    ("May 8 Last day for policy committees to act.", "2026-05-08"),
    # The four that a "[a-z]{3,9}" month pattern got wrong, because a
    # greedy weekday wildcard ate the first letter and left a plausible
    # non-month behind ("uly", "une", "ept").
    ("June 15 Budget bill must be passed.", "2026-06-15"),
    ("July 2 Last day for policy committees to act.", "2026-07-02"),
    ("Sept. 30 Last day for the Governor to sign or veto bills.", "2026-09-30"),
    ("September 30 Last day for the Governor to sign or veto bills.", "2026-09-30"),
    # A weekday in front, which the published calendar sometimes carries.
    ("Mon., Aug. 31 Last day for each house to pass bills.", "2026-08-31"),
])
def test_a_date_is_read_off_the_start_of_a_line(line, expected):
    parsed = deadlines.parse_calendar(line, year=2026)
    assert [d["date"] for d in parsed["deadlines"]] == [expected]


def test_the_year_comes_from_the_caller_not_the_text():
    """The calendar prints month and day only, and a California session
    spans two years."""
    assert deadlines.parse_calendar("Jan. 5 Something.", year=2025)["deadlines"][0]["date"] \
        == "2025-01-05"
    assert deadlines.parse_calendar("Jan. 5 Something.", year=2026)["deadlines"][0]["date"] \
        == "2026-01-05"


def test_an_impossible_date_warns_rather_than_guessing():
    parsed = deadlines.parse_calendar("Feb. 30 Something.", year=2026)
    assert parsed["deadlines"] == []
    assert any("isn't a real date" in w for w in parsed["warnings"])


def test_a_wrapped_line_folds_into_the_entry_above_it():
    """The published calendar wraps long entries across lines."""
    parsed = deadlines.parse_calendar(
        "May 1 Last day for policy committees to hear and report to fiscal committees\n"
        "    fiscal bills introduced in their house.",
        year=2026,
    )
    assert len(parsed["deadlines"]) == 1
    assert parsed["deadlines"][0]["label"].endswith("introduced in their house")
    # The folded text is re-classified, so a category that only becomes
    # apparent on the second line still lands.
    assert parsed["deadlines"][0]["kind"] == "policy_fiscal"


def test_lines_with_no_date_are_counted_not_silently_dropped():
    parsed = deadlines.parse_calendar("Not a dated line at all.", year=2026)
    assert parsed["deadlines"] == []
    assert any("no date" in w for w in parsed["warnings"])


def test_an_empty_paste_says_what_a_line_should_look_like():
    parsed = deadlines.parse_calendar("", year=2026)
    assert parsed["deadlines"] == []
    assert any("month and day" in w for w in parsed["warnings"])


# ── Classification ──────────────────────────────────────────────────

@pytest.mark.parametrize("label,kind", [
    ("Statutes take effect", "effective"),
    ("Spring Recess begins upon adjournment", "recess"),
    ("Last day for the Governor to sign or veto bills passed by the Legislature", "governor"),
    ("Budget bill must be passed by midnight", "budget"),
    ("Last day for bills to be introduced", "introduction"),
    ("Last day for each house to pass bills introduced in that house in the "
     "odd-numbered year", "two_year"),
    ("Last day for policy committees to hear and report to fiscal committees "
     "fiscal bills introduced in their house", "policy_fiscal"),
    ("Last day for fiscal committees to hear and report to the floor bills "
     "introduced in their house", "fiscal_committee"),
    ("Last day for policy committees to hear and report bills introduced in "
     "the other house", "policy_second"),
    ("Last day for fiscal committees to hear and report to the floor bills "
     "introduced in the second house", "fiscal_second"),
])
def test_classify(label, kind):
    assert deadlines.classify(label) == kind


def test_house_of_origin_is_not_read_as_final_floor_passage():
    """"Last day for each house to pass bills" is the end of session;
    the same sentence plus "introduced in that house" is the
    house-of-origin deadline three months earlier. Getting these the
    wrong way round is the most consequential misread available."""
    assert deadlines.classify(
        "Last day for each house to pass bills introduced in that house") == "house_of_origin"
    assert deadlines.classify(
        "Last day for each house to pass bills") == "floor_second"


def test_non_fiscal_is_not_read_as_fiscal():
    """"non-fiscal bills" contains "fiscal bills"."""
    assert deadlines.classify(
        "Last day for policy committees to hear and report to the floor "
        "non-fiscal bills introduced in their house") == "policy_nonfiscal"


def test_an_unrecognised_line_is_kept_as_other():
    """A date the firm can see, uncategorised, beats a date this module
    decided not to believe in."""
    assert deadlines.classify("Joint Session to receive the Governor") == "other"
    parsed = deadlines.parse_calendar("Mar. 4 Joint Session to receive the Governor.", year=2026)
    assert parsed["deadlines"][0]["kind"] == "other"


def test_every_classifier_kind_is_a_declared_kind():
    """A typo'd kind would render as a raw slug on the page."""
    for _label, pattern in deadlines._CLASSIFIERS:
        assert _label in deadlines.DEADLINE_KINDS, pattern


# ── Citation stripping ──────────────────────────────────────────────

@pytest.mark.parametrize("raw,clean", [
    ("Statutes take effect (Art. IV, Sec. 8(c)).", "Statutes take effect"),
    ("Last day for bills to be introduced (J.R. 54(a), J.R. 61(a)(1)).",
     "Last day for bills to be introduced"),
    ("Spring Recess begins upon adjournment (J.R. 51(b)(1)).",
     "Spring Recess begins upon adjournment"),
    ("Budget bill must be passed by midnight (Art. IV, Sec. 12(c)(3)).",
     "Budget bill must be passed by midnight"),
])
def test_clean_label_strips_nested_citations(raw, clean):
    """These citations nest — "(Art. IV, Sec. 8(c))" — and a [^)]*
    pattern stops at the inner bracket, leaving the outer one stranded
    on the end of the sentence."""
    assert deadlines.clean_label(raw) == clean


def test_clean_label_keeps_a_parenthetical_that_is_not_a_citation():
    assert "one house" in deadlines.clean_label("Deadline (one house only) applies.")


# ── Counting down ───────────────────────────────────────────────────

def test_days_until_is_signed():
    today = datetime.date(2026, 5, 20)
    assert deadlines.days_until("2026-05-29", today) == 9
    assert deadlines.days_until("2026-05-20", today) == 0
    assert deadlines.days_until("2026-05-01", today) == -19


def test_days_until_a_nonsense_date_is_none():
    assert deadlines.days_until("not a date") is None
    assert deadlines.days_until(None) is None


# ── Storage ─────────────────────────────────────────────────────────

def _user(conn, email="a@firm.com"):
    return accounts.create_user(conn, email, "a-long-enough-passphrase-1")


ROWS = [
    {"date": "2026-05-29", "label": "House of origin", "kind": "house_of_origin"},
    {"date": "2026-06-15", "label": "Budget bill", "kind": "budget"},
]


def test_deadlines_round_trip_with_countdowns(conn):
    user_id = _user(conn)
    assert db.replace_deadlines(conn, user_id, 2025, ROWS) == 2
    stored = db.list_deadlines(conn, user_id, today=datetime.date(2026, 5, 20))
    assert [d["date"] for d in stored] == ["2026-05-29", "2026-06-15"]
    assert [d["days_until"] for d in stored] == [9, 26]


def test_saving_again_replaces_that_session(conn):
    """A calendar is one document — a corrected paste must not leave the
    superseded dates beside the new ones."""
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    db.replace_deadlines(conn, user_id, 2025,
                         [{"date": "2026-05-30", "label": "Moved", "kind": "house_of_origin"}])
    assert [d["date"] for d in db.list_deadlines(conn, user_id)] == ["2026-05-30"]


def test_another_session_survives_a_replace(conn):
    """Last session's history is not superseded by this session's
    paste."""
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2023,
                         [{"date": "2024-08-31", "label": "Old", "kind": "floor_second"}])
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    assert sorted(d["session_year"] for d in db.list_deadlines(conn, user_id)) == \
        [2023, 2025, 2025]


def test_upcoming_only_excludes_past_dates(conn):
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    upcoming = db.list_deadlines(conn, user_id, upcoming_only=True,
                                 today=datetime.date(2026, 6, 1))
    assert [d["date"] for d in upcoming] == ["2026-06-15"]


def test_next_deadline_is_the_soonest_still_ahead(conn):
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    assert db.next_deadline(conn, user_id, today=datetime.date(2026, 5, 20))["date"] \
        == "2026-05-29"
    assert db.next_deadline(conn, user_id, today=datetime.date(2026, 5, 30))["date"] \
        == "2026-06-15"
    assert db.next_deadline(conn, user_id, today=datetime.date(2027, 1, 1)) is None


def test_deleting_one_deadline(conn):
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    first = db.list_deadlines(conn, user_id)[0]
    db.delete_deadline(conn, user_id, first["id"])
    assert [d["date"] for d in db.list_deadlines(conn, user_id)] == ["2026-06-15"]


def test_one_firms_deadlines_are_not_another_firms(conn):
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    other = _user(conn, "b@other.com")
    assert db.list_deadlines(conn, other) == []
    assert db.next_deadline(conn, other) is None


def test_a_firm_cannot_delete_another_firms_deadline(conn):
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    target = db.list_deadlines(conn, user_id)[0]["id"]
    other = _user(conn, "b@other.com")
    db.delete_deadline(conn, other, target)
    assert len(db.list_deadlines(conn, user_id)) == 2


# ── On the dashboard ────────────────────────────────────────────────

def _flag(conn, user_id, bill_id, number, status_label):
    conn.execute(
        "INSERT INTO bills (id, state, bill_number, status_label) VALUES (?,?,?,?)",
        (bill_id, "CA", number, status_label),
    )
    db.flag_bill(conn, user_id, bill_id)


def _deadline_items(conn, user_id, today):
    return [item for item in db.dashboard_summary(conn, user_id, today=today)["attention"]
            if item["kind"] == "deadline"]


def test_a_near_deadline_reaches_the_attention_queue(conn):
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    items = _deadline_items(conn, user_id, "2026-05-20")
    assert len(items) == 1
    assert items[0]["days"] == 9
    assert items[0]["href"] == "/flagged/calendar"


def test_only_one_deadline_row_appears_at_a_time(conn):
    """The deadline is a single event applying to every bill at once —
    one row per threatened bill would push every other kind of work off
    a capped queue."""
    user_id = _user(conn)
    _flag(conn, user_id, 1, "AB 1", "Introduced")
    _flag(conn, user_id, 2, "AB 2", "Introduced")
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    assert len(_deadline_items(conn, user_id, "2026-05-20")) == 1


def test_settled_bills_are_not_counted_as_threatened(conn):
    """A failed or vetoed bill has nothing left to fear from a committee
    deadline. Matched on status_label because that is what
    list_flagged_bills actually selects — reading a key that isn't there
    counted every dead bill as live."""
    user_id = _user(conn)
    _flag(conn, user_id, 1, "AB 1", "Introduced")
    _flag(conn, user_id, 2, "AB 2", "Engrossed")
    _flag(conn, user_id, 3, "AB 3", "Failed")
    _flag(conn, user_id, 4, "AB 4", "Vetoed")
    _flag(conn, user_id, 5, "AB 5", "Chaptered")
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    assert "2 flagged bills still in progress" in _deadline_items(
        conn, user_id, "2026-05-20")[0]["detail"]


def test_one_threatened_bill_reads_as_singular(conn):
    user_id = _user(conn)
    _flag(conn, user_id, 1, "AB 1", "Introduced")
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    assert "1 flagged bill still" in _deadline_items(conn, user_id, "2026-05-20")[0]["detail"]


def test_a_distant_deadline_stays_off_the_dashboard(conn):
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    assert _deadline_items(conn, user_id, "2026-01-05") == []


def test_a_past_deadline_stays_off_the_dashboard(conn):
    """Unlike an overdue filing, a passed deadline is not work — it is
    history, and nothing can be done about it."""
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    assert _deadline_items(conn, user_id, "2026-07-01") == []


def test_no_deadlines_loaded_means_no_deadline_row(conn):
    assert _deadline_items(conn, _user(conn), "2026-05-20") == []


def test_dashboard_accepts_todays_date_as_a_string(conn):
    """dashboard_summary carries `today` as an ISO string because it goes
    into SQL comparisons, while deadlines.py works in dates. Both forms
    have to work or this raises AttributeError at request time."""
    user_id = _user(conn)
    db.replace_deadlines(conn, user_id, 2025, ROWS)
    assert db.list_deadlines(conn, user_id, today="2026-05-20")[0]["days_until"] == 9
    assert db.list_deadlines(conn, user_id,
                             today=datetime.date(2026, 5, 20))[0]["days_until"] == 9
