"""
Tests for app.search_lobbying() and its two helpers, normalize_org_name()
and _cluster_client_mentions().

The mention_count/latest_filed tests here are exactly the regression
test that would have caught the N+1 bug this function used to have: a
separate COUNT(*)/MAX(filed_date) query per matched client_name (up to
40 extra round trips) was replaced with one GROUP BY query over the
whole matched set. These tests assert on the *values* search_lobbying
returns, not on how many queries it runs — so they'd have failed just
as loudly against the old per-row-loop implementation, and will fail
again if a future change breaks the GROUP BY's correctness (e.g. by
filtering it differently than the LIKE clause that built client_rows).
"""

import app
from conftest import insert_disclosure, insert_entity


def test_search_lobbying_matches_registered_entity_by_name(conn):
    insert_entity(conn, "Chevron Corporation", city="San Ramon", state="CA")

    results = app.search_lobbying(conn, "chevron")

    assert len(results) == 1
    assert results[0]["kind"] == "entity"
    assert results[0]["name"] == "Chevron Corporation"
    assert results[0]["city"] == "San Ramon"


def test_search_lobbying_matches_unregistered_client_name(conn):
    firm_id = insert_entity(conn, "Some Lobbying Firm")
    insert_disclosure(conn, firm_id, "Anthropic PBC", filing_id="F1")

    results = app.search_lobbying(conn, "anthropic")

    assert len(results) == 1
    assert results[0]["kind"] == "client"
    assert results[0]["name"] == "Anthropic PBC"
    assert results[0]["id"] is None


def test_search_lobbying_mention_count_and_latest_filed_are_correct(conn):
    # The core regression test: three separate disclosures naming the
    # same unregistered client, filed on three different dates. Before
    # the N+1 fix, mention_count/latest_filed came from a per-row
    # COUNT(*)/MAX(filed_date) query; now they come from one GROUP BY
    # over every matched client_name. Either implementation MUST
    # produce these same three numbers/dates — this test doesn't care
    # which one computed them.
    firm_id = insert_entity(conn, "Some Lobbying Firm")
    insert_disclosure(conn, firm_id, "Acme Corp", filing_id="F1", filed_date="2025-01-15")
    insert_disclosure(conn, firm_id, "Acme Corp", filing_id="F2", filed_date="2025-06-30")
    insert_disclosure(conn, firm_id, "Acme Corp", filing_id="F3", filed_date="2025-03-01")

    results = app.search_lobbying(conn, "acme")

    assert len(results) == 1
    assert results[0]["mention_count"] == 3
    assert results[0]["latest_filed"] == "2025-06-30"


def test_search_lobbying_mention_count_does_not_leak_across_different_names(conn):
    # A second regression guard: the GROUP BY must be scoped by the
    # same LIKE filter search_lobbying already applies, not grouping
    # the whole table — two different matched names must each get
    # their own count, not one combined total.
    firm_id = insert_entity(conn, "Some Lobbying Firm")
    insert_disclosure(conn, firm_id, "Acme Corp", filing_id="F1", filed_date="2025-01-01")
    insert_disclosure(conn, firm_id, "Acme Corp", filing_id="F2", filed_date="2025-02-01")
    insert_disclosure(conn, firm_id, "Acme Industries", filing_id="F3", filed_date="2025-03-01")

    results = app.search_lobbying(conn, "acme")
    by_name = {r["name"]: r["mention_count"] for r in results}

    assert by_name["Acme Corp"] == 2
    assert by_name["Acme Industries"] == 1


def test_search_lobbying_deduplicates_entity_and_client_name_case_insensitively(conn):
    # "Chevron Corp" is both independently registered AND named as a
    # client_name on some filing (e.g. a subsidiary's own disclosure
    # naming its parent) — it must show up once, as the registered
    # entity, not twice.
    insert_entity(conn, "Chevron Corp")
    insert_disclosure(conn, insert_entity(conn, "Other Firm"), "CHEVRON CORP", filing_id="F1")

    results = app.search_lobbying(conn, "chevron")

    assert len(results) == 1
    assert results[0]["kind"] == "entity"


