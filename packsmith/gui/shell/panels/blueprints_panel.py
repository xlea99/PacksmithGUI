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
    QAbstractItemView, QHBoxLayout, QHeaderView, QMenu, QPushButton, QTreeWidgetItem,
    QWidget,
)

from packsmith.core.blueprints import BlueprintError
from packsmith.gui.shell import icons, style
from packsmith.gui.shell.panels.base import Panel, SearchBox, note_row
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

        self._search = SearchBox("blueprints")
        self._search.textChanged.connect(self.refresh)
        self.body().addWidget(self._search)

        self._tree = PanelTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.itemDoubleClicked.connect(self._on_activated)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        # The name column takes what is left; the gap count takes what it needs. Sizing to
        # contents counts the indent and the icon too, so a long blueprint name would push
        # `4/5` — the number the panel exists to show — off the right edge.
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.body().addWidget(self._tree)

        self.refresh()

    def refresh(self, *_):
        needle = self._search.needle()
        self._tree.clear()
        try:
            names = self._store.names()
        except Exception:
            names = []
        if not names:
            self._tree.addTopLevelItem(note_row("No blueprints yet"))
            return

        for name in names:
            slots = [s for s in self._store.value_slots(name)]
            instances = self._store.instances(name)
            orphaned = {o.instance for o in self._store.orphans(name)}

            # A matching schema keeps all its instances — you searched for the blueprint,
            # so you want the blueprint. Otherwise the instances are matched one by one,
            # which is how you find `granite` without remembering it lives in `StoneType`.
            shown = instances
            if needle and needle not in name.lower():
                shown = [i for i in instances if needle in i.name.lower()]
                if not shown:
                    continue

            root = QTreeWidgetItem(
                [name, f"{len(instances)} × {len(slots)}" if instances else f"{len(slots)} slots"])
            # `len(instances)`, not `len(shown)`: the count is a fact about the blueprint,
            # and a filter is a lens rather than an edit. "1 × 10" while you have `granite`
            # typed would be a lie about the schema that outlives your search term.
            root.setForeground(1, Qt.darkGray)
            root.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            root.setData(0, _ROLE_BLUEPRINT, name)
            root.setToolTip(0, f"{len(slots)} slots, {len(instances)} instances\n\n"
                               f"double-click to open the schema")
            # The icon follows the row's own colour rather than staying neutral: a
            # blueprint with orphaned instances is one broken thing, and half a red row
            # reads as a rendering accident rather than as a state.
            root.setIcon(0, icons.concept_icon(
                "blueprints",
                colour=style.ERROR if orphaned else style.TEXT_MUTED))
            if orphaned:
                root.setForeground(0, style.qt_colour(style.ERROR))
            self._tree.addTopLevelItem(root)
            root.setExpanded(True)

            for instance in shown:
                if instance.name in orphaned:
                    note = "orphaned"
                else:
                    filled = len(slots) - len(self._store.gaps(name, instance.name))
                    note = f"{filled}/{len(slots)}"
                child = QTreeWidgetItem([instance.name, note])
                child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                child.setIcon(0, icons.ui_icon(
                    "instance",
                    colour=style.ERROR if instance.name in orphaned
                    else style.TEXT_MUTED))
                child.setData(0, _ROLE_BLUEPRINT, name)
                child.setData(0, _ROLE_INSTANCE, instance.name)
                if instance.name in orphaned:
                    child.setForeground(1, style.qt_colour(style.ERROR))
                    child.setToolTip(0, "Orphaned by a schema change — resolve it in the "
                                        "Errors panel before actions can touch it.")
                elif note.startswith("0/"):
                    child.setForeground(1, Qt.darkGray)
                root.addChild(child)

        # Only while filtering. "No blueprints yet" above answers a different question, and
        # showing it here would tell you your profile is empty when it is your search that
        # is.
        if needle and not self._tree.topLevelItemCount():
            self._tree.addTopLevelItem(note_row("No blueprint or instance matches that"))

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
