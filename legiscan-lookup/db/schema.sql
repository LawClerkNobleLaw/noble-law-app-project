-- BillWatch data model — Phase 1, Session 4 (per claude_code_execution_roadmap.md),
-- extended in Session 5 with a stored watch-list (bill_sponsors, watchlist).
--
-- Holds LegiScan bill data, CAL-ACCESS lobbying disclosures, the bridge table
-- linking the two (bill_lobbying_link — the hard part, Session 7), each
-- client's tracked interests, and — new — a stored watch-list that a daily
-- job re-checks instead of the app doing a live lookup every time. See
-- client_interest_tracking_framework.md (Desktop/Noble Law Internship/App Project)
-- for the plain-language model this schema implements.
--
-- SQLite. Every CREATE TABLE uses IF NOT EXISTS so this file can be safely
-- re-applied every time the app or the daily job starts.

CREATE TABLE IF NOT EXISTS bills (
  id              INTEGER PRIMARY KEY,   -- LegiScan's bill_id
  state           TEXT NOT NULL,
  bill_number     TEXT NOT NULL,
  session_label   TEXT,
  title           TEXT,
  description     TEXT,
  status_code     INTEGER,               -- LegiScan's numeric status
  status_label    TEXT,
  status_date     TEXT,
  url             TEXT,
  change_hash     TEXT,
  last_synced_at  TEXT
);

CREATE TABLE IF NOT EXISTS bill_status_history (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id   INTEGER NOT NULL REFERENCES bills(id),
  date      TEXT,
  chamber   TEXT,
  action    TEXT
);

CREATE TABLE IF NOT EXISTS bill_sponsors (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id   INTEGER NOT NULL REFERENCES bills(id),
  name      TEXT,
  party     TEXT,
  role      TEXT
);

CREATE TABLE IF NOT EXISTS votes (
  id           INTEGER PRIMARY KEY,      -- LegiScan's roll_call_id
  bill_id      INTEGER NOT NULL REFERENCES bills(id),
  date         TEXT,
  chamber      TEXT,
  description  TEXT,
  yea INTEGER, nay INTEGER, nv INTEGER, absent INTEGER, total INTEGER,
  passed       INTEGER                   -- 0/1
);

-- The stored watch-list. One shared list (no accounts system exists yet) —
-- one row per bill someone's added. `last_checked_at` is what the daily
-- job updates whether or not anything actually changed on the bill.
CREATE TABLE IF NOT EXISTS watchlist (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id         INTEGER NOT NULL UNIQUE REFERENCES bills(id),
  added_at        TEXT,
  last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS lobbying_entities (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  filer_id            TEXT,              -- CAL-ACCESS's own filer ID
  name                TEXT NOT NULL,
  entity_type         TEXT,              -- 'firm' | 'employer' | 'coalition'
  address             TEXT, city TEXT, state TEXT, zip TEXT,
  registration_status TEXT,
  source_form         TEXT,              -- '601' | '603'
  last_synced_at      TEXT
);

CREATE TABLE IF NOT EXISTS lobbyists (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  filer_id            TEXT,
  name                TEXT NOT NULL,
  firm_entity_id      INTEGER REFERENCES lobbying_entities(id),
  registration_status TEXT
);

CREATE TABLE IF NOT EXISTS lobbying_disclosures (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  filer_entity_id   INTEGER NOT NULL REFERENCES lobbying_entities(id),
  form_type         TEXT,               -- '625' | '635' | '645'
  period_start      TEXT,
  period_end        TEXT,
  amount_spent      REAL,
  raw_bill_text     TEXT,               -- exactly as filed: "AB1234", "AB 1234", etc.
  filed_date        TEXT,
  source_url        TEXT
);

CREATE TABLE IF NOT EXISTS bill_lobbying_link (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  disclosure_id    INTEGER NOT NULL REFERENCES lobbying_disclosures(id),
  bill_id          INTEGER REFERENCES bills(id),   -- NULL until matched
  match_confidence TEXT,               -- 'exact' | 'normalized' | 'manual' | 'unmatched'
  matched_at       TEXT,
  notes            TEXT
);

CREATE TABLE IF NOT EXISTS client_interests (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  client_name    TEXT NOT NULL,
  interest_label TEXT NOT NULL          -- e.g. "cannabis retail licensing"
);

CREATE TABLE IF NOT EXISTS client_interest_bills (
  interest_id INTEGER REFERENCES client_interests(id),
  bill_id     INTEGER REFERENCES bills(id),
  PRIMARY KEY (interest_id, bill_id)
);

CREATE TABLE IF NOT EXISTS client_interest_topics (
  interest_id INTEGER REFERENCES client_interests(id),
  keyword     TEXT,
  PRIMARY KEY (interest_id, keyword)
);

CREATE TABLE IF NOT EXISTS client_interest_entities (
  interest_id INTEGER REFERENCES client_interests(id),
  entity_id   INTEGER REFERENCES lobbying_entities(id),
  PRIMARY KEY (interest_id, entity_id)
);
