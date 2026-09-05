-- BillWatch data model — Phase 1, Session 4 (per claude_code_execution_roadmap.md),
-- extended in Session 5 with a stored watch-list (bill_sponsors, watchlist),
-- and in Session 6 with a real CAL-ACCESS ingestion pipeline (lobbying_entities
-- gained a UNIQUE filer_id for upserts; lobbying_disclosures gained
-- filing_id + client_name once real data showed one filing has multiple
-- per-client lines — see calaccess-pipeline/). Session 8 added indexes on
-- every foreign-key/lookup column actually queried by app.py/db.py — none
-- of this needs a migration; init_db() re-applies this whole file (every
-- statement is already IF NOT EXISTS) on every startup.
--
-- Holds LegiScan bill data, CAL-ACCESS lobbying disclosures, the bridge table
-- linking the two (bill_lobbying_link — the hard part, Session 7), and a
-- stored watch-list that a daily job re-checks instead of the app doing a
-- live lookup every time. (An earlier client_interests/client_interest_bills/
-- client_interest_topics/client_interest_entities design, from
-- client_interest_tracking_framework.md, was never built on top of — removed
-- rather than left as unused tables nothing reads or writes.)
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
  last_synced_at  TEXT,
  -- "When does this need to be amended by?" — checked LegiScan's raw
  -- getBill payload directly (every top-level key it returns) and it has
  -- no procedural-deadline field of any kind, so this is manually entered
  -- by a user on /report rather than synced from anywhere. Nullable —
  -- most bills won't have one set.
  amend_by_date   TEXT
);

CREATE TABLE IF NOT EXISTS bill_status_history (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id   INTEGER NOT NULL REFERENCES bills(id),
  date      TEXT,
  chamber   TEXT,
  action    TEXT
);
CREATE INDEX IF NOT EXISTS idx_bill_status_history_bill_id ON bill_status_history(bill_id);

CREATE TABLE IF NOT EXISTS bill_sponsors (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id   INTEGER NOT NULL REFERENCES bills(id),
  name      TEXT,
  party     TEXT,
  role      TEXT
);
CREATE INDEX IF NOT EXISTS idx_bill_sponsors_bill_id ON bill_sponsors(bill_id);

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
CREATE INDEX IF NOT EXISTS idx_bill_amendments_bill_id ON bill_amendments(bill_id);

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
CREATE INDEX IF NOT EXISTS idx_bill_hearings_bill_id ON bill_hearings(bill_id);

CREATE TABLE IF NOT EXISTS votes (
  id           INTEGER PRIMARY KEY,      -- LegiScan's roll_call_id
  bill_id      INTEGER NOT NULL REFERENCES bills(id),
  date         TEXT,
  chamber      TEXT,
  description  TEXT,
  yea INTEGER, nay INTEGER, nv INTEGER, absent INTEGER, total INTEGER,
  passed       INTEGER                   -- 0/1
);
CREATE INDEX IF NOT EXISTS idx_votes_bill_id ON votes(bill_id);

-- What the daily refresh actually observed change on a bill: one row per
-- change, appended, never updated.
--
-- This table exists because nothing else in the schema can answer "what
-- moved since yesterday" after the fact. upsert_bill() replaces a bill's
-- status history, amendments, hearings and votes wholesale on every run,
-- so the comparison is only possible in the moment refresh_one() makes
-- it, between snapshot_bill_state() and upsert_bill(). Once that call
-- returns, the previous state is gone.
--
-- Consequence worth knowing: there is no back-fill. This starts empty on
-- an existing database and only fills from the first refresh after it
-- ships, so the flagged list's "Last change" column falls back to the
-- bill's own latest recorded action until real history accumulates.
--
-- `summary` is the short chip label ("Enrolled", "Amended"); `description`
-- is the full sentence the digest email already sends. `event_date` is
-- the date the change itself carries (a hearing's date, an amendment's
-- date), which is not the same as detected_at — LegiScan often reports an
-- action days after it happened.
CREATE TABLE IF NOT EXISTS bill_change_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id      INTEGER NOT NULL REFERENCES bills(id),
  detected_at  TEXT NOT NULL,
  change_type  TEXT NOT NULL,            -- status | amendment | hearing | vote
  summary      TEXT NOT NULL,
  description  TEXT NOT NULL,
  event_date   TEXT
);
CREATE INDEX IF NOT EXISTS idx_bill_change_events_bill_id
  ON bill_change_events(bill_id, detected_at DESC);

