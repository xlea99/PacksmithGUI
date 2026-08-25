"""Editing a blueprint: its schema and its instances (design 3.2.2).

§5.2's node view is an explicit placeholder, and designing it before there is real data to
look at is how it ends up wrong. This is the plain renderer that gets you to real data:

* **the schema** as a tree, because inline groups are a tree;
* **the instances as a grid** — one row per instance, one column per value slot — because
  "every stone type × cut × form, and which are filled" is literally a table, and it is the
  shape the user was maintaining in a spreadsheet before Packsmith existed.

Gaps are painted rather than left blank. An empty slot is the *output* of the primitive —
missing content that needs generating or sourcing — so it reads as a marked absence, not as
an empty cell you might have missed.
"""
from PySide6.QtCore import QEvent, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox,
    QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
    QSizePolicy, QSplitter, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from packsmith.core.blueprints import BlueprintError, SCALAR_TYPES, SLOT_TYPES
from packsmith.core.query.ast import Cmp, Id, Query, QueryError, Registry
from packsmith.core.query.evaluator import evaluate
from packsmith.core.query.language import parse
from packsmith.core.query.template import (
    UnresolvedParam, candidate_query, context_for, resolve,
)
from packsmith.core.revalidate import project_removal, project_rename, project_retype
from packsmith.core.undo import Batch, BindingEngine, Edit, UndoStack
from packsmith.gui.confirm import confirm_destructive, recovery_note
from packsmith.gui.shell import style
from packsmith.gui.shell.picker import PickerPopup
from packsmith.gui.shell.tree import PanelTree
from packsmith.gui.shell.dropdown import DropDown

_ROLE_PATH = Qt.UserRole
_ROLE_KIND = Qt.UserRole + 1
_ROLE_OWNER = Qt.UserRole + 2      # on a grid cell: the action_ref managing it, or unset

# Wide enough for a real registry id. Fitting to contents alone gives an empty column the
# width of its header, and a blueprint's columns start out mostly empty by definition.
_MIN_COLUMN_WIDTH = 210


def _ancestors(path):
    """Every group path above a slot: 'a.b.c' -> ['a', 'a.b']."""
    parts = path.split(".")[:-1]
    return [".".join(parts[:i + 1]) for i in range(len(parts))]


def _button(text, tooltip=""):
    button = QPushButton(text)
    button.setFixedHeight(22)
    button.setCursor(Qt.PointingHandCursor)
    button.setToolTip(tooltip)
    button.setStyleSheet(f"""
        QPushButton {{
            background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
            border: 1px solid {style.BORDER}; font-size: 11px; padding: 1px 10px;
        }}
        QPushButton:hover {{ color: {style.TEXT}; border-color: {style.ACCENT_EDGE}; }}
    """)
    return button


class _SchemaTree(PanelTree):
    """The schema tree, with drag-to-reorder.

    Qt's ``InternalMove`` would rearrange the items itself and leave us to reverse-engineer
    what happened from the resulting widget state. The store is the model here, so the drop
    is intercepted, translated into (parent, index), and handed to the store — which may
    refuse it. The tree then rebuilds from what the store actually did, so a rejected move
    can't leave the view showing something that isn't true.
    """

    slot_dropped = Signal(str, object, int)     # path, parent path or None, index

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event):
        dragged = self.currentItem()
        if dragged is None or dragged.data(0, _ROLE_PATH) is None:
            event.ignore()
            return

        target = self.itemAt(event.position().toPoint())
        where = self.dropIndicatorPosition()

        if target is None or where == QAbstractItemView.OnViewport:
            parent_path, index = None, self.topLevelItemCount()
        elif where == QAbstractItemView.OnItem and target.data(0, _ROLE_KIND) == "group":
            parent_path, index = target.data(0, _ROLE_PATH), target.childCount()
        else:
            # Beside the target: same parent, at the target's position. Dropping ONTO a
            # value slot lands beside it too — it can't contain anything.
            container = target.parent()
            parent_path = container.data(0, _ROLE_PATH) if container else None
            siblings = container if container else self
            index = (siblings.indexOfChild(target) if container
                     else self.indexOfTopLevelItem(target))
            if where == QAbstractItemView.BelowItem:
                index += 1

        # Never let Qt move the row: the store decides, and the reload reflects it.
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        self.slot_dropped.emit(dragged.data(0, _ROLE_PATH), parent_path, index)


class _TemplateBar(QWidget):
    """Where the candidate template is authored (design 5.3 Part B).

    This is **not** a filter. The bar on a registry view narrows which rows you see; this
    one decides what a *cell* suggests when you go to fill it. §5.3 is emphatic about the
    division of labour it serves: the tool *finds*, the human *decides* — "no heuristic can
    reliably decide that `stoneworks:alexc_galena_cut_brick` is galena's cut-brick."

    Feedback is tied to the cell you're standing on, because resolution is per-cell: a
    template referencing an unbound anchor is fine everywhere else and broken here.
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 5)
        lay.setSpacing(6)

        # A checkbox rather than a label, because the template is the thing being switched
        # off. Some genuine answers no template can find — `stoneworks:alexc_galena_cut_brick`
        # is galena's cut-brick and nothing about its id says so — and §5.3's whole premise
        # is that the human decides. Narrowing that a human cannot escape is a worse tool
        # than one that suggests nothing.
        self.enabled = QCheckBox("Suggest")
        self.enabled.setChecked(True)
        self.enabled.setStyleSheet(
            f"QCheckBox {{ color: {style.TEXT_MUTED}; font-size: 11px; }}")
        self.enabled.setToolTip(
            "What to offer when filling an empty cell.\n\n"
            "Uncheck to offer the whole registry instead, for the rare cell whose real "
            "answer no template can reach. Not saved with the view — it is an escape "
            "hatch, not a setting.")
        self.enabled.toggled.connect(self._input_enabled)
        self.enabled.toggled.connect(lambda _on: self.changed.emit())
        lay.addWidget(self.enabled)

        self._input = QLineEdit()
        self._input.setPlaceholderText("@b:base_block @slot")
        self._input.setToolTip(
            "@b:<slot>   this row's value for that slot (the anchor)\n"
            "@slot       the slot being filled       @group / @leaf  its halves\n"
            "@instance   this row's name\n\n"
            "Plain words are literal tokens. The slot's own registry is searched.")
        self._input.setClearButtonEnabled(True)
        self._base_qss = f"""
            QLineEdit {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                border: 1px solid %s; padding: 3px 6px; font-size: 12px;
            }}
        """
        self._input.setStyleSheet(self._base_qss % style.BORDER)
        self._input.textChanged.connect(lambda _t: self.changed.emit())
        lay.addWidget(self._input, 1)

        self.show_button = _button(
            "Show",
            "List this cell's suggestions without editing it  (Ctrl+Space)")
        lay.addWidget(self.show_button)

        self._status = QLabel()
        self._status.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
        self._status.setMinimumWidth(190)
        lay.addWidget(self._status)

    def _input_enabled(self, on):
        """Grey the template out when it is inert, so an unchecked box cannot be mistaken
        for a template that has stopped working."""
        self._input.setEnabled(on)

    def suggesting(self) -> bool:
        return self.enabled.isChecked()

    def text(self) -> str:
        return self._input.text().strip()

    def set_text(self, text):
        self._input.setText(text or "")

    def report(self, message, *, bad=False):
        self._input.setStyleSheet(self._base_qss % (style.ERROR if bad else style.BORDER))
        self._status.setStyleSheet(
            f"color: {style.ERROR if bad else style.TEXT_FAINT}; font-size: 11px;")
        self._status.setText(message)
        self._status.setToolTip(message)


class _OverridePanel(QWidget):
    """Per-column templates, opt-in.

    One template can't serve every column: an anchor slot can't reference itself, a string
    slot wants no candidates at all, and `@slot` is wrong for any `*.base` column because a
    cut's base form is named `polished_granite`, never `polished_granite_base`. So each
    column can take over with its own, and says so with a checkbox.
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = {}
        self._lay = QFormLayout(self)
        self._lay.setContentsMargins(24, 2, 8, 6)
        self._lay.setSpacing(4)

    def rebuild(self, slots, overrides):
        while self._lay.count():
            item = self._lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows = {}
        for slot in slots:
            box = QCheckBox(slot.path)
            box.setStyleSheet(f"QCheckBox {{ color: {style.TEXT_MUTED}; font-size: 11px; }}")
            field = QLineEdit(overrides.get(slot.path, ""))
            field.setStyleSheet(f"""
                QLineEdit {{
                    background: {style.BG_DEEP}; color: {style.TEXT};
                    border: 1px solid {style.BORDER}; padding: 2px 5px; font-size: 11px;
                }}
                QLineEdit:disabled {{ color: {style.TEXT_FAINT}; }}
            """)
            enabled = slot.path in overrides
            box.setChecked(enabled)
            field.setEnabled(enabled)
            self._sync_placeholder(field, enabled)
            box.toggled.connect(field.setEnabled)
            box.toggled.connect(lambda on, f=field: self._sync_placeholder(f, on))
            box.toggled.connect(lambda _c: self.changed.emit())
            field.textChanged.connect(lambda _t: self.changed.emit())
            self._lay.addRow(box, field)
            self._rows[slot.path] = (box, field)

    @staticmethod
    def _sync_placeholder(field, checked):
        # Once a column has taken over, "uses the bar above" is a lie — it doesn't.
        field.setPlaceholderText("" if checked else "uses the bar above")

    def overrides(self) -> dict:
        """Every **checked** column, empty text included.

        A checked box with an empty field is a deliberate statement — "this column takes
        over, and what it wants is no template at all, so suggest anything." Requiring text
        to record the override would silently un-check the box on the next rebuild and
        throw that decision away.
        """
        return {path: field.text().strip()
                for path, (box, field) in self._rows.items() if box.isChecked()}


