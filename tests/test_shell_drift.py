"""Shell behaviours that drifted from §4.1 — panels, menus, and the status bar.

Small things individually. What they share is that each one silently drops information the
user just supplied (which instance you clicked) or asserts something that stopped being
true (which tab the status bar is describing).
"""
import os

import pytest
from PySide6.QtCore import Qt

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.blueprints import BlueprintStore
from packsmith.core.jobs import JobStore


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    yield app


# --- the blueprints panel keeps the instance you clicked ---------------------------------

@pytest.fixture
def blueprints(user_db):
    store = BlueprintStore(user_db)
    store.define("StoneType")
    store.add_slot("StoneType", "base_block", "registry", registry_type="minecraft:block")
    for name in ("granite", "andesite", "diorite"):
        store.create_instance("StoneType", name)
    return store


def test_clicking_an_instance_reports_which_one(blueprints):
    """It used to emit the blueprint alone, so a click on `diorite` and a click on the
    schema were indistinguishable by the time the window saw them."""
    from packsmith.gui.shell.panels.blueprints_panel import BlueprintsPanel

    panel = BlueprintsPanel(blueprints)
    seen = []
    panel.blueprint_activated.connect(lambda b, i: seen.append((b, i)))

    root = panel._tree.topLevelItem(0)
    root.setExpanded(True)
    child = next(root.child(i) for i in range(root.childCount())
                 if root.child(i).text(0) == "diorite")
    panel._on_activated(child)

    assert seen == [("StoneType", "diorite")]


def test_clicking_the_blueprint_itself_reports_no_instance(blueprints):
    from packsmith.gui.shell.panels.blueprints_panel import BlueprintsPanel

    panel = BlueprintsPanel(blueprints)
    seen = []
    panel.blueprint_activated.connect(lambda b, i: seen.append((b, i)))
    panel._on_activated(panel._tree.topLevelItem(0))
    assert seen == [("StoneType", "")]


def test_the_grid_lands_on_that_instances_row(blueprints):
    """The other half — the tab is still the whole blueprint, but the cursor honours the
    click instead of sitting on row 0."""
    from packsmith.gui.queries import blueprint_query
    from packsmith.gui.blueprint_editor import BlueprintEditorTab

    tab = BlueprintEditorTab(blueprint_query("StoneType"), blueprints)
    names = [i.name for i in tab._instances]
    assert len(names) == 3, "fixture assumption"

    assert tab.focus_instance(names[-1]) is True
    assert tab._grid.currentRow() == len(names) - 1


def test_focusing_an_absent_instance_says_so_rather_than_lying(blueprints):
    from packsmith.gui.queries import blueprint_query
    from packsmith.gui.blueprint_editor import BlueprintEditorTab

    tab = BlueprintEditorTab(blueprint_query("StoneType"), blueprints)
    assert tab.focus_instance("not_here") is False


# --- the jobs panel can make a job when there are none ------------------------------------

def test_right_clicking_empty_space_offers_a_new_job(user_db):
    """An empty Jobs list had no context menu at all, which is the one state where "make a
    new one" is the only thing you could want."""
    from packsmith.gui.shell.panels.jobs_panel import JobsPanel

    panel = JobsPanel(JobStore(user_db).all())
    labels = [a.text() for a in panel._menu_for(None).actions() if a.text()]
    assert labels == ["New Job…"]


def test_a_jobs_own_menu_also_offers_it(user_db):
    from packsmith.gui.shell.panels.jobs_panel import JobsPanel

    jobs = JobStore(user_db)
    jobs.create("nightly")
    panel = JobsPanel(jobs.all())
    item = panel._tree.topLevelItem(0).child(0)
    labels = [a.text() for a in panel._menu_for(item).actions() if a.text()]
    assert "Run now" in labels and "New Job…" in labels


# --- the status bar describes the tab that is actually in front ---------------------------

class _Model:
    def __init__(self, rows):
        self._rows = rows

    def rowCount(self):
        return self._rows


def _window(tabs, models, current, files=None):
    """A stand-in for MainWindow: the methods under test only need these four attributes,
    and a real window wants a profile, a packdump and a Chromium view."""
    from packsmith.gui.main_window import MainWindow

    said = []
    workspace = type("WS", (), {
        "tab_title": lambda s, t: tabs.get(t, ""),
        "current_widget": lambda s: current,
    })()
    window = type("W", (), {
        "_tab_models": models, "_workspace": workspace, "_file_store": files,
        "_set_status": lambda s, m: said.append(m),
        "_status_for": MainWindow._status_for,
    })()
    return MainWindow, window, said


