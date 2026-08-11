"""The suggestion picker is part of the grid, not a window floating over it.

It used to be a top-level window positioned in screen coordinates at open time, so moving
the main window left the suggestions behind — hanging over the desktop beside a cell that
had walked away. Docking it into the viewport makes it move with the window by
construction; the price is that it has to fit inside one, which is what `dock_under` is for.
"""
import os

import pytest
from PySide6.QtCore import QPoint, QRect, Qt

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.blueprints import BlueprintStore
from packsmith.gui.blueprint_editor import BlueprintEditorTab
from packsmith.gui.queries import blueprint_query

MARGIN = 10          # PickerPopup._EDGE_MARGIN


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    yield app


class Dump:
    registry = {"minecraft:block": {"values": [f"minecraft:block_{i}" for i in range(500)]}}

    def attribute(self, *a):
        return None


@pytest.fixture
def tab(user_db):
    from PySide6.QtWidgets import QMainWindow

    store = BlueprintStore(user_db)
    store.define("S")
    store.add_slot("S", "base", "registry", registry_type="minecraft:block")
    for i in range(40):
        store.create_instance("S", f"inst{i:02d}")

    window = QMainWindow()
    editor = BlueprintEditorTab(blueprint_query("S"), store, packdump=Dump())
    window.setCentralWidget(editor)
    window.resize(900, 400)
    window.move(100, 100)
    window.show()
    editor._window = window          # keep it alive for the test's lifetime
    yield editor

    if editor._picker is not None:
        editor._picker.close()
        editor._picker = None
    window.close()


def open_editor(tab, row, qapp):
    from PySide6.QtWidgets import QAbstractItemView
    tab._grid.edit(tab._grid.model().index(row, 0), QAbstractItemView.DoubleClicked, None)
    for _ in range(3):
        qapp.processEvents()
    return tab._picker


def cell_rect(tab, row):
    return tab._grid.visualRect(tab._grid.model().index(row, 0))


# --- the reported problem ----------------------------------------------------------------

def test_the_picker_moves_with_the_main_window(tab, qapp):
    """A screen-positioned popup stayed put while the window it belonged to slid away."""
    picker = open_editor(tab, 0, qapp)
    before = picker.mapToGlobal(QPoint(0, 0))

    tab._window.move(400, 250)                 # a 300x150 drag
    for _ in range(3):
        qapp.processEvents()

    after = picker.mapToGlobal(QPoint(0, 0))
    assert after - before == QPoint(300, 150), "the picker did not travel with the window"


def test_the_picker_lives_inside_the_viewport(tab, qapp):
    picker = open_editor(tab, 0, qapp)
    assert picker.parentWidget() is tab._grid.viewport()
    assert not picker.isWindow(), "still a top-level window"


# --- fitting inside, which is what docking costs ------------------------------------------

@pytest.mark.parametrize("row", [0, 2, 4, 6, 8, 9])
def test_it_never_reaches_the_bottom_edge(tab, qapp, row):
    """The cap the user asked for: bounded by the viewport, never flush against it."""
    picker = open_editor(tab, row, qapp)
    if picker is None or not picker.isVisible():
        pytest.skip("cell not visible in this viewport")
    viewport = tab._grid.viewport()

    assert viewport.rect().contains(picker.geometry()), \
        f"row {row}: {picker.geometry()} escapes {viewport.rect()}"
    assert viewport.height() - picker.geometry().bottom() >= MARGIN, \
        f"row {row}: only {viewport.height() - picker.geometry().bottom()}px of clearance"


def test_a_cell_near_the_bottom_opens_upward(tab, qapp):
    """Capping alone would leave a two-row sliver. Flipping gives it the roomier side."""
    viewport = tab._grid.viewport()
    low = max(r for r in range(40)
              if cell_rect(tab, r).isValid()
              and cell_rect(tab, r).bottom() < viewport.height())
    picker = open_editor(tab, low, qapp)

    assert picker.geometry().bottom() <= cell_rect(tab, low).top(), \
        "the list covered the cell being edited instead of opening above it"