-- Saved searches — the structural hole this app had until now.
--
-- Monitoring only ever applied to bills someone had already flagged, so
-- the daily job could not, by design, see the bill introduced last week
-- that nobody has noticed yet — which is exactly the one that hurts a
-- client. A saved search is a query the refresh job re-runs every day,
-- reporting whatever is new since the last run.
--
-- `client_id` is optional and is what a new match gets auto-assigned to
-- when the user flags it: one saved search per client covers most of a
-- firm's needs. Unlike position_history.client_id (a historical record
-- that has to outlive the client), this is a live association, so it is
-- a real reference and delete_client() clears it.
CREATE TABLE IF NOT EXISTS saved_searches (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  name        TEXT NOT NULL,
  query       TEXT NOT NULL,
  client_id   INTEGER REFERENCES clients(id),
  created_at  TEXT,
  last_run_at TEXT,
  UNIQUE(user_id, name)
);

-- Every bill a saved search has ever matched, so "new since last run"
-- means something. Without this the job would have to either re-report
-- the same 119 results every morning or keep a high-water mark by date,
-- and LegiScan's relevance ordering is not a date.
--
-- bill_number/title are stored alongside the id because a match is
-- reported in an email before anyone has opened the bill — this app has
-- no bills row for it yet, and fetching one would be a getBill call per
-- match just to write a subject line.
--
-- reported flips to 1 once the match has gone out in a digest, so a
-- failed or unconfigured send doesn't silently swallow the news.
CREATE TABLE IF NOT EXISTS saved_search_matches (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  saved_search_id INTEGER NOT NULL REFERENCES saved_searches(id),
  bill_id         INTEGER NOT NULL,
  bill_number     TEXT,
  title           TEXT,
  last_action     TEXT,
  first_seen_at   TEXT NOT NULL,
  reported        INTEGER NOT NULL DEFAULT 0,
  UNIQUE(saved_search_id, bill_id)
);
CREATE INDEX IF NOT EXISTS idx_saved_search_matches_search
  ON saved_search_matches(saved_search_id, first_seen_at DESC);

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
CREATE INDEX IF NOT EXISTS idx_disclosures_client_name ON lobbying_disclosures(client_name);
CREATE INDEX IF NOT EXISTS idx_disclosures_filer_entity_id ON lobbying_disclosures(filer_entity_id);

CREATE TABLE IF NOT EXISTS bill_lobbying_link (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  disclosure_id    INTEGER NOT NULL REFERENCES lobbying_disclosures(id),
  bill_id          INTEGER REFERENCES bills(id),   -- NULL until matched
  match_confidence TEXT,               -- 'exact' | 'normalized' | 'manual' | 'unmatched'
  matched_at       TEXT,
  notes            TEXT
);

-- The firm. Everything a firm's work product consists of — its clients,
-- the bills it tracks, the positions it holds, the filings it prepares,
-- the letters it writes — belongs to one of these rather than to
-- whichever person happened to type it in.
--
-- Introduced while every organization still has exactly one seat, on
-- purpose. The customer is a firm and the data model was one person: a
-- second lobbyist at the same firm could not see the client's position,
-- and Form 601, which exists to register a firm's lobbyists, could only
-- ever list one. That is the kind of thing that gets more expensive to
-- fix with every week of real filing history, so the layer goes in
-- before the seats do.
CREATE TABLE IF NOT EXISTS organizations (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  created_at TEXT
);

