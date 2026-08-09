"""Shared dark-theme constants for the shell (design 4.1: "dark grays, blue accent").

Kept in one place so the sidebar, workspace, and bottom panel stay visually coherent
as they grow. Not a theming system — just the palette the shell agrees on.
"""

BG_DEEP = "#1e1e1e"      # workspace / content background
BG_PANEL = "#252525"     # panel + header backgrounds
BG_CHROME = "#2d2d2d"    # buttons, tabs, inactive chrome
BORDER = "#3a3a3a"
ACCENT = "#3a5070"       # selection blue
ACCENT_EDGE = "#5080b0"
TEXT = "#d0d0d0"
TEXT_MUTED = "#888888"
TEXT_FAINT = "#666666"

SIDEBAR_STRIP_WIDTH = 38
SIDEBAR_PANEL_WIDTH = 230

# Ownership palette (design 3.2.1 / 6.1). Shared by the registry table's per-cell bars and
# the file browser, so "who owns this" reads the same everywhere — the two engines are
# different machinery but they are one vocabulary to the user.
#   user   -> muted slate: "this is yours", low-key
#   action -> amber: "an action manages this", meant to catch the eye
OWNER_USER = "#4a5568"
OWNER_ACTION = "#c8963c"

# Failure text — readable on the dark ground, unlike Qt.red.
ERROR = "#e06c6c"

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
