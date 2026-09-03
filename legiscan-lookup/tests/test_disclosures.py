"""
Tests for the disclosure-form in-place HTML editor's backend — the
prepared_filings edit functions in db.py (update_prepared_filing_field,
set_prepared_filing_client_rows, mark_prepared_filing_pdf_generated) and
the staleness guard they exist to support in sign_off_prepared_filing,
plus disclosure_fields.py's validation. See
docs/disclosure-html-editor-plan.md for the design this implements.
"""

import datetime

import pytest

import accounts
import db
import disclosure_fields
import pdf_forms
from conftest import insert_user


def _make_filing(conn, user_id, extra_profile=None, clients=None):
    """A filing built the same way the real /api/prepared-filings POST
    route builds one — through values_for_form_601 — so these tests
    exercise the same field_data shape the editor will actually see."""
    profile_fields = {
        "legal_name": "Jordan Alvarez",
        "registrant_type": "firm",
        "bus_addr1": "100 Capitol Mall",
        "bus_city": "Sacramento",
        "bus_st": "CA",
        "bus_zip4": "95814",
        "bus_phone": "9165550100",
    }
    profile_fields.update(extra_profile or {})
    accounts.save_profile(conn, user_id, profile_fields)
    profile = accounts.get_profile(conn, user_id)

    client_ids = clients or []
    field_data = pdf_forms.values_for_form_601(
        profile, [db.get_client(conn, user_id, cid) for cid in client_ids],
        "jordan@example.com", sign_off=None, today=datetime.date(2026, 1, 15),
    )
    row_ids = client_ids[: pdf_forms.max_client_rows()]
    filing_id = db.create_prepared_filing(conn, user_id, "601", None, field_data, client_row_ids=row_ids)
    conn.commit()
    return filing_id


def _make_client(conn, user_id, name, **fields):
    cid = db.create_client(conn, user_id, {"name": name, **fields})
    conn.commit()
    return cid


# ── staleness guard: pdf_current / sign-off ─────────────────────────

def test_new_filing_has_no_current_pdf(conn):
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)

    filing = db.get_prepared_filing(conn, user_id, filing_id)
    assert filing["pdf_current"] is False


def test_sign_off_blocked_without_a_generated_pdf(conn):
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)

    with pytest.raises(ValueError, match="changed since the PDF was generated"):
        db.sign_off_prepared_filing(conn, user_id, filing_id, "Jordan Alvarez", True)


def test_mark_pdf_generated_makes_it_current_and_allows_sign_off(conn):
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)

    db.mark_prepared_filing_pdf_generated(conn, user_id, filing_id)
    conn.commit()
    filing = db.get_prepared_filing(conn, user_id, filing_id)
    assert filing["pdf_current"] is True

    signed = db.sign_off_prepared_filing(conn, user_id, filing_id, "Jordan Alvarez", True)
    assert signed["status"] == "ready_to_file"
    assert signed["signed_name"] == "Jordan Alvarez"


def test_editing_a_field_after_generating_pdf_invalidates_it(conn):
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)
    db.mark_prepared_filing_pdf_generated(conn, user_id, filing_id)
    conn.commit()

    db.update_prepared_filing_field(conn, user_id, filing_id, "EMAIL", "new@example.com")
    conn.commit()

    filing = db.get_prepared_filing(conn, user_id, filing_id)
    assert filing["pdf_current"] is False
    with pytest.raises(ValueError, match="changed since the PDF was generated"):
        db.sign_off_prepared_filing(conn, user_id, filing_id, "Jordan Alvarez", True)


def test_editing_after_sign_off_reopens_the_filing(conn):
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)
    db.mark_prepared_filing_pdf_generated(conn, user_id, filing_id)
    conn.commit()
    db.sign_off_prepared_filing(conn, user_id, filing_id, "Jordan Alvarez", True)
    conn.commit()

    db.update_prepared_filing_field(conn, user_id, filing_id, "EMAIL", "changed@example.com")
    conn.commit()

    filing = db.get_prepared_filing(conn, user_id, filing_id)
    assert filing["status"] == "draft"
    assert filing["signed_name"] is None
    assert filing["confirmed_accurate"] is False
    assert filing["signed_at"] is None
    assert filing["pdf_current"] is False
    assert filing["field_data"]["EMAIL"] == "changed@example.com"


