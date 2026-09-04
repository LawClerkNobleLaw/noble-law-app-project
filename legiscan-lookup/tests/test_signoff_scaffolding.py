"""
Tests for the sign-off scaffolding (P1-21).

Sign-off already recorded who attested, and gave them nothing to attest
against: no count of what was outstanding, no way to reach it, and no
indication of where any pre-filled value came from. Two of those three
are server-side, so that is what is tested here — the structured issue
list the banner is built from, and the provenance the field lines are.

The load-bearing case is `edited`: "From your Profile" printed under a
value that no longer matches Profile would be a false statement on a
compliance document, which is worse than printing nothing.
"""

import disclosure_fields as df


COMPLETE = {
    "BUSINESS ADDRESS  Number and street": "1 Capitol Mall",
    "BUSINESS ADDRESS  City": "Sacramento",
    "BUSINESS ADDRESS  State": "CA",
    "BUSINESS ADDRESS  Zip": "95814",
    "TELEPHONE Area Code": "916",
    "TELEPHONE": "555-0100",
    "EMAIL": "clerk@noblelaw.example",
    "INDIVIDUAL LOBBYISTS 1": "Dana Reyes",
}


# ── The banner's list ──────────────────────────────────────────────────

def test_a_complete_filing_has_no_issues(conn):
    assert df.field_issues("601", COMPLETE) == []


def test_a_blank_required_field_is_reported_with_its_key(conn):
    data = dict(COMPLETE, **{"BUSINESS ADDRESS  City": ""})

    issues = df.field_issues("601", data)

    assert [i["field_key"] for i in issues] == ["BUSINESS ADDRESS  City"]
    assert issues[0]["required"] is True
    assert issues[0]["label"] == "City"


def test_a_badly_formatted_value_is_reported_but_is_not_a_blank(conn):
    """The banner counts these separately — "3 required fields still
    blank" and "1 field to fix" are different jobs."""
    data = dict(COMPLETE, EMAIL="not-an-email")

    issues = df.field_issues("601", data)

    assert [i["field_key"] for i in issues] == ["EMAIL"]
    assert issues[0]["required"] is False
    assert "valid email" in issues[0]["message"]


def test_every_problem_is_reported_at_once(conn):
    issues = df.field_issues("601", {})

    assert len(issues) == 8   # every required field on the 601
    assert all(i["required"] for i in issues)


def test_the_banner_and_a_rejected_submit_cannot_disagree(conn):
    """validate_field_data is what the server rejects a generate/sign
    with; field_issues is what the banner renders. One has to be the
    other, or the page can call a draft ready that the server refuses."""
    data = dict(COMPLETE, **{"BUSINESS ADDRESS  Zip": "", "EMAIL": "nope"})

    assert df.validate_field_data("601", data) == [i["message"] for i in df.field_issues("601", data)]


# ── Provenance ─────────────────────────────────────────────────────────

def test_a_value_that_still_matches_its_source_says_so(conn):
    p = df.provenance_for("601", COMPLETE, dict(COMPLETE))

    entry = p["BUSINESS ADDRESS  City"]
    assert entry["state"] == df.INHERITED
    assert entry["source"] == "profile"
    assert entry["href"] == "/signup/profile"


def test_a_value_typed_over_its_source_is_marked_edited_and_carries_the_original(conn):
    """The one state a reviewer has to be able to see. Without it, a
    hand-typed address is indistinguishable from the registered one."""
    data = dict(COMPLETE, **{"BUSINESS ADDRESS  Number and street": "500 Capitol Mall, Suite 1800"})

    entry = df.provenance_for("601", data, dict(COMPLETE))["BUSINESS ADDRESS  Number and street"]

    assert entry["state"] == df.EDITED
    assert entry["source_value"] == "1 Capitol Mall"


def test_a_value_with_nothing_behind_it_is_marked_typed(conn):
    source = dict(COMPLETE, **{"BUSINESS ADDRESS  State": ""})

    entry = df.provenance_for("601", COMPLETE, source)["BUSINESS ADDRESS  State"]

    assert entry["state"] == df.TYPED


def test_a_blank_field_with_a_blank_source_says_there_is_nothing_to_pull(conn):
    entry = df.provenance_for("601", {}, {})["EMAIL"]

    assert entry["state"] == df.BLANK
    assert entry["empty_note"]


def test_whitespace_alone_is_not_an_edit(conn):
    data = dict(COMPLETE, **{"BUSINESS ADDRESS  City": "  Sacramento  "})

    entry = df.provenance_for("601", data, dict(COMPLETE))["BUSINESS ADDRESS  City"]

    assert entry["state"] == df.INHERITED


def test_each_field_points_at_the_screen_that_actually_owns_it(conn):
    p = df.provenance_for("601", COMPLETE, dict(COMPLETE))

    assert p["BUSINESS ADDRESS  City"]["source"] == "profile"
    assert p["EMAIL"]["source"] == "account"
    assert p["INDIVIDUAL LOBBYISTS 1"]["source"] == "roster"
    # Every sourced field names a real screen to go and fix it on.
    assert all(entry["href"] for entry in p.values())


def test_provenance_covers_every_prefilled_field_and_nothing_else(conn):
    p = df.provenance_for("601", COMPLETE, dict(COMPLETE))

    sourced = {f["key"] for f in df._flat_fields("601") if f.get("source")}
    assert set(p) == sourced
    # Client-row fields are per-row, not per-form; they carry the client
    # record's own heading instead of a line each.
    assert not (set(p) & df.client_row_field_keys("601"))
