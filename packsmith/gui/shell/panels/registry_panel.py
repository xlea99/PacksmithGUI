"""The Registry panel (design 4.1) — the bootstrap.

"Clicking a category opens a zero-tag registry-table View scoped to that type —
essentially the fastest way to get a 'just show me everything' table without configuring
a real View."

This is what makes a fresh profile usable: you don't need a saved View or a single tag to
start looking at your pack.

**Grouped by namespace**, because the flat list isn't small: a 300-mod pack registers 135
registry types, and every one of them is a row. The split is the dumbest one available —
the text before the colon — which is exactly right here, since that prefix *is* the mod
that owns the registry. No mapping table, nothing to maintain, and it degrades to one
group if a pack somehow has no namespaces at all.

**Pinning** puts the two or three registries you actually live in above the alphabet. A
fresh profile starts with `minecraft:item` and `minecraft:entity_type` pinned, because on
an empty profile those are what anybody opens first and an empty shortcut list teaches
nobody that the feature exists.
"""
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QAbstractItemView, QHeaderView, QMenu)

from packsmith.gui.shell import style
from packsmith.gui.shell.tree import PanelTree
from packsmith.gui.shell.panels.base import Panel

SEPARATOR_ROLE = Qt.UserRole + 1

# Blank height the flagged row carries so the rule has air around it rather than sitting
# against two lines of text. The rule is drawn *inside* that space, never on the boundary
# between the two rows — a line on the seam belongs to neither, so when a neighbour
# repaints (hovering it is enough) the boundary pixel is cleared and nothing redraws it
# until something dirties this row again. It came back only when the pointer left the
# area, which is the signature of that bug.
RULE_SPACE = 6
# How far into that space the rule sits. Not simply half of it: the row above contributes
# its own bottom padding to the gap, so an even split reads as sitting high. Measured to
# land the line within a pixel of centre between the two labels.
RULE_INSET = 4


class _RegistryTree(PanelTree):
    """Draws a rule near the top of any row flagged with ``SEPARATOR_ROLE``.

    Painted rather than faked with a blank item, because a spacer row is a real row: it
    takes a place in the tree, keyboard navigation lands on it, and every piece of code
    that walks the children has to learn to skip it.

    The flag goes on the row *below* the rule rather than the one above it. The pinned
    section changes height when it is expanded, so "under the last pinned thing" is a
    moving target — the first namespace, by contrast, always sits directly beneath it.
    """

    def drawRow(self, painter, option, index):
        super().drawRow(painter, option, index)
        if not index.data(SEPARATOR_ROLE):
            return
        painter.save()
        painter.setPen(QColor(style.BORDER))
        y = option.rect.top() + RULE_INSET
        painter.drawLine(0, y, self.viewport().width(), y)
        painter.restore()


