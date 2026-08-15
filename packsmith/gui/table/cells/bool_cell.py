from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QApplication
from PySide6.QtCore import Qt, QRect, QPointF, QModelIndex, QEvent
from PySide6.QtGui import QPainter, QColor, QPen, QPolygonF

from packsmith.gui.shell import style
from packsmith.gui.table.cells.ownership import paint_ownership_bar, paint_row_rule, paint_column_rule


class BoolCellDelegate(QStyledItemDelegate):
    """Checkbox delegate for bool tag columns.

    Click toggles between True and False.
    Delete/Backspace clears to unset (handled by the view, only for tags without defaults).

    Rendering is driven entirely by the model's data() — which already returns
    default values for unset tags via get_tag(). No default-awareness needed here."""

    def _get_value(self, index: QModelIndex):
        raw = index.data(Qt.DisplayRole)
        if raw == "" or raw is None:
            return None
        if isinstance(raw, str):
            return raw.lower() == "true"
        return bool(raw)

    def _checkbox_rect(self, option) -> QRect:
        """Center a checkbox-sized rect within the cell."""
        check_size = 15
        x = option.rect.x() + (option.rect.width() - check_size) // 2
        y = option.rect.y() + (option.rect.height() - check_size) // 2
        return QRect(x, y, check_size, check_size)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        """Painted rather than handed to the platform style.

        The native indicator at this size renders as a pale rounded pill on a dark ground —
        it reads as a disabled text field, not as a checkbox, and it was the last thing in
        the table that looked borrowed. Three states, three deliberate weights:

        * **unset** — a faint hollow square. Nobody has decided; it should be quiet enough
          that a column of them looks empty rather than full of unticked boxes.
        * **false** — a decided no. A real border, so it is visibly *stated* rather than
          merely absent, which is the pristine-versus-explicit distinction (§3.2.1) showing
          up in the one place a user can see it.
        * **true** — filled and checked, the only thing in the column that should catch
          the eye when you are scanning for what is flagged.
        """
        self.initStyleOption(option, index)
        QApplication.style().drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter)
        paint_ownership_bar(painter, option, index)
        paint_row_rule(painter, option)
        paint_column_rule(painter, option)

        value = self._get_value(index)
        box = self._checkbox_rect(option)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        if value:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(style.ACCENT_EDGE))
            painter.drawRoundedRect(box, 3, 3)
            tick = QPen(QColor("#ffffff"), 1.8)
            tick.setCapStyle(Qt.RoundCap)
            tick.setJoinStyle(Qt.RoundJoin)
            painter.setPen(tick)
            painter.drawPolyline(QPolygonF([
                QPointF(box.left() + box.width() * 0.26, box.top() + box.height() * 0.52),
                QPointF(box.left() + box.width() * 0.44, box.top() + box.height() * 0.70),
                QPointF(box.left() + box.width() * 0.75, box.top() + box.height() * 0.31),
            ]))
        else:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#5a5a5a" if value is False else "#3a3a3a"), 1.2))
            painter.drawRoundedRect(box.adjusted(0, 0, -1, -1), 3, 3)

        painter.restore()

    def editorEvent(self, event, model, option, index: QModelIndex) -> bool:
        # NOTE: with the per-column arming mode gone, a single click on the checkbox now
        # toggles the cell directly. That is the one place the mode was genuinely earning
        # something — the other three types only open on double-click or F2 — so this is
        # deliberately left as-is pending a decision, not overlooked.
        if event.type() == QEvent.MouseButtonRelease:
            if self._checkbox_rect(option).contains(event.pos()):
                return self._toggle(model, index)

        return False

    def _toggle(self, model, index: QModelIndex) -> bool:
        """Click toggles True <-> False. If unset, first click sets True."""
        current = self._get_value(index)
        if current is None or current is False:
            new_value = True
        else:
            new_value = False
        model.setData(index, new_value, Qt.EditRole)
        return True

    def createEditor(self, parent, option, index):
        return None

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        hint.setHeight(max(hint.height(), 24))
        return hint
