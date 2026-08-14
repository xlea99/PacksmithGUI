from PySide6.QtWidgets import QTableView, QApplication, QMenu
from PySide6.QtCore import Qt, QMimeData

from packsmith.gui.table.edit_commands import TagEditCommand, BatchEditCommand


class RegistryTableView(QTableView):
    """Table view with keyboard-driven bulk editing for tag columns.

    Space: Gmail-style bool toggle on selected bool cells.
    Delete/Backspace: Clear selected tag cells to unset.
    Ctrl+C: Copy selected cells as TSV (plain text) + HTML table (rich paste).
    Right-click: the same operations, for people who don't know the keys yet.

    Tag cells are editable because they are tag cells; L1 columns never are."""

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
        self.menu_for(index).exec(self.viewport().mapToGlobal(pos))

    def menu_for(self, index) -> QMenu:
        """The context menu for the cell that was clicked.

        **The menu follows the column you clicked, not the selection.** An L1 column — id,
        mod, an attribute — is read-only for the life of the app (§3.1), so "Clear
        assignment" is not a thing you could ever be offered there. Showing it greyed out
        is not neutral either: a disabled destructive-sounding verb invites you to work out
        what you did wrong, when the answer is that the column simply has no such concept.

        So those get the short menu. Copy still earns its place — pulling a column of ids
        out to somewhere else is most of what anybody does with them.

        Split from the handler so it can be read without an `exec` blocking on a native
        popup; the tab bar's ✕ menu is built the same way, for the same reason.
        """
        menu = QMenu(self)
        if self.model().tag_type_for_column(index.column()) is not None:
            count, defaulted = self._clearable()
            # Offered only when there is something to clear. A pristine cell has no
            # assignment to remove — `_bulk_clear` already skips it — so a greyed-out
            # "Clear" was the menu describing a state the code had already handled.
            if count:
                clear = menu.addAction(self._clear_label(count, defaulted))
                clear.triggered.connect(self._bulk_clear)
        copy = menu.addAction("Copy")
        copy.triggered.connect(self._copy_selection)
        return menu

    @staticmethod
    def _clear_label(count: int, defaulted: bool) -> str:
        """What clearing looks like from the user's side.

        On a tag with a **default**, removing the assignment doesn't empty the cell — it
        goes back to showing the default, so "Clear" describes a blank that never appears.

        Deliberately *"Reset to default"* rather than *"Set to default"*: setting is what
        the user does by typing the default value in, which writes a row they own and is a
        genuinely different state from having never decided (§3.2.1 — "the default is NOT
        written to the database"). "Reset" is the form-field sense, undoing a decision
        rather than making one, which is exactly what this does.
        """
        if defaulted:
            return "Reset to default" if count == 1 else f"Reset {count} cells to default"
        return "Clear assignment" if count == 1 else f"Clear {count} assignments"

    def _clearable(self):
        """(how many selected cells actually hold an assignment, do they all have defaults).

        Existence, not value: a pristine cell on a defaulted tag reads back *as* the
        default, so asking the value can never tell the two apart.

        Counted a column at a time rather than a cell at a time — one query per tag
        involved instead of one per selected cell, which matters because "select the whole
        column and right-click" is a completely ordinary thing to do.
        """
        source = self.model()
        by_tag = {}
        for idx in self.selectionModel().selectedIndexes():
            tag_name = source.column_tag_name(idx.column())
            entry_id = source.entry_at_row(idx.row())
            if tag_name is None or entry_id is None:
                continue
            by_tag.setdefault(tag_name, set()).add(entry_id)

        count, tags_hit = 0, []
        for tag_name, entries in by_tag.items():
            assigned = source._tag_store.column(source._registry_type, tag_name)
            hits = entries & set(assigned)
            if hits:
                count += len(hits)
                tags_hit.append(tag_name)

        defaulted = bool(tags_hit) and all(
            (source._tag_store.definition(source._registry_type, name) or {})
            .get("default_value") is not None
            for name in tags_hit)
        return count, defaulted

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

    def _priors_for(self, source, tag_names):
        """`{tag_name: {entry_id: Assignment}}`, one query per tag rather than per cell.

        Every bulk operation needs each cell's prior state to build an undoable command,
        and each was asking the store cell by cell — a query per cell for an operation
        whose entire purpose is doing many at once. Selecting a whole column and pressing
        Delete is an ordinary thing to do, and that was 18,638 queries on a real pack.
        """
        return {name: source._tag_store.assignments(source._registry_type, name)
                for name in tag_names}

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

        source = self.model()
        indexes = self.selectionModel().selectedIndexes()
        if not indexes:
            return False

        priors = self._priors_for(source, {
            source.column_tag_name(i.column()) for i in indexes
            if source.column_tag_name(i.column()) is not None})

        edits = []
        for idx in indexes:
            src_col = idx.column()
            src_row = idx.row()

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
            prior = priors[tag_name].get(entry_id)

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
        source = self.model()
        indexes = self.selectionModel().selectedIndexes()

        bool_indexes = []
        for idx in indexes:
            src_col = idx.column()
            if source.tag_type_for_column(src_col) == "bool":
                bool_indexes.append(idx)

        if not bool_indexes:
            return False

        # Gmail rule: if all are True, set all to False. Otherwise set all to True.
        values = [self._get_bool_value(idx) for idx in bool_indexes]
        all_true = all(v is True for v in values)
        new_value = False if all_true else True

        priors = self._priors_for(source, {
            source.column_tag_name(i.column()) for i in bool_indexes
            if source.column_tag_name(i.column()) is not None})

        edits = []
        for idx in bool_indexes:
            src_row = idx.row()
            src_col = idx.column()
            entry_id = source.entry_at_row(src_row)
            tag_name = source.column_tag_name(src_col)
            if entry_id is None or tag_name is None:
                continue
            prior = priors[tag_name].get(entry_id)

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
        source = self.model()
        indexes = self.selectionModel().selectedIndexes()

        priors = self._priors_for(source, {
            source.column_tag_name(i.column()) for i in indexes
            if source.column_tag_name(i.column()) is not None})

        edits = []
        for idx in indexes:
            src_col = idx.column()
            if source.tag_type_for_column(src_col) is None:
                continue  # fixed column

            src_row = idx.row()
            entry_id = source.entry_at_row(src_row)
            tag_name = source.column_tag_name(src_col)
            if entry_id is None or tag_name is None:
                continue
            # Existence, not value: a pristine cell on a defaulted tag reads back as the
            # default, so `is None` never fired and clearing it pushed a no-op edit whose
            # undo materialised the very assignment the clear was meant to avoid.
            prior = priors[tag_name].get(entry_id)

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
