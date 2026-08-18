"""The Packages panel (design 3.3.1) — one package, whole.

The Actions tab answers *"what can I run?"* — every declared action across every package,
and nothing else. This one answers *"what is in this package?"*, which is a different
question with a different shape: one package's **actions** and its **files**, side by side.
They shared a tree once and the tree was bad at both, because a list of entry points and a
source tree want opposite things from the space.

**One package at a time, chosen from a dropdown**, rather than every package expanded into
a forest. A package is the unit you work inside — you are editing `palette`, not browsing
the packages directory — and picking it once puts the whole panel at that scope, which is
what makes an unqualified `+` button and unqualified right-click menus unambiguous.

**Provenance decides what the panel offers** (§3.3.1: you may edit what you authored, not
what you installed). Authored packages sort above a separator in the dropdown and get the
`+` and the full context menus. A downloaded package still opens every file — reading
someone's action is how you learn to write one — but offers nothing that would write.

Files use the Files panel's own icon vocabulary (§6.2) rather than a second set: a `.json`
is a `.json` whether it is in the game instance or in a package, and shape-says-what-it-is
should not depend on which tree you found it in.
"""
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QMenu, QPushButton, QTreeWidgetItem,
    QWidget,
)

from packsmith.core.packages import MANIFEST_NAME, folders, source_files
from packsmith.gui.shell import icons, style
from packsmith.gui.shell.dropdown import DropDown
from packsmith.gui.shell.tree import PanelTree
from packsmith.gui.shell.panels.base import Panel, note_row

_ROLE_KIND = Qt.UserRole          # "actions" | "action" | "files" | "folder" | "file"
_ROLE_PATH = Qt.UserRole + 1      # package-relative path, for folder/file rows
_ROLE_REF = Qt.UserRole + 2       # action ref, for action rows

_ACTIONS_LABEL = "Actions"
_FILES_LABEL = "Files"


def _parent_of(path: str) -> str:
    """The containing folder of a package-relative path; "" for the package root."""
    return path.rsplit("/", 1)[0] if "/" in path else ""


