from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor

from packsmith.gui.shell import style
from packsmith.gui.table.registry_table_model import OwnershipRole

# Per-cell ownership indicator: a subtle vertical bar on the cell's left edge
# (design 3.2.1). First-pass visual — tune freely.
#   user-owned   -> muted slate: "this is yours", low-key and unobtrusive
#   action-owned -> amber accent: "an action manages this", meant to catch the eye
#                   (matches the loud-transfer philosophy — action ownership is notable)
#   pristine     -> no bar
_OWNER_COLORS = {
    "user": QColor(style.OWNER_USER),
    "action": QColor(style.OWNER_ACTION),
}

_BAR_WIDTH = 3

# What an empty cell says. "----" was four characters of noise repeated down a whole
# column; a single en dash reads as "nothing here" without competing with the values
# beside it.
EMPTY = "–"
EMPTY_COLOR = QColor("#4a4a4a")
# Centred, whatever the column's own alignment is. The dash is not a value — it is a mark
# saying there isn't one — so inheriting the column's alignment made it masquerade as
# content, and a column of them zig-zagged left/right/centre by type. Centred, they read
# as one uniform "nothing here" down the table.
EMPTY_ALIGN = Qt.AlignHCenter | Qt.AlignVCenter
# How far text sits off the cell edge. Two more pixels than it had, because content
# touching a boundary is most of what makes a table feel cramped.
INSET = 10


def paint_row_rule(painter, option):
    """The hairline under a row.

    Drawn by the delegate rather than by Qt's grid, because the grid is a *lattice* — it
    draws the vertical lines too, and a full lattice is what makes a table read as a
    database admin tool. Rows want separating; columns already are, by alignment.
    """
    painter.save()
    painter.setPen(QColor(style.RULE))
    painter.drawLine(option.rect.left(), option.rect.bottom(),
                     option.rect.right(), option.rect.bottom())
    painter.restore()


def paint_column_rule(painter, option):
    """The hairline on a cell's right edge.

    A step fainter than the row rule on purpose. Rows are what you *track*, so they get the
    stronger line; columns are already demarcated by alignment and only want confirming
    where one ends. Matched in weight, the two together stop being separators and become a
    lattice — which is the look this was drawn to avoid.
    """
    painter.save()
    painter.setPen(QColor(style.RULE_COLUMN))
    painter.drawLine(option.rect.right(), option.rect.top(),
                     option.rect.right(), option.rect.bottom())
    painter.restore()


def paint_ownership_bar(painter, option, index):
    """Draw the ownership bar for a cell. Reads OwnershipRole off the index
    (None = pristine = nothing drawn). Call right after the cell background is
    painted: the bar hugs the left edge, clear of the content inset, and renders
    at full opacity regardless of any dimming the delegate applies to its content."""
    ownership = index.data(OwnershipRole)
    if not ownership:
        return
    color = _OWNER_COLORS.get(ownership.get("kind"))
    if color is None:
        return
    r = option.rect
    painter.save()
    painter.setOpacity(1.0)
    painter.fillRect(QRect(r.left(), r.top() + 1, _BAR_WIDTH, r.height() - 2), color)
    painter.restore()