def test_delete_prepared_filing_removes_it(conn):
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)

    db.delete_prepared_filing(conn, user_id, filing_id)
    conn.commit()

    assert db.get_prepared_filing(conn, user_id, filing_id) is None
    assert db.list_prepared_filings(conn, user_id) == []


def test_delete_prepared_filing_is_scoped_to_owner(conn):
    owner_id = insert_user(conn, email="owner@example.com")
    other_id = insert_user(conn, email="other@example.com")
    filing_id = _make_filing(conn, owner_id)

    db.delete_prepared_filing(conn, other_id, filing_id)  # no error — just deletes nothing
    conn.commit()

    assert db.get_prepared_filing(conn, owner_id, filing_id) is not None


def test_field_edit_on_unknown_filing_raises(conn):
    user_id = insert_user(conn)
    with pytest.raises(ValueError):
        db.update_prepared_filing_field(conn, user_id, 99999, "EMAIL", "x@example.com")


# ── client rows ──────────────────────────────────────────────────────

def test_select_client_rows_fills_and_clears_rows(conn):
    user_id = insert_user(conn)
    client_a = _make_client(conn, user_id, "Acme Corp")
    client_b = _make_client(conn, user_id, "Baker Industries")
    filing_id = _make_filing(conn, user_id)

    row_values = pdf_forms.client_row_values(
        [db.get_client(conn, user_id, client_a), db.get_client(conn, user_id, client_b)]
    )
    filing = db.set_prepared_filing_client_rows(conn, user_id, filing_id, [client_a, client_b], row_values)
    conn.commit()

    row1, row2, row3 = pdf_forms.CLIENT_ROW_FIELDS[0], pdf_forms.CLIENT_ROW_FIELDS[1], pdf_forms.CLIENT_ROW_FIELDS[2]
    assert "Acme Corp" in filing["field_data"][row1["employer"]]
    assert "Baker Industries" in filing["field_data"][row2["employer"]]
    assert filing["field_data"][row3["employer"]] == ""
    assert filing["client_row_ids"] == [client_a, client_b]

    # Reselecting with fewer clients must clear the now-unused row,
    # not leave Baker Industries' data sitting there stale.
    row_values = pdf_forms.client_row_values([db.get_client(conn, user_id, client_a)])
    filing = db.set_prepared_filing_client_rows(conn, user_id, filing_id, [client_a], row_values)
    conn.commit()
    assert "Acme Corp" in filing["field_data"][row1["employer"]]
    assert filing["field_data"][row2["employer"]] == ""


def test_selecting_client_rows_also_invalidates_pdf_and_reopens_signoff(conn):
    user_id = insert_user(conn)
    client_a = _make_client(conn, user_id, "Acme Corp")
    filing_id = _make_filing(conn, user_id)
    db.mark_prepared_filing_pdf_generated(conn, user_id, filing_id)
    conn.commit()

    row_values = pdf_forms.client_row_values([db.get_client(conn, user_id, client_a)])
    db.set_prepared_filing_client_rows(conn, user_id, filing_id, [client_a], row_values)
    conn.commit()

    filing = db.get_prepared_filing(conn, user_id, filing_id)
    assert filing["pdf_current"] is False


# ── disclosure_fields.py validation ─────────────────────────────────

def test_validate_field_data_requires_business_fields(conn):
    errors = disclosure_fields.validate_field_data("601", {})
    joined = " ".join(errors)
    assert "Street address is required." in errors
    assert "Email is required." in errors
    # Only the first of the six lobbyist slots is required — a 601 with
    # nobody named on it isn't a registration, but a firm of one leaves
    # the other five blank.
    assert "Individual lobbyist 1 is required." in errors
    assert not any("Individual lobbyist 2" in e for e in errors)


