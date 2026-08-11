from PySide6.QtWidgets import QTableView, QApplication, QMenu
from PySide6.QtCore import Qt, QSortFilterProxyModel, QMimeData

from packsmith.gui.table.edit_commands import TagEditCommand, BatchEditCommand


class RegistryTableView(QTableView):
    """Table view with keyboard-driven bulk editing for tag columns.

    Space: Gmail-style bool toggle on selected bool cells.
    Delete/Backspace: Clear selected tag cells to unset.
    Ctrl+C: Copy selected cells as TSV (plain text) + HTML table (rich paste).
    Right-click: the same operations, for people who don't know the keys yet.
    All keyboard editing respects per-column edit mode."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # §3.2.1 names a context menu for clearing an assignment. Keyboard-only meant the
        # operation existed but was undiscoverable — Delete is obvious once you know it,
        # and invisible until then.
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def _on_context_menu(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return
        source = self._source_model()
        editable = [i for i in self.selectionModel().selectedIndexes()
                    if source.is_editing(self._source_col(i))
                    and source.tag_type_for_column(self._source_col(i)) is not None]
        menu = QMenu(self)
        clear = menu.addAction(f"Clear {len(editable)} assignment(s)"
                               if len(editable) != 1 else "Clear assignment")
        clear.setEnabled(bool(editable))
        clear.triggered.connect(self._bulk_clear)
        copy = menu.addAction("Copy")
        copy.triggered.connect(self._copy_selection)
        if not editable:
            menu.addSeparator()
            hint = menu.addAction("Turn on editing for this column to change values")
            hint.setEnabled(False)
        menu.exec(self.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event):
        key = event.key()

        if key in (Qt.Key_Space,):
            if self._bulk_toggle_bools():
                return

        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            if self._bulk_clear():
                return

        if key == Qt.Key_C and event.modifiers() & Qt.ControlModifier:
            self._copy_selection()
            return

        if key == Qt.Key_V and event.modifiers() & Qt.ControlModifier:
            if self._paste_fill():
                return

        if key in (Qt.Key_Return, Qt.Key_Enter):
            current = self.currentIndex()
            if current.isValid():
                # Let Qt commit any open editor first
                super().keyPressEvent(event)
                # Move down one row, same column
                below = self.model().index(current.row() + 1, current.column())
                if below.isValid():
                    self.setCurrentIndex(below)
                return

        super().keyPressEvent(event)

    def _source_model(self):
        model = self.model()
        if isinstance(model, QSortFilterProxyModel):
            return model.sourceModel()
        return model

    def _source_col(self, proxy_index):
        model = self.model()
        if isinstance(model, QSortFilterProxyModel):
            return model.mapToSource(proxy_index).column()
        return proxy_index.column()

    def _source_row(self, proxy_index):
        model = self.model()
        if isinstance(model, QSortFilterProxyModel):
            return model.mapToSource(proxy_index).row()
        return proxy_index.row()

    def _copy_selection(self):
        """Copy selected cells to clipboard as TSV + HTML table."""
        indexes = self.selectionModel().selectedIndexes()
        if not indexes:
            return

        # Build a grid from the bounding rectangle of the selection
        selected = set()
        min_row = min(idx.row() for idx in indexes)
        max_row = max(idx.row() for idx in indexes)
        min_col = min(idx.column() for idx in indexes)
        max_col = max(idx.column() for idx in indexes)

        for idx in indexes:
            selected.add((idx.row(), idx.column()))

        model = self.model()
        rows_tsv = []
        rows_html = []

        for r in range(min_row, max_row + 1):
            cells_tsv = []
            cells_html = []
            for c in range(min_col, max_col + 1):
                if (r, c) in selected:
                    idx = model.index(r, c)
                    value = idx.data(Qt.DisplayRole) or ""
                else:
                    value = ""
                cells_tsv.append(str(value))
                cells_html.append(f"<td>{_html_escape(str(value))}</td>")
            rows_tsv.append("\t".join(cells_tsv))
            rows_html.append("<tr>" + "".join(cells_html) + "</tr>")

        tsv = "\n".join(rows_tsv)
        html = "<table>" + "".join(rows_html) + "</table>"

        mime = QMimeData()
        mime.setText(tsv)
        mime.setHtml(html)
        QApplication.clipboard().setMimeData(mime)

    def _paste_fill(self) -> bool:
        """Ctrl+V: if clipboard is a single value, fill all compatible selected cells."""
        text = QApplication.clipboard().text().strip()
        if not text or "\t" in text or "\n" in text:
            return False  # multi-cell paste, not supported yet

        source = self._source_model()
        indexes = self.selectionModel().selectedIndexes()
        if not indexes:
            return False

        edits = []
        for idx in indexes:
            src_col = self._source_col(idx)
            src_row = self._source_row(idx)

            if not source.is_editing(src_col):
                continue

            tag_type = source.tag_type_for_column(src_col)
            if tag_type is None:
                continue  # fixed column

            # Validate and convert the pasted value for this column's type
            converted = self._convert_paste_value(text, tag_type, source, src_col)
            if converted is _INVALID:
                continue  # value doesn't fit this column, skip silently

            entry_id = source.entry_at_row(src_row)
            tag_name = source.column_tag_name(src_col)
            if entry_id is None or tag_name is None:
                continue
            prior = source._tag_store.assignment(source._registry_type, entry_id, tag_name)

            if prior is not None and prior.value == converted:
                continue

            edits.append(TagEditCommand(
                registry_type=source._registry_type,
                entry_id=entry_id,
                tag_name=tag_name,
                prior=prior,
                new_value=converted,
            ))

        if not edits:
            return True  # consumed the event even if nothing changed

        if not source.confirm_takeover_of([(e.entry_id, e.tag_name) for e in edits]):
            return True
        batch = BatchEditCommand(label=f"Paste '{text}' into {len(edits)} cells", edits=edits)
        source._edit_stack.execute(batch)
        source.emit_all_data_changed()
        return True

    @staticmethod
    def _convert_paste_value(text: str, tag_type: str, source, col: int):
        """Convert pasted text to the correct Python type for a tag column.
        Returns _INVALID if the value doesn't fit the column's type/constraints."""
        if tag_type == "bool":
            if text.lower() == "true":
                return True
            if text.lower() == "false":
                return False
            return _INVALID

        if tag_type == "number":
            try:
                return int(text)
            except ValueError:
                try:
                    return float(text)
                except ValueError:
                    return _INVALID

        if tag_type == "enum":
            defn = source.tag_definition_for_column(col)
            if defn and text in defn.get("values", []):
                return text
            return _INVALID

        if tag_type == "string":
            return text

        return _INVALID

    def _bulk_toggle_bools(self) -> bool:
        """Space: set all selected bool cells to True, unless all are already True."""
        source = self._source_model()
        indexes = self.selectionModel().selectedIndexes()

        # Filter to only bool columns that are in edit mode
        bool_indexes = []
        for idx in indexes:
            src_col = self._source_col(idx)
            if source.tag_type_for_column(src_col) == "bool" and source.is_editing(src_col):
                bool_indexes.append(idx)

        if not bool_indexes:
            return False

        # Gmail rule: if all are True, set all to False. Otherwise set all to True.
        values = [self._get_bool_value(idx) for idx in bool_indexes]
        all_true = all(v is True for v in values)
        new_value = False if all_true else True

        # Build batch command
        edits = []
        for idx in bool_indexes:
            src_row = self._source_row(idx)
            src_col = self._source_col(idx)
            entry_id = source.entry_at_row(src_row)
            tag_name = source.column_tag_name(src_col)
            if entry_id is None or tag_name is None:
                continue
            prior = source._tag_store.assignment(source._registry_type, entry_id, tag_name)

            if prior is not None and prior.value == new_value:
                continue

            edits.append(TagEditCommand(
                registry_type=source._registry_type,
                entry_id=entry_id,
                tag_name=tag_name,
                prior=prior,
                new_value=new_value,
            ))

        if not edits:
            return True

        if not source.confirm_takeover_of([(e.entry_id, e.tag_name) for e in edits]):
            return True
        batch = BatchEditCommand(label=f"Toggle {len(edits)} bools", edits=edits)
        source._edit_stack.execute(batch)
        source.emit_all_data_changed()
        return True

    def _bulk_clear(self) -> bool:
        """Delete/Backspace: clear all selected tag cells to unset."""
        source = self._source_model()
        indexes = self.selectionModel().selectedIndexes()

        edits = []
        for idx in indexes:
            src_col = self._source_col(idx)
            if not source.is_editing(src_col):
                continue
            if source.tag_type_for_column(src_col) is None:
                continue  # fixed column

            src_row = self._source_row(idx)
            entry_id = source.entry_at_row(src_row)
            tag_name = source.column_tag_name(src_col)
            if entry_id is None or tag_name is None:
                continue
            # Existence, not value: a pristine cell on a defaulted tag reads back as the
            # default, so `is None` never fired and clearing it pushed a no-op edit whose
            # undo materialised the very assignment the clear was meant to avoid.
            prior = source._tag_store.assignment(source._registry_type, entry_id, tag_name)

            if prior is None:
                continue

            edits.append(TagEditCommand(
                registry_type=source._registry_type,
                entry_id=entry_id,
                tag_name=tag_name,
                prior=prior,
                new_value=None,
            ))

        if not edits:
            return False

        if not source.confirm_takeover_of([(e.entry_id, e.tag_name) for e in edits]):
            return True
        batch = BatchEditCommand(label=f"Clear {len(edits)} tags", edits=edits)
        source._edit_stack.execute(batch)
        source.emit_all_data_changed()
        return True

    def _get_bool_value(self, index):
        raw = index.data(Qt.DisplayRole)
        if raw == "" or raw is None:
            return None
        if isinstance(raw, str):
            return raw.lower() == "true"
        return bool(raw)


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Sentinel for "this pasted value doesn't fit this column type"
_INVALID = object()
