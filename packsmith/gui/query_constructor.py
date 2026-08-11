"""The View Query constructor — the visual front-end that lowers gestures into the query
AST (design 3.2.4, §3.2.3). No text syntax, no parser: the widgets build ``Cmp``/``Has``/
``And``/``Or`` nodes directly. This is the "builder IS the documentation" surface.

First cut edits the **filter** of an existing view (scope + columns are shown read-only,
as context). It's built to grow: the same dialog will later expose scope + column picking
to author brand-new Views.
"""
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox, QLineEdit, QPushButton,
    QLabel, QCheckBox, QDialogButtonBox, QWidget, QFrame, QMessageBox,
)

from packsmith.core.query.language import (
    QuerySyntaxError, format as format_query, parse as parse_query,
)
from packsmith.core.query import (
    Query, Registry, Id, Mod, Tag, Attribute, Cmp, Has, Not, And, Or, column_name,
)


@dataclass
class FieldDesc:
    label: str
    node: object          # Id / Mod / Tag(name) / Attribute(name)
    type: str             # id | string | bool | enum | number
    enum_values: list
    can_has: bool         # presence ops (exists/missing) only make sense for looked-up fields


def registry_fields(tag_store, registry_type) -> list[FieldDesc]:
    """Every field you can filter on for a registry scope: the intrinsics, the localization
    attribute, and each registry-scoped tag."""
    fields = [
        FieldDesc("id", Id, "id", None, False),
        FieldDesc("mod", Mod, "string", None, False),
        FieldDesc("localization", Attribute("localization"), "string", None, True),
    ]
    for name, defn in tag_store.definitions_for(registry_type).items():
        fields.append(FieldDesc(name, Tag(name), defn["type"], defn.get("values"), True))
    return fields


def _ops_for(field_type: str):
    """Comparison operators offered for a field type (presence ops appended separately)."""
    if field_type == "bool":
        return [("is", "eq")]
    if field_type == "enum":
        return [("is", "eq"), ("is not", "neq"),
                ("is one of", "in"), ("is none of", "not_in")]
    if field_type == "number":
        return [("=", "eq"), ("≠", "neq"), (">", "gt"), ("<", "lt"), ("≥", "gte"), ("≤", "lte")]
    return [("=", "eq"), ("≠", "neq"), ("contains", "contains"), ("matches", "matches"),
            ("is one of", "in"), ("is none of", "not_in")]


def _as_number(text):
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _node_field(node):
    if isinstance(node, Has):
        return node.field
    if isinstance(node, Not) and isinstance(node.clause, Has):
        return node.clause.field
    if isinstance(node, Cmp):
        return node.field
    return None


def _decompose(filt):
    """Split an existing filter into (combiner, [flat nodes]) for editing. Nested groups
    beyond one level aren't represented in the v1 UI; they collapse to their top level."""
    if filt is None:
        return "AND", []
    if isinstance(filt, And):
        return "AND", list(filt.clauses)
    if isinstance(filt, Or):
        return "OR", list(filt.clauses)
    return "AND", [filt]


