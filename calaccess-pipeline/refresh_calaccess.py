#!/usr/bin/env python3
"""
refresh_calaccess.py — the daily CAL-ACCESS ingestion job.

Runs once, does its work, and exits — same shape as legiscan-lookup's
refresh_watchlist.py. A macOS launchd job (see launchd/, set up by
refresh.sh) starts this once a day, at 6am — before the 7am LegiScan job,
so the two don't hit the network at the same time.

What it does, in order:
  1. Downloads the state's full raw export (dbwebexport.zip, ~1.5GB,
     regenerated daily, no key needed) to a scratch temp directory.
  2. Extracts only the 4 files this pipeline actually needs — the rest of
     the 1.5GB is unrelated campaign-finance data.
  3. Parses Forms 601/603 (from CVR_REGISTRATION_CD.TSV, cross-checked
     against FILERNAME_CD.TSV for current status) into lobbying_entities.
  4. Parses Forms 625P2/635P3B (from LPAY_CD.TSV, joined against
     CVR_LOBBY_DISCLOSURE_CD.TSV for the filing's firm/period) into
     lobbying_disclosures — one row per (filing, client) line, since a
     single filing can name several clients each with their own amount
     and bill text.
  5. Deletes the temp directory (zip + extracted TSVs) — nothing raw
     accumulates day over day, only the database.
  6. Appends one plain-English summary line to logs/refresh.log.

These export files are known to contain stray NUL bytes (a real quirk in
the state's own extract, not a bug here) which break Python's csv module
outright, so every file is read through _clean_lines() first.

MEMORY: a real incident (2026-08-18) OOM-killed this job on Render's
Starter plan (512MB) — the two biggest files (CVR_LOBBY_DISCLOSURE_CD,
~569k rows; FILERNAME_CD, ~346k rows) were being kept as full-width
Python dicts (~50+ columns each via csv.DictReader), which cost well
over a gigabyte at this data's real scale. Trimming to just the needed
fields brought a standalone run down to ~470MB peak — better, but still
too close to the ceiling once you add the web server's own baseline
memory in the process that actually runs this in production. So instead:
those two lookups now live in temp SQLite tables (disk-backed, not
Python heap) rather than dicts, and the three largest files are read via
csv.reader (positional lists) instead of csv.DictReader (a full dict per
row), which cuts down the sheer volume of small object churn across the
~1.8 million total rows this job reads.
"""

import csv
import os
import shutil
import subprocess
import tempfile
import time
import zipfile

import calaccess_db as db  # named uniquely — legiscan-lookup/app.py imports this
                            # module alongside its OWN db.py in the same process
                            # (for hosted deployment); a shared name "db" would
                            # collide in sys.modules and silently import the wrong one

EXPORT_URL = "https://campaignfinance.cdn.sos.ca.gov/dbwebexport.zip"
NEEDED_MEMBERS = [
    "CalAccess/DATA/CVR_REGISTRATION_CD.TSV",
    "CalAccess/DATA/FILERNAME_CD.TSV",
    "CalAccess/DATA/CVR_LOBBY_DISCLOSURE_CD.TSV",
    "CalAccess/DATA/LPAY_CD.TSV",
]

# ENTITY_CD as it appears on Form 601/602/603 cover pages.
ENTITY_TYPE_BY_CODE = {"FRM": "firm", "LEM": "employer", "LCO": "coalition"}
SOURCE_FORM_BY_TYPE = {"F601": "601", "F602": "602", "F603": "603"}

INSERT_BATCH = 5000  # rows buffered before each executemany, for the temp-table loads

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_PATH = os.path.join(LOG_DIR, "refresh.log")


def log(message):
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%b %-d, %-I:%M%p").lower()
    with open(LOG_PATH, "a") as f:
        f.write(f"{stamp} — {message}\n")
    # flush=True: stdout is block-buffered, not line-buffered, whenever
    # it isn't attached to a terminal — exactly the case when this runs
    # as a background thread inside app.py on Render. Real incident: an
    # entire run's worth of log lines never appeared in Render's log
    # stream because this job's total output was nowhere near the buffer
    # size needed to trigger a flush on its own, and the web server
    # process never exits to flush it at shutdown either.
    print(message, flush=True)


