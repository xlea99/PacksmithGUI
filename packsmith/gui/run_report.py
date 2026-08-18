"""The Run Report tab — what a run did, or would do (design 3.3, 4.1).

**One tab type, two producers.** A dry run and a real run describe themselves with the same
change record (`core/staging.classify`), so the same surface reads both and nothing here
needs to know which it got beyond a badge in the header. That is the whole reason the
record was unified before this was built: a report that could only describe one of them
would have made the dry run a second-class citizen of its own feature.

**Why a tab and not the bottom panel.** §4.1 already made this ruling for the packdump:
*"the diff itself opens as its own tab — a real update to a 300-mod pack moves thousands of
entries, and a strip a few rows tall is a place to learn THAT something changed, not to read
WHAT"*. Job Results keeps the "it happened, here is how it went" job; this is where you read
it. Structurally identical problem, same answer.

**The tag table is virtualised and filterable, from day one.** A removal job over
`minecraft:item` stages tens of thousands of cells. A list of labels would die on the first
real job, and *"show me the writes touching quark"* is how anyone would actually audit one.
The filter is a plain substring, deliberately — the query language (§3.2.3) answers a
different question over a different universe, and two boxes that look alike and behave
differently is worse than either.
"""
from PySide6.QtCore import QAbstractTableModel, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QMenu, QPlainTextEdit,
    QScrollArea, QTableView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from packsmith.core.job_runner import describe_summary
from packsmith.gui.shell import icons, style
from packsmith.gui.shell.panels.base import SearchBox

# What each kind of change is called, and what colour says so. `unchanged` is deliberately
# faint rather than hidden — see `staging.classify` for why it is reported at all.
KIND_COLOUR = {
    "added": style.SUCCESS,
    "created": style.SUCCESS,
    "changed": style.ACCENT_EDGE,
    "claimed": style.WARNING,
    "removed": style.ERROR,
    "unchanged": style.TEXT_FAINT,
}

# Tall enough to be worth scrolling, short enough that the sections below stay reachable.
# A section shorter than its content shrinks to fit, so a three-change report is three rows
# rather than three rows and a lot of dark grey.
_MAX_TABLE_HEIGHT = 380
_EMPTY = "—"


def _text(value) -> str:
    """A change's value as one line. `None` is absence, which is not the string "None"."""
    if value is None:
        return _EMPTY
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return text if len(text) <= 120 else text[:119] + "…"


def _side_text(side) -> str:
    return _EMPTY if side is None else _text(side["value"])


def _owner_text(side) -> str:
    if side is None:
        return _EMPTY
    if side["owner"] == "action":
        return side["action_ref"] or "action"
    return side["owner"] or _EMPTY


def _size(side) -> int:
    return 0 if side is None or side["value"] is None else len(side["value"])


