"""The Actions panel (design 4.1 / 3.3.1) — every declared action, filed by package.

"Reference/browse panel for all installed actions, organized by package. Inspect
manifests, read documentation… Actions are not pinnable — they are not the primary
runnable surface." (Jobs are, §3.3.2.)

**Two levels, and nothing else: a package, and the actions it declares.** The panel used
to show a package's files here too, on the reasoning that a package has two layers worth
seeing — which is true of a *package*, and turned out to be the wrong thing to put in
front of someone asking "what can I run?". Library files, empty folders and the manifest
all sat between the entry points, so the one list of runnable things was the one thing the
list was bad at. The file layer moves to the Packages tab, where a single package gets the
whole panel and the question being asked is "what is in this thing".

That split does not soften "undeclare ≠ delete" — it sharpens it. Dropping a declaration
turns an entry point back into an ordinary function, and now the two acts live on two
different surfaces rather than two rows of one tree.

Package sources are deliberately *not* in the Files panel. That one is rooted at the game
instance; package sources are Packsmith's own userdata. Each keeps the rules that suit it:
instance files answer to ownership, package sources to provenance (§3.3.1 — you may edit
what you authored, not what you downloaded).
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QAbstractItemView, QHeaderView, QMenu,
)

from packsmith.core.packages import MANIFEST_NAME
from packsmith.gui.shell import icons, style
from packsmith.gui.shell.tree import PanelTree
from packsmith.gui.shell.panels.base import Panel, note_row

_ROLE_DOC = Qt.UserRole        # "<package>/<file>" for an openable source file
_ROLE_REF = Qt.UserRole + 1    # action ref, for action rows
_ROLE_PACKAGE = Qt.UserRole + 2


class ActionsPanel(Panel):

    action_activated = Signal(str)        # action ref — opens its reference page
    document_activated = Signal(str)      # "<package>/<file>" within the packages dir
    new_action_requested = Signal(str)    # package name, or "" to choose in the dialog
    remove_action_requested = Signal(str)          # action ref

    def __init__(self, package_index, parent=None):
        super().__init__("Actions", parent)
        self._packages = package_index

        # No ＋ buttons. "＋ Action" and "＋ File" sat side by side offering two things that
        # sound like the same thing and are not — one edits a manifest, the other writes a
        # file — and the toolbar could not say which you wanted. Both operations survive on
        # the right-click menu, where the row you clicked supplies the context the buttons
        # were missing.

        self._tree = PanelTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.itemDoubleClicked.connect(self._on_activated)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        # The name column takes what is left; provenance and the entry point take what they
        # need. Sizing the name column to its contents counts the indent and the icon too,
        # and pushes the right-hand column off the edge of a 230px panel.
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.body().addWidget(self._tree)

        self.refresh()

    # ------------------------------------------------------------------ building

    def refresh(self):
        expanded = self._expanded_keys()
        self._tree.clear()
        packages = self._packages.packages
        if not packages:
            self._tree.addTopLevelItem(note_row("No packages installed"))
            return

        for name, package in sorted(packages.items()):
            root = QTreeWidgetItem([name, package.provenance])
            root.setForeground(0, Qt.gray)
            root.setForeground(1, Qt.darkGray)
            root.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            root.setIcon(0, icons.ui_icon("package", colour=style.TEXT_MUTED))
            root.setToolTip(0, package.description or name)
            root.setData(0, _ROLE_PACKAGE, name)
            root.setData(0, _ROLE_DOC, f"{name}/{MANIFEST_NAME}")
            self._tree.addTopLevelItem(root)

            self._build_actions(root, package)
            self._restore(root, expanded, default=True)

    def _build_actions(self, root, package):
        """The package's declared actions, hung directly off it.

        No "Actions" group node any more. It was the only child, so it bought one level of
        indent and one extra click for nothing — the group existed to sit *beside* a Files
        group, and Files has moved out.
        """
        if not package.actions:
            self._placeholder(root, "nothing declared")
            return
        for action in sorted(package.actions, key=lambda a: a.action_id):
            entry = f"{action.file} · {action.function}()"
            # The entry point stays in the tooltip and on the reference page, NOT in the
            # second column. `lib/removal/obliterate.star · run()` is wider than this whole
            # panel, and a right column sized to it squeezes the left one until the action's
            # own name elides to "r…" — spending the thing you scan for on the thing you
            # rarely need.
            child = QTreeWidgetItem([action.name or action.action_id, ""])
            child.setIcon(0, icons.ui_icon("action", colour=style.TEXT_MUTED))
            child.setData(0, _ROLE_DOC, f"{package.name}/{action.file}")
            child.setData(0, _ROLE_REF, action.ref)
            child.setData(0, _ROLE_PACKAGE, package.name)
            tip = action.ref
            if action.description:
                tip += f"\n{action.description}"
            tip += f"\n\nentry point: {entry}\ndouble-click to open its reference page"
            child.setToolTip(0, tip)
            root.addChild(child)

    @staticmethod
    def _placeholder(group, text):
        group.addChild(note_row(text))

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
        """Double-click an action → its reference page; a package → its manifest.

        The action row used to open the ``.star`` file it points at, which only told you
        anything under a strict one-action-per-file discipline nobody actually keeps — and
        even then it showed the code without the manifest half: no mappings, no
        configuration, no sign of which jobs depend on it. The page shows the whole
        declaration; the source is one click further in, where it belongs.
        """
        ref = item.data(0, _ROLE_REF)
        if ref:
            self.action_activated.emit(ref)
            return
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

        if ref:
            menu.addAction("Open", lambda: self.action_activated.emit(ref))
            if document:
                menu.addAction("Open source", lambda: self.document_activated.emit(document))
            if editable:
                menu.addSeparator()
                # Worded as the declaration it is, not as a delete — the file survives.
                menu.addAction("Remove declaration…",
                               lambda: self.remove_action_requested.emit(ref))
        elif document:
            menu.addAction("Open manifest",
                           lambda: self.document_activated.emit(document))

        if editable:
            # Declaring an action is the one create this panel still offers, because an
            # action is what this panel is *about*. Files and folders moved to the Packages
            # tab along with the tree you would click to say where they go.
            menu.addSeparator()
            menu.addAction("New action…",
                           lambda: self.new_action_requested.emit(package_name))

        if not menu.isEmpty():
            menu.exec(self._tree.mapToGlobal(pos))
