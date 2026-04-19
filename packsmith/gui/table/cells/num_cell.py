from PySide6.QtWidgets import QStyledItemDelegate, QLineEdit, QStyle, QApplication
from PySide6.QtCore import Qt, QModelIndex, QSortFilterProxyModel, QRegularExpression
from PySide6.QtGui import QPainter, QPalette, QColor, QRegularExpressionValidator


class NumCellDelegate(QStyledItemDelegate):
    """Inline text editor delegate for number tag columns.

    Only allows digits, commas, underscores, periods, and an optional leading minus.
    On commit: strips separators, parses as int or float.
    Invalid input → brief red flash, revert to previous value.
    Delete/Backspace clears to unset (handled by the view)."""

    def _source_model(self, model):
        if isinstance(model, QSortFilterProxyModel):
            return model.sourceModel()
        return model

    def _source_col(self, model, index: QModelIndex) -> int:
        if isinstance(model, QSortFilterProxyModel):
            return model.mapToSource(index).column()
        return index.column()

    def _is_editing(self, model, index: QModelIndex) -> bool:
        source = self._source_model(model)
        return source.is_editing(self._source_col(model, index))

    def paint(self, painter: QPainter, option, index: QModelIndex):
        self.initStyleOption(option, index)
        style = QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter)

        value = index.data(Qt.DisplayRole) or ""
        editing = self._is_editing(index.model(), index)

        painter.save()
        painter.setOpacity(1.0 if editing else 0.5)

        text_rect = option.rect.adjusted(6, 0, -6, 0)
        if value:
            painter.setPen(option.palette.color(QPalette.Text))
            painter.drawText(text_rect, Qt.AlignRight | Qt.AlignVCenter, value)
        else:
            painter.setPen(QColor("#555555"))
            painter.drawText(text_rect, Qt.AlignRight | Qt.AlignVCenter, "----")

        painter.restore()

    def createEditor(self, parent, option, index: QModelIndex):
        if not self._is_editing(index.model(), index):
            return None

        editor = QLineEdit(parent)
        editor.setAlignment(Qt.AlignRight)
        # Allow digits, commas, underscores, periods, optional leading minus
        validator = QRegularExpressionValidator(
            QRegularExpression(r"^-?[0-9,_.]*$"), editor
        )
        editor.setValidator(validator)
        return editor

    def setEditorData(self, editor: QLineEdit, index: QModelIndex):
        current = index.data(Qt.DisplayRole) or ""
        editor.setText(current)

    def setModelData(self, editor: QLineEdit, model, index: QModelIndex):
        text = editor.text().strip()

        # Empty or only separators → clear the cell
        if not text or not text.lstrip("-").replace(",", "").replace("_", "").replace(".", ""):
            model.setData(index, None, Qt.EditRole)
            return

        # Strip thousand separators
        cleaned = text.replace(",", "").replace("_", "")

        parsed = None
        try:
            parsed = int(cleaned)
        except ValueError:
            try:
                parsed = float(cleaned)
            except ValueError:
                pass

        if parsed is not None:
            model.setData(index, parsed, Qt.EditRole)
        # else: invalid input — silently revert to previous value

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        hint.setHeight(max(hint.height(), 24))
        return hint
