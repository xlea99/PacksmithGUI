"""The JAR Viewer tab — design 6.5, step one: browse.

§6.5's mundane purpose, quoted: "peeking into a mod's internals currently requires an
external tool that extracts files to a scratch directory just to view them. The JAR Viewer
lets the user do this in PackSmith without leaving the workspace."

**This is the browsing half only.** Viewing a member's *content* (routing text to the Text
Editor, binary to something honest) and the save-as-override workflow are later steps. What
is here is the tree, the sizes, and a filter — enough to answer "what is actually in this
mod", which on a 300-mod pack is a question you ask constantly.

Nothing is extracted; see `core/archives`.
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTreeWidgetItem,
    QAbstractItemView, QHeaderView,
)

from packsmith.core.archives import ArchiveError, read_entries, summarise
from packsmith.gui.shell import style
from packsmith.gui.shell.tree import PanelTree

_ROLE_PATH = Qt.UserRole


def human_size(count: int) -> str:
    """Bytes as something readable. Jars run from a few KB to 144 MB, so a raw byte count
    is the one format that is wrong at both ends."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" or size >= 100 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class JarViewerTab(QWidget):
    """A read-only tree of an archive's contents."""

    status = Signal(str)

    def __init__(self, archive_path, parent=None):
        super().__init__(parent)
        self.path = str(archive_path)
        self._entries = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 6, 8, 6)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter by path…")
        self._filter.setStyleSheet(f"""
            QLineEdit {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                border: 1px solid {style.BORDER}; padding: 3px 6px; font-size: 11px;
            }}
        """)
        self._filter.textChanged.connect(self._repopulate)
        bar_lay.addWidget(self._filter, 1)
        self._summary = QLabel()
        self._summary.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        bar_lay.addWidget(self._summary)
        root.addWidget(bar)

        self._tree = PanelTree()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Name", "Size", "Packed"])
        self._tree.setHeaderHidden(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        root.addWidget(self._tree)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setContentsMargins(10, 6, 10, 6)
        self._banner.hide()
        root.addWidget(self._banner)

        self.reload()

    # --- loading -----------------------------------------------------------

    def reload(self):
        try:
            self._entries = read_entries(self.path)
        except ArchiveError as e:
            # A jar a launcher half-downloaded is a normal thing to double-click. The tab
            # opens and says so rather than refusing to exist.
            self._entries = []
            self._banner.setText(str(e))
            self._banner.setStyleSheet(
                f"background: {style.BG_CHROME}; color: {style.ERROR}; font-size: 11px;")
            self._banner.show()
            self._summary.setText("unreadable")
            return
        self._banner.hide()
        counts = summarise(self._entries)
        self._summary.setText(
            f"{counts['files']:,} files · {human_size(counts['size'])} "
            f"({human_size(counts['compressed'])} packed)")
        self._repopulate()

    def _repopulate(self):
        """Rebuild the tree, honouring the filter.

        Built eagerly rather than lazily: the whole directory is already in memory, and the
        worst jar in a 300-mod pack is ~14k entries, which Qt handles without complaint.
        Laziness would buy nothing and cost the filter its simplicity.
        """
        needle = self._filter.text().strip().lower()
        self._tree.clear()
        nodes = {"": self._tree.invisibleRootItem()}

        visible = [e for e in self._entries
                   if not needle or needle in e.path.lower()]
        # A filter hides folders whose name doesn't match but whose children's do, so
        # ancestors are re-added: a match you cannot see the path to is not a result.
        if needle:
            wanted = set()
            for entry in visible:
                parts = entry.path.rstrip("/").split("/")
                for i in range(1, len(parts)):
                    wanted.add("/".join(parts[:i]) + "/")
            by_path = {e.path: e for e in self._entries}
            visible = sorted(
                {e.path: e for e in visible} | {p: by_path[p] for p in wanted if p in by_path},
                key=lambda p: p)
            visible = [by_path[p] for p in visible]

        for entry in visible:
            key = entry.path.rstrip("/")
            parent_key = key.rsplit("/", 1)[0] if "/" in key else ""
            parent = nodes.get(parent_key, self._tree.invisibleRootItem())
            item = QTreeWidgetItem([
                entry.name,
                "" if entry.is_dir else human_size(entry.size),
                "" if entry.is_dir else human_size(entry.compressed),
            ])
            item.setData(0, _ROLE_PATH, entry.path)
            if entry.is_dir:
                item.setForeground(0, style.qt_colour(style.TEXT))
            elif entry.is_nested_archive:
                # Marked, not opened. Recursing into a jar-in-a-jar is its own step, and
                # a marker is what makes that discoverable when it arrives.
                item.setForeground(0, style.qt_colour(style.ACCENT_EDGE))
                item.setToolTip(0, f"{entry.path} — a nested archive")
            else:
                item.setForeground(0, style.qt_colour(style.TEXT_MUTED))
            parent.addChild(item)
            if entry.is_dir:
                nodes[key] = item

        if needle:
            self._tree.expandAll()
            self.status.emit(f"{sum(1 for e in visible if not e.is_dir):,} matching files")

    # --- for the window ----------------------------------------------------

    def selected_path(self):
        item = self._tree.currentItem()
        return item.data(0, _ROLE_PATH) if item is not None else None

    def title(self) -> str:
        return Path(self.path).name
