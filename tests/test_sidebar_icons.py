"""The sidebar's icon strip — design 4.1.

It shipped with single letters (V/B/T/F/R/J/A) and the source called them a placeholder.
Letters are wrong here for a reason worth keeping: an edge strip exists to be reached for
*without looking*, and what a glance resolves is a **silhouette**. Seven glyphs at one size,
weight and colour have nearly identical outlines, so the strip has to be read rather than
recognised — and a letter is a label for a label anyway ("T" abbreviates *Tags*, which is
already the name of the thing).

Phosphor, as a font rather than SVG files, because the strip already colours its glyph
through stylesheets and a font inherits that untouched.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.gui.shell import icons
from packsmith.gui.shell.panels import PANEL_SPECS


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# --- the font ---------------------------------------------------------------------------

def test_the_font_is_vendored_not_fetched():
    """Offline, like Monaco. A tool that needs the network to draw its own sidebar is a
    tool that breaks on a plane."""
    assert icons.FONT_PATH.is_file()
    assert icons.FONT_PATH.read_bytes()[:4] == b"\x00\x01\x00\x00", "not a TrueType font"


def test_the_licence_ships_with_it():
    """Phosphor is MIT, which asks for the notice to travel with the copy."""
    licence = icons.FONT_PATH.parent / "LICENSE"
    assert licence.is_file()
    assert "MIT" in licence.read_text(encoding="utf-8")


def test_the_font_actually_loads(qapp):
    assert icons.family(), "Qt could not load the vendored font"


# --- the glyph choice ---------------------------------------------------------------------

def test_every_panel_has_an_icon():
    """A strip where one entry is still a letter looks broken rather than mixed."""
    missing = [spec.key for spec in PANEL_SPECS if not icons.glyph(spec.key)]
    assert missing == [], f"panels with no icon: {missing}"


def test_no_two_panels_share_an_icon():
    """The entire point is telling them apart. Four of these panels are flavours of
    "structured data about things", which is exactly where a careless set collides."""
    chosen = [icons.glyph(spec.key) for spec in PANEL_SPECS]
    assert len(set(chosen)) == len(chosen)


def test_there_are_no_icons_for_panels_that_do_not_exist():
    """A stale entry is a glyph nobody sees and a rename nobody notices."""
    known = {spec.key for spec in PANEL_SPECS}
    assert set(icons.GLYPHS) <= known, f"orphaned: {set(icons.GLYPHS) - known}"


# --- the strip ------------------------------------------------------------------------------

def test_the_strip_shows_icons_rather_than_letters(qapp):
    from packsmith.gui.shell.sidebar import Sidebar

    bar = Sidebar()
    try:
        for spec in PANEL_SPECS:
            button = bar._buttons[spec.key]
            assert button.text() == icons.glyph(spec.key)
            assert button.text() != spec.letter
            assert icons.family() in button.font().family()
    finally:
        bar.deleteLater()


def test_the_letter_survives_as_a_fallback(qapp, monkeypatch):
    """If the font ever fails to load, the strip must still be usable. Refusing to start
    over an icon font would be a wild over-reaction to a cosmetic dependency."""
    from packsmith.gui.shell.sidebar import Sidebar

    monkeypatch.setattr(icons, "icon_font", lambda size=17: None)
    bar = Sidebar()
    try:
        for spec in PANEL_SPECS:
            assert bar._buttons[spec.key].text() == spec.letter
    finally:
        bar.deleteLater()


def test_the_tooltip_still_names_the_panel(qapp):
    """An icon is recognisable, not self-describing — the name has to remain reachable."""
    from packsmith.gui.shell.sidebar import Sidebar

    bar = Sidebar()
    try:
        for spec in PANEL_SPECS:
            assert bar._buttons[spec.key].toolTip() == spec.title
    finally:
        bar.deleteLater()


def test_the_stylesheet_does_not_fight_the_font(qapp):
    """A stylesheet's font rules beat `setFont`, so a `font-size` in the strip's QSS would
    squash the icons back to a text face and quietly undo the whole change."""
    from packsmith.gui.shell import sidebar

    assert "font-size" not in sidebar._STRIP_BUTTON_QSS
    assert "font-weight" not in sidebar._STRIP_BUTTON_QSS


# --- grouping (§4.1) -----------------------------------------------------------------------

def test_the_strip_is_ordered_by_what_the_panels_are_for():
    """Two groups: where you work, then the vocabulary that work is expressed in. The
    order is a claim about meaning, so it is pinned rather than left to whoever edits the
    list next."""
    assert [spec.key for spec in PANEL_SPECS] == [
        "views", "files", "automation", "tags", "blueprints", "registry"]


def test_the_groups_are_contiguous():
    """A group that resumes after the divider would draw two separators and mean nothing."""
    groups = [spec.group for spec in PANEL_SPECS]
    assert groups == sorted(groups)


def test_a_separator_is_drawn_between_the_groups(qapp):
    from PySide6.QtWidgets import QFrame
    from packsmith.gui.shell.sidebar import Sidebar

    bar = Sidebar()
    try:
        rules = [w for w in bar.findChildren(QFrame) if w.frameShape() == QFrame.HLine]
        assert len(rules) == len(set(s.group for s in PANEL_SPECS)) - 1
    finally:
        bar.deleteLater()


def test_the_separator_is_not_clickable(qapp):
    """It is furniture. A divider that reacts to the pointer reads as a dead button."""
    from packsmith.gui.shell.sidebar import Sidebar

    bar = Sidebar()
    try:
        assert len(bar._buttons) == len(PANEL_SPECS)
    finally:
        bar.deleteLater()


# --- ownership at icon weight (§6.1 / §6.2) --------------------------------------------------

def _relative_luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_icon_weight_ownership_colours_are_actually_legible():
    """The accent pair were chosen for a 3px bar and an 8px filled dot, where a small solid
    mass of colour reads fine. A thin-stroked 16px glyph is a different job: `OWNER_USER`
    measures 2.0:1 against the panel, which is invisible rather than subtle.

    Pinned so nobody later "tidies" the icon variants back to the accents.
    """
    from packsmith.gui.shell import style

    for name in ("OWNER_USER_ICON", "OWNER_ACTION_ICON"):
        ratio = contrast(getattr(style, name), style.BG_PANEL)
        assert ratio >= 4.0, f"{name} is only {ratio:.1f}:1 against the panel"


def test_the_accent_colours_were_not_simply_reused():
    """If these ever become equal, the icons have silently gone back to being unreadable."""
    from packsmith.gui.shell import style

    assert style.OWNER_USER_ICON != style.OWNER_USER
    assert contrast(style.OWNER_USER, style.BG_PANEL) < 3.0, \
        "the original accent got brighter — recheck whether the icon variant is still needed"


def test_the_two_owners_stay_distinguishable_from_each_other():
    """Slate versus amber is the whole signal; two colours that read alike say nothing."""
    from packsmith.gui.shell import style

    assert contrast(style.OWNER_USER_ICON, style.OWNER_ACTION_ICON) >= 1.3


def test_an_owned_file_icon_differs_from_an_untracked_one(qapp):
    """End to end: the same file type, different owners, must not produce the same icon."""
    from packsmith.gui.shell import style

    untracked = icons.file_icon("emi.json", colour=style.TEXT_FAINT)
    mine = icons.file_icon("emi.json", colour=style.OWNER_USER_ICON)
    theirs = icons.file_icon("emi.json", colour=style.OWNER_ACTION_ICON)

    shots = [i.pixmap(16, 16).toImage() for i in (untracked, mine, theirs)]
    assert shots[0] != shots[1] and shots[1] != shots[2] and shots[0] != shots[2]


def test_the_same_request_is_cached(qapp):
    """A 300-mod instance has thousands of rows; minting a QIcon per row would be waste."""
    from packsmith.gui.shell import style

    first = icons.file_icon("a.json", colour=style.TEXT)
    second = icons.file_icon("b.json", colour=style.TEXT)
    assert first is second, "same glyph and colour should reuse one icon"
