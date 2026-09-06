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
    # Focused by name through the shared trap (Tier 3's A5), not by
    # being whatever happened to be first in the panel.
    assert "trapFocus(backdrop, cancelBtn)" in js


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


# ══════════════════════════════════════════════════════════════════════
# Tier 2 of the same audit: the rows that finish a Tier 1 change or
# reuse the infrastructure it introduced.
# ══════════════════════════════════════════════════════════════════════

def _shared_js():
    return sorted(glob.glob(os.path.join(ROOT, "static", "js", "*.js")))


# ── Heading outline (WCAG 1.3.1) ──────────────────────────────────────
def test_no_panel_or_modal_heading_is_still_a_div():
    # A1 gave the four headless pages an <h1>; this is the other half.
    # Ten panel headings on /report, four on /dashboard, six on client
    # detail — all of them styled divs, so the product had an <h1> and
    # then nothing.
    for path in _templates() + _shared_js():
        assert '<div class="title"' not in _read(path), os.path.basename(path)


def test_the_panel_heading_still_renders_as_it_did():
    # An <h2> brings the browser's own margin and font-size with it, and
    # .panel-head is a flex row built around the div's zero margin.
    style = _read(STYLE_PATH)
    for selector in (".panel-head .title", ".modal-head .title"):
        rule = style.split(selector + " {", 1)[1].split("}", 1)[0]
        assert "margin: 0" in rule, selector
        assert "font-size" in rule, selector


def test_every_page_that_has_panels_has_headings_in_them():
    # Cheap regression on the find-and-replace: a template with a
    # .panel-head should have at least one h2 carrying .title.
    for path in _templates():
        text = _read(path)
        if '<div class="panel-head"' not in text and 'class="panel-head"' not in text:
            continue
        if 'class="title"' not in text:
            continue
        assert '<h2 class="title"' in text, os.path.basename(path)


# ── Result counts (WCAG 4.1.3) ────────────────────────────────────────
def test_the_two_filtering_pages_announce_how_many_rows_survived():
    # Filtering is the core interaction on both, and its entire outcome
    # was invisible to a screen reader. The region is in the server-sent
    # markup, not built alongside its first message — a live region
    # created in the same tick as its content is the one screen readers
    # miss (see toast.js).
    for name in ("flagged_body.html", "lookup_body.html"):
        text = _read(os.path.join(TEMPLATES, name))
        assert 'id="result-count"' in text, name
        markup = text.split("<script", 1)[0]
        assert 'id="result-count"' in markup, name
        tag = re.search(r'<[a-z]+[^>]*id="result-count"[^>]*>', markup).group(0)
        assert 'role="status"' in tag, name


# ── aria-current ──────────────────────────────────────────────────────
def test_the_current_nav_item_says_so_in_more_than_colour():
    body = app.page("T — Rotunda", "/flagged", "<p>x</p>")
    assert body.count('aria-current="page"') == 1
    active = re.search(r'<a[^>]*class="side-nav-item[^"]*active"[^>]*>', body).group(0)
    assert 'aria-current="page"' in active


def test_a_page_that_is_not_in_the_nav_marks_nothing_current():
    body = app.page("T — Rotunda", "/nowhere", "<p>x</p>")
    assert "aria-current" not in body


def test_the_report_page_keeps_aria_current_with_the_class():
    # REPORT_BODY re-targets the sidebar client-side to point at
    # wherever the user came from; it used to swap only the class.
    text = _read(os.path.join(TEMPLATES, "report_body.html"))
    assert "removeAttribute('aria-current')" in text
    assert "setAttribute('aria-current', 'page')" in text


# ── The drawer's focus trap ───────────────────────────────────────────
def test_the_drawer_trap_skips_elements_nobody_can_focus():
    # It collected every a/button in the sidebar, including links in a
    # collapsed nav group and the closed account menu. .focus() on a
    # display:none element no-ops, so first and last were unreachable
    # and the wrap-around broke.
    # Tier 3 extracted this trap into static/js/focus.js for the three
    # modals to share, so the filter now lives there — see
    # test_the_trap_still_skips_what_cannot_take_focus.
    assert "offsetParent !== null" in _read(os.path.join(ROOT, "static", "js", "focus.js"))
    shell = app.app_shell("/flagged", "<p>x</p>")
    assert "sidebar.querySelectorAll('a, button')" not in shell


