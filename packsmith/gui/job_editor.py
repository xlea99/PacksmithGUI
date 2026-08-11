"""Authoring jobs: the editor tab, the step editor, and the action picker (design 3.3.2).

The step editor follows §3.3.2 literally: *"one unified panel of what this step needs.
Mappings and configuration are presented together as a single list of slots to fill…
The architectural distinction between mappings and configuration is invisible to the
user; they just fill in what the action asks for."* So both kinds of slot render as rows
of one form — artifact pickers for mappings, inline editors for config — and nothing in
the UI names the distinction.
"""
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QCheckBox, QTreeWidget, QTreeWidgetItem, QDialogButtonBox,
    QAbstractItemView, QMessageBox, QListWidget, QListWidgetItem,
)

from packsmith.core.bindings import binding_id, binding_name, mapping_mismatches
from packsmith.core.shapes import describe_shape
from packsmith.gui.shell import style
from packsmith.gui.shell.picker import PickerPopup, _token_match
from packsmith.gui.shell.tree import PanelTree

_INHERIT = "(inherit from job)"


def _refers_to(stored, artifact_id, label) -> bool:
    """Does a stored binding point at this candidate?

    Bindings hold ids (design 3.2.1) but legacy rows hold names, so every widget that has
    to find "the currently bound one" must accept both. Comparing against the LABEL alone
    is what silently unchecked every id-bound row and let OK save an empty selection.
    """
    if stored is None:
        return False
    if stored == artifact_id:
        return True
    return isinstance(stored, str) and stored == label


# --- picking an action ------------------------------------------------------

class ActionPickerDialog(QDialog):
    """Choose an installed action to add as a step."""

    def __init__(self, package_index, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Action Step")
        self.setMinimumSize(520, 360)
        self.result_ref = None

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Installed actions"))

        self._list = QListWidget()
        self._list.setStyleSheet(style.LIST_QSS)
        for ref, manifest in sorted(package_index.actions.items()):
            item = QListWidgetItem(f"{manifest.name or manifest.action_id}   —   {ref}")
            item.setData(Qt.UserRole, ref)
            if manifest.description:
                item.setToolTip(manifest.description)
            self._list.addItem(item)
        self._list.itemDoubleClicked.connect(lambda _: self.accept())
        root.addWidget(self._list)

        if self._list.count() == 0:
            empty = QLabel("No action packages are installed in this profile.")
            empty.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
            root.addWidget(empty)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self):
        item = self._list.currentItem()
        if item is None:
            QMessageBox.warning(self, "Pick an action", "Select an action to add.")
            return
        self.result_ref = item.data(Qt.UserRole)
        super().accept()


class JobPickerDialog(QDialog):
    """Choose another job to nest as a step. Jobs that would create a cycle are excluded
    up front rather than rejected after the fact."""

    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Job Step")
        self.setMinimumSize(420, 300)
        self.result_job_id = None

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Run another job as a step"))
        self._list = QListWidget()
        self._list.setStyleSheet(style.LIST_QSS)
        for job in candidates:
            item = QListWidgetItem(job.name)
            item.setData(Qt.UserRole, job.id)
            self._list.addItem(item)
        self._list.itemDoubleClicked.connect(lambda _: self.accept())
        root.addWidget(self._list)
        if not candidates:
            note = QLabel("No other job can be nested here without creating a cycle.")
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
            root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self):
        item = self._list.currentItem()
        if item is None:
            self.reject()
            return
        self.result_job_id = item.data(Qt.UserRole)
        super().accept()


def _select_stored(combo, stored):
    """Select the row a stored binding refers to, by id or by legacy name.

    `findData` alone missed every legacy name-binding and fell through to index 0 — which
    is not "nothing selected", it is *the alphabetically first artifact*, so pressing OK
    retargeted the step to whatever happened to sort first.
    """
    for i in range(combo.count()):
        if _refers_to(stored, combo.itemData(i), combo.itemText(i)):
            combo.setCurrentIndex(i)
            return
    combo.setCurrentIndex(0)


