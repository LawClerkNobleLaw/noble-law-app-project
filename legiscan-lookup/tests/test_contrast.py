"""
Contrast of the colour ramp against both grounds (P2-31).

The audit's suspicion was that "several of those greys look likely to
fall under WCAG AA 4.5:1". Measured, they don't — the token system's
oklch lightness values hold up on both themes, and the lowest text pair
in the app is --good on --good-soft at 4.63:1. What DID fail was
something the audit didn't name: --rule, at 1.35:1, was the visual
boundary of every input and select, where WCAG 1.4.11 wants 3:1.

This file exists so that stays true. The tokens are read out of
style.css rather than restated here — a copy would drift, and a test
that measures last year's palette measures nothing. A token this can't
find is a hard failure, not a skip, for the same reason.

Colour maths only: no browser, no dependencies, nothing rendered.
"""

import math
import os
import re

import pytest

STYLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "style.css")

AA_TEXT = 4.5          # WCAG 1.4.3, normal-size text
NON_TEXT = 3.0         # WCAG 1.4.11, a control's own visual boundary


# ── colour maths ───────────────────────────────────────────────────────

def _srgb_from_oklch(lightness, chroma, hue_deg):
    hue = math.radians(hue_deg)
    a, b = chroma * math.cos(hue), chroma * math.sin(hue)
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    linear = (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )
    def encode(c):
        c = max(0.0, min(1.0, c))
        return 1.055 * c ** (1 / 2.4) - 0.055 if c > 0.0031308 else 12.92 * c
    return tuple(encode(c) for c in linear)