# ── Focus ring ────────────────────────────────────────────────────────
def test_the_focus_ring_reaches_the_letter_editor():
    style = _read(STYLE_PATH)
    rule = style.split("a:focus-visible, button:focus-visible", 1)[1].split("{", 1)[0]
    for selector in ("textarea:focus-visible", "details:focus-visible", "[tabindex]:focus-visible"):
        assert selector in rule, selector


# ── Table headers ─────────────────────────────────────────────────────
def test_every_table_header_declares_what_it_heads():
    for path in _templates():
        for tag in re.findall(r"<th(?=[\s>])[^>]*>", _read(path)):
            assert "scope=" in tag, "%s: %s" % (os.path.basename(path), tag)


# ── Disclosure review fields (WCAG 3.3.1 / 3.3.2) ─────────────────────
def test_a_required_disclosure_field_says_so_to_a_screen_reader():
    text = _read(os.path.join(TEMPLATES, "disclosure_review_body.html"))
    assert 'aria-required="true"' in text
    # The asterisk is the sighted shorthand for the same fact, so it
    # shouldn't be read out as "star" on top of it.
    assert '<span class="req" aria-hidden="true">*</span>' in text


def test_a_field_with_a_problem_is_linked_to_what_the_problem_is():
    text = _read(os.path.join(TEMPLATES, "disclosure_review_body.html"))
    assert 'aria-invalid="true"' in text
    assert "aria-describedby" in text
    # The banner's jump links land on the field; the described-by target
    # is what tells them what's wrong once they arrive.
    assert 'class="field-issue" id="${issueId}"' in text


def test_the_required_asterisk_has_a_legend_rather_than_a_tooltip():
    text = _read(os.path.join(TEMPLATES, "disclosure_review_body.html"))
    assert "requiredLegendHtml()" in text
    assert 'title="Required before this can be signed off"' not in text


# ── Visible labels ────────────────────────────────────────────────────
def test_no_form_hides_its_labels_off_screen_any_more():
    # These were `position:absolute;clip:rect(0,0,0,0)` labels with the
    # placeholder as the only visible cue — and a placeholder is gone on
    # the first keystroke, which on a compliance record is a mis-entry
    # risk, not a style preference.
    for name in ("clients_body.html", "profile_page.html", "client_detail_body.html"):
        text = _read(os.path.join(TEMPLATES, name))
        assert "clip:rect(0,0,0,0)" not in text, name
        assert 'class="sr-only">Contact name' not in text, name


def test_a_label_is_not_styled_as_help_text():
    style = _read(STYLE_PATH)
    rule = style.split(".field-label {", 1)[1].split("}", 1)[0]
    assert "var(--ink)" in rule          # not --slate, which is .sub
    assert "font-weight: 600" in rule
    for name in ("clients_body.html", "profile_page.html", "login_page.html",
                 "signup_page.html", "report_body.html"):
        text = _read(os.path.join(TEMPLATES, name))
        assert 'class="field-label"' in text, name
        assert '<div class="sub" style="margin:0 0 0.3rem">' not in text, name


# ── Required fields, outside the disclosure review ────────────────────
def test_the_forms_say_which_fields_are_mandatory():
    for name in ("clients_body.html", "profile_page.html", "signup_page.html"):
        text = _read(os.path.join(TEMPLATES, name))
        assert 'class="sub req-legend"' in text, name
        assert '<span class="req" aria-hidden="true">*</span>' in text, name


# ── Signup password (U5 / U7) ─────────────────────────────────────────
def test_the_password_rule_resolves_while_you_type():
    text = _read(os.path.join(TEMPLATES, "signup_page.html"))
    assert 'id="password-hint"' in text
    assert "aria-describedby', 'password-hint'" in text
    assert "hint-met" in text and "hint-unmet" in text


def test_the_show_password_button_says_what_it_shows():
    for name in ("login_page.html", "signup_page.html"):
        text = _read(os.path.join(TEMPLATES, name))
        assert 'aria-label="Show password"' in text, name
        assert "'Hide password' : 'Show password'" in text, name


# ── title= as the only carrier ────────────────────────────────────────
def test_the_flagged_controls_explain_themselves_on_screen():
    text = _read(os.path.join(TEMPLATES, "flagged_body.html"))
    assert 'id="controls-hint"' in text
    assert text.count('aria-describedby="controls-hint"') == 2
    assert 'title="One section per client' not in text   # the old title=
    assert 'title="Download the rows below' not in text