def test_switching_tabs_restates_the_row_count():
    """It was written once when the tab opened, so after a switch the bar described a tab
    that wasn't in front any more."""
    stones, ores = object(), object()
    models = {stones: _Model(1234), ores: _Model(7)}
    titles = {stones: "Stones", ores: "Ores"}

    cls, window, _ = _window(titles, models, current=stones)
    assert cls._status_for(window, ores) == "Ores — 7 rows"
    assert cls._status_for(window, stones) == "Stones — 1,234 rows"


def test_the_row_count_is_thousands_separated():
    tab = object()
    cls, window, _ = _window({tab: "Items"}, {tab: _Model(18042)}, current=tab)
    assert "18,042 rows" in cls._status_for(window, tab)


def test_a_dirty_tab_title_does_not_leak_its_dot_into_the_bar():
    tab = object()
    cls, window, _ = _window({tab: "● Stones"}, {tab: _Model(3)}, current=tab)
    assert cls._status_for(window, tab) == "Stones — 3 rows"


def test_refreshing_says_nothing_for_a_tab_it_knows_nothing_about():
    """Blueprint tabs have no row model here; silence beats a wrong number."""
    cls, window, said = _window({}, {}, current=object())
    cls._refresh_status(window)
    assert said == []


# --- the grid doesn't yank the cursor back to the cell you just left ----------------------

@pytest.fixture
def grid(blueprints):
    from packsmith.gui.queries import blueprint_query
    from packsmith.gui.blueprint_editor import BlueprintEditorTab

    blueprints.add_slot("StoneType", "tier", "string")
    tab = BlueprintEditorTab(blueprint_query("StoneType"), blueprints)
    assert tab._grid.rowCount() >= 2 and tab._grid.columnCount() >= 2, "fixture assumption"
    return tab


def _drain(qapp):
    qapp.processEvents()
    qapp.processEvents()          # _keep_cell defers one turn, then acts


def test_finishing_an_edit_in_place_keeps_the_cursor_there(grid, qapp):
    """The behaviour `_keep_cell` exists for: after Enter, you can keep arrowing."""
    grid._grid.setCurrentCell(1, 1)
    grid._keep_cell(1, 1)
    _drain(qapp)
    assert (grid._grid.currentRow(), grid._grid.currentColumn()) == (1, 1)


def test_moving_to_another_cell_mid_edit_is_not_undone(grid, qapp):
    """The report: double-click a second cell while the first has a picker open, and the
    cell you left stayed highlighted. `_keep_cell` re-asserted the old cell a turn later —
    so the editor was on the new cell and the selection on the old one."""
    from PySide6.QtWidgets import QAbstractItemView

    grid._grid.setCurrentCell(0, 1)
    grid._keep_cell(0, 1)                       # the edit on (0,1) is ending...

    grid._grid.setCurrentCell(1, 1)             # ...because the user moved to (1,1)
    grid._grid.setState(QAbstractItemView.EditingState)
    _drain(qapp)

    assert (grid._grid.currentRow(), grid._grid.currentColumn()) == (1, 1), \
        "the cursor was dragged back to the cell the user left"


def test_an_attached_picker_on_another_cell_also_holds_the_cursor(grid, qapp):
    """The second signal, for the window where the picker has re-attached but the view
    hasn't entered EditingState yet."""
    grid._grid.setCurrentCell(0, 1)
    grid._keep_cell(0, 1)

    grid._grid.setCurrentCell(1, 1)
    grid._picker = type("P", (), {"isVisible": lambda s: True, "close": lambda s: None})()
    grid._picker_cell = (1, 1)
    _drain(qapp)

    assert (grid._grid.currentRow(), grid._grid.currentColumn()) == (1, 1)


def test_a_cursor_anchored_picker_does_not_block_keeping_the_cell(grid, qapp):
    """The ⚙ Show picker isn't attached to any cell, so it must not suppress the restore."""
    grid._grid.setCurrentCell(1, 1)
    grid._keep_cell(1, 1)
    grid._picker = type("P", (), {"isVisible": lambda s: True, "close": lambda s: None})()
    grid._picker_cell = None
    _drain(qapp)

    assert (grid._grid.currentRow(), grid._grid.currentColumn()) == (1, 1)


def test_clicking_another_cell_moves_the_cursor_to_it(grid, qapp):
    """The reported symptom, at the level it actually occurs.

    Clicking B is what ends the edit in A. `_keep_cell(A)` then runs — and Qt already set
    the current cell to B during that press and will not set it again, so restoring A here
    is final: editor on B, highlight on A. It has to follow the click.
    """
    grid._grid._pressed_cell = (1, 1)        # what mousePressEvent recorded for cell B
    grid._keep_cell(0, 1)                    # the edit that was running in cell A commits
    _drain(qapp)

    assert (grid._grid.currentRow(), grid._grid.currentColumn()) == (1, 1),         "the highlight stayed on the cell the user left"


