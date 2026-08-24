"""The Folders panel (design 6.6) — the places Packsmith may look at all.

Everything else in §6 assumed one place: the instance. That held while Packsmith's job
ended at the instance, and stops the moment the pack's content is *generated* — a glue
mod's assets are authored in a Gradle project that will never live inside one.

**Adding a folder here is not granting an action access to it.** §6.2 asked that action
access be a second, explicit, per-root opt-in, and that is what a binding is. Adding a root
lets a *person* browse and edit it; binding it to a step lets *one action* write into it.
The panel says so out loud, because "I registered a folder" and "I let downloaded code
write to my mod's source tree" are consents a user should never confuse.

`minecraft` is always here, always first, and cannot be renamed or removed — a profile
without its instance is not a profile. It is rendered muted rather than hidden, because a
list of "everywhere Packsmith looks" that omits the main one would be a lie of omission.
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMenu,
    QMessageBox, QPushButton, QTreeWidgetItem, QWidget,
)

from packsmith.core.roots import INSTANCE_ROOT, RootError
from packsmith.gui.shell import icons, style
from packsmith.gui.shell.panels.base import Panel
from packsmith.gui.shell.tree import PanelTree

_ROLE_NAME = Qt.UserRole


class FoldersPanel(Panel):
    """Tracked roots: what they are called, where they point, and nothing else."""

    roots_changed = Signal()            # something was added, renamed or removed
    root_activated = Signal(str)        # double-clicked — show it in the file browser

    def __init__(self, roots, parent=None):
        super().__init__("FOLDERS", parent)
        self._roots = roots

        note = QLabel(
            "Tracked folders are browsable here. An action can only write to one you "
            "<b>bind to its step</b>.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)
        note.setStyleSheet(
            f"color: {style.TEXT_FAINT}; font-size: 10px; padding: 0 8px 4px 8px;")
        self.body().addWidget(note)

        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 2, 6, 4)
        add = QPushButton("＋  Track a folder")
        add.setStyleSheet(f"font-size: 11px; color: {style.TEXT_MUTED};")
        add.clicked.connect(self.add_folder)
        row.addWidget(add)
        row.addStretch()
        self.body().addWidget(bar)

        self._tree = PanelTree()
        self._tree.setHeaderHidden(True)
        self._tree.setColumnCount(2)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemDoubleClicked.connect(
            lambda item, _c=0: self.root_activated.emit(item.data(0, _ROLE_NAME)))
        self.body().addWidget(self._tree)

        self.refresh()

    # --- rendering ----------------------------------------------------------

    def refresh(self, *_):
        self._tree.clear()
        for name, path in self._roots.paths().items():
            item = QTreeWidgetItem([name, ""])
            item.setData(0, _ROLE_NAME, name)
            item.setIcon(0, icons.ui_icon(
                "instance" if name == INSTANCE_ROOT else "folder",
                colour=style.TEXT_MUTED))
            # The path is what tells two similarly-named roots apart, and it is the thing
            # you check when a write went somewhere surprising.
            item.setToolTip(0, str(path))
            missing = not Path(path).is_dir()
            if name == INSTANCE_ROOT:
                item.setForeground(0, style.qt_colour(style.TEXT_MUTED))
                item.setText(1, "the instance")
            elif missing:
                # Loud (design 6.6): a moved repo or an unplugged drive must not read as an
                # empty folder that is fine to write into.
                item.setForeground(0, style.qt_colour(style.ERROR))
                item.setText(1, "missing")
                item.setToolTip(0, f"{path}\n\nThis folder is not there any more.")
            else:
                item.setText(1, _shorten(path))
            item.setForeground(1, style.qt_colour(style.TEXT_FAINT))
            self._tree.addTopLevelItem(item)
        self._tree.resizeColumnToContents(0)

    # --- acting -------------------------------------------------------------

    def add_folder(self):
        chosen = QFileDialog.getExistingDirectory(self, "Track a folder")
        if not chosen:
            return
        suggested = Path(chosen).name.lower().replace("-", "_").replace(" ", "_")
        name, ok = QInputDialog.getText(
            self, "Name this folder",
            "A short name actions will bind to.\n"
            "Lowercase letters, digits and underscores.",
            text="".join(c for c in suggested if c.isalnum() or c == "_"))
        if not ok:
            return
        self._guarded(lambda: self._roots.add(name.strip(), chosen))

    def rename_folder(self, name):
        new_name, ok = QInputDialog.getText(self, "Rename folder", "New name:", text=name)
        if not ok or not new_name.strip() or new_name.strip() == name:
            return
        # A name is a LABEL (design 3.2.1): rows key on the id, so this orphans nothing and
        # every step bound to it follows automatically.
        self._guarded(lambda: self._roots.rename(name, new_name.strip()))

    def remove_folder(self, name):
        if QMessageBox.question(
                self, "Stop tracking",
                f"Stop tracking '{name}'?\n\n"
                f"The folder and everything in it is left exactly as it is — this only "
                f"removes Packsmith's view of it, and forgets which files it had claimed. "
                f"Any job step bound to it will need re-binding.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._guarded(lambda: self._roots.remove(name))

    def _guarded(self, operation):
        """Run a change and surface a refusal. Every guard in `FileRoots` is a correctness
        requirement, so its message is the explanation — restating it here would be a second
        place for the reasoning to drift."""
        try:
            operation()
        except RootError as e:
            QMessageBox.warning(self, "Can't track that", str(e))
            return
        self.refresh()
        self.roots_changed.emit()

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        name = item.data(0, _ROLE_NAME)
        menu = QMenu(self)
        menu.addAction("Show in Files", lambda: self.root_activated.emit(name))
        if name != INSTANCE_ROOT:
            menu.addSeparator()
            menu.addAction("Rename…", lambda: self.rename_folder(name))
            menu.addAction("Stop tracking…", lambda: self.remove_folder(name))
        menu.exec(self._tree.viewport().mapToGlobal(pos))


def _shorten(path) -> str:
    """Enough of a path to recognise it, from the END — the last two segments say which
    repo this is, where the first twenty say which drive."""
    parts = Path(path).parts
    return "…/" + "/".join(parts[-2:]) if len(parts) > 2 else str(path)
