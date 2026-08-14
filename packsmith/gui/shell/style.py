"""Shared dark-theme constants for the shell (design 4.1: "dark grays, blue accent").

Kept in one place so the sidebar, workspace, and bottom panel stay visually coherent
as they grow. Not a theming system — just the palette the shell agrees on.
"""

BG_DEEP = "#1e1e1e"      # workspace / content background
BG_PANEL = "#252525"     # panel + header backgrounds
BG_CHROME = "#2d2d2d"    # buttons, tabs, inactive chrome
BG_HOVER = "#383838"     # chrome under the pointer — one step up from BG_CHROME
BORDER = "#3a3a3a"
ACCENT = "#3a5070"       # selection blue
ACCENT_EDGE = "#5080b0"
TEXT = "#d0d0d0"
TEXT_MUTED = "#888888"
TEXT_FAINT = "#666666"

SIDEBAR_STRIP_WIDTH = 38
SIDEBAR_PANEL_WIDTH = 230

# How wide a splitter's grab area is. Qt's default is about 4px, which is a precision-aiming
# exercise — you hunt for the divider rather than just grabbing it. The handle stays
# visually a hairline (see SPLITTER_QSS: the drawn line is the 1px border, the rest is
# transparent), so this buys hit area without making the seams look like gutters.
SPLITTER_WIDTH = 9

# Ownership palette (design 3.2.1 / 6.1). Shared by the registry table's per-cell bars and
# the file browser, so "who owns this" reads the same everywhere — the two engines are
# different machinery but they are one vocabulary to the user.
#   user   -> muted slate: "this is yours", low-key
#   action -> amber: "an action manages this", meant to catch the eye
OWNER_USER = "#4a5568"
OWNER_ACTION = "#c8963c"
# The same amber, dimmed: used where an action-owned value is also LOCKED against casual
# editing. Amber says "an action wrote this"; the dimming says "and it isn't yours to type
# over" — the standard read-only cue, so the cell doesn't invite an edit it will refuse.
OWNER_ACTION_LOCKED = "#96702d"

# The same two owners, at icon weight. The pair above were chosen as ACCENTS — a 3px bar in
# a table cell, an 8px filled dot — where a small solid mass of colour reads fine. A
# thin-stroked 16px glyph is a different job: measured against BG_PANEL, `OWNER_USER` comes
# out at 2.0:1, which is invisible rather than subtle. These are the same hues carried up to
# roughly 5:1 so a file's owner is legible from its icon.
OWNER_USER_ICON = "#7d92b3"
OWNER_ACTION_ICON = "#d4a44e"

# Failure text — readable on the dark ground, unlike Qt.red.
ERROR = "#e06c6c"
# Something is wrong but nobody did anything wrong — a binding whose registry entry left
# with the packdump. Distinct from ERROR so "you must decide" and "you should look" don't
# shout at the same volume.
WARNING = "#d0a050"

# Confirmation that something the app went looking for is genuinely there and checked out —
# a located client jar, a verified path. Muted rather than a signal green: it is the state
# you expect, so it should read as settled, not as a congratulation.
SUCCESS = "#7ea87e"

# A gap in a blueprint instance: an empty slot is missing content, not an error, so it
# reads as absence rather than alarm (design 3.2.2).
GAP = "#3a3a48"


# Wide to grab, thin to look at. The handle itself is transparent and only its inner edge
# is drawn, so the seam reads as a 1px line while the draggable strip around it is 9px. On
# hover the line brightens, which is the only feedback needed to say "this one moves".
SPLITTER_QSS = f"""
    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:horizontal {{
        border-left: 1px solid {BORDER};
        margin: 0 {(SPLITTER_WIDTH - 1) // 2}px;
    }}
    QSplitter::handle:vertical {{
        border-top: 1px solid {BORDER};
        margin: {(SPLITTER_WIDTH - 1) // 2}px 0;
    }}
    QSplitter::handle:hover {{ border-color: {ACCENT_EDGE}; }}
"""


def qt_colour(hex_string):
    """QColor from one of the constants above, for the widget APIs that want an object
    rather than a stylesheet string."""
    from PySide6.QtGui import QColor
    return QColor(hex_string)

# Shared look for the list/tree widgets that fill sidebar panels.
LIST_QSS = f"""
    QTreeWidget, QListWidget {{
        background: {BG_PANEL}; color: {TEXT};
        border: none; outline: none; font-size: 12px;
    }}
    QTreeWidget::item, QListWidget::item {{ padding: 3px 2px; border: none; }}
    QTreeWidget::item:hover, QListWidget::item:hover {{ background: {BG_CHROME}; }}
    QTreeWidget::item:selected, QListWidget::item:selected {{
        background: {ACCENT}; color: {TEXT};
    }}
    QTreeWidget::branch {{ background: {BG_PANEL}; }}
"""
