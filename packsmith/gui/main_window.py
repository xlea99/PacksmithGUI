from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QWidget,
    QLabel, QHBoxLayout, QHeaderView, QPushButton, QTabWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QShortcut, QKeySequence

from packsmith.core.profile import Profile, list_profiles
from packsmith.core.packdump import import_packdump
from packsmith.core.db import UserDB
from packsmith.core.tags import TagStore
from packsmith.gui.demo_views import demo_views
from packsmith.gui.table.registry_table_model import RegistryTableModel
from packsmith.gui.table.registry_sort_proxy import RegistrySortProxy
from packsmith.gui.table.registry_table_view import RegistryTableView
from packsmith.gui.table.cells.bool_cell import BoolCellDelegate
from packsmith.gui.table.cells.enum_cell import EnumCellDelegate
from packsmith.gui.table.cells.num_cell import NumCellDelegate
from packsmith.gui.table.cells.str_cell import StrCellDelegate
from packsmith.gui.editor.editor_widget import MonacoEditor
from packsmith.core.runner import run_action
from packsmith.core.packages import PackageIndex
from packsmith.core.bindings import best_guess_bindings, resolve_step
from packsmith.core.files import FileStore
from packsmith.core.history import StepRunStore

_DELEGATES = {
    "bool": BoolCellDelegate,
    "enum": EnumCellDelegate,
    "number": NumCellDelegate,
    "string": StrCellDelegate,
}


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PackSmith")
        self.setMinimumSize(1200, 700)

        # Load data — TEMP: hardcoded dev profile against a small dedicated test pack.
        # (Real profile create/switch UI is a later slice.)
        _PROFILE = "packsmith_test"
        if _PROFILE not in list_profiles():
            self._profile = Profile.create(
                _PROFILE,
                mc_path=r"C:\Users\timbe\curseforge\minecraft\Instances\Packsmith Test",
                loader="forge", loader_version="47.4.10", mc_version="1.20.1",
            )
        else:
            self._profile = Profile.load(_PROFILE)
        self._packdump = import_packdump(self._profile)
        self._db = UserDB(self._profile.root / "profile.db")
        self._tags = TagStore(self._db)

        # Layer 3 services: installed action packages, the real file store (writes to
        # the actual instance on disk), and run history.
        self._packages = PackageIndex(self._profile.packages_dir)
        self._file_store = FileStore(self._db, self._profile.mc_path)
        self._history = StepRunStore(self._db)

        # TEMP dev seed: ensure a few tags exist to work with (idempotent).
        self._seed_tags()

        # Per-tab query models (for undo/redo + refresh coordination)
        self._tab_models = {}     # tab widget -> RegistryTableModel
        self._all_models = []     # every query model, for reevaluate-all
        self._delegates = []      # keep delegate refs alive

        # Build UI
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # Header bar
        header = QHBoxLayout()
        profile_label = QLabel(f"Profile: {self._profile.name}")
        profile_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #d0d0d0;")

        mods_count = len(self._packdump.mods)
        info_label = QLabel(f"{self._packdump.mc_version}  {self._profile.loader}  {self._profile.loader_version}  |  {mods_count} mods")
        info_label.setStyleSheet("font-size: 12px; color: #888888;")

        # Run the REAL removal action — syncs remove-tagged items into the actual
        # item_obliterator.json5 on disk, then refreshes every view.
        obliterate_btn = QPushButton("▶  Run: removal:obliterate")
        obliterate_btn.setFixedHeight(24)
        obliterate_btn.setStyleSheet("""
            QPushButton {
                background-color: #3a2020; color: #d07070;
                border: 1px solid #c05050; padding: 2px 12px;
                font-size: 12px; font-weight: bold;
            }
            QPushButton:hover { background-color: #4a2828; }
        """)
        obliterate_btn.clicked.connect(self._run_removal)

        header.addWidget(profile_label)
        header.addStretch()
        header.addWidget(info_label)
        header.addWidget(obliterate_btn)
        main_layout.addLayout(header)

        # Tab widget
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #3a3a3a; }
            QTabBar::tab {
                background: #2d2d2d; color: #888888;
                border: 1px solid #3a3a3a; padding: 6px 16px;
            }
            QTabBar::tab:selected {
                background: #1e1e1e; color: #d0d0d0;
                border-bottom: 1px solid #1e1e1e;
            }
        """)
        main_layout.addWidget(self._tabs)

        # --- one tab per demo View (each is a raw query, rendered) ---
        for title, query in demo_views():
            self._tabs.addTab(self._build_view_tab(query), title)

        # --- Editor tab ---
        editor_tab = QWidget()
        editor_layout = QVBoxLayout(editor_tab)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        self._editor = MonacoEditor()
        editor_layout.addWidget(self._editor)
        self._tabs.addTab(editor_tab, "Editor")

        test_file = Path(__file__).resolve().parent.parent.parent / "hehe.py"
        if test_file.exists():
            self._editor.load_file(str(test_file))

        # Undo / Redo — dispatch to whichever query tab is active.
        QShortcut(QKeySequence.Undo, self).activated.connect(self._undo)
        QShortcut(QKeySequence.Redo, self).activated.connect(self._redo)

        # Status bar
        items_count = len(self._packdump.registry.get("minecraft:item", {}).get("values", []))
        self.statusBar().showMessage(f"Loaded {items_count} items from minecraft:item")
        self.statusBar().setStyleSheet("color: #888888;")

    def _build_view_tab(self, query) -> QWidget:
        """Build one tab that renders a query: model -> sort proxy -> table, with
        type-aware cell delegates and per-tag edit-mode toggles."""
        model = RegistryTableModel(query, self._packdump, self._tags)
        proxy = RegistrySortProxy()
        proxy.setSourceModel(model)

        table = RegistryTableView()
        table.setModel(proxy)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(RegistryTableView.SelectItems)
        table.setSelectionMode(RegistryTableView.ExtendedSelection)
        table.setSortingEnabled(True)
        table.sortByColumn(0, Qt.AscendingOrder)

        # Row numbers
        vh = table.verticalHeader()
        vh.setDefaultSectionSize(24)
        vh.setSectionsClickable(True)
        vh.setDefaultAlignment(Qt.AlignCenter)
        vh.setStyleSheet("""
            QHeaderView::section {
                background-color: #252525; color: #666666;
                border: 1px solid #3a3a3a; padding: 0 6px; font-size: 11px;
            }
            QHeaderView::section:checked { background-color: #3a5070; color: #d0d0d0; }
        """)

        # Column sizing + delegates, keyed off each column's kind
        h = table.horizontalHeader()
        for col in range(model.columnCount()):
            h.setSectionResizeMode(col, QHeaderView.Interactive)
            if model.headerData(col, Qt.Horizontal) == "id":
                table.setColumnWidth(col, 300)
            elif model.is_tag_column(col):
                table.setColumnWidth(col, 120)
                delegate_cls = _DELEGATES.get(model.tag_type_for_column(col))
                if delegate_cls:
                    delegate = delegate_cls(table)
                    table.setItemDelegateForColumn(col, delegate)
                    self._delegates.append(delegate)
            else:
                table.setColumnWidth(col, 240)
        h.setStretchLastSection(True)

        # Container: edit-toggle bar (only for tag columns) + the table
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        tag_cols = [c for c in range(model.columnCount()) if model.is_tag_column(c)]
        if tag_cols:
            edit_bar = QHBoxLayout()
            edit_bar.addWidget(QLabel("Edit:"))
            for col in tag_cols:
                btn = QPushButton(model.column_tag_name(col))
                btn.setCheckable(True)
                btn.setFixedHeight(24)
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #2d2d2d; color: #888888;
                        border: 1px solid #3a3a3a; padding: 2px 10px; font-size: 12px;
                    }
                    QPushButton:checked {
                        background-color: #3a5070; color: #d0d0d0; border: 1px solid #5080b0;
                    }
                """)
                btn.toggled.connect(
                    lambda checked, m=model, t=table, c=col: self._toggle_edit_mode(m, t, c, checked))
                edit_bar.addWidget(btn)
            edit_bar.addStretch()
            layout.addLayout(edit_bar)

        layout.addWidget(table)

        self._tab_models[tab] = model
        self._all_models.append(model)
        return tab

    def _toggle_edit_mode(self, model, table, col, enabled):
        model.set_editing(col, enabled)
        table.viewport().update()

    def _active_model(self):
        return self._tab_models.get(self._tabs.currentWidget())

    def _undo(self):
        model = self._active_model()
        if model:
            model.undo()

    def _redo(self):
        model = self._active_model()
        if model:
            model.redo()

    # Runs the real removal:obliterate action against the actual instance file store —
    # writes item_obliterator.json5 on disk, records the run to history — then re-runs
    # every view's query so all tabs reflect the new state.
    def _run_removal(self):
        ref = "removal:obliterate"
        try:
            manifest = self._packages.get(ref)
            fn = self._packages.load_callable(ref)
        except (KeyError, AttributeError, FileNotFoundError) as e:
            self.statusBar().showMessage(f"[{ref}] could not load: {e}")
            return
        guesses = best_guess_bindings(manifest, self._tags)
        try:
            mappings, config = resolve_step(manifest, bindings=guesses, config={}, tag_store=self._tags)
        except ValueError as e:
            self.statusBar().showMessage(f"[{ref}] binding error: {e}")
            return
        result = run_action(fn, tag_store=self._tags, packdump=self._packdump,
                            action_ref=ref, mappings=mappings, config=config,
                            file_store=self._file_store, history=self._history)
        for model in self._all_models:
            model.reevaluate()
        msg = f"[{ref}] {result.status}"
        if result.log_lines:
            msg += " — " + result.log_lines[-1][1]
        if result.reason:
            msg += f" — {result.reason}"
        self.statusBar().showMessage(msg)

    # TEMP dev seed — ensures a handful of tags exist to work with (idempotent). Real
    # tag creation is a user action via the UI; this just gives the dev build columns.
    def _seed_tags(self):
        reg = "minecraft:item"
        if not self._tags.definition(reg, "tier"):
            self._tags.define(reg, "tier", "enum", ["early", "mid", "late", "oh my jesus christ wow"])
        if not self._tags.definition(reg, "banned"):
            self._tags.define(reg, "banned", "bool", default=False)
        if not self._tags.definition(reg, "tooltip"):
            self._tags.define(reg, "tooltip", "string")
        if not self._tags.definition(reg, "weight"):
            self._tags.define(reg, "weight", "number")
        if not self._tags.definition(reg, "remove"):
            self._tags.define(reg, "remove", "bool", default=False)

    def closeEvent(self, event):
        self._db.close()
        super().closeEvent(event)
