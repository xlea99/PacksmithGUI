from PySide6.QtCore import Qt, QAbstractTableModel

from packsmith.gui.table.edit_commands import EditStack, TagEditCommand


class RegistryTableModel(QAbstractTableModel):
    """Virtual table model for displaying registry entries with tag columns.
    Uses Qt's model/view architecture so we never load all rows into widgets —
    Qt only asks for data that's actually visible on screen."""

    def __init__(self, packdump, tag_store, registry_type: str, tag_columns: list[str] = None):
        super().__init__()
        self._packdump = packdump
        self._tag_store = tag_store
        self._registry_type = registry_type
        self._tag_columns = tag_columns or []
        self._edit_stack = EditStack(tag_store)
        self._editing_columns: set[int] = set()

        # Fixed columns: ID, Display Name. Then tag columns after.
        self._fixed_columns = ["ID", "Display Name"]

        # Cache the entry list for fast indexed access
        reg = packdump.registry.get(registry_type, {})
        self._entries = reg.get("values", [])

    def rowCount(self, parent=None):
        return len(self._entries)

    def columnCount(self, parent=None):
        return len(self._fixed_columns) + len(self._tag_columns)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None

        entry_id = self._entries[index.row()]
        col = index.column()

        if col == 0:
            return entry_id
        elif col == 1:
            return self._packdump.display_name(self._registry_type, entry_id)
        else:
            tag_name = self._tag_columns[col - len(self._fixed_columns)]
            value = self._tag_store.get_tag(self._registry_type, entry_id, tag_name)
            if value is None:
                return ""
            return str(value)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            if section < len(self._fixed_columns):
                return self._fixed_columns[section]
            return self._tag_columns[section - len(self._fixed_columns)]
        return str(section + 1)

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
        if col in self._editing_columns:
            base |= Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole:
            return False
        col = index.column()
        if col not in self._editing_columns:
            return False

        entry_id = self._entries[index.row()]
        tag_name = self._tag_columns[col - len(self._fixed_columns)]
        old_value = self._tag_store.get_tag(self._registry_type, entry_id, tag_name)

        if old_value == value:
            return False

        command = TagEditCommand(
            registry_type=self._registry_type,
            entry_id=entry_id,
            tag_name=tag_name,
            old_value=old_value,
            new_value=value,
        )
        self._edit_stack.execute(command)
        self.dataChanged.emit(index, index, [Qt.DisplayRole])
        return True

    def emit_all_data_changed(self):
        """Signal that all visible data may have changed, without a full model reset."""
        if self.rowCount() and self.columnCount():
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(self.rowCount() - 1, self.columnCount() - 1),
                [Qt.DisplayRole],
            )

    def undo(self):
        if self._edit_stack.can_undo:
            self._edit_stack.undo()
            self.emit_all_data_changed()

    def redo(self):
        if self._edit_stack.can_redo:
            self._edit_stack.redo()
            self.emit_all_data_changed()

    def tag_type_for_column(self, col: int) -> str | None:
        """Returns the tag type for a given column index, or None for fixed columns."""
        if col < len(self._fixed_columns):
            return None
        tag_name = self._tag_columns[col - len(self._fixed_columns)]
        defn = self._tag_store.definition(tag_name)
        return defn["type"] if defn else None

    def tag_definition_for_column(self, col: int) -> dict | None:
        """Returns the full cached tag definition for a column, or None for fixed columns."""
        if col < len(self._fixed_columns):
            return None
        tag_name = self._tag_columns[col - len(self._fixed_columns)]
        return self._tag_store.definition(tag_name)

    def entry_at_row(self, row: int) -> str | None:
        """Returns the entry ID at a given row index."""
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None
