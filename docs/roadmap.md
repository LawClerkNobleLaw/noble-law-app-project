# Rotunda — roadmap from today's code to the concept doc

Companion to `docs/Rotunda_Concept_Summary.docx` (Sept 2026). That document describes the
product; this one describes **the distance between it and the repository**, and the order in
which to close that distance.

Two rules this document tries to hold to:

1. Nothing is "done" because the concept doc describes it. Status below is what is in the code.
2. Sequence is driven by what unblocks what — including the non-code answers that would waste
   engineering time if resolved after the build rather than before it.

---

## 1. Where the code actually is

The app is substantially further along than a prototype, and materially narrower than the doc.
It is a **working single-firm tool**, not a multi-tenant platform.

### Epic-by-epic status

| Doc epic | Status | What exists / what's missing |
|---|---|---|
| **A — Bill tracking & search** | **Partial** | Have: LegiScan-backed lookup, watchlist, flagged bills, status history, sponsors, hearings, votes, amendments, saved searches that auto-adopt newly matching bills (US-A5 ✅), client-organized views via `bill_client_links` + `saved_views` (US-A2 ✅), a status dashboard (US-A3 ✅). Missing: **full-text and code-section search** (US-A1 — today's `/api/search` is a LegiScan `getSearch` passthrough, no local text index, no amended-code-section metadata) and **side-by-side redline** (US-A4 — `bill_amendments` and `/api/bill-amend-by-date` exist, diff rendering does not). |
| **B — Alerts & notifications** | **Mostly done** | Have: daily digest with real change diffing (`digest.py`, `bill_change_events`), per-client organization, `notification_prefs`, `digest_mutes`, a calendar page. Missing: **statutory deadline calendar** (US-B3 — the calendar shows *your bills' hearings*, not the Legislature's policy/fiscal/house-of-origin deadlines) and per-alert-type channel/frequency (US-B4 — prefs are coarser than the story). |
| **C — Advocacy drafting** | **Skeleton** | Have: `letters` table, `/draft/letters` editor, `letter_drafts.py` producing a factual first draft (bill, client, position, next hearing). Missing: everything AI (US-C1/C2), floor alerts (US-C3), coalition merge (US-C4), and **all delivery** (US-C5) — there is no send path at all, by design. |
| **D — Client/matter & positions** | **Partial, and diverges** | Have: clients with full Form-601/603/615 field set, contacts, notes, positions with `position_history` audit trail (US-D2 ✅). **Diverges:** US-D1 wants per-client isolation *inside* a firm and US-D3 wants per-matter user assignment; `db.ORG_SCOPE` deliberately shares everything firm-wide. See §5.1. |
| **E — Compliance & filing** | **Partial** | Have: `prepared_filings`, Form 601 field-by-field editor, `pypdf` fill, per-filing deadline, explicit human sign-off gate, CAL-ACCESS cross-reference (1.1M+ rows ingested). Missing: forms beyond 601 (602/603/615/625/635), a **recurring** FPPC deadline calendar (US-E2 — deadlines are per-filing, not a calendar of obligations), audit-ready period export (US-E3), and any e-file path. |
| **F — Campaign finance / PAC** | **Not started** | Nothing. See §6.3 — this is the highest-risk pillar in the doc and the one I'd challenge hardest. |
| **G — Hearing video** | **Not started** | Nothing. See §6.4. |
| **H — News & political intel** | **Not started** | Nothing. |
| **I — Contacts & Capitol directory** | **Barely started** | Have: `client_contacts` — contacts *at the client*, not legislators or Capitol staff. Missing: the entire legislator/staff directory, spreadsheet import, routing suggestions, staleness handling, vCard export. This is the doc's most self-contained new epic. |
| **J — Platform, roles, admin** | **Not started** | Have: accounts, sessions, login lockout, an `organizations` table users belong to. Missing: **roles of any kind**, an **audit log** (US-J2 — `position_history` is the only audit trail, and it covers one field), and anything about session-peak capacity. |

### What the doc doesn't mention that already exists

Worth knowing, because it's leverage: the CAL-ACCESS pipeline (`calaccess-pipeline/`) has
already ingested **667,793 disclosure rows across 1,106 lobbying firms, 9,248 lobbyist
employers and 106 coalitions**. The concept doc treats CAL-ACCESS as a Phase 2 dependency; in
fact it's the deepest asset in the repo and — see §3.1 — it is also the market-sizing dataset
the business plan is currently missing.

---

## 2. The one decision that gates the whole roadmap

The concept doc describes a commercial SaaS product with subscription tiers, multi-tenancy,
SOC 2, and a two-sided network effect. The code is an internal tool for one firm, on SQLite,
on one Render disk, with no roles, no billing, and firm-wide data sharing.

**Those are not the same product, and the roadmap forks on which one you are building.** Answer
this before Phase 1 or the sequencing below is guesswork:

| | **A. Internal tool** | **B. Product, Noble Law-owned** | **C. Product, separate entity** |
|---|---|---|---|
| What it is | Rotunda stays Noble Law's competitive advantage | Noble Law sells Rotunda to the Capitol community | Rotunda spun into its own company; Noble Law is customer #1 and an investor |
| Engineering cost | Roughly the current trajectory | +RBAC, +audit, +billing, +Postgres, +SOC 2, +support | Same as B |
| The blocker | None | **Rival firms must trust a competitor with their client strategy.** See §6.1 | Structurally solves §6.1 |
| Legal overhead | Low | High (see §6) | High, but ring-fenced from the law firm |
| Honest read | Deliverable, high value, low risk | Hardest of the three, for a non-technical reason | The version that matches the concept doc |

My read: **the doc is describing C, and the code is on path A.** That's fine — A is a genuinely
good place to be, and everything in Phase 1 below is worth building under any of the three. But
the moment you commit to B or C, six things (roles, audit log, tenancy model, Postgres, billing,
an entity structure) stop being "later" and become prerequisites, and building Phase 1 without
knowing that means rewriting parts of it.

**Everything through Phase 2 below is common to A, B and C.** The fork bites at Phase 3.

---

## 3. Phase 0 — the diligence workstream (no code; run it in parallel starting now)

Each item is here because a wrong answer invalidates engineering work. Roughly ranked by how
much they'd cost to discover late.

**0.1 — LegiScan redistribution rights, not just cost.** The doc models LegiScan Pull/Push API
*cost at volume*. The bigger question is **whether your license permits redistributing LegiScan
data to your own paying subscribers at all.** Internal use by one firm and resale inside a
commercial SaaS are different licenses. If redistribution is restricted or priced per end-user,
the unit economics in §6.2 of the doc change shape entirely. Ask LegiScan directly, in writing,
before Phase 3.
*Blocks: options B and C. Not option A.*

**0.2 — CAL-ACCESS programmatic access and e-filing.** The doc already flags this. Concretely:
bulk downloads are what the pipeline uses today and they work; a third-party "one-click e-file"
integration should be assumed **unavailable** until the Secretary of State says otherwise in
writing. Plan Phase 2 compliance as *prepare-and-export*, which is what the code already does.
*Blocks: any promise of e-filing in marketing or pricing.*

**0.3 — Current FPPC form set, thresholds, deadlines.** Form 601 is implemented; 602/603/615/
625/635 are not. Get a current, dated inventory of the forms your users actually file, their
quarterly deadlines, and the dollar thresholds — from FPPC regulations, not from memory or from
this repo's comments. Every one of those numbers is a hard-coded constant somewhere once built,
and they change.
*Blocks: Phase 2 scope.*

**0.4 — The lobbyist contribution and gift problem.** Two California rules that the concept doc
does not appear to have priced in, and both cut against pillars in it:
- **Contributions:** Gov. Code §85702 broadly prohibits a registered lobbyist from making
  contributions to state officials they're registered to lobby. Epic F's core users are
  lobbyists; the giving module's most natural buyer is largely barred from its central action.
  Verify the exact scope with FPPC counsel, but if it holds, Epic F is a product for PACs and
  donors who are *not* your lobbyist subscribers — a different customer, different sale.
- **Gifts:** registered lobbyists are subject to a very low monthly gift limit to officials they
  lobby (on the order of $10/month — confirm the current figure). The doc's "free access for
  legislators and Capitol staff" two-sided-network play means **a lobbying firm giving software
  of value to public officials it lobbies.** That may be a reportable or prohibited gift. This
  is not a technical problem and it doesn't have a technical fix; it may be a reason the product
  must be owned by a non-lobbying entity (option C).
*Blocks: Epic F entirely; blocks the free-legislative-seat go-to-market.*
*Get this in writing from FPPC counsel before it appears in a business plan.*

**0.5 — Market size, from your own database.** The doc has no TAM, no pricing research, and no
revenue model — it's a product and competitive summary wearing a business-plan label. You can
close a large part of that gap this week without new data: `lobbying_entities` already holds
**1,106 firms / 9,248 employers / 106 coalitions**. Filter to filers active in the last two
sessions, segment by disclosed spend, and you have a defensible addressable-market count and a
willingness-to-pay proxy (a firm disclosing $2M/yr in lobbying spend prices differently than one
disclosing $40k). **Blocked today by the date bug in §7** — fix that first, it's a day of work.

**0.6 — "Rotunda" trademark and domain clearance.** As the doc says. Cheap, do it now, and note
the code, the DB file, and half the comments still say "BillWatch" — a rename is mechanical but
touches everything, so decide the name before Phase 1 rather than after.

**0.7 — Confidentiality, privilege, and the AI question.** Before any LLM feature: decide whether
client strategy and bill positions may leave your infrastructure, under what data-processing
terms, and whether a law firm's work product going to a third-party model raises privilege
issues for your own clients. This is a policy decision with an engineering consequence — see
§5.3 — and it should be made once, in writing, not per-feature.

**0.8 — Insurance and support model.** A compliance product whose output goes to a regulator
implies E&O exposure and a real support obligation. The doc names "higher-touch onboarding" as a
cost line but not the liability. Price both.

---

## 4. The build sequence

Slices are sized to the repo's existing convention: one branch, one PR, one coherent user-visible
change. Files named are where the work actually lands.

### Phase 1 — Finish being the best CA tracking tool (≈ 8–12 PRs)

*Rationale: every one of these is valuable under option A, B or C; none requires a Phase 0 answer;
and together they close the gap between "our internal tool" and "the thing CapitolTrack sells."*

**1.1 Fix the CAL-ACCESS date bug.** (§7) Small, and it unblocks 0.5.
→ `calaccess-pipeline/refresh_calaccess.py`, a `_migrate()` backfill in `db.py`, `app.py:1601/1609/1630/1454`.

**1.2 Local bill-text search index.** The doc's US-A1 is the single biggest functional gap versus
CapitolTrack, and it can't be done through LegiScan's `getSearch` — that returns relevance-ranked
list rows over LegiScan's index, not your own. Build an SQLite **FTS5** table over bill titles,
summaries and full text as it's fetched, and search that locally.
→ new `search_index.py`, `db/schema.sql`, `_migrate()`, `/api/search`.
*Note: this is the first feature that requires storing full bill text, which changes DB growth
and possibly your LegiScan quota profile — size it before building.*

**1.3 Code-section search.** Extends 1.2. CA bill text names the code sections it amends in
reasonably regular language ("Section 17053.5 of the Revenue and Taxation Code is amended"). Parse
those into a `bill_code_sections` table at ingest and search on it. **The doc's "including bills
that amend the section indirectly" is the hard 20%** — indirect amendment isn't stated in the text
and can't be regex'd out. Scope 1.3 to *directly named sections* and say so; don't promise the
indirect case until you have a method for it.
→ `legiscan_client.py` or a new `bill_text.py`, `db/schema.sql`, `/lookup` UI.

**1.4 Amendment redline (US-A4).** `bill_amendments` already stores the versions; this is a diff
renderer and a two-version picker. Python's `difflib` covers it — no dependency needed.
→ new `static/js/bill_redline.js`, a `/api/bill-diff` endpoint, `templates/lookup_body.html`.

**1.5 Statutory deadline calendar (US-B3).** Load the year's legislative deadlines (they're
published each session as a house resolution) into a small table, overlay them on the existing
calendar page, and surface "3 of your bills face the fiscal deadline in 9 days" on the dashboard.
→ `db/schema.sql`, `templates/calendar_body.html`, `digest.py`.
*Data note: this is a hand-maintained table per session — ~30 rows a year. Accept that; there's no
feed for it.*

