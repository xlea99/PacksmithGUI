import dataclasses

from PySide6.QtCore import Qt, QAbstractTableModel
from PySide6.QtGui import QColor

from packsmith.gui.table.edit_commands import EditStack, TagEditCommand
from packsmith.core.query import evaluate, Tag, Attribute
from packsmith.gui.shell import style


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

    def __init__(self, query, packdump, tag_store, confirm_takeover=None):
        super().__init__()
        self._query = query
        self._packdump = packdump
        self._tag_store = tag_store
        # Called with [(entry_id, tag_name, action_ref), ...] before the user takes cells
        # an action manages (design 3.2.1: transfers are loud). Returns True to proceed.
        # None = no confirmation (headless use).
        self._confirm_takeover = confirm_takeover
        self._registry_type = query.scope.type
        self._edit_stack = EditStack(tag_store)
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

    def set_packdump(self, packdump):
        """Adopt a newly imported dump and re-run (design 3.1).

        A full reset, because the registry underneath decides the *rows*: entries a mod
        update added or removed change row count and order, so patching cells would leave
        the table describing a registry that no longer exists.

        The edit stack is deliberately kept. Its commands address cells by
        (registry_type, entry_id, tag_name), which survives a registry change — an undo
        onto an entry the new dump dropped writes an orphan, and orphans are a state the
        Errors panel already reports.
        """
        self._packdump = packdump
        self.reevaluate()

    def set_filter(self, filter_node):
        """Replace the query's filter and re-evaluate. The view's scope + columns are
        unchanged, so column layout / delegates / edit toggles stay valid. This is the
        constructor's write path — a deliberate query edit, not an in-table cell edit."""
        self._query = dataclasses.replace(self._query, filter=filter_node)
        self.reevaluate()

    # --- column helpers ----------------------------------------------------

    @property
    def _select(self) -> list:
        """The fields behind the columns, taken from the **evaluated result**.

        Not from `query.select`: a wildcard like `AllAttributes` expands during evaluation,
        so the query says two fields while the table has three columns, and every
        column-kind lookup indexes off the end of the list. Reading it from the result also
        keeps it right after `reevaluate()` — adopting a dump that carries a new attribute
        changes the column count, and a list captured in `__init__` would not have moved.
        """
        return self._result.select

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

    # --- sorting -----------------------------------------------------------
    #
    # Done here, with a key, rather than in a QSortFilterProxyModel with `lessThan`.
    # Measured on a real pack's 18,638 items: the proxy called `lessThan` 201,292 times
    # and `data()` 402,584 times to sort one column, taking **2.2 seconds** — and the view
    # sorted twice on open (setSortingEnabled sorts, then sortByColumn sorts again), so
    # every big view cost 4.5s before it appeared. A key function computes one value per
    # row and lets C do the comparing: **3-8ms** for the same column.
    #
    # Pairwise comparison in Python is the whole problem. No amount of making `lessThan`
    # cheaper fixes an O(n log n) count of Python calls; the count itself has to go.

    def sort(self, column: int, order=Qt.AscendingOrder):
        if not self._result.rows or not (0 <= column < self.columnCount()):
            return
        key = self._sort_key_for(column)

        # Empty cells sink to the bottom in BOTH directions, so they are partitioned out
        # rather than folded into the key — `reverse=True` would float them to the top,
        # and "the blanks are wherever the arrow points" is not a useful sort.
        blank, filled = [], []
        for row in self._result.rows:
            (blank if self._is_blank(row, column) else filled).append(row)
        filled.sort(key=key, reverse=(order == Qt.DescendingOrder))

        self.beginResetModel()
        self._result.rows = filled + blank
        self.endResetModel()

    def _sorted_value(self, row, col: int):
        """What a cell sorts by: its value, with the same id fallback the cell displays.

        Reading the displayed value matters — an unlocalized entry renders its raw id
        (§3.1), and sorting the underlying None instead would drop every one of them to
        the bottom of a column where they visibly hold an id.
        """
        value = row.values.get(self._column_name(col))
        if value is None and self._is_localization_fallback(row, col):
            return row.entry_id
        return value

    def _is_blank(self, row, col: int) -> bool:
        value = self._sorted_value(row, col)
        return value is None or value == ""

    def _sort_key_for(self, col: int):
        """A key function for one column's type. Every branch returns the SAME shape of
        tuple, so a column holding an odd value alongside its normal ones sorts oddly
        rather than raising mid-sort."""
        col_type = self.tag_type_for_column(col)

        if col_type == "number":
            def key(row):
                try:
                    return (0, float(self._sorted_value(row, col)), "")
                except (TypeError, ValueError):
                    return (1, 0.0, str(self._sorted_value(row, col)).lower())
            return key

        if col_type == "bool":
            # False before True, which is the useful direction: "not yet decided" first.
            return lambda row: (0, float(bool(self._sorted_value(row, col))), "")

        if col_type == "enum":
            # Declaration order is semantic — `tier` means early < mid < late < end, which
            # alphabetical mangles into early/end/late/mid. Values outside the definition
            # (an orphaned assignment) sort after the known ones rather than crashing.
            defn = self.tag_definition_for_column(col)
            values = defn.get("values", []) if defn else []
            order = {v: i for i, v in enumerate(values)}

            def key(row):
                value = self._sorted_value(row, col)
                if value in order:
                    return (0, float(order[value]), "")
                return (1, 0.0, str(value).lower())
            return key

        return lambda row: (0, 0.0, str(self._sorted_value(row, col)).lower())

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

        if role == Qt.ForegroundRole:
            # The fallback is real content but not the *entry's own* name, so it reads
            # muted — you can tell at a glance which rows a mod never localized.
            return (QColor(style.TEXT_FAINT)
                    if self._is_localization_fallback(row, col) else None)

        if role != Qt.DisplayRole:
            return None
        value = row.values.get(self._column_name(col))
        if value is None and self._is_localization_fallback(row, col):
            # §3.1: "If a display name isn't available in the current locale, the raw
            # registry ID is shown." packdump.attribute() returns None and delegates this
            # `or entry_id` to its callers; no caller did it, so entries outside
            # item/block rendered as blank cells and sorted as null.
            #
            # Applied HERE and not in the resolver on purpose: inflating the value would
            # make `HAS a:localization` true for every entry, which is exactly the bug
            # Q-3 was. Display is display; existence is existence.
            return row.entry_id
        if isinstance(value, bool):
            # §3.2.1 spells bools `true` / `false`. `str(True)` is Python's spelling, and
            # DisplayRole is what Copy puts on the clipboard — so external TSV consumers
            # were getting capitalised values that don't match the language or the store.
            return "true" if value else "false"
        return "" if value is None else str(value)

    def _is_localization_fallback(self, row, col: int) -> bool:
        """Is this cell a localization column with nothing behind it?"""
        field = self._select[col]
        return (isinstance(field, Attribute) and field.name == "localization"
                and row.entry_id is not None
                and row.values.get(self._column_name(col)) is None)

    # --- editing -----------------------------------------------------------
    #
    # A tag cell is editable when it is a tag cell on a row that names an entry. There used
    # to be a third condition: a per-column "Edit:" toggle above the table that armed a
    # column before its cells would accept anything.
    #
    # That arming mode predated both systems that now do the job properly — §3.2.1's
    # ownership and conflict policy for the collision that matters, and the undo stack for
    # the misclick that doesn't. It also taxed all four column types to guard a risk that
    # only existed in one: string, number and enum cells open on double-click or F2 anyway,
    # so the mode was protecting them from a gesture they never accepted.

    def flags(self, index):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        col = index.column()
        row = self._result.rows[index.row()]
        if self.is_tag_column(col) and row.entry_id is not None:
            base |= Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole:
            return False
        col = index.column()
        if not self.is_tag_column(col):
            return False
        row = self._result.rows[index.row()]
        if row.entry_id is None:   # computed/distinct row — nothing to write back to
            return False

        tag_name = self.column_tag_name(col)
        # The cell's whole prior STATE, not its displayed value: a pristine cell shows the
        # tag's default, and re-writing that on undo would invent an assignment.
        prior = self._tag_store.assignment(self._registry_type, row.entry_id, tag_name)
        if prior is not None and prior.value == value:
            return False
        if not self.confirm_takeover_of([(row.entry_id, tag_name)]):
            return False

        command = TagEditCommand(
            registry_type=self._registry_type,
            entry_id=row.entry_id,
            tag_name=tag_name,
            prior=prior,
            new_value=value,
        )
        self._edit_stack.execute(command)
        # Patch the rendered cell from the store (respects defaults + casting); membership
        # is intentionally left alone — no re-eval on edit.
        row.values[self._column_name(col)] = self._tag_store.get_tag(
            self._registry_type, row.entry_id, tag_name)
        self.dataChanged.emit(index, index, [Qt.DisplayRole, OwnershipRole])
        return True

    def confirm_takeover_of(self, cells) -> bool:
        """Gate an edit that would take cells away from an action (design 3.2.1).

        ``cells`` is a list of ``(entry_id, tag_name)``. Cells that are pristine or already
        the user's need no confirmation; only action-owned ones do, and they're confirmed
        **once for the whole batch** — a dialog per cell during a bulk edit would be
        unusable. Returns True when the edit may proceed.
        """
        if self._confirm_takeover is None:
            return True
        owned = []
        for entry_id, tag_name in cells:
            ownership = self._tag_store.get_ownership(self._registry_type, entry_id, tag_name)
            if ownership and ownership["kind"] == "action":
                owned.append((entry_id, tag_name, ownership["action_ref"]))
        return True if not owned else bool(self._confirm_takeover(owned))

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
        """Re-read every tag cell from the store, a **column at a time**.

        Rows were the outer loop and `get_tag` the inner one — a query per cell, so a
        single Delete on one cell re-read the entire table one row at a time: measured at
        800ms for two tag columns over 18,638 entries, and it runs on every bulk edit, undo
        and redo. Columns outside means one query per tag instead, and the default is
        applied here because the map holds assigned cells only.
        """
        for col, field in enumerate(self._select):
            if not isinstance(field, Tag):
                continue
            name = self._column_name(col)
            values = self._tag_store.column(self._registry_type, field.name)
            default = self._tag_store.default_for(self._registry_type, field.name)
            for row in self._result.rows:
                if row.entry_id is not None:
                    row.values[name] = values.get(row.entry_id, default)

    def undo(self):
        """Undo one edit. Raises `UndoBlocked` when the store refuses, with the stack and
        the store both untouched — so the caller can say why and the edit stays undoable
        once the cause is fixed."""
        if self._edit_stack.can_undo:
            self._edit_stack.undo()
            self.emit_all_data_changed()

    def redo(self):
        if self._edit_stack.can_redo:
            self._edit_stack.redo()
            self.emit_all_data_changed()
