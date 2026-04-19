from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QStyleOptionButton, QApplication
from PySide6.QtCore import Qt, QRect, QModelIndex, QEvent, QSortFilterProxyModel
from PySide6.QtGui import QPainter


class BoolCellDelegate(QStyledItemDelegate):
    """Checkbox delegate for bool tag columns.

    Click toggles between True and False (only when edit mode is on).
    Delete/Backspace clears to unset (handled by the view, only for tags without defaults).

    Rendering is driven entirely by the model's data() — which already returns
    default values for unset tags via get_tag(). No default-awareness needed here.

    When edit mode is off, checkboxes render but don't respond to input."""

    def _get_value(self, index: QModelIndex):
        raw = index.data(Qt.DisplayRole)
        if raw == "" or raw is None:
            return None
        if isinstance(raw, str):
            return raw.lower() == "true"
        return bool(raw)

    def _source_model(self, model):
        """Unwrap proxy to get the RegistryTableModel."""
        if isinstance(model, QSortFilterProxyModel):
            return model.sourceModel()
        return model

    def _is_editing(self, model, index: QModelIndex) -> bool:
        source = self._source_model(model)
        if isinstance(model, QSortFilterProxyModel):
            col = model.mapToSource(index).column()
        else:
            col = index.column()
        return source.is_editing(col)

    def _checkbox_rect(self, option) -> QRect:
        """Center a checkbox-sized rect within the cell."""
        check_size = QApplication.style().pixelMetric(QStyle.PM_IndicatorWidth)
        x = option.rect.x() + (option.rect.width() - check_size) // 2
        y = option.rect.y() + (option.rect.height() - check_size) // 2
        return QRect(x, y, check_size, check_size)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        self.initStyleOption(option, index)
        style = QApplication.style()

        # Draw cell background (selection, alternating rows)
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter)

        value = self._get_value(index)
        editing = self._is_editing(index.model(), index)

        checkbox_opt = QStyleOptionButton()
        checkbox_opt.rect = self._checkbox_rect(option)

        if value is None:
            checkbox_opt.state = QStyle.State_Enabled | QStyle.State_NoChange
            painter.setOpacity(0.2 if editing else 0.1)
        elif value:
            checkbox_opt.state = QStyle.State_Enabled | QStyle.State_On
            painter.setOpacity(1.0 if editing else 0.5)
        else:
            checkbox_opt.state = QStyle.State_Enabled | QStyle.State_Off
            painter.setOpacity(1.0 if editing else 0.5)

        style.drawControl(QStyle.CE_CheckBox, checkbox_opt, painter)
        painter.setOpacity(1.0)

    def editorEvent(self, event, model, option, index: QModelIndex) -> bool:
        if not self._is_editing(model, index):
            return False

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
