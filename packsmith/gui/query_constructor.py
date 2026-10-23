"""The View dialog — author a new View, or edit one: its name, its columns, its filter.

The filter half is the visual front-end that lowers gestures into the query AST (design
3.2.4, §3.2.3). No text syntax, no parser: the widgets build ``Cmp``/``Has``/``And``/``Or``
nodes directly. This is the "builder IS the documentation" surface, and it refuses to save
a filter it cannot reproduce (see the reproduce-check in `QueryConstructorDialog`).

The column half is what a View *shows*, and it is editable after the fact: a View is keyed
by its surrogate id and its columns are nothing but `query.select`, so changing them is an
ordinary query update — nothing is keyed on the column tuple.

**Everything offered belongs to the registry.** Tags come from that registry's own
definitions, and attributes from what the dump actually carries for it: offering
`block_class` on `minecraft:worldgen/biome` only produces a column that is empty forever,
which reads as data that failed to load rather than data that was never going to exist.
"""
import dataclasses
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QStyledItemDelegate,
    QVBoxLayout, QWidget,
)

from packsmith.core.query.language import (
    QuerySyntaxError, format as format_query, parse as parse_query,
)
from packsmith.core.query import (
    Query, Registry, Id, Mod, Tag, Attribute, AllAttributes, Cmp, Has, Not, And, Or,
    column_name,
)
from packsmith.core.packdump import Packdump
from packsmith.gui.shell import icons, style
from packsmith.gui.shell.dropdown import DropDown
from packsmith.gui.shell.picker import Choice, PickerPopup
from packsmith.gui.shell.picker_button import PickerButton


@dataclass
class FieldDesc:
    label: str
    node: object          # Id / Mod / Tag(name) / Attribute(name)
    type: str             # id | string | bool | enum | number
    enum_values: list
    can_has: bool         # presence ops (exists/missing) only make sense for looked-up fields

    @property
    def key(self) -> str:
        return field_key(self.node)

    @property
    def detail(self) -> str:
        """The picker's second line: what kind of thing this is, so choosing a column does
        not mean remembering which of your tags was the enum."""
        if isinstance(self.node, Tag):
            if self.type == "enum" and self.enum_values:
                return f"enum tag · {', '.join(self.enum_values)}"
            return f"{self.type} tag"
        if isinstance(self.node, Attribute):
            return "attribute · from the packdump"
        if self.node == Id:
            return "the entry's own id"
        if self.node == Mod:
            return "the mod that adds it"
        return ""


def field_key(node) -> str:
    """A string naming a field unambiguously — the picker's value for it.

    Not the label: a tag and an attribute may share a name (a `localization` tag is legal),
    and the label would conflate them. The prefixes are the filter bar's own (`t:`, `a:`).
    """
    if node == Id:
        return "id"
    if node == Mod:
        return "mod"
    if node == AllAttributes:
        return "a:*"
    if isinstance(node, Tag):
        return f"t:{node.name}"
    if isinstance(node, Attribute):
        return f"a:{node.name}"
    try:
        return f"?:{column_name(node)}"
    except Exception:
        return f"?:{node!r}"


def registry_fields(tag_store, registry_type, packdump=None, keep=()) -> list[FieldDesc]:
    """Every field you can filter on or show for a registry scope: the intrinsics, the
    attributes the dump carries for it, and each tag defined on it.

    Attributes are asked of `Packdump` rather than listed here, because a hardcoded
    `localization` was how the last one got missed. Given a live dump, only the attributes
    this registry actually carries are offered (`attributes_for`); without one — a dialog
    built in isolation — every known attribute is, as it always was.

    `keep` is fields that must appear regardless: an attribute an existing View already
    shows stays offered even if the current dump has nothing for it, so opening the dialog
    and pressing OK can never silently drop a column.
    """
    if registry_type is None:
        return [FieldDesc("id", Id, "id", None, False)]
    fields = [
        FieldDesc("id", Id, "id", None, False),
        FieldDesc("mod", Mod, "string", None, False),
    ]
    names = (packdump.attributes_for(registry_type) if packdump is not None
             else Packdump.attribute_names())
    kept = [n.name for n in keep if isinstance(n, Attribute)]
    names = list(names) + [n for n in kept if n not in names]
    fields += [FieldDesc(name, Attribute(name), "string", None, True) for name in names]
    for name, defn in tag_store.definitions_for(registry_type).items():
        fields.append(FieldDesc(name, Tag(name), defn["type"], defn.get("values"), True))
    return fields