def _dismiss(picker):
    """Retire a picker, tolerating one that Qt has already deleted underneath us.

    A picker dismisses itself when its cell editor is destroyed, so by the time the next
    edit begins the tab's reference can point at a C++ object that no longer exists —
    and PySide raises on any use of the wrapper, not just on a bad call.
    """
    if picker is None:
        return
    try:
        picker.dismiss()
    except RuntimeError:
        pass        # already gone, which is what we wanted


def _disconnect(signal, slot):
    """Drop a connection without caring whether it is still there.

    The picker outlives none of these, but Qt tears widgets down in an order Python does
    not control, so the scrollbar can outlive the popup and keep calling into it.
    """
    try:
        signal.disconnect(slot)
    except (RuntimeError, TypeError):
        pass


class _InstanceGrid(QTableWidget):
    """The instance table, with the keys a grid is expected to have.

    Qt's defaults are for a read-only table: Enter moves down a row and Delete does nothing,
    which is wrong for a grid whose entire purpose is filling cells in.
    """

    def __init__(self, tab):
        super().__init__()
        self._tab = tab
        self._pressed_cell = None

    def mousePressEvent(self, event):
        """Remember where the user just clicked, for exactly one reader.

        Clicking cell B is what ends an edit in cell A, and the sequence that follows is
        awkward: A commits, the grid is rebuilt from scratch (losing all selection), and
        only then does Qt open B's editor — without re-setting the current cell, because
        it already did that during this press. So whoever restores selection after a commit
        has to know that the user's intent has moved to B, and the press is the only place
        that fact exists.
        """
        index = self.indexAt(event.position().toPoint())
        self._pressed_cell = (index.row(), index.column()) if index.isValid() else None
        super().mousePressEvent(event)

    def take_pressed_cell(self):
        """Read-and-clear, so a click can only redirect the *next* commit. A stale press
        from five minutes ago must not move the cursor after an Enter."""
        cell, self._pressed_cell = self._pressed_cell, None
        return cell

    def keyPressEvent(self, event):
        # Any keystroke means the user is driving from the keyboard, so the last click
        # stops speaking for them: click A, arrow to C, press Enter — the commit belongs
        # to C, and a stale press would drag the cursor back to A.
        self._pressed_cell = None
        key = event.key()
        index = self.currentIndex()
        editing = self.state() == QAbstractItemView.EditingState
        if index.isValid() and not editing:
            if key in (Qt.Key_Delete, Qt.Key_Backspace):
                self._tab.clear_cell(index.row(), index.column())
                return
            if key in (Qt.Key_Return, Qt.Key_Enter):
                # Enter opens the editor (and with it the suggestions). Qt would move the
                # cursor down instead, which is the one thing you never want here.
                self._tab.begin_edit(index.row(), index.column())
                return
        super().keyPressEvent(event)


class _SlotDelegate(QStyledItemDelegate):
    """Per-column editors typed by the slot they edit.

    A registry slot on a 300-mod pack is 14,000 candidates, so completion isn't polish —
    hand-typing `minecraft:polished_granite_stairs` correctly, forty times, is the tedium
    that 3.2.2 warns "undermines the whole concept".
    """

    def __init__(self, tab):
        super().__init__(tab)
        self._tab = tab

    def createEditor(self, parent, option, index):
        slot = self._tab.slot_for_column(index.column())
        if slot is None:
            return super().createEditor(parent, option, index)

        if slot.type == "bool":
            editor = QComboBox(parent)
            editor.addItems(["", "true", "false"])
            return editor
        if slot.type == "enum":
            editor = QComboBox(parent)
            editor.addItems([""] + list(slot.enum_values))
            return editor
        if slot.type == "blueprint":
            editor = QComboBox(parent)
            editor.setEditable(True)
            editor.addItems([""] + self._tab.instance_names(slot.ref_blueprint))
            return editor

        editor = QLineEdit(parent)
        if slot.type == "registry":
            suggestions, note = self._tab.candidates_for(index.row(), slot)
            # Suggestions narrow; the FIELD does not. §5.3: a recommendation "is an open
            # hint — it must never restrict, or you lock yourself out of exactly the
            # weirdness that makes the problem hard." So an unlisted id is still typeable.
            editor.setPlaceholderText(note)
            # The same picker the ⚙ Show button opens, anchored under the cell and driven
            # by what you type into it. A QCompleter matched substrings only, so
            # "polished granite stair" found nothing — ids use underscores, and typing a
            # space is the first thing anyone does.
            picker = self._tab.attach_suggestions(
                editor, index.row(), index.column(), suggestions, note)
            row, column = index.row(), index.column()
            picker.chosen.connect(
                lambda value, e=editor: self._choose(e, row, column, value))
        return editor

    def eventFilter(self, editor, event):
        """Notice an Enter on its way to finishing the edit.

        Read here rather than in the view: while a cell is being edited the editor holds
        focus, so `_InstanceGrid.keyPressEvent` never sees the key at all. The picker
        installs its own filter on the same editor and swallows Enter when a suggestion is
        highlighted — that path sets the flag from `_choose` instead, so both arrive.
        """
        if (event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)):
            self._tab.advance_after_commit()
        return super().eventFilter(editor, event)

    def _choose(self, editor, row, column, value):
        """Picking from the list finishes the edit. In a grid you chose the value, not a
        prefix of it — making you press Enter again to confirm your own click is friction
        for nothing."""
        # Choosing IS finishing, however it was chosen, so it moves on like an Enter would.
        self._tab.advance_after_commit()
        try:
            editor.setText(value)
        except RuntimeError:
            # The editor was already torn down (a click that took focus away closes it).
            # The choice still stands — commit it straight to the store.
            self._tab.commit_cell(row, column, value)
            return
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor, index):
        text = index.data(Qt.DisplayRole) or ""
        if isinstance(editor, QComboBox):
            editor.setCurrentText(text)
        else:
            editor.setText(text)

    def setModelData(self, editor, model, index):
        text = editor.currentText() if isinstance(editor, QComboBox) else editor.text()
        self._tab.commit_cell(index.row(), index.column(), text.strip())


