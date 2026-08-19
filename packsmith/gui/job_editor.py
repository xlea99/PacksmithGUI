"""Authoring jobs: the editor tab, the step editor, and the action picker (design 3.3.2).

The step editor follows §3.3.2 literally: *"one unified panel of what this step needs.
Mappings and configuration are presented together as a single list of slots to fill…
The architectural distinction between mappings and configuration is invisible to the
user; they just fill in what the action asks for."* So both kinds of slot render as rows
of one form — artifact pickers for mappings, inline editors for config — and nothing in
the UI names the distinction.
"""
import json
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QCheckBox, QTreeWidget, QTreeWidgetItem, QDialogButtonBox,
    QAbstractItemView, QMessageBox, QListWidget, QListWidgetItem, QMenu, QScrollArea,
    QSplitter, QFrame, QHeaderView,
)

from packsmith.core.bindings import (
    best_guess_bindings, binding_id, binding_name, mapping_mismatches, record_names,
    stale_bindings, step_problems)
from packsmith.core.shapes import describe_shape
from packsmith.gui.shell import icons, style
from packsmith.gui.shell.picker import Choice, PickerPopup, _token_match
from packsmith.gui.shell.tree import PanelTree
from packsmith.gui.shell.dropdown import DropDown

_INHERIT = "(inherit from job)"


def _ago(stamp: str) -> str:
    """"3m ago" from an ISO timestamp. Relative because the question the editor answers is
    *did this run since I last changed it* — and "4 minutes ago" answers that at a glance
    where "2026-08-16 13:42:07" makes you do the arithmetic. The exact time stays in the
    tooltip for when it is the exact time you want."""
    if not stamp:
        return ""
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 0:
        return "just now"               # clock skew; never render a negative age
    for limit, divisor, unit in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h")):
        if seconds < limit:
            return f"{int(seconds // divisor)}{unit} ago"
    return f"{int(seconds // 86400)}d ago"


def _changed_count(row: dict) -> str:
    """How much the last run of this step actually did.

    Counted from the **change record** rather than the inverse, and `unchanged` excluded —
    so a re-run that asserted 200 already-correct cells reads as "no changes", which is
    how you learn a step is idempotent instead of thinking it did 200 things again.
    """
    try:
        stored = json.loads(row.get("rollback_data") or "{}")
    except (TypeError, ValueError):
        return ""
    # Shape-checked, not just parse-checked. This reads a blob off disk, and valid JSON
    # that isn't an object (`null` is the easy one) would raise straight through a
    # parse-only guard and take the whole tab down over a cosmetic column.
    if not isinstance(stored, dict):
        return ""
    changes = stored.get("changes")
    if changes is None:
        return ""           # a row written before change records existed: nothing to say
    # An empty list and an all-`unchanged` list are the same answer, and it is a useful
    # one: the step ran and the pack did not move. That is how you find out a job is
    # idempotent, so it is worth a phrase rather than a blank cell — which would read as
    # "no record" and is a different fact entirely.
    real = [c for c in changes if c.get("kind") != "unchanged"]
    if not real:
        return "no changes"
    return f"{len(real)} change{'s' if len(real) != 1 else ''}"


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


def _select_stored(combo, stored):
    """Select the row a stored binding refers to, by id or by legacy name.

    `findData` alone missed every legacy name-binding and fell through to index 0 — which
    is not "nothing selected", it is *the alphabetically first artifact*, so pressing OK
    retargeted the step to whatever happened to sort first.

    When nothing matches, the combo is marked **unconfirmed**: it still shows a row, because
    a combo box has no empty state, but it renders dimmed and its value is not the step's
    binding. See `_mark_unconfirmed`.
    """
    for i in range(combo.count()):
        if _refers_to(stored, combo.itemData(i), combo.itemText(i)):
            combo.setCurrentIndex(i)
            _mark_unconfirmed(combo, False)
            return
    combo.setCurrentIndex(0)
    # Only a value the user could mistake for a choice needs the treatment: an explicit
    # "(unbound)" row, or an empty picker, already says what it is.
    _mark_unconfirmed(combo, combo.currentData() is not None)


def _mark_unconfirmed(combo, unconfirmed: bool):
    """Dim a suggestion the user has not accepted, and undim it once they have."""
    if combo.property("unconfirmed") == bool(unconfirmed):
        return
    combo.setProperty("unconfirmed", bool(unconfirmed))
    # A dynamic property already set when the stylesheet was applied needs the widget
    # repolished, or Qt keeps painting the old rule.
    combo.style().unpolish(combo)
    combo.style().polish(combo)


class _EntryField(QWidget):
    """Bind one Layer 1 entry: a typed field with a browse button.

    Not a QComboBox — `minecraft:item` is 14,000 entries on a real pack, which is the exact
    case `PickerPopup` was built for. And the field stays typeable: §5.3's rule that a
    recommendation "must never restrict" applies just as much to a picker as to a template.
    """

    changed = Signal()

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
        self._edit.editingFinished.connect(self.changed)
        row.addWidget(self._edit, 1)
        browse = QPushButton("…", self)
        browse.setFixedWidth(28)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)

    def _browse(self):
        picker = PickerPopup(self._entries, header=self._registry_type, parent=self)
        picker.chosen.connect(self._edit.setText)
        picker.chosen.connect(lambda *_: self.changed.emit())
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

    changed = Signal()

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
        # Connected after the rows are built, so populating the list doesn't read as a
        # hundred separate edits by the user.
        self._list.itemChanged.connect(lambda *_: self.changed.emit())
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

