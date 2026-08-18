"""Live content for the bottom panel (design 4.1).

**Job Runs** reads the ``step_runs`` history the runner records, and **Errors** reports
orphaned tags (assignments pointing at entries the current packdump no longer has).

Both are deliberately shallow, and Job Runs is shallow on purpose *again*: it was written
before the Run Report tab existed and had grown into a second, worse account of a run.
§4.1's ruling for the packdump strip settles it for this one too — a strip a few rows tall
says THAT something happened; reading WHAT belongs in a tab. Errors is the exception that
proves the shape, because its rows are *resolved in place* rather than read (§4.1).
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
_ROLE_JOB_RUN = Qt.UserRole + 1     # job_runs id, on a run's top-level row


class JobRunsView(_SummaryView):
    """That a run happened, and how it went (design 4.1).

    **Deliberately not comprehensive.** This was built before the Run Report tab existed,
    when it was the only place a run described itself, so it grew failure reasons wrapped
    across child rows and auto-expanding failures. The report tab now holds all of that —
    with the change list on screen — and §4.1's own ruling applies: *a strip a few rows tall
    is a place to learn THAT something changed, not to read WHAT.* Two surfaces competing to
    be comprehensive means one of them is always the worse copy.

    So: the run, its steps, how each went, how much each changed, when. A failure shows a
    sliver of its reason and nothing more, because the row itself opens the report.
    """

    rolled_back = Signal()
    report_requested = Signal(int)      # job_runs id

    def __init__(self, history, job_history=None, tag_store=None, file_store=None,
                 parent=None, blueprint_store=None):
        super().__init__(["Run", "Status", "Changes", "Finished"],
                         "No runs yet.", parent)
        self._history = history
        self._job_history = job_history
        self._tags = tag_store
        self._files = file_store
        self._blueprints = blueprint_store
        # run id -> expanded, for this session only. A rebuild clears the tree, so without
        # this every refresh — and a refresh happens on every run — would reset whatever
        # the user had opened. Not persisted: which runs you had expanded is a fact about
        # what you were looking at ten minutes ago, not about the profile.
        self._expanded = {}
        self._rebuilding = False
        self._tree.setRootIsDecorated(True)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemDoubleClicked.connect(self._on_activated)
        self._tree.itemExpanded.connect(self._remember_expansion)
        self._tree.itemCollapsed.connect(self._remember_expansion)
        self.refresh()

    def _remember_expansion(self, item):
        """Record what the user opened — and only what the USER opened.

        Qt fires these for a programmatic `setExpanded` too, so a refresh restoring state
        would otherwise re-record what it just applied. Harmless here, but it would also
        record the *default* for a run the user has never touched, which is how a default
        stops being changeable later.
        """
        if self._rebuilding:
            return
        run_id = item.data(0, _ROLE_JOB_RUN)
        if run_id is not None:
            self._expanded[int(run_id)] = item.isExpanded()

    def _on_activated(self, item, _column=0):
        run_id = item.data(0, _ROLE_JOB_RUN)
        if run_id is not None:
            self.report_requested.emit(int(run_id))

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
        self._rebuilding = True
        try:
            self._rebuild()
        finally:
            self._rebuilding = False

    def _rebuild(self):
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
            item = QTreeWidgetItem([
                run["job_name"], run["status"],
                _change_counts(c.get("rollback_data") for c in children),
                (run.get("finished_at") or "")[:19].replace("T", " "),
            ])
            # The way through to the full report. This strip is where you learn THAT a run
            # happened; §4.1 already ruled that reading WHAT it did belongs in a tab.
            item.setData(0, _ROLE_JOB_RUN, run["id"])
            item.setToolTip(0, f"{run['job_name']} — double-click to open the report")
            if run["status"] in _BAD_STATUSES:
                item.setForeground(1, QColor(style.ERROR))
            for child in children:
                item.addChild(self._step_item(child))
            self._tree.addTopLevelItem(item)
            # Collapsed unless the user opened this run before. A new run arriving expanded
            # pushes everything below it down the moment you press play — and since a
            # refresh happens on every run, it did that to *every* run at once.
            item.setExpanded(self._expanded.get(run["id"], False))

        # Standalone action runs (job_run_id is NULL) — still first-class history.
        for step in by_run.get(None, []):
            self._tree.addTopLevelItem(self._step_item(step))

        for col in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(col)
        self._show_empty(False)

    @staticmethod
    def _step_item(step) -> QTreeWidgetItem:
        """One step, as a leaf.

        It used to carry its failure reason as wrapped child rows that auto-expanded. That
        was right when this was the only account of a run and wrong now: the reason is in
        the report, in full, next to what the step managed to change before it stopped.
        Here it is a sliver — enough to recognise which failure this is without reading it.
        """
        status = step.get("status") or ""
        reason = step.get("reason")
        failed = status not in ("success", "rolled_back")
        item = QTreeWidgetItem([
            step.get("action_ref") or "",
            f"{status} — {_error_sliver(reason)}" if failed and reason else status,
            "—" if status == "rolled_back" else _change_counts([step.get("rollback_data")]),
            (step.get("finished_at") or "")[:19].replace("T", " "),
        ])
        if failed:
            item.setForeground(1, QColor(style.ERROR))
        elif status == "rolled_back":
            item.setForeground(1, Qt.gray)
        item.setData(0, Qt.UserRole, step)
        item.setToolTip(1, reason or "")
        item.setToolTip(0, "Double-click the run above to open its report")
        return item


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


def _has_rollback(step) -> bool:
    """Does this run have anything recorded to undo?"""
    if step.get("status") == "rolled_back":
        return False
    try:
        data = json.loads(step.get("rollback_data") or "{}")
    except (TypeError, ValueError):
        return False
    return any(data.get(key) for key in ("l2", "files", "blueprints"))


# The order counts are read in, which is not alphabetical: what came into existence, what
# moved, what left. `unchanged` is absent on purpose — §3.3 keeps it in the record and out
# of the headline, because "the action asserted a cell that was already correct" is not a
# change and would inflate every re-run.
_KIND_ORDER = ("added", "created", "changed", "claimed", "removed")


def _change_counts(rollback_jsons) -> str:
    """How much a step or a run changed, by kind — a number, never a list.

    Reading *what* changed is the report tab's job (§4.1). This answers the only question
    a strip can usefully answer: is this a run that did nothing, a handful of things, or
    twelve thousand things?

    Counted by KIND rather than by engine ("3 tag cells, 1 file") because the kinds are what
    tell you the shape of the edit — a re-run that reports `claimed` where you expected
    `added` has taken cells off you, and that is worth seeing before you open anything.
    """
    counts = {}
    for raw in rollback_jsons:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for change in data.get("changes") or []:
            kind = change.get("kind")
            if kind and kind != "unchanged":
                counts[kind] = counts.get(kind, 0) + 1
    if not counts:
        return "—"
    ordered = [k for k in _KIND_ORDER if k in counts] + \
              [k for k in sorted(counts) if k not in _KIND_ORDER]
    return ", ".join(f"{counts[kind]:,} {kind}" for kind in ordered)


def _error_sliver(reason, limit: int = 64) -> str:
    """Just enough of a failure to recognise which one it is.

    Prefers a line *starting* with `error:` — Starlark's own convention for the line that
    says what actually broke — over the first line, because a Starlark failure arrives as a
    traceback headed *"StarlarkActionError: Traceback (most recent call last):"*, which is
    true of every failure and therefore tells them apart not at all.

    Matched on the prefix rather than anywhere in the line, because that header contains
    the substring `Error:` itself and a looser test picks the very line it exists to skip.
    """
    lines = [line.strip() for line in str(reason or "").splitlines() if line.strip()]
    if not lines:
        return ""
    pick = next((line for line in lines if line.lower().startswith("error:")), lines[0])
    return pick if len(pick) <= limit else pick[:limit - 1].rstrip() + "…"


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