class _EntryField(QWidget):
    """Bind one Layer 1 entry: a typed field with a browse button.

    Not a QComboBox — `minecraft:item` is 14,000 entries on a real pack, which is the exact
    case `PickerPopup` was built for. And the field stays typeable: §5.3's rule that a
    recommendation "must never restrict" applies just as much to a picker as to a template.
    """

    def __init__(self, registry_type, entries, current):
        super().__init__()
        self._entries = entries
        self._registry_type = registry_type
        self._picker = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText(f"{len(entries):,} in {registry_type}")
        if current:
            self._edit.setText(str(current))
        row.addWidget(self._edit, 1)
        browse = QPushButton("…", self)
        browse.setFixedWidth(28)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)

    def _browse(self):
        picker = PickerPopup(self._entries, header=self._registry_type, parent=self)
        picker.chosen.connect(self._edit.setText)
        self._picker = picker           # held: a popup with no owner vanishes mid-show
        QTimer.singleShot(0, lambda: picker.popup_at(QCursor.pos()))

    def value(self):
        return self._edit.text().strip() or None


class _MultiSelect(QWidget):
    """A checkable list for a `cardinality = "many"` mapping (design 3.3).

    Checkboxes rather than Ctrl-click selection: a binding that survives closing the dialog
    should look like a binding, and a multi-select highlight reads as transient. Misfits are
    listed but not checkable, for the same reason the single picker keeps them.

    A filter appears once the list is long enough to need one. A `many` mapping over a
    registry is 14,000 rows on a real pack, and scrolling to find three of them is the
    problem `PickerPopup` was built to solve — the same token matching, applied here by
    hiding rows so checked-but-hidden entries stay bound.
    """

    _FILTER_THRESHOLD = 12

    def __init__(self, candidates, current):
        super().__init__()
        chosen = list(current or ())
        self._original = list(chosen)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(3)

        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText(f"type to narrow {len(candidates):,}")
        self._filter.textChanged.connect(self._apply_filter)
        root.addWidget(self._filter)

        self._list = QListWidget(self)
        for value, artifact_id, reasons in candidates:
            item = QListWidgetItem(value if not reasons else f"{value} — doesn't fit")
            item.setData(Qt.UserRole, artifact_id)
            item.setData(Qt.UserRole + 1, value)          # the label, for filtering
            if reasons:
                item.setFlags(Qt.NoItemFlags)      # visible, inert, uncheckable
                item.setToolTip("\n".join(reasons))
                item.setForeground(style.qt_colour(style.TEXT_FAINT))
            else:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                bound = any(_refers_to(c, artifact_id, value) for c in chosen)
                item.setCheckState(Qt.Checked if bound else Qt.Unchecked)
            self._list.addItem(item)
        self._list.setUniformItemSizes(True)
        self._list.setMaximumHeight(150)
        self._list.setStyleSheet(style.LIST_QSS)
        root.addWidget(self._list)
        # Ordered after addWidget: setVisible on a parentless widget shows a real window.
        self._filter.setVisible(len(candidates) >= self._FILTER_THRESHOLD)

    def _apply_filter(self, text):
        for row in range(self._list.count()):
            item = self._list.item(row)
            item.setHidden(not _token_match(item.data(Qt.UserRole + 1), text))

    def count(self) -> int:
        return self._list.count()

    def chosen(self) -> list:
        """Every checked row, hidden or not — filtering narrows the view, not the binding.

        Ordered by what was already stored, then by the list. Opening a dialog and pressing
        OK should not rewrite a binding just because the widget enumerates candidates in a
        different order than they were saved in.
        """
        picked = [self._list.item(r).data(Qt.UserRole) for r in range(self._list.count())
                  if self._list.item(r).checkState() == Qt.Checked]
        kept = [v for v in self._original if v in picked]
        return kept + [v for v in picked if v not in kept]


# --- editing one step -------------------------------------------------------

