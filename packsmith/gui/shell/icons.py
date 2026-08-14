"""Phosphor icons, as a font (design 4.1's icon strip).

The sidebar shipped with single letters — V, B, T, F, R, J, A — and its own source called
them a placeholder. Three things were wrong with them:

* **A letter is a label for a label.** "T" abbreviates *Tags*, which is itself the name of
  the thing; that is two steps from the concept. An icon is one, and people navigate by
  purpose rather than by name.
* **They cannot be hit peripherally.** The point of an edge strip is reaching for it without
  looking, and muscle memory grabs *silhouettes*. Seven glyphs at one size, weight and
  colour have nearly identical outlines, so the strip has to be read — and reading needs
  the fovea.
* **They run out.** Seven letters are used and §4.1 expects more panels; Search and Settings
  both want S.

**A font rather than SVG files**, because the strip already colours its glyph through
stylesheets (`color:` on `:hover` and `:checked`). A font drops into that untouched — the
same text-colour rules keep working — whereas SVGs would need a recoloured variant per
state or a painting layer, for no gain. It is also one vendored file, which is what was
already done for Monaco.

Phosphor is MIT licensed; see `gui/vendor/phosphor/LICENSE`.
"""
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

from packsmith.common.logging import log

FONT_PATH = Path(__file__).resolve().parents[1] / "vendor" / "phosphor" / "Phosphor.ttf"

# Chosen for DISTINCT OUTLINES rather than for prettiness, because the outline is what a
# glance actually resolves: grid, drafting sheet, angled pentagon, folder, cylinder,
# triangle, box. Four of these panels are flavours of "structured data about things", which
# is exactly where a careless set makes everything look alike.
#
# Blueprints was a `stack` and had to move: stacked bands and the registry's cylinder are
# the same silhouette, and the two sit next to each other in the strip.
GLYPHS = {
    "views": "",        # table — a View is a query, rendered as one
    "blueprints": "",   # blueprint
    "tags": "",         # tag
    "files": "",        # folder
    "registry": "",     # database — Layer 1, the read-only game data
    "automation": "",   # play — Jobs and Actions share a slot; running is the verb
}

# --- file types (design 6.2) ---------------------------------------------------------------
#
# By suffix rather than through `filetypes.classify`, because that answers a different
# question. The classifier decides which *editor* opens a file and groups by capability —
# every `.json`, `.toml` and `.txt` is one TEXT. Here the point is telling them apart at a
# glance in a tree, so a config and a log want different marks even though the same editor
# opens both.
FOLDER_GLYPH = ""          # folder
DEFAULT_FILE_GLYPH = ""    # file

FILE_GLYPHS = {
    # data the pack is made of
    ".json": "", ".json5": "", ".mcmeta": "",   # brackets-curly
    # configuration you tune
    ".toml": "", ".cfg": "", ".ini": "",        # sliders-horizontal
    ".properties": "", ".yaml": "", ".yml": "",
    # code
    ".star": "", ".js": "", ".py": "",          # file-code
    # prose and logs
    ".txt": "", ".md": "", ".log": "",          # file-text
    # pictures
    ".png": "", ".jpg": "", ".jpeg": "",        # file-image
    ".gif": "", ".bmp": "", ".webp": "", ".tga": "",
    # archives — a mod jar is the common one
    ".jar": "", ".zip": "", ".mrpack": "",      # file-zip
    ".gz": "", ".tgz": "", ".7z": "", ".rar": "",
    # NBT is a tag TREE, and looks like one
    ".nbt": "", ".dat": "", ".dat_old": "",     # tree-structure
    ".schematic": "", ".litematic": "",
    # compiled code — the canonical thing you cannot override (§6.5)
    ".class": "",                                # file-c
}


# --- NBT tag types (design 6.4) ---------------------------------------------------------
#
# The viewer already has a Type column, so these are not the only way to know what a tag is.
# They earn their place on a different axis: NBT is deep and you scan it *vertically*, and
# shape says "this branch is structure, that one is leaves" without reading a word.
NBT_GLYPHS = {
    "Compound": "\ue860",        # brackets-curly — the closest NBT has to key/value
    "List": "\ue2f2",            # list-bullets — homogeneous and positional
    "String": "\ue660",          # quotes
    # One mark for every integer width. The Type column already says which, and four
    # near-identical glyphs would be noise pretending to be information.
    "Byte": "\ue2a2", "Short": "\ue2a2", "Int": "\ue2a2", "Long": "\ue2a2",   # hash
    # Distinct from the integers, because exact-versus-not is the difference that actually
    # bites when you edit one.
    "Float": "\uedaa", "Double": "\uedaa",                                 # approximate-equals
    "Byte_Array": "\ue85e", "Int_Array": "\ue85e", "Long_Array": "\ue85e",     # brackets-square
}
DEFAULT_NBT_GLYPH = "\ue1fe"     # dots-three


def nbt_glyph(type_name: str) -> str:
    """``type_name`` is the viewer's shortened form — "Compound", "Byte_Array"…"""
    return NBT_GLYPHS.get(type_name, DEFAULT_NBT_GLYPH)


def tag_icon(type_name: str, *, colour: str, size: int = 16):
    """An icon for one NBT tag type. Cached exactly like `file_icon`."""
    return _render(nbt_glyph(type_name), colour, None, size)

_ICON_CACHE = {}
_FAMILY = None


def file_glyph(name: str, is_dir: bool = False) -> str:
    if is_dir:
        return FOLDER_GLYPH
    suffix = ("." + str(name).rsplit(".", 1)[-1].lower()) if "." in str(name) else ""
    return FILE_GLYPHS.get(suffix, DEFAULT_FILE_GLYPH)


