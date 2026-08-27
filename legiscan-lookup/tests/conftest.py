"""
conftest.py — shared pytest fixtures for legiscan-lookup's test suite.

The one fixture everything else builds on is `conn`: a fresh in-memory
SQLite database, schema applied via db.init_db(conn=...) — the exact
same code path the real app runs on every boot, not a hand-copied
duplicate of schema.sql that could quietly drift out of sync with it.
Each test gets its own brand-new in-memory database (nothing persists
between tests, and nothing ever touches the real db/billwatch.db file).
"""

import os
import sqlite3
import sys

# tests/ sits inside legiscan-lookup/, where app.py/db.py/accounts.py
# etc. live as plain top-level modules (no package __init__.py) —
# added explicitly rather than relying on pytest's own rootdir/sys.path
# insertion behavior, so `pytest tests/` works the same from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    db.init_db(conn=connection)
    yield connection
    connection.close()


def insert_user(conn, email="lawclerk@example.com", password_hash="not-a-real-hash"):
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, datetime('now'))",
        (email, password_hash),
    )
    conn.commit()
    return cur.lastrowid


def insert_bill(conn, bill_id=1, bill_number="SB1", state="CA", title="A test bill.", url=None):
    conn.execute(
        """INSERT INTO bills (id, state, bill_number, title, status_label, url)
           VALUES (?, ?, ?, ?, 'Introduced', ?)""",
        (bill_id, state, bill_number, title, url),
    )
    conn.commit()
    return bill_id


def insert_entity(conn, name, entity_type="firm", filer_id=None, **extra):
    cur = conn.execute(
        "INSERT INTO lobbying_entities (filer_id, name, entity_type, city, state, registration_status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            filer_id, name, entity_type,
            extra.get("city"), extra.get("state"), extra.get("registration_status", "ACTIVE"),
        ),
    )
    conn.commit()
    return cur.lastrowid


def insert_disclosure(conn, filer_entity_id, client_name, filing_id=None, filed_date=None, raw_bill_text=None):
    cur = conn.execute(
        """INSERT INTO lobbying_disclosures
             (filing_id, filer_entity_id, client_name, form_type, filed_date, raw_bill_text)
           VALUES (?, ?, ?, '625P2', ?, ?)""",
        (filing_id, filer_entity_id, client_name, filed_date, raw_bill_text),
    )
    conn.commit()
    return cur.lastrowid
