"""One action's reference page (design 3.3.1, 7.1).

**What an action IS, in one place.** Double-clicking an action used to open the `.star`
file it points at — which answers "what does it do" only under a one-action-per-file
discipline nobody keeps, and never answers the questions you actually arrive with:
what does it need bound, what can I configure, and what breaks if I change it.

Those answers are split across two artifacts by design. The **manifest** holds the
contract — id, entry point, mapping slots, configuration — and the **source** holds the
body. §3.3.1 keeps them apart on purpose, because the manifest is language-agnostic and
durable while the body is Starlark today. This page is where the two are shown together
without either being edited.

**Read-only, deliberately.** An action is a declaration; editing it means editing
`manifest.toml`, which has its own surface. A page you can half-edit would raise the
question of what Save means for a downloaded package, which §3.3.1 answers with
provenance rather than with a disabled button.

The one thing here that is neither manifest nor source: **which of your jobs use this**.
That is the question with a real consequence — it is the blast radius of touching the
action — and it can only be answered by looking at Layer 2 from Layer 3's page.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from packsmith.core.bindings import binding_name
from packsmith.core.packages import function_source
from packsmith.gui.shell import icons, style

# Tall enough to hold a short action whole, and capped so a long one scrolls inside the
# block rather than setting the length of the page. The body is the only section with no
# natural size, so left to grow it would decide how far the scrollbar has to travel to
# reach anything else — even sitting last, where nothing is pushed off the bottom.
_CODE_HEIGHT = 260
# Same reasoning, applied to the other end of the page: one action bound into a dozen jobs
# is a long table, and a page that grows without bound is one you scroll rather than read.
_TABLE_HEIGHT = 220

_JOB_ROLE = Qt.UserRole


def table_job_id(item):
    """The job a Used-by row belongs to, read off whichever cell was clicked."""
    table = item.tableWidget()
    first = table.item(item.row(), 0) if table is not None else None
    return first.data(_JOB_ROLE) if first is not None else None


def _mono() -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.Monospace)
    font.setPixelSize(12)
    return font


class ActionPageTab(QWidget):
    """A reference page for one declared action."""

    status = Signal(str)
    document_activated = Signal(str)   # "<package>/<file>" — open the source in the editor
    job_activated = Signal(int)        # job id — open its editor

    def __init__(self, manifest, package, job_store, tag_store=None,
                 blueprint_store=None, parent=None):
        super().__init__(parent)
        self.ref = manifest.ref
        self._manifest = manifest
        self._package = package
        self._jobs = job_store
        # Needed only to turn a stored binding back into a name. A binding holds an **id**
        # (§3.2.1), so without the stores this page could report that a slot is bound but
        # not to what — which is most of what you came to find out.
        self._tags = tag_store
        self._blueprints = blueprint_store

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(f"QScrollArea {{ background: {style.BG_DEEP}; border: none; }}")
        outer.addWidget(scroll)

        body = QWidget()
        body.setStyleSheet(f"background: {style.BG_DEEP};")
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(24, 20, 24, 24)
        self._body.setSpacing(0)
        scroll.setWidget(body)

        # Contract first, implementation last. What an action *is* — what it binds, what
        # you can configure, who depends on it — is what you opened the page to read, and
        # the body is the one section you can answer every one of those questions without.
        # It is also the only section with no natural length, so it goes where a long one
        # costs nothing.
        self._build_heading()
        self._build_identity()
        self._build_mappings()
        self._build_config()
        self._build_used_by()
        self._build_source()
        self._body.addStretch(1)

    # --- the pieces --------------------------------------------------------

    def _build_heading(self):
        title = QLabel(self._manifest.name or self._manifest.action_id)
        title.setStyleSheet(
            f"color: {style.TEXT}; font-size: 20px; font-weight: bold;")
        self._body.addWidget(title)

        ref = QLabel(self.ref)
        ref.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ref.setStyleSheet(
            f"color: {style.ACCENT_EDGE}; font-size: 12px; padding-top: 2px;")
        ref.setToolTip("How a job step names this action — select to copy")
        self._body.addWidget(ref)

        if self._manifest.description:
            blurb = QLabel(self._manifest.description)
            blurb.setWordWrap(True)
            blurb.setStyleSheet(
                f"color: {style.TEXT_MUTED}; font-size: 12px; padding-top: 8px;")
            self._body.addWidget(blurb)

    def _build_identity(self):
        grid = self._section("Declaration", columns=2)
        package = self._package
        version = f"  {package.version}" if package and package.version else ""
        author = f"  ·  by {package.author}" if package and package.author else ""
        provenance = package.provenance if package else "unknown"

        self._field(grid, "Package", f"{self._manifest.package_name}{version}{author}")
        # Provenance is here rather than as a badge because it is the rule that decides
        # whether anything on this page can be changed (§3.3.1): you may edit what you
        # authored, not what you installed.
        self._field(grid, "Provenance",
                    "authored — yours to edit" if provenance == "authored"
                    else "downloaded — read-only")
        self._field(grid, "Id", self._manifest.action_id)

        entry = QWidget()
        row = QHBoxLayout(entry)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        label = QLabel(f"{self._manifest.file}  ·  {self._manifest.function}()")
        label.setStyleSheet(f"color: {style.TEXT}; font-size: 12px;")
        row.addWidget(label)
        open_source = QPushButton("Open source")
        open_source.setCursor(Qt.PointingHandCursor)
        open_source.setFixedHeight(20)
        open_source.setStyleSheet(f"""
            QPushButton {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER}; font-size: 11px; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {style.TEXT};
                                 border-color: {style.ACCENT_EDGE}; }}
        """)
        open_source.clicked.connect(lambda: self.document_activated.emit(
            f"{self._manifest.package_name}/{self._manifest.file}"))
        row.addWidget(open_source)
        row.addStretch()
        self._widget_field(grid, "Entry point", entry)

    def _build_source(self):
        self._section_title("Source")
        source = None
        if self._package is not None:
            source = function_source(self._package, self._manifest.file,
                                     self._manifest.function)

        block = QPlainTextEdit()
        block.setReadOnly(True)
        block.setFont(_mono())
        block.setLineWrapMode(QPlainTextEdit.NoWrap)
        block.setFixedHeight(_CODE_HEIGHT)
        block.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {style.BG_PANEL}; color: {style.TEXT};
                border: 1px solid {style.BORDER}; padding: 8px;
                selection-background-color: {style.ACCENT};
            }}
        """)
        if source is None:
            # Worth saying loudly. A manifest naming a function its file does not define
            # loads fine and fails at run time, far from the mistake — this is the one
            # place that discrepancy is visible before a job blows up.
            block.setPlainText(
                f"{self._manifest.file} does not define {self._manifest.function}().\n\n"
                f"The manifest declares it, so this action will fail when a job runs it.")
            block.setStyleSheet(block.styleSheet().replace(
                f"color: {style.TEXT};", f"color: {style.ERROR};"))
        else:
            block.setPlainText(source)
        self._body.addWidget(block)

    def _build_mappings(self):
        """One block per slot: what binds here, then what it must look like.

        Run together on one line — `blueprint · read_write · on conflict: skip · needs
        slots: base_block (registry_entry), polished (group), polished.base
        (registry_entry)…` — the facts fight each other. They are different *kinds* of
        fact: a handful of flags, a sentence of prose, and (for blueprints) a nested
        structure that a comma-separated list of dotted paths actively hides. Each gets the
        form that suits it.
        """
        holder = self._stack("Mappings")
        mappings = self._manifest.mappings
        if not mappings:
            holder.addWidget(self._muted(
                "This action binds nothing — it needs no tags, blueprints or packs "
                "from you."))
            return
        for name, slot in sorted(mappings.items()):
            holder.addWidget(self._mapping_block(name, slot))

    def _mapping_block(self, name, slot) -> QWidget:
        block = QFrame()
        block.setStyleSheet(
            f"QFrame {{ border: none; border-left: 2px solid {style.BORDER};"
            f" background: transparent; }}")
        column = QVBoxLayout(block)
        column.setContentsMargins(12, 0, 0, 0)
        column.setSpacing(5)

        chips = [(slot.kind, True)]
        if slot.kind == "tag":
            if slot.tag_type:
                chips.append((slot.tag_type, False))
            if slot.registry_type:
                chips.append((slot.registry_type, False))
        elif slot.kind == "registry_entry" and slot.registry_type:
            chips.append((slot.registry_type, False))
        elif slot.kind == "pack" and slot.pack_kind:
            chips.append((slot.pack_kind, False))
        # A write is the fact worth seeing first — it is what makes ownership, conflict
        # policy and the whole §3.2.1 transfer story apply to this slot at all.
        chips.append((slot.access, slot.access in ("write", "read_write")))
        if slot.conflict_policy:
            chips.append((f"on conflict: {slot.conflict_policy}", True))
        if slot.cardinality != "one":
            chips.append((slot.cardinality, False))
        if not slot.required:
            chips.append(("optional", False))
        column.addWidget(_heading_row(name, chips))

        if slot.description:
            column.addWidget(self._muted(slot.description, italic=False))
        if slot.likely_name:
            column.addWidget(self._muted(f"usually called “{slot.likely_name}”"))
        if slot.requires_values:
            column.addWidget(self._muted(
                "branches on " + ", ".join(f"“{v}”" for v in slot.requires_values)))
        if slot.required_shape:
            column.addWidget(self._muted(
                "any blueprint containing this shape fits — the name is yours:"))
            column.addWidget(_shape_tree(slot.required_shape))
        return block

    def _build_config(self):
        holder = self._stack("Configuration")
        config = self._manifest.config
        if not config:
            holder.addWidget(self._muted("Nothing to configure."))
            return
        for name, param in sorted(config.items()):
            block = QFrame()
            block.setStyleSheet(
                f"QFrame {{ border: none; border-left: 2px solid {style.BORDER};"
                f" background: transparent; }}")
            column = QVBoxLayout(block)
            column.setContentsMargins(12, 0, 0, 0)
            column.setSpacing(5)

            chips = [(param.type, False)]
            if param.required:
                chips.append(("required", True))
            if param.default is not None:
                chips.append((f"default {_as_toml(param.default)}", False))
            column.addWidget(_heading_row(name, chips))
            if param.description:
                column.addWidget(self._muted(param.description, italic=False))
            holder.addWidget(block)

    def _build_used_by(self):
        """Every step running this action — **one row per step**, one column per slot.

        The shape is the point. An action's mappings and configuration are a fixed schema
        (they come from the manifest, so every step has exactly the same set), which makes
        them *columns*, not entries. Reading **down** `(MAP) palette` then tells you at a
        glance that one job bound it to `StoneType` and another to `MyRockKind` — the
        comparison you opened this page to make, and the one §3.3.2's Model B creates by
        putting all per-invocation state on the step rather than on the action.

        Laid out as rows-of-key-value instead, that comparison is spread down the page with
        other steps interleaved between the two cells you want to compare.

        Read-only throughout. Rebinding is the job editor's job, and a table you could edit
        here would be a second surface for the same data with no idea the other exists.
        """
        self._section_title("Used by")
        users = self._users()

        if not users:
            # Not a warning. Plenty of actions sit installed and unused, and one you just
            # declared has no users by definition.
            holder = QWidget()
            grid = QGridLayout(holder)
            grid.setContentsMargins(0, 8, 0, 0)
            self._note(grid, "No job has a step running this action.")
            self._body.addWidget(holder)
            return

        slots = self._slot_columns()
        rows = [(job, position, step)
                for job, steps in users for position, step in steps]

        table = QTableWidget(len(rows), 3 + len(slots))
        table.setHorizontalHeaderLabels(
            ["Job", "Step", "On error"] + [label for label, _, _, _ in slots])
        for offset, (_, _, _, tip) in enumerate(slots):
            header_item = table.horizontalHeaderItem(3 + offset)
            if tip:
                header_item.setToolTip(tip)

        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(style.ROW_HEIGHT)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setFixedHeight(_TABLE_HEIGHT)
        # Qt scrolls sideways a whole COLUMN at a time by default, so one nudge jumps
        # ~150px and the column you were reading leaves the screen. Per-pixel is what makes
        # a wide table readable rather than something you flick between positions.
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        # Cell padding is added HERE rather than in the shared sheet: the registry table
        # insets its text from inside its delegates (§5.1), so putting it in `TABLE_QSS`
        # would inset that one twice.
        table.setStyleSheet(style.TABLE_QSS + """
            QTableView::item { padding: 0 8px; }
        """)
        table.setToolTip("Read-only. Double-click a row to open the job.")

        for index, (job, position, step) in enumerate(rows):
            cells = [(job.name, None),
                     (str(position), style.TEXT_MUTED),
                     _on_error_cell(job, step)]
            cells += [self._slot_cell(kind, name, spec, step)
                      for _label, kind, name, spec in slots]
            for column, (text, colour) in enumerate(cells):
                cell = QTableWidgetItem(text)
                if colour:
                    cell.setForeground(QColor(colour))
                if column == 1:
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                cell.setToolTip(f"{job.name} — step {position}\n{text}")
                if column == 0:
                    cell.setData(_JOB_ROLE, job.id)
                table.setItem(index, column, cell)

        header = table.horizontalHeader()
        header.setHighlightSections(False)
        # Left, like every other table header in the app (§5.1). Qt centres them by default,
        # which floats a heading in the middle of a wide column with nothing under it.
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # Every column sized to its own content and none stretched, so leftover viewport
        # stays empty — the same ruling the registry table came to (§5.1). An action with
        # eight slots is genuinely wider than the page, which is what the horizontal bar is
        # for; stretching would have meant the widest column could never be resized.
        header.setStretchLastSection(False)
        for column in range(table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)

        table.itemDoubleClicked.connect(self._on_row_activated)
        self._body.addWidget(table)

    def _on_row_activated(self, item):
        job_id = table_job_id(item)
        if job_id is not None:
            self.job_activated.emit(job_id)

    # --- what goes in the table --------------------------------------------

    def _slot_columns(self) -> list:
        """``(label, kind, name, spec_or_tip)`` per mapping, then per config parameter.

        Prefixed `(MAP)` / `(CFG)` rather than split into two banded groups, because the two
        are the same *kind* of thing here — something this step supplies — and they differ
        in where the value comes from, which is exactly what a three-letter prefix says
        without spending a whole header row on it.
        """
        columns = []
        for name, slot in sorted(self._manifest.mappings.items()):
            columns.append((f"(MAP) {name}", "map", name,
                            slot.description or _describe_mapping(slot)))
        for name, param in sorted(self._manifest.config.items()):
            columns.append((f"(CFG) {name}", "cfg", name,
                            param.description or param.type))
        return columns

    def _slot_cell(self, kind, name, _tip, step):
        return (self._binding_cell(name, step) if kind == "map"
                else self._config_cell(name, step))

    def _binding_cell(self, name, step):
        slot = self._manifest.mappings[name]
        bound = step.bindings.get(name)
        chosen = bound if isinstance(bound, (list, tuple)) else \
            ([] if bound is None else [bound])
        if not chosen:
            return ("unbound", style.TEXT_FAINT)
        names = [self._name_of(slot, one) for one in chosen]
        # A binding holding an id whose artifact is gone is a real state, not a crash (see
        # `binding_name`) — and it is the single most useful thing this table can tell you,
        # because the job will refuse to run until it is fixed.
        broken = [one for one, shown in zip(chosen, names) if shown is None]
        value = ", ".join(shown for shown in names if shown is not None)
        if broken:
            missing = ", ".join(f"missing (id {one!r})" for one in broken)
            value = f"{value}, {missing}" if value else missing
        return (value, style.ERROR if broken else None)

    def _config_cell(self, name, step):
        """What this step will actually run with.

        A defaulted value is shown and marked rather than left blank. A parameter absent
        from the step's stored config is not unset — it is running on the default, which is
        a fact about this step and not only about the action.
        """
        param = self._manifest.config[name]
        if name in step.config:
            return (_as_toml(step.config[name]), None)
        if param.default is not None:
            return (f"{_as_toml(param.default)}  (default)", style.TEXT_MUTED)
        if param.required:
            return ("unset — required", style.ERROR)
        return ("unset", style.TEXT_FAINT)

    def _name_of(self, slot, stored):
        try:
            return binding_name(slot, stored, tag_store=self._tags,
                                blueprint_store=self._blueprints)
        except Exception:
            # A reference page must not be the thing that fails. Whatever a store raises on
            # a malformed id, "we cannot name this" is the answer the table wants.
            return None

    def _users(self) -> list:
        """Jobs with a step invoking this action, paired with (position, step).

        **Direct steps only.** A job that merely references another job which runs this
        action is not listed, and that is the honest line: §3.3.2's nesting means the outer
        job's connection to this action is its child's, not its own, and following the
        chain would report a job whose step list never mentions it — which is a confusing
        answer to "who uses this".
        """
        if self._jobs is None:
            return []
        out = []
        for job in self._jobs.all():
            steps = [(i + 1, step) for i, step in enumerate(job.steps)
                     if step.is_action and step.action_ref == self.ref]
            if steps:
                out.append((job, steps))
        return out

    # --- layout helpers ----------------------------------------------------

    def _section_title(self, text):
        label = QLabel(text.upper())
        label.setStyleSheet(
            f"color: {style.TEXT_FAINT}; font-size: 11px; font-weight: bold;"
            f" letter-spacing: 1px; padding: 22px 0 6px 0;"
            f" border-bottom: 1px solid {style.BORDER};")
        self._body.addWidget(label)

    def _stack(self, title) -> QVBoxLayout:
        """A section whose contents are blocks stacked down the page, not a key/value grid."""
        self._section_title(title)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 10, 0, 0)
        column.setSpacing(16)
        self._body.addWidget(holder)
        return column

    def _muted(self, text, *, italic=True) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setStyleSheet(
            f"QLabel {{ border: none; color: {style.TEXT_MUTED}; font-size: 12px;"
            f" font-style: {'italic' if italic else 'normal'}; }}")
        return label

    def _section(self, title, *, columns=2) -> QGridLayout:
        self._section_title(title)
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 8, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        if columns == 2:
            grid.setColumnStretch(1, 1)
            grid.setColumnMinimumWidth(0, 110)
        else:
            grid.setColumnStretch(0, 1)
        self._body.addWidget(holder)
        return grid

    def _field(self, grid, name, value, detail=""):
        row = grid.rowCount()
        key = QLabel(name)
        key.setAlignment(Qt.AlignRight | Qt.AlignTop)
        key.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 12px;")
        grid.addWidget(key, row, 0)

        text = value if not detail else f"{value}\n{detail}"
        val = QLabel(text)
        val.setWordWrap(True)
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        val.setStyleSheet(f"color: {style.TEXT}; font-size: 12px;")
        grid.addWidget(val, row, 1)

    def _widget_field(self, grid, name, widget):
        row = grid.rowCount()
        key = QLabel(name)
        key.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        key.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 12px;")
        grid.addWidget(key, row, 0)
        grid.addWidget(widget, row, 1)

    def _note(self, grid, text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {style.TEXT_FAINT}; font-size: 12px; font-style: italic;")
        grid.addWidget(label, grid.rowCount(), 0, 1, grid.columnCount() or 1)

    # --- for the window ----------------------------------------------------

    def title(self) -> str:
        """``"Display Name" (action_id)`` — because the tab bar needs both halves.

        The id alone is what a job step names and what you search for, but `nuke` beside
        `audit` beside `fill` tells you nothing about what they do. The name alone is prose
        and two actions may share one. Quoting the name and bracketing the id keeps them
        visibly separate at a glance, which two space-separated words did not.

        Truncated at 20 characters so a wordy display name cannot push the id — the
        shorter, more identifying half — off the end of the tab.

        A name that IS the id is not repeated: `name` defaults to the id when the manifest
        omits it, and `"nuke" (nuke)` is a tab that looks like a bug.
        """
        name = (self._manifest.name or "").strip()
        if not name or name == self._manifest.action_id:
            return self._manifest.action_id
        if len(name) > 20:
            name = name[:19].rstrip() + "…"
        return f'"{name}" ({self._manifest.action_id})'

    def tab_icon(self):
        return icons.ui_icon("action", colour=style.TEXT)


def _chip(text: str, *, strong: bool = False) -> QLabel:
    """One small pill. Chips rather than `·`-separated words because these are a *set* of
    independent flags, and separators imply a sentence — which invites you to read them in
    order and makes the one you want harder to pick out."""
    label = QLabel(text)
    label.setStyleSheet(f"""
        QLabel {{
            background: {style.BG_CHROME};
            color: {style.TEXT if strong else style.TEXT_MUTED};
            border: 1px solid {style.ACCENT_EDGE if strong else style.BORDER};
            border-radius: 3px; padding: 1px 6px; font-size: 11px;
        }}
    """)
    return label


def _heading_row(name: str, chips) -> QWidget:
    """A slot's name, then its flags as chips."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    label = QLabel(name)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    label.setStyleSheet(
        f"QLabel {{ border: none; color: {style.TEXT}; font-size: 13px;"
        f" font-weight: bold; }}")
    layout.addWidget(label)
    layout.addSpacing(4)
    for text, strong in chips:
        layout.addWidget(_chip(text, strong=strong))
    layout.addStretch(1)
    return row


def _shape_tree(shape) -> QLabel:
    """The structure a blueprint mapping needs, drawn as a tree.

    ``required_shape`` arrives flat — ordered requirements keyed by dotted path — because
    flat is what makes a *mismatch* reportable as one sentence about one slot (see
    `core/shapes.py`). Read as a comma-separated list of paths, though, it is unreadable
    exactly where it matters: `polished.base, polished.stairs, polished.slab,
    polished.wall` repeats the parent four times and never says that `polished` is a group
    with four things in it. The nesting is the fact, so it gets drawn.

    Parents come before their children (`_walk` is depth-first), so one pass rebuilds the
    tree without sorting anything.
    """
    rows = []
    for index, need in enumerate(shape):
        depth = need.path.count(".")
        parent = need.path.rsplit(".", 1)[0] if depth else ""
        # Last among its siblings? Scan forward until something at this depth or shallower
        # turns up; if it shares this parent, there is more to come.
        last = True
        for later in shape[index + 1:]:
            later_depth = later.path.count(".")
            if later_depth > depth:
                continue
            last = not (later_depth == depth
                        and (later.path.rsplit(".", 1)[0] if later_depth else "") == parent)
            break
        rows.append((depth, last, need.path.rsplit(".", 1)[-1], need.describe()))

    # A vertical bar under every ancestor that still has siblings below it — the thing that
    # makes a deep tree readable rather than a staircase of indents.
    lines, open_at = [], {}
    for depth, last, name, describes in rows:
        prefix = "".join("   " if open_at.get(d) is False else "│  " for d in range(depth))
        lines.append((f"{prefix}{'└─ ' if last else '├─ '}{name}", describes))
        open_at[depth] = not last

    width = max(len(label) for label, _ in lines)
    text = "\n".join(f"{label.ljust(width)}   {describes}" for label, describes in lines)

    tree = QLabel(text)
    tree.setFont(_mono())
    tree.setTextInteractionFlags(Qt.TextSelectableByMouse)
    tree.setStyleSheet(f"""
        QLabel {{
            background: {style.BG_PANEL}; color: {style.TEXT};
            border: 1px solid {style.BORDER}; padding: 8px 10px;
        }}
    """)
    return tree


def _on_error_cell(job, step):
    """The policy this step would actually run under (§3.3.2).

    The **effective** value, not the stored one: `on_error` is None on a step that takes
    the job's default, and a blank cell would read as "no policy" when the truth is
    "halt, because the job says so". Inherited is marked and muted so an explicit
    override still stands out — the same treatment a defaulted config value gets.
    """
    policy = job.on_error_for(step)
    if step.on_error:
        return (policy, None)
    return (f"{policy}  (job)", style.TEXT_MUTED)


def _as_toml(value) -> str:
    """A default, spelled the way the manifest spells it.

    Python's `repr` gives `False`, and the manifest this page is describing says `false`.
    A reference that renders a value in a different language from the file it documents
    invites someone to copy it back in and be told the TOML is invalid.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


def _describe_mapping(slot) -> str:
    """One line saying what has to be bound here, and under what rules."""
    bits = [slot.kind]
    if slot.kind == "tag":
        if slot.tag_type:
            bits.append(slot.tag_type)
        if slot.registry_type:
            bits.append(f"on {slot.registry_type}")
    elif slot.kind == "registry_entry":
        bits.append(f"from {slot.registry_type}")
    elif slot.kind == "pack":
        bits.append(slot.pack_kind or "?")
    bits.append(slot.access)
    if slot.conflict_policy:
        # Only meaningful on a write mapping, and mandatory there (§3.3) — so its presence
        # already says "this action writes", and its value says what happens when it writes
        # over something you own.
        bits.append(f"on conflict: {slot.conflict_policy}")
    if slot.cardinality != "one":
        bits.append(slot.cardinality)
    if not slot.required:
        bits.append("optional")
    line = "  ·  ".join(bits)
    if slot.requires_values:
        line += f"\nbranches on: {', '.join(slot.requires_values)}"
    if slot.required_shape:
        # Structural, never nominal (§3.3) — so this lists what the bound schema must
        # CONTAIN, not what it must be called. Any blueprint with these slots fits.
        line += "\nneeds slots: " + ", ".join(
            f"{req.path} ({req.kind})" for req in slot.required_shape)
    if slot.likely_name:
        line += f"\nusually called: {slot.likely_name}"
    return line
