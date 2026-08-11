"""The Blueprints panel (design 4.1 / 3.2.2).

"Browse blueprint schemas and instances as a tree. Click a schema to see all instances;
click an instance to open an editor tab."

Every row carries its **gap count**, because that is what a blueprint is *for*: empty slots
are missing content, and "granite is 4/5" is the answer to the question you opened the
panel to ask. An orphaned instance is marked instead — until the user resolves what a
destructive schema change meant, its gaps aren't a meaningful number.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QMenu, QPushButton, QTreeWidgetItem, QWidget,
)

from packsmith.core.blueprints import BlueprintError
from packsmith.gui.shell import style
from packsmith.gui.shell.panels.base import Panel
from packsmith.gui.shell.tree import PanelTree

_ROLE_BLUEPRINT = Qt.UserRole
_ROLE_INSTANCE = Qt.UserRole + 1


def _button(text):
    button = QPushButton(text)
    button.setFixedHeight(22)
    button.setCursor(Qt.PointingHandCursor)
    button.setStyleSheet(f"""
        QPushButton {{
            background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
            border: 1px solid {style.BORDER}; font-size: 11px; padding: 1px 8px;
        }}
        QPushButton:hover {{ color: {style.TEXT}; border-color: {style.ACCENT_EDGE}; }}
    """)
    return button


class BlueprintsPanel(Panel):

    # blueprint, instance-or-"": the tab is always the whole blueprint, but a click on
    # one instance says which row you meant, and dropping that is dropping the click.
    blueprint_activated = Signal(str, str)
    new_blueprint_requested = Signal()
    delete_blueprint_requested = Signal(str)
    rename_blueprint_requested = Signal(str)
    new_instance_requested = Signal(str)           # blueprint
    delete_instance_requested = Signal(str, str)   # blueprint, instance
    rename_instance_requested = Signal(str, str)

    def __init__(self, blueprint_store, parent=None):
        super().__init__("Blueprints", parent)
        self._store = blueprint_store

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(6, 4, 6, 4)
        new_btn = _button("＋  New Blueprint")
        new_btn.clicked.connect(self.new_blueprint_requested)
        bar_lay.addWidget(new_btn)
        bar_lay.addStretch()
        self.body().addWidget(bar)

        self._tree = PanelTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.itemDoubleClicked.connect(self._on_activated)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self.body().addWidget(self._tree)

        self.refresh()

    def refresh(self):
        self._tree.clear()
        try:
            names = self._store.names()
        except Exception:
            names = []
        if not names:
            empty = QTreeWidgetItem(["No blueprints yet", ""])
            empty.setForeground(0, Qt.gray)
            self._tree.addTopLevelItem(empty)
            return

        for name in names:
            slots = [s for s in self._store.value_slots(name)]
            instances = self._store.instances(name)
            orphaned = {o.instance for o in self._store.orphans(name)}

            root = QTreeWidgetItem(
                [name, f"{len(instances)} × {len(slots)}" if instances else f"{len(slots)} slots"])
            root.setForeground(1, Qt.darkGray)
            root.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            root.setData(0, _ROLE_BLUEPRINT, name)
            root.setToolTip(0, f"{len(slots)} slots, {len(instances)} instances\n\n"
                               f"double-click to open the schema")
            if orphaned:
                root.setForeground(0, style.qt_colour(style.ERROR))
            self._tree.addTopLevelItem(root)
            root.setExpanded(True)

            for instance in instances:
                if instance.name in orphaned:
                    note = "orphaned"
                else:
                    filled = len(slots) - len(self._store.gaps(name, instance.name))
                    note = f"{filled}/{len(slots)}"
                child = QTreeWidgetItem([instance.name, note])
                child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                child.setData(0, _ROLE_BLUEPRINT, name)
                child.setData(0, _ROLE_INSTANCE, instance.name)
                if instance.name in orphaned:
                    child.setForeground(1, style.qt_colour(style.ERROR))
                    child.setToolTip(0, "Orphaned by a schema change — resolve it in the "
                                        "Errors panel before actions can touch it.")
                elif note.startswith("0/"):
                    child.setForeground(1, Qt.darkGray)
                root.addChild(child)
        self._tree.resizeColumnToContents(0)

    def _selected(self):
        items = self._tree.selectedItems()
        if not items:
            return None, None
        return items[0].data(0, _ROLE_BLUEPRINT), items[0].data(0, _ROLE_INSTANCE)

    def _on_activated(self, item, _column=0):
        blueprint = item.data(0, _ROLE_BLUEPRINT)
        if blueprint:
            self.blueprint_activated.emit(blueprint, item.data(0, _ROLE_INSTANCE) or "")

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        blueprint = item.data(0, _ROLE_BLUEPRINT)
        if not blueprint:
            return
        instance = item.data(0, _ROLE_INSTANCE)

        menu = QMenu(self)
        menu.addAction("Open",
                       lambda: self.blueprint_activated.emit(blueprint, instance or ""))
        menu.addSeparator()
        if instance:
            menu.addAction("Rename instance…",
                           lambda: self.rename_instance_requested.emit(blueprint, instance))
            menu.addAction("Delete instance…",
                           lambda: self.delete_instance_requested.emit(blueprint, instance))
        else:
            menu.addAction("New instance…",
                           lambda: self.new_instance_requested.emit(blueprint))
            menu.addSeparator()
            menu.addAction("Rename blueprint…",
                           lambda: self.rename_blueprint_requested.emit(blueprint))
            menu.addAction("Delete blueprint…",
                           lambda: self.delete_blueprint_requested.emit(blueprint))
        menu.exec(self._tree.mapToGlobal(pos))