class RegistryPanel(Panel):

    registry_activated = Signal(str)   # registry type
    pins_changed = Signal(list)        # the new pin list, for whoever persists it

    NO_NAMESPACE = "(no namespace)"
    PINNED_LABEL = "Pinned"
    DEFAULT_PINS = ("minecraft:item", "minecraft:entity_type")

    def __init__(self, packdump, parent=None, pins=None):
        super().__init__("Registry", parent)
        self._packdump = packdump
        # `None` means "this profile has never said", which is different from a profile
        # whose pins are deliberately empty — unpinning everything must not hand the
        # defaults straight back.
        self._pins = list(self.DEFAULT_PINS) if pins is None else [str(p) for p in pins]
        # Which groups are open, remembered across refreshes. Pinned starts open: a
        # shortcut list you have to expand to see is not a shortcut.
        self._opened = {self.PINNED_LABEL}

        self._tree = _RegistryTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        # A single click on a namespace opens it, the way a folder behaves everywhere else.
        # Qt's own double-click expansion is turned off so the two don't both fire and
        # cancel each other out.
        self._tree.setExpandsOnDoubleClick(False)
        self._tree.itemActivated.connect(self._on_activated)
        self._tree.itemClicked.connect(self._on_clicked)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        # The name column takes what is left, the count column takes what it needs. Sizing
        # the name column to its contents instead — which is what the flat list did — now
        # counts the indent as well, and pushed the numbers off the edge of the panel.
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.body().addWidget(self._tree)

        self.refresh()

    def set_packdump(self, packdump):
        """Adopt a newly imported dump (design 3.1). This panel is the entry counts, so it
        is the most visible place a stale registry would show."""
        self._packdump = packdump
        self.refresh()

    def refresh(self):
        # Which groups were open is remembered across a refresh: this runs on packdump
        # adopt, and collapsing everything you had open is a strange thing for an import
        # to do to you. Only re-read it from a tree that has rows — on the first build
        # there is nothing to read, and the constructor's default would be wiped.
        if self._tree.topLevelItemCount():
            self._opened = self._opened_namespaces()
        self._tree.clear()
        registries = self._packdump.registry or {}

        pinned = self._add_pinned(registries)

        groups = {}
        for reg_type in sorted(registries):
            namespace, _, short = reg_type.partition(":")
            if not short:
                namespace, short = self.NO_NAMESPACE, reg_type
            groups.setdefault(namespace, []).append((reg_type, short))

        for namespace in sorted(groups):
            rows = groups[namespace]
            total = sum(len(registries[t].get("values", [])) for t, _ in rows)
            parent = self._row(namespace, total)
            kinds = "registry" if len(rows) == 1 else "registries"
            parent.setToolTip(0,
                              f"{namespace} — {len(rows)} {kinds}, {total:,} entries")
            for reg_type, short in rows:
                count = len(registries[reg_type].get("values", []))
                # Labelled by the short name; the namespace is already the row above it,
                # and repeating it in every child is what made the flat list hard to scan.
                child = self._row(short, count)
                child.setData(0, Qt.UserRole, reg_type)
                child.setToolTip(0, f"{reg_type} — {count:,} entries")
                parent.addChild(child)
            if pinned:
                self._make_ruled(parent)
                pinned = False                            # only the first one gets it
            self._tree.addTopLevelItem(parent)
            parent.setExpanded(namespace in self._opened)

    def _add_pinned(self, registries) -> bool:
        """The shortcut section, above the alphabet. True if one was drawn.

        Pinned entries are shown *as well as* in their namespace rather than moved out of
        it — this is a Quick Access list, and a `minecraft` group that mysteriously has no
        `item` in it would be the more confusing of the two. They are labelled with the
        full id for the same reason: from up here the namespace is not implied.

        A pin whose registry the pack no longer has is skipped, not dropped. Mods come and
        go across packdump imports, and silently forgetting the pin would mean reinstalling
        a mod doesn't bring its shortcut back.
        """
        present = [t for t in self._pins if t in registries]
        if not present:
            return False
        total = sum(len(registries[t].get("values", [])) for t in present)
        section = self._row(self.PINNED_LABEL, total)
        for reg_type in present:
            child = self._row(reg_type, len(registries[reg_type].get("values", [])))
            child.setData(0, Qt.UserRole, reg_type)
            child.setToolTip(0, f"{reg_type} — pinned")
            section.addChild(child)
        self._tree.addTopLevelItem(section)
        section.setExpanded(self.PINNED_LABEL in self._opened)
        return True

    # --- pinning ---------------------------------------------------------------------------

    def pins(self) -> list:
        return list(self._pins)

    def pin(self, reg_type):
        if reg_type in self._pins:
            return
        self._pins.append(reg_type)      # newest last, so the list doesn't reshuffle
        self._pins_changed()

    def unpin(self, reg_type):
        if reg_type not in self._pins:
            return
        self._pins.remove(reg_type)
        self._pins_changed()

    def _pins_changed(self):
        self.refresh()
        self.pins_changed.emit(list(self._pins))

    def menu_for(self, reg_type):
        """The context menu for one row, or None where there is nothing to offer.

        Split from the handler so it can be exercised without a modal `exec` — the menu is
        where the whole feature lives, and a test that could only click through a native
        popup would end up testing nothing at all.
        """
        if not reg_type:
            return None                  # a namespace is a heading, not something to pin
        menu = QMenu(self._tree)
        if reg_type in self._pins:
            menu.addAction("Unpin", lambda: self.unpin(reg_type))
        else:
            menu.addAction("Pin to top", lambda: self.pin(reg_type))
        return menu

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        menu = self.menu_for(item.data(0, Qt.UserRole) if item is not None else None)
        if menu is not None:
            menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _make_ruled(self, item):
        """Mark the row that carries the rule, and give it room to carry it.

        The extra height is pinned to the **top** of the row by bottom-aligning the text.
        Left centred, Qt splits it evenly above and below — which puts only half of it
        where the rule goes, and spends the other half pushing this row away from the next
        one, so the first namespace ends up looking detached from the rest of the list.
        """
        item.setData(0, SEPARATOR_ROLE, True)
        item.setSizeHint(0, QSize(0, self._row_height() + RULE_SPACE))
        item.setTextAlignment(0, Qt.AlignLeft | Qt.AlignBottom)
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignBottom)

    def _row_height(self) -> int:
        """A normal row's height, asked of the tree rather than assumed.

        The pinned section is built before this is needed, so there is always a real row to
        measure; the fallback only covers a tree with nothing in it, where the number is
        never used anyway.
        """
        return self._tree.sizeHintForRow(0) or (self._tree.fontMetrics().height() + 6)

    @staticmethod
    def _row(label, count):
        item = QTreeWidgetItem([label, f"{count:,}"])
        item.setForeground(1, Qt.gray)
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        return item

    def _opened_namespaces(self) -> set:
        return {self._tree.topLevelItem(i).text(0)
                for i in range(self._tree.topLevelItemCount())
                if self._tree.topLevelItem(i).isExpanded()}

    def _on_clicked(self, item, _column=0):
        if item.data(0, Qt.UserRole) is None:
            item.setExpanded(not item.isExpanded())
        else:
            self._on_activated(item)

    def _on_activated(self, item, _column=0):
        reg_type = item.data(0, Qt.UserRole)
        if reg_type:
            self.registry_activated.emit(reg_type)