class ChangeModel(QAbstractTableModel):
    """A table over change records. Virtualised, because the tag list can be enormous.

    Columns are ``(label, extractor)`` pairs so one model serves every engine — the record
    shape is uniform and only the interesting fields differ.
    """

    def __init__(self, changes, columns, parent=None):
        super().__init__(parent)
        self._columns = columns
        self._all = list(changes)
        self._rows = self._all

    # --- Qt ---------------------------------------------------------------

    def rowCount(self, parent=None):
        return 0 if (parent is not None and parent.isValid()) else len(self._rows)

    def columnCount(self, parent=None):
        return len(self._columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self._columns[section][0]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        change = self._rows[index.row()]
        if role == Qt.DisplayRole:
            return self._columns[index.column()][1](change)
        if role == Qt.ForegroundRole:
            # The whole row carries its kind's colour. Colouring only the Kind cell would
            # make the classification something you look up rather than something you see.
            return style.qt_colour(KIND_COLOUR.get(change["kind"], style.TEXT))
        if role == Qt.ToolTipRole:
            return self._columns[index.column()][1](change)
        return None

    # --- filtering ---------------------------------------------------------

    def set_filter(self, needle: str):
        needle = (needle or "").strip().lower()
        self.beginResetModel()
        if not needle:
            self._rows = self._all
        else:
            self._rows = [c for c in self._all
                          if any(needle in str(column[1](c)).lower()
                                 for column in self._columns)]
        self.endResetModel()

    def at(self, row: int):
        """The change behind a visible row — which is not `self._all[row]` once a filter is
        on, and reaching for the unfiltered list is how a double-click opens the wrong
        file."""
        return self._rows[row] if 0 <= row < len(self._rows) else None

    @property
    def total(self) -> int:
        return len(self._all)

    @property
    def shown(self) -> int:
        return len(self._rows)


TAG_COLUMNS = [
    ("Entry", lambda c: c["entry_id"]),
    ("Tag", lambda c: c["tag"]),
    ("", lambda c: c["kind"]),
    ("Before", lambda c: _side_text(c["before"])),
    ("After", lambda c: _side_text(c["after"])),
    ("Owner", lambda c: _owner_text(c["after"])),
]

FILE_COLUMNS = [
    ("Path", lambda c: c["path"]),
    ("", lambda c: c["kind"]),
    ("Size", lambda c: _size_delta(c)),
    ("Owner", lambda c: _owner_text(c["after"])),
]


def _size_delta(change) -> str:
    before, after = _size(change["before"]), _size(change["after"])
    if change["before"] is None:
        return f"{after:,} B"
    sign = "+" if after >= before else ""
    return f"{before:,} → {after:,} B  ({sign}{after - before:,})"


class RunReportTab(QWidget):
    """Everything one run did, in one place."""

    status = Signal(str)
    diff_requested = Signal(object)     # a file change record
    rollback_requested = Signal(int)    # step_runs id

    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.report = report
        self.key = report.key

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

        self._build_heading()
        self._build_steps()
        self._build_tags()
        self._build_blueprints()
        self._build_files()
        self._build_log()
        self._body.addStretch(1)

    # --- heading ------------------------------------------------------------

    def _build_heading(self):
        title = QLabel(self.report.job_name)
        title.setStyleSheet(f"color: {style.TEXT}; font-size: 20px; font-weight: bold;")
        self._body.addWidget(title)

        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 4, 0, 0)
        line.setSpacing(6)
        # A dry run says so first, and in the accent colour, because every number below it
        # is hypothetical. Reading a report as a record of what happened when it is a
        # forecast is the one misunderstanding this page must not allow.
        if self.report.dry_run:
            line.addWidget(_chip("DRY RUN — nothing was written", strong=True))
        line.addWidget(_chip(self.report.status,
                             colour=style.ERROR if self.report.status != "success"
                             else style.SUCCESS))
        if self.report.finished_at:
            line.addWidget(_chip(self.report.finished_at))
        line.addStretch()
        self._body.addWidget(row)

        verb = "would change" if self.report.dry_run else "changed"
        blurb = QLabel(f"{verb} {describe_summary(self.report.summary())}")
        blurb.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 12px;"
                            f" padding-top: 8px;")
        self._body.addWidget(blurb)

    def _build_steps(self):
        self._section_title("Steps")
        tree = QTreeWidget()
        tree.setColumnCount(4)
        tree.setHeaderLabels(["#", "Action", "Status", "Changes"])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setSelectionMode(QAbstractItemView.NoSelection)
        tree.setStyleSheet(style.TABLE_QSS)
        tree.header().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        tree.setContextMenuPolicy(Qt.CustomContextMenu)
        tree.customContextMenuRequested.connect(
            lambda pos, t=tree: self._step_menu(t, pos))

        for position, step in enumerate(self.report.steps, start=1):
            counts = describe_summary(step.summary)
            item = _row([str(position), step.action_ref, step.status, counts])
            item.setData(0, Qt.UserRole, step.run_id if step.can_roll_back else None)
            colour = style.ERROR if step.status != "success" else style.TEXT
            item.setForeground(2, style.qt_colour(colour))
            if step.reason:
                item.setText(3, step.reason)
                item.setForeground(3, style.qt_colour(style.ERROR))
            item.setToolTip(1, step.reason or step.action_ref)
            tree.addTopLevelItem(item)

        if self.report.not_run:
            note = _row(["", f"{self.report.not_run} step(s) never reached", "", ""])
            note.setForeground(1, style.qt_colour(style.TEXT_FAINT))
            tree.addTopLevelItem(note)

        for column in range(3):
            tree.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        _fit(tree, len(self.report.steps) + bool(self.report.not_run))
        self._body.addWidget(tree)

    def _step_menu(self, tree, pos):
        """Rollback, offered from the report rather than only from the bottom panel.

        Job Results has had "Roll back this step" all along, and the only thing describing
        what you were about to undo was a one-line summary. Reversing something you cannot
        see is the gap this whole tab was built to close, so the action belongs here — with
        the change list on the screen behind the menu.
        """
        item = tree.itemAt(pos)
        run_id = item.data(0, Qt.UserRole) if item is not None else None
        if run_id is None:
            return
        menu = QMenu(tree)
        menu.addAction("Roll back this step…",
                       lambda: self.rollback_requested.emit(run_id))
        menu.exec(tree.viewport().mapToGlobal(pos))

    # --- the three engines ---------------------------------------------------

    def _build_tags(self):
        changes = self.report.by_engine("tag")
        if not changes:
            return
        self._section_title(f"Tags · {len(changes):,}")
        self._body.addWidget(_change_table(changes, TAG_COLUMNS, filterable=True))

    def _build_files(self):
        changes = self.report.by_engine("file")
        if not changes:
            return
        self._section_title(f"Files · {len(changes):,}")
        table = _change_table(changes, FILE_COLUMNS, stretch_column=0)
        view = table.findChild(QTableView)
        # Double-click rather than a button per row: a Show Differences button in every row
        # of a hundred-file run is a column of buttons, and the gesture for "open this
        # thing" is already double-click everywhere else in the app.
        view.setToolTip("Double-click a file to see what changed")
        view.doubleClicked.connect(
            lambda index: self.diff_requested.emit(view.model().at(index.row())))
        self._body.addWidget(table)

    def _build_blueprints(self):
        """Grouped by instance, headlined by gaps closed.

        §3.2.2 makes the point of a blueprint "empty slots = missing content", so
        `7/10 → 10/10` is the answer to why the job was run. Eight flat rows saying
        "granite gained a slot" is the same information with the meaning taken out.
        """
        changes = self.report.by_engine("blueprint")
        if not changes:
            return
        self._section_title(f"Blueprints · {len(changes):,}")

        tree = QTreeWidget()
        tree.setColumnCount(4)
        tree.setHeaderLabels(["Instance / slot", "", "Before", "After"])
        tree.setAlternatingRowColors(True)
        tree.setSelectionMode(QAbstractItemView.NoSelection)
        tree.setStyleSheet(style.TABLE_QSS)
        tree.header().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        icons.follow_expansion(tree)

        rows = 0
        for (blueprint, instance), group in self.report.blueprint_groups():
            parent = _row([f"{blueprint}:{instance}", "", "", ""])
            parent.setIcon(0, icons.ui_icon("instance", colour=style.TEXT))
            parent.setText(1, _instance_headline(group))
            parent.setForeground(1, style.qt_colour(style.TEXT_MUTED))
            tree.addTopLevelItem(parent)
            rows += 1

            for change in group:
                if not change["slot"]:
                    continue
                child = _row([change["slot"], change["kind"],
                              _side_text(change["before"]),
                              _side_text(change["after"])])
                colour = style.qt_colour(KIND_COLOUR.get(change["kind"], style.TEXT))
                for column in range(4):
                    child.setForeground(column, colour)
                parent.addChild(child)
                rows += 1
            parent.setExpanded(True)

        # Column 0 stretches and the rest size to content — the reverse of the Steps tree
        # above, and for the reason the Registry panel documents: sizing a tree's first
        # column to its contents counts the indent and the icon, so nested slot paths
        # elide to `polishe…` while the value columns sit half empty.
        tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 4):
            tree.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        _fit(tree, rows)
        self._body.addWidget(tree)

    # --- layout helpers ------------------------------------------------------

    def _build_log(self):
        """Everything the run said, kept with the run that said it.

        The same lines stream into the Logs strip while a job runs (§4.1), but that is a
        live tail shared with the whole application — it scrolls away, it mixes with
        whatever else logged since, and it is gone next session. A run's own output belongs
        to the run: this is the only place it survives, because `step_runs.log_output`
        persists it and `reports.from_history` reads it back.

        Last rather than first, deliberately. The counted sections above answer *what
        happened*; the log answers *why*, which is the question you only have once the
        first one has surprised you. Fixed height and scrollable for the same reason the
        change tables are capped — a chatty action must not push everything else off the
        page.
        """
        lines = self.report.log_lines()
        if not lines:
            return
        self._section_title(f"Log · {len(lines):,}")
        view = QPlainTextEdit()
        view.setReadOnly(True)
        # Undo history on a read-only view of a finished run is pure memory, and the run
        # can be tens of thousands of lines on a real removal job.
        view.setUndoRedoEnabled(False)
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        view.setPlainText("\n".join(f"[{level}] {message}" for level, message in lines))
        view.setFixedHeight(_MAX_TABLE_HEIGHT)
        view.setStyleSheet(
            f"QPlainTextEdit {{ background: {style.BG_PANEL}; color: {style.TEXT_MUTED};"
            f" border: 1px solid {style.BORDER}; border-radius: 3px;"
            f" font-family: {style.MONO_FAMILY}; font-size: 12px; padding: 6px;"
            f" margin-top: 8px; }}")
        self._body.addWidget(view)

    def _section_title(self, text):
        label = QLabel(text.upper())
        label.setStyleSheet(
            f"color: {style.TEXT_FAINT}; font-size: 11px; font-weight: bold;"
            f" letter-spacing: 1px; padding: 22px 0 6px 0;"
            f" border-bottom: 1px solid {style.BORDER};")
        self._body.addWidget(label)

    def title(self) -> str:
        prefix = "Preview" if self.report.dry_run else "Run"
        return f"{prefix}: {self.report.job_name}"

    def tab_icon(self):
        return icons.ui_icon("dry_run" if self.report.dry_run else "play",
                             colour=style.TEXT)


