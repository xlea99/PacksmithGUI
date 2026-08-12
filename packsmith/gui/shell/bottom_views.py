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
    QTreeWidget, QTreeWidgetItem, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMenu,
    QMessageBox, QInputDialog, QPushButton,
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
                 parent=None, blueprint_store=None):
        super().__init__(["Run", "Status", "Changes", "Finished"],
                         "No runs yet.", parent)
        self._history = history
        self._job_history = job_history
        self._tags = tag_store
        self._files = file_store
        self._blueprints = blueprint_store
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
        # Offer rollback when there is something recorded to undo, not when the step
        # "succeeded". A step can fail at commit having already written files — the runner
        # records how to reverse those and its failure reason literally says to — and
        # gating on status made that instruction impossible to follow. An already
        # rolled-back step has its record cleared, so this excludes it for free.
        if not _has_rollback(step):
            return
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
                f"Its tag and blueprint writes are undone, and any files it changed are "
                f"restored to their previous contents.{warning}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            rollback_step(step["id"], tag_store=self._tags, history=self._history,
                          file_store=self._files, blueprint_store=self._blueprints)
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

    def __init__(self, tag_store, packdump, blueprint_store=None, parent=None,
                 job_store=None, package_index=None):
        super().__init__(["Problem", "Registry", "Detail"],
                         "No problems detected.", parent)
        self._tags = tag_store
        self._packdump = packdump
        self._blueprints = blueprint_store
        self._jobs = job_store
        self._packages = package_index
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self.refresh()

    def has_problems(self) -> bool:
        """Whether anything is currently listed. Lets callers decide to interrupt without
        recomputing orphans a second time."""
        return self._tree.topLevelItemCount() > 0

    def set_packdump(self, packdump):
        """Adopt a newly imported dump (design 3.1).

        This panel *is* the fallout report — orphans are assignments pointing at entries
        the current dump no longer has — so it has to recompute against the new one. A
        stale reference here would keep saying the pack was fine while it wasn't.
        """
        self._packdump = packdump
        self.refresh()

    def refresh(self):
        self._tree.clear()
        try:
            orphans = self._tags.find_orphans(self._packdump)
        except Exception as e:                       # never let a panel break the shell
            self._tree.addTopLevelItem(QTreeWidgetItem(["Check failed", "", str(e)]))
            self._show_empty(False)
            return

        # No early return on "no tag orphans": this panel answers "what is broken" across
        # every primitive, and blueprint orphans are added below. Bailing here would hide
        # them whenever tags happened to be clean, which is most of the time.

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

        self._add_blueprint_orphans()
        self._add_relinks_owed()
        for col in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(col)
        self._show_empty(not self.has_problems())

    def _add_relinks_owed(self):
        """Steps bound to a tag that has since been renamed (design 3.2.1).

        These belong here rather than in the Jobs panel because this panel answers one
        question — "what is broken, and how do I fix it" — and a job that refuses to start
        is exactly that. The row is red because the consequence is hard: the whole job is
        gated until this is answered, not just the step.
        """
        if self._jobs is None or self._packages is None:
            return
        from packsmith.core.bindings import stale_bindings
        for job in self._jobs.all():
            for stale in stale_bindings(self._jobs.get(job.id),
                                        package_index=self._packages,
                                        tag_store=self._tags):
                item = QTreeWidgetItem(
                    ["Relink owed", f"{stale.job_name} step {stale.position + 1}",
                     f"'{stale.slot}' was bound to '{stale.was}', now called "
                     f"'{stale.now}' — this job will not run until you confirm"])
                item.setForeground(0, style.qt_colour(style.ERROR))
                item.setData(0, Qt.UserRole + 2, stale)
                item.setToolTip(2, "Right-click to relink")
                self._tree.addTopLevelItem(item)

    def _add_blueprint_orphans(self):
        """Design 3.2.2: an orphaned instance "surfaces in the Errors panel under an
        'Orphaned Instances' section and is locked against action access." Same panel as
        tag orphans on purpose — one place to answer "what is broken", whichever primitive
        broke it."""
        if self._blueprints is None:
            return
        for orphan in self._blueprints.orphans():
            if orphan.reason == "retype_slot":
                detail = (f"'{orphan.slot_path}' changed type"
                          + (f" — {len(orphan.problematic)} value(s) need re-binding"
                             if orphan.problematic else ""))
            else:
                detail = f"'{orphan.slot_path}' was removed while this instance used it"
            item = QTreeWidgetItem(["Orphaned instance",
                                    f"{orphan.blueprint}:{orphan.instance}", detail])
            item.setForeground(0, style.qt_colour(style.ERROR))
            item.setData(0, Qt.UserRole + 1, orphan)
            item.setToolTip(2, "Right-click to resolve — you must choose one")
            self._tree.addTopLevelItem(item)

        # A binding whose target vanished. Not an error the user caused, so it reads as a
        # warning and locks nothing — the same treatment tag orphans get.
        for orphan in self._blueprints.find_binding_orphans(self._packdump):
            item = QTreeWidgetItem(
                ["Orphaned binding",
                 f"{orphan.blueprint}:{orphan.instance}.{orphan.slot_path}",
                 orphan.detail])
            item.setForeground(0, style.qt_colour(style.WARNING))
            self._tree.addTopLevelItem(item)

        for path in self._blueprints.cycles():
            item = QTreeWidgetItem(["Instance cycle", path[0], " → ".join(path)])
            item.setForeground(0, style.qt_colour(style.ERROR))
            item.setToolTip(2, "Unbind one of these references to break the loop")
            self._tree.addTopLevelItem(item)

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        stale = item.data(0, Qt.UserRole + 2)
        if stale is not None:
            menu = QMenu(self)
            menu.addAction(f"Relink to '{stale.now}'", lambda: self._relink(stale))
            menu.exec(self._tree.mapToGlobal(pos))
            return
        orphan = item.data(0, Qt.UserRole + 1)
        if orphan is not None:
            self._blueprint_menu(pos, orphan)
            return
        payload = item.data(0, Qt.UserRole)
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

    def _relink(self, stale):
        """Accept the step's bindings under their new names.

        Nothing about the target changes — it was always this tag id. What the user is
        asserting is that the step still *means* what they want, which is the entire point
        of §3.2.1 gating rather than silently carrying on.
        """
        from packsmith.core.bindings import record_names
        step = next((s for job in self._jobs.all()
                     for s in self._jobs.get(job.id).steps if s.id == stale.step_id), None)
        if step is None:
            self.refresh()
            return
        manifest = self._packages.get(step.action_ref)
        self._jobs.relink_step(step.id,
                               record_names(manifest, step.bindings, tag_store=self._tags))
        self.refresh()
        self.resolved.emit()

    def _blueprint_menu(self, pos, orphan):
        """3.2.2's four resolutions. "The user must choose one; there is no 'ignore' or
        'dismiss' that silently proceeds with a broken instance." — hence no such entry."""
        menu = QMenu(self)
        menu.addAction("Discard the orphaned bindings",
                       lambda: self._resolve_orphan("discard", orphan))
        menu.addAction("Preserve them (hidden from actions)",
                       lambda: self._resolve_orphan("preserve", orphan))
        menu.addAction(f"Revert the schema change to '{orphan.slot_path}'",
                       lambda: self._resolve_orphan("revert", orphan))
        if "rebind" in orphan.resolutions and orphan.problematic:
            menu.addSeparator()
            for path in orphan.problematic:
                menu.addAction(f"Re-bind '{path}'…",
                               lambda p=path: self._rebind_orphan(orphan, p))
        # 3.2.2: the four resolutions are available "per-instance (or applied in bulk)".
        # One destructive schema edit orphans EVERY bound instance at once, so resolving
        # them one right-click at a time is the common case, not the rare one.
        siblings = [o for o in self._blueprints.orphans(orphan.blueprint)
                    if o.slot_path == orphan.slot_path]
        if len(siblings) > 1:
            menu.addSeparator()
            bulk = menu.addMenu(f"All {len(siblings)} orphaned by '{orphan.slot_path}'")
            bulk.addAction("Discard their orphaned bindings",
                           lambda: self._resolve_orphans("discard", siblings))
            bulk.addAction("Preserve them (hidden from actions)",
                           lambda: self._resolve_orphans("preserve", siblings))
        menu.exec(self._tree.mapToGlobal(pos))

    def _resolve_orphan(self, how, orphan):
        target = f"{orphan.blueprint}:{orphan.instance}"
        prompts = {
            "discard": (f"Discard the orphaned bindings on {target}?\n\n"
                        f"The data tied to '{orphan.slot_path}' is deleted permanently."),
            "preserve": (f"Keep {target}'s orphaned bindings in limbo?\n\n"
                         f"The instance un-orphans and actions can use it again. The "
                         f"displaced data stays in the database but is never exposed to "
                         f"actions — useful if the slot comes back."),
            "revert": (f"Revert the schema change to '{orphan.slot_path}' on "
                       f"'{orphan.blueprint}'?\n\nEVERY instance of this blueprint snaps "
                       f"back to how it was before the change."),
        }
        if not self._confirm("Resolve orphan", prompts[how]):
            return
        try:
            if how == "discard":
                self._blueprints.discard(orphan.blueprint, orphan.instance)
            elif how == "preserve":
                self._blueprints.preserve(orphan.blueprint, orphan.instance)
            else:
                self._blueprints.revert(orphan.blueprint)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't resolve", str(e))
            return
        self.refresh()
        self.resolved.emit()

    def _resolve_orphans(self, how, orphans):
        """The same resolution across a whole batch, confirmed once.

        `revert` is deliberately absent from the bulk menu: it already acts on every
        instance of the blueprint, so "apply revert to these twelve" would be a lie about
        its blast radius. `rebind` is absent too — each value is a separate decision.
        """
        verb = ("Discard the orphaned bindings on" if how == "discard"
                else "Keep the orphaned bindings in limbo for")
        names = ", ".join(f"{o.blueprint}:{o.instance}" for o in orphans[:10])
        more = f"\n…and {len(orphans) - 10} more" if len(orphans) > 10 else ""
        detail = ("Their data is deleted permanently."
                  if how == "discard"
                  else "They un-orphan and actions can use them again; the displaced data "
                       "stays in the database but is never exposed to actions.")
        if not self._confirm(
                f"Resolve {len(orphans)} orphans",
                f"{verb} {len(orphans)} instance(s)?\n\n{names}{more}\n\n{detail}"):
            return
        failed = []
        for orphan in orphans:
            try:
                if how == "discard":
                    self._blueprints.discard(orphan.blueprint, orphan.instance)
                else:
                    self._blueprints.preserve(orphan.blueprint, orphan.instance)
            except Exception as e:
                failed.append(f"{orphan.blueprint}:{orphan.instance} — {e}")
        if failed:
            # Named, not counted: a partial failure the user can't see is worse than none.
            QMessageBox.warning(self, "Some couldn't be resolved", "\n".join(failed[:10]))
        self.refresh()
        self.resolved.emit()

    def _rebind_orphan(self, orphan, path):
        slot = self._blueprints.slot(orphan.blueprint, path)
        value, ok = QInputDialog.getText(
            self, "Re-bind slot",
            f"{orphan.blueprint}:{orphan.instance}.{path}\n\nNew value ({slot.describe()}):")
        if not ok or not value.strip():
            return
        try:
            self._blueprints.rebind(orphan.blueprint, orphan.instance, path, value.strip())
        except Exception as e:
            QMessageBox.warning(self, "Couldn't re-bind", str(e))
            return
        self.refresh()
        self.resolved.emit()

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