def test_the_full_change_text_is_in_the_row_not_only_in_a_tooltip():
    text = _read(os.path.join(TEMPLATES, "flagged_body.html"))
    assert "description !== change.summary" in text


# ── The toast's live region (I3) ──────────────────────────────────────
def test_the_toast_announces_from_a_region_that_already_existed():
    js = _read(os.path.join(ROOT, "static", "js", "toast.js"))
    # Built once, at load — not inside showToast alongside the message,
    # which is the shape NVDA and JAWS routinely fail to announce.
    assert "function liveRegion()" in js
    before, after = js.split("function showToast", 1)
    assert "toastLiveRegion = document.createElement" in before
    # The visible toast is presentation now; the role lives on the
    # standing region.
    assert "toastEl.setAttribute('role', 'status')" not in js


# ── Pending state (P3) ────────────────────────────────────────────────
def test_the_longest_onboarding_form_shows_its_own_submit_working():
    # #loading sits below a two-address form — on a phone, below the
    # fold — so the button was the only thing the user could still see,
    # and it didn't change.
    text = _read(os.path.join(TEMPLATES, "profile_page.html"))
    assert "submitBtn.textContent = 'Saving…'" in text
    assert "submitBtn.textContent = submitLabel" in text


# ══════════════════════════════════════════════════════════════════════
# Tier 3 of the same audit: focus, which is the half of keyboard access
# the first two tiers left. Every check here is about where focus is
# after something happens — a modal opens, a menu opens, a region
# redraws itself — since a lost focus is invisible to everyone using a
# mouse and ends the interaction for everyone who isn't.
# ══════════════════════════════════════════════════════════════════════

# ── What every page load pays for (P1) ────────────────────────────────
def _weights_the_app_actually_sets():
    used = set()
    for path in [STYLE_PATH] + _templates():
        used.update(int(w) for w in re.findall(r"font-weight: ?(\d{3})", _read(path)))
    # 400 and 700 stay regardless: they are what unstyled body text and
    # every <strong>/<b>/<th> resolve to.
    return used | {400, 700}


def test_no_weight_is_shipped_that_nothing_sets():
    """Poppins came down in five weights on every navigation, one of
    which (300) the stylesheet never sets. A list of weights is exactly
    the thing that stops matching reality silently — more so now that
    each one is a file in the repo."""
    assert set(app.POPPINS_WEIGHTS) == _weights_the_app_actually_sets()


def test_every_declared_face_is_a_file_this_app_serves():
    faces = re.findall(r"src: url\('([^']+)'\)", app.FONT_LINKS)
    assert len(faces) == len(app.POPPINS_WEIGHTS)
    for url in faces:
        name, _, version = url.lstrip("/").partition("?")
        name = name[len("static/"):]
        assert name in app.STATIC_ASSETS, name
        # The content hash is what makes the year-long immutable
        # Cache-Control on that route safe, and it is the whole reason
        # self-hosting is cheaper than the CDN rather than merely
        # closer.
        assert version.startswith("v="), url


def test_no_page_asks_a_third_party_for_anything():
    """Not only a handshake and a swap per navigation — on a product
    holding client strategy, every page view was a request telling
    someone else which pages get looked at."""
    body = app.page("T — Rotunda", "/flagged", "<p>x</p>")
    for host in ("fonts.googleapis.com", "fonts.gstatic.com", "//"):
        assert host not in app.FONT_LINKS, host
    assert "http://" not in body and "https://" not in body


def test_the_faces_that_are_preloaded_are_the_ones_every_page_needs():
    # Body copy and the heading/label weight. Preloading all four would
    # spend first-paint bandwidth on files some pages never use.
    preloaded = re.findall(r'rel="preload"[^>]*poppins-(\d{3})', app.FONT_LINKS)
    assert preloaded == ["400", "600"]
    # crossorigin even though this is same-origin now: fonts are fetched
    # in CORS mode, and a preload without it downloads the file twice.
    assert app.FONT_LINKS.count("crossorigin") == len(preloaded)


def test_the_serif_token_names_a_font_that_is_actually_there():
    # --font-serif named Instrument Serif and nothing consumes the
    # token, so two more files came down per visitor for text that
    # doesn't exist. Naming it while not shipping it would have been
    # worse than either.
    style = _read(STYLE_PATH)
    serif = style.split("--font-serif:", 1)[1].split(";", 1)[0]
    assert "Instrument" not in serif
    assert "Georgia" in serif


