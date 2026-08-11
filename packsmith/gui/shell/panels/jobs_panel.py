"""The Jobs panel (design 4.1 / 3.3.2) — the user's primary automation surface.

"Pinned jobs at the top with play buttons for one-click execution; all jobs (pinned and
unpinned) listed below, searchable. Right-click to edit, create, pin/unpin, export.
Double-click opens a job editor tab."

The ▶ column is a one-click run. Pinning doesn't change what a job can do — it just lifts
it into the top section, which is what makes "the three jobs I actually run" reachable in
a list that will eventually hold dozens.

**Export is not built, and it isn't a menu-item's worth of work.** §3.2.1 makes job step
bindings identity-by-**id**, which is correct in-profile and meaningless outside one: a job
exported as-is would carry integer ids pointing at another profile's tags. A real export
has to translate id→name on the way out and name→id on the way in, exactly the portability
split saved View queries already use (they bind by name for this reason). Until that
translation exists, an "Export" item would produce files that import wrong — worse than
absent.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QPushButton, QHBoxLayout, QWidget, QLineEdit, QMenu,
    QAbstractItemView,
)

from packsmith.gui.shell import style
from packsmith.gui.shell.tree import PanelTree
from packsmith.gui.shell.panels.base import Panel

_ROLE_JOB = Qt.UserRole


class JobsPanel(Panel):

    job_activated = Signal(object)      # Job — open its editor tab
    run_requested = Signal(object)      # Job — execute now
    new_job_requested = Signal()
    rename_requested = Signal(object)
    delete_requested = Signal(object)
    pin_toggled = Signal(object)

    def __init__(self, jobs=None, parent=None):
        super().__init__("Jobs", parent)
        self._jobs = list(jobs or [])

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(6, 4, 6, 4)
        new_btn = QPushButton("＋  New Job")
        new_btn.setFixedHeight(22)
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER}; font-size: 11px; padding: 1px 8px;
            }}
            QPushButton:hover {{ color: {style.TEXT}; border-color: {style.ACCENT_EDGE}; }}
        """)
        new_btn.clicked.connect(self.new_job_requested)
        bar_lay.addWidget(new_btn)
        bar_lay.addStretch()
        self.body().addWidget(bar)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search jobs…")
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                border: 1px solid {style.BORDER}; margin: 0 6px 4px 6px;
                padding: 3px 6px; font-size: 11px;
            }}
        """)
        self._search.textChanged.connect(self.refresh)
        self.body().addWidget(self._search)

        self._tree = PanelTree()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(10)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setStyleSheet(style.LIST_QSS)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.itemClicked.connect(self._on_clicked)
        self._tree.itemDoubleClicked.connect(self._on_double_clicked)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self.body().addWidget(self._tree)

        self.refresh()

    def set_jobs(self, jobs):
        self._jobs = list(jobs)
        self.refresh()

    def refresh(self, *_):
        needle = self._search.text().strip().lower()
        matching = [j for j in self._jobs if not needle or needle in j.name.lower()]
        self._tree.clear()

        pinned = [j for j in matching if j.pinned]
        if pinned:
            self._add_section("PINNED", pinned)
        self._add_section("ALL JOBS", matching)
        self._tree.setColumnWidth(0, 22)

    def _add_section(self, title, jobs):
        header = QTreeWidgetItem(["", title])
        header.setFlags(Qt.ItemIsEnabled)
        header.setForeground(1, Qt.gray)
        self._tree.addTopLevelItem(header)
        header.setExpanded(True)
        for job in jobs:
            item = QTreeWidgetItem(["▶", job.name])
            item.setData(0, _ROLE_JOB, job)
            item.setToolTip(0, f"Run '{job.name}' now")
            steps = len(job.steps)
            item.setToolTip(1, f"{job.name} — {steps} step{'s' if steps != 1 else ''}; "
                               f"double-click to edit")
            header.addChild(item)

    def _job_at(self, item):
        return item.data(0, _ROLE_JOB) if item is not None else None

    def _on_clicked(self, item, column):
        job = self._job_at(item)
        if job is not None and column == 0:      # the ▶ cell
            self.run_requested.emit(job)

    def _on_double_clicked(self, item, column):
        job = self._job_at(item)
        if job is not None and column != 0:
            self.job_activated.emit(job)

    def _on_context_menu(self, pos):
        self._menu_for(self._tree.itemAt(pos)).exec(self._tree.mapToGlobal(pos))

    def _menu_for(self, item):
        job = self._job_at(item)
        menu = QMenu(self)
        if job is None:
            # Empty space still gets a menu: "make a new one" is the action you want most
            # when the list is empty, and an empty list is the one place with nothing to
            # right-click.
            menu.addAction("New Job…", lambda: self.new_job_requested.emit())
            return menu
        menu.addAction("Run now", lambda: self.run_requested.emit(job))
        menu.addAction("Edit steps…", lambda: self.job_activated.emit(job))
        menu.addSeparator()
        menu.addAction("Unpin" if job.pinned else "Pin",
                       lambda: self.pin_toggled.emit(job))
        menu.addAction("Rename…", lambda: self.rename_requested.emit(job))
        menu.addAction("Delete", lambda: self.delete_requested.emit(job))
        menu.addSeparator()
        menu.addAction("New Job…", lambda: self.new_job_requested.emit())
        return menu
