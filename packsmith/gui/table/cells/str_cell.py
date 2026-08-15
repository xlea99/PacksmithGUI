from PySide6.QtWidgets import QStyledItemDelegate, QLineEdit, QStyle, QApplication
from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtGui import QPainter, QPalette, QColor

from packsmith.gui.table.cells.ownership import (
    paint_ownership_bar, paint_row_rule, paint_column_rule,
    EMPTY, EMPTY_ALIGN, EMPTY_COLOR, INSET)


class StrCellDelegate(QStyledItemDelegate):
    """Inline text editor delegate for string tag columns.

    Double-click opens a QLineEdit. Enter commits, Escape cancels.
    Delete/Backspace clears to unset (handled by the view)."""

    def paint(self, painter: QPainter, option, index: QModelIndex):
        self.initStyleOption(option, index)
        style = QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter)
        paint_ownership_bar(painter, option, index)
        paint_row_rule(painter, option)
        paint_column_rule(painter, option)

        value = index.data(Qt.DisplayRole) or ""

        painter.save()

        text_rect = option.rect.adjusted(INSET, 0, -INSET, 0)
        if value:
            painter.setPen(option.palette.color(QPalette.Text))
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter,
                             painter.fontMetrics().elidedText(
                                 value, Qt.ElideRight, text_rect.width()))
        else:
            painter.setPen(EMPTY_COLOR)
            painter.drawText(option.rect, EMPTY_ALIGN, EMPTY)

        painter.restore()

    def createEditor(self, parent, option, index: QModelIndex):
        return QLineEdit(parent)

    def setEditorData(self, editor: QLineEdit, index: QModelIndex):
        current = index.data(Qt.DisplayRole) or ""
        editor.setText(current)

    def setModelData(self, editor: QLineEdit, model, index: QModelIndex):
        text = editor.text().strip()
        if text:
            model.setData(index, text, Qt.EditRole)
        else:
            model.setData(index, None, Qt.EditRole)

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        hint.setHeight(max(hint.height(), 24))
        return hint
