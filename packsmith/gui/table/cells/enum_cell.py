from PySide6.QtWidgets import QStyledItemDelegate, QComboBox, QStyle, QApplication
from PySide6.QtCore import Qt, QModelIndex, QPointF, QRect, QEvent, QTimer
from PySide6.QtGui import QPainter, QPalette, QColor, QPolygonF

from packsmith.gui.table.cells.ownership import paint_ownership_bar


class EnumCellDelegate(QStyledItemDelegate):
    """Dropdown delegate for enum tag columns.

    Arrow button (single click) and double-click on text both open the same
    QComboBox editor via Qt's standard editor lifecycle.

    For enums without a default, the dropdown includes a blank option at the top
    and the cell renders as empty when unset.
    For enums with a default, no blank option — unset is indistinguishable from default.

    Delete/Backspace clears non-defaulted enums (handled by the view)."""

    def _get_definition(self, model, index: QModelIndex) -> dict | None:
        return model.tag_definition_for_column(index.column())

    def _arrow_rect(self, option) -> QRect:
        """The clickable arrow button area on the right side of the cell."""
        w = 20
        return QRect(option.rect.right() - w, option.rect.top(), w, option.rect.height())

    def paint(self, painter: QPainter, option, index: QModelIndex):
        self.initStyleOption(option, index)
        style = QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter)
        paint_ownership_bar(painter, option, index)

        value = index.data(Qt.DisplayRole) or ""

        painter.save()

        # Text
        text_rect = option.rect.adjusted(6, 0, -22, 0)
        if value:
            painter.setPen(option.palette.color(QPalette.Text))
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, value)
        else:
            painter.setPen(QColor("#555555"))
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, "----")

        # Arrow button — always drawn now that a column is not armed before it will
        # accept anything. It doubles as the affordance: an enum cell looks like a
        # dropdown because it is one.
        arrow = self._arrow_rect(option)
        inset = arrow.adjusted(1, 3, -2, -3)

        # Border only, no fill
        painter.setPen(QColor("#4a4a4a"))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(inset)

        # Triangle
        cx = inset.center().x()
        cy = inset.center().y()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#aaaaaa"))
        painter.setRenderHint(QPainter.Antialiasing)
        painter.drawPolygon(QPolygonF([
            QPointF(cx - 3, cy - 2),
            QPointF(cx + 3, cy - 2),
            QPointF(cx, cy + 2),
        ]))

        painter.restore()

    def editorEvent(self, event, model, option, index: QModelIndex) -> bool:
        # Single click on the arrow button opens the editor via the standard path
        if event.type() == QEvent.MouseButtonRelease:
            if self._arrow_rect(option).contains(event.pos()):
                view = option.widget
                if view:
                    view.edit(index)
                return True

        return False

    def createEditor(self, parent, option, index: QModelIndex):
        defn = self._get_definition(index.model(), index)
        if not defn:
            return None

        combo = QComboBox(parent)

        has_default = defn.get("default_value") is not None
        if not has_default:
            combo.addItem("")

        for val in defn.get("values", []):
            combo.addItem(val)

        combo.activated.connect(lambda: self.commitData.emit(combo))
        combo.activated.connect(lambda: self.closeEditor.emit(combo))

        # Auto-open the dropdown once Qt has positioned the widget
        QTimer.singleShot(0, combo.showPopup)

        return combo

    def setEditorData(self, editor: QComboBox, index: QModelIndex):
        current = index.data(Qt.DisplayRole) or ""
        idx = editor.findText(current)
        if idx >= 0:
            editor.setCurrentIndex(idx)

    def setModelData(self, editor: QComboBox, model, index: QModelIndex):
        value = editor.currentText()
        if value == "":
            model.setData(index, None, Qt.EditRole)
        else:
            model.setData(index, value, Qt.EditRole)

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        hint.setHeight(max(hint.height(), 24))
        return hint
