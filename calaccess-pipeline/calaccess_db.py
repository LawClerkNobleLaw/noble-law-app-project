"""
calaccess_db.py — connection and upsert logic for the CAL-ACCESS side of
the shared database.

Named uniquely rather than "db.py" because legiscan-lookup/app.py imports
this module directly (for hosted deployment, where the web app triggers
this refresh itself) alongside its own db.py in the same process — two
modules literally named "db" would collide in Python's module cache.

This writes into the SAME database as legiscan-lookup/db/billwatch.db —
not a separate one — because the whole point of lobbying_entities and
lobbying_disclosures (per client_interest_tracking_framework.md) is
eventually joining them against LegiScan bill data in bill_lobbying_link.

db/schema.sql (in legiscan-lookup) is the one canonical schema — applied
from here via the same file, not a duplicate copy.
"""

import os
import sqlite3

# schema.sql is always the repo's own copy (source, not data). The actual
# database file follows BILLWATCH_DATA_DIR when set (Render's persistent
# disk), same env var legiscan-lookup/db.py respects, so both modules
# agree on where the one shared database file actually lives.
_REPO_DB_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "legiscan-lookup", "db"
)
DB_DIR = os.environ.get("BILLWATCH_DATA_DIR", _REPO_DB_DIR)
DB_PATH = os.path.join(DB_DIR, "billwatch.db")
SCHEMA_PATH = os.path.join(_REPO_DB_DIR, "schema.sql")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_lobbying_tables(conn):
    """The two lobbying tables were designed in an earlier session before
    any real CAL-ACCESS data had been looked at. Real data showed they
    needed a UNIQUE filer_id (for upserts) and a client_name + filing_id
    (one filing has multiple per-client lines). Both tables were confirmed
    empty before this pipeline existed, so it's safe to drop and recreate
    them from the current schema.sql rather than hand-write an ALTER TABLE
    migration — but this only ever does that once, and only if they're
    still in the old shape. Runs BEFORE schema.sql is applied, purely by
    introspecting whatever's already on disk (schema.sql itself can't be
    applied yet — its new CREATE INDEX statement would fail against the
    old table shape)."""
    if not _table_exists(conn, "lobbying_disclosures"):
        return  # fresh database — nothing to migrate, schema.sql creates it clean

    disclosures_cols = _table_columns(conn, "lobbying_disclosures")
    if "filing_id" in disclosures_cols and "client_name" in disclosures_cols:
        return  # already migrated

    for table in ("lobbying_disclosures", "lobbying_entities"):
        if not _table_exists(conn, table):
            continue
        count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        if count:
            raise RuntimeError(
                f"{table} has {count} row(s) but is missing the new columns — "
                "refusing to drop it automatically. This table was expected to "
                "be empty; if it has real data now, migrate it by hand instead."
            )

    conn.execute("DROP TABLE IF EXISTS lobbying_disclosures")
    conn.execute("DROP TABLE IF EXISTS lobbying_entities")
    conn.commit()


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = get_connection()
    try:
        _migrate_lobbying_tables(conn)
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()


def upsert_entity(conn, entity):
    """entity: filer_id, name, entity_type, address, city, state, zip,
    registration_status, source_form. Returns the entity's local id."""
    conn.execute(
        """INSERT INTO lobbying_entities
             (filer_id, name, entity_type, address, city, state, zip,
              registration_status, source_form, last_synced_at)
           VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(filer_id) DO UPDATE SET
             name=excluded.name, entity_type=excluded.entity_type,
             address=excluded.address, city=excluded.city, state=excluded.state,
             zip=excluded.zip, registration_status=excluded.registration_status,
             source_form=excluded.source_form, last_synced_at=excluded.last_synced_at""",
        (
            entity["filer_id"], entity["name"], entity.get("entity_type"),
            entity.get("address"), entity.get("city"), entity.get("state"),
            entity.get("zip"), entity.get("registration_status"), entity.get("source_form"),
        ),
    )
    row = conn.execute(
        "SELECT id FROM lobbying_entities WHERE filer_id = ?", (entity["filer_id"],)
    ).fetchone()
    return row["id"]


def entity_id_for_filer(conn, filer_id):
    row = conn.execute(
        "SELECT id FROM lobbying_entities WHERE filer_id = ?", (filer_id,)
    ).fetchone()
    return row["id"] if row else None


def upsert_disclosure(conn, disclosure):
    """disclosure: filing_id, filer_entity_id, client_name, form_type,
    period_start, period_end, amount_spent, raw_bill_text, filed_date."""
    conn.execute(
        """INSERT INTO lobbying_disclosures
             (filing_id, filer_entity_id, client_name, form_type, period_start,
              period_end, amount_spent, raw_bill_text, filed_date)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(filing_id, client_name) DO UPDATE SET
             filer_entity_id=excluded.filer_entity_id, form_type=excluded.form_type,
             period_start=excluded.period_start, period_end=excluded.period_end,
             amount_spent=excluded.amount_spent, raw_bill_text=excluded.raw_bill_text,
             filed_date=excluded.filed_date""",
        (
            disclosure["filing_id"], disclosure["filer_entity_id"], disclosure.get("client_name"),
            disclosure.get("form_type"), disclosure.get("period_start"), disclosure.get("period_end"),
            disclosure.get("amount_spent"), disclosure.get("raw_bill_text"), disclosure.get("filed_date"),
        ),
    )
