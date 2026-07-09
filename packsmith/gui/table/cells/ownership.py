from PySide6.QtCore import QRect
from PySide6.QtGui import QColor

from packsmith.gui.table.registry_table_model import OwnershipRole

# Per-cell ownership indicator: a subtle vertical bar on the cell's left edge
# (design 3.2.1). First-pass visual — tune freely.
#   user-owned   -> muted slate: "this is yours", low-key and unobtrusive
#   action-owned -> amber accent: "an action manages this", meant to catch the eye
#                   (matches the loud-transfer philosophy — action ownership is notable)
#   pristine     -> no bar
_OWNER_COLORS = {
    "user": QColor("#4a5568"),
    "action": QColor("#c8963c"),
}

_BAR_WIDTH = 3


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