-- The firm's lobbyists, as Form 601 needs them listed. Separate from
-- users: a firm registers lobbyists who may not have a login here, and
-- someone with a login (an assistant, an associate) is not necessarily a
-- registered lobbyist. `user_id` links the two when they are the same
-- person, and is NULL when they aren't.
CREATE TABLE IF NOT EXISTS org_lobbyists (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id     INTEGER NOT NULL REFERENCES organizations(id),
  user_id    INTEGER REFERENCES users(id),
  name       TEXT NOT NULL,
  cert_id    TEXT,                    -- optional CA SOS lobbyist certification ID
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_lobbyists_org ON org_lobbyists(org_id);

-- Which bills each PERSON has looked at. Split out of flagged_bills when
-- the flag itself became the firm's rather than the individual's: the
-- flag is "our firm tracks this", the view is "I have read this", and
-- one lobbyist opening a bill must not clear their colleague's dot.
CREATE TABLE IF NOT EXISTS bill_views (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        INTEGER NOT NULL REFERENCES users(id),
  bill_id        INTEGER NOT NULL REFERENCES bills(id),
  last_viewed_at TEXT NOT NULL,
  UNIQUE(user_id, bill_id)
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
  -- Which firm this person works at. Every account gets one at sign-up
  -- (see accounts.create_user) — a solo lobbyist is a firm of one, not a
  -- special case with a NULL here.
  org_id         INTEGER REFERENCES organizations(id),
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

-- "Flagged bills" (the user-facing term) — the firm's list, unlike
-- `watchlist` above which is one global list with no owner at all.
-- user_id is who flagged it; ownership is that person's organization
-- (see db.ORG_SCOPE), so a colleague sees the same bills.
-- Reuses that same underlying machinery rather than duplicating it:
-- flagging a bill still upserts it into `bills` and adds it to the
-- shared `watchlist` (so the daily refresh job keeps it fresh) — this
-- table only adds the "which firm cares about this bill" layer on top.
-- Many-to-many: one bill can be flagged by many firms, one firm can flag
-- many bills.
CREATE TABLE IF NOT EXISTS flagged_bills (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  bill_id    INTEGER NOT NULL REFERENCES bills(id),
  flagged_at TEXT,
  -- The firm's own free-text note on this bill. Per flag, not per bill:
  -- two firms tracking the same bill have nothing to say to each other,
  -- and this sits alongside their flag rather than on the shared `bills`
  -- row the refresh job overwrites. Read and edited by anyone at the
  -- firm, same as the flag it hangs off.
  notes      TEXT,
  -- When this user last opened this bill's report. What makes the
  -- flagged list a to-do instead of an inventory: anything in
  -- bill_change_events detected after this instant is unread, and the
  -- row carries a dot until the user goes and looks. NULL means never
  -- opened, which is treated as "everything recorded is unread" — the
  -- honest reading, since the user has demonstrably not seen any of it.
  last_viewed_at TEXT,
  -- NULL while this flag is active. Set to the moment it was archived,
  -- otherwise. "Unflag" used to DELETE this row and every bill_client_links
  -- row hanging off it — the position a firm took, gone with no way back.
  -- Archiving keeps the row (and its client assignments, and its notes)
  -- and just stops the daily refresh and the digest from caring about it.
  -- Flagging the same bill again clears this back to NULL (see
  -- flag_bill's ON CONFLICT clause), so "restore" is the same action as
  -- flagging, not a second code path.
  archived_at TEXT,
  UNIQUE(user_id, bill_id)
);
CREATE INDEX IF NOT EXISTS idx_flagged_bills_user_id ON flagged_bills(user_id);

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
  bus_phone          TEXT,                 -- business phone — not on any CAL-ACCESS filer record, manual-only
  interests          TEXT,                 -- Form 602: description of the client's industry/interests
  existing_filer_id  TEXT,                 -- optional — for future cross-check against lobbying_entities
  -- Form 601 Part II asks for these three per client relationship —
  -- added once "Prepare my disclosure form" (pdf_forms.py) needed them;
  -- all optional, since not every client relationship has them decided
  -- yet and existing clients were created before these existed.
  effective_date     TEXT,                 -- Form 601: "Effective Date" — when lobbying for this client began
  contract_period    TEXT,                 -- Form 601: "Period of Contract" — free text (e.g. a date range, or "Ongoing")
  agencies_lobbied   TEXT,                 -- Form 601: "Agencies to be Lobbied" on this client's behalf
  -- What the firm is paid for this client, and on what basis. The
  -- quarterly forms (615 in particular) report a compensation figure per
  -- client per period, and the client record was the one place that
  -- number could live without being re-typed into every filing.
  --
  -- Two columns rather than one string, because a filing needs the
  -- NUMBER and a human needs the BASIS: "$5,000" alone can't be turned
  -- into a quarter, and "$5,000/month" can't be added up. amount is a
  -- plain decimal string (TEXT, not REAL — money through binary floats
  -- is a rounding bug waiting for a total), period is one of
  -- db.COMPENSATION_PERIODS.
  --
  -- No quarterly derivation is written yet, on purpose: Form 615 doesn't
  -- exist in this app, and deriving a statutory figure for a form nobody
  -- can file would be guessing at the form's own rules. The fact is
  -- stored so 615 can read it; the arithmetic belongs with 615.
  compensation_amount TEXT,
  compensation_period TEXT,                -- monthly | quarterly | annual | hourly | other
  -- The firm's own running notes on this relationship. The bill-level
  -- equivalent is flagged_bills.notes; this is the client-level one, and
  -- like that one it belongs to the firm rather than to whoever typed
  -- it (see the ORG_SCOPE note in db.py).
  notes              TEXT,
  created_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_clients_user_id ON clients(user_id);

-- ── Who to call at a client ───────────────────────────────────────────
--
-- A client record held an address and no people. Every real question
-- about a bill ("do they want us to oppose this?") is a question for a
-- person, and the firm was keeping those names somewhere this app
-- couldn't see.
--
-- Its own table rather than columns on clients, because the count is
-- genuinely open: a trade association has a GC, a policy director and a
-- comms lead, and a one-person shop has one contact. Org-owned through
-- the creating user like everything else here.
CREATE TABLE IF NOT EXISTS client_contacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  client_id  INTEGER NOT NULL REFERENCES clients(id),
  name       TEXT NOT NULL,
  title      TEXT,
  email      TEXT,
  phone      TEXT,
  -- The one to call first. At most one per client is enforced in
  -- db.py (set_primary_contact) rather than by a constraint, since
  -- SQLite can't express "at most one row per client with this flag".
  is_primary INTEGER NOT NULL DEFAULT 0,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_client_contacts_client ON client_contacts(client_id);

-- Which of a user's own clients a flagged bill is being tracked for.
-- Many-to-many on purpose — a bill can matter to more than one client.
-- Both bill_id and client_id are scoped to the organization at the application
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
  -- The date this position took effect, as opposed to the moment
  -- somebody clicked the dropdown. They're usually the same day and
  -- occasionally aren't: a position agreed with the client on Monday and
  -- entered on Thursday took effect Monday, and "what was our position
  -- when we testified in June" is a question about the former.
  -- Defaults to the California date of the change; editable after.
  effective_date TEXT,
  linked_at TEXT,
  UNIQUE(user_id, bill_id, client_id)
);

-- Append-only record of every position this user has held for a client
-- on a bill. bill_client_links carries the current answer and is
-- overwritten in place; this carries how it got there.
--
-- The reason it exists is a question that has a right answer and, before
-- this table, no way to reach it: "what was our position when we
-- testified in June?" A support-to-oppose flip is the most consequential
-- single click in this product, and it used to leave no trace at all.
--
-- A row with to_position NULL is the client being taken off the bill
-- entirely. The link row is deleted; this one survives it, which is the
-- point — a removal is exactly the event someone would later need to
-- account for.
--
-- changed_by is the user who made the change. Identical to user_id
-- today, since an account is a single person (see P1-14 in the product
-- audit — an organization above the user is coming), and recorded
-- separately now so that when it stops being identical there is history
-- to read rather than a column added after the fact.
CREATE TABLE IF NOT EXISTS position_history (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        INTEGER NOT NULL REFERENCES users(id),
  bill_id        INTEGER NOT NULL REFERENCES bills(id),
  -- Deliberately NOT a foreign key, and paired with the name as it read
  -- at the time. A record of what a firm's position was has to outlive
  -- the client row it referred to: with a real reference, deleting a
  -- client would either be blocked by this table or take the history
  -- with it, and both of those are worse than a dangling id. Same
  -- snapshot reasoning as prepared_filings.field_data.
  client_id      INTEGER NOT NULL,
  client_name    TEXT,
  from_position  TEXT,                       -- NULL on the first assignment
  to_position    TEXT,                       -- NULL when the client was removed
  effective_date TEXT,
  changed_at     TEXT NOT NULL,
  changed_by     INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_position_history_bill
  ON position_history(user_id, bill_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_position_history_client
  ON position_history(user_id, client_id, changed_at DESC);

-- Position letters — the deliverable a lobbyist actually hands to a
-- member's office, and the step that justifies keeping position data in
-- this app at all.
--
-- Seeded from the bill, the client, the position and the next hearing,
-- then edited freely: `body` is whatever the user ended up with, not a
-- template plus variables. Regenerating a seed would overwrite what they
-- wrote, so nothing here ever does.
--
-- Same boundary as prepared_filings: this app writes documents, it does
-- not send them. There is no recipient field and no send action —
-- printing or copying it out is the whole delivery path.
--
-- client_id and bill_id are recorded with the names/labels alongside
-- them for the same reason position_history does it: a letter is a
-- document that was written on a date and has to stay readable
-- afterwards, whatever happens to the client record later.
CREATE TABLE IF NOT EXISTS letters (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL REFERENCES users(id),
  bill_id      INTEGER,
  bill_label   TEXT,                      -- "CA SB1159" as it read when written
  client_id    INTEGER,
  client_name  TEXT,
  position     TEXT,                      -- the stance at the time of writing
  subject      TEXT NOT NULL,
  body         TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_letters_user ON letters(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_letters_bill ON letters(user_id, bill_id);
CREATE INDEX IF NOT EXISTS idx_letters_client ON letters(user_id, client_id);

-- "Prepare my disclosure form" — one row per draft/prepared filing.
-- field_data is a JSON snapshot of every value used to fill the PDF at
-- the moment it was generated, not a live pointer back to the profile/
-- clients tables — so what a user reviews and signs off on can't
-- silently drift if they edit their profile or clients afterward. If
-- their real data changes, they prepare a fresh filing rather than this
-- one mutating out from under a signature.
--
-- status is derived, not just decorative: 'draft' until both
-- signed_name and confirmed_accurate are set (see db.sign_off_filing),
-- then 'ready_to_file'. There is deliberately no status beyond that —
-- this app never submits anything to the FPPC or Secretary of State;
-- "ready_to_file" only means "reviewed and ready for the user to go
-- file it themselves."
CREATE TABLE IF NOT EXISTS prepared_filings (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id            INTEGER NOT NULL REFERENCES users(id),
  form_type          TEXT NOT NULL,            -- '601' for now — more forms later
  period_label       TEXT,                     -- NULL when the form has no reporting period (601: none — it's session-based, computed automatically)
  field_data         TEXT NOT NULL,            -- JSON: the exact values used to fill the PDF
  status             TEXT NOT NULL DEFAULT 'draft',  -- 'draft' | 'ready_to_file'
  signed_name        TEXT,
  confirmed_accurate INTEGER NOT NULL DEFAULT 0,      -- 0/1
  signed_at          TEXT,
  -- Which account signed off, as opposed to which name was typed into
  -- the box. Now that a filing belongs to the firm rather than to one
  -- person, "prepared by A, signed off by B" is a thing that can happen,
  -- and the filing is the one place in this app where being able to say
  -- who attested to what has a statutory consequence rather than a
  -- client-relations one.
  signed_by          INTEGER REFERENCES users(id),
  -- Both added for the in-place HTML editor (see
  -- docs/disclosure-html-editor-plan.md) — neither existed when this
  -- filing model was PDF-preview-only.
  pdf_field_data_hash TEXT,                    -- sha256 of field_data as of the last "generate PDF" click; NULL or mismatched vs the CURRENT field_data means the PDF on hand is stale and sign-off is blocked (see db.sign_off_prepared_filing / db._hash_field_data)
  client_row_ids     TEXT,                     -- JSON array of client ids, in order, currently filling the (up to 9) client rows — lets the editor show/reorder them when a firm has more than 9 clients
  created_at         TEXT,
  -- The filing deadline, and the real-world event it's counted from.
  -- Both entered/derived, never inferred from created_at: when a draft
  -- was opened says nothing about when the state needs the filing. The
  -- lobbyist supplies trigger_date (for a 601, the date the firm
  -- qualified); due_date is derived from it by
  -- disclosure_fields.due_date_for and stays editable, since the
  -- statutory reading is theirs to make, not this app's.
  trigger_date       TEXT,
  due_date           TEXT
);

-- ── What the digest is allowed to say, and to whom ────────────────────
--
-- The digest email was half the product and had no settings at all: it
-- went out daily, to the account's own address, about every change on
-- every flagged bill. These two tables are the user's side of that
-- conversation.
--
-- Per PERSON, not per firm — unlike clients, flagged bills and filings,
-- which belong to the organization. Where my mail lands and how often is
-- mine; a colleague turning their own digest off must not turn off
-- everyone's. Same reasoning as bill_views above.
--
-- A missing row means "the defaults", so nothing has to be backfilled
-- and an account that never visits Profile keeps behaving exactly as it
-- did before this table existed — see db.get_notification_prefs, which
-- is the only thing that should ever read these columns directly.
CREATE TABLE IF NOT EXISTS notification_prefs (
  user_id          INTEGER PRIMARY KEY REFERENCES users(id),
  -- daily | weekdays | weekly | off. The daily refresh job decides
  -- whether today is a send day for this row (digest._is_send_day);
  -- 'weekly' is the one value that changes what the email CONTAINS as
  -- well as when it goes, since a Monday roll-up has to cover the six
  -- days the job already ran and said nothing (see
  -- db.changes_by_bill_since, reading bill_change_events).
  frequency        TEXT NOT NULL DEFAULT 'daily',
  -- Comma-separated subset of status,amendment,hearing,vote — the
  -- change_type vocabulary diff_bill_state() emits and
  -- bill_change_events stores. Empty string is a legal value and means
  -- "no flagged-bill news"; that is not the same as frequency 'off',
  -- because saved-search matches can still be wanted.
  event_types      TEXT NOT NULL DEFAULT 'status,amendment,hearing,vote',
  include_matches  INTEGER NOT NULL DEFAULT 1,   -- 0/1 — the saved-search half
  -- Additional addresses, comma-separated, that get the same email: an
  -- assistant, an associate, the client. Cc'd on one message rather than
  -- sent their own copy, so "reply to all" reaches the same thread the
  -- lobbyist is reading.
  extra_recipients TEXT,
  updated_at       TEXT
);

-- Bills this person does not want digest mail about. Per-user for the
-- same reason the prefs are, and a separate table rather than a column
-- on flagged_bills because that row is the firm's.
--
-- Muting is deliberately NOT unflagging: the bill stays tracked, stays
-- on the flagged list, stays in the reports. It just stops sending mail.
CREATE TABLE IF NOT EXISTS digest_mutes (
  user_id    INTEGER NOT NULL REFERENCES users(id),
  bill_id    INTEGER NOT NULL REFERENCES bills(id),
  created_at TEXT,
  PRIMARY KEY (user_id, bill_id)
);

-- ── Saved views on the flagged list ───────────────────────────────────
--
-- "Everything for UCSA before Thursday's call" and "every bill where any
-- client is Oppose" are the two questions this firm actually asks of its
-- flagged list, and both are compositions of filters rather than places
-- in the app. Once the filter state lives in the URL, saving a view is
-- saving that query string under a name.
--
-- `query` is deliberately opaque to SQLite: it is the page's own query
-- string (client=3&position=oppose&urgency=week&group=client), parsed
-- and applied entirely in the browser, the same way the filters are.
-- Storing structured columns per dimension would mean a migration every
-- time the rail grows another control, and the server does not filter
-- this list — it hands over every flagged row and the page narrows it.
--
-- Org-owned like saved_searches: a view is a way of reading the firm's
-- own work, and "the Thursday UCSA call" is a firm's meeting, not one
-- person's bookmark. Scoped through the creating user, same as
-- everything else — see the ORG_SCOPE note in db.py.
CREATE TABLE IF NOT EXISTS saved_views (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  name       TEXT NOT NULL,
  query      TEXT NOT NULL,
  created_at TEXT,
  UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_saved_views_user ON saved_views(user_id);

-- ── The searchable bill corpus (see bill_text.py) ──────────────────
--
-- Everything above this line is about bills someone in this firm has
-- looked at. This table is about every bill in the session, whether
-- anyone ever has: 5,060 of them in 2025-26, held so that "which bills
-- touch my client's concern" can be answered from the operative text
-- rather than from LegiScan's index of titles and summaries.
--
-- Deliberately NOT a set of columns on `bills`, and deliberately
-- carrying its own copy of bill_number/title/url/last_action even
-- though `bills` has columns by those names. The two have different
-- lifecycles and wildly different sizes — `bills` holds the handful
-- this app has pulled full detail for and refreshes nightly (7 rows at
-- the time of writing), the corpus holds the session (5,060). Making
-- search join against `bills` would mean a full-text index that can
-- only find bills somebody already found, which is the problem it
-- exists to fix.
--
-- One row per bill, holding its CURRENT version only. Indexing every
-- prior version as well is ~5x the rows, the API calls and the disk
-- (measured — see bill_text.py's header); it is a deferral, not an
-- omission.
CREATE TABLE IF NOT EXISTS bill_texts (
  bill_id          INTEGER PRIMARY KEY,   -- LegiScan's bill_id, same space as bills.id
  bill_number      TEXT,
  title            TEXT,
  description      TEXT,
  url              TEXT,
  last_action      TEXT,
  last_action_date TEXT,
  doc_id           INTEGER,               -- LegiScan's doc_id for the indexed version
  version_date     TEXT,
  version_type     TEXT,                  -- 'Introduced' | 'Amended' | 'Enrolled' | ...
  body             TEXT,                  -- plain text, markup stripped
  byte_size        INTEGER,               -- of the HTML as fetched, for corpus sizing
  change_hash      TEXT,                  -- the getBill hash this row was built from
  fetched_at       TEXT,
  -- When bill_code_sections was last derived from `body` (see
  -- code_sections.py). NULL means the text is here but its citations
  -- aren't parsed yet — which is the whole queue the corpus builder
  -- works through for free, since parsing spends no API calls. Also
  -- what makes a parser improvement redeployable: clear this column and
  -- the next run re-derives every bill without refetching one.
  sections_parsed_at TEXT
);

-- External-content FTS5: the index points at bill_texts rather than
-- keeping its own copy of `body`, which is the difference between ~75MB
-- and ~150MB of text on disk. content_rowid is bill_id, so a MATCH
-- returns rowids that are already bill_ids with nothing to join.
--
-- porter so that "licensing" finds "license", unicode61 so that
-- punctuation in the Legislature's own markup doesn't glue words
-- together. Both are compiled into SQLite by default (FTS5 has shipped
-- standard since 3.9) — no extension to load and nothing to add to
-- requirements.txt, which is the whole reason this is FTS5 and not an
-- external search engine.
CREATE VIRTUAL TABLE IF NOT EXISTS bill_text_fts USING fts5(
  bill_number, title, description, body,
  content='bill_texts',
  content_rowid='bill_id',
  tokenize='porter unicode61'
);

-- External-content tables do not index themselves; these keep the index
-- and the table in step. The 'delete' command re-supplies the OLD row's
-- values, which is how FTS5 finds the terms to remove — hence the
-- old.* on the update trigger before the fresh insert.
CREATE TRIGGER IF NOT EXISTS bill_texts_ai AFTER INSERT ON bill_texts BEGIN
  INSERT INTO bill_text_fts(rowid, bill_number, title, description, body)
  VALUES (new.bill_id, new.bill_number, new.title, new.description, new.body);
END;
CREATE TRIGGER IF NOT EXISTS bill_texts_ad AFTER DELETE ON bill_texts BEGIN
  INSERT INTO bill_text_fts(bill_text_fts, rowid, bill_number, title, description, body)
  VALUES ('delete', old.bill_id, old.bill_number, old.title, old.description, old.body);
END;
CREATE TRIGGER IF NOT EXISTS bill_texts_au AFTER UPDATE ON bill_texts BEGIN
  INSERT INTO bill_text_fts(bill_text_fts, rowid, bill_number, title, description, body)
  VALUES ('delete', old.bill_id, old.bill_number, old.title, old.description, old.body);
  INSERT INTO bill_text_fts(rowid, bill_number, title, description, body)
  VALUES (new.bill_id, new.bill_number, new.title, new.description, new.body);
END;

-- ── Which code sections a bill touches (see code_sections.py) ──────
--
-- Derived from bill_texts.body, not fetched: the citation is stated in
-- the Legislative Counsel's own title on the front of every bill, so
-- this table costs parsing rather than API calls, and can be rebuilt
-- from text already held.
--
-- Exists because a section number is a bad full-text search term even
-- with the corpus in place. Searching "17053.5" as words also returns
-- every bill that merely cross-references it, and matches "17053.55"
-- besides. A citation is not a word, so it is stored as a citation.
--
-- One row per (bill, code, section, action). "Repeal and add Section
-- 602" is genuinely two rows: a search for what is being repealed and a
-- search for what is being added should each find it, and one row with
-- a merged action would be answerable to neither.
CREATE TABLE IF NOT EXISTS bill_code_sections (
  bill_id  INTEGER NOT NULL,
  code     TEXT NOT NULL,             -- one of code_sections.CALIFORNIA_CODES
  section  TEXT NOT NULL,             -- as cited: '290', '17053.5', '1798.99.80'
  action   TEXT NOT NULL,             -- 'add' | 'amend' | 'repeal'
  -- The citation this was read out of, kept verbatim so a result can
  -- show its own source ("Sections 290 to 290.024, inclusive") rather
  -- than only the number that was matched.
  citation TEXT,
  -- Whether that citation was a range. Ranges record their endpoints
  -- and nothing between: ordering California section numbers is
  -- genuinely ambiguous (290.024 sorts before 290.1 read as decimals
  -- and after it read as sequence numbers), and a wrong guess would
  -- silently return bills that don't touch the section. See
  -- code_sections.py's header.
  is_range INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (bill_id, code, section, action)
);
-- The two shapes of the question: "what's moving against Gov Code
-- 65660" and "what's moving against 65660, whichever code that is".
CREATE INDEX IF NOT EXISTS idx_bill_code_sections_cite ON bill_code_sections(code, section);
CREATE INDEX IF NOT EXISTS idx_bill_code_sections_section ON bill_code_sections(section);

-- ── The Capitol directory: legislators, their staff, and who owns
-- which portfolio (see directory.py) ──────────────────────────────
--
-- The industry keeps this in a crowdsourced spreadsheet that maps each
-- office's staff to the committees, caucuses and issue areas they
-- handle. The point of holding it here instead is that a spreadsheet
-- can't be asked "who handles water in Senate offices" and can't tell
-- you it's eighteen months old.
--
-- ORG-SCOPED, and that is a boundary rather than a convenience. This
-- data is a firm's own copy of a directory it maintains, holding direct
-- phone numbers and emails for identifiable people. Importing it into
-- the account of the firm that already has it is defensible; pooling it
-- across firms, or shipping a seed copy in this repo, is somebody
-- else's crowdsourced work and somebody else's personal data. So there
-- is no global directory table, no cross-org read, and nothing checked
-- in — every row here arrives from an import the firm did itself.
--
-- Scoped through user_id like every other org-owned table, so
-- db.ORG_SCOPE applies unchanged; see its note in db.py.

-- One import: a file, on a date, by a firm. Everything below points at
-- one of these, which is what makes "this came from the March sheet"
-- answerable and what makes an import undoable in one statement.
CREATE TABLE IF NOT EXISTS directory_imports (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  source_name   TEXT,                  -- the file's own name, as uploaded
  -- The date the SHEET is current as of, which is not the date it was
  -- imported: a firm importing January's sheet in June has six-month-old
  -- data and the app should say so rather than call it fresh today.
  as_of         TEXT,
  legislators   INTEGER NOT NULL DEFAULT 0,
  staff         INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_directory_imports_user ON directory_imports(user_id);

CREATE TABLE IF NOT EXISTS legislators (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  import_id   INTEGER REFERENCES directory_imports(id),
  full_name   TEXT NOT NULL,
  chamber     TEXT,                    -- 'Assembly' | 'Senate'
  district    TEXT,                    -- TEXT, not INTEGER: sheets write "AD-12", "12th"
  party       TEXT,
  office_room TEXT,
  office_phone TEXT,
  notes       TEXT,
  updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_legislators_user ON legislators(user_id);

CREATE TABLE IF NOT EXISTS capitol_staff (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  legislator_id INTEGER REFERENCES legislators(id),
  import_id     INTEGER REFERENCES directory_imports(id),
  full_name     TEXT NOT NULL,
  title         TEXT,                  -- 'Chief of Staff', 'Legislative Director', ...
  email         TEXT,
  phone         TEXT,
  -- Marked by a user who found it wrong, independently of how old the
  -- import is. Age is a guess about staleness; this is a report of it,
  -- and the two are worth keeping apart.
  is_stale      INTEGER NOT NULL DEFAULT 0,
  notes         TEXT,
  updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_capitol_staff_user ON capitol_staff(user_id);
CREATE INDEX IF NOT EXISTS idx_capitol_staff_legislator ON capitol_staff(legislator_id);

-- What a staffer owns. One row per (staffer, kind, name) rather than a
-- column per committee, which is exactly the shape the source
-- spreadsheet has and the shape that makes "who handles water" a query
-- instead of a scan across ninety columns.
CREATE TABLE IF NOT EXISTS staff_assignments (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id  INTEGER NOT NULL REFERENCES users(id),
  staff_id INTEGER NOT NULL REFERENCES capitol_staff(id),
  kind     TEXT NOT NULL,              -- 'committee' | 'caucus' | 'issue'
  name     TEXT NOT NULL,
  UNIQUE(staff_id, kind, name)
);
CREATE INDEX IF NOT EXISTS idx_staff_assignments_name ON staff_assignments(name);
CREATE INDEX IF NOT EXISTS idx_staff_assignments_user ON staff_assignments(user_id);
