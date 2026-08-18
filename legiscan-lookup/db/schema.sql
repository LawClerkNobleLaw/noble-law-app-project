-- BillWatch data model — Phase 1, Session 4 (per claude_code_execution_roadmap.md),
-- extended in Session 5 with a stored watch-list (bill_sponsors, watchlist),
-- and in Session 6 with a real CAL-ACCESS ingestion pipeline (lobbying_entities
-- gained a UNIQUE filer_id for upserts; lobbying_disclosures gained
-- filing_id + client_name once real data showed one filing has multiple
-- per-client lines — see calaccess-pipeline/).
--
-- Holds LegiScan bill data, CAL-ACCESS lobbying disclosures, the bridge table
-- linking the two (bill_lobbying_link — the hard part, Session 7), each
-- client's tracked interests, and a stored watch-list that a daily job
-- re-checks instead of the app doing a live lookup every time. See
-- client_interest_tracking_framework.md (Desktop/Noble Law Internship/App Project)
-- for the plain-language model this schema implements.
--
-- SQLite. Every CREATE TABLE uses IF NOT EXISTS so this file can be safely
-- re-applied every time the app or either daily job starts.

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

-- LegiScan's own amendment documents for a bill — distinct from
-- bill_status_history's procedural events. Added for the per-bill
-- "action report" page.
CREATE TABLE IF NOT EXISTS bill_amendments (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id       INTEGER NOT NULL REFERENCES bills(id),
  amendment_id  INTEGER,           -- LegiScan's own ID, if present
  chamber       TEXT,
  date          TEXT,
  title         TEXT,
  description   TEXT,
  adopted       INTEGER,           -- 0/1
  url           TEXT
);

