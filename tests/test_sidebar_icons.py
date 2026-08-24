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
    list next.

    Tracked folders are NOT a slot here. They are a list you configure once and then browse
    through, so they live as a subtab of Files beside the browser they configure — a
    top-level slot would have put a settings list next to the six things you use daily.
    """
    assert [spec.key for spec in PANEL_SPECS] == [
        "views", "automation", "files", "tags", "blueprints", "registry"]


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


# --- how the glyph sits in its box -----------------------------------------------------------

def _ink_rows(icon, size=16):
    """The rows carrying ink, in the pixmap's own pixels."""
    image = icon.pixmap(size, size).toImage()
    rows = [y for y in range(image.height())
            if any(image.pixelColor(x, y).alpha() > 20 for x in range(image.width()))]
    return rows, image.height()


@pytest.mark.parametrize("name", ["options.txt", "emi.json", "pack.png", "Quark.jar"])
def test_the_glyph_is_not_clipped_by_its_own_box(qapp, name):
    """The size was set in **points** while the box is in **pixels**, so a 16px icon was
    drawn with a ~17.3px em and Phosphor — which fills nearly its whole em — lost its top
    row to the edge. It read as an icon sitting too high rather than as a cropped one,
    which is why it survived a look.

    Ink touching row 0 is the signature, and it comes back the moment anyone reaches for a
    point size again.
    """
    from packsmith.gui.shell import style

    rows, height = _ink_rows(icons.file_icon(name, colour=style.TEXT))
    assert rows, "nothing was drawn at all"
    assert rows[0] > 0, f"{name}: glyph is clipped against the top of its box"
    assert rows[-1] < height - 1, f"{name}: glyph is clipped against the bottom of its box"


def test_the_glyph_sits_slightly_low_on_purpose(qapp):
    """Qt centres an icon on its box but centres *text* on the font's line box, which
    reserves a descender whether the word has one or not — so a geometrically centred icon
    floats above the text beside it. The nudge is what puts the two on one line, and a
    "tidy" pass that re-centres the glyph would undo the fix without breaking anything
    visibly enough to notice.
    """
    from packsmith.gui.shell import style

    assert icons.GLYPH_NUDGE > 0
    rows, height = _ink_rows(icons.file_icon("emi.json", colour=style.TEXT))
    above, below = rows[0], height - 1 - rows[-1]
    assert below < above, f"the glyph is centred ({above} above, {below} below), so it floats"


def test_the_nudge_and_the_fill_are_still_compatible(qapp):
    """These two numbers spend the same 4px of headroom, so raising either alone puts the
    glyph back through the edge of its box. The clipping test above catches it, but this
    says *why* — the pair has to be re-solved together, not adjusted one at a time.
    """
    from packsmith.gui.shell import style

    rows, height = _ink_rows(icons.file_icon("emi.json", colour=style.TEXT))
    ink = rows[-1] - rows[0] + 1
    headroom = height - ink
    assert headroom / 2 > icons.GLYPH_NUDGE * height, \
        "the nudge is larger than the room the fill leaves for it"


# --- folders that open with their row ------------------------------------------------

def test_a_folder_row_swaps_its_icon_on_expansion(qapp):
    """Files, jar contents and View groups all use this. A shut folder sitting directly
    above its own visible contents is a small lie the eye catches before the mind does."""
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
    from packsmith.gui.shell import style

    tree = QTreeWidget()
    icons.follow_expansion(tree)
    folder = QTreeWidgetItem(["stuff"])
    tree.addTopLevelItem(folder)
    folder.addChild(QTreeWidgetItem(["inside"]))
    icons.set_folder_icon(folder, colour=style.TEXT)

    shut = folder.icon(0).cacheKey()
    folder.setExpanded(True)
    assert folder.icon(0).cacheKey() != shut, "the folder stayed shut while open"
    folder.setExpanded(False)
    assert folder.icon(0).cacheKey() == shut
    tree.deleteLater()


def test_rows_without_the_pair_are_left_alone(qapp):
    """The handler runs for every expansion in the tree, and a tree mixes folders with
    everything else — a plain row must not lose its icon to it."""
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
    from packsmith.gui.shell import style

    tree = QTreeWidget()
    icons.follow_expansion(tree)
    plain = QTreeWidgetItem(["not a folder"])
    tree.addTopLevelItem(plain)
    plain.addChild(QTreeWidgetItem(["child"]))
    plain.setIcon(0, icons.file_icon("thing.json", colour=style.TEXT))

    before = plain.icon(0).cacheKey()
    plain.setExpanded(True)
    assert plain.icon(0).cacheKey() == before
    tree.deleteLater()


def test_the_pair_can_be_any_two_glyphs(qapp):
    """Views groups use folder-simple rather than the Files panel's notched folder — the
    tree-level handler knows nothing about which, because the pair lives on the item."""
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
    from packsmith.gui.shell import style

    tree = QTreeWidget()
    icons.follow_expansion(tree)
    item = QTreeWidgetItem(["group"])
    tree.addTopLevelItem(item)
    item.addChild(QTreeWidgetItem(["child"]))
    icons.set_expanding_icon(item, icons.ui("group"), icons.ui("group_open"),
                             colour=style.TEXT_MUTED)

    shut = item.icon(0).cacheKey()
    item.setExpanded(True)
    assert item.icon(0).cacheKey() != shut
    tree.deleteLater()