def _srgb_from_hex(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _composite(colour, alpha, ground):
    """What a semi-transparent colour actually looks like — which is the
    only thing contrast can be measured on. color-mix(... N%, transparent)
    is alpha N%, so every grey in this palette has to be flattened
    against the ground it sits on before it means anything."""
    return tuple(c * alpha + g * (1 - alpha) for c, g in zip(colour, ground))


def _relative_luminance(colour):
    def linearize(channel):
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
    r, g, b = (linearize(c) for c in colour)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# ── the palette, read out of the stylesheet ────────────────────────────

@pytest.fixture(scope="module")
def css():
    with open(STYLE_PATH) as f:
        return f.read()


def _alpha_token(css, name, theme_block):
    """The N in `--name: color-mix(in srgb, var(--ink) N%, transparent)`."""
    match = re.search(rf"--{name}:\s*color-mix\(in srgb, var\(--ink\) (\d+)%, transparent\)", css)
    assert match, f"--{name} is not an --ink alpha mix any more — re-measure it here."
    return int(match.group(1)) / 100


def _oklch_token(css, name, block):
    """--good / --error / --warn / --info, from the light :root or from
    the dark override, whichever block was asked for."""
    match = re.search(rf"--{name}:\s*oklch\(([\d.]+) ([\d.]+) ([\d.]+)\)", block)
    assert match, f"--{name} is not an oklch() value in this theme any more — re-measure it here."
    return _srgb_from_oklch(*(float(g) for g in match.groups()))


@pytest.fixture(scope="module")
def dark_block(css):
    """The :root[data-theme="dark"] body — the manual-toggle copy, which
    style.css keeps byte-identical to the prefers-color-scheme one."""
    start = css.index(':root[data-theme="dark"] {')
    return css[start:css.index("}", start)]


@pytest.fixture(scope="module")
def light_block(css):
    start = css.index("--good: oklch")
    return css[start - 200:start + 600]


def _themes(css, dark_block, light_block):
    return {
        "light": {"ink": _srgb_from_hex("#111111"), "bg": _srgb_from_hex("#FAF8F3"),
                  "surface": _srgb_from_hex("#FFFFFF"), "block": light_block},
        "dark": {"ink": _srgb_from_hex("#ffffff"), "bg": _srgb_from_hex("#000000"),
                 "surface": _srgb_from_hex("#0C0C0C"), "block": dark_block},
    }


# ── the grey ramp ──────────────────────────────────────────────────────

def test_secondary_text_is_readable_on_both_grounds(css, dark_block, light_block):
    """--slate is every caption, tile subtitle, bill-number line and
    "Prepared Aug 26, 4:12 PM" in the app — the greys the audit flagged."""
    alpha = _alpha_token(css, "slate", css)
    for name, theme in _themes(css, dark_block, light_block).items():
        for ground_name in ("bg", "surface"):
            ground = theme[ground_name]
            ratio = contrast(_composite(theme["ink"], alpha, ground), ground)
            assert ratio >= AA_TEXT, f"--slate on --{ground_name} ({name}) is {ratio:.2f}:1"


def test_status_colours_are_readable_on_the_page(css, dark_block, light_block):
    for name, theme in _themes(css, dark_block, light_block).items():
        for token in ("good", "error", "warn", "info"):
            colour = _oklch_token(css, token, theme["block"])
            ratio = contrast(colour, theme["bg"])
            assert ratio >= AA_TEXT, f"--{token} on the page ({name}) is {ratio:.2f}:1"


def test_a_badge_is_readable_against_its_own_tint(css, dark_block, light_block):
    """Every position badge and status pill is its colour on a 16% wash
    of the same colour — the tightest text pair in the app, and the one
    a lightness change would break first."""
    for name, theme in _themes(css, dark_block, light_block).items():
        for token in ("good", "error", "warn", "info"):
            colour = _oklch_token(css, token, theme["block"])
            tint = _composite(colour, 0.16, theme["bg"])
            ratio = contrast(colour, tint)
            assert ratio >= AA_TEXT, f"--{token} on --{token}-soft ({name}) is {ratio:.2f}:1"


# ── the boundary of a control ──────────────────────────────────────────

def test_a_control_outline_can_actually_be_seen(css, dark_block, light_block):
    """The one thing that really failed. --rule at 14% measures 1.35:1
    and was the border on every input, select, secondary button and
    filter tab; WCAG 1.4.11 wants 3:1 for the boundary that tells a
    low-vision reader the control is there."""
    alpha = _alpha_token(css, "rule-strong", css)
    for name, theme in _themes(css, dark_block, light_block).items():
        ground = theme["bg"]
        ratio = contrast(_composite(theme["ink"], alpha, ground), ground)
        assert ratio >= NON_TEXT, f"--rule-strong on the page ({name}) is {ratio:.2f}:1"


# Every author-drawn control outline in the app, by the rule that draws
# it. A boundary meeting 3:1 is no use if the controls don't wear it —
# and the first pass at this missed a.secondary, .icon-btn and
# .row-menu-btn, which is exactly the kind of miss a list catches.
CONTROL_OUTLINES = [
    "input, select, textarea",
    "button.secondary, button.danger",
    "a.secondary, a.danger",
    ".filter-tab",
    ".search-box",
    ".icon-btn",
    ".row-menu-btn",
    ".add-client-select",
]


@pytest.mark.parametrize("selector", CONTROL_OUTLINES)
def test_controls_use_the_stronger_token(css, selector):
    start = css.index("\n  " + selector + " {")
    body = css[start:css.index("}", start)]
    assert "--rule-strong" in body, f"{selector} still draws its outline with --rule"
    assert "1px solid var(--rule);" not in body and "1px dashed var(--rule);" not in body


def test_a_disabled_filter_tab_is_still_legible(css):
    """It carries a count the user is meant to read ("Senate 0" is an
    answer), so the blanket opacity that took it to 1.94:1 is gone —
    "off" reads from the flat shape instead."""
    match = re.search(r"\.filter-tab\[disabled\] \{([^}]*)\}", css)
    assert match, "the disabled filter-tab rule moved — re-check its contrast"
    assert "opacity" not in match.group(1)
    assert "var(--slate)" in match.group(1)


# ── meaning that does not depend on colour ─────────────────────────────

def test_every_position_carries_a_glyph_as_well_as_a_colour(css):
    """Red/green is the one pair a deuteranopic reader can't separate,
    and it was carrying a client's official position. + / minus / ring
    survive greyscale and every form of colour blindness."""
    assert '.position-badge.support::before { content: "+\\00a0"; }' in css
    assert '.position-badge.oppose::before { content: "\\2212\\00a0"; }' in css
    assert '.position-badge.watch::before { content: "\\25e6\\00a0"; }' in css


def test_position_badges_have_an_edge_of_their_own(css):
    """A 16% fill tint is barely a boundary; three tints of one shape
    read as one thing scanned quickly."""
    assert "border: 1px solid currentColor;" in css.split(".position-badge.support")[0][-400:]


def test_the_selects_offer_the_same_glyphs(css):
    """::before doesn't render inside a native <select>, so the glyph
    lives in the option text — the one cue that shows both in the closed
    control and in the open dropdown."""
    positions = "[['watch', '\\u25e6 Watch'], ['support', '+ Support'], ['oppose', '\\u2212 Oppose']]"
    for path in ("static/js/client_quickadd.js", "templates/client_detail_body.html"):
        full = os.path.join(os.path.dirname(STYLE_PATH), "..", path)
        with open(os.path.normpath(full)) as f:
            assert positions in f.read(), f"{path} lost the position glyphs"