def test_validate_field_data_does_not_require_client_row_fields():
    # A fully complete business section but zero client rows should
    # validate clean — per-client info was deliberately never made
    # mandatory (see disclosure_fields.py's module docstring).
    field_data = {
        "BUSINESS ADDRESS  Number and street": "100 Capitol Mall",
        "BUSINESS ADDRESS  City": "Sacramento",
        "BUSINESS ADDRESS  State": "CA",
        "BUSINESS ADDRESS  Zip": "95814",
        "TELEPHONE Area Code": "916",
        "TELEPHONE": "555-0100",
        "EMAIL": "jordan@example.com",
        "INDIVIDUAL LOBBYISTS 1": "Jordan Alvarez",
    }
    assert disclosure_fields.validate_field_data("601", field_data) == []


def test_validate_field_data_flags_bad_format_even_when_present():
    field_data = {
        "BUSINESS ADDRESS  Number and street": "100 Capitol Mall",
        "BUSINESS ADDRESS  City": "Sacramento",
        "BUSINESS ADDRESS  State": "CA",
        "BUSINESS ADDRESS  Zip": "not-a-zip",
        "TELEPHONE Area Code": "9",
        "TELEPHONE": "555-0100",
        "EMAIL": "not-an-email",
        "INDIVIDUAL LOBBYISTS 1": "Jordan Alvarez",
    }
    errors = disclosure_fields.validate_field_data("601", field_data)
    assert any("ZIP" in e for e in errors)
    assert any("area code" in e for e in errors)
    assert any("email address" in e for e in errors)


def test_is_editable_field_key_recognizes_client_rows_and_rejects_unknown():
    assert disclosure_fields.is_editable_field_key("601", "EMAIL") is True
    assert disclosure_fields.is_editable_field_key("601", "DESCRIPTION 1") is True
    assert disclosure_fields.is_editable_field_key("601", "Signature_5") is False


# ── filing deadlines ────────────────────────────────────────────────

def test_due_date_for_601_is_ten_days_after_the_qualifying_date():
    assert disclosure_fields.due_date_for("601", "2026-08-26") == "2026-09-05"
    # Across a month boundary, where adding 10 to the day-of-month breaks.
    assert disclosure_fields.due_date_for("601", "2026-02-25") == "2026-03-07"


def test_due_date_is_none_rather_than_guessed():
    # The whole point of the column: no trigger, no deadline. An invented
    # one would look exactly as authoritative as a real one.
    assert disclosure_fields.due_date_for("601", None) is None
    assert disclosure_fields.due_date_for("601", "") is None
    assert disclosure_fields.due_date_for("601", "not-a-date") is None
    # A form with no rule yet (603/615) doesn't get 601's rule applied.
    assert disclosure_fields.due_date_for("615", "2026-08-26") is None


def test_valid_iso_date_rejects_dates_that_only_look_like_dates():
    assert disclosure_fields.valid_iso_date("2026-08-26") is True
    # Matches any reasonable regex, isn't a day.
    assert disclosure_fields.valid_iso_date("2026-02-31") is False
    assert disclosure_fields.valid_iso_date("2026-13-01") is False
    assert disclosure_fields.valid_iso_date("08/26/2026") is False
    assert disclosure_fields.valid_iso_date("") is False
    assert disclosure_fields.valid_iso_date(None) is False


def test_new_filing_has_no_deadline_until_one_is_set(conn):
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)

    filing = db.get_prepared_filing(conn, user_id, filing_id)

    assert filing["trigger_date"] is None
    assert filing["due_date"] is None
    assert filing["days_until_due"] is None


def test_set_prepared_filing_deadline_stores_both_dates(conn):
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)

    filing = db.set_prepared_filing_deadline(conn, user_id, filing_id, "2026-08-26", "2026-09-05")
    conn.commit()

    assert filing["trigger_date"] == "2026-08-26"
    assert filing["due_date"] == "2026-09-05"


def test_set_prepared_filing_deadline_stores_an_override_verbatim(conn):
    # The lobbyist's reading of their own deadline beats the app's
    # arithmetic — db stores what it's handed and does no deriving.
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)

    filing = db.set_prepared_filing_deadline(conn, user_id, filing_id, "2026-08-26", "2026-10-01")
    conn.commit()

    assert filing["due_date"] == "2026-10-01"