def _has_rollback(step) -> bool:
    """Does this run have anything recorded to undo?"""
    if step.get("status") == "rolled_back":
        return False
    try:
        data = json.loads(step.get("rollback_data") or "{}")
    except (TypeError, ValueError):
        return False
    return any(data.get(key) for key in ("l2", "files", "blueprints"))


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
    bindings = len(data.get("blueprints", []) or [])
    if cells:
        parts.append(f"{cells} tag cell{'s' if cells != 1 else ''}")
    if bindings:
        parts.append(f"{bindings} blueprint write{'s' if bindings != 1 else ''}")
    if files:
        parts.append(f"{files} file{'s' if files != 1 else ''}")
    return ", ".join(parts) if parts else "no changes"


class PackdumpView(_SummaryView):
    """Packdump management, in the strip — design 4.1.

    §4.1 asks for "diff summary, orphaned tags at risk, and a Bless action". All of that
    lives here EXCEPT the diff itself, which opens as its own tab: a real update to a
    300-mod pack moves thousands of entries, and a few rows of bottom panel is a place to
    learn *that* something changed, not to read *what*.

    So this is the summary and the launcher. It also carries the two things you can
    actually do to a snapshot — adopt the pending one, or go back to an older one — because
    they are decisions about what you just read.
    """

    open_diff_requested = Signal()
    snapshot_diff_requested = Signal(str)     # vs the snapshot before it
    snapshot_vs_active_requested = Signal(str)   # vs the dump in effect now
    bless_requested = Signal()
    revert_requested = Signal(str)      # snapshot folder name
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(["Snapshot", "When", "Mods"],
                         "No packdump loaded.", parent)
        self._summary = None
        self._pending = False

        self._headline = QLabel()
        self._headline.setWordWrap(True)
        self._headline.setContentsMargins(10, 6, 10, 4)
        self._headline.setStyleSheet(f"color: {style.TEXT}; font-size: 11px;")
        self.layout().insertWidget(0, self._headline)

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 0, 8, 6)
        bar_lay.setSpacing(6)
        self._open_button = self._small_button("View changes…", self.open_diff_requested)
        self._bless_button = self._small_button("Bless this dump", self.bless_requested)
        self._bless_button.hide()
        bar_lay.addWidget(self._open_button)
        bar_lay.addWidget(self._bless_button)
        bar_lay.addStretch()
        self.layout().insertWidget(1, bar)

        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

    @staticmethod
    def _small_button(text, signal):
        button = QPushButton(text)
        button.setStyleSheet(f"""
            QPushButton {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER}; font-size: 11px; padding: 2px 8px;
            }}
            QPushButton:hover {{ color: {style.TEXT}; border-color: {style.ACCENT_EDGE}; }}
        """)
        button.clicked.connect(lambda: signal.emit())
        return button

    def show_state(self, *, summary=None, at_risk=(), pending=False, snapshots=(),
                   active=None):
        """Everything the strip knows, set in one call.

        One setter rather than several because these are facets of a single answer — what
        happened to Layer 1 — and letting them be set independently is how a panel ends up
        showing a diff from one import beside a snapshot list from another.
        """
        self._summary = summary
        self._pending = pending
        # Blessing is only offered when there is something to bless. With auto-adopt on
        # (the default) that is never, and a permanently disabled button would be a
        # standing invitation to wonder what it does.
        self._bless_button.setVisible(pending)
        self._open_button.setEnabled(summary is not None and not summary.empty)

        if summary is None:
            self._headline.setText("No packdump comparison available.")
        elif pending:
            self._headline.setText(
                f"A new dump is waiting: {summary.headline()}. "
                f"Nothing has changed until you bless it.")
        elif summary.empty:
            self._headline.setText("The instance's dump matches the active snapshot.")
        else:
            text = f"Last import: {summary.headline()}."
            if at_risk:
                text += (f"   ⚠ {len(at_risk):,} tag assignment"
                         f"{'s' if len(at_risk) != 1 else ''} orphaned by it.")
            self._headline.setText(text)

        self._tree.clear()
        for snapshot in snapshots:
            item = QTreeWidgetItem([
                snapshot.get("name", ""),
                (snapshot.get("timestamp") or "").replace("T", " ")[:19],
                str(snapshot.get("mod_count", "")),
            ])
            item.setData(0, Qt.UserRole, snapshot.get("name"))
            if snapshot.get("name") == active:
                item.setForeground(0, style.qt_colour(style.ACCENT_EDGE))
                item.setToolTip(0, "the active snapshot")
            self._tree.addTopLevelItem(item)
        self._show_empty(self._tree.topLevelItemCount() == 0)

    def _on_context_menu(self, pos):
        menu = self._menu_for(self._tree.itemAt(pos))
        if menu is not None:
            menu.exec(self._tree.mapToGlobal(pos))

    def _menu_for(self, item):
        """Built separately from being shown, so it can be inspected: `exec` blocks on a
        real menu loop, which in a test is a hang rather than a failure."""
        name = item.data(0, Qt.UserRole) if item is not None else None
        if not name:
            return None
        menu = QMenu(self)
        # First, because looking is what you do before deciding — and reverting to a
        # snapshot you haven't inspected is exactly the guess this tab exists to replace.
        # Two different questions, and both get asked: "what did this update do" (against
        # the snapshot before it) and "what have I gained or lost since" (against the dump
        # in effect now). The second is usually why you are looking at history at all.
        menu.addAction("See what changed in this snapshot",
                       lambda: self.snapshot_diff_requested.emit(name))
        menu.addAction("Compare with the active dump",
                       lambda: self.snapshot_vs_active_requested.emit(name))
        menu.addSeparator()
        menu.addAction(f"Revert to {name}", lambda: self.revert_requested.emit(name))
        return menu