def _field_choices(fields) -> list[Choice]:
    return [Choice(f.label, f.key, f.detail) for f in fields]


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


def _section(text: str) -> QLabel:
    """A small-caps section heading, in the voice of the table headers."""
    label = QLabel(text.upper())
    label.setStyleSheet(
        f"color: {style.TEXT_MUTED}; font-size: 10px; font-weight: bold; "
        f"letter-spacing: 1px; padding-top: 4px;")
    return label


class ConditionRow(QWidget):
    """One filter clause: field ▾ / op ▾ / value. Builds a Cmp / Has / Not(Has) node.

    The field is a `PickerButton`, not a combo: it lists the user's own tags, which is a
    list that only grows, and it should be searched the way every other list of the
    user's vocabulary is.
    """

    removed = Signal(object)

    def __init__(self, fields: list[FieldDesc], parent=None):
        super().__init__(parent)
        self._fields = list(fields)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._field_combo = PickerButton(_field_choices(self._fields),
                                         header="Filter on", placeholder="search fields")
        self._op_combo = DropDown()
        self._value_edit = QLineEdit()
        self._value_combo = DropDown()
        remove_btn = QPushButton()
        icons.mark(remove_btn, "close", size=10)
        remove_btn.setFixedWidth(26)
        remove_btn.setToolTip("Remove this condition")

        self._field_combo.setMinimumWidth(160)
        self._op_combo.setMinimumWidth(100)
        for w in (self._field_combo, self._op_combo, self._value_edit, self._value_combo):
            lay.addWidget(w)
        lay.addWidget(remove_btn)

        self._field_combo.changed.connect(lambda _key: self._on_field_changed())
        self._op_combo.currentIndexChanged.connect(self._rebuild_value)
        remove_btn.clicked.connect(lambda: self.removed.emit(self))

        self._on_field_changed()

    def set_fields(self, fields):
        """Swap in a refreshed field list (a tag was just defined), keeping the choice."""
        self._fields = list(fields)
        before = self._field_combo.current()
        self._field_combo.set_choices(_field_choices(self._fields))
        if self._field_combo.current() != before:
            self._on_field_changed()

    def _field(self) -> FieldDesc:
        key = self._field_combo.current()
        return next((f for f in self._fields if f.key == key), self._fields[0])

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
        """Populate this row from an existing filter node.

        A field the list does not offer leaves the first one selected — and the dialog's
        reproduce-check then sees the row disagree with the filter and falls back to text,
        which is the whole reason that check asks the widgets rather than guessing.
        """
        field = _node_field(node)
        if field is not None and self._field_combo.set_current(field_key(field)):
            self._on_field_changed()

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


# --- the column list ------------------------------------------------------------------

_KEY_ROLE = Qt.UserRole
_DETAIL_ROLE = Qt.UserRole + 1
_REMOVABLE_ROLE = Qt.UserRole + 2
_WARN_ROLE = Qt.UserRole + 3

_ROW_HEIGHT = 26
_GRIP_WIDTH = 20
_X_WIDTH = 24


