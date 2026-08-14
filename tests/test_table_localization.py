"""Localization fallback in the registry table (design 3.1).

§3.1: "If a display name isn't available in the current locale, the raw registry ID is
shown." `packdump.attribute()` returns None and delegates that `or entry_id` to callers —
and no caller did it, so on a real pack 130 of 135 registries rendered a column of blanks.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.query.ast import Attribute, Id, Query, Registry
from packsmith.gui.table.registry_table_model import RegistryTableModel

REG = "minecraft:fluid"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class Dump:
    """Two entries: one the pack localized, one it didn't."""
    registry = {REG: {"values": ["mod:named", "mod:bare"]}}

    def attribute(self, registry_type, entry_id, name):
        if name == "localization" and entry_id == "mod:named":
            return "A Nice Name"
        return None


@pytest.fixture
def model(tags):
    query = Query(scope=Registry(REG), select=[Id, Attribute("localization")],
                  order_by=[Id])
    return RegistryTableModel(query, Dump(), tags)


def _row_of(model, entry_id):
    from PySide6.QtCore import Qt
    for r in range(model.rowCount()):
        if model.data(model.index(r, 0), Qt.DisplayRole) == entry_id:
            return r
    raise AssertionError(f"{entry_id} not in the model")


def test_an_unlocalized_entry_shows_its_raw_id(model):
    from PySide6.QtCore import Qt
    row = _row_of(model, "mod:bare")
    assert model.data(model.index(row, 1), Qt.DisplayRole) == "mod:bare"


def test_a_localized_entry_is_untouched(model):
    from PySide6.QtCore import Qt
    row = _row_of(model, "mod:named")
    assert model.data(model.index(row, 1), Qt.DisplayRole) == "A Nice Name"
    assert model.data(model.index(row, 1), Qt.ForegroundRole) is None


def test_the_fallback_reads_muted_so_it_is_distinguishable(model):
    """It's real content but not the entry's own name — you should be able to see at a
    glance which rows a mod never localized."""
    from PySide6.QtCore import Qt
    row = _row_of(model, "mod:bare")
    assert model.data(model.index(row, 1), Qt.ForegroundRole) is not None


def test_the_fallback_is_display_only_and_does_not_invent_data(model, tags):
    """Applied at render, NOT in the resolver — inflating the value there would make
    `HAS a:localization` true for every entry, which is exactly what Q-3 was."""
    from packsmith.core.query.evaluator import evaluate
    from packsmith.core.query.ast import Has, Not
    q = Query(scope=Registry(REG), select=[Id],
              filter=Not(Has(Attribute("localization"))))
    ids = [r.values["id"] for r in evaluate(q, packdump=Dump(), tag_store=tags).rows]
    assert ids == ["mod:bare"], "existence must still be existence"


def test_the_fallback_sorts_with_the_ids_rather_than_sinking_as_blank(model):
    """The sort proxy reads DisplayRole, so filling the cell fixes ordering for free —
    blanks used to sink to the bottom regardless of direction."""
    from PySide6.QtCore import Qt
    values = [model.data(model.index(r, 1), Qt.DisplayRole)
              for r in range(model.rowCount())]
    assert "" not in values


# --- wildcard columns (design 3.2.4's AllAttributes) -----------------------------------
#
# The model used to snapshot `query.select` in __init__. A wildcard expands during
# EVALUATION, so the query said two fields while the table had three columns, and every
# column-kind lookup ran off the end of the list — `IndexError` the moment the Registry
# panel opened anything. The engine tests all passed: the mismatch only exists in the
# model, which is exactly where nothing was looking.


class WildcardDump(Dump):
    def attribute(self, registry_type, entry_id, name):
        if name == "localization_key" and entry_id == "mod:named":
            return "fluid.mod.named"
        return super().attribute(registry_type, entry_id, name)

    def attributes_for(self, registry_type):
        return ["localization", "localization_key"]


@pytest.fixture
def wildcard_model(tags):
    from packsmith.core.query.ast import AllAttributes
    query = Query(scope=Registry(REG), select=[Id, AllAttributes], order_by=[Id])
    return RegistryTableModel(query, WildcardDump(), tags)


def test_every_column_can_be_asked_what_kind_it_is(wildcard_model):
    """The crash, as a test. Nothing exotic — just asking each column the question the
    view asks while building itself."""
    for col in range(wildcard_model.columnCount()):
        wildcard_model.is_tag_column(col)
        wildcard_model.column_tag_name(col)
        wildcard_model.tag_type_for_column(col)


def test_the_fields_line_up_with_the_columns(wildcard_model):
    assert wildcard_model.columnCount() == 3
    assert len(wildcard_model._select) == wildcard_model.columnCount()


def test_the_columns_follow_a_dump_that_gains_an_attribute(tags):
    """`reevaluate()` after adopting a new dump changes the column count. A field list
    captured at construction would not have moved with it — the same desync, arriving
    later and looking like a different bug."""
    from packsmith.core.query.ast import AllAttributes

    class Growing(WildcardDump):
        names = ["localization"]

        def attributes_for(self, registry_type):
            return list(self.names)

    dump = Growing()
    query = Query(scope=Registry(REG), select=[Id, AllAttributes], order_by=[Id])
    model = RegistryTableModel(query, dump, tags)
    assert model.columnCount() == 2

    dump.names = ["localization", "localization_key"]
    model.reevaluate()

    assert model.columnCount() == 3
    assert len(model._select) == 3