**1.6–1.9 Capitol staff directory (Epic I).** The doc's most self-contained epic and, from the
persona list, plausibly the most-used daily feature. Four slices:
- **1.6** `legislators` + `capitol_staff` tables with committee/caucus/issue-area assignments, and
  a browsable directory page.
- **1.7** Wide-format CSV import with a column-mapping step (the doc is right that the crowdsourced
  format is one column per committee — don't demand a rigid template).
- **1.8** vCard/CSV export (US-I5). Ongoing phone *sync* is a much larger feature (CardDAV server
  or a mobile app) — ship export first and treat sync as a separate decision.
- **1.9** Routing suggestions on the letter editor (US-I2), driven by the bill's committee of
  reference.
→ new `contacts.py`, `db/schema.sql`, `templates/directory_body.html`, `letter_drafts.py`.
*See §6.5 on the provenance of imported directory data — get that answer before 1.7 ships to
anyone outside Noble Law.*

### Phase 2 — Compliance depth (≈ 6–10 PRs)

*Depends on Phase 0.3. Deliberately excludes e-filing.*

**2.1 The remaining FPPC forms.** 603 (employer registration), 615 (lobbyist report), 625 (lobbying
firm report), 635 (employer report). The 601 pattern in `pdf_forms.py`/`disclosure_fields.py`
generalizes, but each form needs its own field map and its own validation.
*Note: `clients.compensation_amount/period` was stored specifically so Form 615 could read it, and
the schema comment correctly says the quarterly arithmetic belongs with 615 — that's this slice.*

**2.2 Activity logging that feeds the forms (US-E1).** The forms need logged *lobbying activity* —
contacts made, gifts, expenditures — and the app currently logs bills and positions, not activity.
This is a new domain object, not a report feature, and it's the real work in Phase 2.
→ new `activity.py`, `db/schema.sql`, a logging UI on the bill and client pages.

**2.3 Recurring FPPC deadline calendar (US-E2).** Obligations, not just filings-in-progress:
"you are a registered lobbying firm, so Form 625 is due 4/30."

**2.4 Audit-ready period export (US-E3).** Falls out of 2.2 nearly free, and it's the honest
answer to "we can't e-file": *here is everything, formatted, for the filing you make yourself.*

**2.5 News & political intelligence (Epic H).** RSS ingestion, filtered by client keywords. Small,
independent, and the cheapest of the remaining pillars — good filler while 2.2 is in review.

### Phase 3 — Only if the answer in §2 is B or C

These are prerequisites for selling to anyone, and none of them ships a user-visible feature — plan
the calendar accordingly.

**3.1 Roles and per-matter access control (US-D1/D3/J1).** Replaces `ORG_SCOPE` with a real
access-control layer. This touches nearly every query in `db.py`. Do it *before* the customer
count grows, never after.
**3.2 Comprehensive audit log (US-J2).** Cross-cutting; cheapest to add alongside 3.1.
**3.3 SQLite → Postgres.** The trigger isn't tenant count, it's **concurrent writes and backup/
point-in-time-recovery obligations**. A compliance product that loses a filing period's data has a
different kind of problem than a tool that does.
**3.4 Billing and subscription management.**
**3.5 Security posture / SOC 2 readiness.** Long lead time; start the moment option B or C is chosen.

### Phase 4 — The pillars I'd defer or drop

**Hearing video (Epic G)** and **campaign finance / PAC giving (Epic F)** are grouped here not
because they're low value but because §6.3 and §6.4 argue they may not be buildable as described.
**AI drafting** sits here too, gated on 0.7 — see §5.3. **Multi-state** is last and I'd question it
at all (§6.6).

---

## 5. Architecture decisions to make deliberately

### 5.1 The tenancy model is currently the opposite of the doc

`db.ORG_SCOPE` scopes queries to the user's *organization*: everything a firm tracks is visible to
every seat in that firm. That was a deliberate, correct choice for one firm. **US-D1 asks for the
opposite** — isolation between a firm's own clients, enforced at the data layer, because a firm may
represent adverse interests.

These conflict, and the doc's version is the harder one: it's not a filter, it's an authorization
model, and retrofitting authorization into ~2,400 lines of `db.py` after customers exist is the
kind of change that produces a breach. **If option B or C is chosen, do 3.1 early — before Phase 2,
not after.** If option A, leave `ORG_SCOPE` alone; it's right for a single firm.

### 5.2 When SQLite stops being the right answer

Not at some row count — at the point where you need concurrent writers, real backups, and
point-in-time recovery. One firm on one Render disk is fine. Fifty firms whose FPPC filings live in
your database is not. Treat 3.3 as a hard prerequisite of the first paying external customer.

### 5.3 AI drafting breaks a standing constraint, twice

`requirements.txt` has one line, on purpose, and `letter_drafts.py` deliberately generates only
fact and leaves the argument to the human. AI drafting breaks both: it adds the first real
dependency and the first time client strategy leaves the machine.

That's not an argument against it — it's the doc's most differentiating feature and I'd want it —
but it should be an explicit, recorded decision (0.7), with the human-review gate the doc already
specifies (US-C1) preserved as an architectural invariant rather than a UI convention. Concretely:
**generation writes a draft row, it never writes a sent row**, and there is no code path from
"model returned text" to "left the building."

### 5.4 The monolith is fine; the doc's "services" are not a mandate

Section 8.2 of the concept doc lists six services. Read that as *six bounded contexts*, not six
deployables. `app.py` at ~3,900 lines with one-job modules underneath is working, and the modules
already map onto those contexts. Split when a piece needs to scale or fail independently — the
payments service would be the first genuine case, and it's in the phase I'd defer.

---

## 6. Holes and flags in the concept doc

Ranked by how much they'd change the plan.

### 6.1 The competitor-trust problem is unaddressed and may be structural

Noble Law PC is a lobbying firm. The doc's target customers are *other* lobbying firms. For option
B, Noble Law would be asking direct competitors to store their confidential client positions,
strategy notes, and draft letters in a system a competitor owns and operates.

Encryption doesn't fix this; the operator can always read the data. This is the single most likely
reason for a commercial version to fail, it's not mentioned anywhere in the doc, and the only real
answers are structural: **spin Rotunda into a separate entity (option C)**, or accept that the
addressable market excludes every firm that competes with Noble Law.

### 6.2 Section 5's table contradicts Section 7

The competitive table claims "Compliance filing (FPPC/Cal-Access): **Yes**" as a differentiator.
Section 7 then says a third-party e-file integration may not be available and shouldn't be assumed.
Both can't go in a business plan. Change the table cell to "Prepared & export-ready filings" —
which is true today, is still a real differentiator over CapitolTrack, and doesn't promise
something the Secretary of State may not permit.

Related: the table is self-scored ("not a verified feature audit," as the doc admits). A reader
won't carry that caveat. Either verify each cell or move the table to an appendix.

### 6.3 Epic F (PAC giving) is the weakest pillar in the doc

Beyond the money-transmitter question the doc does raise:
- **Your buyers may be legally barred from the feature** (§0.4 — lobbyist contribution ban).
- Routing earmarked contributions can make the platform an **intermediary** with its own PRA
  reporting obligations, and possibly a **commercial fundraiser**. The doc treats "partner with a
  processor" as the whole answer; the processor handles money movement, not your status.
- A **law firm** operating contribution rails for clients is a professional-responsibility
  conversation, not just a compliance one.

My recommendation: **split Epic F.** Contribution *tracking* from CAL-ACCESS data (US-F1) is
low-risk, genuinely useful, and you already have the ingestion pipeline for it — build it in Phase
2. Contribution *processing* (US-F2/F3) should come out of the roadmap until 0.4 comes back, and
possibly permanently.

### 6.4 Hearing video assumes rights nobody has confirmed

Three separate assumptions, none flagged in the doc:
- **Embedding** the Legislature's streams is probably fine; *re-hosting* them likely isn't.
- **Clipping** (§4.6) means making and redistributing derivative copies — a materially different
  ask than embedding, and the doc's cost model only budgets "bandwidth and storage."
- **Auto-indexing to agenda items** has no data source. The Legislature doesn't publish
  timestamp→agenda-item mappings. Doing it properly means transcription plus alignment across
  thousands of hours per session — an ongoing per-hour cost, not a feature.

Cal-Span (cited in the doc's sources) already does much of this and has its own terms. **Talk to
Cal-Span about licensing or partnership before scoping this as a build.** That conversation could
turn Epic G from a large engineering project into an integration.

### 6.5 The "Capitol Codex" import has a provenance problem

Ingesting someone else's crowdsourced spreadsheet and shipping it inside a paid product raises both
an IP/terms question and a privacy one — those are direct phone numbers and emails for identifiable
individuals, which puts CCPA in scope. Importing *the user's own copy* into *their own* account is
defensible. **Seeding your product with it, or sharing it across tenants, is not.** Make that a
technical boundary in 1.7, not a policy note.

### 6.6 Multi-state expansion contradicts the core thesis

The doc's stated moat is California-Capitol-specific depth: FPPC compliance, CAL-ACCESS,
CA hearing video, Capitol staff. Phase 3 multi-state throws away all four and re-enters as a
generic LegiScan wrapper against State Net and Quorum — who are better funded and already there.
"LegiScan already covers 50 states" describes the easy 10% of a state's worth of work. I'd cut
multi-state from the roadmap and put the same effort into going deeper in California.

### 6.7 Missing from the business plan entirely

The doc is a strong *product and competitive* summary, and it is not yet a business plan. Absent:
**market sizing** (fixable this week — §0.5), **pricing** (no numbers anywhere), **revenue model**
(no ARPU, no customer count, no forecast), **team and capital** (who builds this — the current app
appears to be one person plus AI assistance, which will not carry Phases 2–4), **timeline**,
**success metrics** (no definition of a successful MVP), and the **liability/insurance** line
(§0.8).

### 6.8 Smaller notes

- **Personas 5 and 6 aren't sized.** "Legislators and Capitol staff" as a free tier is a
  go-to-market strategy with a legal problem (§0.4) and no adoption estimate.
- **"Delivery/receipt tracking" for letters (US-C5)** is read-receipt tracking on email to
  government offices. Many offices strip or block it; this will underreport, and a compliance-
  adjacent product showing a wrong "not received" is worse than showing nothing.
- **US-A1's "prior versions of a bill"** requires storing every version's full text — a real
  storage and LegiScan-quota commitment that should be sized in 1.2, not discovered in 1.3.
- **"High availability tuned to the legislative calendar" (§8.3)** is the only non-functional
  requirement with a number, and there's no SLA, no support hours, and no on-call model behind it.
  For a product whose peak load coincides with the moment users' deadlines are unforgiving, that's
  the requirement most worth making concrete.
- **The name in the code is still "BillWatch."** Decide the rename before Phase 1 (§0.6).

---

## 7. One confirmed bug, found while auditing

Not a concept-doc issue — a live defect in shipped code, surfaced by this review.

**Every CAL-ACCESS date is stored in `M/D/YYYY h:mm:ss AM` format** (667,789 of 667,793 rows),
while the rest of the app stores ISO dates via `datetime('now')`. SQL comparison on those strings
is lexical, so:

- `app.py:1601` and `app.py:1609` — `ORDER BY d.filed_date DESC` on the lobbying detail page sorts
  September before October before January. The "most recent filings" list for the busiest filer in
  the database currently leads with **2007**.
- `app.py:1630` — the same wrong sort, repeated in Python.
- `app.py:1454` — `MAX(filed_date) AS latest` on the lobbying search page returns the
  lexically-largest string, not the latest date.

Fix: normalize to ISO on ingest (`calaccess-pipeline/refresh_calaccess.py`), backfill existing rows
in `db.py`'s `_migrate()` following the existing guarded-migration pattern, and add a test. This is
slice **1.1**, and §0.5 is blocked behind it — you cannot count active filers with a date column
that doesn't sort.
