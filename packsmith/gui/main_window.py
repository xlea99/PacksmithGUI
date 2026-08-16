"""The main window — composes the shell (design 4.1) and hosts view tabs.

Layout, per §4.1: a fixed icon strip pinned to the left edge, a resizable panel stack
beside it, the workspace taking the bulk of the area, and a collapsible bottom panel
whose bottom-most strip is the status bar. The window itself stays thin: it wires
services to the shell and owns the actions that span tabs (run an action, author a view,
undo/redo).
"""
import dataclasses
import re
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton,
    QHeaderView, QSplitter, QInputDialog, QMessageBox, QApplication,
)
from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtGui import QShortcut, QKeySequence, QFont

from packsmith.common.setup import (
    load_state, load_ui_state, save_state, save_ui_state)
from packsmith.core.profile import Profile, list_profiles

# Which profile was open last (§4.1). App STATE, not config — see `common/setup.py`.
LAST_PROFILE_KEY = "last_profile"
from packsmith.core.archives import ArchiveError, read_member
from packsmith.core.capabilities import (
    CapabilityError, DATAPACKS_WRITE, PACK_LOADER_SETTING, RESOURCEPACKS_WRITE,
    PackTargets, override_kind, pack_format_for, resolve)
from packsmith.core.launchers import locate_for
from packsmith.core.packdiff import summarise_diff, tags_at_risk
from packsmith.integrations import PACK_LOADERS
from packsmith.core.packdump import (
    ACTIVE_SNAPSHOT, auto_adopt_enabled, check_packdump, compare_snapshots,
    current_packdump,
    import_packdump, list_snapshots, previous_snapshot, revert_to_snapshot,
    snapshot_timeline)
from packsmith.common.logging import log
from packsmith.gui.table.edit_commands import UndoBlocked
from packsmith.gui.profile_editor import (
    NewProfileDialog, OpenProfileDialog, confirm_force_import,
)
from packsmith.gui.shell.packdump_banner import PackdumpBanner
from packsmith.core.db import UserDB
from packsmith.core.tags import TagStore
from packsmith.core.revalidate import broken_steps, summarise
from packsmith.core.packages import (
    PackageIndex, create_package, add_action, remove_action, create_file, delete_file,
    rename_file, create_folder, delete_folder, rename_folder, source_files,
)
from packsmith.core.bindings import (
    best_guess_bindings, record_names, resolve_step, step_problems)
from packsmith.core import filetypes
from packsmith.core.files import FileStore, content_hash
from packsmith.core.history import StepRunStore, JobRunStore
from packsmith.core.views import ViewStore
from packsmith.core.jobs import JobStore
from packsmith.core.blueprints import BlueprintError, BlueprintStore
from packsmith.core.job_runner import describe_summary, run_job

from packsmith.gui.demo_views import demo_views
from packsmith.gui.queries import blueprint_query, browse_query, tag_query
from packsmith.gui.query_constructor import QueryConstructorDialog
from packsmith.core import reports
from packsmith.core.history import rollback_step
from packsmith.gui.action_page import ActionPageTab
from packsmith.gui.run_report import RunReportTab
from packsmith.gui.encyclopedia import EncyclopediaTab
from packsmith.gui import encyclopedia_pages
from packsmith.gui.tag_editor import TagCreateDialog, EnumValuesDialog
from packsmith.gui.shell import icons, style
from packsmith.gui.shell.sidebar import Sidebar, PanelStack
from packsmith.gui.shell.workspace import Workspace
from packsmith.gui.shell.bottom_panel import BottomPanel
from packsmith.gui.shell.bottom_views import (
    PackdumpView, JobResultsView, ErrorsView, _has_rollback)
from packsmith.gui.shell.panels import PANEL_SPECS
from packsmith.gui.shell.panels.base import StubPanel
from packsmith.gui.shell.panels.registry_panel import RegistryPanel
from packsmith.gui.shell.panels.views_panel import ViewsPanel
from packsmith.gui.shell.panels.tags_panel import TagsPanel
from packsmith.gui.shell.panels.jobs_panel import JobsPanel
from packsmith.gui.shell.panels.files_panel import FilesPanel
from packsmith.gui.editor.host import (
    DiffTab, EditorHost, EditorTab, UnsupportedFileTab)
from packsmith.gui.editor.sources import (
    InstanceFileSource, JarMemberSource, PackageFileSource, is_overridable, member_path,
    split_member)
from packsmith.gui.shell.panels.actions_panel import ActionsPanel
from packsmith.gui.shell.panels.packages_panel import PackagesPanel
from packsmith.gui.shell.panels.automation_panel import AutomationPanel
from packsmith.gui.shell.panels.blueprints_panel import BlueprintsPanel
from packsmith.gui.query_bar import QueryBar, combine
from packsmith.core.query.ast import Blueprint as QueryBlueprint, QueryError
from packsmith.core.query.language import QuerySyntaxError
from packsmith.gui.blueprint_editor import BlueprintEditorTab, NewBlueprintDialog
from packsmith.gui.action_editor import (
    NewActionDialog, NewFileDialog, NewFolderDialog, NewPackageDialog, RenameFileDialog,
)
from packsmith.gui.image_viewer import ImageViewerTab
from packsmith.gui.override_dialog import OverrideTargetDialog
from packsmith.gui.packdump_diff import PackdumpDiffTab
from packsmith.gui.jar_viewer import JarViewerTab
from packsmith.gui.nbt_viewer import NbtViewerTab
from packsmith.gui.run_control import RunControl
from packsmith.gui.settings_dialog import SettingsDialog
from packsmith.gui.job_editor import JobEditorTab
from packsmith.gui.table.registry_table_model import RegistryTableModel
from packsmith.gui.table.registry_table_view import RegistryTableView
from packsmith.gui.table.table_layout import TableLayout
from packsmith.gui.table.cells.bool_cell import BoolCellDelegate
from packsmith.gui.table.cells.enum_cell import EnumCellDelegate
from packsmith.gui.table.cells.num_cell import NumCellDelegate
from packsmith.gui.table.cells.str_cell import StrCellDelegate
from packsmith.gui.table.cells.plain_cell import PlainCellDelegate

_DELEGATES = {
    "bool": BoolCellDelegate,
    "enum": EnumCellDelegate,
    "number": NumCellDelegate,
    "string": StrCellDelegate,
}


