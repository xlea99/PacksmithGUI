"""Creating a tag definition (design 3.2.1) — a dialog, not a tab.

A tag definition is **flat**: registry, name, type, optional enum values, optional default.
Blueprints earn a full tab because they're a nested tree with unlimited depth (§3.2.2); a
tag has no structure to navigate, so a focused modal is the honest shape. Precedent: the
query constructor is already a dialog, and a query is the more complex artifact.

Scope is deliberately **creation only**. Rename, retype, and enum-value removal are governed
by §3.2.1's Tag Schema Evolution rules (rename is loud and requires relinking bound job
steps; retype is forbidden outright; removing an enum value orphans assignments) and none of
that ceremony exists yet. Offering the controls before the consequences are implemented
would be lying to the user.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit, QPushButton,
    QLabel, QCheckBox, QListWidget, QDialogButtonBox, QWidget, QMessageBox,
)

from packsmith.gui.shell import style

TAG_TYPES = [
    ("bool", "true / false"),
    ("string", "any text"),
    ("enum", "one of a fixed set"),
    ("number", "int or float"),
]
# `reference` is a fifth type in §3.2.1 but isn't implemented in the tag store yet, so it is
# deliberately not offered here — a dropdown option core would reject is worse than its absence.


class EnumValuesEditor(QWidget):
    """Add / remove / reorder the values of an enum tag.

    Order is not cosmetic: enum values carry a declaration order that is semantic
    (`early < mid < late < end`), and tables sort by it.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        entry_row = QHBoxLayout()
        self._entry = QLineEdit()
        self._entry.setPlaceholderText("value, then Enter")
        self._entry.returnPressed.connect(self._add)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add)
        entry_row.addWidget(self._entry)
        entry_row.addWidget(add_btn)
        lay.addLayout(entry_row)

        body = QHBoxLayout()
        self._list = QListWidget()
        self._list.setStyleSheet(style.LIST_QSS)
        self._list.setMinimumHeight(110)
        body.addWidget(self._list)

        buttons = QVBoxLayout()
        for label, slot in (("↑", self._move_up), ("↓", self._move_down), ("✕", self._remove)):
            btn = QPushButton(label)
            btn.setFixedWidth(30)
            btn.clicked.connect(slot)
            buttons.addWidget(btn)
        buttons.addStretch()
        body.addLayout(buttons)
        lay.addLayout(body)

    def _add(self):
        text = self._entry.text().strip()
        if not text or text in self.values():
            self._entry.clear()
            return
        self._list.addItem(text)
        self._entry.clear()

    def _remove(self):
        row = self._list.currentRow()
        if row >= 0:
            self._list.takeItem(row)

    def _move_up(self):
        self._move(-1)

    def _move_down(self):
        self._move(+1)

    def _move(self, delta):
        row = self._list.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < self._list.count()):
            return
        item = self._list.takeItem(row)
        self._list.insertItem(target, item)
        self._list.setCurrentRow(target)

    def values(self) -> list[str]:
        return [self._list.item(i).text() for i in range(self._list.count())]


