from PySide6.QtCore import Qt, QAbstractTableModel

from packsmith.gui.table.edit_commands import EditStack, TagEditCommand
from packsmith.core.query import evaluate, Tag


# Custom model role carrying a tag cell's ownership (None = pristine, no row).
# Design 3.2.1: ownership is a separate axis from value — the view needs it to tell
# pristine-with-default apart from an owned value, and to tint user vs. action.
OwnershipRole = Qt.UserRole + 1


class RegistryTableModel(QAbstractTableModel):
    """A table model driven by a **query** (design 3.2.4): a View *is* a query, rendered.

    It materializes ``evaluate(query)`` and renders that Result. Columns come from
    ``query.select`` (aligned 1:1 with the Result's columns); rows from the Result. A
    column is editable iff its select field is a ``Tag`` **and** the row is entry-backed
    (computed/distinct rows carry no entry, so they're read-only). Edits route back through
    (registry_type, entry_id, tag_name) — the entry_id the engine attached to each row is
    exactly what makes write-back possible.

    Re-evaluation is deliberately NOT done on every in-table edit (that live filter-churn
    is a later feature). A single edit patches its own cell; ``reevaluate()`` (a full reset)
    is called after a discrete external change such as an action run.
    """

    def __init__(self, query, packdump, tag_store):
        super().__init__()
        self._query = query
        self._packdump = packdump
        self._tag_store = tag_store
        self._registry_type = query.scope.type
        self._select = list(query.select)
        self._edit_stack = EditStack(tag_store)
        self._editing_columns: set[int] = set()
        self._result = None
        self._evaluate()

    # --- data source -------------------------------------------------------

    def _evaluate(self):
        self._result = evaluate(self._query, packdump=self._packdump, tag_store=self._tag_store)

    def reevaluate(self):
        """Re-run the query and refresh, membership included. For external changes
        (e.g. an action run), not for in-table edits."""
        self.beginResetModel()
        self._evaluate()
        self.endResetModel()

    # --- column helpers ----------------------------------------------------

    def _column_name(self, col: int) -> str:
        return self._result.columns[col].name

    def is_tag_column(self, col: int) -> bool:
        return isinstance(self._select[col], Tag)

    def column_tag_name(self, col: int):
        """The tag name for a Tag column, else None (intrinsic/attribute columns)."""
        f = self._select[col]
        return f.name if isinstance(f, Tag) else None

    def tag_type_for_column(self, col: int):
        """The type that selects a cell delegate: the tag's type for Tag columns,
        None for id/mod/attribute columns (rendered read-only as plain text)."""
        if not self.is_tag_column(col):
            return None
        return self._result.columns[col].type

    def tag_definition_for_column(self, col: int):
        name = self.column_tag_name(col)
        if name is None:
            return None
        return self._tag_store.definition(self._registry_type, name)

    def entry_at_row(self, row: int):
        if 0 <= row < len(self._result.rows):
            return self._result.rows[row].entry_id
        return None

    # --- Qt model interface ------------------------------------------------

    def rowCount(self, parent=None):
        return len(self._result.rows)

    def columnCount(self, parent=None):
        return len(self._result.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self._result.columns[section].name
        return str(section + 1)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        col = index.column()
        row = self._result.rows[index.row()]

        # Ownership channel: who owns this tag cell (None = pristine / not a tag / computed row).
        if role == OwnershipRole:
            name = self.column_tag_name(col)
            if name is None or row.entry_id is None:
                return None
            return self._tag_store.get_ownership(self._registry_type, row.entry_id, name)

        if role != Qt.DisplayRole:
            return None
        value = row.values.get(self._column_name(col))
        return "" if value is None else str(value)

    # --- editing -----------------------------------------------------------

    def is_editing(self, col: int) -> bool:
        return col in self._editing_columns

    def set_editing(self, col: int, enabled: bool):
        if enabled:
            self._editing_columns.add(col)
        else:
            self._editing_columns.discard(col)

    def flags(self, index):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        col = index.column()
        row = self._result.rows[index.row()]
        if col in self._editing_columns and self.is_tag_column(col) and row.entry_id is not None:
            base |= Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole:
            return False
        col = index.column()
        if col not in self._editing_columns or not self.is_tag_column(col):
            return False
        row = self._result.rows[index.row()]
        if row.entry_id is None:   # computed/distinct row — nothing to write back to
            return False

        tag_name = self.column_tag_name(col)
        old_value = self._tag_store.get_tag(self._registry_type, row.entry_id, tag_name)
        if old_value == value:
            return False

        command = TagEditCommand(
            registry_type=self._registry_type,
            entry_id=row.entry_id,
            tag_name=tag_name,
            old_value=old_value,
            new_value=value,
        )
        self._edit_stack.execute(command)
        # Patch the rendered cell from the store (respects defaults + casting); membership
        # is intentionally left alone — no re-eval on edit.
        row.values[self._column_name(col)] = self._tag_store.get_tag(
            self._registry_type, row.entry_id, tag_name)
        self.dataChanged.emit(index, index, [Qt.DisplayRole, OwnershipRole])
        return True

    def emit_all_data_changed(self):
        """Re-sync every tag cell's value from the store (row membership unchanged) and
        signal the view. Used by bulk edits, undo/redo, and action-run refreshes."""
        self._resync_values()
        if self.rowCount() and self.columnCount():
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(self.rowCount() - 1, self.columnCount() - 1),
                [Qt.DisplayRole, OwnershipRole],
            )

    def _resync_values(self):
        for row in self._result.rows:
            if row.entry_id is None:
                continue
            for col, field in enumerate(self._select):
                if isinstance(field, Tag):
                    name = self._column_name(col)
                    row.values[name] = self._tag_store.get_tag(
                        self._registry_type, row.entry_id, field.name)

    def undo(self):
        if self._edit_stack.can_undo:
            self._edit_stack.undo()
            self.emit_all_data_changed()

    def redo(self):
        if self._edit_stack.can_redo:
            self._edit_stack.redo()
            self.emit_all_data_changed()