class ConditionRow(QWidget):
    """One filter clause: field ▾ / op ▾ / value. Builds a Cmp / Has / Not(Has) node."""

    removed = Signal(object)

    def __init__(self, fields: list[FieldDesc], parent=None):
        super().__init__(parent)
        self._fields = fields

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._field_combo = QComboBox()
        for f in fields:
            self._field_combo.addItem(f.label)
        self._op_combo = QComboBox()
        self._value_edit = QLineEdit()
        self._value_combo = QComboBox()
        remove_btn = QPushButton("×")
        remove_btn.setFixedWidth(26)

        self._field_combo.setMinimumWidth(140)
        self._op_combo.setMinimumWidth(100)
        for w in (self._field_combo, self._op_combo, self._value_edit, self._value_combo):
            lay.addWidget(w)
        lay.addWidget(remove_btn)

        self._field_combo.currentIndexChanged.connect(self._on_field_changed)
        self._op_combo.currentIndexChanged.connect(self._rebuild_value)
        remove_btn.clicked.connect(lambda: self.removed.emit(self))

        self._on_field_changed()

    def _field(self) -> FieldDesc:
        return self._fields[self._field_combo.currentIndex()]

    def _op(self):
        return self._op_combo.currentData()

    def _on_field_changed(self):
        f = self._field()
        self._op_combo.blockSignals(True)
        self._op_combo.clear()
        for label, op in _ops_for(f.type):
            self._op_combo.addItem(label, op)
        if f.can_has:
            self._op_combo.addItem("exists", "has")
            self._op_combo.addItem("missing", "not_has")
        self._op_combo.blockSignals(False)
        self._rebuild_value()

    def _rebuild_value(self):
        f = self._field()
        op = self._op()
        if op in ("in", "not_in"):
            # A list, not a value — even an enum needs several, which one combo can't hold.
            self._value_combo.hide()
            self._value_edit.show()
            self._value_edit.setPlaceholderText("comma, separated, values")
            return
        self._value_edit.setPlaceholderText("")
        if op in ("has", "not_has"):
            self._value_edit.hide()
            self._value_combo.hide()
            return
        if f.type == "bool":
            self._value_edit.hide()
            self._value_combo.show()
            self._value_combo.blockSignals(True)
            self._value_combo.clear()
            self._value_combo.addItems(["true", "false"])
            self._value_combo.blockSignals(False)
        elif f.type == "enum":
            self._value_edit.hide()
            self._value_combo.show()
            self._value_combo.blockSignals(True)
            self._value_combo.clear()
            self._value_combo.addItems(f.enum_values or [])
            self._value_combo.blockSignals(False)
        else:
            self._value_combo.hide()
            self._value_edit.show()

    def to_node(self):
        """The filter node for this row, or None if it's incomplete/invalid."""
        f = self._field()
        op = self._op()
        if op == "has":
            return Has(f.node)
        if op == "not_has":
            return Not(Has(f.node))
        if op in ("in", "not_in"):
            items = [v.strip() for v in self._value_edit.text().split(",") if v.strip()]
            if not items:
                return None
            if f.type == "number":
                items = [_as_number(v) for v in items]
            return Cmp(f.node, op, items)
        if f.type == "bool":
            return Cmp(f.node, op, self._value_combo.currentText() == "true")
        if f.type == "enum":
            v = self._value_combo.currentText()
            return Cmp(f.node, op, v) if v else None
        if f.type == "number":
            t = self._value_edit.text().strip()
            if not t:
                return None
            try:
                v = int(t)
            except ValueError:
                try:
                    v = float(t)
                except ValueError:
                    return None
            return Cmp(f.node, op, v)
        t = self._value_edit.text()
        return Cmp(f.node, op, t) if t != "" else None

    def preset(self, node):
        """Populate this row from an existing filter node."""
        field = _node_field(node)
        idx = next((i for i, f in enumerate(self._fields) if f.node == field), 0)
        self._field_combo.setCurrentIndex(idx)   # triggers op rebuild

        if isinstance(node, Has):
            op = "has"
        elif isinstance(node, Not) and isinstance(node.clause, Has):
            op = "not_has"
        elif isinstance(node, Cmp):
            op = node.op
        else:
            op = None
        if op is not None:
            oi = self._op_combo.findData(op)
            if oi >= 0:
                self._op_combo.setCurrentIndex(oi)
        self._rebuild_value()

        if isinstance(node, Cmp):
            f = self._field()
            if isinstance(node.value, (list, tuple)):
                self._value_edit.setText(", ".join(str(v) for v in node.value))
            elif f.type == "bool":
                self._value_combo.setCurrentText("true" if node.value else "false")
            elif f.type == "enum":
                self._value_combo.setCurrentText(str(node.value))
            else:
                self._value_edit.setText(str(node.value))


