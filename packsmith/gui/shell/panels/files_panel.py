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
    QAbstractItemView, QComboBox, QInputDialog,
)

from packsmith.core.capabilities import DATAPACKS_WRITE, RESOURCEPACKS_WRITE
from packsmith.core.files import FileStore
from packsmith.gui.shell import icons, style
from packsmith.gui.shell.tree import PanelTree
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
_ROLE_CATEGORY = Qt.UserRole + 3   # 'datapacks' / 'resourcepacks' in Smart Mode


def _parent_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _norm(path: str) -> str:
    """The store's own canonical key — one definition, so the browser can never disagree
    with the hard-block about which record belongs to which file."""
    return FileStore.key(path)


class FilesPanel(Panel):

    file_activated = Signal(str)        # relative path (double-clicked)
    ownership_changed = Signal(str)     # relative path

    def __init__(self, file_store, parent=None, loader=None):
        super().__init__("Files", parent)
        self._files = file_store
        self._owners = {}
        # The active global pack loader (§8.1), or None. Smart Mode's categories come from
        # it: without a loader there is no such thing as a global datapack, so there is
        # nothing to categorise and the mode stays disabled.
        self._loader = loader
        self._smart = False
        self._mc_version = None      # set by the window; decides pack_format
        self._client_jar = None      # the real jar, when it can be found

        mode_row = QWidget()
        mode_lay = QHBoxLayout(mode_row)
        mode_lay.setContentsMargins(6, 4, 6, 2)
        self._mode = QComboBox()
        self._mode.addItem("Honest — raw filesystem")
        self._mode.addItem("Smart — by purpose"
                           + ("" if loader else " (needs a pack loader)"))
        if loader is None:
            # §6.2 says categories without an integration simply don't appear. Minecraft
            # has no vanilla global datapacks (§8.1), so with no loader mod installed
            # there is genuinely nothing for this mode to show.
            self._mode.model().item(1).setEnabled(False)
            self._mode.setToolTip(
                "Smart Mode groups files by purpose, and its categories come from an "
                "installed global pack loader (Paxi, OpenLoader, …). This pack has none.")
        else:
            self._mode.setToolTip(f"Smart Mode categories come from {loader.name} (§8.1)")
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        self._mode.setStyleSheet(f"font-size: 11px; color: {style.TEXT_MUTED};")
        mode_lay.addWidget(self._mode)
        self.body().addWidget(mode_row)

        legend = QLabel(
            f"<span style='color:{style.OWNER_USER}'>●</span> yours &nbsp;"
            f"<span style='color:{style.OWNER_ACTION}'>●</span> action &nbsp;"
            f"<span style='color:{style.TEXT_FAINT}'>untouched</span>")
        legend.setTextFormat(Qt.RichText)
        legend.setStyleSheet(f"font-size: 10px; padding: 0 8px 4px 8px;")
        self.body().addWidget(legend)

        self._tree = PanelTree()
        icons.follow_expansion(self._tree)
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

    def set_loader(self, loader):
        """Adopt the loader resolved from a newly imported packdump (§8.1).

        Installing or removing Paxi is an ordinary packdump change, and Smart Mode is built
        entirely out of the loader's directories — so losing the loader has to drop the
        panel back to Honest Mode rather than leave a mode with nothing behind it.
        """
        self._loader = loader
        if loader is None:
            self._smart = False
        self.refresh()

    def refresh(self):
        """Reload ownership and rebuild the visible tree, preserving what was expanded."""
        self._owners = {_norm(p): o for p, o in self._files.all_ownership().items()}
        expanded = self._expanded_paths()
        self._tree.clear()
        if self._smart and self._loader is not None:
            self._populate_smart()
        else:
            self._populate(None, self._files.root, "")
        self._restore_expanded(expanded)

    def _on_mode_changed(self, index):
        self._smart = index == 1 and self._loader is not None
        self.refresh()

    def _populate_smart(self):
        """§6.2 Smart Mode: files grouped by purpose rather than by where they sit.

        The categories are the loader's own directories (§8.1), so this is not a
        reinterpretation of the filesystem — it is the same files, reached by what they are
        FOR. A pack under `config/paxi/datapacks/` is a datapack; the honest tree can only
        tell you it is a folder inside a mod's config.

        Order matters here in a way it doesn't in Honest Mode: packs are listed as the
        loader loads them, because with Paxi that is a thing the user controls and needs
        to see.
        """
        # Only the kinds this loader actually has. Moonlight does global datapacks and has
        # no concept of resource packs at all (§8.1), so asking it for that root raises —
        # and asking unconditionally is what took the whole panel down.
        offered = set(self._loader.available_capabilities(self._files.root))
        for label, kind, capability in (
                ("Datapacks", "datapacks", DATAPACKS_WRITE),
                ("Resource Packs", "resourcepacks", RESOURCEPACKS_WRITE)):
            if capability not in offered:
                continue
            root = (self._loader.datapack_root(self._files.root) if kind == "datapacks"
                    else self._loader.resourcepack_root(self._files.root))
            category = QTreeWidgetItem([label])
            # A datapack is a pack, not a folder — the category deserves its own mark
            # rather than the generic folder every other row already uses.
            category.setIcon(0, icons.ui_icon(kind, colour=style.TEXT))
            category.setData(0, _ROLE_CATEGORY, kind)
            category.setData(0, _ROLE_IS_DIR, True)
            # Marked loaded, because its children are built right here. Without this the
            # expand handler treats it as an unvisited folder and populates it a SECOND
            # time from the filesystem — every pack listed twice, which reads as a
            # plausible number rather than as a bug.
            category.setData(0, _ROLE_LOADED, True)
            category.setForeground(0, QColor(style.TEXT))
            try:
                rel_root = Path(root).relative_to(self._files.root).as_posix()
            except ValueError:
                rel_root = None
            category.setData(0, _ROLE_PATH, rel_root)
            self._tree.addTopLevelItem(category)
            category.setExpanded(True)

            packs = self._loader.packs(self._files.root, kind) if rel_root else []
            for name in packs:
                rel = f"{rel_root}/{name}"
                item = QTreeWidgetItem([name])
                item.setData(0, _ROLE_PATH, rel)
                is_dir = (self._files.root / rel).is_dir()
                item.setData(0, _ROLE_IS_DIR, is_dir)
                if is_dir:
                    item.setData(0, _ROLE_LOADED, False)
                    item.addChild(QTreeWidgetItem(["…"]))
                else:
                    # A zipped pack: real, listed, and not something to browse into here.
                    self._style_file(item, rel)
                    item.setToolTip(0, f"{rel} — a zipped pack")
                category.addChild(item)
            if not packs:
                empty = QTreeWidgetItem(["(none yet — right-click to create one)"])
                empty.setForeground(0, QColor(style.TEXT_FAINT))
                empty.setFlags(Qt.ItemIsEnabled)
                category.addChild(empty)

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
                icons.set_folder_icon(item, colour=style.TEXT)
                item.addChild(QTreeWidgetItem(["…"]))   # placeholder so it shows an arrow
            else:
                self._style_file(item, rel)
            if parent_item is None:
                self._tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)

    def _style_file(self, item, rel):
        """§6.2 Tracked vs Untracked: owned files render normally with an ownership badge,
        untouched ones render muted. Not hidden — "hiding them would make the browser
        useless" — just visibly not-yet-tracked.

        Two channels, and they answer different questions: the icon's **shape** is what the
        file is, and its **colour** is whose it is. The name's colour then only has to carry
        tracked-versus-not, which is what §6.2 asks it for.

        This replaced a tiny corner badge. A whole glyph in the ownership colour is legible
        at a glance where a 9px dot needed looking for — and the badge had been sitting in
        the icon slot, so the type icon could not have coexisted with it anyway.
        """
        name = rel.rsplit("/", 1)[-1]
        ownership = self._owners.get(_norm(rel))
        if ownership is None:
            item.setForeground(0, QColor(style.TEXT_FAINT))
            item.setIcon(0, icons.file_icon(name, colour=style.TEXT_FAINT))
            item.setToolTip(0, f"{rel}\nuntouched — no ownership record")
            return
        kind = ownership.get("kind")
        # The ICON-weight ownership colours, not the accent pair: a stroked glyph in
        # `OWNER_USER` measures 2.0:1 against this background, which is invisible.
        item.setIcon(0, icons.file_icon(
            name,
            colour=style.OWNER_USER_ICON if kind == "user" else style.OWNER_ACTION_ICON))
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
        menu = self._menu_for(self._tree.itemAt(pos))
        menu.exec(self._tree.mapToGlobal(pos))

    def _menu_for(self, item):
        """§6.2: "Honest Mode shows a standard filesystem context menu (New File, New
        Folder, Rename, Delete, Reveal in Explorer)" — plus the ownership actions, which
        only mean anything for a file. A null item is empty space, which targets the root,
        so there is always a way to make the first file in an empty instance."""
        rel = item.data(0, _ROLE_PATH) if item is not None else ""
        is_dir = bool(item.data(0, _ROLE_IS_DIR)) if item is not None else True

        # A Smart Mode category is not a folder you manage — it is the loader's own
        # directory. Renaming or deleting it would break the loader, so it gets exactly the
        # one action that belongs there (§8.1's "New Datapack…" registration).
        category = item.data(0, _ROLE_CATEGORY) if item is not None else None
        if category is not None:
            menu = QMenu(self)
            noun = "Datapack" if category == "datapacks" else "Resource Pack"
            menu.addAction(f"New {noun}…", lambda: self._new_pack(category))
            if rel:
                menu.addSeparator()
                menu.addAction("Reveal in Explorer", lambda: self._reveal(rel))
            return menu

        menu = QMenu(self)
        if not is_dir:
            ownership = self._owners.get(_norm(rel))
            kind = ownership.get("kind") if ownership else None
            if kind is None:
                menu.addAction("Claim ownership", lambda: self._claim(rel))
            elif kind == "action":
                menu.addAction("Take ownership from the action…",
                               lambda: self._take(rel, ownership))
                menu.addAction("Release ownership", lambda: self._release(rel))
            else:
                menu.addAction("Release ownership", lambda: self._release(rel))
            menu.addSeparator()

        parent = rel if is_dir else _parent_of(rel)
        menu.addAction("New File…", lambda: self._new_file(parent))
        menu.addAction("New Folder…", lambda: self._new_folder(parent))
        if item is not None:
            menu.addAction("Rename…", lambda: self._rename(rel, is_dir))
            menu.addAction("Delete…", lambda: self._delete(rel, is_dir))
        menu.addSeparator()
        menu.addAction("Reveal in Explorer", lambda: self._reveal(rel))
        return menu

    # --- filesystem actions (design 6.2) ------------------------------------

    def _new_pack(self, kind):
        """Create a global datapack or resource pack through the loader (§8.1).

        The loader owns the layout, not this panel: it decides where the pack goes and
        writes the `pack.mcmeta` Minecraft requires. A pack without one is silently ignored
        by the game — the folder is there, the files are there, and nothing happens — which
        is the most confusing failure available, so it is never left to the user.
        """
        noun = "datapack" if kind == "datapacks" else "resource pack"
        name, ok = QInputDialog.getText(self, f"New {noun}", f"{noun.title()} name:")
        name = (name or "").strip()
        if not ok or not name:
            return
        if "/" in name or chr(92) in name:
            QMessageBox.warning(self, f"New {noun}",
                                "A pack name can't contain a path separator.")
            return
        root = (self._loader.datapack_root(self._files.root) if kind == "datapacks"
                else self._loader.resourcepack_root(self._files.root))
        if (Path(root) / name).exists():
            QMessageBox.warning(self, f"New {noun}", f"'{name}' already exists.")
            return
        try:
            folder = self._loader.create_pack(self._files.root, name, kind=kind,
                                              pack_format=self._pack_format(kind))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, f"New {noun}", f"Could not create '{name}': {e}")
            return
        rel = Path(folder).relative_to(self._files.root).as_posix()
        self._after_change(rel, f"Created {noun} '{name}' — {self._loader.name} loads it.")

    def _pack_format(self, kind) -> int:
        """Which `pack_format` the new pack declares. Minecraft refuses a pack whose
        format doesn't match its version, and "incompatible" is a confusing thing to read
        on a pack you just made."""
        from packsmith.core.capabilities import pack_format_for
        return pack_format_for(self._mc_version, kind, client_jar=self._client_jar)

    def _new_file(self, parent_rel):
        name, ok = QInputDialog.getText(self, "New File", "File name:")
        if not ok or not name.strip():
            return
        rel = f"{parent_rel}/{name.strip()}" if parent_rel else name.strip()
        if self._files.exists(rel):
            QMessageBox.warning(self, "New File", f"{rel} already exists.")
            return
        # Creating a file claims it: the user made it, so it is theirs from birth. This is
        # the one place a claim happens without an edit, and it needs no prompt — there is
        # no prior owner to take it from.
        self._files.write(rel, "", owner="user")
        self._after_change(rel, f"Created {rel} — it's yours.")
        self.file_activated.emit(rel)

    def _new_folder(self, parent_rel):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not ok or not name.strip():
            return
        rel = f"{parent_rel}/{name.strip()}" if parent_rel else name.strip()
        try:
            (self._files.root / rel).mkdir(parents=True, exist_ok=False)
        except OSError as e:
            QMessageBox.warning(self, "New Folder", f"Could not create {rel}:\n{e}")
            return
        self._after_change(rel, f"Created folder {rel}.")

    def _rename(self, rel, is_dir):
        old_name = rel.rsplit("/", 1)[-1]
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=old_name)
        name = name.strip() if ok else ""
        if not name or name == old_name:
            return
        parent = _parent_of(rel)
        target = f"{parent}/{name}" if parent else name

        # A rename doesn't destroy anything, so it isn't gated — but it does quietly break
        # the link between an action and the path it writes, and the user deserves to know
        # that before wondering why the file came back.
        managed = [p for p, o in self._files.owned_under(rel).items() if o["kind"] == "action"]
        if managed and QMessageBox.question(
                self, "Rename a managed file",
                f"{rel}\n\n{'This file is' if len(managed) == 1 else f'{len(managed)} files here are'} "
                f"written by an action. Renaming won't change where the action writes — it "
                f"will recreate the old path on its next run.\n\nRename anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            self._files.rename(rel, target)
        except (OSError, ValueError, FileExistsError, FileNotFoundError) as e:
            QMessageBox.warning(self, "Rename", f"Could not rename {rel}:\n{e}")
            return
        self._after_change(target, f"Renamed {rel} → {target}.")

    def _delete(self, rel, is_dir):
        """Deleting destroys bytes, so it always asks — and it says what it's destroying.
        A folder's confirmation counts the owned files under it, because "delete config/"
        is a very different act depending on whether an action lives in there."""
        owned = self._files.owned_under(rel)
        detail = ""
        if is_dir:
            managed = sum(1 for o in owned.values() if o["kind"] == "action")
            detail = f"\n\n{len(owned)} tracked file(s) beneath it"
            detail += f", {managed} written by actions." if managed else "."
        elif owned:
            who = next(iter(owned.values()))
            if who["kind"] == "action":
                detail = (f"\n\nThis file is written by '{who.get('action_ref') or 'an action'}'. "
                          f"Deleting it won't stop that action — the next run recreates it.")
        if QMessageBox.question(
                self, "Delete", f"Permanently delete {rel}?{detail}\n\nThis cannot be undone.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            self._files.delete_tree(rel) if is_dir else self._files.delete(rel)
        except OSError as e:
            QMessageBox.warning(self, "Delete", f"Could not delete {rel}:\n{e}")
            return
        self._after_change(rel, f"Deleted {rel}.")

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