class BlueprintEditorTab(QWidget):
    """One blueprint: its shape on the left, its instances on the right."""

    changed = Signal()          # something the panel should re-read
    save_requested = Signal()   # keep this ephemeral tab as a named View
    status = Signal(str)        # non-modal notice for the status bar (design 3.2.2)

    def __init__(self, query, store, packdump=None, parent=None, view=None,
                 on_config_changed=None, job_impact=None):
        super().__init__(parent)
        # The grid IS the query, rendered (design 3.2.4: "Rows are the instances of a
        # blueprint (fields become slots)"). Columns come from evaluating it rather than
        # from reading the store directly, which is what makes this a View renderer rather
        # than a bespoke tab.
        self.query = query
        self.blueprint_name = query.scope.name
        self.view = view
        self._store = store
        self._dump = packdump
        self._on_config_changed = on_config_changed or (lambda config: None)
        # Injected rather than reached for: what a schema change breaks is a question about
        # jobs and installed packages, and this tab has no business holding either. Takes
        # the projected slot list, returns a sentence naming the steps (or "").
        self._job_impact = job_impact or (lambda projected, mutation: "")
        self._slots = []
        self._instances = []
        self._loading = False
        self._sticky = {}              # the last slot's type/registry, for the next one
        self._tree_items = {}          # slot path -> its row in the schema tree
        self._picker = None            # the open suggestion popup, kept alive
        self._advance = False          # Enter pending: step down after the commit
        # Per tab, per §9.3.3 — global undo would reverse something you cannot see.
        self._undo = UndoStack(store._db, [BindingEngine(store)])
        self._picker_cell = None       # (row, column) it is attached to, if any
        # Candidate templates (design 5.3) live in the View's renderer_config — a
        # recommendation is a knob on the renderer, never part of the blueprint (§5.3 Part
        # B: strip every one and the blueprint is byte-for-byte still itself). An unsaved
        # tab keeps them in memory until it's saved.
        config = dict((view.renderer_config if view else None) or {})
        self._template = config.get("suggest", "")
        self._overrides = dict(config.get("suggest_overrides") or {})
        self._collapsed = set(config.get("collapsed") or ())
        self._column_widths = dict(config.get("column_widths") or {})

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setTextFormat(Qt.RichText)
        self._banner.setContentsMargins(10, 6, 10, 6)
        self._banner.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._banner.hide()
        root.addWidget(self._banner)

        bar = QWidget()
        # A bare QWidget defaults to an EXPANDING vertical policy, so without this it
        # splits the tab evenly with the splitter below and the toolbar occupies half the
        # screen. Panels elsewhere get away with it because a tree or table sits under the
        # bar and out-expands it.
        bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 5, 8, 5)
        bar_lay.setSpacing(5)
        for text, tip, slot in (
                ("＋  Instance", "Add an instance of this blueprint", self._new_instance),
                ("＋  Slot", "Add a bindable slot", self._new_slot),
                ("＋  Group", "Add an inline group to nest slots under", self._new_group)):
            button = _button(text, tip)
            button.clicked.connect(slot)
            bar_lay.addWidget(button)
        # Collapsing a group in the tree hides its columns in the grid. A 4-cut palette is
        # 21 columns; being able to fold away the three cuts you aren't working on is the
        # difference between a usable table and a horizontal scroll marathon.
        self._follow_tree = QCheckBox("Hide collapsed groups")
        # Restored before it is wired, so setting it does not immediately write it back.
        self._follow_tree.setChecked(bool(config.get("follow_tree", False)))
        self._follow_tree.setToolTip(
            "Columns follow the tree: collapse a group on the left and its slots leave "
            "the grid.")
        self._follow_tree.setStyleSheet(
            f"QCheckBox {{ color: {style.TEXT_MUTED}; font-size: 11px; }}")
        self._follow_tree.toggled.connect(self._apply_column_visibility)
        self._follow_tree.toggled.connect(lambda *_: self.save_config())
        bar_lay.addSpacing(8)
        bar_lay.addWidget(self._follow_tree)

        self._save = _button("Save view",
                             "Keep this as a named View, with its suggestions")
        self._save.clicked.connect(self.save_requested)
        bar_lay.addWidget(self._save)

        bar_lay.addStretch()
        self._summary = QLabel()
        self._summary.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        bar_lay.addWidget(self._summary)
        root.addWidget(bar)

        self._template_bar = _TemplateBar()
        # Populate BEFORE connecting: the handler reads the overrides panel, which hasn't
        # been built yet, so an early signal would save an empty override set over the one
        # just loaded from the view.
        self._template_bar.set_text(self._template)
        self._template_bar.changed.connect(self._on_template_changed)
        self._template_bar.show_button.clicked.connect(lambda: self.show_suggestions())
        QShortcut(QKeySequence("Ctrl+Space"), self,
                  activated=lambda: self.show_suggestions())
        root.addWidget(self._template_bar)

        self._overrides_toggle = _button(
            "▸  per-column suggestions",
            "Columns that need their own template — a base column can't use @slot, and a "
            "string slot wants no suggestions at all")
        self._overrides_toggle.setCheckable(True)
        self._overrides_toggle.setStyleSheet(
            self._overrides_toggle.styleSheet() + "QPushButton { border: none; }")
        self._overrides_panel = _OverridePanel()
        self._overrides_panel.changed.connect(self._on_template_changed)
        self._overrides_panel.hide()
        self._overrides_toggle.toggled.connect(self._toggle_overrides)
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(8, 0, 8, 0)
        toggle_row.addWidget(self._overrides_toggle)
        toggle_row.addStretch()
        root.addLayout(toggle_row)
        root.addWidget(self._overrides_panel)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(style.SPLITTER_WIDTH)
        split.setStyleSheet(style.SPLITTER_QSS)

        self._tree = _SchemaTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Slot", "Type"])
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_slot_menu)
        self._tree.slot_dropped.connect(self._on_slot_dropped)
        self._tree.itemExpanded.connect(self._apply_column_visibility)
        self._tree.itemCollapsed.connect(self._apply_column_visibility)
        split.addWidget(self._tree)

        self._grid = _InstanceGrid(self)
        # Dragging a column is the user arranging the view, and it has to outlive
        # the tab. Widths were only ever captured during a REBUILD, so a drag with
        # no subsequent edit was lost — and coalesced through a timer because a
        # drag emits one signal per pixel.
        self._width_timer = QTimer(self)
        self._width_timer.setSingleShot(True)
        self._width_timer.setInterval(400)
        self._width_timer.timeout.connect(self._remember_widths)
        self._grid.horizontalHeader().sectionResized.connect(
            lambda *_: None if self._loading else self._width_timer.start())
        # LIST_QSS only targets QTreeWidget/QListWidget, so a table styled with it falls
        # back to the app palette and drifts from the panels beside it. Spell it out.
        self._grid.setStyleSheet(f"""
            QTableWidget {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                gridline-color: {style.BORDER}; border: none; font-size: 12px;
                selection-background-color: {style.ACCENT};
                selection-color: {style.TEXT};
            }}
            QTableWidget::item {{ padding: 2px 4px; }}
        """)
        self._grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self._grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self._grid.customContextMenuRequested.connect(self._on_instance_menu)
        # The instance names are the vertical HEADER, which is its own widget — the grid's
        # policy above stops at the viewport, so right-clicking the name (the obvious place
        # to act on an instance) reached nothing at all.
        row_names = self._grid.verticalHeader()
        row_names.setContextMenuPolicy(Qt.CustomContextMenu)
        row_names.customContextMenuRequested.connect(self._on_row_menu)
        self._grid.setItemDelegate(_SlotDelegate(self))
        self._grid.currentCellChanged.connect(self._on_cell_changed)
        self._grid.cellDoubleClicked.connect(self._maybe_claim)
        self._grid.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._grid.horizontalHeader().setStyleSheet(
            f"QHeaderView::section {{ background: {style.BG_PANEL}; "
            f"color: {style.TEXT_MUTED}; border: none; "
            f"border-bottom: 1px solid {style.BORDER}; padding: 3px 6px; font-size: 11px; }}")
        split.addWidget(self._grid)

        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 900])
        root.addWidget(split, 1)          # the splitter takes everything that's left

        self.reload()

    # --- data the delegate asks for ----------------------------------------

    def slot_for_column(self, column):
        return self._slots[column] if 0 <= column < len(self._slots) else None

    def registry_entries(self, registry_type):
        if self._dump is None:
            return []
        return list(self._dump.registry.get(registry_type, {}).get("values", []))

    # --- candidate recommendations (design 5.3) ----------------------------

    def has_template_for(self, path) -> bool:
        """Whether this column has a template at all — i.e. whether its suggestions are a
        curated set rather than the whole registry."""
        return bool(self.template_for(path))

    def show_suggestions(self, row=None, column=None):
        """Browse a cell's candidates without entering edit mode.

        A recommendation you have to start typing to see isn't much of a recommendation —
        §5.3's whole framing is "here's everything relevant, organized; *you* decide", and
        deciding starts with looking.
        """
        index = self._grid.currentIndex()
        if (row is None or column is None) and not index.isValid():
            self._template_bar.report("pick a cell first", bad=True)
            return
        picker = self.suggestion_picker(row, column)
        if picker is None:
            return
        # Held on the tab, not just in a local: the only other owner is the Qt parent, and
        # a popup that gets collected mid-show is a window that vanishes for no reason.
        _dismiss(self._picker)
        self._picker = picker
        self._picker_cell = None       # cursor-anchored, not attached to a cell's editor
        where = QCursor.pos()
        # Opened from a *later* event loop turn. A Qt.Popup grabs the mouse, and showing
        # one inside the click (or the context menu) that spawned it means the release
        # lands outside the new window and closes it again immediately.
        QTimer.singleShot(0, lambda: picker.popup_at(where))

    def attach_suggestions(self, editor, row, column, candidates, note):
        """Hang the suggestion picker under a cell that is being edited.

        The same widget as the ⚙ Show button, in completion clothing: it follows the cell
        editor's text instead of carrying its own filter box, and it anchors to the cell
        rather than the cursor so the list stays visually attached to what it's about.

        It opens **whenever you start editing**, regardless of how many candidates there
        are. Gating that on the list being short enough made the behaviour depend on how
        many results a filter happened to return, which is unpredictable from the outside —
        and the widget is bounded and scrollable now, so a long list is no longer a wall.
        """
        _dismiss(self._picker)
        # Docked INTO the viewport rather than floating above it. As a top-level window it
        # was positioned in screen coordinates once, so moving the main window left the
        # suggestions behind, hovering over the desktop next to a cell that had walked
        # away. A child of the viewport moves with the window because it is part of it —
        # there is no position to keep up to date.
        viewport = self._grid.viewport()
        picker = PickerPopup(candidates, header=note, attach=editor, dock=viewport)
        self._picker = picker
        self._picker_cell = (row, column)
        picker.dismissed.connect(lambda: self._forget_picker(picker))
        picker.destroyed.connect(lambda *_: self._forget_picker(picker))

        def anchor():
            # Re-resolved every time rather than captured: the row moves under the popup
            # whenever the grid scrolls, and `visualRect` is already in viewport
            # coordinates — the same ones the docked picker is positioned in.
            if self._picker is not picker:
                # Superseded: another cell owns the suggestions now. Its handler stays
                # connected until Qt gets round to deleting it, and repositioning would
                # call show() — which is how dragging across a row left a trail of them.
                return
            try:
                rect = self._grid.visualRect(self._grid.model().index(row, column))
                if rect.isValid() and rect.intersects(viewport.rect()):
                    picker.dock_under(rect)
                else:
                    picker.hide()      # the cell scrolled out of sight; so does its list
            except RuntimeError:
                # A scrollbar can outlive the widgets this closure captured when the whole
                # tab is torn down; Qt deletes in an order Python does not control.
                pass

        # A later turn, so the popup isn't born inside the double-click that opened the
        # editor. Typing re-opens it, so Escape dismisses without ending the session.
        QTimer.singleShot(0, anchor)
        editor.textChanged.connect(
            lambda text: None if picker.isVisible() else anchor())
        # Scrolling moves the cell within the viewport, and the popup has to travel with
        # it — being a child only makes it follow the *window*, not the scroll.
        for bar in (self._grid.verticalScrollBar(), self._grid.horizontalScrollBar()):
            bar.valueChanged.connect(anchor)
            picker.destroyed.connect(
                lambda *_, b=bar: _disconnect(b.valueChanged, anchor))
        return picker

    def suggestion_picker(self, row=None, column=None):
        """Build the picker without showing it. Separated so it can be inspected — a shown
        popup grabs the mouse, which makes the feature untestable otherwise.

        **Nothing is truncated.** The old menu capped at 40 and told you to narrow the
        template, which is backwards: with the whole registry offered, the list is exactly
        where you'd want to search. The picker is bounded and filterable instead.
        """
        if row is None or column is None:
            index = self._grid.currentIndex()
            if not index.isValid():
                return None
            row, column = index.row(), index.column()
        slot = self.slot_for_column(column)
        if slot is None or row >= len(self._instances):
            return None
        instance = self._instances[row].name

        if slot.type != "registry":
            return PickerPopup([], header=f"{slot.path} takes {slot.describe()}",
                               parent=self)

        found, note = self.candidates_for(row, slot)
        picker = PickerPopup(found, header=f"{instance}.{slot.path} — {note}",
                             parent=self)
        picker.chosen.connect(lambda value: self.commit_cell(row, column, value))
        return picker

    def template_for(self, path) -> str:
        """The per-column override if this column opted in, else the bar's.

        Membership, not truthiness: an override of ``""`` means "this column opted out of
        suggestions entirely", which is a different answer from "this column has no
        override" and must not fall through to the bar.
        """
        if path in self._overrides:
            return self._overrides[path]
        return self._template

    def candidates_for(self, row, slot) -> tuple:
        """``(suggestions, note)`` for one cell.

        Falling back to the whole registry when there's no template is deliberate: the
        editor must stay usable before anyone has authored one, and a completer over 14,260
        entries is still better than nothing. What it must NOT do is fall back when a
        template exists but couldn't resolve — that would quietly suggest every stairs block
        in the pack while looking like it worked.
        """
        everything = self.registry_entries(slot.registry_type)
        if not self._template_bar.suggesting():
            return everything, f"suggestions off — all {len(everything):,} in {slot.registry_type}"
        text = self.template_for(slot.path)
        if not text or self._dump is None or row >= len(self._instances):
            return everything, f"{len(everything):,} in {slot.registry_type}"

        instance = self._instances[row].name
        try:
            resolved = resolve(parse(text),
                               context_for(self._store, self.blueprint_name, instance, slot))
            found = [r.values["id"] for r in evaluate(
                candidate_query(resolved, slot), packdump=self._dump).rows]
        except (UnresolvedParam, QueryError) as e:
            return [], f"no suggestions — {e}"
        if not found:
            return [], self._explain_empty(resolved, slot)
        return found, f"{len(found)} suggested  ({text})"

    def _explain_empty(self, resolved, slot) -> str:
        """Say what was actually searched for, and which word killed it.

        Reporting the *template* ("nothing matches '@slot'") is useless — the whole point of
        a parameter is that you can't see what it became. And the usual cause is a single
        dead word: `@slot` on a `*.base` column searches for the literal token `base`, which
        a slot name supplies and almost no id contains.
        """
        tokens = []
        for clause in ([resolved] if isinstance(resolved, Cmp) else
                       getattr(resolved, "clauses", [])):
            if isinstance(clause, Cmp) and clause.op == "matches_tokens":
                tokens = [str(t) for t in clause.value]
                break
        if not tokens:
            return "nothing matches here"

        # Per-word counts rather than a verdict. "base works alone" is technically true and
        # useless — it matches 4 ids out of 14,260, and seeing that number is what tells
        # you which word is starving the search.
        parts = " + ".join(f"{t} ({self._alone(t, slot)})" for t in tokens)
        return f"no match for  {parts}"

    def _alone(self, token, slot) -> int:
        query = Query(scope=Registry(slot.registry_type), select=[Id],
                      filter=Cmp(Id, "matches_tokens", [token]))
        try:
            return len(evaluate(query, packdump=self._dump).rows)
        except QueryError:
            return 0

    def _on_cell_changed(self, row, column, *_previous):
        """Report against the cell you're standing on — resolution is per-cell, so a
        template can be perfect on one row and unresolvable on the next."""
        slot = self.slot_for_column(column)
        if slot is None or row < 0:
            return
        if not self._template_bar.suggesting() and slot.type == "registry":
            # Checked before the template, or a column that opted out would report "suggests
            # everything (opted out)" while the box above it is what is actually doing that.
            self._template_bar.report(self.candidates_for(row, slot)[1])
            return
        text = self.template_for(slot.path)
        if not text:
            self._template_bar.report(
                f"{slot.path} suggests everything (opted out)"
                if slot.path in self._overrides else "no template — suggesting everything")
            return
        if slot.type != "registry":
            self._template_bar.report(f"{slot.path} takes {slot.describe()}, not suggestions")
            return
        found, note = self.candidates_for(row, slot)
        self._template_bar.report(note, bad=not found)

    def _on_template_changed(self):
        self._template = self._template_bar.text()
        self._overrides = self._overrides_panel.overrides()
        current = self._grid.currentIndex()
        if current.isValid():
            self._on_cell_changed(current.row(), current.column())
        self.save_config()

    def focus_instance(self, name: str) -> bool:
        """Put the cursor on one instance's row, scrolling it into view.

        A blueprint tab renders the whole blueprint — there is no such thing as a
        one-instance tab, and there shouldn't be: the grid is the point. But clicking a
        *specific* instance in the panel and landing on row 0 of a 40-row palette throws
        away the only thing that click said. Returns False if the instance isn't in this
        grid (a filtered view may legitimately exclude it), so the caller can say so.
        """
        for row, instance in enumerate(self._instances):
            if instance.name == name:
                self._grid.setCurrentCell(row, 0)
                self._grid.scrollToItem(self._grid.item(row, 0),
                                        QAbstractItemView.PositionAtCenter)
                return True
        return False

    def _remember_widths(self):
        """Capture what the header currently shows, then persist it.

        Keyed by slot PATH rather than column index, so a width survives slots being added,
        removed or reordered — the same reason `_build_grid` carries them across a rebuild.
        """
        for column in range(self._grid.columnCount()):
            header = self._grid.horizontalHeaderItem(column)
            if header is not None and not self._grid.isColumnHidden(column):
                self._column_widths[header.text()] = self._grid.columnWidth(column)
        self.save_config()

    def renderer_config(self) -> dict:
        """Everything about *how this view is rendered*, as opposed to what it selects.

        Deliberately none of it touches the blueprint: §5.3's separability test — strip
        every recommendation and the blueprint is byte-for-byte still itself, fully
        functional, just less convenient.
        """
        config = {}
        if self._template:
            config["suggest"] = self._template
        if self._overrides:
            config["suggest_overrides"] = dict(self._overrides)
        if self._collapsed:
            config["collapsed"] = sorted(self._collapsed)
        if self._column_widths:
            config["column_widths"] = dict(self._column_widths)
        # Written even when False, unlike everything above it: the others are absent-means-
        # nothing, but a checkbox the user deliberately turned OFF has to survive as off,
        # and `config.get("follow_tree", False)` cannot tell "never set" from "unset by me"
        # if the key is omitted.
        config["follow_tree"] = self._follow_tree.isChecked()
        return config

    def save_config(self):
        """Push the renderer config out. A no-op on an unsaved tab — nowhere to put it
        yet, and the tab keeps it in memory until there is."""
        self._on_config_changed(self.renderer_config())

    def instance_names(self, blueprint):
        try:
            return [i.name for i in self._store.instances(blueprint)]
        except BlueprintError:
            return []

    # --- rendering ---------------------------------------------------------

    def set_packdump(self, packdump):
        """Adopt a newly imported dump (design 3.1).

        ``reload()`` re-evaluates the query and rebuilds the grid, and it already keeps the
        arrangement the user made — folded groups and column widths — so a packdump import
        costs a repaint rather than the layout.
        """
        self._dump = packdump
        self.reload()

    def reload(self):
        self._loading = True
        try:
            # Remember folded groups before the tree is thrown away and rebuilt, for the
            # same reason column widths are remembered: an edit shouldn't undo the view
            # the user arranged.
            self._collapsed = {path for path, item in self._tree_items.items()
                               if item.childCount() and not item.isExpanded()} \
                if self._tree_items else self._collapsed

            # Evaluating the query is what decides the columns and the rows. `AllSlots`
            # expands here, so a slot added to the schema shows up next time this view is
            # opened rather than being frozen out of it.
            result = evaluate(self.query, blueprint_store=self._store,
                              packdump=self._dump)
            paths = [c.name for c in result.columns if c.name != "id"]
            self._slots = [self._store.slot(self.blueprint_name, p) for p in paths]
            names = [r.entry_id for r in result.rows if r.entry_id]
            by_name = {i.name: i for i in self._store.instances(self.blueprint_name)}
            self._instances = [by_name[n] for n in names if n in by_name]
            self._orphans = {o.instance: o for o in
                             self._store.orphans(self.blueprint_name)}
            # Bindings pointing at something the packdump no longer has. Derived here on
            # every reload rather than stored, for the reason `find_binding_orphans` gives:
            # it is the dump changing underneath the data, so a cached answer would be
            # wrong the moment a dump is imported — and `set_packdump` calls straight
            # through to `reload`, which is what makes this the right place to compute it.
            #
            # With no dump at all every registry binding reports as dangling, which is the
            # right answer for the Errors panel — it cannot verify any of them — and the
            # wrong one to PAINT. A grid tinted end to end says "these are all broken" when
            # the truth is "nothing has been checked", so the tint stays off until there is
            # something to check against.
            self._dangling = {(o.instance, o.slot_path): o for o in
                              self._store.binding_orphans(self.blueprint_name, self._dump)
                              } if self._dump is not None else {}
            self._build_tree()
            self._build_grid()
            self._apply_column_visibility()
            self._overrides_panel.rebuild(self._slots, self._overrides)
            self._paint_banner()
        finally:
            self._loading = False

    def _on_slot_dropped(self, path, parent_path, index):
        if parent_path == path or (parent_path or "").startswith(path + "."):
            return                         # the store would refuse; don't bother it
        self._guarded(lambda: self._store.move_slot(
            self.blueprint_name, path,
            parent=parent_path if parent_path is not None else None, position=index))
        self._select_path(self._moved_path(path, parent_path))

    @staticmethod
    def _moved_path(path, parent_path):
        name = path.rsplit(".", 1)[-1]
        return f"{parent_path}.{name}" if parent_path else name

    def _apply_column_visibility(self, *_args):
        """A column is hidden when any group above it is collapsed in the tree."""
        if not self._slots:
            return
        collapsed = {path for path, item in self._tree_items.items()
                     if item.childCount() and not item.isExpanded()}
        following = self._follow_tree.isChecked()
        for column, slot in enumerate(self._slots):
            hidden = following and any(
                ancestor in collapsed for ancestor in _ancestors(slot.path))
            self._grid.setColumnHidden(column, hidden)
        if not self._loading and collapsed != self._collapsed:
            self._collapsed = collapsed
            self.save_config()

    def _toggle_overrides(self, shown):
        self._overrides_toggle.setText(
            ("▾  " if shown else "▸  ") + "per-column suggestions")
        self._overrides_panel.setVisible(shown)

    def _build_tree(self):
        self._tree.clear()
        self._tree_items = {}
        nodes = {}
        for slot in self._store.slots(self.blueprint_name):
            item = QTreeWidgetItem([slot.name, slot.describe()])
            item.setData(0, _ROLE_PATH, slot.path)
            item.setData(0, _ROLE_KIND, slot.kind)
            item.setForeground(1, Qt.darkGray)
            item.setToolTip(0, slot.path)
            if slot.is_group:
                item.setForeground(0, Qt.gray)
            parent_path = slot.path.rsplit(".", 1)[0] if "." in slot.path else None
            if parent_path and parent_path in nodes:
                nodes[parent_path].addChild(item)
            else:
                self._tree.addTopLevelItem(item)
            nodes[slot.path] = item
            self._tree_items[slot.path] = item
            # Rebuilding the tree would otherwise re-open every group the user folded away.
            item.setExpanded(slot.path not in self._collapsed)
        self._tree.resizeColumnToContents(0)

    def _build_grid(self):
        # Every edit rebuilds the whole table, so column widths have to be carried across
        # or the user's sizing is undone by their own typing. Keyed by slot path, not
        # index, so they survive slots being added, removed or reordered too.
        self._column_widths.update({
            self._grid.horizontalHeaderItem(c).text(): self._grid.columnWidth(c)
            for c in range(self._grid.columnCount())
            if self._grid.horizontalHeaderItem(c) is not None
        })

        self._grid.clear()
        self._grid.setRowCount(len(self._instances))
        self._grid.setColumnCount(len(self._slots))
        self._grid.setHorizontalHeaderLabels([s.path for s in self._slots])
        self._grid.setVerticalHeaderLabels([i.name for i in self._instances])

        gap_brush = style.qt_colour(style.GAP)
        # The whole rectangle in one query, rather than one lookup per row — each of
        # which used to rebuild the blueprint's slot tree. See `all_bindings`.
        every = self._store.all_bindings(self.blueprint_name)
        for row, instance in enumerate(self._instances):
            orphaned = instance.name in self._orphans
            bound = {} if orphaned else every.get(instance.name, {})
            for column, slot in enumerate(self._slots):
                binding = bound.get(slot.path)
                item = QTableWidgetItem(binding.value if binding else "")
                item.setToolTip(f"{instance.name}.{slot.path} — {slot.describe()}")
                if binding is None:
                    # A gap is the deliverable, so it gets painted rather than left blank.
                    item.setBackground(gap_brush)
                elif binding.owner == "action":
                    # Dimmed amber and NOT editable. Taking a cell away from the action that
                    # generates it is a real decision, and it should never be something you
                    # do by double-clicking and clicking away; that made ownership change
                    # by accident, silently. Double-click now asks (see _maybe_claim).
                    item.setForeground(style.qt_colour(style.OWNER_ACTION_LOCKED))
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setData(_ROLE_OWNER, binding.action_ref or "an action")
                    item.setToolTip(
                        item.toolTip() +
                        f"\nmanaged by '{binding.action_ref}' — double-click to take it over")
                # Painted last and as a BACKGROUND, so it survives the ownership branch
                # above instead of competing with it: ownership is carried by the
                # foreground, and an action-owned cell whose entry vanished has to be able
                # to say both things at once. The cell stays editable — the fix for a
                # dangling binding is usually to type a new value into it.
                dangling = self._dangling.get((instance.name, slot.path))
                if dangling is not None:
                    item.setBackground(style.qt_colour(style.DANGLING))
                    item.setToolTip(f"{item.toolTip()}\n⚠ {dangling.detail}")
                if orphaned:
                    item.setFlags(Qt.ItemIsEnabled)
                self._grid.setItem(row, column, item)

        # Fit-to-contents is only ever a *starting* width, and it's a bad one on its own:
        # an empty column shrinks to its header, so a gap-heavy blueprint (the normal case)
        # renders columns too narrow to show the ids you're about to put in them.
        self._grid.resizeColumnsToContents()
        for column, slot in enumerate(self._slots):
            remembered = self._column_widths.get(slot.path)
            self._grid.setColumnWidth(
                column, remembered if remembered
                else max(self._grid.columnWidth(column), _MIN_COLUMN_WIDTH))
        self._grid.verticalHeader().setMinimumWidth(90)

        total = len(self._slots) * len(self._instances)
        filled = sum(len(every.get(i.name, {}))
                     for i in self._instances if i.name not in self._orphans)
        instances = len(self._instances)
        self._summary.setText(
            f"{instances} instance{'' if instances == 1 else 's'} × {len(self._slots)} "
            f"slots — {filled}/{total} filled, {total - filled} gaps"
            if total else "no slots yet")

    def _paint_banner(self):
        if not self._orphans:
            self._banner.hide()
            return
        names = ", ".join(sorted(self._orphans)[:6])
        first = next(iter(self._orphans.values()))
        self._banner.setStyleSheet(
            f"background: {style.BG_CHROME}; color: {style.TEXT}; font-size: 12px; "
            f"border-bottom: 2px solid {style.ERROR};")
        self._banner.setText(
            f"<b>{len(self._orphans)} orphaned instance(s)</b> — {names}. A schema change "
            f"to <code>{first.slot_path}</code> needs resolving before actions can touch "
            f"this blueprint. Resolve them in the Errors panel (Ctrl+`).")
        self._banner.show()

    # --- editing -----------------------------------------------------------

    def managing_action(self, row, column):
        """The action_ref managing this cell, or None when the cell is yours."""
        item = self._grid.item(row, column)
        return item.data(_ROLE_OWNER) if item is not None else None

    def clear_cell(self, row, column):
        """Delete/Backspace empties a cell — back to being a gap, which is a real state."""
        if self.managing_action(row, column):
            self._refuse_locked(row, column)
            return
        item = self._grid.item(row, column)
        if item is None or not (item.flags() & Qt.ItemIsEditable):
            return
        if item.text():
            self.commit_cell(row, column, "")

    def begin_edit(self, row, column):
        """Enter opens the editor — or asks about ownership if the cell isn't yours."""
        if self.managing_action(row, column):
            self._maybe_claim(row, column)
            return
        item = self._grid.item(row, column)
        if item is not None and item.flags() & Qt.ItemIsEditable:
            self._grid.editItem(item)

    def _refuse_locked(self, row, column):
        who = self.managing_action(row, column)
        self._template_bar.report(f"'{who}' manages this cell — double-click to take it",
                                  bad=True)

    def _maybe_claim(self, row, column):
        """Double-clicking an action-managed cell asks before handing it over.

        Ownership used to change by accident: the cell was editable, so double-click then
        click away silently rewrote it as yours. Taking a value away from the thing that
        generates it is a decision, so it gets asked as one — and the question names the
        action, because "which job wrote this?" is the thing you actually want to know.
        """
        who = self.managing_action(row, column)
        if not who:
            return
        instance = self._instances[row].name
        slot = self._slots[column]
        value = self._grid.item(row, column).text()
        # Same wording as the tag side (`MainWindow._confirm_tag_takeover`), because 3.2.2
        # says blueprint conflicts are "identical to tag assignment conflicts" and the two
        # shouldn't describe one rule two ways.
        answer = QMessageBox.question(
            self, "Take ownership",
            f"'{slot.path}' on {instance} is managed by '{who}', which set it to "
            f"'{value}'.\n\nTaking ownership will prevent that action from updating it "
            f"on future runs.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if not self._guarded(lambda: self._store.claim(
                self.blueprint_name, instance, slot.path)):
            return
        self._template_bar.report(f"{instance}.{slot.path} is yours now")
        # Straight into the editor: you double-clicked to edit it, so the confirmation
        # shouldn't cost you a second double-click.
        item = self._grid.item(row, column)
        if item is not None:
            QTimer.singleShot(0, lambda: self._grid.editItem(item))

    def commit_cell(self, row, column, text):
        if self._loading:
            return
        instance = self._instances[row].name
        slot = self._slots[column]
        key = (self.blueprint_name, instance, slot.path)
        # Read before writing: the cell's whole prior STATE, owner included, is what undo
        # has to put back (design 9.3.3). Recorded only after the store accepts the write,
        # so a refused bind never leaves a phantom entry on the stack.
        prior = self._undo.capture("binding", key)
        try:
            if text:
                self._store.bind(self.blueprint_name, instance, slot.path, text,
                                 owner="user")
            else:
                self._store.unbind(self.blueprint_name, instance, slot.path)
        except BlueprintError as e:
            QMessageBox.warning(self, "Can't bind", str(e))
        else:
            after = self._undo.capture("binding", key)
            if after != prior:
                self._undo.push(Batch(f"{instance}.{slot.path}",
                                      [Edit("binding", key, prior, after)]))
        self.reload()
        self.changed.emit()
        self._keep_cell(row, column)

    # --- undo (design 9.3.3) -----------------------------------------------
    #
    # The tab owns the stack, which is what "per tab" means: Ctrl+Z here reverses what was
    # typed here. `MainWindow._move_history` finds these by duck-typing the focused tab, so
    # the registry table and this one answer the same two method names without either
    # knowing about the other.

    def undo(self):
        self._after_move(self._undo.undo())

    def redo(self):
        self._after_move(self._undo.redo())

    def _after_move(self, batch):
        if batch is None:
            return
        self.reload()
        self.changed.emit()
        # Put the cursor on what just changed. An undo you cannot see is indistinguishable
        # from one that did nothing, which is how the whole feature gets mistrusted.
        first = batch.edits[0]
        self.focus_cell(first.key[1], first.key[2])

    def focus_cell(self, instance, slot_path) -> bool:
        rows = {name.name: i for i, name in enumerate(self._instances)}
        columns = {slot.path: i for i, slot in enumerate(self._slots)}
        if instance not in rows or slot_path not in columns:
            return False
        self._grid.setCurrentCell(rows[instance], columns[slot_path])
        return True

    def _forget_picker(self, picker):
        """Drop the tab's reference when a picker retires, so nothing later asks a dead
        widget to reposition itself."""
        if self._picker is picker:
            self._picker = None
            self._picker_cell = None

    def _editing_elsewhere(self, row, column) -> bool:
        """Is the user's attention already on a different cell than (row, column)?

        Two independent signals, because they become true at different moments during a
        cell-to-cell double-click: the view enters EditingState on the new cell, and the
        suggestion picker re-attaches to the new cell's editor.
        """
        current = self._grid.currentIndex()
        if (self._grid.state() == QAbstractItemView.EditingState
                and current.isValid() and (current.row(), current.column()) != (row, column)):
            return True
        return (self._picker is not None and self._picker.isVisible()
                and self._picker_cell not in (None, (row, column)))

    def advance_after_commit(self):
        """Arm the spreadsheet step-down for the commit that is about to happen.

        A flag rather than an argument because the two paths that end an edit with Enter
        both run inside Qt's own machinery — the delegate's event filter and the picker's —
        and neither is on the call path that eventually reaches `commit_cell`.
        """
        self._advance = True

    def _take_advance(self) -> bool:
        armed, self._advance = self._advance, False
        return armed

    def _keep_cell(self, row, column):
        """Leave the cursor where the user's attention is.

        `reload()` rebuilds the grid wholesale, so finishing an edit dropped the current
        cell and with it the ability to arrow onward — every next cell cost a mouse click.
        Deferred a turn because the view is still tearing down its editor at this point and
        would clobber a selection set now.

        Which cell that *is* depends on how the edit ended. Enter or focus-out ends it in
        place, and the answer is the cell that was being edited. Clicking a different cell
        also ends it — and then restoring the old cell is actively wrong: it leaves the
        highlight on the cell you left while the editor opens on the one you clicked. Qt
        set the current cell during that press and won't set it again, so if this puts it
        back, nothing corrects it afterwards.
        """
        # Shared by both passes below: whichever one first sees a press decides the target,
        # and the other must not undo it. `take_pressed_cell` is read-and-clear, so without
        # this the second pass reads None and falls back to the cell being left.
        target = [(row, column)]

        # Enter steps down a row, the way a spreadsheet does — filling a column of 47
        # instances should be type/Enter/type/Enter, not a reach for the mouse between
        # every one. Read here, once, because `restore()` runs twice and a read-and-clear
        # in there would leave the second pass disagreeing with the first.
        #
        # Stopping at the last row rather than wrapping: wrapping to the top of the next
        # column looks like the cursor jumped somewhere random, and the bottom of a column
        # is exactly where you stop to think anyway. A click still wins over this below —
        # clicking elsewhere is a statement about where you want to be next.
        if self._take_advance() and row + 1 < len(self._instances):
            target[0] = (row + 1, column)

        def restore():
            # Read the press HERE rather than in `_keep_cell`, because of the order the
            # events actually arrive in — verified by instrumenting the real sequence:
            #
            #   1. the click takes focus from the editor, which closes and COMMITS it,
            #      so this whole function runs before the click reaches the view;
            #   2. `mousePressEvent` then selects the clicked cell and records it;
            #   3. the deferred pass below runs — and only now is the press visible.
            #
            # Read a step earlier and it is always None, which is why following the press
            # appeared to do nothing.
            pressed = self._grid.take_pressed_cell()
            if pressed is not None and pressed != (row, column):
                target[0] = pressed        # follow the click, don't fight it
            at_row, at_column = target[0]

            if at_row >= self._grid.rowCount() or at_column >= self._grid.columnCount():
                return
            if self._editing_elsewhere(at_row, at_column):
                return
            self._grid.setCurrentCell(at_row, at_column)
            self._grid.setFocus(Qt.OtherFocusReason)

        # Twice on purpose: now, so the grid never renders a frame with nothing selected,
        # and again next turn, because closing the editor happens after this returns and
        # clears the selection on its way out.
        restore()
        QTimer.singleShot(0, restore)

    def _new_instance(self):
        name, ok = _ask(self, "New Instance", "Instance name", "granite")
        if not ok:
            return
        self._guarded(lambda: self._store.create_instance(self.blueprint_name, name))

    def _new_group(self):
        dialog = SlotDialog(self._store, parent=self, parent_group=self._selected_group(),
                            groups=self._group_paths(), group_mode=True,
                            registry_types=self._registry_types())
        if not dialog.exec():
            return
        made = self._guarded(lambda: self._store.add_group(
            self.blueprint_name, dialog.result_name, parent=dialog.result_parent))
        self._select_path(made.path if made else None)

    def _new_slot(self):
        dialog = SlotDialog(self._store, parent=self, parent_group=self._selected_group(),
                            groups=self._group_paths(), sticky=self._sticky,
                            registry_types=self._registry_types())
        if not dialog.exec():
            return

        def create():
            made = None
            for name in dialog.result_names:
                made = self._store.add_slot(
                    self.blueprint_name, name, dialog.result_type,
                    parent=dialog.result_parent, registry_type=dialog.result_registry,
                    ref_blueprint=dialog.result_ref, enum_values=dialog.result_enum)
            return made

        instances = len(self._store.instances(self.blueprint_name))
        made = self._guarded(create)
        if made is not None and instances:
            # 3.2.2: adding a slot propagates silently and "a non-modal notification
            # surfaces the change". Silent propagation is right — nothing is at risk — but
            # unannounced it means a blueprint quietly grows gaps across every instance.
            names = ", ".join(dialog.result_names)
            self.status.emit(
                f"Added {names} to '{self.blueprint_name}'. {instances} existing "
                f"instance(s) now have {'this slot' if len(dialog.result_names) == 1 else 'these slots'}, unset.")
        # Remember what kind of slot this was. Schemas get built in runs — five forms of
        # one cut, then five of the next — so the second slot should cost a name.
        self._sticky = {"type": dialog.result_type, "registry": dialog.result_registry,
                        "ref": dialog.result_ref, "enum": dialog.result_enum}
        # Selecting what was just made is how the PARENT sticks, without a second
        # remembered value that would fight clicking blank space to mean "top level".
        self._select_path(made.path if made else None)

    def _registry_types(self):
        return list(self._dump.registry) if self._dump is not None else []

    def _select_path(self, path):
        if not path:
            return
        for index in range(self._tree.topLevelItemCount()):
            found = self._find_item(self._tree.topLevelItem(index), path)
            if found is not None:
                self._tree.setCurrentItem(found)
                return

    def _find_item(self, item, path):
        if item.data(0, _ROLE_PATH) == path:
            return item
        for index in range(item.childCount()):
            found = self._find_item(item.child(index), path)
            if found is not None:
                return found
        return None

    def _duplicate_slot(self, path):
        slot = self._store.slot(self.blueprint_name, path)
        noun = "group" if slot.is_group else "slot"
        name, ok = _ask(self, f"Duplicate {noun.title()}",
                        f"Name for the copy of '{path}'", f"{slot.name}_copy")
        if not ok:
            return
        made = self._guarded(lambda: self._store.duplicate_slot(
            self.blueprint_name, path, name))
        self._select_path(made.path if made else None)

    def _group_paths(self):
        return [s.path for s in self._store.slots(self.blueprint_name) if s.is_group]

    def _selected_group(self):
        """Only a *suggestion* for the dialog's Inside field, never the decision — see
        SlotDialog."""
        items = self._tree.selectedItems()
        if not items:
            return None
        if items[0].data(0, _ROLE_KIND) == "group":
            return items[0].data(0, _ROLE_PATH)
        path = items[0].data(0, _ROLE_PATH) or ""
        return path.rsplit(".", 1)[0] if "." in path else None

    def _on_slot_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        path = item.data(0, _ROLE_PATH)
        is_group = item.data(0, _ROLE_KIND) == "group"

        menu = QMenu(self)
        menu.addAction("Rename…", lambda: self._rename_slot(path))
        if not is_group:
            menu.addAction("Change type…", lambda: self._retype_slot(path))
        # Cuts share their form set, so duplicating a group is how the schema's second
        # axis gets built.
        menu.addAction("Duplicate as…" if is_group else "Duplicate…",
                       lambda: self._duplicate_slot(path))
        menu.addSeparator()
        menu.addAction("Remove…", lambda: self._remove_slot(path))
        menu.exec(self._tree.mapToGlobal(pos))

    def _rename_slot(self, path):
        impact = self._store.preview_rename_slot(self.blueprint_name, path)
        new_name, ok = _ask(self, "Rename Slot", "New name", path.rsplit(".", 1)[-1])
        if not ok:
            return
        # A rename is harmless to *data* — bindings migrate — but it is exactly what breaks
        # a shape contract, since a mapping asks for a slot by path. So this needs asking
        # even when no instance is bound, which the binding-count check alone would skip.
        breakage = self._job_impact(
            project_rename(self._store.slots(self.blueprint_name), path, new_name),
            "rename")
        if impact.bound or breakage:
            body = ""
            if impact.bound:
                body += (f"This updates {len(impact.bound)} existing instance(s). The old "
                         f"name will no longer exist.\n\nBindings migrate — nothing is "
                         f"orphaned.\n\n")
            if QMessageBox.question(
                    self, "Rename slot",
                    f"Rename '{path}' to '{new_name}'?\n\n{body}{breakage}",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No if breakage else QMessageBox.Yes) != QMessageBox.Yes:
                return
        self._guarded(lambda: self._store.rename_slot(self.blueprint_name, path, new_name))

    def _remove_slot(self, path):
        impact = self._store.preview_remove_slot(self.blueprint_name, path)
        if impact.blocked:
            QMessageBox.warning(self, "Can't change the schema", impact.blocked)
            return
        breakage = self._job_impact(
            project_removal(self._store.slots(self.blueprint_name), path), "removal")
        if impact.destructive or breakage:
            body = ""
            if impact.destructive:
                body += (f"{len(impact.bound)} instance(s) have a binding here: "
                         f"{', '.join(impact.bound[:8])}.\n\nEvery one of them becomes "
                         f"ORPHANED and locked out of actions until you resolve it in the "
                         f"Errors panel. Their data is kept until then.\n\n")
            # Only the destructive case gets the recovery note. A slot nothing is bound to
            # is removed silently by 3.2.2 and takes no snapshot, so promising one would be
            # a lie — and warning about data loss where there is none trains people to
            # click through the warning that matters.
            if impact.destructive:
                if not confirm_destructive(
                        self, "Remove slot", f"Remove '{path}'?\n\n{body}{breakage}",
                        self._store.db_path, ok="Remove slot"):
                    return
            elif QMessageBox.question(
                    self, "Remove slot", f"Remove '{path}'?\n\n{body}{breakage}",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        self._guarded(lambda: self._store.remove_slot(self.blueprint_name, path))

    def _retype_slot(self, path):
        slot = self._store.slot(self.blueprint_name, path)
        dialog = SlotDialog(self._store, parent=self, slot=slot,
                            registry_types=self._registry_types())
        if not dialog.exec():
            return
        try:
            impact = self._store.preview_retype_slot(
                self.blueprint_name, path, dialog.result_type,
                registry_type=dialog.result_registry, ref_blueprint=dialog.result_ref,
                enum_values=dialog.result_enum)
        except BlueprintError as e:
            QMessageBox.warning(self, "Can't change the type", str(e))
            return
        if impact.blocked:
            QMessageBox.warning(self, "Can't change the schema", impact.blocked)
            return

        # A retype breaks a shape contract whether or not any instance is bound — the
        # mapping asked for a *type*. So this is computed before the destructive branch,
        # which only fires when there's data at risk.
        breakage = self._job_impact(
            project_retype(self._store.slots(self.blueprint_name), path,
                           dialog.result_type, registry_type=dialog.result_registry,
                           ref_blueprint=dialog.result_ref,
                           enum_values=dialog.result_enum), "type change")

        auto = False
        if impact.destructive:
            # 3.2.2: auto-coerce is offered only when the change is provably lossless;
            # otherwise the dialog shows Retype & Orphan or Cancel, and nothing else.
            box = QMessageBox(self)
            box.setWindowTitle("Change slot type")
            box.setIcon(QMessageBox.Warning)
            tail = f"\n\n{breakage}" if breakage else ""
            if impact.can_coerce:
                box.setText(
                    f"Change '{path}' to {dialog.result_type}?\n\n"
                    f"All {len(impact.bound)} existing binding(s) convert cleanly, so "
                    f"nothing has to be orphaned.{tail}")
                coerce_button = box.addButton("Retype && Auto-Coerce",
                                              QMessageBox.AcceptRole)
            else:
                shown = ", ".join(f"{i} = {v!r}" for i, v in impact.problematic[:5])
                box.setText(
                    f"Change '{path}' to {dialog.result_type}?\n\n"
                    f"{len(impact.problematic)} binding(s) cannot convert: {shown}.\n\n"
                    f"Every instance with a binding here becomes ORPHANED; the "
                    f"problematic values wait in the Errors panel for you to "
                    f"re-bind.{tail}")
                coerce_button = None
            orphan_button = box.addButton("Retype && Orphan", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            # Three outcomes, so `confirm_destructive` does not fit — but the standing note
            # is the same, and it applies to the auto-coerce branch too: converting cleanly
            # still rewrites every binding, and undo does not reach schema (9.3.3).
            box.setInformativeText(recovery_note(self._store.db_path))
            box.exec()
            clicked = box.clickedButton()
            if clicked is None or clicked not in (coerce_button, orphan_button):
                return
            auto = clicked is coerce_button
        elif breakage and QMessageBox.question(
                self, "Change slot type",
                f"Change '{path}' to {dialog.result_type}?\n\n"
                f"No instance is bound here, so no data is at risk.\n\n{breakage}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        self._guarded(lambda: self._store.retype_slot(
            self.blueprint_name, path, dialog.result_type,
            registry_type=dialog.result_registry, ref_blueprint=dialog.result_ref,
            enum_values=dialog.result_enum, auto_coerce=auto))

    def _on_instance_menu(self, pos):
        row = self._grid.rowAt(pos.y())
        if row < 0 or row >= len(self._instances):
            return
        column = self._grid.columnAt(pos.x())
        slot = self.slot_for_column(column)
        cell = (row, column) if slot is not None and slot.type == "registry" else None
        self._popup(self._instance_menu(row, cell),
                    self._grid.viewport().mapToGlobal(pos))

    def _on_row_menu(self, pos):
        """The same menu, off the instance's own name.

        Selecting the row first: acting on a row you did not visibly select is how you
        delete the wrong one, and a header click alone does not move the grid's selection.
        """
        header = self._grid.verticalHeader()
        row = header.logicalIndexAt(pos)
        if row < 0 or row >= len(self._instances):
            return
        # `selectRow` selects nothing on a SingleSelection/SelectItems grid, so move the
        # current cell instead — that is what actually highlights.
        self._grid.setCurrentCell(row, max(self._grid.currentColumn(), 0))
        self._popup(self._instance_menu(row), header.mapToGlobal(pos))

    def _popup(self, menu, where):
        """The blocking call, alone in a method — everything worth testing about a context
        menu happens before it, and `exec` never returns until someone clicks."""
        menu.exec(where)

    def _instance_menu(self, row, cell=None) -> QMenu:
        """Everything you can do to one instance. `cell` adds the per-cell entry, which the
        name column has no meaningful answer for."""
        instance = self._instances[row].name
        menu = QMenu(self)
        if cell is not None:
            menu.addAction("Suggestions…", lambda: self.show_suggestions(*cell))
            menu.addSeparator()
        menu.addAction("Rename instance…", lambda: self._rename_instance(instance))
        menu.addAction("Delete instance…", lambda: self._delete_instance(instance))
        return menu

    def _rename_instance(self, instance):
        name, ok = _ask(self, "Rename Instance", "New name", instance)
        if not ok:
            return
        self._guarded(lambda: self._store.rename_instance(self.blueprint_name, instance,
                                                          name))

    def _delete_instance(self, instance):
        bound = len(self._store.bindings(self.blueprint_name, instance))
        if not confirm_destructive(
                self, "Delete instance",
                f"Delete '{self.blueprint_name}:{instance}' and its {bound} binding(s)?",
                self._store.db_path, ok="Delete instance"):
            return
        self._guarded(lambda: self._store.delete_instance(self.blueprint_name, instance))

    def _guarded(self, operation):
        """Run a store call, surface a refusal, and hand back whatever it made."""
        try:
            made = operation()
        except BlueprintError as e:
            QMessageBox.warning(self, "Can't do that", str(e))
            return None
        self.reload()
        self.changed.emit()
        return made


def _ask(parent, title, label, placeholder="") -> tuple:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(360)
    layout = QVBoxLayout(dialog)
    form = QFormLayout()
    field = QLineEdit()
    field.setPlaceholderText(placeholder)
    form.addRow(label, field)
    layout.addLayout(form)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    ok = bool(dialog.exec())
    return field.text().strip(), ok and bool(field.text().strip())


_TOP_LEVEL = "(top level)"


class SlotDialog(QDialog):
    """Add a slot or a group, or change a slot's type. Same fields throughout — a retype is
    just the type half of the same question, and a group is a slot without one.

    **Where it goes is a field, not a side effect of what's selected.** Deriving the parent
    from the tree selection makes "add this at the top level" unreachable once you've
    clicked into a group, because a tree has no natural way to select nothing.
    """

    def __init__(self, store, parent=None, slot=None, parent_group=None, groups=(),
                 group_mode=False, sticky=None, registry_types=()):
        super().__init__(parent)
        self._store = store
        self._slot = slot
        self._group_mode = group_mode
        self.setWindowTitle("Change Slot Type" if slot
                            else ("New Group" if group_mode else "New Slots"))
        self.setMinimumWidth(460)

        sticky = sticky or {}
        self.result_names = [slot.name] if slot else []
        self.result_name = slot.name if slot else ""
        self.result_parent = parent_group
        self.result_type = "string"
        self.result_registry = None
        self.result_ref = None
        self.result_enum = None

        root = QVBoxLayout(self)
        form = QFormLayout()

        self._name = QLineEdit(slot.name if slot else "")
        self._multi = slot is None and not group_mode
        self._name.setPlaceholderText(
            "base, stairs, slab, wall, vertical_slab" if self._multi
            else ("polished" if group_mode else "stairs"))
        if slot:
            self._name.setEnabled(False)      # rename is its own operation
        form.addRow("Names" if self._multi else "Name", self._name)

        self._inside = DropDown()
        if slot is None:
            self._inside.addItem(_TOP_LEVEL)
            self._inside.addItems(list(groups))
            if parent_group and parent_group in groups:
                self._inside.setCurrentText(parent_group)
            form.addRow("Inside", self._inside)

        self._type = DropDown()
        self._type.addItems(SLOT_TYPES)
        # Sticky: a schema is built in runs of the same kind of slot, so defaulting back
        # to 'string' every time taxes the common case to serve the rare one.
        self._type.setCurrentText(slot.type if slot else sticky.get("type", "string"))
        self._type.currentTextChanged.connect(self._sync)
        self._type_label = QLabel("Type")
        form.addRow(self._type_label, self._type)

        # Same suggester as the cell editor, one level up: the registry TYPE rather than an
        # entry within it. 135 of them in a real pack, and a typo here silently creates a
        # slot nothing can ever be bound into.
        self._registry = DropDown()
        self._registry.setEditable(True)
        self._registry.addItems(sorted(registry_types))
        self._registry.setCurrentText(
            slot.registry_type if slot else sticky.get("registry", ""))
        completer = self._registry.completer()
        if completer is not None:
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchContains)
            completer.setMaxVisibleItems(15)
        self._registry.lineEdit().setPlaceholderText(
            f"minecraft:block  ({len(registry_types)} registries)" if registry_types
            else "minecraft:block")
        self._registry_label = QLabel("Registry")
        form.addRow(self._registry_label, self._registry)

        self._ref = DropDown()
        self._ref.addItems(store.names())
        if slot and slot.ref_blueprint:
            self._ref.setCurrentText(slot.ref_blueprint)
        elif sticky.get("ref"):
            self._ref.setCurrentText(sticky["ref"])
        self._ref_label = QLabel("Blueprint")
        form.addRow(self._ref_label, self._ref)

        self._enum = QLineEdit(", ".join(slot.enum_values) if slot
                               else ", ".join(sticky.get("enum") or ()))
        self._enum.setPlaceholderText("early, mid, late")
        self._enum_label = QLabel("Values")
        form.addRow(self._enum_label, self._enum)

        root.addLayout(form)
        hint = QLabel(
            ("Separate names with commas to create several slots at once — they share the "
             "type and location below. " if self._multi else "")
            + "Registry slots accept an entry id from that registry; blueprint slots "
              "accept an instance of the named blueprint, as a pointer that follows "
              "renames.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
        root.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._sync()

    def _sync(self):
        kind = self._type.currentText()
        for widget, label, wanted in (
                (self._registry, self._registry_label, "registry"),
                (self._ref, self._ref_label, "blueprint"),
                (self._enum, self._enum_label, "enum")):
            visible = not self._group_mode and kind == wanted
            widget.setVisible(visible)
            label.setVisible(visible)
        self._type.setVisible(not self._group_mode)
        self._type_label.setVisible(not self._group_mode)
        self.adjustSize()

    def accept(self):
        names = [n.strip() for n in self._name.text().replace("\n", ",").split(",")
                 if n.strip()] if self._multi else \
            ([self._name.text().strip()] if self._name.text().strip() else [])
        if not names:
            QMessageBox.warning(self, "Name required",
                                f"The {'group' if self._group_mode else 'slot'} needs a "
                                f"name.")
            return
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            QMessageBox.warning(self, "Repeated name",
                                f"'{sorted(duplicates)[0]}' appears twice in that list.")
            return

        if self._slot is None:
            chosen = self._inside.currentText()
            self.result_parent = None if chosen == _TOP_LEVEL else chosen
        if self._group_mode:
            self.result_names = names
            self.result_name = names[0]
            super().accept()
            return

        kind = self._type.currentText()
        if kind == "registry" and not self._registry.currentText().strip():
            QMessageBox.warning(self, "Registry required",
                                "A registry slot needs a registry type.")
            return
        if kind == "blueprint" and not self._ref.currentText():
            QMessageBox.warning(self, "Blueprint required",
                                "A blueprint slot needs a blueprint to point at.")
            return
        values = [v.strip() for v in self._enum.text().split(",") if v.strip()]
        if kind == "enum" and not values:
            QMessageBox.warning(self, "Values required", "An enum slot needs values.")
            return

        self.result_names = names
        self.result_name = names[0]
        self.result_type = kind
        # Only the qualifier this type uses. The other widgets still hold whatever they
        # were last showing — the blueprint combo in particular defaults to the first
        # blueprint — and handing those back creates real, wrong foreign keys.
        self.result_registry = (self._registry.currentText().strip() or None
                                if kind == "registry" else None)
        self.result_ref = self._ref.currentText() or None if kind == "blueprint" else None
        self.result_enum = values or None if kind == "enum" else None
        super().accept()


class NewBlueprintDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Blueprint")
        self.setMinimumWidth(420)
        self.result_name = ""
        self.result_description = ""

        root = QVBoxLayout(self)
        form = QFormLayout()
        self._name = QLineEdit()
        self._name.setPlaceholderText("StoneType")
        form.addRow("Name", self._name)
        self._description = QLineEdit()
        self._description.setPlaceholderText(
            "One stone and everything it can be cut into")
        form.addRow("Description", self._description)
        root.addLayout(form)

        hint = QLabel("A blueprint is a shape — a struct without methods. Give it slots, "
                      "then instantiate it once per thing it describes.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
        root.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self):
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required", "The blueprint needs a name.")
            return
        self.result_name = name
        self.result_description = self._description.text().strip()
        super().accept()
