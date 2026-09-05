"""
The structural accessibility guarantees, checked against the real files.

Each of these is a WCAG failure the app shipped with until the UX
audit's first tier: no skip link, no headings on the four detail pages,
twenty silent loading regions, a focus ring that reshaped every pill,
and one motion setting honoured in two places out of a 1,800-line
stylesheet. None of them is visible in a screenshot and none of them
breaks a page when it regresses, which is exactly why they need a test.

Markup and stylesheet text only — no browser, no rendering. Templates
are read off disk rather than through app.py's constants so a new page
is covered the moment it exists, without anyone remembering to add it
here.
"""

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
STYLE_PATH = os.path.join(ROOT, "static", "style.css")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _templates():
    return sorted(glob.glob(os.path.join(TEMPLATES, "*.html")))


# ── Skip link (WCAG 2.4.1) ────────────────────────────────────────────
def test_every_shell_page_opens_with_a_skip_link():
    # page() is what every app_shell() page is built from, so one check
    # here covers all of them. First focusable thing in <body>, before
    # the fifteen-item sidebar.
    body = app.page("T — Rotunda", "/flagged", "<p>x</p>")
    assert '<a class="skip-link" href="#main-content">' in body
    assert body.index("skip-link") < body.index("app-sidebar")


def test_the_skip_link_has_somewhere_to_land():
    body = app.page("T — Rotunda", "/flagged", "<p>x</p>")
    assert 'id="main-content" tabindex="-1"' in body


# ── Headings (WCAG 1.3.1 / 2.4.6) ─────────────────────────────────────
def test_the_four_detail_pages_have_a_heading():
    # These render their identity from JSON after load, so the <h1> is
    # inside a template literal rather than static markup — grep for the
    # tag, not for a parsed DOM.
    for name in (
        "report_body.html",
        "client_detail_body.html",
        "letter_edit_body.html",
        "lobbying_detail_body.html",
    ):
        assert "<h1" in _read(os.path.join(TEMPLATES, name)), name


def test_the_detail_pages_name_themselves_in_the_tab_title():
    # Six to ten bills open at once is the normal working pattern; every
    # one of those tabs used to read "Bill report — Rotunda".
    for name in (
        "report_body.html",
        "client_detail_body.html",
        "letter_edit_body.html",
        "lobbying_detail_body.html",
    ):
        assert "document.title" in _read(os.path.join(TEMPLATES, name)), name


# ── Loading regions (WCAG 4.1.3) ──────────────────────────────────────
def test_every_loading_region_is_announced():
    seen = 0
    for path in _templates():
        for tag in re.findall(r'<div id="loading"[^>]*>', _read(path)):
            seen += 1
            assert 'role="status"' in tag, os.path.basename(path)
    # Twenty at the time of writing. The count isn't the point — a bare
    # `for` over zero matches would pass silently.
    assert seen >= 20


def test_skeleton_loading_regions_have_something_to_announce():
    # A skeleton is all empty bars: role="status" on it announces the
    # empty string. The sr-only sentence is what makes the role do
    # anything at all.
    for path in _templates():
        for tag in re.findall(r'<div id="loading"[^>]*>(?:\s*<span[^>]*>)?', _read(path)):
            if "skeleton" not in tag:
                continue
            assert 'class="sr-only"' in tag, os.path.basename(path)


# ── Focus and motion ──────────────────────────────────────────────────
def test_the_focus_rule_does_not_reshape_the_element():
    style = _read(STYLE_PATH)
    rule = style.split("a:focus-visible, button:focus-visible", 1)[1].split("}", 1)[0]
    assert "outline" in rule
    # A radius here outranks every pill button's own rule, so keyboard
    # focus turned a 999px pill into a 6px rectangle.
    assert "border-radius" not in rule


def test_reduced_motion_is_honoured_globally():
    style = _read(STYLE_PATH)
    blanket = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{\s*\*, \*::before, \*::after \{([^}]*)\}",
        style,
    )
    assert blanket, "no blanket reduce block"
    body = blanket.group(1)
    assert "animation-iteration-count: 1 !important" in body   # stops the infinite ones
    assert "transition-duration" in body


def test_scripted_smooth_scrolls_opt_out_of_smooth():
    # An explicit behavior:'smooth' overrides the stylesheet, so these
    # have to check the media query themselves.
    for path in _templates():
        text = _read(path)
        if "scrollIntoView" not in text:
            continue
        assert "prefers-reduced-motion" in text, os.path.basename(path)


# ── Touch targets (WCAG 2.5.5 / 2.5.8) ────────────────────────────────
def test_coarse_pointers_get_a_real_target_on_buttons_and_selects():
    style = _read(STYLE_PATH)
    coarse = style.split("@media (pointer: coarse) {", 1)[1].split("\n  }", 1)[0]
    assert "button, select { min-height: 2.75rem; }" in coarse
    assert ".toast-close { min-width: 2.75rem; }" in coarse


# ── The destructive-confirm dialog ────────────────────────────────────
def test_the_confirm_dialog_takes_its_label_from_the_caller():
    js = _read(os.path.join(ROOT, "static", "js", "confirm_delete.js"))
    assert "function confirmDelete(title, message, confirmLabel)" in js
    assert ">Remove</button>" not in js
    assert "confirmLabel || 'Remove'" in js


def test_cancel_is_the_first_and_focused_button():
    js = _read(os.path.join(ROOT, "static", "js", "confirm_delete.js"))
    assert js.index('id="cd-cancel"') < js.index('id="cd-confirm"')
    assert "cancelBtn.focus();" in js


def test_unflagging_does_not_offer_a_button_labelled_remove():
    # The call site that motivated the third argument: "Unflag this
    # bill? … moves to Archived" over a button that said Remove.
    for name in ("report_body.html", "flagged_body.html"):
        text = _read(os.path.join(TEMPLATES, name))
        assert "'Unflag'" in text, name


# ── Toast ─────────────────────────────────────────────────────────────
def test_the_toast_timer_pauses_while_it_is_being_used():
    js = _read(os.path.join(ROOT, "static", "js", "toast.js"))
    for event in ("mouseenter", "mouseleave", "focusin", "focusout"):
        assert "'%s'" % event in js, event


# ── Form autofill (WCAG 1.3.5) ────────────────────────────────────────
def test_the_users_own_address_fields_name_their_purpose():
    text = _read(os.path.join(TEMPLATES, "profile_page.html"))
    for token in (
        'autocomplete="organization"',
        'autocomplete="section-business street-address"',
        'autocomplete="section-mailing street-address"',
        'autocomplete="tel"',
    ):
        assert token in text, token


def test_client_and_contact_fields_are_their_own_autofill_section():
    # These describe a client or a contact at one, not the user — the
    # section prefix keeps a browser from filling them from the user's
    # own /profile address.
    clients = _read(os.path.join(TEMPLATES, "clients_body.html"))
    assert 'autocomplete="section-client street-address"' in clients
    detail = _read(os.path.join(TEMPLATES, "client_detail_body.html"))
    assert 'autocomplete="section-contact name"' in detail