def test_search_lobbying_no_match_returns_empty_list(conn):
    insert_entity(conn, "Chevron Corp")
    assert app.search_lobbying(conn, "nonexistent-search-term") == []


# ── normalize_org_name() ─────────────────────────────────────────────

def test_normalize_org_name_strips_corporate_suffix_noise():
    assert app.normalize_org_name("Chevron Corp & its subsidiaries") == "chevron"
    assert app.normalize_org_name("CHEVRON CORPORATION AND ITS AFFILIATES") == "chevron"


def test_normalize_org_name_handles_possessive_typo():
    assert app.normalize_org_name("Chevron and it's subsidiaries") == "chevron"


def test_normalize_org_name_is_conservative_about_what_it_strips():
    # "U.S.A." isn't a corporate-suffix token this deliberately strips
    # — a more aggressive normalizer could accidentally fold a real,
    # distinct subsidiary name into its parent's key.
    assert app.normalize_org_name("Chevron U.S.A. Inc.") != app.normalize_org_name("Chevron Corp")


def test_normalize_org_name_handles_empty_input():
    assert app.normalize_org_name("") == ""
    assert app.normalize_org_name(None) == ""


# ── Clustering: near-duplicate client-only mentions get grouped;
#    registered entities never merge with each other. ──

def test_cluster_groups_near_duplicate_client_only_mentions(conn):
    firm_id = insert_entity(conn, "Some Lobbying Firm")
    insert_disclosure(conn, firm_id, "Chevron Corp & its subsidiaries", filing_id="F1", filed_date="2025-01-01")
    insert_disclosure(conn, firm_id, "CHEVRON CORPORATION AND ITS AFFILIATES", filing_id="F2", filed_date="2025-02-01")

    results = app.search_lobbying(conn, "chevron")

    assert len(results) == 1
    canonical = results[0]
    assert canonical["kind"] == "client"
    assert len(canonical.get("variants") or []) == 1


def test_cluster_never_merges_two_independently_registered_entities(conn):
    # Even if both happen to normalize to the same key, two distinct
    # registered entities must never collapse into one row — that
    # would misrepresent one organization's real filings as the
    # other's, not just look tidier. Since _group_duplicate_entities()
    # started visually grouping same-key registered entities, that
    # takes the shape of one `entity_group` row wrapping both real
    # entities rather than two flat rows — still not a merge: each
    # entity's own id/name stays fully intact and individually
    # addressable inside `entities`. (Contrived same-name-normalizing
    # pair, since real CAL-ACCESS data wouldn't usually produce this,
    # but the safety property must hold regardless.)
    id_a = insert_entity(conn, "Chevron Corp")
    id_b = insert_entity(conn, "Chevron Corporation")

    results = app.search_lobbying(conn, "chevron")

    assert len(results) == 1
    group = results[0]
    assert group["kind"] == "entity_group"
    assert {e["id"] for e in group["entities"]} == {id_a, id_b}
    assert {e["name"] for e in group["entities"]} == {"Chevron Corp", "Chevron Corporation"}
    assert all(e["kind"] == "entity" for e in group["entities"])


def test_cluster_attaches_client_mention_to_its_matching_registered_entity(conn):
    # A registered entity and an unregistered near-duplicate spelling
    # of the same org: the mention should fold into the entity's own
    # `variants`, not stand alone as its own row.
    insert_entity(conn, "Chevron Corporation")
    other_firm = insert_entity(conn, "Some Other Firm")
    insert_disclosure(conn, other_firm, "Chevron Corp & its subsidiaries", filing_id="F1")

    results = app.search_lobbying(conn, "chevron")

    assert len(results) == 1
    assert results[0]["kind"] == "entity"
    assert len(results[0].get("variants") or []) == 1
    assert results[0]["variants"][0]["name"] == "Chevron Corp & its subsidiaries"
