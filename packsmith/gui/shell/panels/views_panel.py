"""The Views panel (design 4.1) — the home for saved Views.

"Searchable list of saved view configurations. Double-click opens the View in its chosen
renderer."

This panel is what makes closing a tab safe: a View lives *here*, not in the tab bar, so
closing its tab only closes a window onto it. Deleting is a separate, explicit act —
right-click → Delete — which is exactly the close-≠-delete lifecycle the shell wanted.

**Groups**, because a flat list lies about what a set of views is. A removal workflow is
one working view plus three saturated filters of it: four entries that are *one* thing,
and side by side with everything else they read as four unrelated things. A group says
"these belong together" without needing a naming convention to carry it.

One level deep, deliberately. Nesting invites a hierarchy nobody wants to maintain, and
the thing being organised is a couple of dozen hand-made artifacts, not a filesystem.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QPushButton, QHBoxLayout, QWidget, QMenu, QLineEdit, QAbstractItemView,
    QTreeWidgetItem, QInputDialog,
)

from packsmith.gui.shell import icons, style
from packsmith.gui.shell.tree import PanelTree
from packsmith.gui.shell.panels.base import Panel, SearchBox

_ROLE_VIEW = Qt.UserRole        # the View on a leaf
_ROLE_GROUP = Qt.UserRole + 1   # the group name on a folder

# Which concept mark a View wears, by the renderer it opens in (design 5.0).
#
# One list in the panel, several renderers behind it — so the row has to say which, the
# same way the tab does. A blueprint View wearing the table mark in the sidebar and the
# blueprint mark on its tab is the one place that inconsistency is visible side by side.
#
# Keyed by renderer rather than branched on, because §5.0 makes renderers additive: a new
# one registers a type identifier and this grows by a line.
_RENDERER_MARKS = {"blueprint_grid": "blueprints"}
_DEFAULT_MARK = "views"


class _ViewsTree(PanelTree):
    """A tree that stays one level deep however you drop things into it."""

    dropped = Signal()

    def dropEvent(self, event):
        super().dropEvent(event)
        self._flatten_groups()
        self.dropped.emit()

    def _flatten_groups(self):
        """Pull any group that landed inside another back to the top level.

        Enforced *after* the drop rather than by refusing it mid-drag: Qt's drop validation
        runs on every mouse move and getting it subtly wrong makes the whole list feel
        sticky. Correcting one rare drop afterwards is cheaper than a drag that fights you.
        """
        for top in range(self.topLevelItemCount()):
            parent = self.topLevelItem(top)
            nested = [parent.child(i) for i in range(parent.childCount())
                      if parent.child(i).data(0, _ROLE_GROUP)]
            for child in nested:
                parent.removeChild(child)
                self.addTopLevelItem(child)


class ViewsPanel(Panel):

    view_activated = Signal(object)    # View
    new_view_requested = Signal()
    edit_requested = Signal(object)    # View — open its query in the constructor
    layout_changed = Signal(list)      # the panel's whole arrangement, see current_layout
    rename_requested = Signal(object)  # View
    delete_requested = Signal(object)  # View

    def __init__(self, views=None, parent=None):
        super().__init__("Views", parent)
        self._views = list(views or [])
        self._entries = []             # the arrangement; see current_layout()

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(6, 4, 6, 4)
        bar_lay.setSpacing(4)

        new_btn = self._small_button("＋  New View")
        new_btn.clicked.connect(self.new_view_requested)
        bar_lay.addWidget(new_btn)

        group_btn = self._small_button("＋  Group")
        group_btn.setToolTip("Make a group to file views under")
        group_btn.clicked.connect(self._new_group)
        bar_lay.addWidget(group_btn)
        bar_lay.addStretch()
        self.body().addWidget(bar)

        # §4.1 calls this a *searchable* list — a mature pack accumulates hundreds.
        self._search = SearchBox("views")
        self._search.textChanged.connect(self.refresh)
        self.body().addWidget(self._search)

        self._tree = _ViewsTree()
        self._tree.setHeaderHidden(True)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        # Drag to reorder and to file into a group. Views are hand-authored, so their order
        # is a workflow — the one you open first, then the one you work, then reference —
        # a sequence rather than a ranking, which is why this beats pinning here. (The
        # Registry panel is pinned instead: 135 machine-generated names have a long tail
        # and no workflow.)
        self._tree.setDragDropMode(QAbstractItemView.InternalMove)
        self._tree.setDefaultDropAction(Qt.MoveAction)
        self._tree.dropped.connect(self._on_dropped)
        self._tree.itemActivated.connect(self._on_activated)
        self._tree.itemDoubleClicked.connect(self._on_activated)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        icons.follow_expansion(self._tree)
        self.body().addWidget(self._tree)

        self.refresh()

    @staticmethod
    def _small_button(text):
        button = QPushButton(text)
        button.setFixedHeight(22)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet(f"""
            QPushButton {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER}; font-size: 11px; padding: 1px 8px;
            }}
            QPushButton:hover {{ color: {style.TEXT}; border-color: {style.ACCENT_EDGE}; }}
        """)
        return button

    # --- the stored arrangement --------------------------------------------

    def set_views(self, views, layout=None):
        """Adopt the view list, arranged as the user left it.

        ``layout`` is a list of entries, each either ``{"view": id}`` or
        ``{"group": name, "views": [ids]}``, in display order. Ids it doesn't mention go to
        the **end at the top level** — which is where a view you just made belongs — and
        ids naming views that no longer exist are dropped. Both are ordinary rather than
        exceptional: the arrangement is furniture, and it outlives edits to the things it
        arranges.
        """
        self._views = list(views)
        known = {v.id: v for v in self._views}
        seen, arranged = set(), []

        for entry in (layout or []):
            if "group" in entry:
                members = [known[i] for i in entry.get("views", []) if i in known]
                seen.update(v.id for v in members)
                # A group whose views have all been deleted is kept: it is a thing the user
                # named and may still be filing into. Only the user removes a group.
                arranged.append({"group": entry["group"], "views": members})
            elif entry.get("view") in known:
                seen.add(entry["view"])
                arranged.append({"view": known[entry["view"]]})

        arranged.extend({"view": v} for v in self._views if v.id not in seen)
        self._entries = arranged
        self.refresh()

    def current_layout(self) -> list:
        """The arrangement, as storable data."""
        out = []
        for entry in self._entries:
            if "group" in entry:
                out.append({"group": entry["group"],
                            "views": [v.id for v in entry["views"]]})
            else:
                out.append({"view": entry["view"].id})
        return out

    def _publish(self):
        self.layout_changed.emit(self.current_layout())

    def _on_dropped(self):
        """Read the arrangement back off the widget after a drop.

        Off the WIDGET rather than computed from drop indices: Qt's internal move already
        did the work, and re-deriving it from source and destination rows is the classic
        place to get an off-by-one that only shows when you drag downward.
        """
        arranged = []
        for top in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(top)
            name = item.data(0, _ROLE_GROUP)
            if name:
                arranged.append({
                    "group": name,
                    "views": [item.child(i).data(0, _ROLE_VIEW)
                              for i in range(item.childCount())],
                })
            elif item.data(0, _ROLE_VIEW) is not None:
                arranged.append({"view": item.data(0, _ROLE_VIEW)})
        self._entries = arranged
        self._publish()
        self.refresh()

    # --- groups ------------------------------------------------------------

    def _group_names(self) -> list:
        return [e["group"] for e in self._entries if "group" in e]

    def _new_group(self):
        name, ok = QInputDialog.getText(self, "New group", "Group name:")
        name = name.strip()
        if not ok or not name or name in self._group_names():
            return
        self._entries.append({"group": name, "views": []})
        self._publish()
        self.refresh()

    def _rename_group(self, old):
        name, ok = QInputDialog.getText(self, "Rename group", "Group name:", text=old)
        name = name.strip()
        if not ok or not name or (name != old and name in self._group_names()):
            return
        for entry in self._entries:
            if entry.get("group") == old:
                entry["group"] = name
        self._publish()
        self.refresh()

    def _delete_group(self, name):
        """Remove the folder; its views return to the top level.

        Never the views themselves — a group is how they are filed, and deleting a folder
        that silently took four saved Views with it would be the worst kind of surprise in
        a panel whose whole point is that closing is not deleting.
        """
        kept = []
        for entry in self._entries:
            if entry.get("group") == name:
                kept.extend({"view": v} for v in entry["views"])
            else:
                kept.append(entry)
        self._entries = kept
        self._publish()
        self.refresh()

    def _move_to_group(self, view, name):
        for entry in list(self._entries):
            if "group" in entry:
                entry["views"] = [v for v in entry["views"] if v.id != view.id]
            elif entry["view"].id == view.id:
                self._entries.remove(entry)
        if name is None:
            self._entries.append({"view": view})
        else:
            for entry in self._entries:
                if entry.get("group") == name:
                    entry["views"].append(view)
                    break
        self._publish()
        self.refresh()

    # --- rendering ---------------------------------------------------------

    def refresh(self, *_):
        needle = self._search.needle()
        # Dragging while filtered is meaningless — you would be rearranging a subset and
        # the drop position says nothing about where an item sits in the whole list. The
        # list you rearrange has to be the list you can see all of.
        self._tree.setDragDropMode(
            QAbstractItemView.NoDragDrop if needle else QAbstractItemView.InternalMove)
        self._tree.clear()

        def matches(view):
            return not needle or needle in view.name.lower()

        for entry in self._entries:
            if "group" in entry:
                members = [v for v in entry["views"] if matches(v)]
                # A group with nothing left to show is hidden while filtering and shown
                # when not: an empty folder is a real, useful state to see.
                if needle and not members:
                    continue
                folder = QTreeWidgetItem([entry["group"]])
                folder.setData(0, _ROLE_GROUP, entry["group"])
                icons.set_expanding_icon(folder, icons.ui("group"),
                                         icons.ui("group_open"),
                                         colour=style.TEXT_MUTED)
                folder.setForeground(0, style.qt_colour(style.TEXT_MUTED))
                # A folder takes drops (that is how you file into it) and is itself
                # draggable (that is how you order the groups).
                folder.setFlags(folder.flags() | Qt.ItemIsDropEnabled
                                | Qt.ItemIsDragEnabled)
                self._tree.addTopLevelItem(folder)
                for view in members:
                    folder.addChild(self._leaf(view))
                folder.setExpanded(True)
            elif matches(entry["view"]):
                self._tree.addTopLevelItem(self._leaf(entry["view"]))

    @staticmethod
    def _leaf(view) -> QTreeWidgetItem:
        item = QTreeWidgetItem([view.name])
        item.setData(0, _ROLE_VIEW, view)
        # The same mark this View's tab wears in the workspace: a View looks like itself
        # wherever you meet it. An unrecognised renderer falls back to the generic mark
        # rather than going blank — the same stance `ViewStore.all()` takes about a row it
        # cannot decode, since a panel is a place to find things, not a place to be strict.
        mark = _RENDERER_MARKS.get(getattr(view, "renderer", ""), _DEFAULT_MARK)
        item.setIcon(0, icons.concept_icon(mark, colour=style.TEXT_MUTED))
        item.setToolTip(0, f"{view.name} — double-click to open")
        # A view is a leaf: droppable ONTO a group, never a container itself.
        item.setFlags(item.flags() & ~Qt.ItemIsDropEnabled)
        return item

    def select_view(self, view):
        for item in self._all_leaves():
            if item.data(0, _ROLE_VIEW).id == view.id:
                self._tree.setCurrentItem(item)
                return

    def _all_leaves(self):
        for top in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(top)
            if item.data(0, _ROLE_VIEW) is not None:
                yield item
            for i in range(item.childCount()):
                yield item.child(i)

    # --- interaction -------------------------------------------------------

    def _on_activated(self, item, _column=0):
        view = item.data(0, _ROLE_VIEW)
        if view is not None:
            self.view_activated.emit(view)
        else:
            item.setExpanded(not item.isExpanded())

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        group = item.data(0, _ROLE_GROUP)
        if group:
            menu.addAction("Rename group…", lambda: self._rename_group(group))
            menu.addAction("Delete group",
                           lambda: self._delete_group(group)).setToolTip(
                "The views inside return to the top level")
        else:
            view = item.data(0, _ROLE_VIEW)
            menu.addAction("Open", lambda: self.view_activated.emit(view))
            menu.addSeparator()
            # Editing the query IS editing the View, so it belongs on the View — beside
            # Rename and Delete, which are the other things you do TO one.
            menu.addAction("Edit view…", lambda: self.edit_requested.emit(view))
            menu.addAction("Rename…", lambda: self.rename_requested.emit(view))
            menu.addAction("Delete", lambda: self.delete_requested.emit(view))
            names = self._group_names()
            if names:
                # Drag is the fast way; a menu is the discoverable one, and it is the only
                # way to file something when the search box is filtering.
                sub = menu.addMenu("Move to group")
                for name in names:
                    sub.addAction(name,
                                  lambda _=False, n=name: self._move_to_group(view, n))
                sub.addSeparator()
                sub.addAction("(none)", lambda: self._move_to_group(view, None))
        menu.exec(self._tree.mapToGlobal(pos))