class StepForm(QWidget):
    """The slots one step needs, as a form. Mappings and config are one list.

    Kept apart from :class:`StepPanel` — the chrome around it — because this is where every
    binding decision lives: which artifacts are candidates, what a legacy name-binding
    resolves to, what gets stored. That is worth being able to test without building a
    window around it.
    """

    # Emitted when a field is committed — a combo changed, a line edit finished, a box
    # ticked. Not on every keystroke: a half-typed number is not an edit yet.
    committed = Signal()

    def __init__(self, manifest, tag_store, step, parent=None, blueprint_store=None,
                 packdump=None, pack_targets=None):
        super().__init__(parent)
        self._dump = packdump
        self._pack_targets = pack_targets
        self._manifest = manifest
        self._tags = tag_store
        self._blueprints = blueprint_store
        self._mapping_widgets = {}
        self._labels = {}       # slot or config name -> (QLabel, name, description, required)
        self._config_widgets = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignLeft)
        # The panel is narrow, so labels sit ABOVE their fields rather than beside them —
        # otherwise a long slot name and its picker fight over the same 300px.
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)

        # One list of slots — mappings and config together, in declaration order.
        for name, slot in manifest.mappings.items():
            widget = self._mapping_widget(name, slot, step.bindings.get(name))
            self._mapping_widgets[name] = widget
            label = self._label_for(name, slot.description, slot.required)
            self._labels[name] = (label, name, slot.description, slot.required)
            form.addRow(label, widget)

        for name, param in manifest.config.items():
            current = step.config.get(name, param.default)
            widget = self._config_widget(param, current)
            self._config_widgets[name] = (widget, param)
            label = self._label_for(name, param.description, param.required)
            self._labels[name] = (label, name, param.description, param.required)
            form.addRow(label, widget)

        if not manifest.mappings and not manifest.config:
            nothing = QLabel("This action needs nothing configured.")
            nothing.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
            form.addRow(nothing)
        root.addLayout(form)

        # Error policy is step state, not action state (design 3.3.2, Model B).
        policy_label = QLabel("If this step fails")
        policy_label.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        root.addWidget(policy_label)
        self._on_error = DropDown()
        self._on_error.addItem(_INHERIT, None)
        self._on_error.addItem("halt — stop the job", "halt")
        self._on_error.addItem("skip — carry on", "skip")
        index = self._on_error.findData(step.on_error)
        self._on_error.setCurrentIndex(max(0, index))
        root.addWidget(self._on_error)

        self._wire_commits()
        self.committed.connect(self._refresh_labels)
        self._refresh_labels()

    def _wire_commits(self):
        """Every field reports when it settles, so the panel can write it through.

        `editingFinished` rather than `textChanged` for text: typing "1" on the way to "12"
        should not be persisted and then complained about.
        """
        widgets = ([w for w in self._mapping_widgets.values()]
                   + [w for w, _ in self._config_widgets.values()]
                   + [self._on_error])
        for widget in widgets:
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self.committed)
                # `activated` fires for any user pick, INCLUDING re-picking the row that is
                # already current — which `currentIndexChanged` does not, because the index
                # did not move. That is the whole of the "I clicked the recommendation and
                # nothing happened" bug: the one row you most want to choose is the one row
                # that emitted nothing.
                widget.activated.connect(
                    lambda _index, w=widget: (_mark_unconfirmed(w, False), self.committed.emit()))
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self.committed)
            elif isinstance(widget, QLineEdit):
                widget.editingFinished.connect(self.committed)
            elif isinstance(widget, (_EntryField, _MultiSelect)):
                widget.changed.connect(self.committed)

    def _label_for(self, name, description, required, unbound=True):
        """What this slot is called, and whether it still needs you.

        Optionality used to be visible only when a step refused to run. A **required** slot
        with nothing bound wears a red asterisk; once satisfied the asterisk goes, because
        a form that shouts at every required field shouts at a finished one too. An
        **optional** one says so quietly, in italic, because "you may skip this" is
        permission rather than an instruction.
        """
        label = QLabel()
        label.setTextFormat(Qt.RichText)
        self._paint_label(label, name, description, required, unbound)
        return label

    @staticmethod
    def _paint_label(label, name, description, required, unbound):
        if required:
            mark = (f' <span style="color: {style.ERROR};">*</span>') if unbound else ""
            label.setText(f"{name}{mark}")
            label.setToolTip((description + "\n\n" if description else "")
                             + ("Required — this step cannot run until it is bound."
                                if unbound else "Required."))
            return
        label.setText(
            f'{name} <span style="color: {style.TEXT_FAINT};"><i>(optional)</i></span>')
        label.setToolTip((description + "\n\n" if description else "")
                         + "Optional — the action handles it being left unbound.")

    def _refresh_labels(self):
        """Re-mark the labels after a commit, so an asterisk clears the moment its slot is
        satisfied rather than at the next time the panel is rebuilt."""
        try:
            bindings, config, _on_error = self.read()
        except ValueError:
            return              # a field is mid-edit and invalid; its label can wait
        for key, (label, name, description, required) in self._labels.items():
            widget = self._mapping_widgets.get(key)
            value = bindings.get(key) if widget is not None else config.get(key)
            unbound = value is None or value == [] or value == ""
            # A picker showing a value the user has not accepted is NOT bound — `read()`
            # reports what is displayed, and the step stores nothing until it is chosen.
            # The two marks then say the same thing from opposite ends: the value is dimmed
            # because it is a suggestion, and the asterisk stays because nothing is bound.
            if widget is not None and widget.property("unconfirmed"):
                unbound = True
            self._paint_label(label, name, description, required, unbound)

    def _mapping_widget(self, name, slot, current):
        """An artifact picker: the user's own artifacts of the kind this slot asks for."""
        if slot.cardinality == "many":
            # 3.3: "many — zero or more artifacts (UI: multi-select list)".
            return _MultiSelect(self._candidates_for(slot), current)

        combo = DropDown()
        if not slot.required:
            combo.addItem("(unbound)", None)

        if slot.kind == "pack":
            # 3.3: a pack is bound like any other artifact, so this is the same picker
            # gesture as choosing a tag — and the same one the JAR viewer's save-as-override
            # prompt already uses, because "which pack does this go into" is one question
            # whether a person or an action is asking it.
            packs = self._packs_for(slot)
            if packs is None:
                combo.addItem("(no pack loader installed — see design 8.1)", None)
                combo.setEnabled(False)
                return combo
            for pack_name in packs:
                combo.addItem(pack_name, pack_name)
            if not packs:
                combo.addItem(f"(no {slot.pack_kind[:-1]}s yet)", None)
                combo.setEnabled(False)
            # Listed in LOAD ORDER, and later packs win — the same warning the override
            # dialog carries, for the same reason.
            combo.setToolTip("Listed in load order — later packs override earlier ones")
            _select_stored(combo, current)
            return combo

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

    def _packs_for(self, slot):
        """Pack names in load order, or None when no loader provides this kind at all.

        None and [] have to stay distinct all the way to the picker: "install a loader" and
        "make your first datapack" are different instructions, and collapsing them would
        send the user looking in the wrong place.
        """
        if self._pack_targets is None:
            return None
        try:
            return self._pack_targets.available(slot.pack_kind)
        except OSError:
            return []

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
        if slot.kind == "pack":
            return [(p, p, []) for p in (self._packs_for(slot) or [])]
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

    def read(self):
        """``(bindings, config, on_error)`` as the widgets currently stand.

        Raises :class:`ValueError` for a value the action cannot accept. Raising rather
        than warning in a message box is what lets the panel report it inline and simply
        not persist that field — a modal complaint per keystroke-ish commit would be
        unusable in a surface you edit continuously.
        """
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
                        raise ValueError(f"'{name}' expects a number.")
            else:
                config[name] = text

        return bindings, config, self._on_error.currentData()