FOCUS_JS_PATH = os.path.join(ROOT, "static", "js", "focus.js")


# ── The shared helper (A5) ────────────────────────────────────────────
def test_every_shell_page_has_the_focus_helpers_before_its_own_script():
    # In <body> above app_shell()'s output, so it is defined before any
    # page's inline script and before the shell's own drawer wiring.
    body = app.page("T — Rotunda", "/flagged", "<p>x</p>")
    assert "js/focus.js" in body
    assert body.index("js/focus.js") < body.index("app-shell")
    assert "js/focus.js" in app.STATIC_ASSETS


def test_the_trap_is_defined_once_and_not_recopied():
    # A5's point was that the drawer already had this and nothing else
    # did. A second definition anywhere means it drifted again.
    defined_in = [
        os.path.basename(path)
        for path in _templates() + _shared_js() + [os.path.join(ROOT, "app.py")]
        if "function focusablesIn" in _read(path) or "const focusablesIn" in _read(path)
    ]
    assert defined_in == ["focus.js"], defined_in


def test_the_trap_still_skips_what_cannot_take_focus():
    # Moved out of app_shell() by A5, so the guarantee moved with it:
    # .focus() on a display:none element no-ops, which is what broke
    # the drawer's wrap-around before Tier 2.
    assert "offsetParent !== null" in _read(FOCUS_JS_PATH)


def test_the_drawer_uses_the_helper_it_donated():
    shell = app.app_shell("/flagged", "<p>x</p>")
    assert "trapFocus(sidebar)" in shell
    assert "sidebar.querySelectorAll" not in shell


def test_all_three_modals_take_focus_and_give_it_back():
    # aria-modal="true" was already on all three and does none of this:
    # it tells a screen reader the page behind is inert while leaving
    # the tab order exactly as it was.
    for path in ("static/js/confirm_delete.js",
                 "static/js/client_quickadd.js",
                 "templates/report_body.html"):
        text = _read(os.path.join(ROOT, path))
        assert "trapFocus(" in text, path
        # The release is what returns focus to the trigger; a trap
        # without one is a page the user can't get out of.
        assert "release" in text.lower(), path


def test_the_destructive_confirm_can_be_escaped():
    js = _read(os.path.join(ROOT, "static", "js", "confirm_delete.js"))
    assert "e.key === 'Escape'" in js
    # Escape must resolve false — the cheap way out of a destructive
    # dialog is the safe one, same reasoning as Cancel going first.
    assert "onEscape = (e) => { if (e.key === 'Escape') finish(false); }" in js


# ── Row menus (I6) ────────────────────────────────────────────────────
def test_opening_a_row_menu_moves_focus_into_it():
    js = _read(os.path.join(ROOT, "static", "js", "row_menu.js"))
    assert "firstItem.focus()" in js


def test_closing_a_row_menu_only_takes_focus_back_if_it_had_it():
    # Closing because the user clicked elsewhere must not pull focus off
    # whatever they clicked.
    js = _read(os.path.join(ROOT, "static", "js", "row_menu.js"))
    assert "m.contains(document.activeElement)" in js
    assert "if (heldFocus) openBtn.focus();" in js


# ── Filter rails (A7) ─────────────────────────────────────────────────
def test_the_filtering_pages_redraw_without_dropping_focus():
    assert "keepFocus(railEl" in _read(os.path.join(TEMPLATES, "flagged_body.html"))
    assert "keepFocus(resultsEl" in _read(os.path.join(TEMPLATES, "lookup_body.html"))
    # /lookup empties its results before the fetch and refills them
    # after, so there is nothing left to match by the time it renders.
    assert "rememberFocus(resultsEl)" in _read(os.path.join(TEMPLATES, "lookup_body.html"))


def test_every_filter_control_can_be_found_again_after_a_redraw():
    # Matched by identity, not position: the counts change and options
    # drop out from under the user between one render and the next.
    for name in ("flagged_body.html", "lookup_body.html"):
        text = _read(os.path.join(TEMPLATES, name))
        for tag in re.findall(r"<button[^>]*filter-tab[^>]*>", text):
            assert "focusKeyAttr(" in tag, "%s: %s" % (name, tag)
