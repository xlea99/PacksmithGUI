"""The Actions panel (design 4.1 / 3.3.1) — installed packages, by their two layers.

"Reference/browse panel for all installed actions, organized by package. Inspect
manifests, read documentation… Actions are not pinnable — they are not the primary
runnable surface." (Jobs are, §3.3.2.)

A package genuinely has **two layers**, and collapsing them was making the model harder
to see than it is:

* **Actions** — what ``manifest.toml`` declares. Each is an ``(id, file, function)``
  triple. Many actions may share one file; the operations here are *declare* and
  *undeclare*.
* **Files** — what is on disk. A file with no declaration is an ordinary source file, and
  the operations here are *create*, *rename* and *delete*.

Keeping them apart is what makes "undeclare ≠ delete" expressible: dropping a declaration
turns an entry point back into a plain function, which is how a file becomes a library.

This is also where a package's source files surface — deliberately *not* the Files panel.
That panel is a tree rooted at the game instance, and package sources are not in it; they
are PackSmith's own userdata. Each keeps the rules that suit it: instance files answer to
ownership, package sources to provenance (§3.3.1 — you may edit what you authored, not
what you downloaded).
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QAbstractItemView, QMenu, QWidget, QHBoxLayout,
    QPushButton,
)

from packsmith.core.packages import MANIFEST_NAME, folders, source_files
from packsmith.gui.shell import style
from packsmith.gui.shell.tree import PanelTree
from packsmith.gui.shell.panels.base import Panel

_ROLE_DOC = Qt.UserRole        # "<package>/<file>" for an openable source file
_ROLE_REF = Qt.UserRole + 1    # action ref, for action rows
_ROLE_PACKAGE = Qt.UserRole + 2
_ROLE_FILE = Qt.UserRole + 3   # package-relative file path, for file rows
_ROLE_FOLDER = Qt.UserRole + 4  # package-relative folder path ("" = the package root)


def _parent_of(path: str) -> str:
    """The containing folder of a package-relative path; "" for the package root."""
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _button(text, tooltip=""):
    button = QPushButton(text)
    button.setFixedHeight(22)
    button.setCursor(Qt.PointingHandCursor)
    button.setToolTip(tooltip)
    button.setStyleSheet(f"""
        QPushButton {{
            background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
            border: 1px solid {style.BORDER}; font-size: 11px; padding: 1px 8px;
        }}
        QPushButton:hover {{ color: {style.TEXT}; border-color: {style.ACCENT_EDGE}; }}
    """)
    return button


class ActionsPanel(Panel):

    document_activated = Signal(str)      # "<package>/<file>" within the packages dir
    new_action_requested = Signal(str)    # package name, or "" to choose in the dialog
    new_file_requested = Signal(str, str)          # package (or ""), destination folder
    new_folder_requested = Signal(str, str)        # package (or ""), parent folder
    remove_action_requested = Signal(str)          # action ref
    rename_file_requested = Signal(str, str)       # package, file
    delete_file_requested = Signal(str, str)       # package, file
    rename_folder_requested = Signal(str, str)     # package, folder
    delete_folder_requested = Signal(str, str)     # package, folder

    def __init__(self, package_index, parent=None):
        super().__init__("Actions", parent)
        self._packages = package_index

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(6, 4, 6, 4)
        bar_lay.setSpacing(4)
        new_action = _button("＋  Action", "Declare an action in a package's manifest")
        new_action.clicked.connect(lambda: self.new_action_requested.emit(""))
        new_file = _button("＋  File", "Add a Starlark file without declaring anything")
        new_file.clicked.connect(lambda: self.new_file_requested.emit("", ""))
        bar_lay.addWidget(new_action)
        bar_lay.addWidget(new_file)
        bar_lay.addStretch()
        self.body().addWidget(bar)

        self._tree = PanelTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.itemDoubleClicked.connect(self._on_activated)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self.body().addWidget(self._tree)

        self.refresh()

    # ------------------------------------------------------------------ building

    def refresh(self):
        expanded = self._expanded_keys()
        self._tree.clear()
        packages = self._packages.packages
        if not packages:
            empty = QTreeWidgetItem(["No packages installed", ""])
            empty.setForeground(0, Qt.gray)
            self._tree.addTopLevelItem(empty)
            return

        for name, package in sorted(packages.items()):
            root = QTreeWidgetItem([name, package.provenance])
            root.setForeground(0, Qt.gray)
            root.setForeground(1, Qt.darkGray)
            root.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            root.setToolTip(0, package.description or name)
            root.setData(0, _ROLE_PACKAGE, name)
            root.setData(0, _ROLE_DOC, f"{name}/{MANIFEST_NAME}")
            self._tree.addTopLevelItem(root)

            self._build_actions(root, package)
            self._build_files(root, package)
            self._restore(root, expanded, default=True)
        self._tree.resizeColumnToContents(0)

    def _build_actions(self, root, package):
        group = QTreeWidgetItem(["Actions", str(len(package.actions) or "")])
        group.setForeground(0, Qt.darkGray)
        group.setForeground(1, Qt.darkGray)
        group.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        group.setToolTip(0, "Declared in manifest.toml — an id bound to a file and a "
                            "function. One file can hold several.")
        group.setData(0, _ROLE_PACKAGE, package.name)
        root.addChild(group)

        if not package.actions:
            self._placeholder(group, "nothing declared")
            return
        for action in sorted(package.actions, key=lambda a: a.action_id):
            entry = f"{action.file} · {action.function}()"
            child = QTreeWidgetItem([action.name or action.action_id, entry])
            child.setForeground(1, Qt.darkGray)
            child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            child.setData(0, _ROLE_DOC, f"{package.name}/{action.file}")
            child.setData(0, _ROLE_REF, action.ref)
            child.setData(0, _ROLE_PACKAGE, package.name)
            tip = action.ref
            if action.description:
                tip += f"\n{action.description}"
            tip += f"\n\nentry point: {entry}\ndouble-click to open its source"
            child.setToolTip(0, tip)
            group.addChild(child)

    def _build_files(self, root, package):
        files = source_files(package)
        group = QTreeWidgetItem(["Files", str(len(files) or "")])
        group.setForeground(0, Qt.darkGray)
        group.setForeground(1, Qt.darkGray)
        group.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        group.setToolTip(0, "What's actually on disk. A file with no declaration is an "
                            "ordinary source file, not a mistake.")
        group.setData(0, _ROLE_PACKAGE, package.name)
        group.setData(0, _ROLE_FOLDER, "")     # the package root, as a drop target
        root.addChild(group)

        # Folders come from disk, not from the file paths, so an empty one still shows.
        # Sorted parents-before-children, which is what makes this single pass work.
        nodes = {"": group}
        for path in folders(package):
            parent = nodes.get(_parent_of(path), group)
            item = QTreeWidgetItem([f"{path.rsplit('/', 1)[-1]}/", ""])
            item.setForeground(0, Qt.gray)
            item.setForeground(1, Qt.darkGray)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            item.setData(0, _ROLE_PACKAGE, package.name)
            item.setData(0, _ROLE_FOLDER, path)
            item.setToolTip(0, f"{package.name}/{path}/\n\nOrganisation only — an action's "
                               f"file is just a relative path.")
            parent.addChild(item)
            nodes[path] = item

        if not files:
            self._placeholder(group, "empty")

        counts = {}
        for name in files:
            users = [a.action_id for a in package.actions if a.file == name]
            if name == MANIFEST_NAME:
                note = "manifest"
            elif users:
                note = f"{len(users)} action{'s' if len(users) > 1 else ''}"
            else:
                note = "—"
            child = QTreeWidgetItem([name.rsplit("/", 1)[-1], note])
            child.setForeground(1, Qt.darkGray)
            child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            child.setData(0, _ROLE_DOC, f"{package.name}/{name}")
            child.setData(0, _ROLE_PACKAGE, package.name)
            child.setData(0, _ROLE_FILE, name)
            tip = f"{package.name}/{name}"
            if users:
                tip += "\n\nimplements " + ", ".join(sorted(users))
            elif name != MANIFEST_NAME:
                tip += "\n\nno action points at this file"
            child.setToolTip(0, tip)
            nodes.get(_parent_of(name), group).addChild(child)
            # Roll the count up every ancestor, so a collapsed folder still says how much
            # it's hiding.
            folder = _parent_of(name)
            while folder:
                counts[folder] = counts.get(folder, 0) + 1
                folder = _parent_of(folder)

        for path, item in nodes.items():
            if path:
                item.setText(1, str(counts[path]) if path in counts else "")

        # Empty folders would otherwise look like leaves you could open.
        for path, item in nodes.items():
            if path and item.childCount() == 0:
                self._placeholder(item, "empty")

    @staticmethod
    def _placeholder(group, text):
        item = QTreeWidgetItem([text, ""])
        item.setForeground(0, Qt.darkGray)
        item.setFlags(Qt.ItemIsEnabled)
        group.addChild(item)

    # -------------------------------------------------- expansion state, across refreshes

    def _expanded_keys(self) -> set:
        """Refresh rebuilds the tree, so remember which branches were open. Without this,
        every create/delete would slam the panel shut on the user."""
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

    # ------------------------------------------------------------------ interaction

    def _on_activated(self, item, _column=0):
        document = item.data(0, _ROLE_DOC)
        if document:
            self.document_activated.emit(document)

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        package_name = item.data(0, _ROLE_PACKAGE)
        if not package_name:
            return
        package = self._packages.package(package_name)
        editable = package is not None and package.provenance == "authored"

        menu = QMenu(self)
        document = item.data(0, _ROLE_DOC)
        ref = item.data(0, _ROLE_REF)
        file_name = item.data(0, _ROLE_FILE)
        folder = item.data(0, _ROLE_FOLDER)

        if document:
            label = "Open manifest" if document.endswith(MANIFEST_NAME) else "Open"
            menu.addAction(label, lambda: self.document_activated.emit(document))

        if ref and editable:
            menu.addSeparator()
            # Worded as the declaration it is, not as a delete — the file survives.
            menu.addAction("Remove declaration…",
                           lambda: self.remove_action_requested.emit(ref))
        elif file_name and editable and file_name != MANIFEST_NAME:
            menu.addSeparator()
            # "Rename" moves too: a new name may name a folder.
            menu.addAction("Rename or move…", lambda: self.rename_file_requested.emit(
                package_name, file_name))
            menu.addAction("Delete…", lambda: self.delete_file_requested.emit(
                package_name, file_name))
        elif folder and editable:
            menu.addSeparator()
            menu.addAction("Rename or move…", lambda: self.rename_folder_requested.emit(
                package_name, folder))
            menu.addAction("Delete folder…", lambda: self.delete_folder_requested.emit(
                package_name, folder))

        if editable:
            # A folder row (or the Files group) creates *into* itself; a file row creates
            # a sibling; anywhere else creates at the package root.
            if folder is not None:
                into = folder
            elif file_name:
                into = _parent_of(file_name)
            else:
                into = ""
            menu.addSeparator()
            menu.addAction("New action…",
                           lambda: self.new_action_requested.emit(package_name))
            menu.addAction("New file…" if not into else f"New file in {into}/…",
                           lambda: self.new_file_requested.emit(package_name, into))
            menu.addAction("New folder…" if not into else f"New folder in {into}/…",
                           lambda: self.new_folder_requested.emit(package_name, into))

        if not menu.isEmpty():
            menu.exec(self._tree.mapToGlobal(pos))
