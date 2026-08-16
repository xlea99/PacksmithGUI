"""How a View is arranged, remembered per View — design 5.1.

A View is one saved configuration, and arranging it is part of configuring it. The rule
that makes it work is that **everything is keyed by name, never by position**: a row's
height by its entry id, a column's width by its column name.

Qt makes the opposite easy. `QHeaderView` section sizes are positional *and* they survive
a model reset, so a tall row silently becomes "whatever entry is at index 4 now" the first
time you sort. That failure is invisible — the table still looks arranged, just around the
wrong rows — which is why it is worth pinning.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from packsmith.core.query.ast import Id, Query, Registry, Tag
from packsmith.gui.table.registry_table_model import RegistryTableModel
from packsmith.gui.table.registry_table_view import RegistryTableView
from packsmith.gui.table.table_layout import TableLayout

REG = "minecraft:item"
ROW_H = 28


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class Dump:
    registry = {REG: {"values": ["mod:a", "mod:b", "mod:c", "mod:d"]}}

    def attribute(self, *a):
        return None


@pytest.fixture
def table(tags):
    tags.define(REG, "tier", "string")
    model = RegistryTableModel(
        Query(scope=Registry(REG), select=[Id, Tag("tier")], order_by=[Id]), Dump(), tags)
    view = RegistryTableView()
    view.setModel(model)
    view.verticalHeader().setDefaultSectionSize(ROW_H)
    view.verticalHeader().setMinimumSectionSize(ROW_H)
    yield view, model
    view.deleteLater()


def make(table, stored=None):
    """Parented to the view, exactly as `_build_view_tab` does it.

    Without a parent the layout is only kept alive by whatever holds the return value —
    and a test that ignores it silently loses every signal connection, so the re-apply on
    sort never runs and the failure looks like a bug in the code under test.
    """
    view, model = table
    layout = TableLayout(view, model, ROW_H, stored, parent=view)
    layout.restore()
    return layout


def height_of(view, model, entry_id):
    for row in range(model.rowCount()):
        if model.entry_at_row(row) == entry_id:
            return view.verticalHeader().sectionSize(row)
    return None


# --- rows follow their entry, not their position ---------------------------------------

def test_a_tall_row_is_remembered_against_its_entry(table):
    view, model = table
    layout = make(table)
    view.verticalHeader().resizeSection(1, 70)

    assert layout.as_dict()["rows"] == {"mod:b": 70}


def test_a_tall_row_follows_its_entry_through_a_sort(table):
    """The whole point. Section sizes are positional and survive a reset, so without
    re-application the 70px would stay at index 1 and belong to whatever landed there."""
    view, model = table
    make(table)
    view.verticalHeader().resizeSection(1, 70)
    assert model.entry_at_row(1) == "mod:b"

    model.sort(0, Qt.DescendingOrder)

    assert model.entry_at_row(1) != "mod:b", "the sort did not actually move it"
    assert height_of(view, model, "mod:b") == 70
    assert view.verticalHeader().sectionSize(1) == ROW_H, \
        "the height stayed at the index instead of following the entry"


def test_every_other_row_returns_to_the_default(table):
    view, model = table
    make(table)
    view.verticalHeader().resizeSection(1, 70)
    model.sort(0, Qt.DescendingOrder)

    others = [view.verticalHeader().sectionSize(r) for r in range(model.rowCount())
              if model.entry_at_row(r) != "mod:b"]
    assert others == [ROW_H] * 3


def test_dragging_a_row_back_to_default_forgets_it(table):
    """Rather than storing 28 forever, which would pin the row against a later change to
    the default height."""
    view, model = table
    layout = make(table)
    view.verticalHeader().resizeSection(1, 70)
    view.verticalHeader().resizeSection(1, ROW_H)

    assert "rows" not in layout.as_dict()


def test_a_stored_height_is_restored_on_open(table):
    view, model = table
    make(table, {"rows": {"mod:c": 64}})
    assert height_of(view, model, "mod:c") == 64
    assert height_of(view, model, "mod:a") == ROW_H


def test_restoring_does_not_look_like_the_user_resizing(table):
    """Applying stored heights fires `sectionResized` too. Recording those would be
    harmless here and wrong the moment restoration is partial."""
    view, model = table
    layout = make(table, {"rows": {"mod:c": 64}})
    assert layout.as_dict()["rows"] == {"mod:c": 64}


# --- columns ---------------------------------------------------------------------------

def test_a_column_width_is_remembered_by_name(table):
    view, model = table
    layout = make(table)
    view.setColumnWidth(1, 220)
    assert layout.as_dict()["columns"]["tier"] == 220


def test_a_stored_width_is_restored(table):
    view, model = table
    make(table, {"columns": {"tier": 200}})
    assert view.columnWidth(1) == 200


def test_a_width_for_a_column_the_query_no_longer_has_is_ignored(table):
    """Editing a query drops columns. A stored width for one that is gone must not land on
    whatever now occupies that position."""
    view, model = table
    make(table, {"columns": {"gone": 500, "tier": 150}})
    assert view.columnWidth(1) == 150


# --- sort ------------------------------------------------------------------------------

def test_the_sort_is_remembered_by_column_name(table):
    view, model = table
    layout = make(table)
    view.sortByColumn(1, Qt.DescendingOrder)
    assert layout.as_dict()["sort"] == ["tier", "desc"]


def test_a_stored_sort_is_restored(table):
    view, model = table
    make(table, {"sort": ["tier", "desc"]})
    assert view.horizontalHeader().sortIndicatorSection() == 1
    assert view.horizontalHeader().sortIndicatorOrder() == Qt.DescendingOrder


def test_a_sort_on_a_column_that_is_gone_is_dropped_quietly(table):
    """The query was edited. Opening the view in its natural order is the right answer,
    and it is not worth an error about."""
    view, model = table
    make(table, {"sort": ["vanished", "asc"]})       # must not raise


# --- nothing customised costs nothing ---------------------------------------------------

def test_an_untouched_table_stores_nothing(table):
    layout = make(table)
    assert layout.as_dict() == {}