class MainWindow(QMainWindow):

    def __init__(self, profile_name: str = None):
        super().__init__()
        self.setWindowTitle("Packsmith")
        self.setMinimumSize(1200, 700)

        self._tab_models = {}     # tab widget -> RegistryTableModel
        self._tab_bars = {}       # tab widget -> its QueryBar
        self._tab_base_queries = {}   # tab widget -> the view's own query (bar ANDs onto it)
        self._tab_totals = {}     # tab widget -> unrefined row count, for "N of M"
        self._tab_views = {}      # tab widget -> the saved View it renders (if any)
        self._open_tabs = {}      # open-key -> tab widget (so we focus, not duplicate)
        self._delegates = []      # keep delegate refs alive
        # Table arrangements waiting to be written. Coalesced through one timer so a
        # column drag doesn't rewrite state.json on every pixel.
        # View tabs whose rows are out of date because a write happened somewhere else.
        # Reconciled when you look at them — see `_on_tags_written`.
        self._stale_tabs = set()
        self._pending_layouts = {}
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(400)
        self._layout_timer.timeout.connect(self._flush_layouts)
        self._editor_host = None  # survives profile switches; see _switch_profile
        self._shortcuts = []      # ditto — parented to the window, not the central widget
        self._import_result = None
        self._last_focus_check = None
        # Set when a dump was written to disk but the window could not take it on (the
        # unsaved-changes guard refused the fallback rebuild). Without it the import would
        # be lost for good: `check_packdump` compares the instance against what is already
        # on disk, so it would answer "unchanged" from then on.
        self._rebuild_pending = False

        self._build_menu_bar()
        self._enter_profile(profile_name or self._default_profile())

    # --- services ----------------------------------------------------------

    @staticmethod
    def _default_profile() -> str:
        """Which profile to open at launch, or "" when the user has none yet.

        There used to be a hardcoded fallback that CREATED a profile pointing at a path
        under one developer's home directory: a guaranteed launch crash on any other
        machine, and on that one machine it quietly invented a profile nobody asked for.
        First run now opens the gated window instead, which is what §3.1 describes —
        Packsmith "won't let you do anything until it loads its first packdump".

        §4.1: "Packsmith remembers the last active profile and auto-loads it on startup."
        A remembered profile that has since been deleted falls through to the old fallback
        rather than blocking startup — the breadcrumb is a convenience, never a dependency.
        """
        existing = list_profiles()
        if not existing:
            return ""
        remembered = load_state().get(LAST_PROFILE_KEY)
        if remembered in existing:
            return remembered
        if "packsmith_test" in existing:
            return "packsmith_test"
        return existing[0]

    def _enter_profile(self, name: str):
        """Open a profile and build the window around it."""
        self._load_profile(name)
        if self._blocked:
            self._build_blocked_shell(self._blocked)
            return
        self._seed_tags()
        self._seed_views()
        self._seed_packages()
        self._seed_jobs()
        self._build_shell()
        # Into the Logs tab as well as the file: opening a profile is the single most
        # consequential thing that happens at launch — it decides which registry, which
        # database and which instance everything else is about — and until now the only
        # trace of it was in the log file nobody has open.
        remembered = load_ui_state(name).get("last_job")
        self._run_control.set_jobs(self._jobs.all(), readiness=self._job_problems,
                                   select=remembered)
        self._bottom.log(f"Profile '{name}' loaded")
        self._remember_profile(name)
        self._report_import(self._import_result, initial=True)

    @staticmethod
    def _remember_profile(name: str):
        """Record the profile for next launch (§4.1).

        Written on ENTRY rather than on exit, so a crash or a force-quit still leaves the
        right breadcrumb — the whole point is surviving an untidy shutdown.
        """
        if name:
            save_state(**{LAST_PROFILE_KEY: name})

    def _load_profile(self, name: str):
        self._blocked = None
        if not name:
            self._profile = None
            self._packdump = None
            self._import_result = None
            self._blocked = (
                "Packsmith needs a profile before it can do anything.\n\n"
                "A profile points at one Minecraft instance. Create one from the Profiles "
                "menu, then launch the game once so the Packsmith mod writes its packdump.")
            return
        self._profile = Profile.load(name)
        # Auto-import, always — a stale registry is the worse failure, because it produces
        # confidently wrong output that looks fine, while an unwanted import announces
        # itself the moment you look at anything. What is *not* automatic is adopting a
        # dump that fails the profile's contract; see _report_import.
        # Auto-adopt is the DEFAULT, not a hardcoded truth: `auto_adopt_enabled` is the
        # one place a settings menu will flip. With it off, the dump is only inspected —
        # nothing changes until the user blesses it from the Packdump tab.
        self._import_result = (import_packdump(self._profile)
                               if auto_adopt_enabled(self._profile)
                               else check_packdump(self._profile))
        self._packdump = self._import_result.packdump or current_packdump(self._profile)
        if self._packdump is None:
            # §3.1 gates the app on the first packdump; it does not crash it. Raising here
            # propagated straight out of __init__ at first launch, so the user got a console
            # traceback and no window — indistinguishable from the app being broken.
            self._blocked = (
                f"Profile '{self._profile.name}' has no packdump yet.\n\n"
                f"Launch Minecraft once with the Packsmith mod installed so it can write "
                f"one to:\n{self._profile.mc_path}\n\n"
                f"{self._import_result.reason or ''}").strip()
            return
        self._db = UserDB(self._profile.root / "profile.db")
        self._tags = TagStore(self._db)
        self._packages = PackageIndex(self._profile.packages_dir)
        self._file_store = FileStore(self._db, self._profile.mc_path)
        self._history = StepRunStore(self._db)
        self._job_history = JobRunStore(self._db)
        self._views = ViewStore(self._db)
        self._jobs = JobStore(self._db)
        self._blueprints = BlueprintStore(self._db, packdump=self._packdump)

    # --- shell -------------------------------------------------------------

    def _build_blocked_shell(self, reason: str):
        """A window that explains what's missing instead of a console traceback.

        §3.1: Packsmith "won't let you do anything until it loads its first packdump" —
        gated, not absent. The menu bar stays live so the one thing that can fix this
        (Profiles) is reachable; everything that needs a registry simply isn't built.
        """
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(48, 48, 48, 48)
        lay.addStretch()

        title = QLabel("Nothing to work on yet")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"color: {style.TEXT}; font-size: 18px; font-weight: bold;")
        lay.addWidget(title)

        body = QLabel(reason)
        body.setAlignment(Qt.AlignCenter)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 12px;")
        lay.addWidget(body)

        row = QHBoxLayout()
        row.addStretch()
        again = QPushButton("Check again")
        again.clicked.connect(lambda: self._enter_profile(self._default_profile()))
        row.addWidget(again)
        profiles = QPushButton("Profiles…")
        profiles.clicked.connect(self._open_profile)
        row.addWidget(profiles)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch()

        self.setCentralWidget(page)
        self.statusBar().showMessage("No packdump loaded")

    def _build_shell(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        self._banner = PackdumpBanner()
        self._banner.force_requested.connect(self._force_import)
        root.addWidget(self._banner)

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)

        # Icon strip: fixed furniture at the window edge, always visible.
        self._sidebar = Sidebar()
        self._sidebar.panel_selected.connect(self._show_panel)
        self._sidebar.collapsed.connect(self._collapse_panel)
        content.addWidget(self._sidebar)

        # All seven panels are live now. This said "three" for a long time after the other
        # four grew backends, which is the kind of comment that quietly teaches you the
        # wrong shape of your own app.
        self._views_panel = ViewsPanel(self._views.all())
        self._views_panel.view_activated.connect(self._open_view)
        self._views_panel.new_view_requested.connect(self._new_view)
        self._views_panel.edit_requested.connect(self._edit_view)
        self._views_panel.layout_changed.connect(self._remember_view_layout)
        self._views_panel.rename_requested.connect(self._rename_view)
        self._views_panel.delete_requested.connect(self._delete_view)

        # `None` when the profile has never said, which is what makes the panel fall back
        # to its defaults — an empty saved list means the user unpinned everything and must
        # not be handed the defaults back.
        self._registry_panel = RegistryPanel(
            self._packdump,
            pins=load_ui_state(self._profile.name if self._profile else "")
            .get("registry_pins"))
        self._registry_panel.registry_activated.connect(self._open_browse)
        self._registry_panel.pins_changed.connect(self._remember_registry_pins)

        self._tags_panel = TagsPanel(self._tags)
        self._tags_panel.tag_activated.connect(self._open_tag_view)
        self._tags_panel.new_tag_requested.connect(self._new_tag)
        self._tags_panel.delete_tag_requested.connect(self._delete_tag)
        self._tags_panel.edit_values_requested.connect(self._edit_enum_values)
        self._tags_panel.rename_tag_requested.connect(self._rename_tag)

        self._jobs_panel = JobsPanel(self._jobs.all(), readiness=self._job_problems)
        self._jobs_panel.job_activated.connect(self._open_job_editor)
        self._jobs_panel.run_requested.connect(self._run_job)
        self._jobs_panel.dry_run_requested.connect(self._dry_run_job)
        self._jobs_panel.new_job_requested.connect(self._new_job)
        self._jobs_panel.rename_requested.connect(self._rename_job)
        self._jobs_panel.delete_requested.connect(self._delete_job)
        self._jobs_panel.pin_toggled.connect(self._toggle_pin)

        # §8.1: the loader detected in this pack, or None. It is what gives Smart Mode
        # its categories — with no loader mod there are no global datapacks to categorise.
        self._loaders = self._resolve_loaders()
        active_loader = self._loaders.provider_for(DATAPACKS_WRITE)
        self._files_panel = FilesPanel(self._file_store, loader=active_loader)
        self._files_panel._mc_version = self._profile.mc_version
        # The real client jar, when this launcher's layout is one Packsmith knows or the
        # user has pointed at it. Used for `pack_format` today (Mojang's own number rather
        # than a memorised one) and for browsing vanilla data later (§3.1).
        self._client_jar = locate_for(self._profile)
        self._files_panel._client_jar = self._client_jar.path if self._client_jar else None
        self._files_panel.ownership_changed.connect(self._file_ownership_changed)
        self._files_panel.file_activated.connect(self._open_file)

        self._blueprints_panel = BlueprintsPanel(self._blueprints)
        self._blueprints_panel.blueprint_activated.connect(self._open_blueprint)
        self._blueprints_panel.new_blueprint_requested.connect(self._new_blueprint)
        self._blueprints_panel.delete_blueprint_requested.connect(self._delete_blueprint)
        self._blueprints_panel.rename_blueprint_requested.connect(self._rename_blueprint)
        self._blueprints_panel.new_instance_requested.connect(self._new_instance)
        self._blueprints_panel.delete_instance_requested.connect(self._delete_instance)
        self._blueprints_panel.rename_instance_requested.connect(self._rename_instance)

        self._actions_panel = ActionsPanel(self._packages)
        self._actions_panel.action_activated.connect(self._open_action)
        self._actions_panel.document_activated.connect(self._open_package_document)
        self._actions_panel.new_action_requested.connect(self._new_action)
        self._actions_panel.remove_action_requested.connect(self._remove_action)

        # The same operations as the Actions panel offers, scoped to one package. They
        # share handlers deliberately: two surfaces onto the same acts, one implementation.
        self._packages_panel = PackagesPanel(self._packages)
        self._packages_panel.action_activated.connect(self._open_action)
        self._packages_panel.document_activated.connect(self._open_package_document)
        self._packages_panel.new_package_requested.connect(self._new_package)
        self._packages_panel.new_action_requested.connect(
            lambda name: self._new_action(name, fixed=True))
        self._packages_panel.new_file_requested.connect(
            lambda name, folder: self._new_package_file(name, folder, fixed=True))
        self._packages_panel.new_folder_requested.connect(
            lambda name, folder: self._new_package_folder(name, folder, fixed=True))
        self._packages_panel.rename_file_requested.connect(self._rename_package_file)
        self._packages_panel.delete_file_requested.connect(self._delete_package_file)
        self._packages_panel.rename_folder_requested.connect(self._rename_package_folder)
        self._packages_panel.delete_folder_requested.connect(self._delete_package_folder)

        # Jobs, Actions and Packages share one slot (§4.1). Every panel is built exactly as
        # before — the signals above are untouched — and the wrapper only decides where
        # they sit.
        self._automation_panel = AutomationPanel(
            self._jobs_panel, self._actions_panel, self._packages_panel)
        self._automation_panel.show_tab(
            load_ui_state(self._profile.name if self._profile else "")
            .get("automation_tab", "jobs"))
        self._automation_panel._tabs.currentChanged.connect(
            lambda _: self._remember_automation_tab())

        live = {
            "views": self._views_panel,
            "registry": self._registry_panel,
            "tags": self._tags_panel,
            "files": self._files_panel,
            "automation": self._automation_panel,
            "blueprints": self._blueprints_panel,
        }
        panels = {
            spec.key: live.get(spec.key) or StubPanel(spec.title, spec.description)
            for spec in PANEL_SPECS
        }
        # How each live panel reloads itself from its store (see _show_panel).
        self._panel_reloaders = {
            "views": self._reload_views,
            "tags": self._tags_panel.refresh,
            "files": self._files_panel.refresh,
            "registry": self._registry_panel.refresh,
            # Reloads BOTH tabs: the stack keeps hidden widgets alive, so refreshing only
            # the visible one leaves the other sitting on stale data.
            "automation": self._reload_automation,
            "blueprints": self._blueprints_panel.refresh,
        }

        self._panel_stack = PanelStack(panels)
        self._panel_stack.setMinimumWidth(160)

        self._workspace = Workspace()
        self._workspace.tab_closed.connect(self._on_tab_closed)
        self._workspace.tab_activated.connect(self._on_tab_activated)
        self._workspace.close_guard = self._may_close_tab

        # One Monaco for every editor tab (design 4.2 — measured: per-tab views cost a
        # Chromium process and ~119MB each). It also OUTLIVES a profile switch: recreating
        # a QWebEngineView means paying Chromium startup again and re-entering the
        # widget-lifetime problems the shared-view design already solved, so a switch
        # rebinds its sources instead of rebuilding it.
        sources = {
            # Instance files answer to §6.1 ownership; package sources answer to
            # provenance. Different worlds, deliberately different rules.
            "instance": InstanceFileSource(self._file_store, on_claim=self._on_file_claimed),
            "package": PackageFileSource(self._packages, self._profile.packages_dir),
            # Members of jars in the instance. Read-only by provenance (§6.5) — the source
            # itself refuses writes, so the editor needs no special case.
            "jar": JarMemberSource(self._file_store.root),
        }
        if self._editor_host is None:
            self._editor_host = EditorHost(sources, container=self)
            self._editor_host.dirty_changed.connect(self._on_editor_dirty)
            self._editor_host.file_saved.connect(self._on_document_saved)
            self._editor_host.save_failed.connect(
                lambda key, why: QMessageBox.warning(self, "Couldn't save",
                                                     f"{key}\n\n{why}"))
            self._editor_host.edit_blocked.connect(self._offer_unlock)
            self._editor_host.unlocked.connect(self._on_unlocked)
        else:
            self._editor_host.rebind(sources)

        self._bottom = BottomPanel()
        results = JobResultsView(self._history, self._job_history,
                                 tag_store=self._tags, file_store=self._file_store,
                                 blueprint_store=self._blueprints)
        results.rolled_back.connect(self._on_rolled_back)
        results.report_requested.connect(self.open_run_report)
        self._bottom.set_panel("job_results", results)
        errors = ErrorsView(self._tags, self._packdump,
                            job_store=self._jobs, package_index=self._packages,
                            blueprint_store=self._blueprints)
        errors.resolved.connect(self._on_orphans_resolved)
        self._bottom.set_panel("errors", errors)

        packdump_view = PackdumpView()
        packdump_view.open_diff_requested.connect(self._open_packdump_diff)
        packdump_view.snapshot_diff_requested.connect(self._open_snapshot_diff)
        packdump_view.snapshot_vs_active_requested.connect(
            self._open_snapshot_vs_active)
        packdump_view.bless_requested.connect(self._bless_packdump)
        packdump_view.revert_requested.connect(self._revert_packdump)
        packdump_view.status.connect(self._set_status)
        self._bottom.set_panel("packdump", packdump_view)
        self._refresh_packdump_panel()

        vertical = QSplitter(Qt.Vertical)
        vertical.setHandleWidth(style.SPLITTER_WIDTH)
        vertical.setStyleSheet(style.SPLITTER_QSS)
        vertical.addWidget(self._workspace)
        vertical.addWidget(self._bottom)
        vertical.setStretchFactor(0, 1)
        vertical.setStretchFactor(1, 0)
        vertical.setCollapsible(1, False)
        vertical.setSizes([1000, self._bottom.collapsed_height()])
        # A splitter told to hold the panel at its collapsed height will keep doing so
        # forever, so opening a tab would show a sliver rather than a panel. The panel says
        # when it opens and shuts; the window, which owns the splitter, does the resizing.
        self._bottom.expanded_changed.connect(
            lambda opened, s=vertical: self._resize_bottom(s, opened))
        # Dragging the divider IS how the height is set — there is no dialog for it — so the
        # drag has to be what gets remembered.
        self._bottom_height = load_ui_state(
            self._profile.name if self._profile else "").get("bottom_height") or 0
        self._bottom_save = QTimer(self)
        self._bottom_save.setSingleShot(True)
        self._bottom_save.setInterval(500)
        self._bottom_save.timeout.connect(self._save_bottom_height)
        vertical.splitterMoved.connect(
            lambda _pos, _i, s=vertical: self._on_bottom_dragged(s))
        # The panel collapses during its own construction, before that signal existed, so
        # the handle starts out live over an unresizable panel unless it is synced once.
        self._resize_bottom(vertical, self._bottom.is_expanded)

        horizontal = QSplitter(Qt.Horizontal)
        horizontal.setHandleWidth(style.SPLITTER_WIDTH)
        horizontal.setStyleSheet(style.SPLITTER_QSS)
        horizontal.addWidget(self._panel_stack)
        horizontal.addWidget(vertical)
        horizontal.setStretchFactor(0, 0)
        horizontal.setStretchFactor(1, 1)
        horizontal.setSizes([style.SIDEBAR_PANEL_WIDTH, 1000])
        self._h_split = horizontal

        content.addWidget(horizontal)
        root.addLayout(content)

        self._sidebar.select("views")

        # Once only: shortcuts are parented to the window, not the central widget, so a
        # rebuild on profile switch would stack duplicates and make each one ambiguous.
        if not self._shortcuts:
            self._shortcuts = [
                QShortcut(QKeySequence.Undo, self, activated=self._undo),
                QShortcut(QKeySequence.Redo, self, activated=self._redo),
                QShortcut(QKeySequence("Ctrl+`"), self,
                          activated=lambda: self._bottom.toggle()),
                QShortcut(QKeySequence("Ctrl+R"), self,
                          activated=self._run_selected_job),
                QShortcut(QKeySequence("Ctrl+Shift+R"), self,
                          activated=lambda: self._run_selected_job(dry_run=True)),
            ]

    def _resize_bottom(self, splitter, opened: bool):
        """Give the bottom panel room when it opens, and take it back when it shuts.

        The height is remembered — per profile, across sessions — rather than snapping back
        to a default each time. It is never *set* anywhere: you drag the panel and that is
        the setting. Somewhere to configure it would be worse than the dragging.
        """
        total = sum(splitter.sizes())
        # The handle follows the panel: draggable only while there is content to resize.
        # The height cap already makes a collapsed drag do nothing, but a live handle over
        # a dead divider still offers a resize cursor and invites the attempt.
        handle = splitter.handle(1)
        if handle is not None:
            handle.setEnabled(opened)
            handle.setCursor(Qt.SplitVCursor if opened else Qt.ArrowCursor)
        if not opened:
            self._remember_bottom_height(splitter)
            splitter.setSizes([total - self._bottom.collapsed_height(),
                               self._bottom.collapsed_height()])
            return
        wanted = self._bottom_height or self._bottom.expanded_height()
        # Never taller than the window can spare, never shorter than a sliver — a
        # remembered height from a bigger monitor must not swallow the workspace.
        wanted = max(self._bottom.minimum_expanded_height(),
                     min(wanted, max(total - 200, self._bottom.minimum_expanded_height())))
        splitter.setSizes([total - wanted, wanted])

    def _on_bottom_dragged(self, splitter):
        if not self._bottom.is_expanded:
            return          # dragging a shut panel is not a height worth keeping
        self._remember_bottom_height(splitter)
        self._bottom_save.start()

    def _remember_bottom_height(self, splitter):
        """Capture the current height, if the panel is actually open enough to have one."""
        height = splitter.sizes()[1]
        if height > self._bottom.collapsed_height():
            self._bottom_height = height

    def _save_bottom_height(self):
        """Persist the dragged height for this profile.

        Debounced: `splitterMoved` fires on every mouse-move of a drag, and writing the
        state file on each of them would be dozens of writes a second for one gesture.
        """
        if self._profile is not None and self._bottom_height:
            save_ui_state(self._profile.name, bottom_height=int(self._bottom_height))

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setStyleSheet(
            f"background: {style.BG_PANEL}; border-bottom: 1px solid {style.BORDER};")
        lay = QHBoxLayout(header)
        lay.setContentsMargins(10, 6, 10, 6)

        name = QLabel(f"{self._profile.name}")
        name.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {style.TEXT};")

        # Kept, because adopting a dump rewrites it in place: the mod count in the header is
        # the first thing that would silently disagree with the pack after an import.
        self._header_info = QLabel()
        self._header_info.setStyleSheet(f"font-size: 12px; color: {style.TEXT_MUTED};")
        self._refresh_header()

        # The run control sits between the profile name and the pack facts, in the header's
        # otherwise empty middle. §3.3.2 calls jobs "rarely edited but constantly re-run",
        # and an IDE answers that with a toolbar control rather than by making you dock a
        # panel to press play. See gui/run_control.py.
        self._run_control = RunControl()
        self._run_control.run_requested.connect(self._run_job)
        self._run_control.dry_run_requested.connect(self._dry_run_job)

        lay.addWidget(name)
        lay.addStretch()
        lay.addWidget(self._run_control)
        lay.addSpacing(14)
        lay.addWidget(self._header_info)
        return header

    def _refresh_header(self):
        label = getattr(self, "_header_info", None)
        if label is None or self._packdump is None:
            return
        label.setText(
            f"{self._packdump.mc_version}  {self._profile.loader} "
            f"{self._profile.loader_version}  |  {len(self._packdump.mods)} mods")

    # --- blueprints --------------------------------------------------------

    def _open_blueprint(self, name, instance=""):
        """Panel B: an ephemeral view over the whole blueprint — the same gesture as
        clicking a tag, and savable the same way.

        Clicking an *instance* opens the same tab, because a one-instance tab would be a
        worse view of a blueprint than the grid. What it additionally does is land on that
        instance's row instead of on row 0."""
        tab = self._open_blueprint_tab(("blueprint", name), name, blueprint_query(name))
        if instance and not tab.focus_instance(instance):
            self._set_status(f"'{instance}' isn't in this view of {name}")
        return tab

    def _open_blueprint_tab(self, key, title, query, view=None):
        existing = self._open_tabs.get(key)
        if existing is not None and self._workspace.focus_widget(existing):
            return existing
        tab = BlueprintEditorTab(query, self._blueprints, packdump=self._packdump,
                                 view=view,
                                 on_config_changed=lambda c, v=view:
                                     self._save_renderer_config(v, c),
                                 job_impact=lambda projected, mutation, q=query:
                                     self._schema_change_impact(q.scope.name, projected,
                                                                mutation))
        tab.changed.connect(self._after_blueprint_change)
        tab.status.connect(self._set_status)
        tab.save_requested.connect(lambda t=tab: self._save_blueprint_view(t))
        # The blueprint mark rather than the View one, even when this IS a saved View: what
        # you are looking at is a blueprint's grid, and that is the more specific true
        # thing. The Views panel is where a View's *identity* is legible; a tab is where
        # its *content* is.
        self._workspace.add_tab(tab, title,
                                icon=icons.concept_icon("blueprints", colour=style.TEXT))
        self._open_tabs[key] = tab
        return tab

    def _schema_change_impact(self, blueprint, projected_slots, mutation) -> str:
        """What a proposed schema change would break, named (design 3.2.2).

        The blueprint editor asks; the answer needs jobs and installed packages, which are
        this window's to hold. Returns "" when nothing breaks, so the caller can treat it
        as "is there anything to warn about".
        """
        return summarise(
            broken_steps(blueprint, projected_slots,
                         job_store=self._jobs, package_index=self._packages,
                         blueprint_store=self._blueprints),
            mutation=mutation)

    def _save_renderer_config(self, view, config):
        """Renderer config belongs to the View. Nowhere to put it until the tab is saved,
        which is why an ephemeral tab just holds it in memory."""
        if view is not None:
            self._views.set_renderer_config(view.id, config)

    def _save_blueprint_view(self, tab):
        """Turn an ephemeral blueprint tab into a saved View — §3.2.3's "filter bar ->
        save" path, for the renderer that isn't a table."""
        if tab.view is not None:
            self._save_renderer_config(tab.view, tab.renderer_config())
            self._set_status(f"Saved '{tab.view.name}'")
            return
        name, ok = QInputDialog.getText(self, "Save View", "Name this view:",
                                        text=tab.blueprint_name)
        if not ok or not name.strip():
            return
        view = self._views.create(name.strip(), tab.query, renderer="blueprint_grid",
                                  renderer_config=tab.renderer_config())
        tab.view = view
        tab._on_config_changed = lambda c, v=view: self._save_renderer_config(v, c)
        self._open_tabs.pop(("blueprint", tab.blueprint_name), None)
        self._open_tabs[("view", view.id)] = tab
        index = self._workspace._tabs.indexOf(tab)
        if index >= 0:
            self._workspace._tabs.setTabText(index, view.name)
        self._reload_views()
        self._set_status(f"Saved view '{view.name}'")

    def _after_blueprint_change(self):
        self._blueprints_panel.refresh()
        errors = self._bottom.panel("errors")
        if errors is not None:
            errors.refresh()

    def _new_blueprint(self):
        dialog = NewBlueprintDialog(parent=self)
        if not dialog.exec():
            return
        if not self._blueprint_op(
                lambda: self._blueprints.define(dialog.result_name,
                                                dialog.result_description)):
            return
        self._open_blueprint(dialog.result_name)

    def _rename_blueprint(self, name):
        new_name, ok = QInputDialog.getText(self, "Rename Blueprint", "New name:",
                                            text=name)
        if not ok or not new_name.strip() or new_name.strip() == name:
            return
        new_name = new_name.strip()
        if self._blueprint_op(lambda: self._blueprints.rename(name, new_name)):
            self._repoint_blueprint_views(name, new_name)
            self._retitle_blueprint_tab(name, new_name)

    def _repoint_blueprint_views(self, old, new):
        """A saved query names its blueprint, so a rename has to follow it into every view.

        Blueprint identity is the surrogate id and the name is only a label (3.2.2), which
        is what makes rename cheap in the store — but a query says `Blueprint("StoneType")`
        because a query is meant to be readable. This is the price of that, paid once.
        """
        for view in self._views.all():
            scope = getattr(view.query, "scope", None)
            if isinstance(scope, QueryBlueprint) and scope.name == old:
                self._views.update_query(
                    view.id, dataclasses.replace(view.query, scope=QueryBlueprint(new)))

    def _retitle_blueprint_tab(self, old, new):
        for key, tab in list(self._open_tabs.items()):
            if not isinstance(tab, BlueprintEditorTab) or tab.blueprint_name != old:
                continue
            tab.blueprint_name = new
            tab.query = dataclasses.replace(tab.query, scope=QueryBlueprint(new))
            tab.reload()
            index = self._workspace._tabs.indexOf(tab)
            # A saved view keeps its own name; only an unnamed tab is titled by its
            # blueprint, so only that one gets retitled.
            if index >= 0 and tab.view is None:
                self._workspace._tabs.setTabText(index, new)
            if key == ("blueprint", old):
                self._open_tabs.pop(key, None)
                self._open_tabs[("blueprint", new)] = tab

    def _delete_blueprint(self, name):
        instances = len(self._blueprints.instances(name))
        if QMessageBox.question(
                self, "Delete blueprint",
                f"Delete the blueprint '{name}'?\n\nThis removes its shape and all "
                f"{instances} instance(s) with their bindings.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        views = [v for v in self._views.all()
                 if isinstance(getattr(v.query, "scope", None), QueryBlueprint)
                 and v.query.scope.name == name]
        if views and QMessageBox.question(
                self, "Views point at this blueprint",
                f"{len(views)} saved view(s) render '{name}': "
                f"{', '.join(v.name for v in views)}.\n\nDelete them too?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        if not self._blueprint_op(lambda: self._blueprints.delete(name)):
            return
        for view in views:
            self._views.delete(view.id)
        for key, tab in list(self._open_tabs.items()):
            if isinstance(tab, BlueprintEditorTab) and tab.blueprint_name == name:
                index = self._workspace._tabs.indexOf(tab)
                if index >= 0:
                    self._workspace._close_tab(index)
        self._reload_views()

    def _new_instance(self, blueprint):
        name, ok = QInputDialog.getText(self, "New Instance",
                                        f"Name of the new {blueprint}:")
        if not ok or not name.strip():
            return
        if self._blueprint_op(
                lambda: self._blueprints.create_instance(blueprint, name.strip())):
            self._open_blueprint(blueprint)

    def _rename_instance(self, blueprint, instance):
        name, ok = QInputDialog.getText(self, "Rename Instance", "New name:",
                                        text=instance)
        if not ok or not name.strip():
            return
        self._blueprint_op(
            lambda: self._blueprints.rename_instance(blueprint, instance, name.strip()))

    def _delete_instance(self, blueprint, instance):
        if QMessageBox.question(
                self, "Delete instance",
                f"Delete '{blueprint}:{instance}' and all its bindings?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._blueprint_op(
            lambda: self._blueprints.delete_instance(blueprint, instance))

    def _blueprint_op(self, operation) -> bool:
        try:
            operation()
        except BlueprintError as e:
            QMessageBox.warning(self, "Can't do that", str(e))
            return False
        self._blueprints_panel.refresh()
        self._reload_blueprint_tabs()
        self._after_blueprint_change()
        return True

    def _reload_job_tabs(self):
        """Re-read every open job editor from the store.

        Same blind spot `_reload_blueprint_tabs` exists for: a job tab renders itself
        rather than going through `_tab_models`, so refreshing the models leaves it
        showing whatever was true when it opened. That matters more now that the tab flags
        unrunnable steps — deleting a tag would grey a job in the sidebar while its open
        tab still showed the step as perfectly fine, which is a worse state than not
        flagging at all: two surfaces disagreeing about the same fact.
        """
        for (kind, _key), tab in list(self._open_tabs.items()):
            if kind == "job" and isinstance(tab, JobEditorTab):
                try:
                    tab.refresh()
                except RuntimeError:
                    pass        # the tab was closed while we walked

    def _reload_blueprint_tabs(self):
        """Re-read every open blueprint view from the store.

        Blueprint tabs render themselves rather than going through `_tab_models`, so the
        model-level refreshes miss them entirely — which is why an action that filled in
        bindings left the open grid looking empty until something else forced a redraw.
        """
        for (kind, key), tab in list(self._open_tabs.items()):
            if kind in ("blueprint", "view") and isinstance(tab, BlueprintEditorTab):
                try:
                    tab.reload()
                except BlueprintError:
                    pass          # its blueprint was just deleted; the tab is closing

    # --- profiles ----------------------------------------------------------
    #
    # A profile is a different world, not a filter: every service, panel and open tab is
    # scoped to one. So switching REBUILDS the window rather than re-pointing nine services
    # through seven panels and N tabs — which would leave stale references to be found one
    # at a time, in use, for weeks. The single exception is the Monaco host (see
    # _build_shell).

    def _switch_profile(self, name: str):
        # From the blocked shell there is no current profile to compare against, nothing
        # open to close and no database to shut — and entering the new one is the entire
        # point of having just created it.
        previous = self._profile.name if self._profile else None
        if name == previous:
            return
        if not self._close_all_tabs():
            return                              # a dirty buffer said no
        try:
            if getattr(self, "_db", None) is not None:
                self._db.close()
        except Exception:                       # closing is best-effort; the switch isn't
            log.warning("Could not close the previous profile's database", exc_info=True)
        try:
            self._enter_profile(name)
        except Exception as e:
            QMessageBox.critical(self, "Couldn't open profile",
                                 f"{name}\n\n{e}\n\nStaying where we were.")
            self._enter_profile(self._profile.name)
            return
        self.setWindowTitle(f"Packsmith — {name}")

    def _close_all_tabs(self) -> bool:
        """Close every open tab, honouring the unsaved-changes guard. False if cancelled.

        The blocked shell never builds a workspace, so "close everything" is vacuously
        done — this is reached by the profile switch that is the way OUT of that state.
        """
        if getattr(self, "_workspace", None) is None:
            return True
        tabs = self._workspace._tabs
        while tabs.count():
            before = tabs.count()
            self._workspace._close_tab(0)
            if tabs.count() == before:
                return False
        return True

    def _open_profile(self):
        dialog = OpenProfileDialog(
            current=self._profile.name if self._profile else None, parent=self)
        dialog.exec()
        if dialog.result_name:
            self._switch_profile(dialog.result_name)
        elif dialog.result_deleted:
            self._set_status(f"Deleted {', '.join(dialog.result_deleted)}")

    def _new_profile(self):
        dialog = NewProfileDialog(parent=self)
        if not dialog.exec():
            return
        try:
            Profile.create(
                dialog.result_name, mc_path=dialog.result_mc_path,
                mc_version=dialog.result_mc_version or None,
                loader=dialog.result_loader or None,
                loader_version=dialog.result_loader_version or None,
                mc_version_policy=dialog.result_policy,
            )
        except Exception as e:
            QMessageBox.warning(self, "Couldn't create profile", str(e))
            return
        self._switch_profile(dialog.result_name)

    # --- packdump ----------------------------------------------------------

    # --- packdump review (design 3.1 / 4.1) --------------------------------

    def _packdump_summary(self):
        """The last comparison, normalised into added/removed terms."""
        result = getattr(self, "_import_result", None)
        return summarise_diff(result.diff) if result is not None else None

    def _packdump_at_risk(self):
        """Assignments the ACTIVE dump orphans. Computed against what is in effect, so it
        is a report when auto-adopt is on and a warning when it isn't."""
        try:
            return tags_at_risk(self._tags, self._packdump) if self._packdump else []
        except Exception:
            return []

    def _refresh_packdump_panel(self):
        view = self._bottom.panel("packdump")
        if view is None or not isinstance(view, PackdumpView):
            return
        result = getattr(self, "_import_result", None)
        snapshots = []
        for entry in list_snapshots(self._profile):
            snapshots.append({**entry, "name": entry["path"].name})
        view.show_state(
            summary=self._packdump_summary(),
            at_risk=self._packdump_at_risk(),
            # Pending only ever happens with auto-adopt turned off: the dump differs and
            # nothing has been written.
            pending=bool(result is not None and result.status == "changed"),
            snapshots=snapshots)

    def _open_packdump_diff(self):
        """Open the comparison as its own tab.

        The strip says *that* something changed; this is where you read *what*. A real
        update to a 300-mod pack moves thousands of entries, which is not a thing a few
        rows of bottom panel can show.
        """
        summary = self._packdump_summary()
        if summary is None:
            self._set_status("No packdump comparison to show")
            return
        key = ("packdump-diff", "latest")
        existing = self._open_tabs.get(key)
        if existing is not None and self._workspace.focus_widget(existing):
            return existing
        tab = PackdumpDiffTab(summary, at_risk=self._packdump_at_risk())
        tab.status.connect(self._set_status)
        self._workspace.add_tab(tab, "Packdump changes")
        self._open_tabs[key] = tab
        return tab

    def _open_snapshot_diff(self, snapshot_name):
        """What this snapshot changed, against the snapshot before it."""
        previous = previous_snapshot(self._profile, snapshot_name)
        if previous is None:
            self._set_status(
                f"{snapshot_name} is the oldest snapshot kept — there is nothing before "
                f"it to compare against")
            return
        return self._open_diff_between(previous["name"], snapshot_name)

    def _open_snapshot_vs_active(self, snapshot_name):
        """What has changed between this snapshot and the dump in effect now.

        Usually the reason you were looking at history in the first place: not "what did
        that one update do" but "what have I gained and lost since then".
        """
        if snapshot_name == ACTIVE_SNAPSHOT:
            self._set_status("That snapshot is the active dump")
            return
        return self._open_diff_between(snapshot_name, ACTIVE_SNAPSHOT)

    def _open_diff_between(self, older_name, newer_name):
        """Open the diff between two stored snapshots.

        One implementation for every pair, because the direction is the only thing that can
        be wrong here and it should be decided in exactly one place. The tab is the same one
        an import opens — "what did this change" is the same question whenever it is asked.
        """
        try:
            diff = compare_snapshots(self._profile, older_name, newer_name)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "Compare snapshots", str(e))
            return

        key = ("packdump-diff", f"{older_name}->{newer_name}")
        existing = self._open_tabs.get(key)
        if existing is not None and self._workspace.focus_widget(existing):
            return existing
        summary = summarise_diff(diff)
        tab = PackdumpDiffTab(summary, title=f"{older_name} → {newer_name}")
        tab.status.connect(self._set_status)
        # Named for the pair: several of these can be open at once, and three tabs all
        # called "Packdump changes" tell you nothing about which is which.
        label = "now" if newer_name == ACTIVE_SNAPSHOT else newer_name[:10]
        self._workspace.add_tab(tab, f"{older_name[:10]} → {label}")
        self._open_tabs[key] = tab
        self._set_status(f"{older_name} → {label}: {summary.headline()}")
        return tab

    def _bless_packdump(self):
        """Adopt a dump that was held for review (auto-adopt off)."""
        self._import_result = import_packdump(self._profile)
        if self._import_result.status == "imported":
            self._adopt_packdump(self._import_result.packdump)
            return
        self._report_import(self._import_result)
        self._refresh_packdump_panel()

    def _revert_packdump(self, snapshot_name):
        """Go back to an archived snapshot (design 3.1's review workflow).

        Loud, because it changes what every view reads — but not destructive: the dump it
        replaces is archived first, so this is revertible in its own right.
        """
        if QMessageBox.question(
                self, "Revert packdump",
                f"Make snapshot '{snapshot_name}' the active packdump?\n\n"
                f"Your tags, blueprints and jobs are untouched — but anything referring to "
                f"entries that only exist in the newer dump will read as orphaned until "
                f"you come back.\n\n"
                f"The dump you are replacing is archived, so this is undoable.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        result = revert_to_snapshot(self._profile, snapshot_name)
        if result.status != "imported":
            QMessageBox.warning(self, "Revert packdump", result.reason or "Could not revert")
            return
        self._import_result = result
        self._adopt_packdump(result.packdump)
        self._set_status(f"Reverted to packdump snapshot {snapshot_name}")

    def _report_import(self, result, *, initial=False):
        """Be loud in proportion to what the import did to the user's DATA.

        Importing itself is never the risky part — Layer 1 is read-only, so adopting a dump
        modifies nothing the user owns. What changes is how their Layer 2 data reads
        against it, so that is what decides the volume.
        """
        self._banner.show_result(result)
        if result is None:
            return

        if result.status == "refused":
            self._set_status("A newer packdump was refused — see the banner")
            return
        if result.status == "unreadable":
            self._set_status(f"Packdump unreadable: {result.reason}")
            return
        if result.status == "missing":
            self._set_status(f"No packdump at {self._profile.mc_path}")
            return
        if result.status == "unchanged":
            if initial:
                items = len(self._packdump.registry.get("minecraft:item", {})
                            .get("values", []))
                self._set_status(f"{items} items in minecraft:item")
            return

        added, removed = result.registry_delta()
        mods_added, mods_removed = result.mod_delta()
        summary = (f"Packdump updated — {added:+d}/{-removed:+d} entries, "
                   f"{mods_added:+d}/{-mods_removed:+d} mods")
        if result.adopted:
            summary += f" (adopted {', '.join(sorted(result.adopted))})"
        if result.forced:
            summary = "FORCED — " + summary
        self._bottom.log(summary)
        self._set_status(summary)

        # Fallout, not the fact of the import, is what earns an interruption. Orphans mean
        # assignments now point at entries this pack no longer has.
        errors = self._bottom.panel("errors")
        if errors is not None:
            errors.refresh()
            if errors.has_problems():
                # Was `show_panel("errors")` followed by `expand(1)` — and index 1 is Job
                # Results, so surfacing the errors immediately switched away from them.
                self._bottom.show_panel("errors")

    def _force_import(self):
        result = self._import_result
        if result is None or not result.errors:
            return
        actual = result.errors.get("mc_version", {}).get("actual") \
            or result.errors.get("loader", {}).get("actual", "")
        expected = result.errors.get("mc_version", {}).get("expected", "")
        if not confirm_force_import(self, expected, actual, result.errors):
            return
        self._import_result = import_packdump(self._profile, force=True)
        if self._import_result.status != "imported":
            self._report_import(self._import_result)
            return
        # The registry underneath everything just changed; every holder has to be re-pointed.
        self._adopt_packdump(self._import_result.packdump)

    # --- adopting a new packdump (design 3.1) ------------------------------
    #
    # A packdump change is NOT a profile change. Layer 2 — tags, blueprints, views, jobs —
    # lives in the profile database, and a new registry does not touch a byte of it. So the
    # database never needs closing and the workspace never needs tearing down: what changes
    # is one Layer 1 object, read by a countable set of holders.
    #
    # This matters because the import fires on window focus, which is exactly when the user
    # comes back from playtesting — and a mod the launcher updated on its own is enough to
    # trigger it. Charging them their whole workspace for that is a punishment for the loop
    # Packsmith exists to support.
    #
    # **THE RULE: anything that stores a packdump implements `set_packdump`, and is reached
    # by `_packdump_holders`.** A holder that forgets goes silently stale — it keeps
    # answering from a registry the game no longer has, which is precisely the failure §3.1
    # calls worse than an unwanted import, *because it looks fine*. Holders read their dump
    # lazily inside a refresh method; none of them derive-and-cache at construction, and
    # that property is what makes the swap safe rather than hopeful. It has to be kept.
    # `tests/test_packdump_adopt.py` walks the live window and fails on any holder left
    # pointing at the old dump.

    def _packdump_holders(self):
        """Every object that stores the packdump. Anything without `set_packdump` (an
        editor, an image viewer) simply isn't one and is skipped by the caller."""
        yield getattr(self, "_blueprints", None)
        yield getattr(self, "_registry_panel", None)
        bottom = getattr(self, "_bottom", None)
        if bottom is not None:
            yield bottom.panel("errors")
        yield from self._tab_models.values()          # open view tabs
        yield from self._open_tabs.values()           # blueprint and job tabs

    def _adopt_packdump(self, dump):
        """Point the window at a newly imported dump, leaving the workspace alone.

        Falls back to the full rebuild if rebinding raises: a half-rebound window is worse
        than a rebuilt one, because some of it would still be answering from the old
        registry — the exact thing this is here to prevent.
        """
        if dump is None:
            return
        if getattr(self, "_blocked", None) or getattr(self, "_workspace", None) is None:
            # Nothing was ever built to rebind; entering the profile is what builds it.
            self._enter_profile(self._profile.name)
            return
        try:
            self._rebind_packdump(dump)
        except Exception:
            log.warning("Could not rebind onto the new packdump; rebuilding the window",
                        exc_info=True)
            self._switch_profile_in_place()
            return
        self._report_import(self._import_result)

    def _open_settings(self):
        """File → Packsmith Settings. Profile-scoped, because both settings are.

        Applying is deliberately narrow: the client jar is re-located and the panels that
        hold it are re-pointed, but nothing is torn down. Neither setting changes the
        registry, so the packdump-adopt path (which does rebuild things) has no business
        running here.
        """
        if self._profile is None:
            return
        dialog = SettingsDialog(self._profile, packdump=self._packdump, parent=self)
        if not dialog.exec() or dialog.result_settings is None:
            return
        self._profile.settings = dialog.result_settings
        try:
            self._profile.save()
        except Exception as e:
            QMessageBox.warning(self, "Couldn't save settings", str(e))
            return
        self._apply_jar_setting()
        self._apply_loader_setting()
        self._set_status("Settings saved")

    def _resolve_loaders(self, dump=None):
        """The capability resolution table for this profile (§7.1 / §8.1).

        One place, because getting it slightly different in two would mean the app disagreed
        with itself about which loader receives overrides. Both arguments matter: the
        instance lets each loader read its own config and report what it *actually* offers,
        and the stored preference decides who wins a contested capability — without it the
        winner is alphabetical, which on a pack holding both Paxi and Moonlight silently
        hands Moonlight the overrides.
        """
        return resolve(dump if dump is not None else self._packdump,
                       loaders=PACK_LOADERS,
                       preferred=self._profile.settings.get(PACK_LOADER_SETTING),
                       instance_root=self._profile.mc_path)

    def _apply_loader_setting(self):
        """Re-resolve capabilities against the newly chosen loader (§8.1).

        The same call `_rebind_packdump` makes, for the same reason: the resolution table is
        derived, so it is recomputed rather than patched — and the Files panel's Smart Mode
        is built out of whichever loader won.
        """
        self._loaders = self._resolve_loaders()
        if getattr(self, "_files_panel", None) is not None:
            self._files_panel.set_loader(self._loaders.provider_for(DATAPACKS_WRITE))

    def _apply_jar_setting(self):
        """Re-locate the client jar and hand it to everything that reads it."""
        self._client_jar = locate_for(self._profile)
        if getattr(self, "_files_panel", None) is not None:
            self._files_panel._client_jar = self._client_jar.path if self._client_jar else None
            self._files_panel.refresh()

    def _pack_targets(self):
        """Which datapacks and resource packs this profile has (design 3.3 / 8.1).

        Built fresh each time rather than cached: packs are folders on disk that the user
        (or anything else) can create and delete between one call and the next, and a picker
        offering a pack that is no longer there is worse than a moment's directory read.
        """
        loaders = getattr(self, "_loaders", None)
        store = getattr(self, "_file_store", None)
        if loaders is None or store is None:
            return None
        return PackTargets(loaders, store.root)

    def _rebind_packdump(self, dump):
        self._packdump = dump
        self._rebuild_pending = False
        for holder in self._packdump_holders():
            rebind = getattr(holder, "set_packdump", None)
            if rebind is not None:
                rebind(dump)
        # §8.1: a mod update can install or remove the pack loader itself, so the resolution
        # table is derived from the dump and has to be recomputed rather than kept.
        self._loaders = self._resolve_loaders(dump)
        if getattr(self, "_files_panel", None) is not None:
            self._files_panel.set_loader(self._loaders.provider_for(DATAPACKS_WRITE))
        self._refresh_header()
        self._refresh_packdump_panel()

    def _switch_profile_in_place(self):
        """Rebuild against the same profile — the fallback when rebinding fails.

        Reopens whatever was open, best-effort. Not the normal path any more: see
        `_adopt_packdump`, which is what runs when a dump is imported.
        """
        reopen = list(self._open_tabs)
        if not self._close_all_tabs():
            # The unsaved-changes guard refused. The new dump is already written to disk,
            # so bailing outright would leave the window reading an old registry that
            # nothing would ever re-offer — `check_packdump` compares against what is on
            # disk and would report "unchanged" forever after. So: put back what we closed,
            # and remember to try again on the next focus.
            self._reopen_tabs(reopen)
            self._rebuild_pending = True
            self._set_status("Packdump import postponed — save or close your edited tabs")
            return
        try:
            self._db.close()
        except Exception:
            log.warning("Could not close the database before rebuilding", exc_info=True)
        self._rebuild_pending = False
        self._enter_profile(self._profile.name)
        self._reopen_tabs(reopen)

    def _reopen_tabs(self, keys):
        """Put back the tabs a rebuild closed, in the order they were in.

        Best-effort by design: a view, blueprint or job the new packdump invalidated simply
        doesn't come back, and that is the honest outcome — better than an empty tab
        claiming to show something that is gone.

        Every kind `_open_tabs` can hold is handled here. A kind that is missing doesn't
        fail loudly, it just silently stops coming back — which is how the packdump-diff
        tab quietly went missing across a rebuild for as long as it did.
        """
        for key in keys:
            kind = key[0] if isinstance(key, tuple) else None
            try:
                if kind == "doc":
                    source, path = self._editor_host.split_key(key[1])
                    self._open_document(source, path)
                elif kind == "browse":
                    self._open_browse(key[1])
                elif kind == "tag":
                    self._open_tag_view(key[1], key[2])
                elif kind == "packdump-diff":
                    # The tab that says what changed is the one an adopt would otherwise
                    # close — which is the worst possible thing to drop on an import.
                    if key[1] == "latest":
                        self._open_packdump_diff()
                    else:
                        older, _, newer = key[1].partition("->")
                        self._open_diff_between(older, newer)
                elif kind == "view":
                    view = next((v for v in self._views.all() if v.id == key[1]), None)
                    if view is not None:
                        self._open_view(view)
                elif kind == "blueprint":
                    if key[1] in set(self._blueprints.names()):
                        self._open_blueprint(key[1])
                elif kind == "job":
                    job = self._jobs.get(key[1])
                    if job is not None:
                        self._open_job_editor(job)
            except Exception:
                log.info("Could not reopen %s after the packdump changed", key, exc_info=True)

    def _check_for_new_packdump(self):
        """Poll on window focus. The mental model is that the packdump is a LIVE snapshot,
        and you generate a new one by leaving Packsmith to run the game — so coming back is
        exactly the moment to look. Cheap: a load and a comparison, no watcher."""
        if self._rebuild_pending:
            # A dump already landed on disk that the window never took on. Nothing on the
            # instance side has to change for that to still be owed, so it is checked
            # against what we are HOLDING rather than against the instance.
            latest = current_packdump(self._profile)
            if latest is not None and latest != self._packdump:
                self._adopt_packdump(latest)
                return
            self._rebuild_pending = False
        try:
            checked = check_packdump(self._profile)
        except Exception:
            log.warning("Packdump check failed", exc_info=True)
            return
        if checked.status in ("unchanged", "missing"):
            return
        if getattr(self, "_blocked", None):
            # The blocked shell's whole instruction is "run the game once, then come back",
            # and this poll is what "come back" fires. So a dump appearing here should LEAVE
            # the blocked state rather than try to update a window that was never built.
            self._enter_profile(self._profile.name)
            return
        if checked.status == "imported":
            self._import_result = import_packdump(self._profile)
            if self._import_result.status == "imported":
                self._adopt_packdump(self._import_result.packdump)
                return
        self._import_result = checked
        self._report_import(checked)

    def changeEvent(self, event):
        super().changeEvent(event)
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow()
                and getattr(self, "_profile", None) is not None):
            self._check_for_new_packdump()

    def _set_status(self, message):
        """Bound indirection: panels are built before the bottom panel exists, so they
        connect here rather than straight to it."""
        self._bottom.set_status(message)

    def _show_panel(self, key):
        # Reload from source on the way in. A panel that only refreshes when it *itself*
        # changes something goes stale behind your back — an action run claims a file, or
        # something outside Packsmith edits the instance, and the panel keeps showing the
        # world as it was when you last looked at it.
        reload_panel = self._panel_reloaders.get(key)
        if reload_panel is not None:
            reload_panel()
        self._panel_stack.show_panel(key)
        if self._h_split.sizes()[0] == 0:
            self._h_split.setSizes([style.SIDEBAR_PANEL_WIDTH, 1000])

    def _collapse_panel(self):
        self._panel_stack.hide()

    # --- menu bar ----------------------------------------------------------

    def _build_menu_bar(self):
        """Mostly placeholders for the eventual shell (§4.1). Live today: New View,
        opening a demo view, undo/redo, and the window toggles."""
        bar = self.menuBar()

        file_menu = bar.addMenu("File")
        file_menu.addAction("New Profile…", self._new_profile)
        file_menu.addAction("Open Profile…", self._open_profile)
        file_menu.addSeparator()
        file_menu.addAction("Check for New Packdump", self._check_for_new_packdump)
        file_menu.addSeparator()
        file_menu.addAction("Packsmith Settings…", self._open_settings)
        file_menu.addSeparator()
        file_menu.addAction("Exit", self.close)

        edit_menu = bar.addMenu("Edit")
        edit_menu.addAction("Undo", self._undo)
        edit_menu.addAction("Redo", self._redo)

        window_menu = bar.addMenu("Window")
        window_menu.addAction("Toggle Bottom Panel\tCtrl+`", lambda: self._bottom.toggle())

        views_menu = bar.addMenu("Views")
        views_menu.addAction("New View…", self._new_view)
        views_menu.addSeparator()
        for label in ("Manage Views…", "Import View…"):
            views_menu.addAction(label).setEnabled(False)

        profiles_menu = bar.addMenu("Profiles")
        profiles_menu.addAction("New Profile…", self._new_profile)
        profiles_menu.addAction("Switch Profile…", self._open_profile)

        help_menu = bar.addMenu("Help")
        help_menu.addAction("Encyclopedia Packsmithia",
                            lambda: self._open_encyclopedia())
        help_menu.addSeparator()
        help_menu.addAction("About Packsmith").setEnabled(False)

    def _open_encyclopedia(self, page_id=None):
        """Open the guide, or focus it if it is already open, and go to `page_id`.

        One tab, not one per page: the encyclopedia navigates internally, so opening it
        twice would give you two of the same thing with different scroll positions. That
        is also what makes deep-linking cheap — a ? anywhere in the app is one call.
        """
        key = ("encyclopedia",)
        tab = self._open_tabs.get(key)
        if tab is None:
            tab = EncyclopediaTab()
            self._workspace.add_tab(tab, "Encyclopedia",
                                    icon=icons.ui_icon("hint", colour=style.TEXT))
            self._open_tabs[key] = tab
        else:
            self._workspace.focus_widget(tab)
        if page_id:
            tab.show_page(page_id)
        return tab

    # --- view tabs ---------------------------------------------------------

    def _open_tab_for(self, key, title, query, view=None):
        """Open a query as a tab — or focus it if that same thing is already open, so
        clicking around the sidebar doesn't pile up duplicate tabs."""
        existing = self._open_tabs.get(key)
        if existing is not None and self._workspace.focus_widget(existing):
            return existing
        tab = self._build_view_tab(query, view=view)
        self._workspace.add_tab(tab, title,
                                icon=icons.concept_icon("views", colour=style.TEXT))
        self._open_tabs[key] = tab
        self._bottom.set_status(f"{title} — {self._tab_models[tab].rowCount():,} rows")
        return tab

    def _open_view(self, view):
        """Open a saved View (from the Views panel). The tab remembers which View it
        renders, so constructor edits can be written back to it.

        Which renderer it gets is the View's own business — one list in the panel, two
        renderers behind it."""
        if view.renderer == "blueprint_grid":
            return self._open_blueprint_tab(("view", view.id), view.name, view.query,
                                            view=view)
        return self._open_tab_for(("view", view.id), view.name, view.query, view=view)

    def _open_browse(self, registry_type):
        """Open a zero-tag browse table for a registry type (from the Registry panel)."""
        return self._open_tab_for(("browse", registry_type), registry_type,
                                  browse_query(registry_type))

    def _open_tag_view(self, registry_type, tag_name):
        """Open a minimal one-tag view (the Tags panel quick-action)."""
        return self._open_tab_for(("tag", registry_type, tag_name), tag_name,
                                  tag_query(registry_type, tag_name))

    def _build_view_tab(self, query, view=None) -> QWidget:
        """One tab rendering a query: model -> table, with type-aware cell delegates
        and per-tag edit-mode toggles. The model sorts itself; there is no proxy."""
        model = RegistryTableModel(query, self._packdump, self._tags,
                                   confirm_takeover=self._confirm_tag_takeover)

        table = RegistryTableView()
        # The model sorts itself, so there is no proxy: a QSortFilterProxyModel compares
        # pairwise through Python, which measured 2.2s per sort on an 18,638-row registry
        # against 3ms for a key sort in the model. It was doing nothing else — filtering
        # is the query's job, and the model already numbers its own vertical header.
        table.setModel(model)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(RegistryTableView.SelectItems)
        table.setSelectionMode(RegistryTableView.ExtendedSelection)
        table.setStyleSheet(style.TABLE_QSS)
        # Horizontal hairlines only — drawn by the delegates, so the vertical lattice goes.
        table.setShowGrid(False)
        # Hover state has to reach the delegates for the enum arrow to appear under the
        # pointer rather than on all 18,638 rows at once.
        table.setMouseTracking(True)
        # Indicator first, THEN enable: `setSortingEnabled(True)` immediately sorts by the
        # current section, so enabling and then calling sortByColumn sorted the whole table
        # twice on open. The rows already arrive in the query's `order_by`, so this initial
        # pass only has to agree with the arrow rather than discover anything.
        table.horizontalHeader().setSortIndicator(0, Qt.AscendingOrder)
        table.setSortingEnabled(True)

        vh = table.verticalHeader()
        # 24 was cramped; rows need somewhere to sit. The default is also the FLOOR: a row
        # dragged shorter than this clips its own content — the checkbox and the ownership
        # bar are sized to it — so shrinking is a way to break the table rather than a way
        # to fit more in. Growing stays free, which is the direction anyone actually wants.
        vh.setDefaultSectionSize(style.ROW_HEIGHT)
        vh.setMinimumSectionSize(style.ROW_HEIGHT)
        vh.setSectionsClickable(True)
        vh.setDefaultAlignment(Qt.AlignRight | Qt.AlignVCenter)
        vh.setFixedWidth(46)              # sized for five digits, not for the widest label

        # L1 columns get a delegate of their own rather than Qt's stock one, so text
        # inset and row rules match the tag columns beside them.
        plain = PlainCellDelegate(table)
        table.setItemDelegate(plain)
        self._delegates.append(plain)

        h = table.horizontalHeader()
        h.setHighlightSections(False)     # the clicked column shouldn't shout
        h.setFixedHeight(30)
        # Small caps and a little tracking, applied as a FONT rather than by rewriting the
        # header text — `headerData` keeps returning the real column name, so Copy and the
        # query language still see `localization`, not `LOCALIZATION`.
        header_font = QFont(h.font())
        header_font.setCapitalization(QFont.AllUppercase)
        header_font.setLetterSpacing(QFont.PercentageSpacing, 108)
        header_font.setPointSizeF(max(7.5, header_font.pointSizeF() - 1.5))
        header_font.setBold(True)
        h.setFont(header_font)
        for col in range(model.columnCount()):
            h.setSectionResizeMode(col, QHeaderView.Interactive)
            if model.headerData(col, Qt.Horizontal) == "id":
                table.setColumnWidth(col, 300)
            elif model.is_tag_column(col):
                table.setColumnWidth(col, 120)
                delegate_cls = _DELEGATES.get(model.tag_type_for_column(col))
                if delegate_cls:
                    delegate = delegate_cls(table)
                    table.setItemDelegateForColumn(col, delegate)
                    self._delegates.append(delegate)
            else:
                table.setColumnWidth(col, 240)
        # The last column keeps the width it was given; leftover viewport stays empty.
        #
        # Stretching it to fill was worst exactly where it was least wanted: a bool column
        # is 120px of checkbox and becomes 600px of mostly nothing, with the checkbox
        # marooned in the middle. It also made that column the only one you could not
        # resize, since it snapped back to whatever was left over.
        #
        # The empty strip to the right IS the dummy space — no placeholder column needed,
        # and nothing in the header to explain away.
        h.setStretchLastSection(False)

        # Widths, per-row heights and the sort, remembered for this View alone. Restored
        # after the columns exist, so stored widths land on real sections.
        key = self._layout_key(view, model._registry_type)
        stored = (load_ui_state(self._profile.name).get("table_layouts", {}).get(key)
                  if self._profile is not None else None)
        table_layout = TableLayout(table, model, style.ROW_HEIGHT, stored, parent=table)
        table_layout.restore()
        table_layout.changed.connect(
            lambda k=key, l=table_layout: self._remember_layout(k, l))
        model.tags_written.connect(lambda t=None: self._on_tags_written())

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # The bar refines what you're looking at; the ⚙ beside it edits what the view IS.
        # Typing here never changes the view — "Keep" is the deliberate act that does.
        bar = QueryBar()
        bar.set_keepable(view is not None)
        bar.filter_changed.connect(
            lambda node, t=tab: self._apply_refinement(t, node))
        # Remembered for **saved Views only**. A tab you got by clicking a registry or a
        # tag is a scratch surface — you opened it to look at something, and it should
        # open clean every time rather than resuming a search you have forgotten making.
        # A View is a thing you named and came back to, so where you had got to in it is
        # part of coming back.
        if view is not None:
            bar.filter_changed.connect(
                lambda _node, k=key, b=bar: self._remember_filter(k, b.text()))
        bar.keep_requested.connect(lambda node, t=tab: self._keep_refinement(t, node))
        bar.help_requested.connect(
            lambda: self._open_encyclopedia(encyclopedia_pages.FILTER_HELP))

        layout.addWidget(bar)
        layout.addWidget(table)

        self._tab_models[tab] = model
        self._tab_bars[tab] = bar
        self._tab_base_queries[tab] = query
        # The denominator of "N of M" is the view's own size, captured here while nothing
        # is refined. Deriving it later reads a count that a refinement has already shrunk.
        self._tab_totals[tab] = model.rowCount()
        if view is not None:
            self._tab_views[tab] = view
        bar.report(model.rowCount(), model.rowCount())

        # Restore the refinement LAST: applying it re-runs the query and re-reports the
        # count, both of which need the bookkeeping above to already be in place.
        remembered = self._stored_filter(key) if view is not None else ""
        if remembered:
            bar.set_text(remembered)
        return tab

    def _apply_refinement(self, tab, node):
        """Re-run the tab's query with the bar's filter ANDed onto the view's own."""
        model = self._tab_models.get(tab)
        base = self._tab_base_queries.get(tab)
        bar = self._tab_bars.get(tab)
        if model is None or base is None:
            return
        started = time.perf_counter()
        try:
            model.set_filter(combine(base.filter, node))
        except QueryError as e:
            bar._show_error(QuerySyntaxError(str(e)))
            return
        if node is None:                       # cleared: this IS the view's own size
            self._tab_totals[tab] = model.rowCount()
        bar.report(model.rowCount(), self._tab_totals.get(tab, model.rowCount()),
                   time.perf_counter() - started)

    def _keep_refinement(self, tab, node):
        """Fold the refinement into the view's own query — §3.2.3's "filter bar -> save"."""
        view = self._tab_views.get(tab)
        base = self._tab_base_queries.get(tab)
        if view is None or base is None or node is None:
            return
        merged = dataclasses.replace(base, filter=combine(base.filter, node))
        self._views.update_query(view.id, merged)
        self._tab_base_queries[tab] = merged
        self._tab_bars[tab].clear()
        self._tab_totals[tab] = self._tab_models[tab].rowCount()   # the view is smaller now
        self._reload_views()
        self._set_status(f"Folded the filter into '{view.name}'")

    # --- text editor (design 6.3) ------------------------------------------

    def _open_file(self, rel_path):
        """A file from the Files panel — an instance file, governed by ownership."""
        return self._open_document("instance", rel_path)

    def _open_package_document(self, path):
        """A package source or manifest from the Actions panel — governed by provenance.
        These deliberately never appear in the Files panel: they're Packsmith's own
        userdata, not game files."""
        return self._open_document("package", path)

    # --- action reference pages (design 3.3.1) -----------------------------

    def _open_action(self, ref):
        """One action's reference page, or focus it if already open.

        Keyed by the **ref**, which is the action's identity — so clicking the same action
        in two places lands on one tab rather than two copies of the same reading matter.
        """
        key = ("action", ref)
        existing = self._open_tabs.get(key)
        if existing is not None and self._workspace.focus_widget(existing):
            return existing
        try:
            manifest = self._packages.get(ref)
        except KeyError:
            # The index is refreshed on every package edit, so this means the manifest lost
            # the declaration between the panel drawing it and the click landing.
            self._set_status(f"'{ref}' is no longer declared")
            self._actions_panel.refresh()
            return None

        tab = ActionPageTab(manifest, self._packages.package(manifest.package_name),
                            self._jobs, tag_store=self._tags,
                            blueprint_store=self._blueprints)
        tab.status.connect(self._set_status)
        tab.document_activated.connect(self._open_package_document)
        tab.job_activated.connect(self._open_job_by_id)
        self._workspace.add_tab(tab, tab.title(), icon=tab.tab_icon())
        self._open_tabs[key] = tab
        self._set_status(f"{ref} — {manifest.file} · {manifest.function}()")
        return tab

    def _open_job_by_id(self, job_id):
        """From an action page's Used-by list to the job editor. The page names jobs, so
        it has to be able to hand you one."""
        job = self._jobs.get(job_id)
        if job is not None:
            self._open_job_editor(job)

    def _tab_icon(self, source, path):
        """The file-type icon for a document tab (design 6.2's vocabulary, reused).

        The tab bar is heterogeneous by design (§4.2), so every icon in it has to say
        *what kind of thing this tab is* — which means each kind brings its own vocabulary
        rather than borrowing one. Documents get the file glyphs; a View gets the Views
        mark and a blueprint grid the blueprint one. What must never happen is a View
        wearing a paper-and-lines glyph, which would say something untrue about it.

        For a jar member the icon comes from the MEMBER's name, not the jar's — the tab is
        showing `recipes/stone.json`, so a zip glyph would describe the container instead of
        what you are looking at.
        """
        name = Path(split_member(path)[1]).name if source == "jar" else Path(path).name
        return icons.file_icon(name, colour=self._tab_icon_colour(source, path))

    def _tab_icon_colour(self, source, path) -> str:
        """Whose the open file is, in the same colours the Files panel uses (§6.1).

        Ownership is the fact the whole file engine turns on — an action's write to a
        user-owned file is hard-blocked, and a user's write to an action-owned one is a loud
        transfer — so knowing whose a file is *while you are editing it* is worth a tab
        icon. Muted for a jar member because it is nobody's to own: the mod's copy must stay
        exactly as it shipped (§6.5).

        Untracked reads as normal rather than faint. In the Files panel faint means "not yet
        tracked" among hundreds of rows; in a tab bar it would mean "this thing you have
        open is somehow lesser", and untracked is simply the default state of a file.
        """
        if source == "jar":
            return style.TEXT_MUTED
        if source != "instance":
            return style.TEXT           # a package's own sources are yours by construction
        try:
            ownership = self._file_store.ownership(path)
        except Exception:
            return style.TEXT
        if not ownership:
            return style.TEXT
        return (style.OWNER_USER_ICON if ownership.get("kind") == "user"
                else style.OWNER_ACTION_ICON)

    def _refresh_tab_icons(self):
        """Re-colour every open document tab.

        Called wherever ownership can move: the first keystroke in an editor claims a file,
        a job run claims whatever it wrote, the Files panel can take or release one, and
        saving an NBT tree claims it. A tab showing yesterday's owner would be worse than
        showing none — the colour is only worth having if it is current.
        """
        for (kind, key), tab in list(self._open_tabs.items()):
            if kind != "doc":
                continue
            try:
                source, path = self._editor_host.split_key(key)
                self._workspace.set_tab_icon(tab, self._tab_icon(source, path))
            except Exception:
                continue        # a cosmetic pass must never break on one odd tab

    def _open_document(self, source, path):
        key = self._editor_host.key_for(source, path)
        existing = self._open_tabs.get(("doc", key))
        if existing is not None and self._workspace.focus_widget(existing):
            return existing

        # Design 6.0 Editor Dispatch. The text editor assumes UTF-8, and a modpack instance
        # is mostly NOT that — `mods/` is nothing but jars, and the browser lists them, so
        # double-clicking one is ordinary rather than perverse. The NBT and JAR editors are
        # a sanctioned deferral; reaching them through a decode crash is not.
        kind = self._file_kind(source, path)
        if kind == filetypes.ARCHIVE and source == "instance":
            # §6.5, step one. Only for instance files: a package's own source tree isn't
            # an archive, and a jar in `userdata/` would be Packsmith's business rather
            # than the pack's.
            tab = JarViewerTab(self._file_store.root / path)
            tab.status.connect(self._set_status)
            tab.member_activated.connect(
                lambda _archive, member, rel=path: self._open_jar_member(rel, member))
            tab.override_requested.connect(
                lambda _archive, member, rel=path: self._save_as_override(rel, member))
            self._workspace.add_tab(tab, Path(path).name, icon=self._tab_icon(source, path))
            self._open_tabs[("doc", key)] = tab
            self._set_status(f"{Path(path).name} — {tab._summary.text()}")
            return tab
        if kind == filetypes.NBT:
            # Bytes, like the image viewer — so a structure file inside a mod's jar opens
            # exactly like one on disk, and neither is extracted (design 6.5).
            tab = NbtViewerTab(self._bytes_for(source, path) or b"", Path(path).name,
                               read_only_reason=self._nbt_read_only_reason(source, path))
            tab.status.connect(self._set_status)
            tab.save_requested.connect(
                lambda data, s=source, p=path, t=tab: self._save_nbt(s, p, t, data))
            tab.dirty_changed.connect(
                lambda dirty, p=path, t=tab: self._on_tab_dirty(t, Path(p).name, dirty))
            self._workspace.add_tab(tab, Path(path).name, icon=self._tab_icon(source, path))
            self._open_tabs[("doc", key)] = tab
            self._set_status(f"{Path(path).name} — {tab._summary.text()}")
            return tab
        if kind == filetypes.IMAGE:
            # One branch for both worlds: the viewer takes BYTES, so a texture on disk and
            # a texture inside a jar are the same case and neither is extracted.
            data = self._bytes_for(source, path)
            tab = ImageViewerTab(data or b"", Path(path).name)
            tab.status.connect(self._set_status)
            self._workspace.add_tab(tab, Path(path).name, icon=self._tab_icon(source, path))
            self._open_tabs[("doc", key)] = tab
            self._set_status(f"{Path(path).name} — {tab._info.text()}")
            return tab
        if kind != filetypes.TEXT:
            tab = UnsupportedFileTab(path, kind)
            self._workspace.add_tab(tab, Path(path).name, icon=self._tab_icon(source, path))
            self._open_tabs[("doc", key)] = tab
            self._set_status(f"{Path(path).name} — {filetypes.describe(kind)}")
            return tab

        try:
            tab = EditorTab(self._editor_host, source, path)
        except (UnicodeDecodeError, OSError) as e:
            # Sniffing reads the first few KB; a file can be clean there and hold bad bytes
            # later, and a config can vanish or lock between the click and the read. The
            # classifier makes that rare rather than impossible, so the open path still
            # needs to survive it — this is the last guard, not the first.
            log.info("Falling back to the placeholder for %s: %s", path, e)
            tab = UnsupportedFileTab(path, filetypes.BINARY)
            self._workspace.add_tab(tab, Path(path).name, icon=self._tab_icon(source, path))
            self._open_tabs[("doc", key)] = tab
            self._set_status(f"{Path(path).name} — can't be opened as text ({e})")
            return tab
        tab.unlock_requested.connect(self._request_unlock)
        self._workspace.add_tab(tab, Path(path).name, icon=self._tab_icon(source, path))
        self._open_tabs[("doc", key)] = tab
        tab.activate()
        return tab

    def _nbt_read_only_reason(self, source, path):
        """Why this NBT file can't be saved in place, or None.

        A jar member is the case that matters and it is the same answer §6.5 gives for
        text: the mod's own copy must stay exactly as it shipped, because the whole
        override mechanism depends on it. So the reason names the way forward rather than
        just refusing.
        """
        if source == "jar":
            archive, member = split_member(path)
            if is_overridable(member):
                return (f"Read-only — this is inside {Path(archive).name}. Right-click it "
                        f"in the jar and choose \"Save as override\" for an editable copy.")
            return f"Read-only — {Path(archive).name} members have no override target."
        if source == "package":
            return None
        try:
            locked = self._file_store.ownership(path)
        except Exception:
            return None
        if locked and locked.get("kind") == "action":
            # Whole-file ownership (§6.1). §6.4 wants this per-path, which would let two
            # actions own different branches; §1.1 defers per-key claims, so the file is
            # the unit here exactly as it is for a config.
            return (f"Read-only — an action owns this file "
                    f"({locked.get('action_ref') or 'unknown'}). Take it in the Files panel "
                    f"to edit it.")
        return None

    def _save_nbt(self, source, path, tab, data: bytes):
        """Write an edited NBT tree back (design 6.4).

        Binary all the way: `write_bytes` exists precisely so a gzipped tag tree isn't
        routed through the text path, which would mangle it — or raise while capturing the
        prior content for rollback, which is a confusing place to fail.
        """
        if source != "instance":
            self._set_status(f"{Path(path).name} can't be saved here")
            return
        try:
            self._file_store.write_bytes(path, data, owner="user")
        except Exception as e:
            QMessageBox.warning(self, "Couldn't save", f"{path}\n\n{e}")
            self._set_status(f"Save failed: {e}")
            return
        tab.mark_saved()
        self._refresh_tab_icons()
        self._set_status(f"Saved {path} — {len(data):,} bytes")
        self._files_panel.refresh()          # the save may have claimed ownership

    def _on_tab_dirty(self, tab, name, dirty):
        """The unsaved dot, for tabs that aren't Monaco (design 4.1 wants it visible)."""
        self._workspace.set_tab_title(tab, f"● {name}" if dirty else name)

    def _save_as_override(self, archive_rel, member):
        """Copy a file out of a mod jar into a datapack or resource pack (design 6.5).

        "Packsmith doesn't do anything clever here — it just writes the file to the right
        place and lets Minecraft's pack layering do the rest" (§8.1). The path inside the
        pack is the path inside the jar, unchanged: that is the entire mechanism, and
        altering it would produce a file the game never looks at.
        """
        kind = override_kind(member)
        if kind is None:
            self._set_status(f"{member} has no override target")
            return
        capability = (DATAPACKS_WRITE if kind == "datapacks" else RESOURCEPACKS_WRITE)
        try:
            loader = self._loaders.require(capability)
        except CapabilityError as e:
            QMessageBox.information(
                self, "No pack loader",
                f"{e}\n\nMinecraft has no built-in way to load a global datapack, so "
                f"overrides need a loader mod such as Paxi, OpenLoader or Moonlight.")
            return

        setting = f"override_target_{kind}"
        dialog = OverrideTargetDialog(
            member, kind, loader.packs(self._file_store.root, kind),
            default=self._profile.settings.get(setting), parent=self)
        if not dialog.exec():
            return

        pack = dialog.result_pack
        try:
            if dialog.result_is_new:
                loader.create_pack(
                    self._file_store.root, pack, kind=kind,
                    pack_format=pack_format_for(
                        self._profile.mc_version, kind,
                        client_jar=self._client_jar.path if self._client_jar else None))
            target = loader.override_path(self._file_store.root, pack, member, kind=kind)
            rel = target.relative_to(self._file_store.root).as_posix()
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Save as override", str(e))
            return

        if target.exists() and QMessageBox.question(
                self, "Overwrite existing override",
                f"'{pack}' already overrides {member}.\n\nReplace it with the copy from "
                f"{Path(archive_rel).name}? Anything you changed in it will be lost.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        try:
            data = read_member(self._file_store.root / archive_rel, member)
            # Bytes, not text: an override is as often a PNG as a JSON, and the ownership
            # record matters either way — this file is now the user's.
            self._file_store.write_bytes(rel, data, owner="user")
        except (ArchiveError, OSError) as e:
            QMessageBox.warning(self, "Save as override", f"Could not write it: {e}")
            return

        # Sticky per profile (§8.1), so the next override goes where the last one did.
        self._profile.settings[setting] = pack
        self._profile.save()
        self._files_panel.refresh()
        self._set_status(f"Overriding {member} in '{pack}' — {loader.name} will load it")
        return self._open_file(rel)

    def _open_jar_member(self, archive_rel, member):
        """Open one file from inside a jar (design 6.5, step two).

        Nothing is extracted — the member is read out of the archive into memory and handed
        to the editor as a string. A scratch copy on disk would be the very thing §6.5
        exists to remove, only hidden somewhere the user can't see it.
        """
        return self._open_document("jar", member_path(archive_rel, member))

    def _bytes_for(self, source, path) -> bytes:
        """The raw bytes of a document, whichever world it lives in.

        The two are genuinely different reads — one from disk, one out of a zip — and
        everything downstream is happier not knowing which it got.
        """
        try:
            if source == "jar":
                return self._editor_host.source_named("jar").raw(path)
            root = (self._file_store.root if source == "instance"
                    else self._packages.directory)
            return (Path(root) / path).read_bytes()
        except Exception:
            return b""

    def _file_kind(self, source, path) -> str:
        """Classify by extension plus a peek at the bytes, per 6.0's "content sniffing"."""
        if source == "jar":
            # Classified from the member's OWN bytes: `data/.../x.json` is text and
            # `Mod.class` is not, and neither fact is knowable from the jar's extension.
            try:
                probe = self._editor_host.source_named("jar").raw(path)[:4096]
            except Exception:
                return filetypes.BINARY
            return filetypes.classify(split_member(path)[1], probe)
        root = (self._file_store.root if source == "instance"
                else self._packages.directory)
        try:
            full = Path(root) / path
        except (TypeError, ValueError):
            return filetypes.TEXT
        return filetypes.classify(path, filetypes.probe_file(full))

    # --- packages: the declaration layer and the file layer ------------------
    #
    # A package has two layers and they get separate operations, because "stop calling
    # this file an action" and "destroy this file" are different things to want.

    def _target_package(self, dlg):
        """Resolve the package half of a dialog, creating it if that's what was asked."""
        if dlg.result_new_package:
            return create_package(self._packages.directory, dlg.result_new_package,
                                  author=self._profile.name)
        return self._packages.package(dlg.result_package)

    def _after_package_change(self, status, select=None):
        # Manifests are only parsed on scan, so any structural change needs a reload
        # before anything can bind, run, or even list it.
        self._packages.reload()
        self._actions_panel.refresh()
        # `reload` rather than `refresh`: a create may have added a package, and the picker
        # is the only thing that knows the list.
        self._packages_panel.reload()
        if select:
            self._packages_panel.select(select)
        self._set_status(status)

    def _new_package(self):
        """Create an authored package and select it (§3.3.1).

        Nothing about an authored package is special — it is exactly what a downloaded one
        is, which is what lets something you wrote be published later without restructuring
        it. Selecting it afterwards is the point of creating it.
        """
        dlg = NewPackageDialog(self._packages, parent=self)
        if not dlg.exec():
            return
        try:
            package = create_package(self._packages.directory, dlg.result_name,
                                     author=self._profile.name,
                                     version=dlg.result_version)
        except ValueError as e:
            QMessageBox.warning(self, "Can't create package", str(e))
            return
        self._after_package_change(f"Created package '{package.name}'",
                                   select=package.name)
        self._automation_panel.show_tab("packages")

    def _new_action(self, package_name="", fixed=False):
        """Declare an action, creating its package and/or file first if needed (§3.3.1)."""
        dlg = NewActionDialog(self._packages, package=package_name or None, parent=self,
                              fixed=fixed and bool(package_name))
        if not dlg.exec():
            return
        try:
            package = self._target_package(dlg)
            source = add_action(package, dlg.result_action_id, file=dlg.result_file,
                                function=dlg.result_function, name=dlg.result_name,
                                description=dlg.result_description)
        except ValueError as e:
            QMessageBox.warning(self, "Can't create action", str(e))
            return
        self._after_package_change(f"Declared {package.name}:{dlg.result_action_id}")
        self._open_package_document(f"{package.name}/{source.name}")

    def _new_package_file(self, package_name="", folder="", fixed=False):
        """Add a file with no declaration — a helper, a library, a fixture, a README."""
        dlg = NewFileDialog(self._packages, package=package_name or None, folder=folder,
                            parent=self, fixed=fixed and bool(package_name))
        if not dlg.exec():
            return
        try:
            package = self._target_package(dlg)
            create_file(package, dlg.result_file)
        except ValueError as e:
            QMessageBox.warning(self, "Can't create file", str(e))
            return
        self._after_package_change(f"Created {package.name}/{dlg.result_file}")
        self._open_package_document(f"{package.name}/{dlg.result_file}")

    def _new_package_folder(self, package_name="", folder="", fixed=False):
        """Folders are organisation and nothing else: an action's file is a relative path,
        so the shape of the tree never changes what a package means."""
        dlg = NewFolderDialog(self._packages, package=package_name or None, folder=folder,
                              parent=self, fixed=fixed and bool(package_name))
        if not dlg.exec():
            return
        try:
            package = self._target_package(dlg)
            create_folder(package, dlg.result_folder)
        except ValueError as e:
            QMessageBox.warning(self, "Can't create folder", str(e))
            return
        self._after_package_change(f"Created {package.name}/{dlg.result_folder}/")

    def _remove_action(self, action_ref):
        """Undeclare. The file stays — that's the whole point of the two layers."""
        try:
            manifest = self._packages.get(action_ref)
        except KeyError:
            return
        package = self._packages.package(manifest.package_name)
        confirm = QMessageBox.question(
            self, "Remove declaration",
            f"Stop declaring '{action_ref}'?\n\n"
            f"{manifest.file} is kept and {manifest.function}() stays in it — it just "
            f"becomes an ordinary function that nothing can run directly.\n\n"
            f"Jobs with steps bound to this action will fail to resolve it.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        try:
            remove_action(package, manifest.action_id)
        except ValueError as e:
            QMessageBox.warning(self, "Can't remove declaration", str(e))
            return
        self._after_package_change(f"Removed the declaration for {action_ref}")

    def _rename_package_file(self, package_name, file_name):
        package = self._packages.package(package_name)
        if package is None:
            return
        dlg = RenameFileDialog(package_name, file_name, parent=self)
        if not dlg.exec() or dlg.result_name == file_name:
            return
        # Close the old tab first: its key is the path, and renaming underneath it would
        # leave a buffer that saves to a file that no longer exists.
        if not self._close_package_tab(f"{package_name}/{file_name}"):
            return
        try:
            rename_file(package, file_name, dlg.result_name)
        except ValueError as e:
            QMessageBox.warning(self, "Can't rename", str(e))
            return
        self._after_package_change(f"Renamed to {package_name}/{dlg.result_name}")

    def _delete_package_file(self, package_name, file_name):
        package = self._packages.package(package_name)
        if package is None:
            return
        users = sorted(a.action_id for a in package.actions if a.file == file_name)
        if users:
            # Refused, not confirmed-around: the declaration has to come off first so the
            # destructive step is the one actually asked for.
            QMessageBox.warning(
                self, "Still declared",
                f"{package_name}/{file_name} implements {', '.join(users)}.\n\n"
                f"Remove {'those declarations' if len(users) > 1 else 'that declaration'} "
                f"first, then delete the file.")
            return
        confirm = QMessageBox.question(
            self, "Delete file",
            f"Delete {package_name}/{file_name}?\n\nThis removes the file from disk.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        self._close_package_tab(f"{package_name}/{file_name}", force=True)
        try:
            delete_file(package, file_name)
        except ValueError as e:
            QMessageBox.warning(self, "Can't delete", str(e))
            return
        self._after_package_change(f"Deleted {package_name}/{file_name}")

    def _rename_package_folder(self, package_name, folder):
        package = self._packages.package(package_name)
        if package is None:
            return
        dlg = RenameFileDialog(package_name, folder, is_folder=True, parent=self)
        if not dlg.exec() or dlg.result_name == folder:
            return
        inside = [f for f in source_files(package) if f.startswith(f"{folder}/")]
        for path in inside:
            if not self._close_package_tab(f"{package_name}/{path}"):
                return
        try:
            rename_folder(package, folder, dlg.result_name)
        except ValueError as e:
            QMessageBox.warning(self, "Can't rename", str(e))
            return
        self._after_package_change(f"Renamed to {package_name}/{dlg.result_name}/")

    def _delete_package_folder(self, package_name, folder):
        package = self._packages.package(package_name)
        if package is None:
            return
        inside = [f for f in source_files(package) if f.startswith(f"{folder}/")]
        declared = sorted(a.action_id for a in package.actions
                          if a.file.startswith(f"{folder}/"))
        if declared:
            QMessageBox.warning(
                self, "Still declared",
                f"{package_name}/{folder}/ holds the source for "
                f"{', '.join(declared)}.\n\nRemove "
                f"{'those declarations' if len(declared) > 1 else 'that declaration'} "
                f"first, then delete the folder.")
            return
        # Say how much is about to go. A folder delete is the one operation here that can
        # destroy code the user never named.
        detail = (f"\n\nThis deletes {len(inside)} file"
                  f"{'s' if len(inside) != 1 else ''} inside it:\n  "
                  + "\n  ".join(inside)) if inside else "\n\nIt's empty."
        confirm = QMessageBox.question(
            self, "Delete folder", f"Delete {package_name}/{folder}/?{detail}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        for path in inside:
            self._close_package_tab(f"{package_name}/{path}", force=True)
        try:
            delete_folder(package, folder)
        except ValueError as e:
            QMessageBox.warning(self, "Can't delete", str(e))
            return
        self._after_package_change(f"Deleted {package_name}/{folder}/")

    def _close_package_tab(self, path, *, force=False) -> bool:
        """Close an open editor onto a package file. Returns False if the user cancelled.

        ``force`` skips the unsaved-changes prompt, which is right for deletion — offering
        to save a file you just agreed to destroy is nonsense.
        """
        key = self._editor_host.key_for("package", path)
        tab = self._open_tabs.get(("doc", key))
        if tab is None:
            return True
        index = self._workspace._tabs.indexOf(tab)
        if index < 0:
            return True
        guard = self._workspace.close_guard
        if force:
            self._workspace.close_guard = None
        try:
            self._workspace._close_tab(index)
        finally:
            self._workspace.close_guard = guard
        return ("doc", key) not in self._open_tabs

    def _request_unlock(self, key):
        reason = self._editor_host.lock_reason(key)
        if reason:
            self._offer_unlock(key, reason)

    def _offer_unlock(self, key, reason):
        """The Rust move (design 6.1): the destructive act stays available, but you have to
        name it. Fires the instant they type into a locked buffer, so the consequence lands
        beside the intent instead of days later when the job fails."""
        source, path = self._editor_host.split_key(key)
        if not self._editor_host.can_unlock(key):
            # Nothing to release — a downloaded package isn't locked by a claim, it's
            # locked by what it *is*. Editing it would silently fork upstream.
            QMessageBox.information(
                self, "Read-only", f"{path}\n\nThis document is {reason} and can't be "
                                   f"edited here.")
            return
        if QMessageBox.question(
                self, "Take ownership",
                f"{path}\n\nThis file is {reason}.\n\n"
                f"Taking ownership lets you edit it, and blocks that action from writing "
                f"it until you release it again in the Files panel.\n\nTake ownership?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._editor_host.unlock(key)

    def _on_unlocked(self, key):
        tab = self._open_tabs.get(("doc", key))
        if tab is not None:
            tab.sync_lock()
        self._files_panel.refresh()
        self._set_status(f"You now own {self._editor_host.split_key(key)[1]}")

    def _on_file_claimed(self, rel_path):
        self._set_status(f"You now own {rel_path} — actions are blocked from writing it.")

    def _on_tags_written(self):
        """An L2 write happened in one view. Every OTHER open view is now out of date.

        Marked rather than refreshed. Only one view tab is visible at a time, so refreshing
        a hidden one is work nobody sees — and the tab you are *editing in* must not
        re-evaluate at all: the row you just unchecked would vanish from under the cursor
        in a view whose filter it no longer matches.

        The tab doing the writing is marked too, and simply never acts on it while it is
        the one in front. That is what makes "the row leaves the Remove view when you come
        back to it" fall out of one rule instead of an exemption list.
        """
        current = self._workspace.current_widget()
        for tab in self._tab_models:
            if tab is not current:
                self._stale_tabs.add(tab)

    def _reconcile(self, tab) -> bool:
        """Re-run a stale tab's query, keeping your place in it."""
        if tab not in self._stale_tabs:
            return False
        self._stale_tabs.discard(tab)
        model = self._tab_models.get(tab)
        if model is None:
            return False

        table = tab.findChild(RegistryTableView)
        # A full reset scrolls to the top. Coming back to a tab and losing your place every
        # time would be a worse bug than the staleness this fixes.
        scroll = table.verticalScrollBar().value() if table else 0
        model.reevaluate()
        if table:
            table.verticalScrollBar().setValue(scroll)

        # The bar's "N of M" was measured before the edit; left alone it describes a table
        # that no longer exists.
        bar = self._tab_bars.get(tab)
        if bar is not None:
            bar.report(model.rowCount(), self._tab_totals.get(tab, model.rowCount()))
        return True

    def _on_tab_activated(self, widget):
        """Hand the shared web view to whichever editor tab is now in front, and re-state
        what the status bar is describing.

        The row count used to be written once when a tab opened and never again, so after
        switching tabs — or after an edit changed the row count — the bar described a tab
        that wasn't in front any more. A status bar that is wrong is worse than one that is
        empty, because you have no way to tell which."""
        self._reconcile(widget)
        if isinstance(widget, (EditorTab, DiffTab)):
            # Both hold the one shared web view (§4.2), so both have to claim it back when
            # they come forward — a diff left un-activated shows whatever the last editor
            # tab had in it.
            widget.activate()
            self._set_status(self._status_for(widget))
        else:
            line = self._status_for(widget)
            if line:
                self._set_status(line)

    def _status_for(self, tab) -> str:
        if isinstance(tab, EditorTab):
            line = self._ownership_line(tab)
            return f"{line} — unsaved changes" if tab.is_dirty else line
        model = self._tab_models.get(tab)
        if model is None:
            return ""
        title = self._workspace.tab_title(tab) or ""
        return f"{title.lstrip('● ')} — {model.rowCount():,} rows"

    def _refresh_status(self):
        """Re-state the bar for whatever tab is in front (after an edit, run, or save)."""
        current = self._workspace.current_widget()
        if current is not None:
            line = self._status_for(current)
            if line:
                self._set_status(line)

    def _ownership_line(self, tab) -> str:
        """§6.3: for unstructured files ownership is whole-file, "surfaced as a status-bar
        indicator — the decorations don't apply because there's no key granularity to
        decorate". Per-key gutter decorations wait for the structured editor; the file's
        own answer doesn't have to."""
        source, path = self._editor_host.split_key(tab.key)
        if source != "instance":
            return path
        ownership = self._file_store.ownership(path)
        if ownership is None:
            return f"{path} — untouched; editing it makes it yours"
        if ownership["kind"] == "user":
            return f"{path} — yours; actions are blocked from writing it"
        who = ownership.get("action_ref") or "an action"
        return f"{path} — managed by {who}; take ownership to edit"

    def _on_editor_dirty(self, key, is_dirty):
        tab = self._open_tabs.get(("doc", key))
        if tab is None:
            return
        tab.set_dirty(is_dirty)
        name = Path(tab.path).name
        self._workspace.set_tab_title(tab, f"● {name}" if is_dirty else name)
        # §4.1 wants unsaved state visible. The tab title's dot carries it, but the title
        # is easy to miss on a tab that isn't in front — so the bar says it in words for
        # the one that is.
        if self._workspace.current_widget() is tab:
            self._refresh_status()
        if is_dirty:
            # §6.2: "editing is the gesture that tracks it" — the first keystroke claims
            # the file, so the tab turns yours now rather than waiting for a save.
            self._refresh_tab_icons()

    def _on_document_saved(self, key):
        source, path = self._editor_host.split_key(key)
        self._set_status(f"Saved {path}")
        self._refresh_tab_icons()
        if source == "instance":
            self._files_panel.refresh()      # the save may have claimed ownership

    def _may_close_tab(self, widget) -> bool:
        """Veto closing a tab with unsaved changes unless the user insists."""
        if isinstance(widget, NbtViewerTab):
            # An NBT tab edits an in-memory tree with no autosave anywhere, so closing it
            # is the only way to lose the work — which makes the guard MORE necessary here
            # than for Monaco, not less.
            if not widget.is_dirty:
                return True
            answer = QMessageBox.question(
                self, "Unsaved changes",
                f"{widget.label} has unsaved changes.\n\nSave before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save)
            if answer == QMessageBox.Cancel:
                return False
            if answer == QMessageBox.Save:
                # Synchronous, unlike Monaco's: the bytes come straight off the tree, so
                # they are written before this returns and the tab is destroyed.
                widget.request_save()
                return not widget.is_dirty      # a failed write must not close the tab
            return True
        if not isinstance(widget, EditorTab) or not widget.is_dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes",
            f"{widget.path} has unsaved changes.\n\nSave before closing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            # Returning True closes the tab, which disposes the Monaco model — so this
            # asks for a save and then immediately destroys the thing holding the text.
            # It is safe for two reasons, both worth stating because neither is obvious:
            #   1. The buffer is read on the JS side. `request_save` and `closeModel` are
            #      queued to the renderer in that order, so the text is captured before
            #      the model dies; only the *notification* comes back asynchronously.
            #   2. `_on_save` writes through the DocumentSource, which needs the key and
            #      the content — not the model. It works fine after the tab is gone.
            # Break either and this silently discards the user's edits, so if the save
            # path ever stops going through the JS queue, this needs a real veto-then-
            # close-on-`file_saved` handshake instead.
            self._editor_host.request_save(widget.key)
        return True

    def _on_tab_closed(self, widget):
        """Closing a tab closes a *window onto* a View — the View itself lives in the
        Views panel and is untouched."""
        self._tab_models.pop(widget, None)
        self._tab_views.pop(widget, None)
        # A closed tab is owed nothing, and a deleted widget left in the set would keep it
        # growing for the life of the session.
        self._stale_tabs.discard(widget)
        if isinstance(widget, EditorTab):
            # Reclaim the shared view BEFORE the tab is destroyed, or it takes the editor
            # with it as a child.
            widget.detach()
            self._editor_host.close_document(widget.key)
        elif isinstance(widget, DiffTab):
            widget.detach()      # reclaims the view and disposes both diff models
        for key, tab in list(self._open_tabs.items()):
            if tab is widget:
                del self._open_tabs[key]

    def _edit_view(self, view):
        """Edit a saved View's query, from the Views panel's context menu.

        Keyed on the **View**, not on an open tab: editing a View is something you do to
        the View, and it should not require having opened it first. If it happens to be
        open, the tab is re-run in place so you are not looking at a stale table.

        This replaced a ⚙ beside the open table's filter bar, which was genuinely
        misleading — the two sat inches apart, both were about queries, and only one of
        them changed what the saved View *is*.
        """
        dlg = QueryConstructorDialog(self._tags, view.query.scope.type,
                                     query=view.query, parent=self)
        if not dlg.exec():
            return
        updated = dataclasses.replace(view.query, filter=dlg.result_filter)
        self._views.update_query(view.id, updated)
        self._reload_views()

        tab = self._open_tabs.get(("view", view.id))
        model = self._tab_models.get(tab) if tab is not None else None
        if model is not None:
            model.set_filter(dlg.result_filter)
            self._tab_base_queries[tab] = updated
            self._tab_totals[tab] = model.rowCount()
            bar = self._tab_bars.get(tab)
            if bar is not None:
                bar.report(model.rowCount(), model.rowCount())
            self._bottom.set_status(f"{view.name} — {model.rowCount():,} rows")
        else:
            self._bottom.set_status(f"Edited '{view.name}'")

    def _new_view(self):
        """Author a new View: saved to the database, filed in the Views panel, opened."""
        registries = sorted(self._packdump.registry)
        dlg = QueryConstructorDialog(
            self._tags, "minecraft:item" if "minecraft:item" in registries
            else (registries[0] if registries else "minecraft:item"),
            new_view=True, view_store=self._views, parent=self, registries=registries)
        if not dlg.exec():
            return
        try:
            view = self._views.create(dlg.result_name, dlg.result_query)
        except ValueError as e:
            QMessageBox.warning(self, "Can't save view", str(e))
            return
        self._reload_views()
        self._open_view(view)

    def _rename_view(self, view):
        name, ok = QInputDialog.getText(self, "Rename View", "Name:", text=view.name)
        if not ok or not name.strip():
            return
        try:
            self._views.rename(view.id, name)
        except ValueError as e:
            QMessageBox.warning(self, "Can't rename view", str(e))
            return
        self._reload_views()
        tab = self._open_tabs.get(("view", view.id))
        if tab is not None:
            self._workspace.set_tab_title(tab, name.strip())
            self._tab_views[tab] = self._views.get(view.id)

    def _delete_view(self, view):
        """Delete is explicit and permanent — unlike closing a tab, which only closes a
        window onto the View."""
        confirm = QMessageBox.question(
            self, "Delete View",
            f"Delete the view '{view.name}'?\n\nThis removes the saved view itself, "
            f"not just its tab.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        self._views.delete(view.id)
        tab = self._open_tabs.get(("view", view.id))
        if tab is not None:
            idx = self._workspace._tabs.indexOf(tab)
            if idx >= 0:
                self._workspace._close_tab(idx)
        self._reload_views()
        self._bottom.set_status(f"Deleted view '{view.name}'")

    def _view_layout(self) -> list:
        if self._profile is None:
            return []
        return load_ui_state(self._profile.name).get("view_layout", []) or []

    def _remember_view_layout(self, layout):
        """Per profile, in UI state — not columns on the views table.

        How a View is filed and where it sits are presentation, and a schema change would
        mean a SCHEMA_VERSION bump and a migration for furniture. The View is the artifact;
        its place in the panel is how you like to look at it.
        """
        if self._profile is not None:
            save_ui_state(self._profile.name, view_layout=list(layout))

    def _reload_views(self):
        self._views_panel.set_views(self._views.all(), self._view_layout())
        # A view that couldn't be decoded is skipped rather than taking the panel down
        # (Q-1) — but skipped silently is just a view that vanished. Say so.
        broken = self._views.unreadable()
        if broken:
            names = ", ".join(f"'{name}'" for _id, name, _why in broken[:4])
            more = f" and {len(broken) - 4} more" if len(broken) > 4 else ""
            self._set_status(
                f"{len(broken)} view(s) could not be loaded: {names}{more} — "
                f"{broken[0][2]}")

    # --- tags --------------------------------------------------------------

    def _new_tag(self):
        """Create a tag definition — the user's vocabulary (§3.2.1). Creation only:
        rename and retype are governed by rules whose ceremony isn't built yet."""
        registries = sorted(self._packdump.registry.keys())
        dlg = TagCreateDialog(registries, tag_store=self._tags,
                              preferred_registry="minecraft:item", parent=self)
        if not dlg.exec():
            return
        try:
            self._tags.define(dlg.result_registry, dlg.result_name, dlg.result_type,
                              enum_values=dlg.result_enum_values, default=dlg.result_default)
        except ValueError as e:
            QMessageBox.warning(self, "Can't create tag", str(e))
            return
        self._tags_panel.refresh()
        self._bottom.set_status(
            f"Created tag '{dlg.result_name}' ({dlg.result_type}) on {dlg.result_registry}")

    def _edit_enum_values(self, registry_type, tag_name):
        """Add / remove / reorder an enum tag's values (§3.2.1). The dialog owns the
        blast-radius confirmation; removed values leave orphans, not deleted data."""
        dlg = EnumValuesDialog(self._tags, registry_type, tag_name, parent=self,
                               job_store=self._jobs, package_index=self._packages)
        if not dlg.exec():
            return
        self._tags_panel.refresh()
        self._refresh_after_tag_change()
        values = self._tags.definition(registry_type, tag_name).get("values", [])
        self._bottom.set_status(f"'{tag_name}' values: {', '.join(values)}")

    def _confirm_tag_takeover(self, cells) -> bool:
        """Design 3.2.1: taking a cell an action manages is loud. `cells` is
        [(entry_id, tag_name, action_ref), ...] — confirmed once, however many."""
        if len(cells) == 1:
            entry_id, tag_name, action_ref = cells[0]
            text = (f"'{tag_name}' on {entry_id} is managed by '{action_ref}'.\n\n"
                    f"Taking ownership will prevent that action from updating it on "
                    f"future runs.\n\nContinue?")
        else:
            refs = ", ".join(sorted({f"'{c[2]}'" for c in cells}))
            text = (f"{len(cells)} of the cells you're editing are managed by {refs}.\n\n"
                    f"Taking ownership will prevent those actions from updating them on "
                    f"future runs.\n\nContinue?")
        return QMessageBox.question(self, "Take ownership", text,
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    def _on_rolled_back(self):
        self._bottom.set_status("Step rolled back")
        self._refresh_after_run()      # rollback restores file ownership too

    def _on_orphans_resolved(self):
        """An orphan resolution changed assignments — refresh everything that reads them."""
        self._refresh_after_tag_change()

    def _refresh_after_tag_change(self):
        for model in self._tab_models.values():
            model.reevaluate()
        # Everything was just re-run, so nothing is owed a reconcile — leaving the flags
        # set would cost a second full pass the next time each tab came forward.
        self._stale_tabs.clear()
        # A tag is part of a job's contract, not just of a view's columns: deleting or
        # renaming one can make a step unrunnable (design 3.2.1). Both surfaces that say
        # so have to hear about it, or the sidebar greys a job while its open tab still
        # shows the step as fine — two places disagreeing about one fact.
        self._jobs_panel.refresh()
        self._reload_job_tabs()
        self._bottom.refresh_panels()

    def _refresh_after_run(self):
        """An action run can touch both engines — L2 cells AND file ownership — so the
        Files panel has to refresh too, not just the views and the bottom panels.

        Blueprint grids need saying explicitly: they aren't model-backed, so nothing in
        `_refresh_after_tag_change` reaches them and a run that wrote bindings would leave
        the open grid stale until an unrelated edit happened to rebuild it.
        """
        self._refresh_after_tag_change()
        self._reload_blueprint_tabs()
        self._blueprints_panel.refresh()
        self._files_panel.refresh()
        self._refresh_tab_icons()      # the run may have claimed a file you have open
        # An open editor's lock was decided when it opened. If the run took a file the user
        # had open, the tab has to stop looking editable — the save is refused either way
        # (§6.1), but discovering that at Ctrl+S is a worse way to learn it.
        for key in self._editor_host.resync_locks():
            self._set_status(f"{key.split(':', 1)[-1]} was changed by this run — "
                             f"it's now locked; reload it to see the new contents")

    def _file_ownership_changed(self, message):
        """Ownership moved from the Files panel — so an open tab's lock may be stale.

        The panel and the editor decide lock state from the same source, but at different
        moments: the tab decided when it opened. Taking a file from an action in the panel
        used to leave the tab showing a lock banner over a buffer that was now perfectly
        saveable, with an unlock button that did nothing. Same staleness the run path fixed
        (see :meth:`_after_run`), reached from the other direction.
        """
        freed = [key for key in self._editor_host.resync_locks()
                 if not self._editor_host.is_locked(key)]
        if freed:
            message += "  The open tab is editable now."
        self._refresh_tab_icons()
        self._set_status(message)

    def _job_problems(self, job):
        """Why ``job`` would refuse to run, without running it (design 3.2.1).

        The same function the pre-flight gate uses, so the colour in the panel and the
        behaviour on pressing play can never disagree — a job painted runnable that then
        refuses would be worse than not colouring at all.
        """
        try:
            return step_problems(self._jobs.get(job.id), package_index=self._packages,
                                 tag_store=self._tags, blueprint_store=self._blueprints,
                                 packdump=self._packdump,
                                 pack_targets=self._pack_targets())
        except Exception:            # never let a cosmetic check break the panel
            return []

    def _rename_tag(self, registry_type, tag_name):
        """Rename a tag, stating both blast radii first (design 3.2.1).

        The two consequences are reported separately because they are genuinely different
        in kind, and the difference *is* the rule: Views only observe, so they follow
        silently; job steps act, so they stop until relinked. Saying "3 views and 2 steps
        are affected" as one number would hide that half of it needs nothing from you and
        half of it will not run until it does.
        """
        new_name, ok = QInputDialog.getText(self, "Rename Tag", "New name:", text=tag_name)
        new_name = (new_name or "").strip()
        if not ok or not new_name or new_name == tag_name:
            return

        preview = self._tags.preview_rename(
            registry_type, tag_name, new_name, view_store=self._views,
            job_store=self._jobs, package_index=self._packages)
        detail = ""
        if preview["views"]:
            detail += (f"\n\n{len(preview['views'])} saved view(s) will follow "
                       f"automatically:\n  " + "\n  ".join(preview["views"]))
        if preview["steps"]:
            detail += ("\n\nThese job steps are bound to it and will REFUSE TO RUN until "
                       "you relink them (Errors panel):\n  "
                       + "\n  ".join(preview["steps"]))
        if not detail:
            detail = "\n\nNothing else references it."

        if QMessageBox.question(
                self, "Rename Tag",
                f"Rename '{tag_name}' to '{new_name}' on {registry_type}?"
                f"\n\nAssignments are untouched — they reference the tag itself, not its "
                f"name.{detail}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        try:
            rewritten = self._tags.rename(registry_type, tag_name, new_name,
                                          view_store=self._views)
        except (ValueError, KeyError) as e:
            QMessageBox.warning(self, "Rename Tag", str(e))
            return

        self._tags_panel.refresh()
        self._views_panel.set_views(self._views.all(), self._view_layout())
        self._refresh_after_tag_change()
        said = f"Renamed '{tag_name}' to '{new_name}'"
        if rewritten:
            said += f" — {len(rewritten)} view(s) followed"
        if preview["steps"]:
            said += f"; {len(preview['steps'])} job step(s) need relinking"
        self._set_status(said)

    def _delete_tag(self, registry_type, tag_name):
        """Undefine a tag. §3.2.1: this cascades to every assignment, so the confirmation
        names the count being destroyed."""
        assigned = self._tags.query(
            registry_type, filters=[{"tag": tag_name, "op": "exists"}])
        detail = (f"\n\n{len(assigned)} assignment(s) will be permanently deleted."
                  if assigned else "\n\nIt has no assignments.")
        confirm = QMessageBox.question(
            self, "Delete Tag",
            f"Delete the tag '{tag_name}' on {registry_type}?{detail}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        self._tags.undefine(registry_type, tag_name)
        self._tags_panel.refresh()
        # Any open view selecting that tag now has a stale column set, and any job step
        # bound to it just became unrunnable — one helper knows the whole blast radius, so
        # a new consequence gets picked up here without anyone remembering to add it.
        self._refresh_after_tag_change()
        self._bottom.set_status(f"Deleted tag '{tag_name}' and {len(assigned)} assignment(s)")

    def _active_model(self):
        return self._tab_models.get(self._workspace.current_widget())

    def _undo(self):
        self._move_history("undo")

    def _redo(self):
        self._move_history("redo")

    def _move_history(self, direction: str):
        """Ctrl+Z / Ctrl+Y, with the refusal reported rather than thrown.

        An old edit can become unrestorable — the tag it names gets undefined, or an enum
        value it holds is dropped from the definition. That used to raise straight out of
        the shortcut into Qt, which is a traceback on the console and nothing at all for
        the user. The stack survives a refusal, so saying why is enough: fix the cause and
        the edit is still there to undo.
        """
        model = self._active_model()
        if model is None:
            return
        try:
            getattr(model, direction)()
        except UndoBlocked as blocked:
            self._set_status(f"Can't {direction}: {blocked.reason}")
            log.warning("%s refused: %s", direction, blocked.reason)

    # --- actions -----------------------------------------------------------

    def _open_job_editor(self, job):
        """Open (or focus) a job's editor tab."""
        key = ("job", job.id)
        existing = self._open_tabs.get(key)
        if existing is not None and self._workspace.focus_widget(existing):
            return existing
        tab = JobEditorTab(job, blueprint_store=self._blueprints,
                           job_store=self._jobs, package_index=self._packages,
                           tag_store=self._tags, parent=self, packdump=self._packdump,
                           history=self._history)
        tab.changed.connect(self._reload_jobs)
        tab.run_requested.connect(self._run_job)
        tab.dry_run_requested.connect(self._dry_run_job)
        tab.step_run_requested.connect(
            lambda j, step_id, dry, through: self._run_one_step(
                j, step_id, dry_run=dry, through=through))
        tab.status.connect(self._set_status)
        # The same page the Actions and Packages panels open, reached from the step that
        # uses it — one action, one tab, whichever door you came through.
        tab.action_info_requested.connect(self._open_action)
        self._workspace.add_tab(tab, f"Job: {job.name}")
        self._open_tabs[key] = tab
        return tab

    def _dry_run_job(self, job):
        return self._run_job(job, dry_run=True)

    def _run_one_step(self, job, step_id, dry_run=False, through=False):
        """Run part of a job — the authoring loop (design 3.3).

        ``through`` runs the job from the top up to and including this step, rather than
        the step alone. Which you want depends on whether the step reads anything the ones
        above it write; see `run_job` for why that is not a detail.
        """
        if through:
            return self._run_job(job, dry_run=dry_run, through_step=step_id)
        return self._run_job(job, dry_run=dry_run, only_step=step_id)

    def _run_job(self, job, dry_run=False, only_step=None, through_step=None):
        """Execute a job: every step in order, each its own transaction (design 3.3.2).

        ``dry_run`` walks the identical path and promotes nothing. It is the same call with
        one flag — not a second code path — because a preview produced by different code is
        a preview that can disagree with the run.
        """
        job = self._jobs.get(job.id)
        if job is None:
            return
        if not job.steps:
            self._bottom.set_status(f"[{job.name}] has no steps")
            return

        # Runs are SYNCHRONOUS, and deliberately so: §3.3 promises they are globally
        # serialized, and a blocked window is that promise enforced by physics rather than
        # by a lock somebody has to remember to hold. What a frozen window costs is not
        # "you can't click" — nothing would be safe to click mid-run anyway — it is that
        # after a few seconds the OS marks the app Not Responding, at which point working
        # and crashed look identical. That is what this reporting is for.
        if getattr(self, "_job_running", False):
            return                      # `processEvents` below lets the button be clicked again
        self._job_running = True
        self._run_control.set_running(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        verb = "dry-running" if dry_run else "running"
        what = f"job '{job.name}'"
        target = only_step if only_step is not None else through_step
        if target is not None:
            position = next((i for i, s in enumerate(job.steps, start=1)
                             if s.id == target), None)
            if position is None:
                what = f"part of '{job.name}'"
            elif only_step is not None:
                what = f"step {position} of '{job.name}'"
            else:
                what = (f"steps 1–{position} of '{job.name}'" if position > 1
                        else f"step 1 of '{job.name}'")
        self._bottom.log(f"=== {verb} {what} ===")
        try:
            result = run_job(job, blueprint_store=self._blueprints,
                             job_store=self._jobs, package_index=self._packages,
                             tag_store=self._tags, packdump=self._packdump,
                             file_store=self._file_store, history=self._history,
                             job_history=self._job_history,
                             pack_targets=self._pack_targets(),
                             on_progress=self._on_job_progress,
                             dry_run=dry_run, only_step=only_step,
                             through_step=through_step)
        finally:
            QApplication.restoreOverrideCursor()
            self._job_running = False
            self._run_control.set_running(False)

        if result.blocked:
            # A pre-flight refusal never reaches a step, so it records nothing and the
            # Job Results panel has nothing to show — leaving "failed, 0 steps run" as the
            # entire explanation for a job that refused on purpose. §3.2.1 asks for the
            # opposite: "won't start, here's what to relink".
            detail = "; ".join(b.describe() for b in result.blocked[:3])
            if len(result.blocked) > 3:
                detail += f"; and {len(result.blocked) - 3} more"
            refused = f"[{job.name}] won't run until relinked — {detail}"
            self._bottom.log(refused)
            self._set_status(refused)
            self._bottom.show_panel("errors")
            return

        if result.reason:
            self._bottom.log(f"[{job.name}] {result.reason}")
            self._set_status(f"[{job.name}] {result.reason}")
            return

        partial = only_step is not None or through_step is not None
        label = what if partial else job.name
        summary = (f"[{label}] {result.status} — {len(result.step_results)} step(s) run"
                   + (f", {result.not_run} not reached" if result.not_run else ""))
        if dry_run:
            # The whole point of the gesture, so it goes in the headline rather than being
            # left for the log. Until the report tab lands (§3.3's Pre-Run Preview) this
            # count IS the answer — and "nothing" is a real one, which is how you find out
            # a job you already ran is idempotent.
            summary = (f"[{label}] dry run — would change "
                       f"{describe_summary(result.summary())}"
                       + (f"; {len(result.failed_steps)} step(s) failed"
                          if result.failed_steps else ""))
        self._bottom.log(summary)
        self._bottom.set_status(summary)
        if result.status != "success":
            # By key, not position: a bare index means a different tab the moment
            # BOTTOM_TABS is reordered, and it silently means the wrong one.
            self._bottom.show_panel("job_results")

        self._open_run_report(result, label=label if partial else None)
        if not dry_run:
            # Nothing moved, so there is nothing to re-read — and a refresh here would
            # rebuild every open table to show it exactly as it was.
            self._refresh_after_run()

    def _open_run_report(self, result, label=None):
        """Open (or replace) the report for a run that just finished.

        A **real** run is keyed by its `job_runs` id, so re-opening it from history lands on
        the same tab. A **dry** run has no id and no history: each preview is its own
        answer, so the previous one is closed rather than left beside it, which would leave
        two tabs called the same thing describing different worlds.
        """
        if not result.step_results:
            return                    # nothing ran; the status line already said why
        finished = datetime.now().strftime("%H:%M:%S")
        if result.dry_run:
            stale = self._open_tabs.pop(("report", "dry"), None)
            if stale is not None:
                self._workspace.close_widget(stale)
            key = ("report", "dry")
        else:
            # `run_id` is None when the run was not recorded — and then every unrecorded
            # run would share one key, so the second would silently focus the first and
            # show you the wrong report. An unkeyable run gets a key nothing else can
            # match instead.
            key = ("report", result.run_id if result.run_id is not None
                   else ("unrecorded", id(result)))
        report = reports.from_result(result, finished_at=finished, key=key, label=label)
        return self._show_report(report, key)

    def _show_report(self, report, key):
        existing = self._open_tabs.get(key)
        if existing is not None and self._workspace.focus_widget(existing):
            return existing
        tab = RunReportTab(report)
        tab.status.connect(self._set_status)
        # The report's key travels with the request, because a diff belongs to ONE run: two
        # runs that both rewrote `emi.json` are two different before/after pairs, and a key
        # of just the path would hand the second one the first one's tab.
        tab.diff_requested.connect(
            lambda change, k=key: self._open_file_diff(change, k))
        tab.rollback_requested.connect(self._roll_back_step)
        self._workspace.add_tab(tab, tab.title(), icon=tab.tab_icon())
        self._open_tabs[key] = tab
        return tab

    def open_run_report(self, job_run_id):
        """Re-open a recorded run from the bottom panel. Same tab type, same key — so a run
        you already have open comes forward instead of opening twice."""
        run = self._job_history.get(job_run_id)
        if run is None:
            return
        report = reports.from_history(run, self._history.for_job_run(job_run_id))
        return self._show_report(report, ("report", job_run_id))

    def _roll_back_step(self, run_id):
        """Reverse one committed step, from the report that shows what it did."""
        step = self._history.get(run_id)
        if step is None:
            return
        # Re-checked against the store, not against what the report said when it was built.
        # A report is a snapshot: roll a step back, leave the tab open, right-click the same
        # row again, and its menu still offers it. The second rollback would replay an
        # emptied inverse — harmless, and it would report success at undoing nothing.
        if not _has_rollback(step):
            self._set_status(
                f"{step['action_ref']} has already been rolled back")
            return
        if QMessageBox.question(
                self, "Roll back step",
                f"Undo everything '{step['action_ref']}' committed?\n\n"
                f"Its writes are restored to what they were before the step ran. Later "
                f"steps are NOT rolled back — each step is its own transaction (§3.3.2).",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            rollback_step(run_id, tag_store=self._tags, history=self._history,
                          file_store=self._file_store,
                          blueprint_store=self._blueprints)
        except (ValueError, KeyError) as e:
            QMessageBox.warning(self, "Can't roll back", str(e))
            return
        self._set_status(f"Rolled back {step['action_ref']}")
        self._refresh_after_run()
        self._bottom.refresh_panels()

    def _open_file_diff(self, change, report_key=("report", None)):
        """Show what a run did to one file — the snapshot on the left, today's bytes on
        the right.

        Where "today's bytes" come from is the whole subtlety. A run that just happened
        carries the new content in memory; a run read back from history does not, because
        §1.1 warns against growing the store-by-path snapshot and only the **hash** was
        persisted. So the current file is read from disk and the hash decides whether it
        can still be called the run's own output.
        """
        if change is None or change.get("engine") != "file":
            return
        path = change["path"]
        before = (change.get("before") or {}).get("value") or ""
        after = (change.get("after") or {}).get("value")
        note = ""
        if after is None:
            after = self._file_store.read(path) or ""
            expected = (change.get("after") or {}).get("hash")
            if expected and content_hash(after) != expected:
                note = "this file has been edited since the run"

        tab_key = ("diff", report_key, path)
        existing = self._open_tabs.get(tab_key)
        if existing is not None and self._workspace.focus_widget(existing):
            return existing
        # The Monaco-side model key has to be unique for the same reason the tab key is:
        # two diffs open on one path would otherwise share one pair of models, and the
        # second `openDiff` would find them already there and show the first one's text.
        tab = DiffTab(self._editor_host, f"diff:{report_key[1]}:{path}", path,
                      left="before this run", right="now", original=before,
                      modified=after, note=note)
        self._workspace.add_tab(tab, f"Diff: {Path(path).name}",
                                icon=icons.file_icon(Path(path).name,
                                                     colour=style.TEXT_MUTED))
        self._open_tabs[tab_key] = tab
        return tab

    def _on_job_progress(self, progress):
        """Say what is happening, while it happens (design 3.3.2).

        Two things, and the second is the point. The step's own log lines are written as it
        finishes rather than in a batch afterwards, so a long job reads like a log instead
        of arriving all at once. And the event loop is pumped, which keeps the window
        painting — without it the OS marks the app Not Responding and the user cannot tell
        a working job from a hung one.

        Pumping events means a click can land mid-run; `_run_job` guards re-entry, and
        nothing else here mutates state.
        """
        where = (f"{progress.position}/{progress.total}" if progress.total
                 else str(progress.position))
        if progress.phase == "start":
            # Announced BEFORE the step, because on a slow job the thing you want to know
            # is which step is slow — and afterwards is too late to learn it.
            self._bottom.set_status(f"Running step {where} — {progress.action_ref}…")
        else:
            step = progress.result
            for level, message in getattr(step, "log_lines", ()):
                self._bottom.log(f"  [{level}] {message}")
            if step is not None and not step.ok:
                self._bottom.log(f"  [error] {step.action_ref}: {step.reason}")
        QApplication.processEvents()

    def _new_job(self):
        name, ok = QInputDialog.getText(self, "New Job", "Name:")
        if not ok or not name.strip():
            return
        try:
            job = self._jobs.create(name)
        except ValueError as e:
            QMessageBox.warning(self, "Can't create job", str(e))
            return
        self._reload_jobs()
        self._open_job_editor(job)

    def _rename_job(self, job):
        name, ok = QInputDialog.getText(self, "Rename Job", "Name:", text=job.name)
        if not ok or not name.strip():
            return
        try:
            self._jobs.rename(job.id, name)
        except ValueError as e:
            QMessageBox.warning(self, "Can't rename job", str(e))
            return
        self._reload_jobs()
        tab = self._open_tabs.get(("job", job.id))
        if tab is not None:
            self._workspace.set_tab_title(tab, f"Job: {name.strip()}")
            tab.refresh()

    def _delete_job(self, job):
        if QMessageBox.question(
                self, "Delete Job",
                f"Delete the job '{job.name}' and its {len(job.steps)} step(s)?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._jobs.delete(job.id)
        tab = self._open_tabs.get(("job", job.id))
        if tab is not None:
            index = self._workspace._tabs.indexOf(tab)
            if index >= 0:
                self._workspace._close_tab(index)
        self._reload_jobs()
        self._bottom.set_status(f"Deleted job '{job.name}'")

    def _toggle_pin(self, job):
        self._jobs.set_pinned(job.id, not job.pinned)
        self._reload_jobs()

    def _reload_jobs(self):
        jobs = self._jobs.all()
        self._jobs_panel.set_jobs(jobs)
        control = getattr(self, "_run_control", None)
        if control is not None:
            # Same list, same readiness function as the panel. Two surfaces disagreeing
            # about whether a job can run is worse than either being wrong alone.
            control.set_jobs(jobs, readiness=self._job_problems)
            self._remember_selected_job()

    def _reload_automation(self):
        self._reload_jobs()
        self._actions_panel.refresh()

    def _remember_automation_tab(self):
        """Which half you were last on, per profile — authoring sessions and running
        sessions are different moods, and being dropped back into the wrong one is a small
        papercut that repeats."""
        panel = getattr(self, "_automation_panel", None)
        if panel is not None and self._profile is not None:
            save_ui_state(self._profile.name, automation_tab=panel.current_key)

    # --- how a View is arranged (design 5.1) -------------------------------

    @staticmethod
    def _layout_key(view, registry_type: str) -> str:
        """Where one table's arrangement is filed.

        A saved View is keyed by its **id**, so its arrangement belongs to that one
        configuration and cannot leak into another view of the same registry. A browse tab
        is not a View at all, so it gets its own namespace — re-openable and stable, but
        never sharing a key with anything the user saved.
        """
        return f"view:{view.id}" if view is not None else f"browse:{registry_type}"

    def _remember_layout(self, key: str, layout):
        """Queue a layout for saving. Debounced, because `sectionResized` fires on every
        pixel of a drag and each save is a read-modify-write of state.json."""
        self._pending_layouts[key] = layout.as_dict()
        self._layout_timer.start()

    def _flush_layouts(self):
        if not self._pending_layouts or self._profile is None:
            self._pending_layouts.clear()
            return
        stored = load_ui_state(self._profile.name).get("table_layouts", {})
        stored.update(self._pending_layouts)
        self._pending_layouts.clear()
        save_ui_state(self._profile.name, table_layouts=stored)

    def _stored_filter(self, key: str) -> str:
        if self._profile is None:
            return ""
        return load_ui_state(self._profile.name).get("view_filters", {}).get(key, "")

    def _remember_filter(self, key: str, text: str):
        """Where you had got to in a view, kept per view.

        A refinement is still transient in the sense §3.2.3 means — ANDed on top, clearable,
        never part of what the View *is*. What it stops being is transient across a tab
        close, which was only ever an accident of where it happened to live. "Keep" is
        still the deliberate act that folds it into the View itself.

        Stored even when empty: clearing the bar is a decision, and a cleared filter that
        came back next time you opened the tab would be the same bug as defaults creeping
        back over an emptied setting.
        """
        if self._profile is None:
            return
        stored = load_ui_state(self._profile.name).get("view_filters", {})
        stored[key] = text or ""
        save_ui_state(self._profile.name, view_filters=stored)

    def _remember_registry_pins(self, pins):
        """Per profile, in UI state — a pin is furniture the user moved, like the panel
        heights, not something they configured. Saved even when empty, because "I unpinned
        them all" is an answer and the defaults must not creep back."""
        if self._profile is not None:
            save_ui_state(self._profile.name, registry_pins=list(pins))

    def _remember_selected_job(self):
        """Persist which job is picked, so the header comes back where you left it.

        Per profile, in UI state rather than settings — you never *set* this, you just pick
        a job and that is the picking.
        """
        control = getattr(self, "_run_control", None)
        if control is not None and self._profile is not None:
            job_id = control.current_job_id()
            if job_id is not None:
                save_ui_state(self._profile.name, last_job=int(job_id))

    def _run_selected_job(self, dry_run=False):
        """Ctrl+R, or Ctrl+Shift+R for a dry one. The whole point of the control:
        re-running without going anywhere."""
        control = getattr(self, "_run_control", None)
        job = control.current_job() if control is not None else None
        if job is not None:
            self._run_job(job, dry_run=dry_run)

    # --- dev seed ----------------------------------------------------------

    def _seed_tags(self):
        """TEMP dev seed — ensures a handful of tags exist to work with (idempotent).
        Real tag creation is a user action via the Tags panel, which doesn't exist yet."""
        reg = "minecraft:item"
        if not self._tags.definition(reg, "tier"):
            self._tags.define(reg, "tier", "enum", ["early", "mid", "late", "oh my jesus christ wow"])
        if not self._tags.definition(reg, "banned"):
            self._tags.define(reg, "banned", "bool", default=False)
        if not self._tags.definition(reg, "tooltip"):
            self._tags.define(reg, "tooltip", "string")
        if not self._tags.definition(reg, "weight"):
            self._tags.define(reg, "weight", "number")
        if not self._tags.definition(reg, "remove"):
            self._tags.define(reg, "remove", "bool", default=False)

    def _seed_packages(self):
        """A fresh profile gets one ordinary authored package to put actions in.

        Nothing about it is privileged — §3.3.1 rules out a "magic default bucket", and
        this isn't one: it is a perfectly normal package that happens to be pre-created,
        deletable and renameable and publishable like any other. Same move as seeding
        views and jobs; it just means the New Action dialog has somewhere to go on day one.
        """
        if self._packages.packages:
            return
        slug = re.sub(r"[^a-z0-9_]+", "_", self._profile.name.lower()).strip("_")
        if not slug or not slug[0].isalpha():
            slug = f"pack_{slug}" if slug else "my_pack"
        try:
            create_package(self._packages.directory, slug, author=self._profile.name,
                           description=f"Actions authored for {self._profile.name}.")
        except ValueError:
            return
        self._packages.reload()

    def _seed_jobs(self):
        """A fresh profile gets a job for each installed action, best-guess bound — the
        successor to the hardcoded run button. Like views, there are no built-in jobs,
        only saved ones."""
        if self._jobs.count:
            return
        for ref, manifest in sorted(self._packages.actions.items()):
            try:
                bindings = best_guess_bindings(manifest, self._tags,
                                               blueprint_store=self._blueprints,
                                               pack_targets=self._pack_targets())
                job = self._jobs.create(manifest.name or ref)
            except ValueError:
                continue
            self._jobs.add_action_step(
                job.id, ref, bindings=bindings,
                bound_names=record_names(manifest, bindings, tag_store=self._tags))
            self._jobs.set_pinned(job.id, True)

    def _seed_views(self):
        """A fresh profile ships with sensible default Views (§4.1). They're saved as
        ordinary rows — there's no such thing as a built-in View, only saved ones."""
        if self._views.count == 0:
            for title, query in demo_views():
                self._views.create(title, query)

    def closeEvent(self, event):
        self._db.close()
        super().closeEvent(event)