def test_the_press_only_speaks_for_one_commit(grid, qapp):
    """Read-and-clear: a click from earlier must not redirect a later Enter-commit."""
    grid._grid._pressed_cell = (1, 1)
    grid._keep_cell(0, 1)
    _drain(qapp)
    assert grid._grid._pressed_cell is None

    grid._grid.setCurrentCell(0, 1)
    grid._keep_cell(0, 1)                    # a later in-place commit on A
    _drain(qapp)
    assert (grid._grid.currentRow(), grid._grid.currentColumn()) == (0, 1)


def test_typing_revokes_a_stale_click(grid, qapp):
    """Click A, arrow to C, hit Enter: the commit belongs to C, not to where you clicked."""
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent

    grid._grid._pressed_cell = (0, 1)
    grid._grid.setCurrentCell(1, 1)
    grid._grid.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Down, Qt.NoModifier))
    assert grid._grid._pressed_cell is None, "a keystroke left the old click speaking"

    grid._keep_cell(1, 1)
    _drain(qapp)
    assert (grid._grid.currentRow(), grid._grid.currentColumn()) == (1, 1)


def test_a_press_on_the_same_cell_changes_nothing(grid, qapp):
    grid._grid._pressed_cell = (1, 1)
    grid._keep_cell(1, 1)
    _drain(qapp)
    assert (grid._grid.currentRow(), grid._grid.currentColumn()) == (1, 1)


# --- the reported bug, driven through the real event sequence ----------------------------

@pytest.fixture
def live_grid(user_db):
    """A grid whose registry columns really do attach pickers, so the editor lifecycle
    under test is the one the user sees."""
    from packsmith.gui.queries import blueprint_query
    from packsmith.gui.blueprint_editor import BlueprintEditorTab

    store = BlueprintStore(user_db)
    store.define("S")
    for name in ("base", "stairs"):
        store.add_slot("S", name, "registry", registry_type="minecraft:block")
    for name in ("granite", "andesite"):
        store.create_instance("S", name)

    class Dump:
        registry = {"minecraft:block": {"values": ["minecraft:stone", "minecraft:granite"]}}
        def attribute(self, *a): return None

    tab = BlueprintEditorTab(blueprint_query("S"), store, packdump=Dump())
    tab.resize(900, 400)
    tab.show()
    yield tab

    # Tear down in the right order or Qt segfaults: the popup is a live window and the cell
    # editor is a live child, and letting Python collect the tab first leaves Qt holding
    # pointers to both.
    if tab._picker is not None:
        tab._picker.close()
        tab._picker = None
    tab._grid.closePersistentEditor(tab._grid.currentItem() or tab._grid.item(0, 0))
    tab.close()
    tab.deleteLater()


def _open_editor(tab, row, column):
    """`QTest.mouseDClick` does not open a cell editor offscreen — the view ignores the
    synthetic double-click — but the trigger it would use does."""
    from PySide6.QtWidgets import QAbstractItemView
    tab._grid.edit(tab._grid.model().index(row, column),
                   QAbstractItemView.DoubleClicked, None)


def _click_cell(tab, row, column, qapp):
    from PySide6.QtTest import QTest
    rect = tab._grid.visualRect(tab._grid.model().index(row, column))
    QTest.mousePress(tab._grid.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    QTest.mouseRelease(tab._grid.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    for _ in range(3):
        qapp.processEvents()


def _selection(tab):
    return sorted((i.row(), i.column()) for i in tab._grid.selectedIndexes())


def test_double_clicking_a_second_cell_moves_the_highlight_with_it(live_grid, qapp):
    """The bug, end to end.

    Order of events, which is the whole story: the click takes focus from the open editor,
    so the edit COMMITS before `mousePressEvent` ever runs. The restore that follows a
    commit therefore fires first, the click lands second, and the deferred half of that
    restore fires third — putting the highlight back on the cell being left, while the
    editor opens on the cell being clicked.
    """
    _open_editor(live_grid, 0, 0)
    qapp.processEvents()
    assert live_grid._picker_cell == (0, 0), "fixture assumption: the picker attached"

    _click_cell(live_grid, 0, 1, qapp)          # first click of the double-click
    _open_editor(live_grid, 0, 1)               # ...and the second
    for _ in range(4):
        qapp.processEvents()

    assert live_grid._picker_cell == (0, 1), "the new cell did not take the editor"
    assert _selection(live_grid) == [(0, 1)], \
        "the highlight stayed on the cell the user left"


def test_committing_in_place_still_keeps_the_cell(live_grid, qapp):
    """The behaviour `_keep_cell` exists for, verified through the same real path: no
    click means nothing to follow, so the cursor stays where the edit was."""
    _open_editor(live_grid, 1, 0)
    qapp.processEvents()
    live_grid.commit_cell(1, 0, "minecraft:stone")
    for _ in range(4):
        qapp.processEvents()

    assert _selection(live_grid) == [(1, 0)]
