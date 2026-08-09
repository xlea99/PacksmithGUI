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
        return [("is", "eq"), ("is not", "neq")]
    if field_type == "number":
        return [("=", "eq"), ("≠", "neq"), (">", "gt"), ("<", "lt"), ("≥", "gte"), ("≤", "lte")]
    return [("=", "eq"), ("≠", "neq"), ("contains", "contains"), ("matches", "matches")]


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
            if f.type == "bool":
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
                 view_store=None, parent=None):
        super().__init__(parent)
        self._new_view = new_view
        self._registry_type = registry_type
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
        combiner, nodes = _decompose(query.filter if query else None)
        self._combiner.setCurrentIndex(0 if combiner == "AND" else 1)
        for n in nodes:
            self._add_row(preset=n)

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
