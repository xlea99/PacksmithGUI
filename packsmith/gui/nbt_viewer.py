"""The NBT editor tab — design 6.4.

§6.4 asks for "a tree viewer with expandable nodes and typed value editors, similar in
shape to NBTExplorer". Both halves are here now: looking, and changing a value.

**Values only — not structure.** You can edit what a tag holds; you cannot add, delete,
rename or reorder tags. That is not an arbitrary stopping point: §6.4 explicitly defers
"editor ergonomics, list addressing" to a proper design session, and structural editing is
where the deferred questions actually live — `MyList[3]` is a fragile handle precisely
*because* something can reorder the list. Value editing needs none of that settled.

**A value never changes type.** `Count: 64` and `Count: 64b` are different files, so the
existing tag decides how its replacement is parsed and an edit that doesn't fit is refused
rather than coerced (`nbt.retype`). A byte silently promoted to an int is a corrupted save
that still loads, which is the worst failure available here.

**Ownership is whole-file.** §6.4 wants per-path ownership, and that is right, but §1.1
defers per-key claims until whole-file ones are solid; this saves the file the way the text
editor saves a config.

**Children are built on expand, not up front.** A real `level.dat` from a played world is
337,695 nodes; materialising that as tree items costs seconds and hundreds of megabytes to
show you a root with two keys in it. Compounds and lists know their own size, so the row
can say how many children it has without creating any of them.

**The type is shown, always.** `Count: 64` and `Count: 64b` are different files, and a
viewer that renders both as "64" is hiding the single most important thing about NBT.

It takes **bytes** rather than a path, so a structure file inside a mod's jar opens the
same way as one on disk (design 6.5) — mods ship `.nbt` structures constantly.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTreeWidgetItem,
    QAbstractItemView, QHeaderView, QStyledItemDelegate,
)

from packsmith.core import nbt
from packsmith.gui.shell import style
from packsmith.gui.shell.tree import PanelTree

_ROLE_TAG = Qt.UserRole
_ROLE_LOADED = Qt.UserRole + 1
_ROLE_PATH = Qt.UserRole + 2

_NAME_COLUMN, _TYPE_COLUMN, _VALUE_COLUMN = 0, 1, 2

# A filter that matched everything would build the tree it exists to avoid.
_MAX_MATCHES = 500
# Enough of an array to recognise it; the rest is noise in a tree row.
_ARRAY_PREVIEW = 8


def type_name(tag) -> str:
    """`TAG_Byte`, `TAG_Compound`… — shortened, since the prefix is on every row."""
    try:
        return nbt.TAG_NAMES[nbt.tag_id_of(tag)].removeprefix("TAG_")
    except nbt.NbtError:
        return "?"


def describe_value(tag) -> str:
    """What a row shows in the Value column.

    Containers report their size instead of their contents: the contents are the children,
    and repeating them in the parent's row is noise. Arrays are the exception — they are
    leaves, so a preview is the only look you get without an editor.
    """
    if isinstance(tag, nbt.Compound):
        return f"{len(tag)} entr{'y' if len(tag) == 1 else 'ies'}"
    if isinstance(tag, nbt.List):
        element = nbt.TAG_NAMES.get(tag.element_id, "?").removeprefix("TAG_")
        return f"{len(tag)} × {element}" if tag else f"empty ({element})"
    if isinstance(tag, (nbt.ByteArray, nbt.IntArray, nbt.LongArray)):
        preview = ", ".join(str(v) for v in tag[:_ARRAY_PREVIEW])
        if len(tag) > _ARRAY_PREVIEW:
            preview += f", … ({len(tag):,} total)"
        return f"[{preview}]" if tag else "[]"
    if isinstance(tag, nbt.String):
        return f'"{tag}"'
    return str(tag)


def children_of(tag):
    """``(label, child)`` pairs, or [] for a leaf.

    List items are labelled by index because that is genuinely how they are addressed —
    they have no names — and §6.4 already flags that `MyList[3]` is a fragile handle when
    a list can be reordered. Showing the index is honest about that rather than hiding it.
    """
    if isinstance(tag, nbt.Compound):
        return list(tag.items())
    if isinstance(tag, nbt.List):
        return [(f"[{index}]", item) for index, item in enumerate(tag)]
    return []


def is_leaf(tag) -> bool:
    """Editable as a single value? Compounds and lists are structure, not values."""
    return not isinstance(tag, (nbt.Compound, nbt.List))


class _ValueOnlyDelegate(QStyledItemDelegate):
    """Editing is offered on the Value column and nowhere else.

    `Qt.ItemIsEditable` is a property of the whole row, so without this a double-click on
    the Name or Type column opens a box that the change handler then silently refuses —
    an edit box that accepts nothing is worse than no edit box.
    """

    def createEditor(self, parent, option, index):
        if index.column() != _VALUE_COLUMN:
            return None
        return super().createEditor(parent, option, index)


class NbtViewerTab(QWidget):
    """A tree over one NBT file, with typed value editing."""

    status = Signal(str)
    save_requested = Signal(bytes)     # serialised file, for whoever owns the path
    dirty_changed = Signal(bool)

    def __init__(self, data: bytes, label: str, *, read_only_reason=None, parent=None):
        super().__init__(parent)
        self.label = label
        self.root_name = ""
        self.root = None
        self.compression = "none"
        self._error = None
        # Why this file can't be written, or None. A jar member is the case that matters:
        # it is read-only permanently, and the answer is to save an override (§6.5), so the
        # banner says that rather than just refusing.
        self.read_only_reason = read_only_reason
        self._dirty = False
        # Where each row's tag LIVES: (container, key). Tags are immutable for every scalar
        # type, so an edit cannot mutate the tag in place — it has to assign back into the
        # compound or list that holds it, and the row is the only thing that knows which.
        self._slots = []
        self._applying = False       # guards the itemChanged handler against its own writes
        # Tags are held HERE and referenced from rows by index. Storing the tag itself in
        # Qt item data silently converts it: a `List` comes back a plain `list` and a
        # `Compound` a plain `dict`, losing both the NBT type and, for a list, its element
        # type — so the row would report "1 × Compound" and then expand to nothing.
        # Integers survive the round trip untouched.
        self._tags = []
        try:
            self.root_name, self.root, self.compression = nbt.loads(data)
        except Exception as e:                    # not NBT, truncated, or empty
            self._error = str(e)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 6, 8, 6)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter by name or value…")
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
        layout.addWidget(bar)

        self._tree = PanelTree()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Name", "Type", "Value"])
        self._tree.setHeaderHidden(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self._tree.setColumnWidth(0, 260)
        self._tree.itemExpanded.connect(self._on_expanded)
        self._tree.itemChanged.connect(self._on_item_changed)
        # Double-click lands on the Value column rather than editing whichever column was
        # clicked: names and types are not editable in this slice, and an edit box that
        # appears and then refuses everything is worse than no edit box.
        self._tree.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.EditKeyPressed)
        self._delegate = _ValueOnlyDelegate(self._tree)      # held: Qt won't own it
        self._tree.setItemDelegate(self._delegate)
        layout.addWidget(self._tree)

        # Parented to the tab, so it only fires while this tab has focus — Monaco handles
        # its own Ctrl+S on the JS side and the two must not race.
        self._save_shortcut = QShortcut(QKeySequence.Save, self)
        self._save_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._save_shortcut.activated.connect(self.request_save)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setContentsMargins(10, 6, 10, 6)
        self._banner.hide()
        layout.addWidget(self._banner)

        if self._error is not None:
            # A `.dat` that isn't NBT is a real thing to double-click: mods use the
            # extension for their own formats. Saying so beats an empty tree, which reads
            # as a broken viewer rather than an unsupported file.
            self._banner.setText(
                f"{label} isn't readable as NBT — {self._error}. "
                f"Some mods use the .dat extension for their own binary formats.")
            self._banner.setStyleSheet(
                f"background: {style.BG_CHROME}; color: {style.WARNING}; font-size: 11px;")
            self._banner.show()
            self._summary.setText("not NBT")
            return

        if read_only_reason:
            self._banner.setText(read_only_reason)
            self._banner.setStyleSheet(
                f"background: {style.BG_CHROME}; color: {style.TEXT_MUTED}; font-size: 11px;")
            self._banner.show()

        self._summary.setText(
            f"{len(self.root)} top-level · {self.compression} compressed"
            if self.compression != "none" else f"{len(self.root)} top-level · uncompressed")
        self._repopulate()

    @property
    def ok(self) -> bool:
        return self._error is None

    @property
    def editable(self) -> bool:
        return self._error is None and not self.read_only_reason

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    # --- building ----------------------------------------------------------

    def _repopulate(self):
        if self._error is not None:
            return
        self._applying = True
        try:
            self._tree.clear()
            self._tags.clear()
            self._slots.clear()
            needle = self._filter.text().strip().lower()
            if needle:
                self._show_matches(needle)
                return
            for name, child in children_of(self.root):
                self._add_row(self._tree.invisibleRootItem(), name, child, name,
                              self.root, name)
        finally:
            self._applying = False

    def _add_row(self, parent, label, tag, path, container, key):
        item = QTreeWidgetItem([str(label), type_name(tag), describe_value(tag)])
        self._tags.append(tag)
        self._slots.append((container, key))
        item.setData(0, _ROLE_TAG, len(self._tags) - 1)
        item.setData(0, _ROLE_PATH, path)
        item.setForeground(1, style.qt_colour(style.TEXT_FAINT))
        if children_of(tag):
            item.setData(0, _ROLE_LOADED, False)
            item.addChild(QTreeWidgetItem(["…"]))     # so it shows an expand arrow
        else:
            item.setForeground(2, style.qt_colour(style.TEXT_MUTED))
            if self.editable and is_leaf(tag):
                item.setFlags(item.flags() | Qt.ItemIsEditable)
        parent.addChild(item) if parent is not self._tree.invisibleRootItem() \
            else self._tree.addTopLevelItem(item)
        return item

    def _tag_of(self, item):
        """The real Python tag behind a row (see the note in __init__)."""
        index = item.data(0, _ROLE_TAG)
        return self._tags[index] if isinstance(index, int) else None

    def _on_expanded(self, item):
        if item.data(0, _ROLE_LOADED) is not False:
            return
        self._applying = True
        try:
            item.takeChildren()                        # drop the placeholder
            item.setData(0, _ROLE_LOADED, True)
            tag = self._tag_of(item)
            path = item.data(0, _ROLE_PATH) or ""
            for index, (label, child) in enumerate(children_of(tag)):
                joined = f"{path}{label}" if str(label).startswith("[") else f"{path}.{label}"
                # A list is addressed positionally and a compound by name — the same
                # distinction `children_of` renders as `[3]` versus `Count`.
                key = index if isinstance(tag, nbt.List) else label
                self._add_row(item, label, child, joined, tag, key)
        finally:
            self._applying = False

    def _show_matches(self, needle):
        """Filter results are shown FLAT, as full paths.

        A filtered tree would have to materialise every ancestor of every hit, which on a
        337,000-node file is most of the tree — the thing lazy loading exists to avoid. A
        list of paths is also just a better answer to "where is this": you can read where
        it lives without expanding anything.
        """
        matches = []
        for path, tag, container, key in self._walk(self.root, ""):
            if len(matches) >= _MAX_MATCHES:
                break
            if needle in path.lower() or needle in describe_value(tag).lower():
                matches.append((path, tag, container, key))
        for path, tag, container, key in matches:
            item = QTreeWidgetItem([path, type_name(tag), describe_value(tag)])
            self._tags.append(tag)
            self._slots.append((container, key))
            item.setData(0, _ROLE_TAG, len(self._tags) - 1)
            item.setData(0, _ROLE_PATH, path)
            item.setForeground(1, style.qt_colour(style.TEXT_FAINT))
            # Editable here too: finding a value by filtering and then having to clear the
            # filter and hunt for it in the tree would make the filter useless for the
            # thing people filter *for*.
            if self.editable and is_leaf(tag):
                item.setFlags(item.flags() | Qt.ItemIsEditable)
            self._tree.addTopLevelItem(item)
        capped = " (showing the first 500)" if len(matches) >= _MAX_MATCHES else ""
        self.status.emit(f"{len(matches):,} matching tags{capped}")

    def _walk(self, tag, path):
        for index, (label, child) in enumerate(children_of(tag)):
            joined = f"{path}{label}" if str(label).startswith("[") else (
                f"{path}.{label}" if path else str(label))
            key = index if isinstance(tag, nbt.List) else label
            yield joined, child, tag, key
            yield from self._walk(child, joined)

    # --- editing -----------------------------------------------------------

    def _on_item_changed(self, item, column):
        """Apply a typed edit, or put the old text back and say why it was refused."""
        if self._applying or column != _VALUE_COLUMN:
            return
        index = item.data(0, _ROLE_TAG)
        if not isinstance(index, int):
            return
        tag = self._tags[index]
        container, key = self._slots[index]
        typed = item.text(_VALUE_COLUMN)
        # Strings render quoted, so accept the quotes back rather than making them part of
        # the value — otherwise every round trip through the cell grows two characters.
        if isinstance(tag, nbt.String) and len(typed) >= 2 \
                and typed.startswith('"') and typed.endswith('"'):
            typed = typed[1:-1]

        try:
            new_tag = nbt.retype(tag, typed)
        except ValueError as e:
            self._revert(item, tag)
            self.status.emit(f"Not changed — {e}")
            return

        if new_tag == tag and type(new_tag) is type(tag):
            self._revert(item, tag)          # re-render, but nothing actually moved
            return

        try:
            container[key] = new_tag
        except (KeyError, IndexError, TypeError) as e:
            # The tree was rebuilt under this row, or the container no longer has the key.
            self._revert(item, tag)
            self.status.emit(f"Not changed — {e}")
            return

        self._tags[index] = new_tag
        self._applying = True
        try:
            item.setText(_TYPE_COLUMN, type_name(new_tag))
            item.setText(_VALUE_COLUMN, describe_value(new_tag))
        finally:
            self._applying = False
        self._set_dirty(True)
        self.status.emit(f"{item.data(0, _ROLE_PATH)} = {describe_value(new_tag)}")

    def _revert(self, item, tag):
        self._applying = True
        try:
            item.setText(_VALUE_COLUMN, describe_value(tag))
        finally:
            self._applying = False

    def _set_dirty(self, dirty: bool):
        if dirty == self._dirty:
            return
        self._dirty = dirty
        self.dirty_changed.emit(dirty)

    # --- saving ------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """The whole file, re-serialised in the compression it arrived in.

        The **NBT payload** round-trips byte-identically — that is the reader/writer
        guarantee, verified against thousands of real files. The gzip *container* may not:
        Minecraft's Java writer stamps `XFL=0` in the header and Python's stamps `XFL=2`,
        and the deflate streams of two different compressors won't match anyway. Chasing
        that is a fool's errand; what protects the user is that `request_save` refuses when
        nothing is dirty, so an untouched file is never rewritten in the first place.
        """
        return nbt.dumps(self.root, self.root_name, self.compression)

    def request_save(self):
        """Ctrl+S. Emits the bytes; whoever opened the tab decides where they go."""
        if self._error is not None:
            return
        if self.read_only_reason:
            # §6.3's rule, borrowed: a silent no-op on Ctrl+S reads as a bug, so say why.
            self.status.emit(self.read_only_reason)
            return
        if not self._dirty:
            self.status.emit(f"{self.label} — no changes to save")
            return
        self.save_requested.emit(self.to_bytes())

    def mark_saved(self):
        self._set_dirty(False)

    # --- for callers -------------------------------------------------------

    def selected_path(self):
        item = self._tree.currentItem()
        return item.data(0, _ROLE_PATH) if item is not None else None