def _clean_lines(path):
    """Strips stray NUL bytes the state's export is known to contain —
    without this, csv.DictReader/csv.reader crashes outright partway
    through."""
    with open(path, "rb") as f:
        for line in f:
            yield line.replace(b"\x00", b"").decode("utf-8", errors="replace")


def _rows(path, wanted_fields):
    """Reads a tab-delimited export file and yields a small dict holding
    ONLY wanted_fields per row, using csv.reader (a plain positional
    list per row) rather than csv.DictReader (a full-width dict — every
    one of the ~50-70 source columns — per row). At this data's real
    scale (hundreds of thousands to low millions of rows across this
    job's 4 files), building one full dict per row versus one small dict
    per row is the difference between the OOM incident this docstring
    describes and comfortably fitting in 512MB.
    """
    lines = _clean_lines(path)
    header = next(csv.reader([next(lines)], delimiter="\t"))
    positions = {name: header.index(name) for name in wanted_fields if name in header}
    for raw in csv.reader(lines, delimiter="\t"):
        yield {name: (raw[i] if i < len(raw) else None) for name, i in positions.items()}


def download_export(dest_path):
    """Shells out to curl rather than using urllib.request. This isn't
    just style: four real scheduled runs in a row failed with generic
    network errors (ETIMEDOUT, connection reset) using urllib, while an
    earlier manual curl download of the same 1.5GB file succeeded cleanly
    — consistent with the state's CDN/WAF (Imperva/Incapsula headers were
    visible in its response) treating Python's default urllib user agent
    differently from curl's. --retry/--retry-delay give it a few real
    chances against a transient drop instead of failing on the first one,
    and -m caps how long a single fully-stalled attempt can hang for.
    """
    log("downloading raw export…")
    cmd = [
        "curl", "-fSL",
        "--retry", "3", "--retry-delay", "10", "--retry-all-errors",
        "-m", "1800",
        "-o", dest_path,
        EXPORT_URL,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"curl exited {result.returncode}: {result.stderr.strip()}")
    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    log(f"downloaded {size_mb:.0f}MB")


def extract_needed(zip_path, dest_dir):
    with zipfile.ZipFile(zip_path) as zf:
        for member in NEEDED_MEMBERS:
            zf.extract(member, dest_dir)
    return {m: os.path.join(dest_dir, m) for m in NEEDED_MEMBERS}


def load_filer_status_table(conn, path):
    """Loads FILERNAME_CD (~346k rows) into a temp SQLite table instead
    of a Python dict — this was one of the two biggest memory costs
    before. INSERT OR IGNORE keeps the first-seen row per filer (the
    file isn't in a guaranteed order, but status rarely changes within a
    session, so first-seen is good enough — same semantic as before)."""
    conn.execute("DROP TABLE IF EXISTS tmp_filer_status")
    conn.execute("CREATE TEMP TABLE tmp_filer_status (filer_id TEXT PRIMARY KEY, status TEXT)")
    batch = []
    for row in _rows(path, ["FILER_ID", "STATUS"]):
        fid = row["FILER_ID"]
        if not fid:
            continue
        batch.append((fid, row["STATUS"]))
        if len(batch) >= INSERT_BATCH:
            conn.executemany("INSERT OR IGNORE INTO tmp_filer_status VALUES (?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT OR IGNORE INTO tmp_filer_status VALUES (?,?)", batch)
    conn.commit()


def get_filer_status(conn, filer_id):
    row = conn.execute(
        "SELECT status FROM tmp_filer_status WHERE filer_id = ?", (filer_id,)
    ).fetchone()
    return row["status"] if row else None


