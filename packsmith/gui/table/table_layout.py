"""How a View is arranged, remembered per View (design 5.1).

A View is one saved configuration, and arranging it — widening a column, making the row
with the long notes tall, sorting by tier — is part of configuring it. None of that is
data, so none of it belongs in `profile.db`; it lives in per-profile UI state beside the
panel heights and the registry pins.

**Everything is keyed by name, never by position.**

* a row's height by its **entry id**, which is the same key every tag assignment uses
* a column's width by its **column name**
* the sort by its **column name**

That is what lets the arrangement survive the things that move rows and columns around:
sorting, refining the filter, adopting a new packdump, or editing the query to add a
column. Keyed by index, a tall row would stay at *row 4* while the entry it was made tall
for moved to row 900 — the height would visibly attach to the wrong thing.

Qt makes that trap easy to fall into: `QHeaderView` section sizes are positional and they
**survive a model reset**, so after a sort the tall row is simply whichever entry now sits
at that index. Re-applying by entry id after every reset is the whole job of this class.
"""
from PySide6.QtCore import QObject, Qt, Signal


class TableLayout(QObject):
    """Records and restores one table's arrangement. Emits `changed` when it needs saving."""

    changed = Signal()

    def __init__(self, table, model, default_row_height, stored=None, parent=None):
        super().__init__(parent)
        self._table = table
        self._model = model
        self._default = default_row_height
        stored = stored or {}
        self._heights = {str(k): int(v) for k, v in (stored.get("rows") or {}).items()}
        self._widths = {str(k): int(v) for k, v in (stored.get("columns") or {}).items()}
        self._sort = stored.get("sort")          # [column_name, "asc"|"desc"]
        # Which row indices we last sized, so a re-apply can put them back to default
        # without walking every row in an 18,000-row table.
        self._applied = set()
        self._muted = False

    # --- restoring ---------------------------------------------------------

    def restore(self):
        """Apply the stored arrangement. Call once, after the columns are built."""
        self._muted = True
        try:
            self._restore_columns()
            self._restore_sort()
        finally:
            self._muted = False
        self.apply_row_heights()

        header = self._table.horizontalHeader()
        header.sectionResized.connect(self._on_column_resized)
        header.sortIndicatorChanged.connect(self._on_sort_changed)
        self._table.verticalHeader().sectionResized.connect(self._on_row_resized)
        # Rows move under their heights on every reset — sorting is a reset, and so is
        # re-running the query for a filter change or a new packdump.
        self._model.modelReset.connect(self.apply_row_heights)
        self._model.layoutChanged.connect(self.apply_row_heights)

    def _restore_columns(self):
        for col in range(self._model.columnCount()):
            width = self._widths.get(self._column_name(col))
            if width:
                self._table.setColumnWidth(col, width)

    def _restore_sort(self):
        if not self._sort:
            return
        name, direction = self._sort
        for col in range(self._model.columnCount()):
            if self._column_name(col) == name:
                order = (Qt.DescendingOrder if direction == "desc" else Qt.AscendingOrder)
                self._table.sortByColumn(col, order)
                return
        # The column is gone — the query was edited. Nothing to restore, and nothing worth
        # complaining about; the view simply opens in its natural order.

    def apply_row_heights(self):
        """Put each remembered height back on the row its entry currently occupies."""
        if not self._heights and not self._applied:
            return                                  # nothing customised: touch nothing
        vh = self._table.verticalHeader()
        vh.blockSignals(True)                       # restoring is not the user resizing
        try:
            for index in self._applied:
                vh.resizeSection(index, self._default)
            self._applied = set()
            if not self._heights:
                return
            for row in range(self._model.rowCount()):
                height = self._heights.get(self._model.entry_at_row(row))
                if height:
                    vh.resizeSection(row, height)
                    self._applied.add(row)
        finally:
            vh.blockSignals(False)

    # --- recording ---------------------------------------------------------

    def _on_row_resized(self, index, _old, new):
        if self._muted:
            return
        entry_id = self._model.entry_at_row(index)
        if entry_id is None:
            return
        if new == self._default:
            # Dragged back to normal — forget it, rather than storing "28" forever and
            # pinning the row against a future change to the default.
            self._heights.pop(entry_id, None)
            self._applied.discard(index)
        else:
            self._heights[entry_id] = new
            self._applied.add(index)
        self.changed.emit()

    def _on_column_resized(self, index, _old, new):
        if self._muted:
            return
        self._widths[self._column_name(index)] = new
        self.changed.emit()

    def _on_sort_changed(self, index, order):
        if self._muted or not (0 <= index < self._model.columnCount()):
            return
        self._sort = [self._column_name(index),
                      "desc" if order == Qt.DescendingOrder else "asc"]
        self.changed.emit()

    # --- storage -----------------------------------------------------------

    def as_dict(self) -> dict:
        stored = {}
        if self._heights:
            stored["rows"] = dict(self._heights)
        if self._widths:
            stored["columns"] = dict(self._widths)
        if self._sort:
            stored["sort"] = list(self._sort)
        return stored

    def _column_name(self, col: int) -> str:
        return self._model.headerData(col, Qt.Horizontal) or str(col)
