"""The View dialog's columns: which are offered, which survive, and editing them later.

Everything here is a failure that **hides**:

* The column checkboxes were built once, for `minecraft:item`, and *relabelled* on a
  registry switch with `zip()` — so a registry with more fields than item silently lost
  the extras (`superbiome` on `minecraft:worldgen/biome`), and OK then threw a KeyError
  inside a Qt slot, which reads as a button that does nothing.
* Pressing OK on an existing View must not cost it a column the dialog does not
  understand — `AllAttributes`, a tag undefined since, a node from a newer build.
* A tag created from the column picker must land on the View's registry; one defined
  anywhere else would be a column that is empty forever.
* A column edit on an open View has to reach the open tab, or the table keeps showing
  columns the View no longer has, and nothing says so.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.query import AllAttributes, Attribute, Id, Query, Registry, Tag
from packsmith.core.tags import TagStore

ITEM = "minecraft:item"
BIOME = "minecraft:worldgen/biome"


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class FakeDump:
    """Just the two things the dialog asks of a packdump."""

    registry = {ITEM: {"values": ["minecraft:stone"]},
                BIOME: {"values": ["minecraft:plains", "minecraft:desert"]}}

    @staticmethod
    def attributes_for(registry_type):
        return ["localization", "texture"] if registry_type == ITEM else ["localization"]


@pytest.fixture
def tags(user_db):
    store = TagStore(user_db)
    for name in ("hide_only", "remove"):
        store.define(ITEM, name, "bool")
    for name in ("cave_biome", "dimension", "notes", "ocean_biome", "removed", "superbiome"):
        store.define(BIOME, name, "bool")
    return store


def new_dialog(tags, **kw):
    from packsmith.gui.query_constructor import QueryConstructorDialog
    return QueryConstructorDialog(tags, ITEM, new_view=True, registries=[ITEM, BIOME],
                                  packdump=FakeDump(), **kw)


def offered(dlg) -> set:
    picker = dlg._open_column_picker()
    values = set(picker._choices[i].returns for i in range(len(picker._choices)))
    picker.close()
    return values


# --- the reported bug --------------------------------------------------------------

def test_switching_to_a_bigger_registry_offers_every_one_of_its_tags(tags, qapp):
    dlg = new_dialog(tags)
    dlg._registry_combo.set_current(BIOME)
    dlg._on_registry_changed(BIOME)

    assert {"t:removed", "t:superbiome"} <= offered(dlg)
    assert dlg.add_column("t:superbiome")
    dlg._name_edit.setText("Biomes")
    dlg.accept()

    assert dlg.result_query.scope == Registry(BIOME)
    assert Tag("superbiome") in dlg.result_query.select


def test_only_this_registrys_tags_and_attributes_are_offered(tags, qapp):
    dlg = new_dialog(tags)
    dlg._on_registry_changed(BIOME)
    choices = offered(dlg)

    assert "t:remove" not in choices, "an item tag on a biome view"
    assert "a:texture" not in choices, "an attribute the biome dump does not carry"


# --- editing an existing View --------------------------------------------------------

def edit_dialog(tags, query, **kw):
    from packsmith.gui.query_constructor import QueryConstructorDialog
    return QueryConstructorDialog(tags, query=query, packdump=FakeDump(), **kw)


def test_ok_on_an_existing_view_changes_nothing(tags, qapp):
    """Including what the dialog cannot describe: those are kept, not dropped."""
    query = Query(scope=Registry(ITEM),
                  select=[Attribute("localization"), Id, AllAttributes,
                          Tag("since_undefined"), Attribute("form"), Tag("remove")],
                  order_by=[Id])
    dlg = edit_dialog(tags, query)
    dlg.accept()

    assert dlg.result_query == query


def test_columns_can_be_added_removed_and_reordered_but_id_stays(tags, qapp):
    query = Query(scope=Registry(ITEM), select=[Id, Attribute("localization")],
                  order_by=[Id])
    dlg = edit_dialog(tags, query)

    assert not dlg._columns.remove("id"), "id is the row's identity"
    dlg._columns.remove("a:localization")
    dlg.add_column("t:remove")
    dlg._columns.move("t:remove", 0)
    dlg.accept()

    assert dlg.result_query.select == [Tag("remove"), Id]
    assert dlg.result_query.order_by == [Tag("remove")]


def test_a_blueprint_view_edits_its_filter_and_keeps_its_slots(tags, qapp):
    """"Edit view…" on a blueprint grid used to crash reading `.type` off its scope."""
    from packsmith.core.query import AllSlots, Blueprint
    query = Query(scope=Blueprint("StoneType"), select=[Id, AllSlots], order_by=[Id])
    dlg = edit_dialog(tags, query)
    dlg.accept()

    assert dlg.result_query == query


# --- a tag born in the picker --------------------------------------------------------

def test_a_new_tag_from_the_picker_lands_on_the_views_registry(tags, qapp):
    asked = []

    def create(registry, suggested):
        asked.append((registry, suggested))
        tags.define(registry, suggested, "bool")
        return suggested

    dlg = new_dialog(tags, create_tag=create)
    dlg._on_registry_changed(BIOME)
    row = dlg._add_row()
    dlg._create_tag_column("temperate")

    assert asked == [(BIOME, "temperate")]
    assert dlg._columns.keys()[-1] == "t:temperate"
    assert "t:temperate" in [c.returns for c in row._field_combo.choices()], \
        "the conditions should be able to filter on it too"


def test_the_create_row_never_reaches_chosen(qapp):
    """A listener wired for values would otherwise get a name that is not one of them."""
    from packsmith.gui.shell.picker import PickerPopup
    picker = PickerPopup(["remove"], create="tag", searchable=True)
    chosen, created = [], []
    picker.chosen.connect(chosen.append)
    picker.create_requested.connect(created.append)

    picker._filter.setText("superbiome")
    picker._pick(picker._list.item(picker._list.count() - 1))

    assert (chosen, created) == ([], ["superbiome"])


# --- the open tab follows ------------------------------------------------------------

from tests.test_open_tabs import profile, window   # noqa: E402,F401
from tests.test_profile_lifecycle import instance, userdata   # noqa: E402,F401


def test_a_column_edit_rebuilds_the_open_tab_where_it_was(window, qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from packsmith.gui import main_window as mw

    view = window._views.create("Mine", Query(scope=Registry(ITEM), select=[Id],
                                              order_by=[Id]))
    window._open_browse(ITEM)
    old = window._open_view(view)
    window._open_encyclopedia()
    index = window._workspace.index_of(old)

    class Accepting:
        def __init__(self, *a, **kw):
            self.result_query = Query(scope=Registry(ITEM),
                                      select=[Id, Attribute("localization")], order_by=[Id])
            self.result_filter = None
            self.result_name = "Mine"

        def exec(self):
            return True

    monkeypatch.setattr(mw, "QueryConstructorDialog", Accepting)
    before = window._workspace.current_widget()
    window._edit_view(view)

    tab = window._open_tabs[("view", view.id)]
    assert tab is not old
    assert window._workspace.index_of(tab) == index
    assert window._workspace.current_widget() is before, "focus should not jump"
    model = window._tab_models[tab]
    assert [model.headerData(c, Qt.Horizontal) for c in range(model.columnCount())] == \
        ["id", "localization"]
    assert old not in window._tab_bars and old not in window._tab_base_queries