def load_registrations(path):
    """FILER_ID -> latest-amendment Form 601/602/603 fields, trimmed to
    just what sync_entities() reads. Stays a plain Python dict — after
    dedup this is only ~10k entries (one per registered firm/employer),
    nowhere near the scale that caused the two big tables above to move
    into SQLite instead.

    Form 602 matters as much as 601/603: an employer who only lobbies
    through a hired firm is often never independently registered under
    603 at all — their name/address instead lives in the firm's 602
    attachment (confirmed against real 2026 disclosures: ~35% of current
    employer-side lines pointed at a filer with only an F602 on file, no
    F603). Skipping it would silently leave those clients out of
    lobbying_entities even though CAL-ACCESS does have their name/address.
    """
    fields = ["FILER_ID", "FORM_TYPE", "AMEND_ID", "FILER_NAML", "ENTITY_CD",
              "BUS_CITY", "BUS_ST", "BUS_ZIP4"]
    latest_amend = {}
    latest = {}
    for row in _rows(path, fields):
        if row["FORM_TYPE"] not in ("F601", "F602", "F603"):
            continue
        fid = row["FILER_ID"]
        amend = int(row["AMEND_ID"] or 0)
        if fid not in latest_amend or amend >= latest_amend[fid]:
            latest_amend[fid] = amend
            latest[fid] = {
                "FILER_NAML": row["FILER_NAML"],
                "ENTITY_CD": row["ENTITY_CD"],
                "BUS_CITY": row["BUS_CITY"],
                "BUS_ST": row["BUS_ST"],
                "BUS_ZIP4": row["BUS_ZIP4"],
                "FORM_TYPE": row["FORM_TYPE"],
            }
    return latest


def load_disclosure_filings_table(conn, path):
    """Loads CVR_LOBBY_DISCLOSURE_CD (~569k rows, ~400k distinct
    filings) into a temp SQLite table instead of a Python dict — this
    was the single largest memory cost before, and the direct cause of
    a real OOM kill on Render's Starter plan (512MB) on 2026-08-18.

    All amendments get inserted first, then a dedup pass keeps only the
    max-amend_id row per filing_id, using SQLite's documented "bare
    columns alongside MIN()/MAX()" behavior — the non-aggregated columns
    in a query with exactly one MAX() come from the same row that
    supplied the max value, which is exactly "give me the latest
    amendment's fields" without a slower correlated subquery.
    """
    conn.execute("DROP TABLE IF EXISTS tmp_filings_raw")
    conn.execute("""CREATE TEMP TABLE tmp_filings_raw (
        filing_id TEXT, amend_id INTEGER, filer_id TEXT,
        from_date TEXT, thru_date TEXT, rpt_date TEXT
    )""")
    fields = ["FILING_ID", "AMEND_ID", "FILER_ID", "FROM_DATE", "THRU_DATE", "RPT_DATE"]
    batch = []
    for row in _rows(path, fields):
        batch.append((
            row["FILING_ID"], int(row["AMEND_ID"] or 0), row["FILER_ID"],
            row["FROM_DATE"], row["THRU_DATE"], row["RPT_DATE"],
        ))
        if len(batch) >= INSERT_BATCH:
            conn.executemany("INSERT INTO tmp_filings_raw VALUES (?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO tmp_filings_raw VALUES (?,?,?,?,?,?)", batch)

    conn.execute("DROP TABLE IF EXISTS tmp_filings")
    conn.execute("""
        CREATE TEMP TABLE tmp_filings AS
        SELECT filing_id, filer_id, from_date, thru_date, rpt_date, MAX(amend_id) AS amend_id
        FROM tmp_filings_raw
        GROUP BY filing_id
    """)
    conn.execute("CREATE INDEX idx_tmp_filings_id ON tmp_filings(filing_id)")
    conn.execute("DROP TABLE tmp_filings_raw")
    conn.commit()


def get_filing(conn, filing_id):
    row = conn.execute(
        "SELECT filer_id, from_date, thru_date, rpt_date FROM tmp_filings WHERE filing_id = ?",
        (filing_id,),
    ).fetchone()
    return dict(row) if row else None


def sync_entities(conn, registrations):
    count = 0
    for filer_id, row in registrations.items():
        name = (row.get("FILER_NAML") or "").strip()
        if not name:
            continue
        entity_cd = (row.get("ENTITY_CD") or "").strip()
        entity_type = ENTITY_TYPE_BY_CODE.get(
            entity_cd, "firm" if row.get("FORM_TYPE") == "F601" else "employer"
        )
        db.upsert_entity(conn, {
            "filer_id": filer_id,
            "name": name,
            "entity_type": entity_type,
            "address": None,  # street address isn't in the daily export — see reference guide
            "city": (row.get("BUS_CITY") or "").strip(),
            "state": (row.get("BUS_ST") or "").strip(),
            "zip": (row.get("BUS_ZIP4") or "").strip(),
            "registration_status": get_filer_status(conn, filer_id),
            "source_form": SOURCE_FORM_BY_TYPE.get(row.get("FORM_TYPE")),
        })
        count += 1
    conn.commit()
    return count


