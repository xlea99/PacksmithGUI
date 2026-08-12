"""The NBT viewer tab — design 6.4, read-only.

Two things drive the whole widget, both measured against a real world save:

- A played `level.dat` is **337,695 nodes**. Building that eagerly costs seconds and
  hundreds of megabytes to show a root with two keys in it, so children are built on
  expand.
- `Count: 64` and `Count: 64b` are different files, so the type is on every row. A viewer
  that renders both as "64" hides the one thing about NBT that bites.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core import nbt
from packsmith.core.nbt import (
    Byte, ByteArray, Compound, Double, Float, Int, IntArray, List, Long, String)
from packsmith.gui.nbt_viewer import NbtViewerTab, describe_value, type_name


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


SAMPLE = Compound({
    "Data": Compound({
        "Difficulty": Byte(2),
        "LevelName": String("New World"),
        "BorderSize": Double(59999968.0),
        "Player": Compound({
            "Health": Float(20.0),
            "Inventory": List([Compound({"id": String("minecraft:stone"),
                                         "Count": Byte(64)})], element_id=nbt.COMPOUND),
        }),
    }),
    "fml": Compound({"loadingModList": List([], element_id=nbt.COMPOUND)}),
})


# Tabs are held for the module's lifetime on purpose: a QTreeWidgetItem belongs to its
# tree, so letting the tab be collected leaves every item this returns dangling — which
# surfaces as "Internal C++ object already deleted" rather than as a missing reference.
_ALIVE = []


def tab_for(tree=SAMPLE, name="", compression="none"):
    tab = NbtViewerTab(nbt.dumps(tree, name, compression), "level.dat")
    _ALIVE.append(tab)
    return tab


def top_rows(tab):
    return [tab._tree.topLevelItem(i) for i in range(tab._tree.topLevelItemCount())]


def row_named(parent, name):
    return next((parent.child(i) for i in range(parent.childCount())
                 if parent.child(i).text(0) == name), None)


# --- it opens ------------------------------------------------------------------------------

def test_the_root_children_are_the_top_rows(qapp):
    assert sorted(r.text(0) for r in top_rows(tab_for())) == ["Data", "fml"]


def test_a_compound_reports_its_size_rather_than_its_contents(qapp):
    data = next(r for r in top_rows(tab_for()) if r.text(0) == "Data")
    assert data.text(1) == "Compound"
    assert data.text(2) == "4 entries"


def test_the_type_is_shown_on_every_row(qapp):
    """The point of the Type column: 64 and 64b look identical without it."""
    tab = tab_for()
    data = next(r for r in top_rows(tab) if r.text(0) == "Data")
    data.setExpanded(True)
    assert row_named(data, "Difficulty").text(1) == "Byte"
    assert row_named(data, "BorderSize").text(1) == "Double"
    assert row_named(data, "LevelName").text(1) == "String"


# --- laziness, which is not optional here ---------------------------------------------------

def test_children_are_not_built_until_the_row_is_expanded(qapp):
    """337,695 nodes in a real level.dat. Eager construction is seconds and hundreds of
    megabytes to show two rows."""
    tab = tab_for()
    data = next(r for r in top_rows(tab) if r.text(0) == "Data")
    assert data.childCount() == 1, "should hold only the placeholder"
    assert data.child(0).text(0) == "…"

    data.setExpanded(True)
    assert data.childCount() == 4
    assert row_named(data, "Player") is not None


def test_expanding_twice_does_not_duplicate_children(qapp):
    tab = tab_for()
    data = next(r for r in top_rows(tab) if r.text(0) == "Data")
    data.setExpanded(True)
    tab._on_expanded(data)              # as a second expand event would
    assert data.childCount() == 4


def test_a_leaf_has_no_expander(qapp):
    tab = tab_for()
    data = next(r for r in top_rows(tab) if r.text(0) == "Data")
    data.setExpanded(True)
    assert row_named(data, "Difficulty").childCount() == 0


def test_list_children_are_labelled_by_index(qapp):
    """They have no names — index is genuinely how they are addressed, and §6.4 already
    flags that `MyList[3]` is a fragile handle."""
    tab = tab_for()
    data = next(r for r in top_rows(tab) if r.text(0) == "Data")
    data.setExpanded(True)
    player = row_named(data, "Player")
    player.setExpanded(True)
    inventory = row_named(player, "Inventory")
    inventory.setExpanded(True)
    assert inventory.child(0).text(0) == "[0]"


# --- how values read --------------------------------------------------------------------------

@pytest.mark.parametrize("tag, expected", [
    (Byte(2), "2"),
    (String("New World"), '"New World"'),
    (Compound({"a": Byte(1)}), "1 entry"),
    (Compound(), "0 entries"),
    (List([Byte(1), Byte(2)], element_id=nbt.BYTE), "2 × Byte"),
    (List([], element_id=nbt.COMPOUND), "empty (Compound)"),
    (IntArray([1, 2, 3]), "[1, 2, 3]"),
    (ByteArray([]), "[]"),
])
def test_values_read_as_themselves(tag, expected):
    assert describe_value(tag) == expected


def test_a_long_array_is_summarised_not_dumped():
    """Chunk data runs to thousands of entries; a tree row is not the place for them."""
    text = describe_value(IntArray(range(5000)))
    assert "5,000 total" in text and len(text) < 80


def test_an_empty_lists_element_type_is_visible():
    """It is part of the data, and the viewer is the only place you can see it."""
    assert "Compound" in describe_value(List([], element_id=nbt.COMPOUND))


def test_type_names_drop_the_prefix():
    assert type_name(Byte(1)) == "Byte" and type_name(Compound()) == "Compound"


# --- filtering ----------------------------------------------------------------------------------

def test_filtering_shows_full_paths_flat(qapp):
    """A filtered TREE would have to materialise every ancestor of every hit — on a
    337,000-node file that is most of the tree, which is what laziness exists to avoid."""
    tab = tab_for()
    tab._filter.setText("health")
    rows = top_rows(tab)
    assert [r.text(0) for r in rows] == ["Data.Player.Health"]
    assert rows[0].text(1) == "Float"


def test_filtering_matches_values_as_well_as_names(qapp):
    tab = tab_for()
    tab._filter.setText("minecraft:stone")
    assert any("Inventory[0].id" in r.text(0) for r in top_rows(tab))


def test_clearing_the_filter_restores_the_tree(qapp):
    tab = tab_for()
    tab._filter.setText("health")
    assert len(top_rows(tab)) == 1
    tab._filter.setText("")
    assert sorted(r.text(0) for r in top_rows(tab)) == ["Data", "fml"]


# --- files that aren't NBT -------------------------------------------------------------------

def test_a_dat_that_is_not_nbt_explains_itself(qapp):
    """Real case from the user's instances: `enigmatic_persistence.dat` is a mod's own
    binary format. An empty tree would read as a broken viewer."""
    tab = NbtViewerTab(b"Qewnfles\x00\x01\x02", "enigmatic_persistence.dat")
    assert not tab.ok
    assert not tab._banner.isHidden()
    assert "isn't readable as NBT" in tab._banner.text()
    assert "mods use the .dat extension" in tab._banner.text()


def test_an_empty_file_does_not_raise(qapp):
    assert not NbtViewerTab(b"", "overworld.dat").ok


def test_compression_is_reported(qapp):
    assert "gzip" in tab_for(compression="gzip")._summary.text()
    assert "uncompressed" in tab_for(compression="none")._summary.text()