class EnumValuesDialog(QDialog):
    """Edit an existing enum tag's values (design 3.2.1, Tag Schema Evolution).

    Adding and reordering are free. **Removing** is destructive, so OK computes the blast
    radius first and states it plainly: how many assignments will be orphaned, and whether
    the tag's default is about to be invalidated. Orphaned assignments are kept, not
    deleted — the user resolves them in the Errors panel.
    """

    def __init__(self, tag_store, registry_type, tag_name, parent=None):
        super().__init__(parent)
        self._tags = tag_store
        self._registry_type = registry_type
        self._tag_name = tag_name
        self.setWindowTitle(f"Values — {tag_name}")
        self.setMinimumWidth(400)

        definition = tag_store.definition(registry_type, tag_name)
        current = list(definition.get("values", [])) if definition else []

        root = QVBoxLayout(self)
        root.setSpacing(8)

        header = QLabel(f"<b>{tag_name}</b>  on  {registry_type}")
        header.setTextFormat(Qt.RichText)
        root.addWidget(header)

        note = QLabel("Order is meaningful — tables sort by it. Adding is free; removing a "
                      "value in use will orphan those assignments.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
        root.addWidget(note)

        self._editor = EnumValuesEditor()
        for value in current:
            self._editor._list.addItem(value)
        root.addWidget(self._editor)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self):
        values = self._editor.values()
        if not values:
            QMessageBox.warning(self, "Values required", "An enum tag needs at least one value.")
            return

        preview = self._tags.preview_enum_change(self._registry_type, self._tag_name, values)
        if preview["removed"]:
            lines = [f"Remove {', '.join(repr(v) for v in preview['removed'])} "
                     f"from '{self._tag_name}'?", ""]
            if preview["orphan_count"]:
                for value, entries in preview["orphaned"].items():
                    lines.append(f"• {len(entries)} assignment(s) still use '{value}'.")
                lines.append("")
                lines.append("Those assignments will be kept but marked orphaned — resolve "
                             "them in the Errors panel (Clear, Reassign, or Restore).")
            else:
                lines.append("No assignments use the removed value(s).")
            if preview["default_cleared"]:
                lines.append("")
                lines.append("This tag's default points at a removed value, so the default "
                             "will be cleared.")
            confirm = QMessageBox.question(self, "Remove enum values", "\n".join(lines),
                                           QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if confirm != QMessageBox.Yes:
                return

        self._tags.set_enum_values(self._registry_type, self._tag_name, values)
        super().accept()


class TagCreateDialog(QDialog):
    """Create a tag definition. On accept, the ``result_*`` attributes hold the inputs."""

    def __init__(self, registries: list[str], *, tag_store=None, preferred_registry=None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Tag")
        self.setMinimumWidth(460)
        # Given a store, the dialog checks name collisions itself so it can stay open and
        # keep the user's work instead of closing and reporting failure afterwards.
        self._tag_store = tag_store

        self.result_registry = None
        self.result_name = ""
        self.result_type = "bool"
        self.result_enum_values = None
        self.result_default = None

        root = QVBoxLayout(self)
        root.setSpacing(8)
        form = QFormLayout()
        form.setSpacing(6)

        self._registry = QComboBox()
        self._registry.addItems(registries)
        if preferred_registry in registries:
            self._registry.setCurrentText(preferred_registry)
        form.addRow("Registry", self._registry)

        self._name = QLineEdit()
        self._name.setPlaceholderText("remove")
        form.addRow("Name", self._name)

        self._type = QComboBox()
        for key, blurb in TAG_TYPES:
            self._type.addItem(f"{key}  —  {blurb}", key)
        self._type.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Type", self._type)
        root.addLayout(form)

        self._enum_label = QLabel("Values  (order is meaningful — tables sort by it)")
        self._enum_label.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        self._enum_editor = EnumValuesEditor()
        root.addWidget(self._enum_label)
        root.addWidget(self._enum_editor)

        # Default is opt-in: an unset tag with no default is a genuinely different state
        # from one defaulting to false (§3.2.1), so "no default" must be expressible.
        default_row = QHBoxLayout()
        self._has_default = QCheckBox("Default value")
        self._has_default.toggled.connect(self._sync_default_widget)
        default_row.addWidget(self._has_default)
        self._default_combo = QComboBox()
        self._default_text = QLineEdit()
        default_row.addWidget(self._default_combo)
        default_row.addWidget(self._default_text)
        root.addLayout(default_row)

        hint = QLabel("Unset entries show the default but stay pristine — no assignment is written.")
        hint.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        root.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._on_type_changed()

    # --- reactive bits -----------------------------------------------------

    def _tag_type(self) -> str:
        return self._type.currentData()

    def _on_type_changed(self):
        is_enum = self._tag_type() == "enum"
        self._enum_label.setVisible(is_enum)
        self._enum_editor.setVisible(is_enum)
        self._sync_default_widget()
        self.adjustSize()

    def _sync_default_widget(self):
        tag_type = self._tag_type()
        enabled = self._has_default.isChecked()
        use_combo = tag_type in ("bool", "enum")

        self._default_combo.setVisible(use_combo)
        self._default_text.setVisible(not use_combo)
        self._default_combo.setEnabled(enabled)
        self._default_text.setEnabled(enabled)

        if use_combo:
            current = self._default_combo.currentText()
            self._default_combo.clear()
            if tag_type == "bool":
                self._default_combo.addItems(["true", "false"])
            else:
                self._default_combo.addItems(self._enum_editor.values())
            if current:
                idx = self._default_combo.findText(current)
                if idx >= 0:
                    self._default_combo.setCurrentIndex(idx)
        else:
            self._default_text.setPlaceholderText("0" if tag_type == "number" else "text")

    # --- commit ------------------------------------------------------------

    def accept(self):
        name = self._name.text().strip()
        tag_type = self._tag_type()
        registry = self._registry.currentText()

        if not name:
            QMessageBox.warning(self, "Name required", "A tag needs a name.")
            return

        if self._tag_store is not None and self._tag_store.definition(registry, name):
            QMessageBox.warning(
                self, "Name already used",
                f"'{name}' is already defined on {registry}.\n\n"
                f"Tag names are unique within a registry — but the same name on a "
                f"different registry is a separate tag, and that's allowed.")
            self._name.setFocus()
            self._name.selectAll()
            return

        enum_values = None
        if tag_type == "enum":
            # Refresh in case values were added after the default widget was last synced.
            self._sync_default_widget()
            enum_values = self._enum_editor.values()
            if not enum_values:
                QMessageBox.warning(self, "Values required",
                                    "An enum tag needs at least one value.")
                return

        default = None
        if self._has_default.isChecked():
            if tag_type == "bool":
                default = self._default_combo.currentText() == "true"
            elif tag_type == "enum":
                default = self._default_combo.currentText()
                if not default:
                    QMessageBox.warning(self, "Default required",
                                        "Pick a default value, or untick Default value.")
                    return
            elif tag_type == "number":
                raw = self._default_text.text().strip()
                try:
                    default = int(raw)
                except ValueError:
                    try:
                        default = float(raw)
                    except ValueError:
                        QMessageBox.warning(self, "Invalid default",
                                            f"'{raw}' is not a number.")
                        return
            else:
                default = self._default_text.text()

        self.result_registry = registry
        self.result_name = name
        self.result_type = tag_type
        self.result_enum_values = enum_values
        self.result_default = default
        super().accept()
