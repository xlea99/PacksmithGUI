"""The packdump diff, as a first-class tab — design 3.1 / 4.1.

§4.1 puts packdump review in the bottom panel, and most of it belongs there: what changed,
how much, what's at risk. But the bottom panel is a strip a few rows tall, and a real
update to a 300-mod pack moves *thousands* of entries — `alexscaves` alone can add two
hundred. A summary belongs in the strip; the diff itself needs somewhere you can actually
read it, filter it, and scroll it.

So the strip is the summary and the launcher, and this is where you go to look. The two
show the same `DiffSummary`; neither recomputes anything.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTreeWidgetItem,
    QAbstractItemView, QHeaderView, QPushButton,
)

from packsmith.gui.shell import style
from packsmith.gui.shell.tree import PanelTree

_ROLE_ENTRY = Qt.UserRole


class PackdumpDiffTab(QWidget):
    """Everything one packdump comparison contains, grouped and filterable."""

    status = Signal(str)

    def __init__(self, summary, *, title="Packdump changes", at_risk=(), parent=None):
        super().__init__(parent)
        self._summary = summary
        self._at_risk = list(at_risk)
        self.title = title

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QLabel(self._headline())
        header.setWordWrap(True)
        header.setContentsMargins(10, 8, 10, 8)
        header.setStyleSheet(f"color: {style.TEXT}; font-size: 12px;")
        root.addWidget(header)

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 0, 8, 6)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter by id…")
        self._filter.setStyleSheet(f"""
            QLineEdit {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                border: 1px solid {style.BORDER}; padding: 3px 6px; font-size: 11px;
            }}
        """)
        self._filter.textChanged.connect(self._repopulate)
        bar_lay.addWidget(self._filter, 1)
        expand = QPushButton("Expand all")
        expand.setStyleSheet(f"""
            QPushButton {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER}; font-size: 11px; padding: 2px 8px;
            }}
            QPushButton:hover {{ color: {style.TEXT}; }}
        """)
        expand.clicked.connect(lambda: self._tree.expandAll())
        bar_lay.addWidget(expand)
        root.addWidget(bar)

        self._tree = PanelTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Change", "Detail"])
        self._tree.setHeaderHidden(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        root.addWidget(self._tree)

        self._repopulate()

    def _headline(self) -> str:
        summary = self._summary
        if summary.empty:
            return "This dump is identical to the one it replaced."
        line = summary.headline()
        if self._at_risk:
            line += (f"      ⚠ {len(self._at_risk)} tag assignment"
                     f"{'s' if len(self._at_risk) != 1 else ''} would be orphaned")
        return line

    def _repopulate(self):
        needle = self._filter.text().strip().lower()
        self._tree.clear()

        # Identity first: a loader or MC version change is not one row among thousands, it
        # is the thing that decides whether this dump is even the same pack (§3.1).
        for field_name, (was, now) in sorted(self._summary.identity.items()):
            self._add_group(f"{field_name.replace('_', ' ')} changed", f"{was} → {now}",
                            colour=style.WARNING)

        if self._at_risk:
            group = self._add_group(
                f"Tag assignments at risk ({len(self._at_risk):,})",
                "these entries no longer exist in the new dump", colour=style.ERROR)
            for orphan in self._at_risk:
                if needle and needle not in orphan.entry_id.lower():
                    continue
                self._add_child(group, orphan.entry_id,
                                f"{orphan.tag_name} = {orphan.value}", style.ERROR)

        for name, members in (("Registries added", self._summary.registries_added),
                              ("Registries removed", self._summary.registries_removed)):
            if members:
                group = self._add_group(f"{name} ({len(members)})", "")
                for member in members:
                    self._add_child(group, member, "")

        if self._summary.mods:
            group = self._add_group(f"Mods ({len(self._summary.mods)})", "")
            for change in self._summary.mods:
                if needle and needle not in change.mod_id.lower():
                    continue
                colour = {"added": style.OWNER_USER, "removed": style.ERROR}.get(
                    change.kind, style.TEXT_MUTED)
                self._add_child(group, change.mod_id, change.headline(), colour)

        for change in self._summary.registries:
            shown_added = [e for e in change.added if not needle or needle in e.lower()]
            shown_removed = [e for e in change.removed if not needle or needle in e.lower()]
            if needle and not (shown_added or shown_removed):
                continue
            group = self._add_group(change.registry_type, change.headline())
            for entry in shown_added:
                self._add_child(group, entry, "added", style.OWNER_USER)
            for entry in shown_removed:
                self._add_child(group, entry, "removed", style.ERROR)

        if self._summary.renamed:
            self._add_group(f"Display names changed ({self._summary.renamed:,})",
                            "the table shows names, so these change what you read")

        if needle:
            self._tree.expandAll()
            self.status.emit(f"{self._match_count():,} matching entries")

    def _match_count(self) -> int:
        return sum(self._tree.topLevelItem(i).childCount()
                   for i in range(self._tree.topLevelItemCount()))

    def _add_group(self, text, detail, colour=None):
        item = QTreeWidgetItem([text, detail])
        if colour:
            item.setForeground(0, style.qt_colour(colour))
        self._tree.addTopLevelItem(item)
        item.setExpanded(True)
        return item

    @staticmethod
    def _add_child(parent, text, detail, colour=None):
        child = QTreeWidgetItem([text, detail])
        child.setData(0, _ROLE_ENTRY, text)
        if colour:
            child.setForeground(1, style.qt_colour(colour))
        parent.addChild(child)
        return child
