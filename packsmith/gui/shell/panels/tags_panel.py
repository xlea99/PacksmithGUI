"""The Tags panel (design 4.1) — the user's vocabulary.

"All declared tags. Click to edit a definition. Quick-action to spawn a minimal view
(registry type + that one editable column)."

Tags are grouped by registry type because a definition is **scoped to one registry**
(design 3.2.1): `remove` on items is a different tag from `remove` on blocks, and the
panel should make that structural, not incidental.

Live today: listing, creating (＋ New Tag), renaming, deleting (right-click), and the
quick-action (double-click → a minimal view for that tag).

**Rename carries §3.2.1's full ceremony**, which is the interesting part: it states its
blast radius first, then rewrites saved Views silently and gates every bound job step until
the user relinks it. **Retype remains absent on purpose** — §3.2.1 forbids it rather than
deferring it, because a tag has no structure to preserve and changing its type invalidates
its values rather than reshaping them.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QPushButton, QHBoxLayout, QWidget, QAbstractItemView,
    QHeaderView, QMenu,
)

from packsmith.gui.shell import icons, style
from packsmith.gui.shell.tree import PanelTree
from packsmith.gui.shell.panels.base import Panel, SearchBox, note_row

_ROLE_REGISTRY = Qt.UserRole
_ROLE_TAG = Qt.UserRole + 1


class TagsPanel(Panel):

    tag_activated = Signal(str, str)          # registry type, tag name
    new_tag_requested = Signal()
    delete_tag_requested = Signal(str, str)   # registry type, tag name
    edit_values_requested = Signal(str, str)  # registry type, tag name (enum tags only)
    rename_tag_requested = Signal(str, str)   # registry type, tag name

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

        self._search = SearchBox("tags")
        self._search.textChanged.connect(self.refresh)
        self.body().addWidget(self._search)

        self._tree = PanelTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.itemDoubleClicked.connect(self._on_activated)
        self._tree.itemActivated.connect(self._on_activated)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        # The name column takes what is left; the type column takes what it needs. Sizing
        # the name column to its *contents* instead counts the indent and the icon as well,
        # so it grows past the panel and shoves the type off the right edge — the same
        # trap the Registry panel documents, and adding icons is what springs it.
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
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
        menu.addAction("Rename…", lambda: self.rename_tag_requested.emit(reg_type, tag_name))
        menu.addSeparator()
        # Retype stays absent: §3.2.1 forbids it outright ("a tag has no structure to
        # preserve"), and undefine-and-redefine is the honest expression of that.
        menu.addAction("Delete tag…",
                       lambda: self.delete_tag_requested.emit(reg_type, tag_name))
        menu.exec(self._tree.mapToGlobal(pos))

    def refresh(self, *_):
        needle = self._search.needle()
        self._tree.clear()
        for reg_type in self._tags.defined_registries():
            definitions = sorted(self._tags.definitions_for(reg_type).items())
            # A registry type is a real thing to search *for*, not just a heading — typing
            # `item` to see everything scoped to `minecraft:item` is the obvious gesture,
            # and it is the same question the panel's grouping already answers. So a
            # matching group keeps all its tags; otherwise the tags are matched one by one
            # and a group with nothing left drops out.
            if needle and needle not in reg_type.lower():
                definitions = [(n, d) for n, d in definitions if needle in n.lower()]
                if not definitions:
                    continue

            parent = QTreeWidgetItem([reg_type, ""])
            parent.setForeground(0, Qt.gray)
            # The group IS a registry type, so it wears the Registry panel's own mark
            # rather than a folder. A folder would say "these are filed together"; the
            # database says "these belong to `minecraft:item`", which is the load-bearing
            # fact (§3.2.1) — `remove` on items is a different tag from `remove` on blocks,
            # and this panel is where that stops being a footnote.
            parent.setIcon(0, icons.concept_icon("registry", colour=style.TEXT_MUTED))
            self._tree.addTopLevelItem(parent)
            parent.setExpanded(True)
            for name, defn in definitions:
                child = QTreeWidgetItem([name, defn["type"]])
                # One mark for every tag, not one per type. The second column already says
                # `bool` / `enum` / `number`, and five near-identical glyphs beside five
                # words that already say it would be noise pretending to be information.
                child.setIcon(0, icons.concept_icon("tags", colour=style.TEXT_MUTED))
                child.setData(0, _ROLE_REGISTRY, reg_type)
                child.setData(0, _ROLE_TAG, name)
                child.setForeground(1, Qt.darkGray)
                child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                child.setToolTip(0, f"{name} ({defn['type']}) on {reg_type} — "
                                    f"double-click to open a view for this tag")
                parent.addChild(child)

        # A tree that simply empties leaves you wondering whether you typed a filter or
        # broke something. Said only while filtering: an empty panel on a fresh profile is
        # a different statement, and this one would be the wrong one.
        if needle and not self._tree.topLevelItemCount():
            self._tree.addTopLevelItem(note_row("No tag matches that"))

    def _on_activated(self, item, _column=0):
        reg_type = item.data(0, _ROLE_REGISTRY)
        tag_name = item.data(0, _ROLE_TAG)
        if reg_type and tag_name:
            self.tag_activated.emit(reg_type, tag_name)
