"""
Getting to search without reaching for the mouse.

There was a global search INPUT in the topbar once, and it was removed
as redundant: /lookup is one sidebar click away and does the same job
better, so a second always-visible box competing with it was furniture.
That reasoning is still good, and these tests pin down that what came
back is a different thing — a link shaped like a search field, whose
real job is to say that the keystroke exists.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


# ── The topbar affordance ───────────────────────────────────────────

def test_a_shell_page_offers_a_way_into_search():
    shell = app.app_shell("/flagged", "<p>body</p>")
    assert 'class="topbar-search"' in shell
    assert 'href="/lookup"' in shell


def test_it_is_a_link_not_a_second_search_box():
    """A link navigates, can be middle-clicked, and works with JS
    blocked — and it can't quietly become the thing that was removed."""
    shell = app.app_shell("/flagged", "<p>body</p>")
    start = shell.index('class="topbar-search"')
    end = shell.index("</a>", start)
    assert "<input" not in shell[start:end]


def test_it_names_the_keystroke():
    assert "<kbd>" in app.app_shell("/flagged", "<p>body</p>")


def test_scoped_search_pages_still_get_the_global_affordance():
    """The global "Search bills" affordance is a different scope than a
    page's own box, so hiding it wherever a page had any search of its
    own left visitors on /directory asking "where do I search bills from
    here?". It now sits in the same spot on those pages too, alongside
    the page's own scoped box."""
    for path in ("/lobbying", "/directory"):
        assert 'class="topbar-search"' in app.app_shell(path, "<p>body</p>")


def test_lookup_itself_does_not_get_it():
    """/lookup *is* the global bill search, so a topbar link back to it
    would be a dead link-to-self (⌘K still focuses its box there)."""
    assert 'class="topbar-search"' not in app.app_shell("/lookup", "<p>body</p>")


# ── The keystroke itself ────────────────────────────────────────────

def test_the_shortcut_is_served():
    """A file in static/js/ that isn't in STATIC_ASSETS is a 404 with a
    <script src> pointing at it — the one way this can be wired wrong."""
    assert "js/search_shortcut.js" in app.STATIC_ASSETS


def test_every_shell_page_loads_the_shortcut():
    assert app.SEARCH_SHORTCUT_SRC in app.page("Title", "/flagged", "<p>body</p>")


def test_slash_is_ignored_while_the_caret_is_in_a_field():
    """Otherwise the shortcut eats the slash out of "Health and Safety
    39617.2" — a real query on this app's own citation search."""
    js = app.SEARCH_SHORTCUT_JS
    assert "isTypingIn(document.activeElement)" in js
    assert "isContentEditable" in js


def test_the_key_hint_is_corrected_off_the_mac_default():
    assert "'Ctrl K'" in app.SEARCH_SHORTCUT_JS


def test_command_k_is_the_global_search_gated_on_the_lookup_path():
    """⌘K means the global bill search from every page. Its box only
    lives on /lookup, and #q is *also* the id of the scoped box on
    /lobbying and /directory — so the global path must gate it, or ⌘K
    would mistake a scoped box for the global search."""
    js = app.SEARCH_SHORTCUT_JS
    assert "GLOBAL_SEARCH_PATH = '/lookup'" in js
    assert "window.location.pathname === GLOBAL_SEARCH_PATH" in js
    # commandK falls back to navigating to the global search page.
    assert "if (commandK)" in js