def _instance_headline(group) -> str:
    """What happened to one instance, counted by KIND rather than by "has an after".

    Counting anything with a value as "+N bound" was wrong twice over: a `claimed` binding
    was already bound and only changed hands, and a `changed` one was rebound rather than
    filled. §3.2.2 makes filling gaps the number that means something, so it has to count
    gaps actually filled.
    """
    counts = {}
    created = False
    for change in group:
        if change["slot"] is None:
            created = created or change["kind"] == "created"
            continue
        counts[change["kind"]] = counts.get(change["kind"], 0) + 1

    parts = ["new instance"] if created else []
    for kind, label in (("added", "+{} bound"), ("changed", "{} rebound"),
                        ("removed", "−{} unbound"), ("claimed", "{} claimed")):
        if counts.get(kind):
            parts.append(label.format(counts[kind]))
    return "  ".join(parts)


def _chip(text: str, *, colour=None, strong: bool = False) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"""
        QLabel {{
            background: {style.BG_CHROME}; color: {colour or style.TEXT_MUTED};
            border: 1px solid {style.ACCENT_EDGE if strong else style.BORDER};
            border-radius: 3px; padding: 1px 6px; font-size: 11px;
        }}
    """)
    return label


def _row(texts) -> QTreeWidgetItem:
    """A tree row at the same height as a table row.

    A `QTreeWidget` sizes rows to its font, not to the vertical header's default section
    size — so left alone the Steps tree renders at ~16px beside 28px tables, and the rows
    read as clipped rather than as compact.
    """
    item = QTreeWidgetItem(list(texts))
    item.setSizeHint(0, QSize(0, style.ROW_HEIGHT))
    return item