class QueryConstructorDialog(QDialog):
    """Construct or edit a view's query. Two modes:

    * **edit** (``query=...``): edit an existing view's FILTER; scope + columns are shown
      read-only as context. On accept, ``result_filter`` holds the new filter.
    * **new** (``new_view=True``): author a brand-new view — pick a name + columns + filter.
      On accept, ``result_query`` and ``result_name`` are set.

    Either way the widgets build AST nodes directly — no text syntax involved.
    """

    def __init__(self, tag_store, registry_type, *, query=None, new_view=False,
                 view_store=None, parent=None, registries=None):
        super().__init__(parent)
        self._new_view = new_view
        self._registry_type = registry_type
        self._tags = tag_store
        # Given a store, the dialog checks the name itself rather than closing and failing
        # afterwards — losing a query you just built would be a miserable way to learn the
        # name was taken.
        self._view_store = view_store
        self._fields = registry_fields(tag_store, registry_type)
        self._rows = []
        self._col_checks = {}    # field label -> QCheckBox (new mode only)
        self.result_filter = query.filter if query else None
        self.result_query = None
        self.result_name = ""

        self.setWindowTitle("New View" if new_view else "View Query")
        self.setMinimumWidth(600)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        # A new view has to be able to target any registry — hard-coding minecraft:item
        # made 134 of the pack's 135 registries unreachable from this wizard. Editing an
        # existing view still shows its scope read-only: changing it would invalidate every
        # column and filter already chosen.
        self._registry_combo = None
        if new_view and registries:
            scope_row = QHBoxLayout()
            scope_row.addWidget(QLabel("Registry"))
            self._registry_combo = QComboBox()
            for name in registries:
                self._registry_combo.addItem(name, name)
            index = self._registry_combo.findData(registry_type)
            self._registry_combo.setCurrentIndex(max(0, index))
            self._registry_combo.currentIndexChanged.connect(self._on_registry_changed)
            scope_row.addWidget(self._registry_combo, 1)
            root.addLayout(scope_row)
        else:
            scope_lbl = QLabel(f"Registry:  <b>{registry_type}</b>")
            scope_lbl.setTextFormat(Qt.RichText)
            root.addWidget(scope_lbl)

        if new_view:
            name_row = QHBoxLayout()
            name_row.addWidget(QLabel("Name"))
            self._name_edit = QLineEdit()
            self._name_edit.setPlaceholderText("My View")
            name_row.addWidget(self._name_edit)
            root.addLayout(name_row)

            root.addWidget(QLabel("Columns"))
            grid = QGridLayout()
            grid.setSpacing(4)
            for i, f in enumerate(self._fields):
                cb = QCheckBox(f.label)
                if f.label in ("id", "localization"):
                    cb.setChecked(True)
                if f.label == "id":
                    cb.setEnabled(False)   # id is always present
                self._col_checks[f.label] = cb
                grid.addWidget(cb, i // 4, i % 4)
            root.addLayout(grid)
        else:
            cols = ", ".join(column_name(f) for f in query.select)
            cols_lbl = QLabel(f"Columns:  {cols}")
            cols_lbl.setStyleSheet("color: #888888;")
            root.addWidget(cols_lbl)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #3a3a3a;")
        root.addWidget(line)

        # Combiner
        combiner_row = QHBoxLayout()
        combiner_row.addWidget(QLabel("Show entries that match"))
        self._combiner = QComboBox()
        self._combiner.addItem("ALL of the conditions  (AND)", "AND")
        self._combiner.addItem("ANY of the conditions  (OR)", "OR")
        combiner_row.addWidget(self._combiner)
        combiner_row.addStretch()
        root.addLayout(combiner_row)

        # Condition rows
        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(4)
        root.addLayout(self._rows_box)

        add_btn = QPushButton("+  Add condition")
        add_btn.clicked.connect(lambda: self._add_row())
        root.addWidget(add_btn, alignment=Qt.AlignLeft)

        root.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # Preload the current filter as editable rows (new views start empty)
        original = query.filter if query else None
        combiner, nodes = _decompose(original)
        self._combiner.setCurrentIndex(0 if combiner == "AND" else 1)
        for n in nodes:
            self._add_row(preset=n)

        # ...then check the rows can actually REPRODUCE it, by reading them straight back.
        # A capability list would drift from the widgets; this asks the widgets. Anything
        # the visual builder can't express — token matching from the filter bar, IN over a
        # field type that doesn't offer it, groups nested deeper than one level — used to
        # be silently rewritten into whatever the rows happened to hold, and saved on OK.
        if original is not None and self.build_filter() != original:
            self._enter_text_mode(original)

    def _enter_text_mode(self, original):
        """Show the filter as text instead of rows, when the rows would corrupt it.

        Not a dead end: this is the same language the filter bar speaks, and it round-trips
        every node the builder can't (§3.2.3 — "builder and text stay in sync"). So the
        filter stays editable, just in the form that can hold it.
        """
        self._original_filter = original
        for row in list(self._rows):
            self._remove_row(row)
        self._combiner.setEnabled(False)
        self._text_edit = QLineEdit(format_query(original))
        self._text_edit.setToolTip("The same syntax as the filter bar")
        self._text_note = QLabel(
            "This filter uses something the visual builder can't show (token matching, a "
            "list, or nested groups), so it's shown as text — edit it here and nothing is "
            "lost.")
        self._text_note.setWordWrap(True)
        self._text_note.setStyleSheet("color: #d0a050; font-size: 11px;")
        self._rows_box.addWidget(self._text_note)
        self._rows_box.addWidget(self._text_edit)

    def _add_row(self, preset=None):
        row = ConditionRow(self._fields, self)
        row.removed.connect(self._remove_row)
        self._rows.append(row)
        self._rows_box.addWidget(row)
        if preset is not None:
            row.preset(preset)
        return row

    def _remove_row(self, row):
        if row in self._rows:
            self._rows.remove(row)
            self._rows_box.removeWidget(row)
            row.deleteLater()

    def _on_registry_changed(self):
        """Switching registry invalidates the conditions, which name the old registry's
        fields. Cleared rather than silently kept pointing at fields that no longer exist.
        """
        self._registry_type = self._registry_combo.currentData()
        self._fields = registry_fields(self._tags, self._registry_type)
        for row in list(self._rows):
            self._remove_row(row)
        self._rebuild_columns()

    def _rebuild_columns(self):
        """Re-label the column checkboxes for the current registry's fields."""
        if not self._col_checks:
            return
        checks = list(self._col_checks.values())
        for i, check in enumerate(checks):
            visible = i < len(self._fields)
            check.setVisible(visible)
            if visible:
                check.setText(self._fields[i].label)
                check.setChecked(self._fields[i].label in ("id", "localization"))
        self._col_checks = {f.label: c for f, c in zip(self._fields, checks)}

    def build_filter(self):
        nodes = [n for n in (r.to_node() for r in self._rows) if n is not None]
        if not nodes:
            return None
        if len(nodes) == 1:
            return nodes[0]
        return And(nodes) if self._combiner.currentData() == "AND" else Or(nodes)

    def accept(self):
        if self._new_view:
            name = self._name_edit.text().strip() or "Untitled View"
            if self._view_store is not None and self._view_store.get_by_name(name):
                QMessageBox.warning(
                    self, "Name already used",
                    f"A view named '{name}' already exists.\n\nPick a different name — "
                    f"your query is kept.")
                self._name_edit.setFocus()
                self._name_edit.selectAll()
                return

        if getattr(self, "_text_edit", None) is not None:
            text = self._text_edit.text().strip()
            try:
                self.result_filter = parse_query(text) if text else None
            except QuerySyntaxError as e:
                QMessageBox.warning(self, "Can't read that filter", str(e))
                self._text_edit.setFocus()
                return
        else:
            self.result_filter = self.build_filter()
        if self._new_view:
            selected = [f.node for f in self._fields if self._col_checks[f.label].isChecked()]
            if not selected:
                selected = [Id]
            self.result_name = name
            self.result_query = Query(
                scope=Registry(self._registry_type),
                select=selected,
                filter=self.result_filter,
                order_by=[selected[0]],
            )
        super().accept()
