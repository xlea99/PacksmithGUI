"""Type-aware sorting in the registry table.

This lived in a `QSortFilterProxyModel` and had **no tests**. It moved into the model for
speed — measured on a real pack's 18,638 items, the proxy compared pairwise through Python
(`lessThan` 201,292 calls, `data()` 402,584) and took 2.2s per sort, where a key function
takes 3-8ms. The rules came along unchanged, and a sort that is subtly wrong does not
announce itself: it just quietly puts things in an order nobody asked for.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from packsmith.core.query.ast import Attribute, Id, Query, Registry, Tag
from packsmith.gui.table.registry_table_model import RegistryTableModel

REG = "minecraft:item"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class Dump:
    def __init__(self, entries, names=None):
        self.registry = {REG: {"values": list(entries)}}
        self._names = names or {}

    def attribute(self, registry_type, entry_id, name):
        return self._names.get(entry_id) if name == "localization" else None


def build(tags, entries, select, names=None):
    return RegistryTableModel(Query(scope=Registry(REG), select=select),
                              Dump(entries, names), tags)


def column(model, col):
    return [model.data(model.index(r, col), Qt.DisplayRole)
            for r in range(model.rowCount())]


def test_enum_sorts_by_declaration_order_not_alphabetically(tags):
    """`tier` means early < mid < late < end. Alphabetical mangles that into
    early/end/late/mid, which is worse than useless — it looks sorted."""
    order = ["early", "mid", "late", "end"]
    tags.define(REG, "tier", "enum", enum_values=order)
    entries = ["mod:%d" % i for i in range(4)]
    for entry, value in zip(entries, ["end", "early", "late", "mid"]):
        tags.assign(REG, entry, "tier", value)

    model = build(tags, entries, [Id, Tag("tier")])
    model.sort(1, Qt.AscendingOrder)
    assert column(model, 1) == order


def test_numbers_sort_numerically_not_as_text(tags):
    """The classic: "10" before "9" reads as a broken table."""
    tags.define(REG, "weight", "number")
    entries = ["mod:%d" % i for i in range(3)]
    for entry, value in zip(entries, [9, 10, 2]):
        tags.assign(REG, entry, "weight", value)

    model = build(tags, entries, [Id, Tag("weight")])
    model.sort(1, Qt.AscendingOrder)
    assert [float(v) for v in column(model, 1)] == [2, 9, 10]


def test_bools_sort_false_first(tags):
    """"Not yet decided" first is the useful direction for a gap-finding table."""
    tags.define(REG, "remove", "bool", default=False)
    tags.assign(REG, "mod:a", "remove", True)
    tags.assign(REG, "mod:b", "remove", False)

    model = build(tags, ["mod:a", "mod:b"], [Id, Tag("remove")])
    model.sort(1, Qt.AscendingOrder)
    assert column(model, 1) == ["false", "true"]


def test_blank_cells_sink_in_both_directions(tags):
    """Folded into the sort key, `reverse=True` floats the blanks to the top — and "the
    empty rows are wherever the arrow points" is not a sort anybody wants."""
    tags.define(REG, "note", "string")
    tags.assign(REG, "mod:a", "note", "alpha")
    tags.assign(REG, "mod:c", "note", "zeta")
    entries = ["mod:a", "mod:b", "mod:c"]

    model = build(tags, entries, [Id, Tag("note")])
    model.sort(1, Qt.AscendingOrder)
    assert column(model, 1) == ["alpha", "zeta", ""]

    model.sort(1, Qt.DescendingOrder)
    assert column(model, 1) == ["zeta", "alpha", ""]


def test_an_unlocalized_row_sorts_by_the_id_it_displays(tags):
    """§3.1 renders the raw id when there is no display name. Sorting the underlying None
    instead would drop every unlocalized entry to the bottom of a column where they are
    visibly showing an id."""
    model = build(tags, ["mod:zzz", "mod:aaa"], [Id, Attribute("localization")],
                  names={"mod:zzz": "Aardvark"})
    model.sort(1, Qt.AscendingOrder)
    assert column(model, 1) == ["Aardvark", "mod:aaa"]


def test_sorting_is_case_insensitive(tags):
    tags.define(REG, "note", "string")
    for entry, value in (("mod:a", "banana"), ("mod:b", "Apple"), ("mod:c", "cherry")):
        tags.assign(REG, entry, "note", value)

    model = build(tags, ["mod:a", "mod:b", "mod:c"], [Id, Tag("note")])
    model.sort(1, Qt.AscendingOrder)
    assert column(model, 1) == ["Apple", "banana", "cherry"]


def test_an_enum_value_outside_its_definition_does_not_crash_the_sort(tags):
    """An orphaned assignment — the enum value was removed from the definition but the
    cell still holds it. It sorts after the known values rather than taking the table
    down mid-sort."""
    tags.define(REG, "tier", "enum", enum_values=["early", "late"])
    tags.assign(REG, "mod:a", "tier", "late")
    tags.assign(REG, "mod:b", "tier", "early")
    tags._db.execute(
        "UPDATE tag_assignments SET value = 'gone' WHERE entry_id = 'mod:a'")

    model = build(tags, ["mod:a", "mod:b"], [Id, Tag("tier")])
    model.sort(1, Qt.AscendingOrder)          # must not raise
    assert column(model, 1)[0] == "early"


def test_sorting_reorders_rows_rather_than_values(tags):
    """A row is a unit: sorting one column must carry its whole row along, not shuffle a
    column independently of the ids beside it."""
    tags.define(REG, "note", "string")
    tags.assign(REG, "mod:a", "note", "zeta")
    tags.assign(REG, "mod:b", "note", "alpha")

    model = build(tags, ["mod:a", "mod:b"], [Id, Tag("note")])
    model.sort(1, Qt.AscendingOrder)
    assert list(zip(column(model, 0), column(model, 1))) == [
        ("mod:b", "alpha"), ("mod:a", "zeta")]