class PackagesPanel(Panel):

    action_activated = Signal(str)                 # action ref
    document_activated = Signal(str)               # "<package>/<file>"
    new_package_requested = Signal()
    new_action_requested = Signal(str)             # package
    new_file_requested = Signal(str, str)          # package, destination folder
    new_folder_requested = Signal(str, str)        # package, parent folder
    rename_file_requested = Signal(str, str)       # package, file
    delete_file_requested = Signal(str, str)       # package, file
    rename_folder_requested = Signal(str, str)     # package, folder
    delete_folder_requested = Signal(str, str)     # package, folder

    def __init__(self, package_index, parent=None):
        super().__init__("Packages", parent)
        self._packages = package_index
        self._selected = None

        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(4)

        # This control is where the shared dropdown came from — it is now the same widget
        # every simple dropdown in the app uses (`shell/dropdown.py`).
        self._picker = DropDown()
        self._picker.currentIndexChanged.connect(self._on_picked)
        row.addWidget(self._picker, 1)

        self._add = QPushButton()
        icons.mark(self._add, "add", size=12)
        self._add.setFixedSize(22, 22)
        self._add.setCursor(Qt.PointingHandCursor)
        self._add.setToolTip("Add to this package")
        self._add.setStyleSheet(f"""
            QPushButton {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER};
            }}
            QPushButton:hover {{ color: {style.TEXT};
                                 border-color: {style.ACCENT_EDGE}; }}
        """)
        self._add.clicked.connect(self._show_add_menu)
        row.addWidget(self._add)
        self.body().addWidget(bar)

        self._tree = PanelTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.itemDoubleClicked.connect(self._on_activated)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        icons.follow_expansion(self._tree)

        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.body().addWidget(self._tree)

        self.reload()

    # --- the package picker -------------------------------------------------

    def reload(self):
        """Re-read the package list, keeping the selection if it survived."""
        wanted = self._selected
        self._picker.blockSignals(True)
        self._picker.clear()

        packages = self._packages.packages
        authored = sorted(n for n, p in packages.items() if p.provenance == "authored")
        installed = sorted(n for n, p in packages.items() if p.provenance != "authored")
        for name in authored:
            self._picker.addItem(name, name)
        # One list with a rule through it, not two dropdowns: they are the same kind of
        # thing and you pick one of them, so splitting the control would be modelling
        # provenance as a mode. The line says which half you are in, which is all the
        # separation the difference needs.
        if authored and installed:
            self._picker.insertSeparator(self._picker.count())
        for name in installed:
            self._picker.addItem(f"{name}  (installed)", name)
        self._picker.blockSignals(False)

        # `if wanted` is load-bearing. A separator is a real row carrying **None** as its
        # data, and so is "nothing selected yet" — so `findData(None)` on first load finds
        # the separator, selects it, and the panel reports no package while holding three.
        # It only bites once both halves exist, which means a profile with nothing
        # installed never shows it.
        index = self._picker.findData(wanted) if wanted else -1
        self._picker.setCurrentIndex(index if index >= 0 else 0)
        self._on_picked()

    def select(self, name):
        index = self._picker.findData(name)
        if index >= 0:
            self._picker.setCurrentIndex(index)

    def _on_picked(self, *_):
        self._selected = self._picker.currentData()
        self.refresh()

    @property
    def package(self):
        return self._packages.package(self._selected) if self._selected else None

    def _editable(self) -> bool:
        package = self.package
        return package is not None and package.provenance == "authored"

    # --- the tree -----------------------------------------------------------

    def refresh(self):
        expanded = self._expanded_keys()
        self._tree.clear()
        package = self.package
        if package is None:
            self._tree.addTopLevelItem(note_row("No packages yet — ＋ makes one"))
            return

        actions_root = QTreeWidgetItem([_ACTIONS_LABEL, str(len(package.actions) or "")])
        actions_root.setForeground(0, Qt.gray)
        actions_root.setForeground(1, Qt.darkGray)
        actions_root.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        actions_root.setIcon(0, icons.ui_icon("action", colour=style.TEXT_MUTED))
        actions_root.setData(0, _ROLE_KIND, "actions")
        actions_root.setToolTip(0, f"Declared in {MANIFEST_NAME} — an id bound to a file "
                                   f"and a function. One file can hold several.")
        self._tree.addTopLevelItem(actions_root)
        self._build_actions(actions_root, package)

        files_root = QTreeWidgetItem([_FILES_LABEL, ""])
        files_root.setForeground(0, Qt.gray)
        files_root.setForeground(1, Qt.darkGray)
        files_root.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        icons.set_folder_icon(files_root, colour=style.TEXT_MUTED)
        files_root.setData(0, _ROLE_KIND, "files")
        files_root.setData(0, _ROLE_PATH, "")
        files_root.setToolTip(0, f"{package.root}")
        self._tree.addTopLevelItem(files_root)
        self._build_files(files_root, package)

        # Actions starts SHUT and Files starts open. You come to this tab for the file
        # tree — the action list has its own tab and a page each — so the half you came
        # for should not need a click, and the half that has a better home elsewhere
        # should not be in the way.
        self._restore(actions_root, expanded, default=False)
        self._restore(files_root, expanded, default=True)

    def _build_actions(self, root, package):
        if not package.actions:
            self._placeholder(root, "nothing declared")
            return
        for action in sorted(package.actions, key=lambda a: a.action_id):
            entry = f"{action.file} · {action.function}()"
            child = QTreeWidgetItem([action.name or action.action_id, ""])
            child.setIcon(0, icons.ui_icon("action", colour=style.TEXT_MUTED))
            child.setData(0, _ROLE_KIND, "action")
            child.setData(0, _ROLE_REF, action.ref)
            tip = action.ref
            if action.description:
                tip += f"\n{action.description}"
            tip += f"\n\nentry point: {entry}\ndouble-click to open its reference page"
            child.setToolTip(0, tip)
            root.addChild(child)

    def _build_files(self, root, package):
        """The package's real tree, folders included.

        Folders come from **disk**, not from the file paths, so a folder you made and have
        not filled yet is still a folder. Sorted parents-before-children, which is what
        lets one pass build the whole thing.
        """
        files = source_files(package)
        nodes = {"": root}
        for path in folders(package):
            parent = nodes.get(_parent_of(path), root)
            item = QTreeWidgetItem([path.rsplit("/", 1)[-1], ""])
            item.setForeground(0, style.qt_colour(style.TEXT))
            item.setForeground(1, Qt.darkGray)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            icons.set_folder_icon(item, colour=style.TEXT)
            item.setData(0, _ROLE_KIND, "folder")
            item.setData(0, _ROLE_PATH, path)
            item.setToolTip(0, f"{package.name}/{path}/\n\nOrganisation only — an action's "
                               f"file is just a relative path.")
            parent.addChild(item)
            nodes[path] = item

        counts = {}
        for name in files:
            users = [a.action_id for a in package.actions if a.file == name]
            child = QTreeWidgetItem([name.rsplit("/", 1)[-1], ""])
            # The Files panel's vocabulary, unchanged (§6.2) — a `.json` is a `.json`
            # wherever you found it. The manifest is the one file with a role rather than
            # a type, so it is muted and says so.
            child.setIcon(0, icons.file_icon(
                name, colour=style.TEXT_MUTED if name == MANIFEST_NAME else style.TEXT))
            if name == MANIFEST_NAME:
                child.setForeground(0, style.qt_colour(style.TEXT_MUTED))
            child.setForeground(1, Qt.darkGray)
            child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            child.setData(0, _ROLE_KIND, "file")
            child.setData(0, _ROLE_PATH, name)
            tip = f"{package.name}/{name}"
            if users:
                tip += "\n\nimplements " + ", ".join(sorted(users))
                child.setText(1, str(len(users)))
            elif name != MANIFEST_NAME:
                tip += "\n\nno action points at this file"
            child.setToolTip(0, tip)
            nodes.get(_parent_of(name), root).addChild(child)
            folder = _parent_of(name)
            while folder:
                counts[folder] = counts.get(folder, 0) + 1
                folder = _parent_of(folder)

        # A collapsed folder still says how much it is hiding.
        for path, item in nodes.items():
            if path:
                item.setText(1, str(counts[path]) if path in counts else "")
        for path, item in nodes.items():
            if path and item.childCount() == 0:
                self._placeholder(item, "empty")

    @staticmethod
    def _placeholder(parent, text):
        item = note_row(text)
        parent.addChild(item)

    # --- expansion state, across refreshes ----------------------------------

    def _expanded_keys(self) -> set:
        keys = set()

        def walk(item, path):
            here = path + (item.text(0),)
            if item.isExpanded():
                keys.add(here)
            for i in range(item.childCount()):
                walk(item.child(i), here)

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i), ())
        return keys

    def _restore(self, item, expanded, path=(), default=False):
        here = path + (item.text(0),)
        item.setExpanded(here in expanded if expanded else default)
        for i in range(item.childCount()):
            self._restore(item.child(i), expanded, here, default)

    # --- the ＋ menu ---------------------------------------------------------

    def add_menu(self) -> QMenu:
        """What the ＋ offers: New Package always, the rest only for a package you authored.

        Keeping "New Package" here even when the selected package is installed is a
        deliberate departure from "the ＋ is only for authored packages": creating a package
        is not an edit to the selected one, and gating it on the selection would leave a
        profile holding only downloaded packages — or none at all — with no way to make a
        first one.

        Built separately from showing it, like `menu_for`, so it can be exercised without a
        modal `exec`.
        """
        menu = QMenu(self)
        menu.addAction("New Authored Package…", self.new_package_requested)
        if self._editable():
            menu.addSeparator()
            menu.addAction("New Action…",
                           lambda: self.new_action_requested.emit(self._selected))
            menu.addAction("New File…",
                           lambda: self.new_file_requested.emit(self._selected, ""))
        return menu

    def _show_add_menu(self):
        self.add_menu().exec(self._add.mapToGlobal(self._add.rect().bottomLeft()))

    # --- interaction --------------------------------------------------------

    def _on_activated(self, item, _column=0):
        kind = item.data(0, _ROLE_KIND)
        if kind == "action":
            self.action_activated.emit(item.data(0, _ROLE_REF))
        elif kind == "file":
            self.document_activated.emit(f"{self._selected}/{item.data(0, _ROLE_PATH)}")
        elif kind in ("folder", "files", "actions"):
            item.setExpanded(not item.isExpanded())

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None or self.package is None:
            return
        menu = self.menu_for(item)
        if menu is not None and not menu.isEmpty():
            menu.exec(self._tree.viewport().mapToGlobal(pos))

    def menu_for(self, item):
        """The context menu for one row, or None where there is nothing to offer.

        Split from the handler so it can be exercised without a modal `exec` — the menus
        are where most of this panel's behaviour lives, and a test that could only click
        through a native popup would test nothing.
        """
        kind = item.data(0, _ROLE_KIND)
        path = item.data(0, _ROLE_PATH)
        editable = self._editable()
        package = self._selected
        menu = QMenu(self._tree)

        if kind == "actions":
            if editable:
                menu.addAction("New Action…",
                               lambda: self.new_action_requested.emit(package))
                menu.addSeparator()
            menu.addAction(f"Open {MANIFEST_NAME}",
                           lambda: self.document_activated.emit(
                               f"{package}/{MANIFEST_NAME}"))
            return menu

        if kind == "action":
            ref = item.data(0, _ROLE_REF)
            # See the Actions panel: an action opens a reference page, a file opens its
            # bytes, and both used to be called "Open".
            menu.addAction("View Info", lambda: self.action_activated.emit(ref))
            return menu

        if kind not in ("files", "folder", "file"):
            return None

        # A folder (or the Files root) creates INTO itself; a file creates a sibling. That
        # is the only place the clicked row's identity changes what the menu means, so it
        # is worth being exact about rather than always using the package root.
        into = path if kind in ("files", "folder") else _parent_of(path)
        if kind == "file":
            menu.addAction("Open", lambda: self.document_activated.emit(
                f"{package}/{path}"))
            menu.addSeparator()
        if editable:
            menu.addAction("New Folder…",
                           lambda: self.new_folder_requested.emit(package, into))
            menu.addAction("New File…",
                           lambda: self.new_file_requested.emit(package, into))
            menu.addSeparator()
        menu.addAction("Open in File Explorer", lambda: self._reveal(path, kind))
        if editable and kind == "folder":
            menu.addSeparator()
            menu.addAction("Rename…",
                           lambda: self.rename_folder_requested.emit(package, path))
            menu.addAction("Delete…",
                           lambda: self.delete_folder_requested.emit(package, path))
        elif editable and kind == "file" and path != MANIFEST_NAME:
            menu.addSeparator()
            menu.addAction("Rename…",
                           lambda: self.rename_file_requested.emit(package, path))
            menu.addAction("Delete…",
                           lambda: self.delete_file_requested.emit(package, path))
        return menu

    def _reveal(self, path, kind):
        """Open Explorer at the folder holding this row — the folder itself for a folder,
        its parent for a file, since revealing a file means showing where it lives."""
        package = self.package
        if package is None:
            return
        target = Path(package.root) / path if path else Path(package.root)
        if kind == "file":
            target = target.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
