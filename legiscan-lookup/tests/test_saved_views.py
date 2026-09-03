"""
Tests for saved views on the flagged list (P2-24).

The filtering itself is client-side — the page narrows rows GET
/api/flagged already returned — so what there is to test on this side is
storage: that a view is the firm's rather than one person's, that
re-saving a standing view under the same name updates it instead of
erroring, and that the query string comes back out exactly as it went
in (it is opaque to SQLite on purpose; see saved_views in schema.sql).
"""

import db
from conftest import insert_user


UCSA_VIEW = "client=3&position=oppose&urgency=week&group=client"


def _firm(conn, *emails):
    org_id = db.create_organization(conn, "Noble Law")
    ids = []
    for email in emails:
        user_id = insert_user(conn, email=email)
        conn.execute("UPDATE users SET org_id = ? WHERE id = ?", (org_id, user_id))
        ids.append(user_id)
    conn.commit()
    return ids


def test_a_saved_view_is_a_query_string_under_a_name(conn):
    user_id = insert_user(conn)

    views = db.create_saved_view(conn, user_id, "UCSA — Thursday call", UCSA_VIEW)

    assert [(v["name"], v["query"]) for v in views] == [("UCSA — Thursday call", UCSA_VIEW)]


def test_the_query_string_is_stored_verbatim(conn):
    """It is parsed and applied entirely in the browser. Anything this
    layer does to it is a bug — including helpfully reordering it."""
    user_id = insert_user(conn)

    views = db.create_saved_view(conn, user_id, "Odd order", "sort=client&q=AI%20bills&position=support")

    assert views[0]["query"] == "sort=client&q=AI%20bills&position=support"


def test_a_leading_question_mark_is_not_part_of_the_query(conn):
    user_id = insert_user(conn)

    views = db.create_saved_view(conn, user_id, "From location.search", "?position=oppose")

    assert views[0]["query"] == "position=oppose"


def test_resaving_a_standing_view_updates_it(conn):
    """"UCSA — Thursday call" is a standing view whose filters get
    adjusted. Making the user delete it to re-save it would be a worse
    answer than the name collision."""
    user_id = insert_user(conn)
    db.create_saved_view(conn, user_id, "UCSA — Thursday call", UCSA_VIEW)

    views = db.create_saved_view(conn, user_id, "UCSA — Thursday call", "client=3&position=support")

    assert len(views) == 1
    assert views[0]["query"] == "client=3&position=support"


def test_a_view_belongs_to_the_firm(conn):
    mine, theirs = _firm(conn, "a@firm.com", "b@firm.com")
    db.create_saved_view(conn, mine, "Every Oppose", "position=oppose")

    assert [v["name"] for v in db.list_saved_views(conn, theirs)] == ["Every Oppose"]


def test_another_firms_views_are_invisible(conn):
    (mine,) = _firm(conn, "a@firm.com")
    (outsider,) = _firm(conn, "elsewhere@other.com")
    db.create_saved_view(conn, mine, "Every Oppose", "position=oppose")

    assert db.list_saved_views(conn, outsider) == []


def test_a_colleague_can_adjust_a_shared_view(conn):
    mine, theirs = _firm(conn, "a@firm.com", "b@firm.com")
    db.create_saved_view(conn, mine, "Every Oppose", "position=oppose")

    views = db.create_saved_view(conn, theirs, "Every Oppose", "position=oppose&urgency=week")

    assert len(views) == 1
    assert views[0]["query"] == "position=oppose&urgency=week"


def test_views_come_back_alphabetically_regardless_of_case(conn):
    user_id = insert_user(conn)
    for name in ("zoning", "Appropriations", "budget"):
        db.create_saved_view(conn, user_id, name, "position=watch")

    assert [v["name"] for v in db.list_saved_views(conn, user_id)] == [
        "Appropriations", "budget", "zoning"]


def test_deleting_a_view_leaves_the_others(conn):
    user_id = insert_user(conn)
    db.create_saved_view(conn, user_id, "Keep", "position=support")
    views = db.create_saved_view(conn, user_id, "Drop", "position=oppose")
    drop_id = next(v["id"] for v in views if v["name"] == "Drop")

    assert db.delete_saved_view(conn, user_id, drop_id) is True
    assert [v["name"] for v in db.list_saved_views(conn, user_id)] == ["Keep"]


def test_deleting_someone_elses_view_does_nothing(conn):
    (mine,) = _firm(conn, "a@firm.com")
    (outsider,) = _firm(conn, "elsewhere@other.com")
    views = db.create_saved_view(conn, mine, "Every Oppose", "position=oppose")

    assert db.delete_saved_view(conn, outsider, views[0]["id"]) is False
    assert len(db.list_saved_views(conn, mine)) == 1


def test_an_unnamed_view_is_refused(conn):
    user_id = insert_user(conn)

    for name in (None, "", "   "):
        try:
            db.create_saved_view(conn, user_id, name, "position=oppose")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected a ValueError for {name!r}")


def test_a_view_with_no_filters_is_still_a_view(conn):
    """An empty query is "everything, grouped by nothing" — a legitimate
    thing to save as "All bills" and land back on."""
    user_id = insert_user(conn)

    views = db.create_saved_view(conn, user_id, "All bills", "")

    assert views[0]["query"] == ""


def test_there_is_a_ceiling_on_saved_views(conn):
    user_id = insert_user(conn)
    for n in range(db.MAX_SAVED_VIEWS):
        db.create_saved_view(conn, user_id, f"View {n}", "position=watch")

    try:
        db.create_saved_view(conn, user_id, "One too many", "position=watch")
    except ValueError as err:
        assert str(db.MAX_SAVED_VIEWS) in str(err)
    else:
        raise AssertionError("expected a ValueError")

    # The ceiling is on new names, not on adjusting the ones that exist.
    assert len(db.create_saved_view(conn, user_id, "View 0", "position=oppose")) == db.MAX_SAVED_VIEWS