class _ColumnDelegate(QStyledItemDelegate):
    """grip · name ········ what it is · ×

    Painted rather than built from item widgets, because `QListWidget` drops item widgets
    on an internal move — the × buttons would vanish from every row you dragged.
    """

    def __init__(self, view):
        super().__init__(view)
        self._view = view

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), _ROW_HEIGHT)

    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect
        selected = bool(option.state & option.state.__class__.State_Selected)
        hovered = self._view.hover_row == index.row()
        if selected:
            painter.fillRect(rect, QColor(style.ACCENT))
        elif hovered:
            painter.fillRect(rect, QColor(style.BG_HOVER))

        # The grip: six dots, drawn, so it needs no glyph from any font.
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(style.TEXT_MUTED if hovered or selected else style.TEXT_FAINT))
        cx, cy = rect.left() + 9, rect.center().y()
        for dx in (0, 4):
            for dy in (-4, 0, 4):
                painter.drawEllipse(QPoint(cx + dx, cy + dy), 1, 1)

        removable = bool(index.data(_REMOVABLE_ROLE))
        label = index.data(Qt.DisplayRole) or ""
        detail = index.data(_DETAIL_ROLE) or ""
        warn = bool(index.data(_WARN_ROLE))

        text_left = rect.left() + _GRIP_WIDTH
        right = rect.right() - _X_WIDTH
        metrics = QFontMetrics(option.font)
        painter.setFont(option.font)
        painter.setPen(QColor("#ffffff" if selected else style.TEXT))
        label_width = min(metrics.horizontalAdvance(label), (right - text_left) // 2)
        painter.drawText(QRect(text_left, rect.top(), label_width, rect.height()),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         metrics.elidedText(label, Qt.ElideRight, label_width))

        detail_rect = QRect(text_left + label_width + 12, rect.top(),
                            max(0, right - text_left - label_width - 16), rect.height())
        small = QFontMetrics(option.font)
        painter.setPen(QColor("#d0a050" if warn else
                              ("#d8e4f0" if selected else style.TEXT_FAINT)))
        painter.drawText(detail_rect, Qt.AlignRight | Qt.AlignVCenter,
                         small.elidedText(detail, Qt.ElideRight, detail_rect.width()))

        if removable:
            glyph = icons.ui("close") or "×"
            font = icons.icon_font(10) if icons.ui("close") else option.font
            painter.setFont(font)
            over_x = hovered and self._view.hover_on_x
            painter.setPen(QColor(style.TEXT if over_x else style.TEXT_FAINT))
            painter.drawText(self._view.x_rect(rect), Qt.AlignCenter, glyph)
        painter.restore()


class ColumnList(QListWidget):
    """The View's columns, in order: drag to reorder, × (or Delete) to remove.

    Holds field keys; the dialog owns what they mean. Rows marked not-removable (`id`)
    can still be moved — the id column is the row's identity and must be present, but
    where it sits is taste.
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover_row = -1
        self.hover_on_x = False
        self.setItemDelegate(_ColumnDelegate(self))
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet(f"""
            QListWidget {{ background: {style.BG_DEEP}; color: {style.TEXT};
                           border: 1px solid {style.BORDER}; outline: none; }}
        """)
        self.model().rowsMoved.connect(lambda *_: self.changed.emit())

    # --- contents ----------------------------------------------------------

    def add(self, key, label, detail="", *, removable=True, warn=False, row=None):
        item = QListWidgetItem(label)
        item.setData(_KEY_ROLE, key)
        item.setData(_DETAIL_ROLE, detail)
        item.setData(_REMOVABLE_ROLE, removable)
        item.setData(_WARN_ROLE, warn)
        item.setToolTip("\n".join(filter(None, (label, detail))))
        flags = item.flags() | Qt.ItemIsDragEnabled
        item.setFlags(flags & ~Qt.ItemIsDropEnabled)   # drop BETWEEN rows, never onto one
        if row is None:
            self.addItem(item)
        else:
            self.insertItem(row, item)
        self._fit()
        self.changed.emit()
        return item

    def keys(self) -> list:
        return [self.item(r).data(_KEY_ROLE) for r in range(self.count())]

    def remove(self, key) -> bool:
        for r in range(self.count()):
            item = self.item(r)
            if item.data(_KEY_ROLE) == key and item.data(_REMOVABLE_ROLE):
                self.takeItem(r)
                self._fit()
                self.changed.emit()
                return True
        return False

    def move(self, key, to_row: int):
        for r in range(self.count()):
            if self.item(r).data(_KEY_ROLE) == key:
                item = self.takeItem(r)
                self.insertItem(max(0, min(to_row, self.count())), item)
                self.setCurrentItem(item)
                self.changed.emit()
                return

    def set_columns(self, entries):
        """Replace the whole list: ``[(key, label, detail, removable, warn), ...]``."""
        self.clear()
        for key, label, detail, removable, warn in entries:
            self.add(key, label, detail, removable=removable, warn=warn)
        self._fit()

    def _fit(self):
        """Tall enough for every column up to ten, then it scrolls — a dialog that grows
        without bound with the column count pushes its own OK button off the screen."""
        rows = max(3, min(self.count(), 10))
        self.setFixedHeight(rows * _ROW_HEIGHT + 4)

    # --- interaction -------------------------------------------------------

    def x_rect(self, row_rect: QRect) -> QRect:
        return QRect(row_rect.right() - _X_WIDTH + 1, row_rect.top(), _X_WIDTH, row_rect.height())

    def _row_and_x(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return -1, False
        on_x = (bool(index.data(_REMOVABLE_ROLE))
                and self.x_rect(self.visualRect(index)).contains(pos))
        return index.row(), on_x

    def mouseMoveEvent(self, event):
        row, on_x = self._row_and_x(event.position().toPoint())
        if (row, on_x) != (self.hover_row, self.hover_on_x):
            self.hover_row, self.hover_on_x = row, on_x
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hover_row, self.hover_on_x = -1, False
        self.viewport().update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        row, on_x = self._row_and_x(event.position().toPoint())
        if on_x and event.button() == Qt.LeftButton:
            self.remove(self.item(row).data(_KEY_ROLE))
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        item = self.currentItem()
        if item is not None:
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                self.remove(item.data(_KEY_ROLE))
                return
            if event.modifiers() & Qt.AltModifier and event.key() in (Qt.Key_Up, Qt.Key_Down):
                step = -1 if event.key() == Qt.Key_Up else 1
                self.move(item.data(_KEY_ROLE), self.row(item) + step)
                return
        super().keyPressEvent(event)


# --- the dialog ------------------------------------------------------------------------

def _default_columns(fields) -> list:
    """What a fresh View shows: the id, and the display name when the registry has one."""
    keys = {f.key for f in fields}
    return ["id"] + (["a:localization"] if "a:localization" in keys else [])


class QueryConstructorDialog(QDialog):
    """Author a new View, or edit an existing one.

    * **edit** (``query=...``): name (when ``view_name`` is given), columns and filter are
      all editable; the registry is shown but fixed — changing it would invalidate every
      column and condition already chosen. On accept, ``result_query`` holds the whole
      updated query and ``result_filter`` its filter.
    * **new** (``new_view=True``): pick a registry, name, columns and filter. On accept,
      ``result_query`` and ``result_name`` are set.

    ``create_tag(registry_type, suggested_name) -> str | None`` enables the column
    picker's "＋ New tag" row; it should run the app's own tag wizard and return the name
    of the tag it defined, or None if the user backed out.
    """

    def __init__(self, tag_store, registry_type=None, *, query=None, new_view=False,
                 view_store=None, parent=None, registries=None, packdump=None,
                 view_name=None, view_id=None, create_tag=None):
        super().__init__(parent)
        self._new_view = new_view
        self._query = query
        # The scope decides the registry when there is one; a blueprint-scoped View has
        # none, and gets the filter only (its columns ARE the blueprint's slots).
        scope = query.scope if query is not None else None
        if scope is not None:
            registry_type = scope.type if isinstance(scope, Registry) else None
        self._registry_type = registry_type
        self._tags = tag_store
        self._packdump = packdump
        self._create_tag = create_tag
        # Given a store, the dialog checks the name itself rather than closing and failing
        # afterwards — losing a query you just built would be a miserable way to learn the
        # name was taken.
        self._view_store = view_store
        self._view_id = view_id
        self._rows = []
        self._keep = list(query.select) if query is not None else []
        self._fields = registry_fields(tag_store, registry_type, packdump, self._keep)
        self._columns = None
        self._name_edit = None
        self.result_filter = query.filter if query else None
        self.result_query = None
        self.result_name = ""

        self.setWindowTitle("New View" if new_view else "Edit View")
        self.setMinimumWidth(640)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        # --- identity: name + registry ---------------------------------------------
        form = QHBoxLayout()
        form.setSpacing(8)
        if new_view or view_name is not None:
            form.addWidget(QLabel("Name"))
            self._name_edit = QLineEdit(view_name or "")
            self._name_edit.setPlaceholderText("My View")
            form.addWidget(self._name_edit, 1)

        # A new view has to be able to target any registry — hard-coding minecraft:item
        # made 134 of the pack's 135 registries unreachable from this wizard. A picker, not
        # a combo: 135 of them is a list you search, not one you scroll.
        self._registry_combo = None
        if new_view and registries:
            form.addWidget(QLabel("Registry"))
            self._registry_combo = PickerButton(
                self._registry_choices(registries), header="Registry",
                placeholder="search registries")
            if not self._registry_combo.set_current(registry_type):
                # Asked for a registry this pack doesn't have: follow what the picker shows,
                # or the fields would describe one registry while the query names another.
                registry_type = self._registry_type = self._registry_combo.current()
                self._fields = registry_fields(tag_store, registry_type, packdump)
            self._registry_combo.setMinimumWidth(260)
            self._registry_combo.changed.connect(self._on_registry_changed)
            form.addWidget(self._registry_combo, 1)
        else:
            scope_text = (registry_type if registry_type is not None
                          else f"blueprint {getattr(scope, 'name', '?')}")
            scope_lbl = QLabel(f"Registry:  <b>{scope_text}</b>" if registry_type
                               else f"Scope:  <b>{scope_text}</b>")
            scope_lbl.setTextFormat(Qt.RichText)
            scope_lbl.setToolTip("A View's scope is fixed once it exists — changing it would "
                                 "invalidate every column and condition chosen for it.")
            form.addWidget(scope_lbl)
            if self._name_edit is None:
                form.addStretch()
        root.addLayout(form)

        # --- columns -----------------------------------------------------------------
        if registry_type is not None:
            root.addWidget(_section("Columns"))
            self._columns = ColumnList(self)
            root.addWidget(self._columns)
            add_row = QHBoxLayout()
            self._add_column_btn = QPushButton("＋  Add column")
            self._add_column_btn.setToolTip(
                "Search this registry's tags and attributes"
                + (" — or define a new tag" if create_tag else ""))
            self._add_column_btn.clicked.connect(self._open_column_picker)
            add_row.addWidget(self._add_column_btn)
            hint = QLabel("drag to reorder · Delete or × to remove")
            hint.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
            add_row.addWidget(hint)
            add_row.addStretch()
            root.addLayout(add_row)
            if query is not None:
                self._columns.set_columns([self._column_entry(n) for n in query.select])
            else:
                self._reset_default_columns()
        else:
            note = QLabel("Columns follow the blueprint's slots — show or hide them from the "
                          "grid itself.")
            note.setStyleSheet(f"color: {style.TEXT_MUTED};")
            root.addWidget(note)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {style.DIVIDER};")
        root.addWidget(line)

        # --- filter ------------------------------------------------------------------
        root.addWidget(_section("Filter"))
        combiner_row = QHBoxLayout()
        combiner_row.addWidget(QLabel("Show entries that match"))
        self._combiner = DropDown()
        self._combiner.addItem("ALL of the conditions  (AND)", "AND")
        self._combiner.addItem("ANY of the conditions  (OR)", "OR")
        combiner_row.addWidget(self._combiner)
        combiner_row.addStretch()
        root.addLayout(combiner_row)

        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(4)
        root.addLayout(self._rows_box)

        controls = QHBoxLayout()
        add_btn = QPushButton("＋  Add condition")
        add_btn.clicked.connect(lambda: self._add_row())
        controls.addWidget(add_btn)
        # The builder knows a fixed set of row shapes, and the language keeps outgrowing it
        # — token matching, nested groups, and now MENTIONS / BOUND_IN, which is the whole
        # "what did I not choose" question (§3.2.4). Text mode already existed as a
        # *fallback* for opening a query the rows could not reproduce, which meant a NEW
        # view could never reach it: it starts from no filter, so there is nothing to fail
        # to decompose. One button turns a rescue hatch into a front door, and every future
        # node is reachable the day it parses rather than the day someone builds a widget.
        self._mode_btn = QPushButton("Edit as text")
        self._mode_btn.setToolTip("Write the filter in the same syntax as the filter bar")
        self._mode_btn.clicked.connect(self._toggle_mode)
        controls.addWidget(self._mode_btn)
        controls.addStretch()
        root.addLayout(controls)

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

        if self._name_edit is not None and new_view:
            self._name_edit.setFocus()

    # --- registry ----------------------------------------------------------------

    def _registry_choices(self, registries) -> list[Choice]:
        """Each registry with how big it is and how many of your tags live on it — the
        two things that tell `minecraft:worldgen/biome` from `minecraft:worldgen/biome_source`
        at a glance."""
        choices = []
        for name in registries:
            parts = []
            if self._packdump is not None and name in self._packdump.registry:
                count = len(self._packdump.registry[name].get("values", ()))
                parts.append(f"{count:,} entries")
            tags = len(self._tags.definitions_for(name))
            if tags:
                parts.append(f"{tags} tag{'s' if tags != 1 else ''}")
            choices.append(Choice(name, name, " · ".join(parts)))
        return choices

    def _on_registry_changed(self, registry):
        """Switching registry invalidates the columns and conditions, which name the old
        registry's fields. Both are reset rather than silently kept pointing at fields
        that no longer exist."""
        self._registry_type = registry
        self._fields = registry_fields(self._tags, registry, self._packdump)
        for row in list(self._rows):
            self._remove_row(row)
        self._reset_default_columns()

    # --- columns -----------------------------------------------------------------

    def _field(self, key):
        return next((f for f in self._fields if f.key == key), None)

    def _column_entry(self, node):
        """``(key, label, detail, removable, warn)`` for one selected field.

        Anything the field list does not know is kept, not dropped — a tag undefined since
        the View was made, `AllAttributes` from a browse table, a node from a newer build.
        Pressing OK on a View you only meant to rename must not cost it a column.
        """
        key = field_key(node)
        removable = key != "id"
        known = self._field(key)
        if known is not None:
            return key, known.label, known.detail, removable, False
        if node == AllAttributes:
            return key, "all attributes", "every attribute the dump carries", removable, False
        if isinstance(node, Tag):
            return key, node.name, "tag no longer defined on this registry", removable, True
        try:
            label = column_name(node)
        except Exception:
            label = repr(node)
        return key, label, "kept as is", removable, False

    def _reset_default_columns(self):
        if self._columns is None:
            return
        self._columns.set_columns([self._column_entry(self._field(k).node)
                             for k in _default_columns(self._fields)])
        self._keep = [self._field(k).node for k in _default_columns(self._fields)]

    def _column_nodes(self) -> list:
        """The selected fields, in list order, as query nodes."""
        known = {f.key: f.node for f in self._fields}
        kept = {field_key(n): n for n in self._keep}
        return [known.get(k) or kept[k] for k in self._columns.keys()
                if k in known or k in kept]

    def _open_column_picker(self):
        shown = set(self._columns.keys())
        choices = [Choice(f.label, f.key, f.detail) for f in self._fields
                   if f.key not in shown]
        if "a:*" not in shown:
            choices.append(Choice("all attributes", "a:*",
                                  "every attribute the dump carries, as it grows"))
        picker = PickerPopup(choices, header=f"Add a column · {self._registry_type}",
                             placeholder="search tags and attributes", parent=self,
                             create="tag" if self._create_tag else None, searchable=True)
        picker.chosen.connect(self.add_column)
        picker.create_requested.connect(self._create_tag_column)
        self._picker = picker           # held: a popup with no owner vanishes mid-show
        button = self._add_column_btn
        rect = QRect(button.mapToGlobal(QPoint(0, 0)), button.size())
        QTimer.singleShot(0, lambda: picker.popup_under(rect))
        return picker

    def add_column(self, key) -> bool:
        """Append a column by field key. False if it is unknown or already shown."""
        if self._columns is None or key in self._columns.keys():
            return False
        if key == "a:*":
            node = AllAttributes
        else:
            field = self._field(key)
            if field is None:
                return False
            node = field.node
        if node not in self._keep:
            self._keep.append(node)
        item = self._columns.add(*self._column_entry(node)[:3],
                                 removable=True, warn=False)
        self._columns.setCurrentItem(item)
        self._columns.scrollToItem(item)
        return True

    def _create_tag_column(self, suggested):
        """"＋ New tag" from the column picker: run the app's tag wizard pinned to this
        registry, then show the new tag as a column and offer it to the conditions.

        The tag is real the moment the wizard closes — cancelling this dialog afterwards
        does not undefine it. That is the right way round: a tag is vocabulary, and losing
        one you just named because you then backed out of a different dialog would be a
        surprise in the direction that costs work.
        """
        if self._create_tag is None:
            return
        name = self._create_tag(self._registry_type, suggested)
        if not name:
            return
        self._fields = registry_fields(self._tags, self._registry_type, self._packdump,
                                       self._keep)
        for row in self._rows:
            row.set_fields(self._fields)
        self.add_column(f"t:{name}")

    # --- filter rows / text mode -------------------------------------------------

    def _toggle_mode(self):
        """Swap between the rows and the text, carrying the filter across intact."""
        if getattr(self, "_text_edit", None) is not None:
            self._leave_text_mode()
        else:
            # Whatever the rows currently say, so switching never costs work already done.
            self._enter_text_mode(self.build_filter())

    def _leave_text_mode(self):
        """Back to rows — but only if they can hold what the text says.

        Refusing is the point. Silently rewriting a filter the rows cannot express is
        exactly the bug the reproduce-check below was added to prevent, and it would be
        worse here because the user typed the thing being discarded.
        """
        text = self._text_edit.text().strip()
        try:
            parsed = parse_query(text) if text else None
        except QuerySyntaxError as e:
            QMessageBox.warning(self, "Can't read that filter", str(e))
            return

        for widget in (self._text_edit, self._text_note):
            widget.setParent(None)
            widget.deleteLater()
        self._text_edit = self._text_note = None
        self._combiner.setEnabled(True)
        self._mode_btn.setText("Edit as text")

        combiner, nodes = _decompose(parsed)
        self._combiner.setCurrentIndex(0 if combiner == "AND" else 1)
        for node in nodes:
            self._add_row(preset=node)
        if parsed is not None and self.build_filter() != parsed:
            self._enter_text_mode(parsed)      # the rows would have corrupted it
            QMessageBox.information(
                self, "Kept as text",
                "The visual builder can't express that filter without changing it, so "
                "it's staying as text. Nothing was lost.")

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
        self._mode_btn.setText("Back to conditions")
        self._text_edit = QLineEdit(format_query(original) if original is not None else "")
        self._text_edit.setPlaceholderText('MENTIONS "StoneType" AND NOT BOUND_IN "StoneType"')
        self._text_edit.setToolTip(
            "The same syntax as the filter bar.\n\n"
            't:remove                     a tag\n'
            'mod == quark                 a comparison\n'
            'id MATCHES_TOKENS_I ("cut")  word matching\n'
            'MENTIONS "StoneType"         names one of a blueprint\'s instances\n'
            'BOUND_IN "StoneType"         already chosen in that blueprint')
        self._text_note = QLabel(
            "This filter uses something the visual builder can't show (token matching, a "
            "list, nested groups, or a blueprint question), so it's shown as text — edit "
            "it here and nothing is lost.")
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

    def build_filter(self):
        nodes = [n for n in (r.to_node() for r in self._rows) if n is not None]
        if not nodes:
            return None
        if len(nodes) == 1:
            return nodes[0]
        return And(nodes) if self._combiner.currentData() == "AND" else Or(nodes)

    # --- commit ------------------------------------------------------------------

    def accept(self):
        name = ""
        if self._name_edit is not None:
            name = self._name_edit.text().strip()
            if not name:
                if not self._new_view:
                    QMessageBox.warning(self, "Name required", "A view needs a name.")
                    self._name_edit.setFocus()
                    return
                name = "Untitled View"
            clash = (self._view_store.get_by_name(name)
                     if self._view_store is not None else None)
            if clash is not None and clash.id != self._view_id:
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

        self.result_name = name
        if self._columns is not None:
            selected = self._column_nodes() or [Id]
        else:
            selected = list(self._query.select) if self._query is not None else [Id]

        if self._query is not None:
            # An unchanged column list keeps whatever order it had; a changed one sorts by
            # its first column, which is the column the table's sort arrow starts on.
            order_by = (self._query.order_by if selected == list(self._query.select)
                        else [selected[0]])
            self.result_query = dataclasses.replace(
                self._query, select=selected, filter=self.result_filter, order_by=order_by)
        else:
            self.result_query = Query(
                scope=Registry(self._registry_type),
                select=selected,
                filter=self.result_filter,
                order_by=[selected[0]],
            )
        super().accept()
