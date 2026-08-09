"""Authoring jobs: the editor tab, the step editor, and the action picker (design 3.3.2).

The step editor follows §3.3.2 literally: *"one unified panel of what this step needs.
Mappings and configuration are presented together as a single list of slots to fill…
The architectural distinction between mappings and configuration is invisible to the
user; they just fill in what the action asks for."* So both kinds of slot render as rows
of one form — artifact pickers for mappings, inline editors for config — and nothing in
the UI names the distinction.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QCheckBox, QTreeWidget, QTreeWidgetItem, QDialogButtonBox,
    QAbstractItemView, QMessageBox, QListWidget, QListWidgetItem,
)

from packsmith.gui.shell import style

_INHERIT = "(inherit from job)"


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


# --- editing one step -------------------------------------------------------

class StepEditorDialog(QDialog):
    """Fill in what a step needs. Mappings and config are one list of slots."""

    def __init__(self, manifest, tag_store, step, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Step — {manifest.name or manifest.action_id}")
        self.setMinimumWidth(480)
        self._manifest = manifest
        self._tags = tag_store
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
        """An artifact picker: the user's own tags of the type this slot asks for."""
        combo = QComboBox()
        if not slot.required:
            combo.addItem("(unbound)", None)
        definitions = self._tags.definitions_for(slot.registry_type) if slot.registry_type else {}
        for tag_name, definition in sorted(definitions.items()):
            if slot.tag_type and definition["type"] != slot.tag_type:
                continue
            combo.addItem(tag_name, tag_name)
        if combo.count() == 0:
            combo.addItem(f"(no {slot.tag_type or ''} tags on {slot.registry_type})", None)
            combo.setEnabled(False)
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        return combo

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
        for name, combo in self._mapping_widgets.items():
            value = combo.currentData()
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

    def __init__(self, job, *, job_store, package_index, tag_store, parent=None):
        super().__init__(parent)
        self._job_id = job.id
        self._jobs = job_store
        self._packages = package_index
        self._tags = tag_store

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

        self._tree = QTreeWidget()
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
                configured = ", ".join(f"{k} → {v}" for k, v in sorted(step.bindings.items()))
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
            self._tree.addTopLevelItem(item)
        for col in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(col)

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
        dlg = StepEditorDialog(manifest, self._tags, step, self)
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
