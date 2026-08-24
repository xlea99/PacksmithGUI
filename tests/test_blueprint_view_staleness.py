"""A registry view going stale from a BLUEPRINT edit (design 3.2.4, 3.2.3).

Registry views have always gone stale from a tag edit — `_on_tags_written` marks every other
open view, and `_reconcile` re-runs it when you look at it. Nothing did the same for
blueprints, and until `BoundIn` / `Mentions` nothing needed to: no registry view could depend
on a blueprint at all.

The leftover view is the case that exposes it. Bind `quark:granite_pillar` into the grid and
it should leave "everything I did not choose" — instead it sat there, correct-looking and
wrong, until the tab was closed and reopened. A view that is quietly out of date is worse
than one that is obviously broken: you make decisions from it.

Two things have to hold, and they pull against each other:

* the view that reads a blueprint **does** refresh;
* the views that do not **are not** dragged through a re-evaluation of 18,639 rows for an
  edit that cannot possibly affect them.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.blueprints import BlueprintStore
from packsmith.core.query.ast import (
    And, BoundIn, Blueprint, Cmp, Id, Mentions, Mod, Not, Query, Registry, Tag,
    blueprints_read)
from packsmith.core.tags import TagStore
from packsmith.gui.table.registry_table_model import RegistryTableModel

REG = "minecraft:item"


class Dump:
    registry = {REG: {"values": [
        "minecraft:granite", "quark:granite_pillar", "stoneworks:granite_pillar",
        "minecraft:oak_planks"]}}

    def attribute(self, *a, **k):
        return None


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def blueprints(user_db):
    store = BlueprintStore(user_db, packdump=Dump())
    store.define("StoneType")
    store.add_slot("StoneType", "base", "registry", registry_type=REG)
    store.add_group("StoneType", "pillar")
    store.add_slot("StoneType", "base", "registry", parent="pillar", registry_type=REG)
    store.create_instance("StoneType", "granite")
    store.bind("StoneType", "granite", "base", "minecraft:granite")
    return store


LEFTOVERS = Query(scope=Registry(REG), select=[Id],
                  filter=And([Mentions("StoneType"), Not(BoundIn("StoneType"))]),
                  order_by=[Id])


def rows(model):
    return [model.entry_at_row(r) for r in range(model.rowCount())]


# --- which queries care ------------------------------------------------------------------

def test_a_query_can_say_which_blueprints_it_reads():
    """Asked of the query rather than tracked alongside it — a hand-kept list drifts from
    what the query actually does, and the drift is invisible until a view goes stale."""
    assert blueprints_read(LEFTOVERS) == {"StoneType"}
    assert blueprints_read(Query(scope=Registry(REG), select=[Id],
                                 filter=Cmp(Mod, "eq", "quark"))) == set()
    assert blueprints_read(Query(scope=Blueprint("StoneType"), select=[Id])) ==         {"StoneType"}


def test_it_finds_them_however_deeply_nested():
    buried = Query(scope=Registry(REG), select=[Id],
                   filter=Not(And([BoundIn("A"), Not(Mentions("B"))])))
    assert blueprints_read(buried) == {"A", "B"}


# --- the refresh itself --------------------------------------------------------------------

def test_binding_a_cell_takes_it_out_of_the_leftovers(user_db, blueprints, qapp):
    """The reported bug, end to end at the model level."""
    tags = TagStore(user_db)
    model = RegistryTableModel(LEFTOVERS, Dump(), tags, blueprint_store=blueprints)
    assert "quark:granite_pillar" in rows(model)

    blueprints.bind("StoneType", "granite", "pillar.base", "quark:granite_pillar")
    model.reevaluate()

    assert "quark:granite_pillar" not in rows(model)
    assert "stoneworks:granite_pillar" in rows(model), "the others are still leftovers"


def test_unbinding_puts_it_back(user_db, blueprints, qapp):
    tags = TagStore(user_db)
    blueprints.bind("StoneType", "granite", "pillar.base", "quark:granite_pillar")
    model = RegistryTableModel(LEFTOVERS, Dump(), tags, blueprint_store=blueprints)
    assert "quark:granite_pillar" not in rows(model)

    blueprints.unbind("StoneType", "granite", "pillar.base")
    model.reevaluate()

    assert "quark:granite_pillar" in rows(model)


# --- marking: the right tabs, and only those -----------------------------------------------

class _FakeModel:
    def __init__(self, query):
        self._query = query


def _window(**models):
    """The marking rule alone. A real MainWindow needs a profile, a packdump and a shell;
    what is being tested is which tabs it writes into `_stale_tabs`."""
    from packsmith.gui.main_window import MainWindow
    window = MainWindow.__new__(MainWindow)
    window._tab_models = {name: _FakeModel(q) for name, q in models.items()}
    window._stale_tabs = set()
    window._blueprints_panel = type("P", (), {"refresh": lambda self: None})()
    window._bottom = type("B", (), {"panel": lambda self, name: None})()
    return window


def test_a_blueprint_edit_marks_the_view_that_reads_it(qapp):
    window = _window(leftovers=LEFTOVERS)
    window._after_blueprint_change()
    assert window._stale_tabs == {"leftovers"}


def test_it_does_not_drag_in_views_that_cannot_be_affected(qapp):
    """18,639 rows re-evaluated because a cell changed in a blueprint the view never
    mentions is exactly the kind of cost that makes a feature feel slow for no reason."""
    window = _window(leftovers=LEFTOVERS,
                     plain=Query(scope=Registry(REG), select=[Id, Tag("remove")]))
    window._after_blueprint_change()
    assert window._stale_tabs == {"leftovers"}


def test_marked_rather_than_refreshed(qapp):
    """Same rule as tag writes: the reconcile happens when you look at the tab, so nothing
    re-runs a query nobody is watching — and the row cannot vanish under your cursor."""
    window = _window(leftovers=LEFTOVERS)
    window._after_blueprint_change()
    assert window._stale_tabs, "it refreshed eagerly instead of marking"
