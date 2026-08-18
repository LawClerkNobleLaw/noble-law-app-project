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

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_PATH = os.path.join(LOG_DIR, "refresh.log")


def log(message):
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%b %-d, %-I:%M%p").lower()
    with open(LOG_PATH, "a") as f:
        f.write(f"{stamp} — {message}\n")
    print(message)


def _clean_lines(path):
    """Strips stray NUL bytes the state's export is known to contain —
    without this, csv.DictReader crashes outright partway through."""
    with open(path, "rb") as f:
        for line in f:
            yield line.replace(b"\x00", b"").decode("utf-8", errors="replace")


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


def load_filer_status(path):
    """FILER_ID -> current status ('ACTIVE', 'TERMINATED', etc.), from the
    first row seen per filer (the file isn't in a guaranteed order, but
    status rarely changes within a session so first-seen is good enough)."""
    status = {}
    for row in csv.DictReader(_clean_lines(path), delimiter="\t"):
        fid = row.get("FILER_ID")
        if fid and fid not in status:
            status[fid] = row.get("STATUS")
    return status


def load_registrations(path):
    """FILER_ID -> latest-amendment Form 601/602/603 row.

    Form 602 matters as much as 601/603: an employer who only lobbies
    through a hired firm is often never independently registered under
    603 at all — their name/address instead lives in the firm's 602
    attachment (confirmed against real 2026 disclosures: ~35% of current
    employer-side lines pointed at a filer with only an F602 on file, no
    F603). Skipping it would silently leave those clients out of
    lobbying_entities even though CAL-ACCESS does have their name/address.
    """
    latest = {}
    for row in csv.DictReader(_clean_lines(path), delimiter="\t"):
        if row.get("FORM_TYPE") not in ("F601", "F602", "F603"):
            continue
        fid = row.get("FILER_ID")
        amend = int(row.get("AMEND_ID") or 0)
        if fid not in latest or amend >= int(latest[fid].get("AMEND_ID") or 0):
            latest[fid] = row
    return latest


def load_disclosure_filings(path):
    """FILING_ID -> latest-amendment disclosure cover-page row."""
    latest = {}
    for row in csv.DictReader(_clean_lines(path), delimiter="\t"):
        fid = row.get("FILING_ID")
        amend = int(row.get("AMEND_ID") or 0)
        if fid not in latest or amend >= int(latest[fid].get("AMEND_ID") or 0):
            latest[fid] = row
    return latest


def sync_entities(conn, registrations, filer_status):
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
            "registration_status": filer_status.get(filer_id),
            "source_form": SOURCE_FORM_BY_TYPE.get(row.get("FORM_TYPE")),
        })
        count += 1
    conn.commit()
    return count


def sync_disclosures(conn, lpay_path, filings):
    new_count = 0
    updated_count = 0
    unmatched_filer = 0
    unmatched_filing = 0
    for row in csv.DictReader(_clean_lines(lpay_path), delimiter="\t"):
        form_type = row.get("FORM_TYPE")
        if form_type not in ("F625P2", "F635P3B"):
            continue
        filing_id = row.get("FILING_ID")
        cv = filings.get(filing_id)
        if not cv:
            unmatched_filing += 1
            continue
        entity_id = db.entity_id_for_filer(conn, cv.get("FILER_ID"))
        if entity_id is None:
            unmatched_filer += 1
            continue

        client_name = (row.get("EMPLR_NAML") or "").strip()
        try:
            amount = float(row.get("PER_TOTAL") or 0)
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
            "period_start": cv.get("FROM_DATE"),
            "period_end": cv.get("THRU_DATE"),
            "amount_spent": amount,
            "raw_bill_text": row.get("LBY_ACTVTY"),
            "filed_date": cv.get("RPT_DATE"),
        })
        if existing:
            updated_count += 1
        else:
            new_count += 1

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
            return

        log("extracting needed files…")
        paths = extract_needed(zip_path, tmp_dir)

        conn = db.get_connection()
        try:
            log("loading firm/employer registrations (Forms 601/603)…")
            filer_status = load_filer_status(paths["CalAccess/DATA/FILERNAME_CD.TSV"])
            registrations = load_registrations(paths["CalAccess/DATA/CVR_REGISTRATION_CD.TSV"])
            entity_count = sync_entities(conn, registrations, filer_status)

            log("loading quarterly disclosures (Forms 625P2/635P3B)…")
            filings = load_disclosure_filings(paths["CalAccess/DATA/CVR_LOBBY_DISCLOSURE_CD.TSV"])
            new_c, updated_c, unmatched_filer, unmatched_filing = sync_disclosures(
                conn, paths["CalAccess/DATA/LPAY_CD.TSV"], filings
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
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