def test_set_prepared_filing_deadline_can_clear_the_dates(conn):
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)
    db.set_prepared_filing_deadline(conn, user_id, filing_id, "2026-08-26", "2026-09-05")

    filing = db.set_prepared_filing_deadline(conn, user_id, filing_id, "", "")
    conn.commit()

    assert filing["trigger_date"] is None
    assert filing["due_date"] is None
    assert filing["days_until_due"] is None


def test_set_prepared_filing_deadline_is_scoped_to_the_owner(conn):
    # Same reasoning as get_prepared_filing/delete_client: never trust a
    # client-supplied filing id on its own.
    owner = insert_user(conn)
    other = insert_user(conn, email="someone@example.com")
    filing_id = _make_filing(conn, owner)

    with pytest.raises(ValueError):
        db.set_prepared_filing_deadline(conn, other, filing_id, "2026-08-26", "2026-09-05")


def test_days_until_due_is_negative_once_overdue(conn):
    # Drives the "N days overdue" chip; it has to survive as a negative
    # number rather than being clamped or dropped.
    user_id = insert_user(conn)
    filing_id = _make_filing(conn, user_id)
    today = db.today_in_california()
    past = (datetime.date.fromisoformat(today) - datetime.timedelta(days=3)).isoformat()
    future = (datetime.date.fromisoformat(today) + datetime.timedelta(days=4)).isoformat()

    overdue = db.set_prepared_filing_deadline(conn, user_id, filing_id, None, past)
    assert overdue["days_until_due"] == -3

    upcoming = db.set_prepared_filing_deadline(conn, user_id, filing_id, None, future)
    assert upcoming["days_until_due"] == 4

    due_today = db.set_prepared_filing_deadline(conn, user_id, filing_id, None, today)
    assert due_today["days_until_due"] == 0


# ── client rows carry their typed values (P1-20) ─────────────────────
#
# The review screen now renders only the rows that hold something, and
# adding or removing a client is a one-click action rather than a rare
# trip through a nine-row picker. That makes row rebuilding common, and
# four of a row's five fields — nature of interests, effective date,
# period of contract, agencies lobbied — are things the client record
# usually doesn't hold, so they get typed on this screen. Losing them on
# the next add would be a data-loss bug the old picker's rarity hid.


def test_adding_a_client_keeps_what_was_typed_into_the_other_rows(conn):
    user_id = insert_user(conn)
    client_a = _make_client(conn, user_id, "Acme Corp")
    client_b = _make_client(conn, user_id, "Baker Industries")
    filing_id = _make_filing(conn, user_id)
    row1, row2 = pdf_forms.CLIENT_ROW_FIELDS[0], pdf_forms.CLIENT_ROW_FIELDS[1]

    filing = db.set_prepared_filing_client_rows(
        conn, user_id, filing_id, [client_a],
        pdf_forms.client_row_values([db.get_client(conn, user_id, client_a)]),
    )
    # Typed on the review screen — the client record holds none of this.
    filing = db.update_prepared_filing_field(
        conn, user_id, filing_id, row1["description"], "AI safety and model transparency")
    conn.commit()

    filing = db.set_prepared_filing_client_rows(
        conn, user_id, filing_id, [client_a, client_b],
        pdf_forms.client_row_values(
            [db.get_client(conn, user_id, client_a), db.get_client(conn, user_id, client_b)],
            previous_clients=[client_a],
            previous_field_data=filing["field_data"],
        ),
    )
    conn.commit()

    assert filing["field_data"][row1["description"]] == "AI safety and model transparency"
    assert "Baker Industries" in filing["field_data"][row2["employer"]]


