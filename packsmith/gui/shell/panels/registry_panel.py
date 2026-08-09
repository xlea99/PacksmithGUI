"""The Registry panel (design 4.1) — the bootstrap.

"Clicking a category opens a zero-tag registry-table View scoped to that type —
essentially the fastest way to get a 'just show me everything' table without configuring
a real View."

This is what makes a fresh profile usable: you don't need a saved View or a single tag to
start looking at your pack.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QAbstractItemView

from packsmith.gui.shell import style
from packsmith.gui.shell.panels.base import Panel


class RegistryPanel(Panel):

    registry_activated = Signal(str)   # registry type

    def __init__(self, packdump, parent=None):
        super().__init__("Registry", parent)
        self._packdump = packdump

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.itemActivated.connect(self._on_activated)
        self._tree.itemClicked.connect(self._on_activated)
        self.body().addWidget(self._tree)

        self.refresh()

    def refresh(self):
        self._tree.clear()
        registries = self._packdump.registry or {}
        for reg_type in sorted(registries.keys()):
            count = len(registries[reg_type].get("values", []))
            item = QTreeWidgetItem([reg_type, f"{count:,}"])
            item.setData(0, Qt.UserRole, reg_type)
            item.setForeground(1, Qt.gray)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            item.setToolTip(0, f"{reg_type} — {count:,} entries")
            self._tree.addTopLevelItem(item)
        self._tree.resizeColumnToContents(0)

    def _on_activated(self, item, _column=0):
        reg_type = item.data(0, Qt.UserRole)
        if reg_type:
            self.registry_activated.emit(reg_type)
