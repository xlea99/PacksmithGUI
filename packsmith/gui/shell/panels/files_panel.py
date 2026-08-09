"""The Files panel (design 6.2) — Honest Mode.

§6.2 specifies two modes. **Honest Mode** is "a raw directory tree rooted at the Minecraft
instance directory. No abstractions, no reordering, just the filesystem" — buildable today.
**Smart Mode** organizes files by purpose, but its categories are contributed by
*integrations* (§7.1 Global Pack Loader, §7.2 KubeJS), and none exist yet; §6.2 already
says categories without an integration simply don't appear. So Honest Mode is the whole
panel for now, and Smart Mode arrives with the integrations that give it content.

What this panel is really for is **§6.2's Tracked vs Untracked rendering**: owned files
render normally, untouched ones render muted. That visibility is a precondition for
enforcing file ownership at all — hard-blocking writes to a file whose ownership the user
cannot see would be hostile.

The tree populates lazily; a modpack instance has thousands of files and `mods/` alone can
hold hundreds of jars.
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QWidget, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QAbstractItemView, QComboBox,
)

from packsmith.gui.shell import style
from packsmith.gui.shell.panels.base import Panel

_DOT_CACHE = {}


def _owner_dot(colour: str) -> QIcon:
    """A small filled circle in the ownership colour — the file-browser counterpart of the
    registry table's per-cell ownership bar."""
    if colour not in _DOT_CACHE:
        pixmap = QPixmap(8, 8)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(0, 0, 7, 7)
        painter.end()
        _DOT_CACHE[colour] = QIcon(pixmap)
    return _DOT_CACHE[colour]

_ROLE_PATH = Qt.UserRole        # relative path (posix-style)
_ROLE_IS_DIR = Qt.UserRole + 1
_ROLE_LOADED = Qt.UserRole + 2


def _norm(path: str) -> str:
    """Ownership records are keyed by whatever string the writer used; compare on a
    single normalized form so `config/x.json5` and `config\\x.json5` are one file."""
    return str(path).replace("\\", "/")


