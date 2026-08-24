"""Rows that no longer match, still on screen (design 3.2.3, 5.1).

The view you are editing in deliberately never re-evaluates — `test_cross_view_staleness`
pins that, because the row you just ticked vanishing from under the cursor mid-click is a
worse bug than seeing it a moment longer. The cost of that rule is a quieter problem: the
row stays looking *exactly like the rows that still belong*, so a triage list gives you no
way to tell what you have already dealt with.

So the row is tinted rather than removed. The contract is untouched — membership still only
changes on a real re-evaluation — and the display now tells the truth about what the
membership will be.

The failure mode this guards is the silent kind in both directions: a row that has left
looking like it belongs, and a row that belongs looking like it has left. Neither announces
itself, and either one makes you act on a list you have misread.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from packsmith.core.query.ast import Cmp, Id, Query, Registry, Tag
from packsmith.core.tags import TagStore
from packsmith.gui.table.registry_table_model import RegistryTableModel

REG = "minecraft:item"


class Dump:
    registry = {REG: {"values": ["mod:a", "mod:b", "mod:c"]}}

    def attribute(self, *a, **k):
        return None


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def model(user_db):
    """A triage view: everything not yet excepted."""
    tags = TagStore(user_db)
    tags.define(REG, "exception", "bool", default=False)
    query = Query(scope=Registry(REG), select=[Id, Tag("exception")],
                  filter=Cmp(Tag("exception"), "eq", False), order_by=[Id])
    m = RegistryTableModel(query, Dump(), tags)
    m._confirm_takeover = lambda cells: True
    return m


def bg(model, row):
    return model.data(model.index(row, 0), Qt.BackgroundRole)


def fg(model, row):
    return model.data(model.index(row, 0), Qt.ForegroundRole)


def rows(model):
    return [model.entry_at_row(r) for r in range(model.rowCount())]


# --- the point ---------------------------------------------------------------------------

def test_a_row_that_stops_matching_is_tinted_and_dimmed(model):
    assert bg(model, 0) is None and fg(model, 0) is None

    model.setData(model.index(0, 1), True, Qt.EditRole)

    assert bg(model, 0) is not None, "no way to tell it has been dealt with"
    assert fg(model, 0) is not None


def test_it_is_still_there(model):
    """The contract this must not break: the row you just changed stays put."""
    model.setData(model.index(0, 1), True, Qt.EditRole)

    assert rows(model) == ["mod:a", "mod:b", "mod:c"]
    assert model.data(model.index(0, 1)) == "true", "and shows the truth"


def test_the_whole_row_tints_not_just_the_cell(model):
    """A tint on one cell reads as "this cell"; what changed is the row's right to be
    here at all."""
    model.setData(model.index(0, 1), True, Qt.EditRole)

    assert all(model.data(model.index(0, c), Qt.BackgroundRole) is not None
               for c in range(model.columnCount()))


def test_the_other_rows_are_left_alone(model):
    model.setData(model.index(0, 1), True, Qt.EditRole)

    assert bg(model, 1) is None and bg(model, 2) is None


def test_an_edit_that_keeps_the_row_tints_nothing(model):
    """Setting it back to the value the filter wants is not a departure."""
    model.setData(model.index(0, 1), False, Qt.EditRole)
    assert bg(model, 0) is None


# --- coming back ---------------------------------------------------------------------------

def test_undo_un_tints_it(model):
    """A row painted as leaving, that is no longer leaving, is the same lie the other way
    round — and undo is exactly when that happens."""
    model.setData(model.index(0, 1), True, Qt.EditRole)
    assert bg(model, 0) is not None

    model.undo()

    assert bg(model, 0) is None
    assert model.data(model.index(0, 1)) == "false"


def test_redo_tints_it_again(model):
    model.setData(model.index(0, 1), True, Qt.EditRole)
    model.undo()
    model.redo()
    assert bg(model, 0) is not None


def test_a_real_re_evaluation_clears_the_marks(model):
    """Re-evaluating IS the reconcile: what left is gone from the rows, what came back
    belongs again. Carrying the set across would tint rows that are perfectly fine."""
    model.setData(model.index(0, 1), True, Qt.EditRole)

    model.reevaluate()

    assert rows(model) == ["mod:b", "mod:c"], "the row has actually left now"
    assert model._departing == set()
    assert bg(model, 0) is None


def test_an_edit_made_elsewhere_shows_up_on_the_next_bulk_refresh(model, user_db):
    """`emit_all_data_changed` is the path bulk edits, undo and action runs all share, so
    the recheck lives there rather than in each caller."""
    tags = TagStore(user_db)
    tags.assign(REG, "mod:c", "exception", True)

    model.emit_all_data_changed()

    assert bg(model, 2) is not None
    assert bg(model, 0) is None


# --- not paying for it when there is nothing to pay for ---------------------------------------

def test_a_view_with_no_filter_never_marks_anything(user_db):
    """Nothing can stop matching a filter that does not exist — and this is the case where
    the row count is the whole registry, so the check must not run at all."""
    tags = TagStore(user_db)
    tags.define(REG, "exception", "bool", default=False)
    unfiltered = RegistryTableModel(
        Query(scope=Registry(REG), select=[Id, Tag("exception")], order_by=[Id]),
        Dump(), tags)
    unfiltered._confirm_takeover = lambda cells: True

    unfiltered.setData(unfiltered.index(0, 1), True, Qt.EditRole)

    assert unfiltered._departing == set()
    assert bg(unfiltered, 0) is None


def test_a_filter_the_engine_cannot_answer_tints_nothing(user_db):
    """Delete the blueprint a `MENTIONS` view names, while the view is open.

    `all_bindings` then raises **BlueprintError**, from underneath the query engine — not
    QueryError, which is what the first version of this guard caught and why it caught
    nothing. Unguarded it escapes through `setData`, i.e. on every keystroke in the view.

    The rows on screen are still the last good answer, so they stay unmarked; tinting all
    of them would say every one had been dealt with. (Undefining a filter's TAG does not
    come here — it resolves to None and stops matching, so every row tints, which is true:
    they are all leaving.)
    """
    from packsmith.core.blueprints import BlueprintStore
    from packsmith.core.query.language import parse

    tags = TagStore(user_db)
    tags.define(REG, "exception", "bool", default=False)
    blueprints = BlueprintStore(user_db, packdump=Dump())
    blueprints.define("StoneType")
    blueprints.add_slot("StoneType", "base", "registry", registry_type=REG)

    query = Query(scope=Registry(REG), select=[Id, Tag("exception")],
                  filter=parse('MENTIONS "StoneType"'), order_by=[Id])
    model = RegistryTableModel(query, Dump(), tags, blueprint_store=blueprints)
    model._confirm_takeover = lambda cells: True

    blueprints.delete("StoneType")

    model._mark_departing(["mod:a"])                      # must not raise
    model.emit_all_data_changed()                         # nor here

    assert model._departing == set()
