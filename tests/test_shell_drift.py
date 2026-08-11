"""Shell behaviours that drifted from §4.1 — panels, menus, and the status bar.

Small things individually. What they share is that each one silently drops information the
user just supplied (which instance you clicked) or asserts something that stopped being
true (which tab the status bar is describing).
"""
import os

import pytest

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