def test_nothing_is_dropped_from_the_list_when_it_is_capped(tab, qapp):
    """Capping changes how many rows you see at once, never how many candidates exist."""
    picker = open_editor(tab, 4, qapp)
    assert picker.match_count("") == 500
    assert picker._list.verticalScrollBar().maximum() > 0, "capped but not scrollable"


# --- scrolling ---------------------------------------------------------------------------

def test_scrolling_carries_the_picker_with_its_cell(tab, qapp):
    """Being a child makes it follow the *window*; the scroll still has to be tracked."""
    picker = open_editor(tab, 2, qapp)
    before = picker.geometry().top()

    bar = tab._grid.verticalScrollBar()
    bar.setValue(bar.value() + 2)
    for _ in range(3):
        qapp.processEvents()

    assert picker.geometry().top() != before, "the picker stayed put while the cell moved"
    assert tab._grid.viewport().rect().contains(picker.geometry())


def test_scrolling_the_cell_out_of_sight_hides_the_list(tab, qapp):
    picker = open_editor(tab, 0, qapp)
    assert picker.isVisible()

    bar = tab._grid.verticalScrollBar()
    bar.setValue(bar.value() + 12)
    for _ in range(3):
        qapp.processEvents()

    assert not picker.isVisible(), "suggestions for a cell you can no longer see"


# --- exactly one picker exists at a time --------------------------------------------------

def _all_pickers(tab):
    from packsmith.gui.shell.picker import PickerPopup
    return tab._grid.viewport().findChildren(PickerPopup)


def _visible_pickers(tab):
    return [p for p in _all_pickers(tab) if p.isVisible()]


def _click(tab, row, column, qapp):
    from PySide6.QtTest import QTest
    rect = tab._grid.visualRect(tab._grid.model().index(row, column))
    QTest.mousePress(tab._grid.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    QTest.mouseRelease(tab._grid.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    for _ in range(2):
        qapp.processEvents()


def _visit(tab, row, column, qapp):
    from PySide6.QtWidgets import QAbstractItemView
    _click(tab, row, column, qapp)
    tab._grid.edit(tab._grid.model().index(row, column),
                   QAbstractItemView.DoubleClicked, None)
    for _ in range(3):
        qapp.processEvents()


def test_visiting_many_cells_leaves_one_picker(tab, qapp):
    """Dragging across a row opened one per cell and none of them went away.

    Docking made the picker a child widget, and `close()` on a child only *hides* it — it
    stays in the viewport's child list, still wired to the scrollbars.
    """
    for row in range(6):
        _visit(tab, row, 0, qapp)

    assert len(_visible_pickers(tab)) <= 1, "a trail of suggestion lists was left behind"
    assert len(_all_pickers(tab)) <= 1, "retired pickers are still alive in the viewport"


def test_scrolling_does_not_resurrect_retired_pickers(tab, qapp):
    """The second half: a dead picker's scroll handler repositioned it, and repositioning
    calls show(). Autoscrolling during a drag brought every one of them back."""
    for row in range(4):
        _visit(tab, row, 0, qapp)

    bar = tab._grid.verticalScrollBar()
    for delta in (1, 2, -1, 3):
        bar.setValue(max(0, bar.value() + delta))
        for _ in range(2):
            qapp.processEvents()

    assert len(_visible_pickers(tab)) <= 1, "scrolling brought old suggestion lists back"


def test_the_surviving_picker_belongs_to_the_current_cell(tab, qapp):
    """One left is only correct if it is the right one."""
    _visit(tab, 0, 0, qapp)
    _visit(tab, 3, 0, qapp)

    assert tab._picker_cell == (3, 0)
    assert tab._picker is not None and tab._picker.isVisible()


def test_the_tab_forgets_a_picker_that_retired_itself(tab, qapp):
    """A picker dies with its editor, so the tab's reference can outlive the C++ object —
    and PySide raises on any use of a deleted wrapper, not just a bad call."""
    _visit(tab, 0, 0, qapp)
    picker = tab._picker
    picker.dismiss()
    for _ in range(3):
        qapp.processEvents()

    assert tab._picker is None, "the tab still points at a retired picker"
    _visit(tab, 1, 0, qapp)          # must not raise RuntimeError
    assert tab._picker is not None
