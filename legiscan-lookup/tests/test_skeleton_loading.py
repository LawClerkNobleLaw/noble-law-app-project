"""
Tests for app._skeleton_rows / app._skeleton_panel — the shared
loading-state building blocks P2-32 rolled out from Organization Search
(the only page that had one before) to the Dashboard and Flagged Bills.

These are pure string builders with no request or DB involved, so
there's no HTTP server or `conn` fixture needed — just import app (which
already exercises _render_template's slot-matching at module load, so a
mismatched {{slot}} would fail collection for every test file, not just
this one) and call the functions directly.
"""

import app


def test_skeleton_rows_produces_one_row_per_count():
    html = app._skeleton_rows(3)
    assert html.count('class="skeleton-row"') == 3


def test_skeleton_rows_widths_are_used_in_order():
    html = app._skeleton_rows(1, widths=(50, 20))
    assert html == (
        '<div class="skeleton-row">'
        '<div class="skeleton-bar" style="width:50%"></div>'
        '<div class="skeleton-bar" style="width:20%"></div>'
        "</div>"
    )


def test_skeleton_rows_zero_count_is_empty():
    assert app._skeleton_rows(0) == ""


def test_skeleton_panel_wraps_rows_in_a_panel_with_a_head_bar():
    html = app._skeleton_panel(rows=2)
    assert html.startswith('<div class="panel"><div class="panel-head">')
    assert html.count('class="skeleton-row"') == 2


def test_dashboard_body_renders_four_skeleton_panels():
    # Guards the actual DASHBOARD_BODY constant, not just the helpers —
    # a future edit that changes the loop count or drops the slot value
    # would fail here even though _skeleton_panel itself still works.
    assert app.DASHBOARD_BODY.count('class="panel"><div class="panel-head"') == 4


def test_flagged_body_skeleton_matches_its_own_five_columns():
    # Bill / Next action / Status / Last change / Clients — same column
    # count FLAGGED_BODY's own TABLE_HEAD renders.
    # Split on the skeleton container's closing ">" rather than its whole
    # open tag: the tag also carries role="status" and an sr-only
    # "Loading…" span now, and this test is about the row shape.
    after_open = app.FLAGGED_BODY.split('id="loading"', 1)[1].split(">", 1)[1]
    first_row = after_open.split("</div></div>")[0]
    assert first_row.count('skeleton-bar') == 5


# ══════════════════════════════════════════════════════════════════════
# Tier 3, row 25 of the UX audit (P2): the rollout finished. Six pages
# had a skeleton; the other eleven showed a centred spinner and then
# swapped in a whole table or form, which is the layout shift the
# skeleton work existed to remove — still in place on the frequent-visit
# pages (client detail, the letter editor, the disclosure review).
# ══════════════════════════════════════════════════════════════════════

import glob
import os

import pytest

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _template_paths():
    return sorted(glob.glob(os.path.join(TEMPLATES, "*.html")))


def test_a_spinner_means_an_action_never_a_page_arriving():
    """The rule the rollout leaves behind, and the reason it is worth a
    test: a spinner is right for "Saving…", "Generating…", "Logging
    in…" — an action the user started, of unknown length, with the page
    already on screen. It is wrong as a page's first content, where it
    says only "something is coming" about a layout the server already
    knows the shape of."""
    for path in _template_paths():
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert '<span class="spinner"></span>Loading…' not in text, os.path.basename(path)


# The pages the audit named, plus the disclosure review, by the constant
# each one is rendered into. Named rather than globbed: the point is
# that each of these specific pages got one, and a glob would pass while
# any of them silently lost it.
SKELETON_BODIES = [
    "ARCHIVED_BODY", "CALENDAR_BODY", "LETTERS_BODY", "SPONSOR_ROLLUP_BODY",
    "CLIENTS_BODY", "DISCLOSURES_BODY", "CLIENT_DETAIL_BODY", "LETTER_EDIT_BODY",
    "LOBBYING_DETAIL_BODY", "PROFILE_BODY", "DISCLOSURE_REVIEW_BODY",
]


@pytest.mark.parametrize("name", SKELETON_BODIES)
def test_every_page_arrives_in_the_shape_of_its_own_content(name):
    body = getattr(app, name)
    assert 'class="skeleton-bar"' in body, name


@pytest.mark.parametrize("name", ["CLIENTS_BODY", "DISCLOSURES_BODY"])
def test_the_two_lists_that_had_no_loading_state_start_as_one(name):
    """These two are the odd pair: their #loading was a SAVE indicator
    ("Saving…" / "Generating…"), so the list itself simply appeared with
    nothing before it. The placeholder is the list's own starting
    content instead, which render() overwrites like any other render."""
    body = getattr(app, name)
    after_list = body.split('<div id="list">', 1)[1]
    assert 'class="skeleton-row"' in after_list.split("</div>\n  </div>", 1)[0], name