class StepPanel(QWidget):
    """The selected step's controls, docked beside the step list.

    This replaces a modal dialog, and the reason is the shape of the work rather than
    taste: configuring a step is not a decision you make once and dismiss — it is where
    you spend the iteration, tweaking a binding and running the step again. A modal makes
    that a sequence of round trips through a window that hides the list you are working
    against, and it cannot be open while you look at anything else.

    **Edits are written as they are committed**, with no OK button. A docked panel has no
    natural moment to press one, and an Apply you can forget is how edits get lost. Each
    field persists when it settles, which is what every property panel in every IDE does.
    """

    changed = Signal()                      # the step was written; refresh the row
    status = Signal(str)
    picked = Signal(str)                    # a value chosen in picking mode
    pick_cancelled = Signal()

    def __init__(self, *, job_store, tag_store, package_index, blueprint_store=None,
                 packdump=None, pack_targets=None, parent=None):
        super().__init__(parent)
        self._jobs = job_store
        self._tags = tag_store
        self._packages = package_index
        self._blueprints = blueprint_store
        self._dump = packdump
        self._pack_targets = pack_targets
        self._step_id = None
        self._form = None
        self._picker = None
        self._writing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setTextFormat(Qt.RichText)
        self._title.setStyleSheet(f"font-size: 13px; color: {style.TEXT};")
        root.addWidget(self._title)

        self._subtitle = QLabel()
        self._subtitle.setWordWrap(True)
        self._subtitle.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        root.addWidget(self._subtitle)

        self._notice = QLabel()
        self._notice.setWordWrap(True)
        self._notice.hide()
        root.addWidget(self._notice)

        self._relink = QPushButton("Relink this step")
        self._relink.setToolTip("Confirm this step still means what you want it to mean")
        self._relink.clicked.connect(self._do_relink)
        self._relink.hide()
        root.addWidget(self._relink)

        self._host = QWidget()
        self._host_layout = QVBoxLayout(self._host)
        self._host_layout.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._host)
        root.addStretch()

        self.set_step(None)

    # --- what is being edited ---------------------------------------------

    @property
    def step_id(self):
        return self._step_id

    @property
    def picking(self) -> bool:
        return self._picker is not None

    def show_picker(self, header, choices, *, empty=""):
        """Turn the panel into a chooser.

        Adding a step is the one thing here that still opened a modal, and it was the worst
        candidate for one: a flat unfiltered list is unusable at a few hundred jobs, and the
        dialog covered the very list you were adding to. The picker that already handles
        14,000 registry ids does this properly — token matching, a windowful of rows at a
        time — and it has an inline mode, so it can simply *be* the panel for a moment.

        Nothing is created until something is chosen. A step that exists with no action yet
        would be a broken step: flagged, and blocking the whole job on pre-flight, for as
        long as it took you to get distracted.
        """
        self._clear_form()
        self._clear_picker()
        self._step_id = None
        self._notice.hide()
        self._relink.hide()
        self._title.setText(f"<b>{header}</b>")
        self._subtitle.setText("Type to narrow · Enter to add · Esc to cancel"
                               if choices else empty)
        self._subtitle.show()
        if not choices:
            return
        self._picker = PickerPopup(choices, inline=self._host, placeholder="type to narrow")
        self._picker.chosen.connect(self._on_picked)
        self._picker.dismissed.connect(self._on_pick_cancelled)
        self._host_layout.addWidget(self._picker)
        self._picker.show()
        self._picker.focus_filter()

    def _clear_picker(self, *, cancelled=False):
        """Drop the chooser, and **say so** when dropping it means the pick is off.

        The panel and the tab were each tracking "are we picking" — the panel by holding a
        picker, the tab by remembering which kind. Anything that reached `set_step` (a click
        on a step, a click on empty space) cleared the picker without the tab hearing, and
        the two then disagreed: no chooser on screen, but the tab still in picking mode with
        its placeholder row stranded in the list. One owner announces the change instead.
        """
        had = self._picker is not None
        if had:
            self._picker.setParent(None)
            self._picker.deleteLater()
            self._picker = None
        if had and cancelled:
            self.pick_cancelled.emit()

    def _on_picked(self, value):
        # Cleared before the signal: the handler creates a step and asks this panel to show
        # it, and the picker must be gone by then or it would sit above the new form.
        self._clear_picker()
        self.picked.emit(value)

    def _on_pick_cancelled(self):
        self._clear_picker()
        self.pick_cancelled.emit()

    def set_packdump(self, packdump):
        """Adopt a new dump (design 3.1). The form is rebuilt rather than repointed: its
        registry pickers were populated FROM the old dump, so swapping the reference alone
        would leave 14,000 entries from a registry the game no longer has."""
        self._dump = packdump
        showing, self._step_id = self._step_id, None    # force a real rebuild
        self.set_step(self._find_step(showing))

    def set_step(self, step):
        """Show this step, or the empty state when there isn't one.

        Re-showing the step already on screen keeps the existing widgets. Rebuilding them
        would be actively hostile here: the panel writes as you edit, every write refreshes
        the row, and the refresh reselects the step — so a rebuild-on-show would destroy
        the combo box you just used, mid-use, on every single change.
        """
        self._clear_picker(cancelled=True)
        if (step is not None and self._form is not None
                and step.id == self._step_id and step.is_action):
            self._notice.hide()
            self._relink.hide()
            self._show_relink_if_owed(step)
            return
        self._step_id = step.id if step is not None else None
        self._clear_form()
        self._notice.hide()
        self._relink.hide()

        if step is None:
            self._title.setText("<b>No step selected</b>")
            self._subtitle.setText("Pick a step on the left to configure it.")
            return
        if not step.is_action:
            referenced = self._jobs.get(step.ref_job_id)
            self._title.setText("<b>Job step</b>")
            self._subtitle.setText(
                f"Runs '{referenced.name}' in place." if referenced else
                "The job this step referenced no longer exists.")
            return
        try:
            manifest = self._packages.get(step.action_ref)
        except (KeyError, ValueError):
            self._title.setText(f"<b>{step.action_ref}</b>")
            self._notice.setText(f"'{step.action_ref}' isn't installed in this profile, so "
                                 f"there is nothing to configure. The step will refuse to "
                                 f"run until the package is installed or the step removed.")
            self._notice.setStyleSheet(f"color: {style.ERROR}; font-size: 11px;")
            self._notice.show()
            self._subtitle.clear()
            return

        self._title.setText(f"<b>{manifest.name or manifest.action_id}</b>"
                            f"<span style='color:{style.TEXT_FAINT}'>  {manifest.ref}</span>")
        self._subtitle.setText(manifest.description or "")
        self._subtitle.setVisible(bool(manifest.description))

        self._form = StepForm(manifest, self._tags, step, blueprint_store=self._blueprints,
                              packdump=self._dump, pack_targets=self._pack_targets)
        self._form.committed.connect(self._write)
        self._host_layout.addWidget(self._form)
        self._show_relink_if_owed(step)

    def _clear_form(self):
        if self._form is not None:
            self._form.setParent(None)
            self._form.deleteLater()
            self._form = None

    def _show_relink_if_owed(self, step):
        """§3.2.1's relink, as an explicit button rather than a side effect.

        The dialog this replaces cleared every relink the step owed the moment you pressed
        OK, whether or not you had touched the slot in question — "saving is an assertion"
        applied to the whole step at once. A panel has no OK, so the assertion gets its own
        control, which is also more honest: confirming a renamed tag still means what you
        want is a decision, not a side effect of having opened something.
        """
        job = self._jobs.get(step.job_id)
        if job is None or self._packages is None:
            return
        try:
            owed = [s for s in stale_bindings(job, package_index=self._packages,
                                              tag_store=self._tags)
                    if s.step_id == step.id]
        except Exception:
            return
        if not owed:
            return
        detail = "; ".join(f"'{s.slot}' was bound to '{s.was}', now called '{s.now}'"
                           for s in owed)
        self._notice.setText(f"This step won't run until you confirm it: {detail}")
        self._notice.setStyleSheet(f"color: {style.WARNING}; font-size: 11px;")
        self._notice.show()
        self._relink.show()

    def _do_relink(self):
        step = self._current_step()
        if step is None:
            return
        manifest = self._packages.get(step.action_ref)
        self._jobs.relink_step(step.id, record_names(manifest, step.bindings,
                                                     tag_store=self._tags))
        self.changed.emit()
        self.status.emit("Step relinked — it will run as bound")
        self.set_step(self._jobs.get(step.job_id) and self._current_step())

    def _current_step(self):
        return self._find_step(self._step_id)

    def _find_step(self, step_id):
        """Re-read the step from the store rather than holding the one we were handed —
        it is a frozen snapshot, and every write here makes the copy we have stale."""
        if step_id is None:
            return None
        for job in self._jobs.all():
            for step in job.steps:
                if step.id == step_id:
                    return step
        return None

    # --- persisting -------------------------------------------------------

    def _write(self):
        """A field settled: put it in the database."""
        if self._writing or self._form is None:
            return
        step = self._current_step()
        if step is None:
            return
        try:
            bindings, config, on_error = self._form.read()
        except ValueError as e:
            self._notice.setText(str(e))
            self._notice.setStyleSheet(f"color: {style.ERROR}; font-size: 11px;")
            self._notice.show()
            self.status.emit(str(e))
            return
        if (bindings, config, on_error) == (step.bindings, step.config, step.on_error):
            return                          # nothing moved; don't churn the row
        self._writing = True
        try:
            self._jobs.update_step(step.id, bindings=bindings, config=config,
                                   on_error=on_error,
                                   bound_names=self._names_after(step, bindings))
        finally:
            self._writing = False
        self.changed.emit()

    def _names_after(self, step, bindings) -> dict:
        """What each bound tag is called, recorded only for the slots that actually moved.

        The dialog re-recorded everything on OK, so opening a step and pressing OK silently
        settled a relink you never looked at. Here, changing a slot asserts *that* slot —
        which is what the gesture actually means — and any other slot goes on owing its
        relink until the button above says otherwise.
        """
        manifest = self._packages.get(step.action_ref)
        fresh = record_names(manifest, bindings, tag_store=self._tags)
        merged = dict(step.bound_names)
        for slot_name, recorded in fresh.items():
            if bindings.get(slot_name) != step.bindings.get(slot_name):
                merged[slot_name] = recorded
        # A slot that is no longer bound has no name to remember.
        for slot_name in list(merged):
            if slot_name not in bindings:
                merged.pop(slot_name)
        return merged


