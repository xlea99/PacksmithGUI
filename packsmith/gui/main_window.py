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
from packsmith.gui.table.registry_table_model import RegistryTableModel
from packsmith.gui.table.registry_sort_proxy import RegistrySortProxy
from packsmith.gui.table.registry_table_view import RegistryTableView
from packsmith.gui.table.cells.bool_cell import BoolCellDelegate
from packsmith.gui.table.cells.enum_cell import EnumCellDelegate
from packsmith.gui.table.cells.num_cell import NumCellDelegate
from packsmith.gui.table.cells.str_cell import StrCellDelegate
from packsmith.gui.editor.editor_widget import MonacoEditor


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PackSmith")
        self.setMinimumSize(1200, 700)

        # Load data
        # TODO This is temp, just for testing
        if "testicles" not in list_profiles():
            self._profile = Profile.create("testicles",
                                           mc_path=r"C:\Users\timbe\curseforge\minecraft\Instances\Personal Pack",
                                           loader="forge",loader_version="47.4.16",mc_version="1.20.1")
        else:
            self._profile = Profile.load("testicles")
        self._packdump = import_packdump(self._profile)
        self._db = UserDB(self._profile.root / "profile.db")
        self._tags = TagStore(self._db)

        # Seed some tags if none exist
        if not self._tags.all_definitions:
            self._seed_test_tags()

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
        info_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        header.addWidget(profile_label)
        header.addStretch()
        header.addWidget(info_label)
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

        # --- Registry tab ---
        registry_tab = QWidget()
        registry_layout = QVBoxLayout(registry_tab)
        registry_layout.setContentsMargins(4, 4, 4, 4)
        registry_layout.setSpacing(6)
        self._build_registry_tab(registry_layout)
        self._tabs.addTab(registry_tab, "Registry")

        # --- Editor tab ---
        editor_tab = QWidget()
        editor_layout = QVBoxLayout(editor_tab)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        self._editor = MonacoEditor()
        editor_layout.addWidget(self._editor)
        self._tabs.addTab(editor_tab, "Editor")

        # Load test file
        test_file = Path(__file__).resolve().parent.parent.parent / "hehe.py"
        if test_file.exists():
            self._editor.load_file(str(test_file))

        # Status bar
        items_count = len(self._packdump.registry.get("minecraft:item", {}).get("values", []))
        self.statusBar().showMessage(f"Loaded {items_count} items from minecraft:item")
        self.statusBar().setStyleSheet("color: #888888;")

    def _build_registry_tab(self, layout):
        """Build the registry table view with edit controls."""
        tag_columns = [d["name"] for d in self._tags.all_definitions.values()]
        self._model = RegistryTableModel(
            self._packdump,
            self._tags,
            "minecraft:item",
            tag_columns=tag_columns,
        )
        self._sort_proxy = RegistrySortProxy(self._tags, tag_columns)
        self._sort_proxy.setSourceModel(self._model)

        self._table = RegistryTableView()
        self._table.setModel(self._sort_proxy)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(RegistryTableView.SelectItems)
        self._table.setSelectionMode(RegistryTableView.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.AscendingOrder)

        # Row numbers
        vh = self._table.verticalHeader()
        vh.setDefaultSectionSize(24)
        vh.setSectionsClickable(True)
        vh.setDefaultAlignment(Qt.AlignCenter)
        vh.setStyleSheet("""
            QHeaderView::section {
                background-color: #252525;
                color: #666666;
                border: 1px solid #3a3a3a;
                padding: 0 6px;
                font-size: 11px;
            }
            QHeaderView::section:checked {
                background-color: #3a5070;
                color: #d0d0d0;
            }
        """)

        # Column sizing
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Interactive)
        h.setSectionResizeMode(1, QHeaderView.Interactive)
        self._table.setColumnWidth(0, 300)
        self._table.setColumnWidth(1, 250)
        for i in range(2, self._model.columnCount()):
            h.setSectionResizeMode(i, QHeaderView.Interactive)
            self._table.setColumnWidth(i, 120)
        h.setStretchLastSection(True)

        # Assign cell delegates based on tag type
        self._delegates = []
        for i in range(len(self._model._fixed_columns), self._model.columnCount()):
            tag_type = self._model.tag_type_for_column(i)
            if tag_type == "bool":
                delegate = BoolCellDelegate(self._table)
                self._table.setItemDelegateForColumn(i, delegate)
                self._delegates.append(delegate)
            elif tag_type == "enum":
                delegate = EnumCellDelegate(self._table)
                self._table.setItemDelegateForColumn(i, delegate)
                self._delegates.append(delegate)
            elif tag_type == "number":
                delegate = NumCellDelegate(self._table)
                self._table.setItemDelegateForColumn(i, delegate)
                self._delegates.append(delegate)
            elif tag_type == "string":
                delegate = StrCellDelegate(self._table)
                self._table.setItemDelegateForColumn(i, delegate)
                self._delegates.append(delegate)

        # Edit mode toggle buttons
        edit_bar = QHBoxLayout()
        edit_bar.addWidget(QLabel("Edit:"))
        self._edit_buttons = {}
        for i in range(len(self._model._fixed_columns), self._model.columnCount()):
            tag_name = self._model._tag_columns[i - len(self._model._fixed_columns)]
            btn = QPushButton(tag_name)
            btn.setCheckable(True)
            btn.setFixedHeight(24)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #2d2d2d;
                    color: #888888;
                    border: 1px solid #3a3a3a;
                    padding: 2px 10px;
                    font-size: 12px;
                }
                QPushButton:checked {
                    background-color: #3a5070;
                    color: #d0d0d0;
                    border: 1px solid #5080b0;
                }
            """)
            col = i
            btn.toggled.connect(lambda checked, c=col: self._toggle_edit_mode(c, checked))
            edit_bar.addWidget(btn)
            self._edit_buttons[i] = btn
        edit_bar.addStretch()
        layout.addLayout(edit_bar)

        # Undo / Redo
        undo_shortcut = QShortcut(QKeySequence.Undo, self)
        undo_shortcut.activated.connect(self._model.undo)
        redo_shortcut = QShortcut(QKeySequence.Redo, self)
        redo_shortcut.activated.connect(self._model.redo)

        layout.addWidget(self._table)

    def _toggle_edit_mode(self, col: int, enabled: bool):
        self._model.set_editing(col, enabled)
        self._table.viewport().update()

    def _seed_test_tags(self):
        self._tags.define("tier", "enum", ["early", "mid", "late", "oh my jesus christ wow"])
        self._tags.define("banned", "bool", default=False)
        self._tags.define("tooltip", "string")
        self._tags.define("weight", "number")

    def closeEvent(self, event):
        self._db.close()
        super().closeEvent(event)
