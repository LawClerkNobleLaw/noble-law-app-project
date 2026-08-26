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
    assert "Individual lobbyist (your legal name) is required." in errors


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
