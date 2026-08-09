"""The Tags panel (design 4.1) — the user's vocabulary.

"All declared tags. Click to edit a definition. Quick-action to spawn a minimal view
(registry type + that one editable column)."

Tags are grouped by registry type because a definition is **scoped to one registry**
(design 3.2.1): `remove` on items is a different tag from `remove` on blocks, and the
panel should make that structural, not incidental.

Live today: listing, creating (＋ New Tag), deleting (right-click → Delete), and the
quick-action (double-click → a minimal view for that tag). **Rename and retype are absent
on purpose** — §3.2.1's Tag Schema Evolution makes rename a loud operation that must relink
bound job steps, and forbids retype outright; neither ceremony is built yet.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QPushButton, QHBoxLayout, QWidget, QAbstractItemView, QMenu,
)

from packsmith.gui.shell import style
from packsmith.gui.shell.tree import PanelTree
from packsmith.gui.shell.panels.base import Panel

_ROLE_REGISTRY = Qt.UserRole
_ROLE_TAG = Qt.UserRole + 1


class TagsPanel(Panel):

    tag_activated = Signal(str, str)          # registry type, tag name
    new_tag_requested = Signal()
    delete_tag_requested = Signal(str, str)   # registry type, tag name
    edit_values_requested = Signal(str, str)  # registry type, tag name (enum tags only)

    def __init__(self, tag_store, parent=None):
        super().__init__("Tags", parent)
        self._tags = tag_store

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(6, 4, 6, 4)
        new_btn = QPushButton("＋  New Tag")
        new_btn.setFixedHeight(22)
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER}; font-size: 11px; padding: 1px 8px;
            }}
            QPushButton:hover {{ color: {style.TEXT}; border-color: {style.ACCENT_EDGE}; }}
        """)
        new_btn.clicked.connect(self.new_tag_requested)
        bar_lay.addWidget(new_btn)
        bar_lay.addStretch()
        self.body().addWidget(bar)

        self._tree = PanelTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.itemDoubleClicked.connect(self._on_activated)
        self._tree.itemActivated.connect(self._on_activated)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self.body().addWidget(self._tree)

        self.refresh()

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        reg_type = item.data(0, _ROLE_REGISTRY)
        tag_name = item.data(0, _ROLE_TAG)
        if not reg_type or not tag_name:
            return
        menu = QMenu(self)
        menu.addAction("Open a view for this tag",
                       lambda: self.tag_activated.emit(reg_type, tag_name))
        definition = self._tags.definition(reg_type, tag_name)
        if definition and definition["type"] == "enum":
            menu.addAction("Edit values…",
                           lambda: self.edit_values_requested.emit(reg_type, tag_name))
        menu.addSeparator()
        # Rename/retype deliberately absent — see module docstring and §3.2.1.
        menu.addAction("Delete tag…",
                       lambda: self.delete_tag_requested.emit(reg_type, tag_name))
        menu.exec(self._tree.mapToGlobal(pos))

    def refresh(self):
        self._tree.clear()
        for reg_type in self._tags.defined_registries():
            parent = QTreeWidgetItem([reg_type, ""])
            parent.setForeground(0, Qt.gray)
            self._tree.addTopLevelItem(parent)
            parent.setExpanded(True)
            for name, defn in sorted(self._tags.definitions_for(reg_type).items()):
                child = QTreeWidgetItem([name, defn["type"]])
                child.setData(0, _ROLE_REGISTRY, reg_type)
                child.setData(0, _ROLE_TAG, name)
                child.setForeground(1, Qt.darkGray)
                child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                child.setToolTip(0, f"{name} ({defn['type']}) on {reg_type} — "
                                    f"double-click to open a view for this tag")
                parent.addChild(child)
        self._tree.resizeColumnToContents(0)

    def _on_activated(self, item, _column=0):
        reg_type = item.data(0, _ROLE_REGISTRY)
        tag_name = item.data(0, _ROLE_TAG)
        if reg_type and tag_name:
            self.tag_activated.emit(reg_type, tag_name)