def test_a_typed_value_follows_its_client_to_a_new_row(conn):
    # Removing the client above it renumbers everything below, and the
    # typed value has to move with its own client, not stay at row 2.
    user_id = insert_user(conn)
    client_a = _make_client(conn, user_id, "Acme Corp")
    client_b = _make_client(conn, user_id, "Baker Industries")
    filing_id = _make_filing(conn, user_id)
    row1, row2 = pdf_forms.CLIENT_ROW_FIELDS[0], pdf_forms.CLIENT_ROW_FIELDS[1]

    db.set_prepared_filing_client_rows(
        conn, user_id, filing_id, [client_a, client_b],
        pdf_forms.client_row_values([db.get_client(conn, user_id, client_a),
                                     db.get_client(conn, user_id, client_b)]),
    )
    filing = db.update_prepared_filing_field(
        conn, user_id, filing_id, row2["agencies"], "CPUC; CARB")
    conn.commit()

    filing = db.set_prepared_filing_client_rows(
        conn, user_id, filing_id, [client_b],
        pdf_forms.client_row_values(
            [db.get_client(conn, user_id, client_b)],
            previous_clients=[client_a, client_b],
            previous_field_data=filing["field_data"],
        ),
    )
    conn.commit()

    assert "Baker Industries" in filing["field_data"][row1["employer"]]
    assert filing["field_data"][row1["agencies"]] == "CPUC; CARB"
    assert filing["field_data"][row2["agencies"]] == ""


def test_a_corrected_client_record_still_flows_through(conn):
    # Only values that DIFFER from what the client record derives count
    # as hand-typed. An untouched row must still pick up a correction
    # made on the client itself.
    user_id = insert_user(conn)
    client_a = _make_client(conn, user_id, "Acme Corp")
    filing_id = _make_filing(conn, user_id)
    row1 = pdf_forms.CLIENT_ROW_FIELDS[0]

    filing = db.set_prepared_filing_client_rows(
        conn, user_id, filing_id, [client_a],
        pdf_forms.client_row_values([db.get_client(conn, user_id, client_a)]),
    )
    conn.commit()
    db.update_client(conn, user_id, client_a, {"name": "Acme Corporation"})
    conn.commit()

    filing = db.set_prepared_filing_client_rows(
        conn, user_id, filing_id, [client_a],
        pdf_forms.client_row_values(
            [db.get_client(conn, user_id, client_a)],
            previous_clients=[client_a],
            previous_field_data=filing["field_data"],
        ),
    )
    conn.commit()
    assert "Acme Corporation" in filing["field_data"][row1["employer"]]


def test_the_pdf_still_gets_all_nine_rows(conn):
    # The screen renders only filled rows; the form has nine, and every
    # one of its field names must still be written so a stale value can
    # never survive in a row the screen no longer shows.
    user_id = insert_user(conn)
    client_a = _make_client(conn, user_id, "Acme Corp")
    filing_id = _make_filing(conn, user_id)

    filing = db.set_prepared_filing_client_rows(
        conn, user_id, filing_id, [client_a],
        pdf_forms.client_row_values([db.get_client(conn, user_id, client_a)]),
    )
    conn.commit()

    assert len(pdf_forms.CLIENT_ROW_FIELDS) == 9
    for i, row in enumerate(pdf_forms.CLIENT_ROW_FIELDS):
        for field_name in row.values():
            assert field_name in filing["field_data"]
        if i:
            assert filing["field_data"][row["employer"]] == ""


def test_more_clients_than_rows_still_fits_the_form(conn):
    # The nine-row picker this screen used to carry was the only way to
    # handle a firm with more clients than rows. Add/remove replaces it:
    # the form holds nine, and swapping one out frees a row for another.
    user_id = insert_user(conn)
    client_ids = [_make_client(conn, user_id, "Client %02d" % n) for n in range(1, 13)]
    filing_id = _make_filing(conn, user_id)
    first_nine = client_ids[:9]

    filing = db.set_prepared_filing_client_rows(
        conn, user_id, filing_id, first_nine,
        pdf_forms.client_row_values([db.get_client(conn, user_id, c) for c in first_nine]),
    )
    conn.commit()
    assert filing["client_row_ids"] == first_nine

    swapped = first_nine[1:] + [client_ids[11]]
    filing = db.set_prepared_filing_client_rows(
        conn, user_id, filing_id, swapped,
        pdf_forms.client_row_values(
            [db.get_client(conn, user_id, c) for c in swapped],
            previous_clients=first_nine,
            previous_field_data=filing["field_data"],
        ),
    )
    conn.commit()
    assert filing["client_row_ids"] == swapped
    assert "Client 12" in filing["field_data"][pdf_forms.CLIENT_ROW_FIELDS[8]["employer"]]
