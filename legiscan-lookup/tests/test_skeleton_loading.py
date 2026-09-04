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
    first_row = app.FLAGGED_BODY.split('id="loading" class="skeleton show">', 1)[1].split("</div></div>")[0]
    assert first_row.count('skeleton-bar') == 5