def file_icon(name: str, is_dir: bool = False, *, colour: str, badge: str = None,
              size: int = 16):
    """A type glyph, optionally with a small ownership badge in its corner.

    Three signals share this row and each gets its own channel, which is the only way they
    stay readable together: **shape** says what the file is, the **badge** says who owns it
    (design 6.1), and the **text colour** says whether it is tracked at all (§6.2).

    Overlapping any two of those would be the bug this design avoids — the ownership dot
    used to *be* the icon, so adding a type icon naively would have silently deleted the
    ownership indicator on every owned file.
    """
    return _render(file_glyph(name, is_dir), colour, badge, size)


# How much of the icon box the glyph's em fills, and how far down it is nudged.
#
# **Points were the bug.** The size was set in points while the box is in pixels, so a 16px
# icon was drawn with a 13pt em — about 17.3px — and Phosphor's ink, which fills nearly its
# whole em, ran off the top edge. Pixels make the fill a number we choose rather than one
# the screen's DPI chooses for us.
#
# The nudge is optical, not geometric. Qt centres an icon on its box but centres *text* on
# the font's line box, and a line box includes the descender — so the text a reader sees
# sits lower than the row's true middle, and an icon centred beside it floats.
#
# The two numbers trade against each other, which is why they live together. Phosphor's ink
# is about 81% of its em, so a 16px icon has roughly 4px of headroom in total and every
# pixel of nudge spends some of it. One pixel down is what the largest glyph that still
# fits can afford; buying a second would mean a fifth off the icon, and an icon a fifth
# smaller to sit a pixel truer is a bad trade.
GLYPH_FILL = 0.92
GLYPH_NUDGE = 1 / 16


def _render(glyph: str, colour: str, badge, size: int):
    """Paint one glyph into a cached QIcon."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

    key = (glyph, colour, badge, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]

    name = family()
    if not name:
        return QIcon()

    # Drawn at 2x and marked as such, so the glyph stays crisp on a scaled display rather
    # than being a blurry 16px bitmap stretched out.
    scale = 2
    box = size * scale
    pixmap = QPixmap(box, box)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)
    scaled = QFont(name)
    scaled.setPixelSize(max(1, round(box * GLYPH_FILL)))
    painter.setFont(scaled)
    painter.setPen(QColor(colour))
    drop = round(box * GLYPH_NUDGE)
    painter.drawText(pixmap.rect().adjusted(0, drop, 0, drop), Qt.AlignCenter, glyph)

    if badge:
        radius = 4.5 * scale
        edge = pixmap.width() - radius * 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(badge))
        painter.drawEllipse(QRectF(edge, edge, radius * 2, radius * 2))
    painter.end()

    pixmap.setDevicePixelRatio(scale)
    _ICON_CACHE[key] = QIcon(pixmap)
    return _ICON_CACHE[key]


def family() -> str:
    """The loaded font family name, or "" if the font could not be loaded.

    Loaded once and cached. A failure is not fatal: the sidebar falls back to its letters,
    which is worse-looking but perfectly usable — an icon strip is not worth refusing to
    start over.
    """
    global _FAMILY
    if _FAMILY is not None:
        return _FAMILY
    _FAMILY = ""
    font_id = QFontDatabase.addApplicationFont(str(FONT_PATH))
    if font_id == -1:
        log.warning("Could not load the Phosphor icon font from %s", FONT_PATH)
        return _FAMILY
    families = QFontDatabase.applicationFontFamilies(font_id)
    if families:
        _FAMILY = families[0]
    return _FAMILY


def icon_font(size: int = 17) -> QFont | None:
    name = family()
    return QFont(name, size) if name else None


def glyph(key: str) -> str:
    """The character for a panel, or "" when there isn't one."""
    return GLYPHS.get(key, "")


# --- UI chrome ---------------------------------------------------------------------------
#
# Buttons, row markers, category headers. The line drawn here is deliberate: **Phosphor for
# affordances, plain typography for inline text marks.** A `→` in a sentence, a `✓` in a
# status line and a `⚠` before a warning are punctuation — they read fine mid-prose and
# turning them into icons would be worse, not more consistent. A thing you *click* is a
# different matter, and an emoji button next to a real icon set is what looks unfinished.
UI_GLYPHS = {
    "play": "\ue3d0",
    "pin": "\ue3e2",            # push-pin
    "up": "\ue13c",             # caret-up
    "down": "\ue136",           # caret-down
    "close": "\ue4f6",          # x
    "add": "\ue3d4",            # plus
    "settings": "\ue270",       # gear
    "save": "\ue248",           # floppy-disk
    "hint": "\ue2dc",           # lightbulb
    "datapacks": "\ue390",      # package — a datapack IS a pack
    "resourcepacks": "\ue6c8",  # palette — assets rather than data
}


def ui(key: str) -> str:
    """The character for a piece of chrome, or "" if the font never loaded."""
    return UI_GLYPHS.get(key, "") if family() else ""


def mark(widget, key: str, *, size: int = 13, text: str = None):
    """Put a Phosphor glyph on a widget, keeping any label beside it.

    Falls back to leaving whatever text was already there when the font is missing, so a
    button never ends up blank — an unlabelled button is worse than an ugly one.
    """
    glyph = ui(key)
    if not glyph:
        return widget
    widget.setText(f"{glyph}  {text}" if text else glyph)
    font = icon_font(size)
    if font is not None:
        widget.setFont(font)
    return widget


def ui_icon(key: str, *, colour: str, size: int = 16):
    """A chrome glyph as a QIcon, for tree rows and anywhere `setIcon` is wanted."""
    return _render(ui(key), colour, None, size)
