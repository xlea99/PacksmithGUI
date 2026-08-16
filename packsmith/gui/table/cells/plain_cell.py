from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QApplication
from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtGui import QPainter, QPalette

from packsmith.gui.table.cells.ownership import paint_row_rule, paint_column_rule, INSET


class PlainCellDelegate(QStyledItemDelegate):
    """Read-only text for the L1 columns — id, mod, attributes.

    These used Qt's stock delegate, which was *nearly* right and wrong in the two ways that
    make a table look assembled rather than designed: its text inset is about 4px against
    the 10px the tag delegates use, so every column started at a slightly different place;
    and it draws no row rule, so the hairlines stopped dead where the tag columns did.

    Read-only in the strong sense — no editor, ever. L1 is a report (§3.1), and a delegate
    that cannot make one is a better guarantee than a flag that says it won't.
    """

    def paint(self, painter: QPainter, option, index: QModelIndex):
        self.initStyleOption(option, index)
        QApplication.style().drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter)
        paint_row_rule(painter, option)
        paint_column_rule(painter, option)

        text = option.text
        if not text:
            return

        painter.save()
        # Read off `option` rather than asking the model again: `initStyleOption` has
        # already resolved DisplayRole, ForegroundRole and TextAlignmentRole into it, and
        # `data()` is the hottest call in the table. The dimming of an id standing in for a
        # missing display name (§3.1) survives because ForegroundRole lands in the palette.
        painter.setPen(option.palette.color(QPalette.Text))
        rect = option.rect.adjusted(INSET, 0, -INSET, 0)
        align = option.displayAlignment or (Qt.AlignLeft | Qt.AlignVCenter)
        painter.drawText(rect, align,
                         painter.fontMetrics().elidedText(
                             str(text), Qt.ElideRight, rect.width()))
        painter.restore()

    def createEditor(self, parent, option, index):
        return None
