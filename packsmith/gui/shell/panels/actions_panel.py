"""The Actions panel (design 4.1 / 3.3.1) — installed actions, by package.

"Reference/browse panel for all installed actions, organized by package. Inspect
manifests, read documentation… Actions are not pinnable — they are not the primary
runnable surface." (Jobs are, §3.3.2.)

This is also where a package's **source files** surface — deliberately *not* the Files
panel. That panel is the semantic browser over the game instance, and a `.star` file the
user wrote is not a game file in any meaningful sense; it's PackSmith's own userdata. So
the two never mix, and each keeps the rules that suit it: instance files answer to
ownership, package sources to provenance (§3.3.1 — you may edit what you authored, not
what you downloaded).
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QAbstractItemView, QMenu

from packsmith.gui.shell import style
from packsmith.gui.shell.panels.base import Panel

_ROLE_DOC = Qt.UserRole        # "<package>/<file>" for an openable source file
_ROLE_REF = Qt.UserRole + 1    # action ref, for action rows


class ActionsPanel(Panel):

    document_activated = Signal(str)     # "<package>/<file>" within the packages dir

    def __init__(self, package_index, parent=None):
        super().__init__("Actions", parent)
        self._packages = package_index

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.itemDoubleClicked.connect(self._on_activated)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self.body().addWidget(self._tree)

        self.refresh()

    def refresh(self):
        self._tree.clear()
        packages = self._packages.packages
        if not packages:
            empty = QTreeWidgetItem(["No packages installed", ""])
            empty.setForeground(0, Qt.gray)
            self._tree.addTopLevelItem(empty)
            return

        for name, package in sorted(packages.items()):
            downloaded = package.provenance == "downloaded"
            parent = QTreeWidgetItem([name, "downloaded" if downloaded else "authored"])
            parent.setForeground(0, Qt.gray)
            parent.setForeground(1, Qt.darkGray)
            parent.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            parent.setToolTip(0, package.description or name)
            manifest_path = f"{name}/manifest.toml"
            parent.setData(0, _ROLE_DOC, manifest_path)
            self._tree.addTopLevelItem(parent)
            parent.setExpanded(True)

            for action in sorted(package.actions, key=lambda a: a.action_id):
                child = QTreeWidgetItem([action.name or action.action_id, action.file])
                child.setForeground(1, Qt.darkGray)
                child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                child.setData(0, _ROLE_DOC, f"{name}/{action.file}")
                child.setData(0, _ROLE_REF, action.ref)
                tip = f"{action.ref}"
                if action.description:
                    tip += f"\n{action.description}"
                tip += "\n\ndouble-click to open its source"
                child.setToolTip(0, tip)
                parent.addChild(child)
        self._tree.resizeColumnToContents(0)

    def _on_activated(self, item, _column=0):
        document = item.data(0, _ROLE_DOC)
        if document:
            self.document_activated.emit(document)

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        document = item.data(0, _ROLE_DOC)
        if not document:
            return
        menu = QMenu(self)
        if item.data(0, _ROLE_REF):
            menu.addAction("Open source", lambda: self.document_activated.emit(document))
            package = document.split("/", 1)[0]
            menu.addAction("Open package manifest",
                           lambda: self.document_activated.emit(f"{package}/manifest.toml"))
        else:
            menu.addAction("Open manifest", lambda: self.document_activated.emit(document))
        menu.exec(self._tree.mapToGlobal(pos))