class FilesPanel(Panel):

    file_activated = Signal(str)        # relative path (double-clicked)
    ownership_changed = Signal(str)     # relative path

    def __init__(self, file_store, parent=None):
        super().__init__("Files", parent)
        self._files = file_store
        self._owners = {}

        mode_row = QWidget()
        mode_lay = QHBoxLayout(mode_row)
        mode_lay.setContentsMargins(6, 4, 6, 2)
        mode = QComboBox()
        mode.addItem("Honest — raw filesystem")
        mode.addItem("Smart — by purpose (needs integrations)")
        mode.model().item(1).setEnabled(False)      # §6.2: no integrations installed yet
        mode.setToolTip("Smart Mode's categories come from integrations (§7); none are "
                        "installed in this profile yet.")
        mode.setStyleSheet(f"font-size: 11px; color: {style.TEXT_MUTED};")
        mode_lay.addWidget(mode)
        self.body().addWidget(mode_row)

        legend = QLabel(
            f"<span style='color:{style.OWNER_USER}'>●</span> yours &nbsp;"
            f"<span style='color:{style.OWNER_ACTION}'>●</span> action &nbsp;"
            f"<span style='color:{style.TEXT_FAINT}'>untouched</span>")
        legend.setTextFormat(Qt.RichText)
        legend.setStyleSheet(f"font-size: 10px; padding: 0 8px 4px 8px;")
        self.body().addWidget(legend)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.itemExpanded.connect(self._on_expanded)
        self._tree.itemDoubleClicked.connect(self._on_double_clicked)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self.body().addWidget(self._tree)

        self.refresh()

    # --- population ---------------------------------------------------------

    def refresh(self):
        """Reload ownership and rebuild the visible tree, preserving what was expanded."""
        self._owners = {_norm(p): o for p, o in self._files.all_ownership().items()}
        expanded = self._expanded_paths()
        self._tree.clear()
        self._populate(None, self._files.root, "")
        self._restore_expanded(expanded)

    def _expanded_paths(self) -> set:
        found = set()

        def walk(item):
            for i in range(item.childCount()):
                child = item.child(i)
                if child.isExpanded():
                    found.add(child.data(0, _ROLE_PATH))
                    walk(child)
        root = self._tree.invisibleRootItem()
        walk(root)
        return found

    def _restore_expanded(self, paths):
        def walk(item):
            for i in range(item.childCount()):
                child = item.child(i)
                if child.data(0, _ROLE_PATH) in paths:
                    child.setExpanded(True)     # triggers lazy load
                    walk(child)
        walk(self._tree.invisibleRootItem())

    def _populate(self, parent_item, directory: Path, rel_prefix: str):
        try:
            entries = sorted(directory.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
            item = QTreeWidgetItem([entry.name])
            item.setData(0, _ROLE_PATH, rel)
            item.setData(0, _ROLE_IS_DIR, entry.is_dir())
            if entry.is_dir():
                item.setData(0, _ROLE_LOADED, False)
                item.addChild(QTreeWidgetItem(["…"]))   # placeholder so it shows an arrow
            else:
                self._style_file(item, rel)
            if parent_item is None:
                self._tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)

    def _style_file(self, item, rel):
        """§6.2 Tracked vs Untracked: owned files render normally with an ownership dot,
        untouched ones render muted. Not hidden — "hiding them would make the browser
        useless" — just visibly not-yet-tracked."""
        ownership = self._owners.get(_norm(rel))
        if ownership is None:
            item.setForeground(0, QColor(style.TEXT_FAINT))
            item.setToolTip(0, f"{rel}\nuntouched — no ownership record")
            return
        kind = ownership.get("kind")
        item.setIcon(0, _owner_dot(style.OWNER_USER if kind == "user" else style.OWNER_ACTION))
        item.setForeground(0, QColor(style.TEXT))
        who = "you" if kind == "user" else (ownership.get("action_ref") or "an action")
        item.setToolTip(0, f"{rel}\nowned by {who}")

    def _on_expanded(self, item):
        if item.data(0, _ROLE_IS_DIR) and not item.data(0, _ROLE_LOADED):
            item.takeChildren()                       # drop the placeholder
            item.setData(0, _ROLE_LOADED, True)
            rel = item.data(0, _ROLE_PATH)
            self._populate(item, self._files.root / rel, rel)

    def _on_double_clicked(self, item, _column=0):
        if not item.data(0, _ROLE_IS_DIR):
            self.file_activated.emit(item.data(0, _ROLE_PATH))

    # --- ownership actions (design 6.1) -------------------------------------

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None or item.data(0, _ROLE_IS_DIR):
            return
        rel = item.data(0, _ROLE_PATH)
        ownership = self._owners.get(_norm(rel))
        kind = ownership.get("kind") if ownership else None

        menu = QMenu(self)
        if kind is None:
            menu.addAction("Claim ownership", lambda: self._claim(rel))
        elif kind == "action":
            menu.addAction("Take ownership from the action…", lambda: self._take(rel, ownership))
            menu.addAction("Release ownership", lambda: self._release(rel))
        else:
            menu.addAction("Release ownership", lambda: self._release(rel))
        menu.addSeparator()
        menu.addAction("Reveal in Explorer", lambda: self._reveal(rel))
        menu.exec(self._tree.mapToGlobal(pos))

    def _claim(self, rel):
        self._files.claim(rel, owner="user")
        self._after_change(rel, f"You now own {rel} — actions are blocked from writing it.")

    def _take(self, rel, ownership):
        """§6.1: taking a file from an action is loud, and blocks that action until
        released — the file counterpart of the L2 transfer confirmation."""
        who = ownership.get("action_ref") or "an action"
        if QMessageBox.question(
                self, "Take ownership",
                f"{rel}\n\nThis file is managed by '{who}'. Taking ownership will block "
                f"that action from writing it until you release it again.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._files.claim(rel, owner="user")
        self._after_change(rel, f"You took ownership of {rel} from {who}.")

    def _release(self, rel):
        self._files.release(rel)
        self._after_change(rel, f"Released {rel} — it's untouched again; anyone may claim it.")

    def _after_change(self, rel, message):
        self.refresh()
        self.ownership_changed.emit(message)

    def _reveal(self, rel):
        target = (self._files.root / rel).parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
