"""Live content for the bottom panel (design 4.1).

Two of §4.1's bottom tabs can be genuinely real on machinery we already have: **Job
Results** reads the ``step_runs`` history the runner records, and **Errors** reports
orphaned tags (assignments pointing at entries the current packdump no longer has).
Both stay read-only summaries — the deeper surfaces (per-file diffs, click-to-jump,
rollback buttons) are later slices.
"""
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QWidget, QVBoxLayout, QLabel, QMenu, QMessageBox,
    QInputDialog,
)

from packsmith.core.history import rollback_step
from packsmith.gui.shell import style
from packsmith.gui.shell.tree import PanelTree


def _empty_label(text) -> QLabel:
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px; font-style: italic;")
    return lbl


class _SummaryView(QWidget):
    """A tree with an empty-state message swapped in when there's nothing to show."""

    def __init__(self, headers, empty_text, parent=None):
        super().__init__(parent)
        self._empty_text = empty_text

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._empty = _empty_label(empty_text)
        self._tree = PanelTree()
        self._tree.setColumnCount(len(headers))
        self._tree.setHeaderLabels(headers)
        self._tree.setRootIsDecorated(False)
        self._tree.setStyleSheet(style.LIST_QSS + f"""
            QHeaderView::section {{
                background: {style.BG_PANEL}; color: {style.TEXT_FAINT};
                border: none; border-bottom: 1px solid {style.BORDER};
                padding: 3px 6px; font-size: 11px;
            }}
        """)
        lay.addWidget(self._empty)
        lay.addWidget(self._tree)
        self._show_empty(True)

    def _show_empty(self, empty):
        self._empty.setVisible(empty)
        self._tree.setVisible(not empty)


_BAD_STATUSES = {"failed", "partial"}


class JobResultsView(_SummaryView):
    """Per-run summary (design 4.1). Job runs are top-level with their steps nested
    beneath; a standalone action run (no job) appears on its own."""

    rolled_back = Signal()

    def __init__(self, history, job_history=None, tag_store=None, file_store=None,
                 parent=None):
        super().__init__(["Run", "Status", "Changes", "Finished"],
                         "No runs yet.", parent)
        self._history = history
        self._job_history = job_history
        self._tags = tag_store
        self._files = file_store
        self._tree.setRootIsDecorated(True)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self.refresh()

    # --- rollback (design 3.3.2: steps are independently rollback-able) -----

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        step = item.data(0, Qt.UserRole) if item else None
        if not step or self._tags is None:
            return
        if step.get("status") != "success":
            return                          # nothing committed, nothing to reverse
        menu = QMenu(self)
        menu.addAction("Roll back this step…", lambda: self._rollback(step, item))
        menu.exec(self._tree.mapToGlobal(pos))

    def _rollback(self, step, item):
        warning = ""
        parent = item.parent()
        if parent is not None:
            later = [parent.child(i).data(0, Qt.UserRole) for i in range(parent.childCount())]
            after = [s for s in later if s and s.get("status") == "success"
                     and (s.get("position_in_run") or 0) > (step.get("position_in_run") or 0)]
            if after:
                # Steps are independently reversible, but later ones may have been built on
                # this one — say so rather than quietly leaving an incoherent state.
                warning = (f"\n\n{len(after)} later step(s) in this run also committed and "
                           f"may depend on it. They will NOT be rolled back.")
        if QMessageBox.question(
                self, "Roll back step",
                f"Reverse '{step.get('action_ref')}'?\n\n"
                f"Its Layer 2 writes are undone and any files it changed are restored to "
                f"their previous contents.{warning}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            rollback_step(step["id"], tag_store=self._tags, history=self._history,
                          file_store=self._files)
        except Exception as e:
            QMessageBox.warning(self, "Rollback failed", f"{type(e).__name__}: {e}")
            return
        self._history.mark_rolled_back(step["id"])
        self.refresh()
        self.rolled_back.emit()

    def refresh(self):
        self._tree.clear()
        steps = self._history.list()
        job_runs = self._job_history.list() if self._job_history is not None else []
        if not steps and not job_runs:
            self._show_empty(True)
            return

        by_run = {}
        for step in steps:
            by_run.setdefault(step.get("job_run_id"), []).append(step)

        for run in job_runs:
            children = sorted(by_run.pop(run["id"], []),
                              key=lambda s: s.get("position_in_run") or 0)
            changed = ", ".join(filter(None, (_describe_changes(c.get("rollback_data"))
                                              for c in children if c.get("rollback_data"))))
            item = QTreeWidgetItem([
                run["job_name"], run["status"],
                f"{len(children)} step(s)" if children else "—",
                (run.get("finished_at") or "")[:19].replace("T", " "),
            ])
            if run["status"] in _BAD_STATUSES:
                item.setForeground(1, Qt.red)
            for child in children:
                self._tree_add_step(item, child)
            self._tree.addTopLevelItem(item)
            item.setExpanded(run["status"] in _BAD_STATUSES)

        # Standalone action runs (job_run_id is NULL) — still first-class history.
        for step in by_run.get(None, []):
            self._tree.addTopLevelItem(self._step_item(step))

        for col in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(col)
        self._expand_failures(self._tree.invisibleRootItem())
        self._show_empty(False)

    def _tree_add_step(self, parent, step):
        parent.addChild(self._step_item(step))

    @staticmethod
    def _step_item(step) -> QTreeWidgetItem:
        status = step.get("status") or ""
        item = QTreeWidgetItem([
            step.get("action_ref") or "",
            status,
            "—" if status == "rolled_back" else _describe_changes(step.get("rollback_data")),
            (step.get("finished_at") or "")[:19].replace("T", " "),
        ])
        if status not in ("success", "rolled_back"):
            item.setForeground(1, QColor(style.ERROR))
        elif status == "rolled_back":
            item.setForeground(1, Qt.gray)
        item.setData(0, Qt.UserRole, step)
        item.setToolTip(0, step.get("reason") or "Right-click to roll this step back")

        # Why it failed belongs ON SCREEN, not in a tooltip nobody thinks to hover.
        # Nested one level under the step, in red, wrapped across rows if it's long —
        # and the step auto-expands (see _expand_failures) so it's visible immediately.
        if status not in ("success", "rolled_back") and step.get("reason"):
            for line in _reason_lines(step["reason"]):
                detail = QTreeWidgetItem([line])
                detail.setForeground(0, QColor(style.ERROR))
                detail.setToolTip(0, step["reason"])
                item.addChild(detail)
        return item

    def _expand_failures(self, item):
        """Open failed steps so their reason is on screen without a click, and let the
        reason lines span the full width instead of being clipped by the Run column."""
        for i in range(item.childCount()):
            child = item.child(i)
            step = child.data(0, Qt.UserRole)
            if step is None:                       # a reason line
                child.setFirstColumnSpanned(True)
            elif step.get("status") not in ("success", "rolled_back"):
                child.setExpanded(True)
            self._expand_failures(child)