-- LegiScan's `calendar` array — scheduled committee/floor events for a
-- bill. Rows aren't limited to future dates; "upcoming" is an
-- application-layer filter (date >= today) applied when the action
-- report reads this table, so past hearings still stay on record.
CREATE TABLE IF NOT EXISTS bill_hearings (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id      INTEGER NOT NULL REFERENCES bills(id),
  event_type   TEXT,
  date         TEXT,
  time         TEXT,
  location     TEXT,
  description  TEXT
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
  filer_id            TEXT UNIQUE,       -- CAL-ACCESS's own filer ID — upserts key off this
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

-- One row per (filing, client) line. Real Form 625P2/635P3B filings list one
-- line per client/employer named, each with its own amount and bill text —
-- confirmed against a real Aug 2026 export before this table was finalized
-- (e.g. one Public Policy Advocates LLC filing named Meta Platforms and
-- reported $537,500 and "AB 2, SB 690" on a single line of a larger filing).
CREATE TABLE IF NOT EXISTS lobbying_disclosures (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id         TEXT,               -- CAL-ACCESS's own FILING_ID
  filer_entity_id   INTEGER NOT NULL REFERENCES lobbying_entities(id),
  client_name       TEXT,               -- client/employer named on this specific line, as filed
  form_type         TEXT,               -- '625P2' | '635P3B' etc., as filed
  period_start      TEXT,
  period_end        TEXT,
  amount_spent      REAL,
  raw_bill_text     TEXT,               -- exactly as filed: "AB 2, SB 690", "See Attachment A", etc.
  filed_date        TEXT,
  source_url        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_disclosure_filing_client
  ON lobbying_disclosures(filing_id, client_name);

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

-- Individual accounts, layered INSIDE the site's existing shared
-- LOOKUP_USER/PASSWORD login (see app.py's module docstring) — that
-- outer login still gates the whole site; this is a second, personal
-- layer inside it. password_hash is never the plain password — see
-- accounts.py for the PBKDF2 scheme.
CREATE TABLE IF NOT EXISTS users (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  email          TEXT NOT NULL UNIQUE,
  password_hash  TEXT NOT NULL,
  created_at     TEXT
);

-- One row per logged-in browser session. token is a random value set as
-- an HttpOnly cookie — looking it up here is how a request is tied back
-- to a user, instead of trusting anything the client claims about itself.
CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT
);

-- Sign-up step 2. Field names and order follow CAL-ACCESS Form 601 (the
-- Lobbying Firm Registration Statement) — see reference_guide.md and
-- calaccess-pipeline's own CVR_REGISTRATION_CD handling — so the
-- language matches what a lobbyist already recognizes from the real
-- state form, even though this collects it directly from the user
-- rather than pulling it from the daily CAL-ACCESS export (which is
-- why full street addresses are collectible here but aren't available
-- in lobbying_entities — the daily export only has city/state/zip).
CREATE TABLE IF NOT EXISTS lobbyist_profiles (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id            INTEGER NOT NULL UNIQUE REFERENCES users(id),
  legal_name         TEXT NOT NULL,        -- Form 601: FILER_NAML — "legal name of firm or individual"
  registrant_type    TEXT NOT NULL,        -- 'individual' | 'firm' — Form 601's ENTITY_CD concept
  bus_addr1          TEXT, bus_city TEXT, bus_st TEXT, bus_zip4 TEXT,   -- Form 601: BUS_CITY/BUS_ST/BUS_ZIP4 + street
  mail_same_as_bus   INTEGER NOT NULL DEFAULT 1,                        -- 0/1 — mailing address only if different
  mail_addr1         TEXT, mail_city TEXT, mail_st TEXT, mail_zip4 TEXT, -- Form 601: MAIL_CITY/MAIL_ST/MAIL_ZIP4
  bus_phone          TEXT,                 -- Form 601: BUS_PHON
  existing_filer_id  TEXT,                 -- optional — CA SOS filer ID, if already registered
  created_at         TEXT
);

-- "Flagged bills" (the user-facing term) — a personal, per-user list,
-- unlike `watchlist` above which is one shared list with no owner.
-- Reuses that same underlying machinery rather than duplicating it:
-- flagging a bill still upserts it into `bills` and adds it to the
-- shared `watchlist` (so the daily refresh job keeps it fresh) — this
-- table only adds the "which user cares about this bill" layer on top.
-- Many-to-many: one bill can be flagged by many users, one user can
-- flag many bills.
CREATE TABLE IF NOT EXISTS flagged_bills (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  bill_id    INTEGER NOT NULL REFERENCES bills(id),
  flagged_at TEXT,
  UNIQUE(user_id, bill_id)
);

-- A user's own clients — one-to-many (unlike lobbyist_profiles, which
-- is one-to-one with a user). Field names/order follow CAL-ACCESS Forms
-- 602 (Lobbying Firm Activity Authorization — the client's own
-- description of their industry/interests) and 603 (Lobbyist Employer
-- Registration — name/address), same reasoning as lobbyist_profiles:
-- language a lobbyist already recognizes from the real state forms.
-- existing_filer_id is deliberately unused by any matching logic yet —
-- it's stored now so a future feature can cross-check it against the
-- filer_id already loaded into lobbying_entities by calaccess-pipeline,
-- not built as part of this table.
CREATE TABLE IF NOT EXISTS clients (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id            INTEGER NOT NULL REFERENCES users(id),
  name               TEXT NOT NULL,        -- Form 603: client/employer name
  bus_addr1          TEXT, bus_city TEXT, bus_st TEXT, bus_zip4 TEXT,  -- Form 603-style business address
  interests          TEXT,                 -- Form 602: description of the client's industry/interests
  existing_filer_id  TEXT,                 -- optional — for future cross-check against lobbying_entities
  created_at         TEXT
);

-- Which of a user's own clients a flagged bill is being tracked for.
-- Many-to-many on purpose — a bill can matter to more than one client.
-- Both bill_id and client_id are scoped to user_id at the application
-- layer (db.link_bill_to_client checks the client is actually theirs
-- and the bill is actually one they've flagged before inserting), not
-- just left to the foreign keys, since SQLite FKs alone can't express
-- "these three all belong to the same user."
-- position is the client's stance on this bill: 'support' | 'oppose' |
-- 'watch' — validated in db.link_bill_to_client, not via a CHECK
-- constraint here, same style as registrant_type above. Defaults to
-- 'watch' (the most neutral stance) when a bill is first assigned to a
-- client; changeable afterward through the same function/endpoint.
CREATE TABLE IF NOT EXISTS bill_client_links (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id   INTEGER NOT NULL REFERENCES users(id),
  bill_id   INTEGER NOT NULL REFERENCES bills(id),
  client_id INTEGER NOT NULL REFERENCES clients(id),
  position  TEXT NOT NULL DEFAULT 'watch',
  linked_at TEXT,
  UNIQUE(user_id, bill_id, client_id)
);