COMMIT_EVERY = 5000  # rows between commits — see sync_disclosures docstring


def sync_disclosures(conn, lpay_path):
    """Commits every COMMIT_EVERY rows rather than once at the very end.
    ~850k LPAY_CD rows in one uncommitted transaction means any
    interruption (a crash, an OOM kill, a redeploy) loses ALL of it —
    exactly what happened in the real incident that motivated the memory
    work above. Committing in batches means a future interruption loses
    at most one batch's worth of work, not the whole run."""
    new_count = 0
    updated_count = 0
    unmatched_filer = 0
    unmatched_filing = 0
    processed_since_commit = 0
    fields = ["FORM_TYPE", "FILING_ID", "EMPLR_NAML", "PER_TOTAL", "LBY_ACTVTY"]
    for row in _rows(lpay_path, fields):
        form_type = row["FORM_TYPE"]
        if form_type not in ("F625P2", "F635P3B"):
            continue
        filing_id = row["FILING_ID"]
        cv = get_filing(conn, filing_id)
        if not cv:
            unmatched_filing += 1
            continue
        entity_id = db.entity_id_for_filer(conn, cv.get("filer_id"))
        if entity_id is None:
            unmatched_filer += 1
            continue

        client_name = (row["EMPLR_NAML"] or "").strip()
        try:
            amount = float(row["PER_TOTAL"] or 0)
        except ValueError:
            amount = 0.0

        existing = conn.execute(
            "SELECT 1 FROM lobbying_disclosures WHERE filing_id = ? AND client_name = ?",
            (filing_id, client_name),
        ).fetchone()

        db.upsert_disclosure(conn, {
            "filing_id": filing_id,
            "filer_entity_id": entity_id,
            "client_name": client_name,
            "form_type": form_type,
            "period_start": cv.get("from_date"),
            "period_end": cv.get("thru_date"),
            "amount_spent": amount,
            "raw_bill_text": row["LBY_ACTVTY"],
            "filed_date": cv.get("rpt_date"),
        })
        if existing:
            updated_count += 1
        else:
            new_count += 1

        processed_since_commit += 1
        if processed_since_commit >= COMMIT_EVERY:
            conn.commit()
            processed_since_commit = 0

    conn.commit()
    return new_count, updated_count, unmatched_filer, unmatched_filing


def main():
    start = time.time()
    db.init_db()

    tmp_dir = tempfile.mkdtemp(prefix="calaccess_")
    try:
        zip_path = os.path.join(tmp_dir, "dbwebexport.zip")
        try:
            download_export(zip_path)
        except Exception as e:
            log(f"download failed — {e}")
            return {"ok": False, "stage": "download", "error": str(e)}

        log("extracting needed files…")
        paths = extract_needed(zip_path, tmp_dir)

        conn = db.get_connection()
        try:
            log("loading firm/employer registrations (Forms 601/603)…")
            load_filer_status_table(conn, paths["CalAccess/DATA/FILERNAME_CD.TSV"])
            registrations = load_registrations(paths["CalAccess/DATA/CVR_REGISTRATION_CD.TSV"])
            entity_count = sync_entities(conn, registrations)
            registrations = None  # done with it — let it go before the next big load

            log("loading quarterly disclosures (Forms 625P2/635P3B)…")
            load_disclosure_filings_table(conn, paths["CalAccess/DATA/CVR_LOBBY_DISCLOSURE_CD.TSV"])
            new_c, updated_c, unmatched_filer, unmatched_filing = sync_disclosures(
                conn, paths["CalAccess/DATA/LPAY_CD.TSV"]
            )
        finally:
            conn.close()

        elapsed = time.time() - start
        log(
            f"{entity_count} active firms/employers, "
            f"{new_c} new disclosure lines, {updated_c} updated, "
            f"{unmatched_filer} skipped (firm not registered), "
            f"{unmatched_filing} skipped (filing not found) — {elapsed:.0f}s"
        )
        return {
            "ok": True,
            "entities": entity_count,
            "new_disclosures": new_c,
            "updated_disclosures": updated_c,
            "unmatched_filer": unmatched_filer,
            "unmatched_filing": unmatched_filing,
            "elapsed_seconds": round(elapsed),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