class ErrorsView(_SummaryView):
    """Problems worth the user's attention, with the actions that resolve them.

    Today that means **orphaned tag assignments** (design 3.2.1) — assignments pointing at
    an entry the packdump no longer has, or at an enum value its definition no longer has.
    Orphans are grouped by (tag, cause) because resolutions apply to the whole group, and
    right-clicking a group offers exactly the resolutions that make sense for its cause:
    a stale value can be cleared, reassigned, or un-removed; a missing entry can only be
    cleared, since nothing can conjure the entry back.
    """

    resolved = Signal()   # something changed; the app should re-evaluate views

    def __init__(self, tag_store, packdump, parent=None):
        super().__init__(["Problem", "Registry", "Detail"],
                         "No problems detected.", parent)
        self._tags = tag_store
        self._packdump = packdump
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self.refresh()

    def has_problems(self) -> bool:
        """Whether anything is currently listed. Lets callers decide to interrupt without
        recomputing orphans a second time."""
        return self._tree.topLevelItemCount() > 0

    def refresh(self):
        self._tree.clear()
        try:
            orphans = self._tags.find_orphans(self._packdump)
        except Exception as e:                       # never let a panel break the shell
            self._tree.addTopLevelItem(QTreeWidgetItem(["Check failed", "", str(e)]))
            self._show_empty(False)
            return

        if not orphans:
            self._show_empty(True)
            return

        # Group by (registry, tag, cause, value) — one row per thing the user can act on.
        groups = {}
        for orphan in orphans:
            key = (orphan.registry_type, orphan.tag_name, orphan.reason,
                   orphan.value if orphan.reason == "stale_value" else None)
            groups.setdefault(key, []).append(orphan)

        for (registry_type, tag_name, reason, value), members in sorted(groups.items()):
            count = len(members)
            if reason == "stale_value":
                problem = "Orphaned value"
                detail = (f"{tag_name} = '{value}' — no longer a value of that tag "
                          f"({count} assignment{'s' if count != 1 else ''})")
            else:
                problem = "Missing entry"
                detail = (f"{tag_name} — {count} assignment{'s' if count != 1 else ''} on "
                          f"entries not in the current packdump")
            item = QTreeWidgetItem([problem, registry_type, detail])
            item.setData(0, Qt.UserRole, (registry_type, tag_name, reason, value, members))
            item.setToolTip(2, "Right-click to resolve")
            self._tree.addTopLevelItem(item)

        for col in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(col)
        self._show_empty(False)

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        payload = item.data(0, Qt.UserRole) if item else None
        if not payload:
            return
        registry_type, tag_name, reason, value, members = payload

        menu = QMenu(self)
        if reason == "stale_value":
            menu.addAction(f"Clear  ({len(members)} assignments)",
                           lambda: self._clear_value(registry_type, tag_name, value))
            menu.addAction("Reassign to…",
                           lambda: self._reassign(registry_type, tag_name, value))
            menu.addAction(f"Restore '{value}' to the tag",
                           lambda: self._restore(registry_type, tag_name, value))
        else:
            menu.addAction(f"Clear  ({len(members)} assignments)",
                           lambda: self._clear_entries(registry_type, tag_name, members))
        menu.exec(self._tree.mapToGlobal(pos))

    # --- resolutions -------------------------------------------------------

    def _confirm(self, title, text) -> bool:
        return QMessageBox.question(self, title, text,
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    def _clear_value(self, registry_type, tag_name, value):
        entries = self._tags.entries_with_value(registry_type, tag_name, value)
        if not self._confirm("Clear assignments",
                             f"Delete {len(entries)} assignment(s) of '{tag_name}' "
                             f"holding '{value}'?\n\nThis cannot be undone."):
            return
        self._tags.clear_value(registry_type, tag_name, value)
        self._done()

    def _clear_entries(self, registry_type, tag_name, members):
        if not self._confirm("Clear assignments",
                             f"Delete {len(members)} assignment(s) of '{tag_name}' on "
                             f"entries missing from the packdump?\n\nThis cannot be undone."):
            return
        self._tags.unassign(registry_type, [o.entry_id for o in members], tag_name)
        self._done()

    def _reassign(self, registry_type, tag_name, value):
        definition = self._tags.definition(registry_type, tag_name)
        choices = list(definition.get("values", [])) if definition else []
        if not choices:
            QMessageBox.warning(self, "No valid values",
                                f"'{tag_name}' has no values to reassign to.")
            return
        target, ok = QInputDialog.getItem(
            self, "Reassign", f"Move assignments of '{value}' to:", choices, 0, False)
        if not ok or not target:
            return
        moved = self._tags.reassign_value(registry_type, tag_name, value, target)
        QMessageBox.information(self, "Reassigned",
                                f"Moved {moved} assignment(s) from '{value}' to '{target}'.")
        self._done()

    def _restore(self, registry_type, tag_name, value):
        definition = self._tags.definition(registry_type, tag_name)
        values = list(definition.get("values", [])) if definition else []
        self._tags.set_enum_values(registry_type, tag_name, values + [value])
        self._done()

    def _done(self):
        self.refresh()
        self.resolved.emit()


_REASON_WRAP = 96


def _reason_lines(reason) -> list[str]:
    """Break a failure reason into displayable rows: honour its own line breaks, and wrap
    anything longer than the panel comfortably shows rather than clipping it."""
    lines = []
    for raw in str(reason).splitlines():
        line = raw.strip()
        while len(line) > _REASON_WRAP:
            cut = line.rfind(" ", 0, _REASON_WRAP)
            if cut <= 0:
                cut = _REASON_WRAP
            lines.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        if line:
            lines.append(line)
    return lines or [str(reason)]


def _describe_changes(rollback_json) -> str:
    """Summarize what a run touched, from the inverse data it recorded."""
    if not rollback_json:
        return "—"
    try:
        data = json.loads(rollback_json)
    except (TypeError, ValueError):
        return "—"
    parts = []
    cells = len(data.get("l2", []) or [])
    files = len(data.get("files", {}) or {})
    if cells:
        parts.append(f"{cells} tag cell{'s' if cells != 1 else ''}")
    if files:
        parts.append(f"{files} file{'s' if files != 1 else ''}")
    return ", ".join(parts) if parts else "no changes"