# --- the job editor tab -----------------------------------------------------

class JobEditorTab(QWidget):
    """A workspace tab for building one job: its steps, their order, their state."""

    changed = Signal()             # the job was modified; panels should refresh
    run_requested = Signal(object)  # Job
    dry_run_requested = Signal(object)  # Job — walk it, promote nothing
    # (Job, step_id, dry) — run ONE step. The authoring loop: iterating on step 4 of 6
    # should not mean executing, and re-applying, steps 1 to 3 every time.
    # (Job, step_id, dry, through). `through` runs the job's first N steps rather than
    # the one step alone — see `run_job` for why those answer different questions.
    step_run_requested = Signal(object, int, bool, bool)
    status = Signal(str)           # a line for the status bar, from the step panel
    action_info_requested = Signal(str)    # action ref — open its reference page

    def __init__(self, job, *, job_store, package_index, tag_store, parent=None,
                 blueprint_store=None, packdump=None, pack_targets=None, history=None):
        super().__init__(parent)
        self._job_id = job.id
        self._jobs = job_store
        self._packages = package_index
        self._tags = tag_store
        self._blueprints = blueprint_store
        self._dump = packdump
        self._pack_targets = pack_targets
        # Optional: without it the Last run column simply stays empty, which is the honest
        # rendering of "this tab has no history to read" rather than a reason to refuse.
        self._history = history

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        header = QHBoxLayout()
        self._title = QLabel()
        self._title.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {style.TEXT};")
        header.addWidget(self._title)
        header.addSpacing(16)
        header.addWidget(QLabel("On failure:"))
        self._default_policy = DropDown()
        self._default_policy.addItem("halt", "halt")
        self._default_policy.addItem("skip", "skip")
        self._default_policy.currentIndexChanged.connect(self._on_policy_changed)
        header.addWidget(self._default_policy)
        header.addStretch()
        dry = QPushButton()
        icons.mark(dry, "dry_run", text="Dry run")
        dry.setToolTip("Walk every step and change nothing")
        dry.setStyleSheet(f"""
            QPushButton {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER};
                padding: 3px 14px; font-size: 12px;
            }}
            QPushButton:hover {{ color: {style.TEXT};
                                 border-color: {style.ACCENT_EDGE}; }}
        """)
        dry.clicked.connect(lambda: self.dry_run_requested.emit(self.job()))
        header.addWidget(dry)

        run = QPushButton()
        icons.mark(run, "play", text="Run job")
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
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(["#", "Step", "Configured", "On failure", "Last run"])
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS + f"""
            QHeaderView::section {{
                background: {style.BG_PANEL}; color: {style.TEXT_FAINT};
                border: none; border-bottom: 1px solid {style.BORDER};
                padding: 3px 6px; font-size: 11px;
            }}
        """)
        # Sizing every column to its contents overflowed the tree once the step panel took
        # a third of the width, and what fell off the right was **Last run** — the column
        # added precisely so you would not have to go looking for it. One column has to
        # absorb the slack instead, and `Configured` is the one that can: it is the longest,
        # and it is the only one the panel beside it also spells out in full.
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)   # #
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)   # Step
        header.setSectionResizeMode(2, QHeaderView.Stretch)            # Configured
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)   # On failure
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)   # Last run
        self._tree.setTextElideMode(Qt.ElideRight)

        self._loading = False
        # Rebuilds that must not happen inside a signal — see `_set_step_enabled`.
        self._pending_select = None
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.timeout.connect(self._deferred_rebuild)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.currentItemChanged.connect(self._on_selection_changed)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        buttons = QHBoxLayout()
        # "Edit…" is gone with the dialog it opened: selecting a step IS editing it now.
        for label, slot in (("＋ Action step", self._add_action_step),
                            ("＋ Job step", self._add_job_step),
                            ("Remove", self._remove_step),
                            (icons.ui("up") or "↑", lambda: self._move(-1)),
                            (icons.ui("down") or "↓", lambda: self._move(+1))):
            btn = QPushButton(label)
            if label in (icons.ui("up"), icons.ui("down")) and icons.family():
                btn.setFont(icons.icon_font(12))
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

        # The list and its buttons on the left, the selected step's controls on the right.
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(self._tree)
        left_layout.addLayout(buttons)

        self._panel = StepPanel(job_store=job_store, tag_store=tag_store,
                                package_index=package_index,
                                blueprint_store=blueprint_store, packdump=packdump,
                                pack_targets=pack_targets)
        self._panel.changed.connect(self._on_panel_changed)
        self._panel.status.connect(self.status)
        self._panel.picked.connect(self._on_picked)
        self._panel.pick_cancelled.connect(self._end_pick)
        self._picking = None        # "action" | "job" while the panel is a chooser
        self._ghost = None

        # Scrolled, because an action with a dozen slots is taller than the tab — and the
        # whole point of docking this is that configuring a step never takes you out of
        # the window you are working in.
        scroller = QScrollArea()
        scroller.setWidget(self._panel)
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.NoFrame)
        scroller.setMinimumWidth(240)
        scroller.setStyleSheet(f"QScrollArea {{ background: {style.BG_PANEL}; "
                               f"border-left: 1px solid {style.BORDER}; }}")

        self._split = QSplitter(Qt.Horizontal)
        self._split.setHandleWidth(style.SPLITTER_WIDTH)
        self._split.setStyleSheet(style.SPLITTER_QSS)
        self._split.addWidget(left)
        self._split.addWidget(scroller)
        self._split.setStretchFactor(0, 3)
        self._split.setStretchFactor(1, 1)
        self._split.setSizes([680, 320])
        root.addWidget(self._split)

        self.refresh()

    # --- state -------------------------------------------------------------

    def job(self):
        return self._jobs.get(self._job_id)

    def set_packdump(self, packdump):
        """Adopt a newly imported dump (design 3.1).

        The tab flags steps whose bindings no longer resolve, and a mod update is exactly
        what breaks one — so this has to re-run that check, not just swap the reference.

        The step panel holds a dump of its own — its registry-entry pickers enumerate one —
        so it is rebound here rather than being left to answer from a registry the game no
        longer has. §3.1's rule about holders, applied one level down.
        """
        self._dump = packdump
        self._panel.set_packdump(packdump)
        self.refresh()

    def refresh(self):
        job = self.job()
        if job is None:
            return
        # Rebuilding sets check states, and setting one emits `itemChanged` — which is the
        # same signal a user's click arrives on. Without this the refresh that FOLLOWS a
        # mute would re-fire the mute for every other row.
        self._loading = True
        self._title.setText(job.name)
        self._default_policy.blockSignals(True)
        self._default_policy.setCurrentIndex(self._default_policy.findData(job.default_on_error))
        self._default_policy.blockSignals(False)

        self._tree.clear()
        # One pass, from the same function the pre-flight gate and the Jobs panel use, so
        # the ⚠ here can never disagree with what happens when you press play.
        self._problems = {}
        if self._packages is not None:
            try:
                for problem in step_problems(job, package_index=self._packages,
                                             tag_store=self._tags,
                                             blueprint_store=self._blueprints,
                                             packdump=self._dump,
                                             pack_targets=self._pack_targets):
                    self._problems.setdefault(problem.step_id, []).append(problem)
            except Exception:        # a cosmetic check must never stop the tab opening
                pass
        # One query for the whole job rather than one per row.
        last = {}
        if self._history is not None:
            try:
                last = self._history.latest_for_steps([s.id for s in job.steps])
            except Exception:            # never let a cosmetic column stop the tab opening
                last = {}

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
            item = QTreeWidgetItem([str(index), what, configured or "—", policy, ""])
            item.setData(0, Qt.UserRole, step)
            # The mute switch lives on the number column, which is otherwise pure
            # decoration — so the row gains a control without gaining a column.
            item.setCheckState(0, Qt.Checked if step.enabled else Qt.Unchecked)
            item.setToolTip(0, "Muted — this step is skipped when the job runs"
                            if not step.enabled else "Part of this job's run")
            self._render_last_run(item, last.get(step.id))
            if not step.enabled:
                # Struck through as well as dimmed: dimming alone is what "disabled" looks
                # like everywhere else in the app, and this row is very much still yours to
                # click on.
                font = item.font(1)
                font.setStrikeOut(True)
                for col in range(self._tree.columnCount()):
                    item.setFont(col, font)
                    item.setForeground(col, style.qt_colour(style.TEXT_FAINT))
            # 3.2.2: a step whose mapping no longer validates "is flagged as needing
            # attention". It already refuses to run — this is what stops that being a
            # surprise at run time, long after the schema edit that caused it.
            found = self._problems.get(step.id, [])
            if found:
                # Amber when a relink would fix it, red when the binding is actually
                # broken: "confirm this still means what you want" and "this points at
                # nothing" are different amounts of trouble, and §3.2.1 treats them
                # differently, so they should not look the same.
                relink_only = all(p.needs_relink for p in found)
                item.setText(1, f"⚠ {what}")
                item.setForeground(1, style.qt_colour(
                    style.WARNING if relink_only else style.ERROR))
                item.setToolTip(1, chr(10).join(p.detail for p in found))
            self._tree.addTopLevelItem(item)
        self._loading = False
        # A pick in flight owns the panel and the ghost row; a refresh arriving mid-pick
        # (a run finishing, a tag renamed elsewhere) must not steal either.
        if self._picking is not None:
            self._show_ghost_row("Add an action step" if self._picking == "action"
                                 else "Run another job as a step")
            return
        # Clearing the tree dropped the selection, and the panel is driven by it — without
        # this, any refresh would empty the panel out from under whatever step you were
        # configuring.
        if self._panel.step_id is not None:
            self._select_step(self._panel.step_id)

    _LAST_RUN_COLOURS = {"success": style.TEXT_MUTED, "rolled_back": style.TEXT_FAINT}

    def _render_last_run(self, item, row):
        """The loop's feedback, on the row you are editing.

        A step that has never run reads as "never" rather than blank: blank is what an
        absent *column* looks like, and "this has not run yet" is a fact worth stating
        when the whole point of the row is deciding whether to run it.
        """
        if not row:
            item.setText(4, "never")
            item.setForeground(4, style.qt_colour(style.TEXT_FAINT))
            return
        status = row.get("status") or ""
        when = _ago(row.get("finished_at") or row.get("started_at"))
        parts = [when] + ([_changed_count(row)] if status == "success" else [status])
        item.setText(4, "  ·  ".join(p for p in parts if p))
        item.setForeground(4, style.qt_colour(
            self._LAST_RUN_COLOURS.get(status, style.ERROR)))
        stamp = (row.get("finished_at") or "")[:19].replace("T", " ")
        item.setToolTip(4, "\n".join(filter(None, (
            f"Last run {stamp} UTC" if stamp else None, row.get("reason")))))

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

    def _select_step(self, step_id):
        for index in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(index)
            step = item.data(0, Qt.UserRole)
            if step is not None and step.id == step_id:
                self._tree.setCurrentItem(item)
                return

    def _touched(self):
        self.refresh()
        self.changed.emit()

    # --- actions -----------------------------------------------------------

    def _on_policy_changed(self):
        self._jobs.set_default_on_error(self._job_id, self._default_policy.currentData())
        self._touched()

    def _add_action_step(self):
        """Choose the action in the panel, then create the step.

        Deliberately not the other way round. A step created first would have no action
        yet, and a step with no action is a *broken* step — flagged, and blocking the whole
        job at pre-flight — for as long as it takes to get distracted. Nothing is written
        until something is chosen; Escape leaves the job exactly as it was.
        """
        choices = [Choice(label=(m.name or m.action_id), value=ref,
                          detail=" · ".join(filter(None, (ref, m.description))))
                   for ref, m in sorted(self._packages.actions.items())]
        self._begin_pick("action", "Add an action step", choices,
                         empty="No action packages are installed in this profile.")

    def _add_job_step(self):
        candidates = [j for j in self._jobs.all()
                      if j.id != self._job_id and not self._jobs._reaches(j.id, self._job_id)]
        choices = [Choice(label=j.name, value=str(j.id),
                          detail=f"{len(j.steps)} step{'s' if len(j.steps) != 1 else ''}")
                   for j in candidates]
        self._begin_pick("job", "Run another job as a step", choices,
                         empty="No other job can be nested here without creating a cycle.")

    def _begin_pick(self, kind, header, choices, *, empty):
        # Mashing the button, or switching from +Action to +Job mid-pick, must REPLACE the
        # placeholder rather than add another. Safe to remove directly: this runs from a
        # button click, not from inside a tree signal.
        self._drop_ghost()
        self._picking = None
        if not choices:
            # Nothing to choose, so there is no pick to be in. Entering picking mode here
            # would strand the placeholder for good: with no chooser on screen, the panel
            # has nothing to clear and therefore never announces that the pick is off.
            self._panel.show_picker(header, [], empty=empty)
            self.refresh()
            return
        self._picking = kind
        self._show_ghost_row(header)
        self._panel.show_picker(header, choices, empty=empty)

    def _show_ghost_row(self, text):
        """Where the new step will land, shown while you choose.

        Purely visual — it carries no step and cannot be selected — because the alternative
        (a real, action-less step row) is the broken state `_add_action_step` exists to
        avoid. It is re-added by `refresh` so a reload mid-pick does not lose it.
        """
        # A plain ellipsis rather than a decorative glyph: the marker has to exist in the
        # UI font, and a missing one renders as a tofu box on the row that is supposed to
        # read as "not real yet".
        ghost = QTreeWidgetItem([str(self._tree.topLevelItemCount() + 1),
                                 f"{text.lower()}…", "", "", ""])
        ghost.setFlags(Qt.ItemIsEnabled)
        for col in range(self._tree.columnCount()):
            ghost.setForeground(col, style.qt_colour(style.TEXT_FAINT))
        self._tree.addTopLevelItem(ghost)
        self._ghost = ghost

    def _drop_ghost(self):
        if self._ghost is None:
            return
        index = self._tree.indexOfTopLevelItem(self._ghost)
        if index >= 0:
            self._tree.takeTopLevelItem(index)
        self._ghost = None

    def _end_pick(self):
        """The pick is off — forget it and redraw, but **not from here**.

        This is reached from `set_step`, which is reached from `currentItemChanged`. A
        rebuild there destroys the very item Qt is still delivering for, which is the
        use-after-free `_set_step_enabled` documents. Posted to the event loop instead.
        """
        self._picking = None
        self._ghost = None
        self._pending_select = self._panel.step_id
        self._rebuild_timer.start(0)

    def _opening_bindings(self, action_ref) -> dict:
        """The best guess, PERSISTED at creation rather than merely displayed.

        3.3's fill pre-selects a candidate — "if exactly one compatible artifact exists,
        pre-select it" — and the form duly showed it. Nothing wrote it. A new step's
        bindings stayed empty until a widget emitted a *change*, so opening the dropdown
        and clicking the one tag already highlighted committed nothing: the index never
        moved, so no signal fired. The only way to make the suggestion stick was to go and
        edit some unrelated field, which is a strange thing to have to learn.

        Writing it here makes the shown value the stored value from the first instant —
        which is also what `_seed_jobs` already does for the jobs a fresh profile starts
        with, so the two paths now agree.
        """
        try:
            manifest = self._packages.get(action_ref)
        except KeyError:
            return {}
        guessed = best_guess_bindings(manifest, self._tags,
                                      blueprint_store=self._blueprints,
                                      packdump=self._dump,
                                      pack_targets=self._pack_targets)
        # A slot it declined to guess stays genuinely unbound: an empty entry would read as
        # "bound to nothing" to `step_problems`, which is a different claim.
        bindings = {name: value for name, value in guessed.items()
                    if value is not None and value != []}
        return {"bindings": bindings,
                "bound_names": record_names(manifest, bindings, tag_store=self._tags)}

    def _on_picked(self, value):
        kind, self._picking = self._picking, None
        self._ghost = None
        if kind == "action":
            step = self._jobs.add_action_step(self._job_id, value,
                                              **self._opening_bindings(value))
        else:
            try:
                step = self._jobs.add_job_step(self._job_id, int(value))
            except (ValueError, TypeError) as e:
                self.status.emit(str(e))
                self._end_pick()
                return
        self._touched()
        # Select it, which puts it in the panel — an unbound required mapping can't run,
        # so landing on its controls is the useful next thing.
        self._select_step(step.id)

    def _on_selection_changed(self, current, _previous=None):
        """Selecting a step is what opens it — there is no separate edit gesture now."""
        if self._loading:
            return
        self._panel.set_step(current.data(0, Qt.UserRole) if current is not None else None)

    def _on_panel_changed(self):
        """The panel wrote a field. Redraw the row it belongs to, keeping the selection.

        Deferred for the same reason muting is: the panel's write can arrive from inside a
        widget's own signal, and rebuilding the tree from there destroys items Qt is still
        working with.
        """
        self._pending_select = self._panel.step_id
        self._rebuild_timer.start(0)

    def _on_item_changed(self, item, column):
        """A step's mute checkbox was clicked."""
        if self._loading or column != 0:
            return
        step = item.data(0, Qt.UserRole)
        if step is None:
            return
        wanted = item.checkState(0) == Qt.Checked
        if wanted == step.enabled:
            return
        self._set_step_enabled(step, wanted)

    def _set_step_enabled(self, step, enabled):
        """Write the mute, then rebuild — but **not from inside this call**.

        `refresh()` clears the tree, which destroys every `QTreeWidgetItem` in it. When the
        caller is `itemChanged`, one of those is the item Qt is *still delivering the signal
        for*: it returns into `QTreeModel` code that goes on using the pointer, and that is
        a use-after-free. It crashes only when the freed block happens to have been reused,
        so it survives being clicked in a test and takes the application down in real use —
        measured as an access violation (0xC0000005) on a single click.

        The rebuild is therefore posted to the event loop, to run once this signal has
        finished unwinding. The timer is parented to the tab so closing the tab destroys
        it; a bare `singleShot` would fire into a deleted widget instead.
        """
        self._jobs.set_step_enabled(step.id, enabled)
        self._pending_select = step.id
        self._rebuild_timer.start(0)

    def _deferred_rebuild(self):
        self._touched()
        if self._pending_select is not None:
            self._select_step(self._pending_select)
            self._pending_select = None

    def _on_context_menu(self, pos):
        self._menu_for(self._tree.itemAt(pos)).exec(self._tree.mapToGlobal(pos))

    def _menu_for(self, item):
        """The row's own menu. Built separately from showing it so a test can read the
        actions without `exec()` blocking on a modal event loop."""
        menu = QMenu(self)
        step = item.data(0, Qt.UserRole) if item is not None else None
        if step is None:
            menu.addAction("Add action step…", self._add_action_step)
            return menu
        # Three groups, weakest consequence first: read it, run it, change it. "View Action
        # Info" leads because it is the only one that does nothing at all — and because
        # "what does this action actually do" is the question you have while looking at a
        # step you did not write.
        if step.is_action:
            menu.addAction("View Action Info",
                           lambda: self.action_info_requested.emit(step.action_ref))
            menu.addSeparator()
        # Dry run before Run, deliberately. It is the one you want while iterating, it
        # cannot hurt anything, and putting the destructive twin at the top of a menu you
        # open dozens of times an hour is how a mis-click writes files.
        #
        # The labels state the range rather than saying "up to here", which never settles
        # whether *this* step is included. Two gestures, two questions: run the prefix and
        # you get the world the job would build; run the step alone and you get it against
        # whatever is committed now.
        position = next((i for i, s in enumerate(self.job().steps, start=1)
                         if s.id == step.id), None)
        for dry, verb in ((True, "Dry run"), (False, "Run")):
            # Hidden on step 1 (identical to running it alone) and on a muted step, where
            # "run the sequence up to and including one you switched off" is a contradiction.
            if position and position > 1 and step.enabled:
                menu.addAction(
                    f"{verb} steps 1–{position}",
                    lambda _=False, d=dry: self.step_run_requested.emit(
                        self.job(), step.id, d, True))
            menu.addAction(
                f"{verb} step {position} only" if position else f"{verb} this step",
                lambda _=False, d=dry: self.step_run_requested.emit(
                    self.job(), step.id, d, False))
        menu.addSeparator()
        menu.addAction("Duplicate", lambda: self._duplicate_step(step))
        menu.addAction("Unmute step" if not step.enabled else "Mute step",
                       lambda: self._set_step_enabled(step, not step.enabled))
        menu.addAction("Remove", lambda: self._remove_specific_step(step))
        return menu

    def _duplicate_step(self, step):
        """Copy a step, bindings and all, directly beneath the original.

        Two steps that differ in one binding are common — the same action over `remove`
        and then over `deprecated` — and rebuilding the second by hand means re-picking the
        action and re-filling every slot to change one of them.
        """
        if not step.is_action:
            copy = self._jobs.add_job_step(self._job_id, step.ref_job_id,
                                           on_error=step.on_error)
        else:
            # `bound_names` copies too: the duplicate is bound to the same tags under the
            # same names, so it owes exactly the relinks the original owes — no more (a
            # fresh copy that immediately demanded relinking would be nonsense) and no
            # fewer (dropping them would launder a stale binding clean).
            copy = self._jobs.add_action_step(
                self._job_id, step.action_ref, bindings=dict(step.bindings),
                config=dict(step.config), on_error=step.on_error,
                bound_names=dict(step.bound_names))
        # Added at the end, then moved to sit just after its original: a copy that appears
        # eight rows away reads as a new step rather than a copy of this one.
        job = self.job()
        ids = [s.id for s in job.steps if s.id != copy.id]
        at = ids.index(step.id) + 1
        self._jobs.reorder_steps(self._job_id, ids[:at] + [copy.id] + ids[at:])
        self._touched()
        self._select_step(copy.id)

    def _remove_step(self):
        step = self._selected_step()
        if step is not None:
            self._remove_specific_step(step)

    def _remove_specific_step(self, step):
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