def _fit(widget, rows: int):
    """Size a table to its contents, up to a cap.

    A fixed height would leave a three-change report showing three rows and 350px of empty
    grid — which reads as "something failed to load" rather than as "there were three".
    The padding covers the header band and the frame; without it a table that exactly fits
    grows a scrollbar for its last four pixels.
    """
    height = min(_MAX_TABLE_HEIGHT, 44 + max(1, rows) * style.ROW_HEIGHT)
    widget.setFixedHeight(height)


def _change_table(changes, columns, *, filterable: bool = False,
                  stretch_column: int = 0) -> QWidget:
    holder = QWidget()
    column = QVBoxLayout(holder)
    column.setContentsMargins(0, 8, 0, 0)
    column.setSpacing(6)

    view = QTableView()
    model = ChangeModel(changes, columns, parent=view)
    view.setModel(model)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(style.ROW_HEIGHT)
    view.setEditTriggers(QAbstractItemView.NoEditTriggers)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.SingleSelection)
    view.setShowGrid(False)
    view.setAlternatingRowColors(True)
    view.setWordWrap(False)
    view.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    view.setStyleSheet(style.TABLE_QSS + """
        QTableView::item { padding: 0 8px; }
    """)
    header = view.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    header.setHighlightSections(False)
    header.setStretchLastSection(False)
    for index in range(len(columns)):
        header.setSectionResizeMode(index, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(stretch_column, QHeaderView.Stretch)

    if filterable:
        search = SearchBox("changes")
        count = QLabel()
        count.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")

        def refilter():
            model.set_filter(search.text())
            count.setText(f"{model.shown:,} of {model.total:,}"
                          if model.shown != model.total else f"{model.total:,}")

        search.textChanged.connect(refilter)
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        line.addWidget(search, 1)
        line.addWidget(count)
        column.addWidget(row)
        refilter()

    _fit(view, len(changes))
    column.addWidget(view)
    return holder
