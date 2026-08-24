"""Building a view whose filter the visual builder cannot draw (design 3.2.3).

The constructor has always had a text mode, and it was **only reachable by accident**: it
triggered when an EXISTING query failed to decompose into rows. A new view starts from no
filter, so nothing could fail to decompose, so nothing could ever open it — every query you
could save was one the row widgets already knew how to draw.

That is a hole with a growing edge. The language keeps outgrowing the builder — token
matching, nested groups, and now `MENTIONS` / `BOUND_IN`, which express "what did I not
choose", the question the blueprint primitive implies and has no other way to ask. Each one
was expressible in the engine, parseable from text, saveable as JSON, and unreachable from
the app.

What must not regress: a filter surviving the trip through the dialog **unchanged**.
Silently rewriting one into whatever the rows happen to hold is the bug the reproduce-check
guards, and it is worse coming back from text, because there the user typed the thing being
discarded.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.query.ast import (
    And, BoundIn, Cmp, Id, Mentions, Mod, Not, Query, Registry, Tag)
from packsmith.core.query.language import parse
from packsmith.core.tags import TagStore

LEFTOVERS = 'MENTIONS "StoneType" AND NOT BOUND_IN "StoneType"'


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def tags(user_db):
    store = TagStore(user_db)
    store.define("minecraft:item", "remove", "bool")
    return store


def dialog(tags, *, query=None, new_view=True):
    from packsmith.gui.query_constructor import QueryConstructorDialog
    return QueryConstructorDialog(tags, "minecraft:item", query=query,
                                  new_view=new_view, registries=["minecraft:item"])


def in_text_mode(dlg):
    return getattr(dlg, "_text_edit", None) is not None


# --- the hole ---------------------------------------------------------------------------

def test_a_new_view_can_reach_text_mode_at_all(tags, qapp):
    """The whole bug: it started empty, so the fallback never fired, so there was no way in."""
    dlg = dialog(tags)
    assert not in_text_mode(dlg), "it should still open as rows"

    dlg._toggle_mode()

    assert in_text_mode(dlg)


def test_a_blueprint_question_survives_being_saved(tags, qapp):
    """`MENTIONS ... AND NOT BOUND_IN ...` has no row shape and never will — it is a
    question about a blueprint, not a comparison of a field."""
    dlg = dialog(tags)
    dlg._toggle_mode()
    dlg._text_edit.setText(LEFTOVERS)
    dlg._name_edit.setText("Stone Leftovers")
    dlg.accept()

    assert dlg.result_filter == parse(LEFTOVERS)
    assert dlg.result_query.filter == parse(LEFTOVERS)
    assert dlg.result_name == "Stone Leftovers"


def test_the_tag_column_rides_along(tags, qapp):
    """The point of the view is to DECIDE in it, not just to look — so `remove` has to be
    selectable as a column beside the ids."""
    dlg = dialog(tags)
    dlg._toggle_mode()
    dlg._text_edit.setText(LEFTOVERS)
    for label, check in dlg._col_checks.items():
        check.setChecked(label in ("id", "remove"))
    dlg.accept()

    assert [getattr(f, "name", "id") for f in dlg.result_query.select] == ["id", "remove"]


# --- carrying work across the switch ------------------------------------------------------

def test_switching_to_text_keeps_what_the_rows_said(tags, qapp):
    """Otherwise the button is a trap: you build three conditions, click it to add a fourth
    the builder can't draw, and lose the three."""
    dlg = dialog(tags)
    row = dlg._add_row(preset=Cmp(Mod, "eq", "quark"))
    assert dlg.build_filter() is not None

    dlg._toggle_mode()

    assert dlg._text_edit.text() == 'mod == "quark"'


def test_switching_back_restores_the_rows(tags, qapp):
    dlg = dialog(tags)
    dlg._toggle_mode()
    dlg._text_edit.setText("mod == quark")

    dlg._toggle_mode()

    assert not in_text_mode(dlg)
    assert dlg.build_filter() == Cmp(Mod, "eq", "quark")


def test_what_the_rows_cannot_hold_stays_text(tags, qapp):
    """The refusal that matters. Going back to rows must not quietly rewrite the filter
    into whatever they happen to express — the user typed this one."""
    from PySide6.QtWidgets import QMessageBox
    dlg = dialog(tags)
    dlg._toggle_mode()
    dlg._text_edit.setText(LEFTOVERS)
    told = []
    QMessageBox.information = staticmethod(lambda *a, **k: told.append(a[2]))

    dlg._toggle_mode()

    assert in_text_mode(dlg), "it fell back to rows and lost the filter"
    assert dlg._text_edit.text() == LEFTOVERS
    assert told, "and it said so"


def test_unreadable_text_refuses_rather_than_discarding_it(tags, qapp):
    from PySide6.QtWidgets import QMessageBox
    dlg = dialog(tags)
    dlg._toggle_mode()
    dlg._text_edit.setText("MENTIONS AND AND")
    warned = []
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a[2]))

    dlg._toggle_mode()

    assert in_text_mode(dlg) and warned
    assert dlg._text_edit.text() == "MENTIONS AND AND", "the text you typed is still there"


# --- the saved view has to RUN --------------------------------------------------------------

def test_the_view_renders_and_needs_a_blueprint_store_to_do_it(user_db, tags, qapp):
    """`evaluate` refuses a blueprint question without a store, and the table model is
    where a saved view actually runs — so this is the wiring that makes the feature real
    rather than merely saveable."""
    from packsmith.core.blueprints import BlueprintStore
    from packsmith.core.query.evaluator import evaluate
    from packsmith.core.query.ast import QueryError
    from packsmith.gui.table.registry_table_model import RegistryTableModel

    class Dump:
        registry = {"minecraft:item": {"values": [
            "minecraft:granite", "quark:granite_pillar", "minecraft:oak_planks"]}}
        def attribute(self, *a, **k): return None

    bp = BlueprintStore(user_db, packdump=Dump())
    bp.define("StoneType")
    bp.add_slot("StoneType", "base", "registry", registry_type="minecraft:item")
    bp.create_instance("StoneType", "granite")
    bp.bind("StoneType", "granite", "base", "minecraft:granite")

    query = Query(scope=Registry("minecraft:item"), select=[Id, Tag("remove")],
                  filter=parse(LEFTOVERS), order_by=[Id])

    with pytest.raises(QueryError, match="needs a blueprint store"):
        evaluate(query, packdump=Dump(), tag_store=tags)

    model = RegistryTableModel(query, Dump(), tags, blueprint_store=bp)
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0)) == "quark:granite_pillar"
