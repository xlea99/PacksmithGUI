"""Two views over one registry, and a write in one of them — design 3.2.4, 5.1.

Each model caches its rows when the query is evaluated, patches only its own cell on its
own edit, and has no way to learn that the store moved underneath it. With `All Items` and
`Remove` both open that produced two failures of different severity:

* the row you just ticked never appeared in `Remove` — **stale membership**, an out-of-date
  answer to the query;
* the row you *unticked* stayed ticked in `Remove` — **stale values**, a table showing a
  value that is not in the database. Strictly worse: you can act on it.

Neither announces itself, which is what earns them permanent tests.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from packsmith.core.query.ast import Cmp, Id, Query, Registry, Tag
from packsmith.gui.table.registry_table_model import RegistryTableModel

REG = "minecraft:item"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class Dump:
    registry = {REG: {"values": ["mod:a", "mod:b", "mod:c"]}}

    def attribute(self, *a):
        return None


@pytest.fixture
def views(tags):
    """`All Items` and `Remove`, over one store — the reported setup."""
    tags.define(REG, "remove", "bool", default=False)
    everything = RegistryTableModel(
        Query(scope=Registry(REG), select=[Id, Tag("remove")], order_by=[Id]), Dump(), tags)
    removals = RegistryTableModel(
        Query(scope=Registry(REG), select=[Id, Tag("remove")],
              filter=Cmp(Tag("remove"), "eq", True), order_by=[Id]), Dump(), tags)
    everything._confirm_takeover = lambda cells: True
    return everything, removals, tags


def rows_of(model):
    return [model.entry_at_row(r) for r in range(model.rowCount())]


def shown(model, entry_id):
    for r in range(model.rowCount()):
        if model.entry_at_row(r) == entry_id:
            return model.data(model.index(r, 1), Qt.DisplayRole)
    return None


# --- the signal that makes it possible ---------------------------------------------------

def test_a_write_announces_itself(views):
    """`dataChanged` says "this cell changed" to this model's own views. Nothing said "the
    store moved", which is the fact another model needs and cannot otherwise learn."""
    everything, _, _ = views
    heard = []
    everything.tags_written.connect(lambda: heard.append(True))

    everything.setData(everything.index(0, 1), True, Qt.EditRole)

    assert heard == [True]


def test_bulk_edits_and_undo_announce_too(views):
    """Undo and bulk operations go through `emit_all_data_changed`, not `setData` — a
    signal wired only to the single-cell path would leave the other view stale after a
    Ctrl+Z, which is a nastier bug than the one being fixed."""
    everything, _, _ = views
    everything.setData(everything.index(0, 1), True, Qt.EditRole)
    heard = []
    everything.tags_written.connect(lambda: heard.append(True))

    everything.undo()

    assert heard == [True]


# --- what the other view does about it -----------------------------------------------------

def test_a_re_evaluated_view_picks_up_a_new_member(views):
    """Stale membership: the row you ticked belongs in `Remove` now."""
    everything, removals, _ = views
    assert rows_of(removals) == []

    everything.setData(everything.index(0, 1), True, Qt.EditRole)
    removals.reevaluate()

    assert rows_of(removals) == ["mod:a"]


def test_a_re_evaluated_view_drops_a_member_that_left(views):
    everything, removals, tags = views
    tags.assign(REG, "mod:a", "remove", True)
    removals.reevaluate()
    assert rows_of(removals) == ["mod:a"]

    everything.setData(everything.index(0, 1), False, Qt.EditRole)
    removals.reevaluate()

    assert rows_of(removals) == []


def test_the_worse_bug_a_stale_value_on_screen(views):
    """The reported one: unticked in `All Items`, still ticked in `Remove`.

    Checked against the *displayed* value rather than the row set, because a table showing
    a value the database does not hold is the failure that lets you act on false
    information — and it survives even when membership happens to be right.
    """
    everything, removals, tags = views
    tags.assign(REG, "mod:a", "remove", True)
    tags.assign(REG, "mod:b", "remove", True)
    removals.reevaluate()
    assert shown(removals, "mod:a") == "true"

    everything.setData(everything.index(0, 1), False, Qt.EditRole)
    removals.reevaluate()

    assert shown(removals, "mod:a") is None, "it should have left the view entirely"
    assert shown(removals, "mod:b") == "true", "the row that did not change was disturbed"


def test_the_editing_view_keeps_the_row_it_just_changed(views):
    """The reason this is reconciled on activation rather than on write: re-evaluating the
    tab you are working in would delete the row from under your cursor mid-click."""
    _, removals, tags = views
    tags.assign(REG, "mod:a", "remove", True)
    removals.reevaluate()
    removals._confirm_takeover = lambda cells: True

    removals.setData(removals.index(0, 1), False, Qt.EditRole)

    assert rows_of(removals) == ["mod:a"], "the row vanished mid-edit"
    assert shown(removals, "mod:a") == "false", "but it must show the truth"


# --- where a refinement is remembered (design 3.2.3) ---------------------------------
#
# Only for **saved Views**. A tab you got by clicking a registry or a tag is a scratch
# surface — you opened it to look at something, and it should open clean rather than
# resuming a search you have forgotten making. A View is a thing you named and came back
# to, so where you had got to in it is part of coming back.

def test_only_saved_views_have_somewhere_to_remember(qapp):
    """The key is the whole mechanism: a View is keyed by its id, so two Views cannot
    share a filter; a browse tab is keyed by registry and is never written."""
    from packsmith.gui.main_window import MainWindow

    class V:
        id = 7

    assert MainWindow._layout_key(V(), "minecraft:item") == "view:7"
    assert MainWindow._layout_key(None, "minecraft:item") == "browse:minecraft:item"


def test_two_views_over_one_registry_do_not_share_a_filter(qapp):
    from packsmith.gui.main_window import MainWindow

    first = MainWindow._layout_key(type("V", (), {"id": 1})(), "minecraft:item")
    second = MainWindow._layout_key(type("V", (), {"id": 2})(), "minecraft:item")
    assert first != second
