"""
CAL-ACCESS dates arrive as "M/D/YYYY h:mm:ss AM" and have to be stored
as ISO, or nothing that reads them can sort.

Two halves, tested separately because they live in different projects
and heal different databases: calaccess_db.normalize_filing_date() is
what keeps new rows right, and db._migrate_calaccess_dates() is what
fixes the rows written before it existed. Both are pinned to the same
table of cases below so the SQL rewrite and the Python one can't drift.
"""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "calaccess-pipeline",
    ),
)

import calaccess_db  # noqa: E402 — must follow the sys.path insert above
import db  # noqa: E402


# (as CAL-ACCESS files it, as it must be stored)
CASES = [
    ("3/31/2000 12:00:00 AM", "2000-03-31"),   # single-digit month and day
    ("12/9/2015 12:00:00 AM", "2015-12-09"),   # two-digit month, single-digit day
    ("9/5/2007 12:00:00 AM", "2007-09-05"),    # the one that sorted above October
    ("10/31/2024 5:04:00 PM", "2024-10-31"),   # a time that isn't midnight
    ("1/1/1999", "1999-01-01"),                # no time component at all
]

# Values that are not a US-format date, and must survive untouched
# rather than being dropped or half-parsed.
PASSTHROUGH = ["2024-01-05", "", "See Attachment A"]


def _insert(conn, filed_date, filing_id="F1", client_name="Acme"):
    conn.execute(
        "INSERT INTO lobbying_entities (filer_id, name) VALUES (?, ?)"
        " ON CONFLICT(filer_id) DO NOTHING",
        ("1234", "Some Firm"),
    )
    entity_id = conn.execute(
        "SELECT id FROM lobbying_entities WHERE filer_id = '1234'"
    ).fetchone()["id"]
    calaccess_db.upsert_disclosure(conn, {
        "filing_id": filing_id,
        "filer_entity_id": entity_id,
        "client_name": client_name,
        "form_type": "F625P2",
        "period_start": filed_date,
        "period_end": filed_date,
        "amount_spent": 1000.0,
        "raw_bill_text": "AB 1",
        "filed_date": filed_date,
    })


@pytest.mark.parametrize("raw,iso", CASES)
def test_normalize_filing_date_converts_to_iso(raw, iso):
    assert calaccess_db.normalize_filing_date(raw) == iso


@pytest.mark.parametrize("value", PASSTHROUGH + [None])
def test_normalize_filing_date_leaves_everything_else_alone(value):
    assert calaccess_db.normalize_filing_date(value) == value


@pytest.mark.parametrize("raw,iso", CASES)
def test_upsert_disclosure_stores_iso(conn, raw, iso):
    _insert(conn, raw)
    row = conn.execute(
        "SELECT filed_date, period_start, period_end FROM lobbying_disclosures"
    ).fetchone()
    assert (row["filed_date"], row["period_start"], row["period_end"]) == (iso, iso, iso)


def test_migration_rewrites_rows_written_before_the_normalizer(conn):
    """The rows already on disk — written straight through, as the
    pipeline used to — come out ISO after a boot."""
    conn.execute(
        "INSERT INTO lobbying_entities (filer_id, name) VALUES ('1234', 'Some Firm')"
    )
    entity_id = conn.execute("SELECT id FROM lobbying_entities").fetchone()["id"]
    for i, (raw, _) in enumerate(CASES):
        conn.execute(
            """INSERT INTO lobbying_disclosures
                 (filing_id, filer_entity_id, client_name, form_type,
                  period_start, period_end, filed_date)
               VALUES (?,?,?,?,?,?,?)""",
            (f"F{i}", entity_id, f"Client {i}", "F625P2", raw, raw, raw),
        )

    db._migrate_calaccess_dates(conn)

    stored = [
        r["filed_date"]
        for r in conn.execute(
            "SELECT filed_date FROM lobbying_disclosures ORDER BY filing_id"
        )
    ]
    assert stored == [iso for _, iso in CASES]


def test_migration_sorts_most_recent_first(conn):
    """The actual defect: ORDER BY filed_date DESC used to lead with
    2007, because "9/..." is lexically above "10/..." and "1/...".
    """
    conn.execute(
        "INSERT INTO lobbying_entities (filer_id, name) VALUES ('1234', 'Some Firm')"
    )
    entity_id = conn.execute("SELECT id FROM lobbying_entities").fetchone()["id"]
    for i, (raw, _) in enumerate(CASES):
        conn.execute(
            """INSERT INTO lobbying_disclosures
                 (filing_id, filer_entity_id, client_name, form_type, filed_date)
               VALUES (?,?,?,?,?)""",
            (f"F{i}", entity_id, f"Client {i}", "F625P2", raw),
        )

    db._migrate_calaccess_dates(conn)

    newest = conn.execute(
        "SELECT filed_date FROM lobbying_disclosures ORDER BY filed_date DESC LIMIT 1"
    ).fetchone()["filed_date"]
    assert newest == "2024-10-31"

    latest = conn.execute(
        "SELECT MAX(filed_date) AS latest FROM lobbying_disclosures"
    ).fetchone()["latest"]
    assert latest == "2024-10-31"


def test_migration_is_a_no_op_on_rows_already_iso(conn):
    """Runs on every boot, so running it twice — or on a database the
    pipeline already wrote correctly — must change nothing."""
    _insert(conn, "9/5/2007 12:00:00 AM")
    db._migrate_calaccess_dates(conn)
    once = conn.execute("SELECT filed_date FROM lobbying_disclosures").fetchone()["filed_date"]
    db._migrate_calaccess_dates(conn)
    twice = conn.execute("SELECT filed_date FROM lobbying_disclosures").fetchone()["filed_date"]
    assert once == twice == "2007-09-05"


def test_migration_on_an_empty_table_does_nothing(conn):
    db._migrate_calaccess_dates(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM lobbying_disclosures").fetchone()["n"] == 0
