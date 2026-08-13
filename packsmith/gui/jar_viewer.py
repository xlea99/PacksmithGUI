"""The JAR Viewer tab — design 6.5, step one: browse.

§6.5's mundane purpose, quoted: "peeking into a mod's internals currently requires an
external tool that extracts files to a scratch directory just to view them. The JAR Viewer
lets the user do this in Packsmith without leaving the workspace."

**This is the browsing half only.** Viewing a member's *content* (routing text to the Text
Editor, binary to something honest) and the save-as-override workflow are later steps. What
is here is the tree, the sizes, and a filter — enough to answer "what is actually in this
mod", which on a 300-mod pack is a question you ask constantly.

Nothing is extracted; see `core/archives`.
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QMenu, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTreeWidgetItem,
    QAbstractItemView, QHeaderView,
)

from packsmith.core.archives import ArchiveError, read_entries, summarise
from packsmith.core.capabilities import override_kind
from packsmith.gui.editor.sources import is_overridable
from packsmith.gui.shell import icons, style
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
    member_activated = Signal(str, str)   # archive path (absolute), member path
    override_requested = Signal(str, str)  # archive path, member path

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
        self._tree.itemDoubleClicked.connect(self._on_double_clicked)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
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
            # Colour says what you can DO with it, the same way it does in the Files panel
            # (§6.2) — there it is ownership, here it is overridability (§6.5). Everything
            # used to be muted alike, which said nothing: the one fact that matters in a
            # jar is whether a file can be copied back out as an override, and a `.class`
            # never can.
            if entry.is_dir:
                item.setIcon(0, icons.file_icon(entry.name, True, colour=style.TEXT))
                item.setForeground(0, style.qt_colour(style.TEXT))
            elif entry.is_nested_archive:
                # Marked, not opened. Recursing into a jar-in-a-jar is its own step, and
                # a marker is what makes that discoverable when it arrives.
                item.setIcon(0, icons.file_icon(entry.name, colour=style.ACCENT_EDGE))
                item.setForeground(0, style.qt_colour(style.ACCENT_EDGE))
                item.setToolTip(0, f"{entry.path} — a nested archive")
            elif is_overridable(entry.path):
                item.setIcon(0, icons.file_icon(entry.name, colour=style.TEXT))
                item.setForeground(0, style.qt_colour(style.TEXT))
                item.setToolTip(0, f"{entry.path}\ncan be saved as an override")
            else:
                item.setIcon(0, icons.file_icon(entry.name, colour=style.TEXT_FAINT))
                item.setForeground(0, style.qt_colour(style.TEXT_MUTED))
                item.setToolTip(0, f"{entry.path}\nno override target — §6.3")
            parent.addChild(item)
            if entry.is_dir:
                nodes[key] = item

        if needle:
            self._tree.expandAll()
            self.status.emit(f"{sum(1 for e in visible if not e.is_dir):,} matching files")

    # --- for the window ----------------------------------------------------

    def _on_double_clicked(self, item, _column=0):
        path = item.data(0, _ROLE_PATH)
        if path and not path.endswith("/"):
            self.member_activated.emit(self.path, path)

    def _on_context_menu(self, pos):
        menu = self._menu_for(self._tree.itemAt(pos))
        if menu is not None:
            menu.exec(self._tree.mapToGlobal(pos))

    def _menu_for(self, item):
        """Save-as-override is offered only where it means something (§6.5).

        A file under `data/` or `assets/` can be shadowed by a datapack or resource pack.
        A `.class` file, `META-INF/`, or the mod's own `mods.toml` cannot — there is no
        override target for compiled code, and there never will be. Offering the action
        everywhere and failing afterwards would teach the user to distrust the menu.
        """
        path = item.data(0, _ROLE_PATH) if item is not None else None
        if not path or path.endswith("/"):
            return None
        menu = QMenu(self)
        kind = override_kind(path)
        if kind is not None:
            noun = "datapack" if kind == "datapacks" else "resource pack"
            menu.addAction(f"Save as override into a {noun}…",
                           lambda: self.override_requested.emit(self.path, path))
            menu.addSeparator()
        menu.addAction("Open", lambda: self.member_activated.emit(self.path, path))
        return menu

    def selected_path(self):
        item = self._tree.currentItem()
        return item.data(0, _ROLE_PATH) if item is not None else None

    def title(self) -> str:
        return Path(self.path).name