class StepEditorDialog(QDialog):
    """Fill in what a step needs. Mappings and config are one list of slots."""

    def __init__(self, manifest, tag_store, step, parent=None, blueprint_store=None,
                 packdump=None):
        super().__init__(parent)
        self._dump = packdump
        self.setWindowTitle(f"Step — {manifest.name or manifest.action_id}")
        self.setMinimumWidth(480)
        self._manifest = manifest
        self._tags = tag_store
        self._blueprints = blueprint_store
        self._mapping_widgets = {}
        self._config_widgets = {}

        self.result_bindings = dict(step.bindings)
        self.result_config = dict(step.config)
        self.result_on_error = step.on_error

        root = QVBoxLayout(self)
        root.setSpacing(8)

        title = QLabel(f"<b>{manifest.ref}</b>")
        title.setTextFormat(Qt.RichText)
        root.addWidget(title)
        if manifest.description:
            desc = QLabel(manifest.description)
            desc.setWordWrap(True)
            desc.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
            root.addWidget(desc)

        form = QFormLayout()
        form.setSpacing(6)

        # One list of slots — mappings and config together, in declaration order.
        for name, slot in manifest.mappings.items():
            widget = self._mapping_widget(name, slot, step.bindings.get(name))
            self._mapping_widgets[name] = widget
            form.addRow(self._label_for(name, slot.description, slot.required), widget)

        for name, param in manifest.config.items():
            current = step.config.get(name, param.default)
            widget = self._config_widget(param, current)
            self._config_widgets[name] = (widget, param)
            form.addRow(self._label_for(name, param.description, param.required), widget)

        if not manifest.mappings and not manifest.config:
            form.addRow(QLabel("This action needs nothing configured."))
        root.addLayout(form)

        # Error policy is step state, not action state (design 3.3.2, Model B).
        policy_row = QHBoxLayout()
        policy_row.addWidget(QLabel("If this step fails"))
        self._on_error = QComboBox()
        self._on_error.addItem(_INHERIT, None)
        self._on_error.addItem("halt — stop the job", "halt")
        self._on_error.addItem("skip — carry on", "skip")
        index = self._on_error.findData(step.on_error)
        self._on_error.setCurrentIndex(max(0, index))
        policy_row.addWidget(self._on_error)
        policy_row.addStretch()
        root.addLayout(policy_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _label_for(name, description, required):
        label = QLabel(name + ("" if required else "  (optional)"))
        if description:
            label.setToolTip(description)
        return label

    def _mapping_widget(self, name, slot, current):
        """An artifact picker: the user's own artifacts of the kind this slot asks for."""
        if slot.cardinality == "many":
            # 3.3: "many — zero or more artifacts (UI: multi-select list)".
            return _MultiSelect(self._candidates_for(slot), current)

        combo = QComboBox()
        if not slot.required:
            combo.addItem("(unbound)", None)

        if slot.kind == "registry_entry":
            return _EntryField(slot.registry_type,
                               self._registry_entries(slot.registry_type), current)

        if slot.kind == "blueprint_instance":
            self._fill_choice_combo(combo, slot, current,
                                    empty="(no instances fit this action)")
            return combo

        if slot.kind == "blueprint":
            # Blueprint mappings bind a SCHEMA, so the action never names the user's own
            # (design 3.3) — the same rule tags follow. Which schemas qualify is decided
            # structurally, by required_shape.
            self._fill_choice_combo(combo, slot, current,
                                    empty="(no blueprint fits this action)")
            return combo

        definitions = self._tags.definitions_for(slot.registry_type) if slot.registry_type else {}
        for tag_name, definition in sorted(definitions.items()):
            if slot.tag_type and definition["type"] != slot.tag_type:
                continue
            combo.addItem(tag_name, binding_id(slot, tag_name, tag_store=self._tags))
        if combo.count() == 0:
            combo.addItem(f"(no {slot.tag_type or ''} tags on {slot.registry_type})", None)
            combo.setEnabled(False)
        _select_stored(combo, current)
        return combo

    def _registry_entries(self, registry_type) -> list:
        if self._dump is None:
            return []
        return sorted((self._dump.registry.get(registry_type) or {}).get("values", ()))

    def _candidates_for(self, slot) -> list:
        """``[(value, reasons)]`` — everything bindable here, fitting ones first.

        Misfits are kept, with their reasons, rather than filtered out: a user whose
        `StoneType` doesn't appear has no way to learn that it's missing `polished.wall`.
        Showing it greyed with "why" turns a dead end into a to-do.
        """
        if slot.kind == "registry_entry":
            return [(e, e, []) for e in self._registry_entries(slot.registry_type)]
        if self._blueprints is None:
            return []
        if slot.kind == "blueprint_instance":
            values = [i.ref for name in self._blueprints.names()
                      for i in self._blueprints.instances(name)]
        else:
            values = list(self._blueprints.names())
        scored = [(v, mapping_mismatches(slot, v, self._blueprints)) for v in values]
        ordered = [s for s in scored if not s[1]] + [s for s in scored if s[1]]
        # Paired with the ID that actually gets stored (design 3.2.1) — the name is only
        # what the row says.
        return [(name, binding_id(slot, name, tag_store=self._tags,
                                  blueprint_store=self._blueprints), reasons)
                for name, reasons in ordered]

    def _fill_choice_combo(self, combo, slot, current, *, empty):
        candidates = self._candidates_for(slot)
        if not candidates:
            combo.addItem("(nothing of this kind exists yet)", None)
            combo.setEnabled(False)
            return

        fitting = [v for v, _id, reasons in candidates if not reasons]
        for value, artifact_id, reasons in candidates:
            combo.addItem(value if not reasons else f"{value} — doesn't fit", artifact_id)
            if reasons:
                item = combo.model().item(combo.count() - 1)
                item.setEnabled(False)
                item.setToolTip("\n".join(reasons))

        if slot.required_shape:
            combo.setToolTip(f"needs: {describe_shape(slot.required_shape)}")
        if not fitting:
            # A placeholder carrying None, or the disabled misfit at row 0 would become
            # `currentData()` and get saved as the binding — the picker would look refused
            # and bind anyway.
            combo.insertItem(0, empty, None)
            combo.setEnabled(False)
        # An existing binding stays selected even if it no longer fits: opening this dialog
        # shouldn't quietly unbind a step. The run refuses instead, and says why.
        _select_stored(combo, current)

    @staticmethod
    def _config_widget(param, current):
        if param.type == "bool":
            box = QCheckBox()
            box.setChecked(bool(current))
            return box
        edit = QLineEdit()
        if current is not None:
            edit.setText(str(current))
        if param.default is not None:
            edit.setPlaceholderText(f"default: {param.default}")
        return edit

    def accept(self):
        bindings = {}
        for name, widget in self._mapping_widgets.items():
            if isinstance(widget, _EntryField):
                value = widget.value()
                if value is not None:
                    bindings[name] = value
                continue
            if isinstance(widget, _MultiSelect):
                # Stored even when empty: "I deliberately chose none" and "I never opened
                # this" are different, and only the second should read as unbound.
                bindings[name] = widget.chosen()
                continue
            value = widget.currentData()
            if value is not None:
                bindings[name] = value

        config = {}
        for name, (widget, param) in self._config_widgets.items():
            if isinstance(widget, QCheckBox):
                config[name] = widget.isChecked()
                continue
            text = widget.text().strip()
            if text == "":
                continue
            if param.type in ("number", "int", "float"):
                try:
                    config[name] = int(text)
                except ValueError:
                    try:
                        config[name] = float(text)
                    except ValueError:
                        QMessageBox.warning(self, "Invalid value",
                                            f"'{name}' expects a number.")
                        return
            else:
                config[name] = text

        self.result_bindings = bindings
        self.result_config = config
        self.result_on_error = self._on_error.currentData()
        super().accept()


# --- the job editor tab -----------------------------------------------------

class JobEditorTab(QWidget):
    """A workspace tab for building one job: its steps, their order, their state."""

    changed = Signal()             # the job was modified; panels should refresh
    run_requested = Signal(object)  # Job

    def __init__(self, job, *, job_store, package_index, tag_store, parent=None,
                 blueprint_store=None, packdump=None):
        super().__init__(parent)
        self._job_id = job.id
        self._jobs = job_store
        self._packages = package_index
        self._tags = tag_store
        self._blueprints = blueprint_store
        self._dump = packdump

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        header = QHBoxLayout()
        self._title = QLabel()
        self._title.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {style.TEXT};")
        header.addWidget(self._title)
        header.addSpacing(16)
        header.addWidget(QLabel("On failure:"))
        self._default_policy = QComboBox()
        self._default_policy.addItem("halt", "halt")
        self._default_policy.addItem("skip", "skip")
        self._default_policy.currentIndexChanged.connect(self._on_policy_changed)
        header.addWidget(self._default_policy)
        header.addStretch()
        run = QPushButton("▶  Run job")
        run.setStyleSheet(f"""
            QPushButton {{
                background: #24402a; color: #8fd39a; border: 1px solid #4a8055;
                padding: 3px 14px; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ background: #2d5034; }}
        """)
        run.clicked.connect(lambda: self.run_requested.emit(self.job()))
        header.addWidget(run)
        root.addLayout(header)

        self._tree = PanelTree()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["#", "Step", "Configured", "On failure"])
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS + f"""
            QHeaderView::section {{
                background: {style.BG_PANEL}; color: {style.TEXT_FAINT};
                border: none; border-bottom: 1px solid {style.BORDER};
                padding: 3px 6px; font-size: 11px;
            }}
        """)
        self._tree.itemDoubleClicked.connect(lambda *_: self._edit_step())
        root.addWidget(self._tree)

        buttons = QHBoxLayout()
        for label, slot in (("＋ Action step", self._add_action_step),
                            ("＋ Job step", self._add_job_step),
                            ("Edit…", self._edit_step),
                            ("Remove", self._remove_step),
                            ("↑", lambda: self._move(-1)),
                            ("↓", lambda: self._move(+1))):
            btn = QPushButton(label)
            btn.setFixedHeight(24)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                    border: 1px solid {style.BORDER}; padding: 2px 10px; font-size: 12px;
                }}
                QPushButton:hover {{ color: {style.TEXT}; border-color: {style.ACCENT_EDGE}; }}
            """)
            btn.clicked.connect(slot)
            buttons.addWidget(btn)
        buttons.addStretch()
        root.addLayout(buttons)

        self.refresh()

    # --- state -------------------------------------------------------------

    def job(self):
        return self._jobs.get(self._job_id)

    def refresh(self):
        job = self.job()
        if job is None:
            return
        self._title.setText(job.name)
        self._default_policy.blockSignals(True)
        self._default_policy.setCurrentIndex(self._default_policy.findData(job.default_on_error))
        self._default_policy.blockSignals(False)

        self._tree.clear()
        for index, step in enumerate(job.steps, start=1):
            if step.is_action:
                what = step.action_ref
                configured = ", ".join(f"{k} → {self._binding_label(step, k, v)}"
                                       for k, v in sorted(step.bindings.items()))
                if step.config:
                    extra = ", ".join(f"{k}={v}" for k, v in sorted(step.config.items()))
                    configured = f"{configured}   [{extra}]" if configured else f"[{extra}]"
            else:
                referenced = self._jobs.get(step.ref_job_id)
                what = f"job: {referenced.name if referenced else '(missing job)'}"
                configured = ""
            policy = step.on_error or f"({job.default_on_error})"
            item = QTreeWidgetItem([str(index), what, configured or "—", policy])
            item.setData(0, Qt.UserRole, step)
            # 3.2.2: a step whose mapping no longer validates "is flagged as needing
            # attention". It already refuses to run — this is what stops that being a
            # surprise at run time, long after the schema edit that caused it.
            reasons = self._step_problems(step)
            if reasons:
                item.setText(1, f"⚠ {what}")
                item.setForeground(1, style.qt_colour(style.ERROR))
                item.setToolTip(1, "\n".join(reasons))
            self._tree.addTopLevelItem(item)
        for col in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(col)

    def _binding_label(self, step, mapping_name, stored):
        """A binding is stored as an id; a human needs the name. Falls back to a visible
        marker rather than a bare number when the artifact is gone."""
        try:
            slot = self._packages.get(step.action_ref).mappings.get(mapping_name)
        except (KeyError, ValueError):
            slot = None
        if slot is None:
            return stored
        items = stored if isinstance(stored, list) else [stored]
        labels = [binding_name(slot, i, tag_store=self._tags,
                               blueprint_store=self._blueprints) or "(deleted)"
                  for i in items]
        return ", ".join(labels) if isinstance(stored, list) else labels[0]

    def _step_problems(self, step) -> list:
        """Why this step won't run, as sentences — empty when it's fine."""
        if not step.is_action or self._packages is None or self._blueprints is None:
            return []
        try:
            manifest = self._packages.get(step.action_ref)
        except (KeyError, ValueError):
            return [f"'{step.action_ref}' is not installed"]
        problems = []
        for name, slot in manifest.mappings.items():
            bound = step.bindings.get(name)
            if bound is None or bound == []:
                if slot.required:
                    problems.append(f"'{name}' is unbound")
                continue
            if slot.kind in ("blueprint", "blueprint_instance"):
                for item in (bound if isinstance(bound, list) else [bound]):
                    label = binding_name(slot, item, tag_store=self._tags,
                                         blueprint_store=self._blueprints)
                    if label is None:
                        problems.append(
                            f"'{name}' points at something that no longer exists")
                        continue
                    problems += [f"'{name}': {why}" for why in
                                 mapping_mismatches(slot, label, self._blueprints)]
            else:
                if binding_name(slot, bound, tag_store=self._tags,
                                blueprint_store=self._blueprints) is None:
                    problems.append(
                        f"'{name}' points at something that no longer exists")
        return problems

    def _selected_step(self):
        item = self._tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def _touched(self):
        self.refresh()
        self.changed.emit()

    # --- actions -----------------------------------------------------------

    def _on_policy_changed(self):
        self._jobs.set_default_on_error(self._job_id, self._default_policy.currentData())
        self._touched()

    def _add_action_step(self):
        picker = ActionPickerDialog(self._packages, self)
        if not picker.exec():
            return
        step = self._jobs.add_action_step(self._job_id, picker.result_ref)
        self._touched()
        # Go straight into configuring it — an unbound required mapping can't run.
        self._edit_specific_step(step)

    def _add_job_step(self):
        candidates = [j for j in self._jobs.all()
                      if j.id != self._job_id and not self._jobs._reaches(j.id, self._job_id)]
        picker = JobPickerDialog(candidates, self)
        if not picker.exec() or picker.result_job_id is None:
            return
        try:
            self._jobs.add_job_step(self._job_id, picker.result_job_id)
        except ValueError as e:
            QMessageBox.warning(self, "Can't nest that job", str(e))
            return
        self._touched()

    def _edit_step(self):
        step = self._selected_step()
        if step is not None:
            self._edit_specific_step(step)

    def _edit_specific_step(self, step):
        if not step.is_action:
            QMessageBox.information(self, "Job step",
                                    "A job step just runs another job — set its failure "
                                    "policy from the job it belongs to.")
            return
        try:
            manifest = self._packages.get(step.action_ref)
        except KeyError:
            QMessageBox.warning(self, "Action missing",
                                f"'{step.action_ref}' isn't installed in this profile.")
            return
        dlg = StepEditorDialog(manifest, self._tags, step, self,
                               blueprint_store=self._blueprints,
                               packdump=self._dump)
        if not dlg.exec():
            return
        self._jobs.update_step(step.id, bindings=dlg.result_bindings,
                               config=dlg.result_config, on_error=dlg.result_on_error)
        self._touched()

    def _remove_step(self):
        step = self._selected_step()
        if step is None:
            return
        self._jobs.remove_step(step.id)
        self._touched()

    def _move(self, delta):
        step = self._selected_step()
        job = self.job()
        if step is None or job is None:
            return
        ids = [s.id for s in job.steps]
        i = ids.index(step.id)
        target = i + delta
        if not (0 <= target < len(ids)):
            return
        ids[i], ids[target] = ids[target], ids[i]
        self._jobs.reorder_steps(self._job_id, ids)
        self._touched()
        self._tree.setCurrentItem(self._tree.topLevelItem(target))
