"""Blueprint-scoped queries (design 3.2.4).

§3.2.4's AST has said this since it was written: *"Rows are the instances of a blueprint
(fields become slots)."* That sentence describes the instances-as-rows, slots-as-columns
grid exactly — which means the blueprint grid was always meant to be **a query, rendered**,
i.e. a View, and this is the evaluator half catching up.

The payoff worth watching for below: "which stone types are missing a polished wall" needs
no new predicate. It's ``Not(Has(Slot(...)))`` — gap-finding falls out of the null semantics
the engine already had.
"""
import pytest

from packsmith.core.blueprints import BlueprintStore
from packsmith.core.query.ast import (
    AllSlots, And, Attribute, Blueprint, Cmp, Has, Id, Mod, Not, Query, Registry, Slot,
    Tag,
    QueryError,
)
from packsmith.core.query.evaluator import evaluate
from tests.test_blueprints import FakeDump


@pytest.fixture
def store(user_db):
    bp = BlueprintStore(user_db, packdump=FakeDump())
    bp.define("StoneType")
    bp.add_slot("StoneType", "base_block", "registry", registry_type="minecraft:block")
    bp.add_slot("StoneType", "hardness", "number")
    bp.add_group("StoneType", "polished")
    for form in ("base", "wall"):
        bp.add_slot("StoneType", form, "registry", parent="polished",
                    registry_type="minecraft:block")

    bp.create_instance("StoneType", "granite")
    bp.bind("StoneType", "granite", "base_block", "minecraft:granite")
    bp.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite")
    bp.bind("StoneType", "granite", "hardness", 3)

    bp.create_instance("StoneType", "andesite")
    bp.bind("StoneType", "andesite", "base_block", "minecraft:andesite")
    bp.bind("StoneType", "andesite", "polished.base", "minecraft:polished_andesite")
    bp.bind("StoneType", "andesite", "polished.wall", "minecraft:andesite")
    bp.bind("StoneType", "andesite", "hardness", 6)

    bp.create_instance("StoneType", "tuff")          # nothing bound at all
    return bp


def run(query, store):
    return evaluate(query, blueprint_store=store)


def rows(result):
    return [r.values for r in result.rows]


def test_rows_are_instances_and_fields_are_slots(store):
    result = run(Query(scope=Blueprint("StoneType"),
                       select=[Id, Slot("base_block")], order_by=[Id]), store)
    assert [c.name for c in result.columns] == ["id", "base_block"]
    assert rows(result) == [
        {"id": "andesite", "base_block": "minecraft:andesite"},
        {"id": "granite", "base_block": "minecraft:granite"},
        {"id": "tuff", "base_block": None},
    ]


def test_gap_finding_needs_no_new_predicate(store):
    """"Which stone types are missing a polished wall" — the question the whole primitive
    exists to answer — is Not(Has(...)) over the null semantics already in the engine."""
    result = run(Query(scope=Blueprint("StoneType"), select=[Id],
                       filter=Not(Has(Slot("polished.wall"))), order_by=[Id]), store)
    assert [r.values["id"] for r in result.rows] == ["granite", "tuff"]


def test_filtering_on_a_bound_value(store):
    result = run(Query(scope=Blueprint("StoneType"), select=[Id],
                       filter=Cmp(Slot("base_block"), "contains", "granite")), store)
    assert [r.values["id"] for r in result.rows] == ["granite"]


def test_a_slots_type_reaches_the_column(store):
    """Renderers pick their cell editor from the column type, so a number slot has to
    arrive as a number rather than as text."""
    result = run(Query(scope=Blueprint("StoneType"),
                       select=[Id, Slot("hardness")], order_by=[Id]), store)
    assert {c.name: c.type for c in result.columns} == {"id": "id", "hardness": "number"}
    assert rows(result)[1]["hardness"] == 3


def test_ordering_and_comparison_use_the_real_type(store):
    result = run(Query(scope=Blueprint("StoneType"), select=[Id],
                       filter=Cmp(Slot("hardness"), "gte", 5)), store)
    assert [r.values["id"] for r in result.rows] == ["andesite"]


def test_combining_conditions(store):
    result = run(Query(scope=Blueprint("StoneType"), select=[Id],
                       filter=And([Has(Slot("polished.base")),
                                   Not(Has(Slot("polished.wall")))])), store)
    assert [r.values["id"] for r in result.rows] == ["granite"]


def test_an_unknown_slot_is_a_clear_error(store):
    with pytest.raises(QueryError, match="no slot 'nope'"):
        run(Query(scope=Blueprint("StoneType"), select=[Slot("nope")]), store)


def test_registry_only_fields_say_why_they_do_not_apply(store):
    """Tag and Attribute are facts about a registry ENTRY. An instance isn't one — getting
    there means following a slot binding, which is Deref and genuinely later."""
    for field, expected in ((Tag("remove"), "Deref"), (Attribute("localization"), "Deref"),
                            (Mod, "not a blueprint instance")):
        with pytest.raises(QueryError, match=expected):
            run(Query(scope=Blueprint("StoneType"), select=[field]), store)


def test_a_blueprint_query_without_a_store_says_so(store):
    with pytest.raises(QueryError, match="needs a blueprint store"):
        evaluate(Query(scope=Blueprint("StoneType"), select=[Id]), packdump=FakeDump())


def test_a_registry_query_still_needs_its_packdump():
    with pytest.raises(QueryError, match="needs a packdump"):
        evaluate(Query(scope=Registry("minecraft:item"), select=[Id]))


def test_limit_and_distinct_work_the_same_here(store):
    result = run(Query(scope=Blueprint("StoneType"), select=[Id], order_by=[Id],
                       limit=2), store)
    assert [r.values["id"] for r in result.rows] == ["andesite", "granite"]

    result = run(Query(scope=Blueprint("StoneType"), select=[Slot("polished.wall")],
                       distinct=True), store)
    assert len(result.rows) == 2          # one bound value + one None, deduped


# --- AllSlots: columns follow the schema -------------------------------------
#
# A blueprint's slots are a fact ABOUT the blueprint, not a preference of the view. A saved
# view that froze its column list would begin hiding *gaps* the moment the schema grew —
# and gaps are the primitive's entire output (§3.2.2, "empty slots = missing content").

def test_all_slots_selects_every_slot_in_schema_order(store):
    from packsmith.core.query.ast import AllSlots
    result = run(Query(scope=Blueprint("StoneType"), select=[Id, AllSlots],
                       order_by=[Id]), store)
    assert [c.name for c in result.columns] == [
        "id", "base_block", "hardness", "polished.base", "polished.wall"]


def test_a_growing_schema_reaches_an_already_saved_query(store):
    """The same query object, evaluated before and after — this is why expansion happens
    at evaluation rather than when the query is written."""
    from packsmith.core.query.ast import AllSlots
    query = Query(scope=Blueprint("StoneType"), select=[Id, AllSlots], order_by=[Id])
    before = [c.name for c in run(query, store).columns]
    assert "polished.stairs" not in before

    store.add_slot("StoneType", "stairs", "registry", parent="polished",
                   registry_type="minecraft:block")
    after = [c.name for c in run(query, store).columns]

    # The new slot shows up, and every gap in it is now visible — which is the whole
    # argument for following rather than freezing.
    assert "polished.stairs" in after
    assert set(before) <= set(after)


def test_a_curated_view_still_freezes_its_columns(store):
    """Naming slots explicitly still means what it says. The difference between
    "everything" and "these three" is visible in the query."""
    query = Query(scope=Blueprint("StoneType"), select=[Id, Slot("polished.base")],
                  order_by=[Id])
    store.add_slot("StoneType", "chiseled", "registry", registry_type="minecraft:block")
    assert [c.name for c in run(query, store).columns] == ["id", "polished.base"]


def test_all_slots_needs_a_blueprint_scope(store):
    from packsmith.core.query.ast import AllSlots
    from packsmith.core.query.evaluator import evaluate as ev
    with pytest.raises(QueryError, match="needs a Blueprint scope"):
        ev(Query(scope=Registry("minecraft:item"), select=[AllSlots]),
           packdump=FakeDump(), tag_store=None)


def test_all_slots_survives_serialization(store):
    """A View stores its query as JSON, so "follow the schema" has to be expressible as
    data, not as a flag on the widget."""
    from packsmith.core.query import from_dict, to_dict
    from packsmith.core.query.ast import AllSlots
    query = Query(scope=Blueprint("StoneType"), select=[Id, AllSlots], order_by=[Id])
    assert from_dict(to_dict(query)) == query


# --- reading a whole grid, not a cell at a time ---------------------------------------
#
# Reported from real use: filling out a StoneType schema made every cell commit hang. The
# grid redraws after each edit, and each redraw asked for one binding at a time — and
# answering ONE binding rebuilds the blueprint's whole slot tree. 47 instances x 38 slots
# meant 1,786 tree rebuilds per redraw, half a million recursive calls, 560 ms. The shape
# was wrong, not the code: a grid wants the rectangle, so it asks for the rectangle.

class _CountingStore:
    """Wraps a store and counts the calls that scale with the number of CELLS."""

    def __init__(self, store):
        self._store = store
        self.slot_tree_reads = 0
        self.per_instance_reads = 0

    def __getattr__(self, name):
        return getattr(self._store, name)

    def slots(self, blueprint):
        self.slot_tree_reads += 1
        return self._store.slots(blueprint)

    def bindings(self, blueprint, instance):
        self.per_instance_reads += 1
        return self._store.bindings(blueprint, instance)

    def value_of(self, blueprint, instance, path):
        self.per_instance_reads += 1
        return self._store.value_of(blueprint, instance, path)


def test_evaluating_a_blueprint_query_reads_the_schema_a_bounded_number_of_times(
        store):
    """Counted, not timed: the defect is that the work grew with the CELL count, and a
    stopwatch would only notice once a pack got big enough to hurt."""
    store.define("Grid")
    for slot in ("a", "b", "c", "d"):
        store.add_slot("Grid", slot, "registry", registry_type="minecraft:block")
    for name in ("one", "two", "three", "four", "five"):
        store.create_instance("Grid", name)
        for slot in ("a", "b", "c", "d"):
            store.bind("Grid", name, slot, "minecraft:granite", owner="user")

    counting = _CountingStore(store)
    result = evaluate(Query(scope=Blueprint("Grid"), select=[Id] + [Slot(x) for x in ("a", "b", "c", "d")]),
                      blueprint_store=counting)

    assert len(result.rows) == 5
    assert counting.per_instance_reads == 0, (
        "the grid was read one cell (or one instance) at a time — "
        f"{counting.per_instance_reads} lookups for 20 cells")
    assert counting.slot_tree_reads <= 4, (
        f"the slot tree was rebuilt {counting.slot_tree_reads} times; it should be read "
        f"a handful of times per query, never per cell")


def test_the_rectangle_reports_the_same_values_as_cell_by_cell(store):
    """The guard on the optimisation: faster must not mean different. Owner and action_ref
    travel with it too, because the grid paints ownership from them."""
    store.define("Grid")
    store.add_slot("Grid", "a", "registry", registry_type="minecraft:block")
    store.add_slot("Grid", "b", "registry", registry_type="minecraft:block")
    store.create_instance("Grid", "one")
    store.bind("Grid", "one", "a", "minecraft:granite", owner="user")
    store.bind("Grid", "one", "b", "minecraft:andesite",
                    owner="action", action_ref="p:a")

    every = store.all_bindings("Grid")

    assert every["one"]["a"].value == store.bindings("Grid", "one")["a"].value
    assert every["one"]["b"].owner == "action"
    assert every["one"]["b"].action_ref == "p:a"
    assert "two" not in every, "an instance with no bindings simply is not in it"


# --- what the view remembers about how it looks (design 5.3 / 3.2.3) ------------------

class _EditorDump(FakeDump):
    """FakeDump is bind-time validation only; the editor also *evaluates* candidate
    templates, which reaches for attributes."""
    def attribute(self, *_args, **_kwargs):
        return None


def _tab(store, view=None, saved=None):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from packsmith.gui.blueprint_editor import BlueprintEditorTab
    QApplication.instance() or QApplication([])

    class View:
        renderer_config = saved or {}
    written = []
    tab = BlueprintEditorTab(Query(scope=Blueprint("StoneType"), select=[Id, AllSlots]),
                             store, packdump=_EditorDump(),
                             view=View() if saved is not None else None,
                             on_config_changed=written.append)
    return tab, written


def test_hide_collapsed_groups_survives_reopening(store):
    """A checkbox is a decision about how you want to work, and it was the one piece of the
    renderer config that never left the tab."""
    tab, written = _tab(store, saved={})
    tab._follow_tree.setChecked(True)

    assert written and written[-1]["follow_tree"] is True

    again, _ = _tab(store, saved=written[-1])
    assert again._follow_tree.isChecked() is True


def test_turning_it_off_survives_too(store):
    """`follow_tree` is written even when False. Everything else in the config is
    absent-means-nothing, so omitting a False would make "I turned this off" and "I have
    never touched this" the same stored state."""
    tab, written = _tab(store, saved={"follow_tree": True})
    assert tab._follow_tree.isChecked() is True

    tab._follow_tree.setChecked(False)
    assert written[-1]["follow_tree"] is False

    again, _ = _tab(store, saved=written[-1])
    assert again._follow_tree.isChecked() is False


def test_a_column_drag_is_remembered_without_an_edit(store):
    """Widths were only captured during a REBUILD, so dragging a column and then doing
    nothing else lost it — the view came back at its default width."""
    tab, written = _tab(store, saved={})
    path = tab._grid.horizontalHeaderItem(0).text()

    tab._grid.setColumnWidth(0, 321)
    tab._remember_widths()                 # what the debounce timer fires

    assert written[-1]["column_widths"][path] == 321
    again, _ = _tab(store, saved=written[-1])
    assert again._column_widths[path] == 321


def test_widths_are_keyed_by_slot_path_not_column_index(store):
    """So a width survives slots being added, removed or reordered."""
    tab, written = _tab(store, saved={})
    tab._grid.setColumnWidth(1, 250)
    tab._remember_widths()

    stored = written[-1]["column_widths"]
    assert all(not key.isdigit() for key in stored)
    assert tab._slots[1].path in stored


# --- the escape hatch (design 5.3: the tool finds, the human decides) -----------------

def test_unchecking_suggest_offers_the_whole_registry(store):
    """A template that narrows to nothing — or to the wrong thing — used to make a cell
    unfillable. Some real answers no template can reach: `alexc_galena_cut_brick` is
    galena's cut-brick and nothing in the id says so."""
    tab, _ = _tab(store, saved={"suggest": "nothing_matches_this_token"})
    slot = tab._slots[0]

    narrowed, _note = tab.candidates_for(0, slot)
    assert narrowed == [], "the template has to actually be filtering, or this proves nothing"

    tab._template_bar.enabled.setChecked(False)
    everything, note = tab.candidates_for(0, slot)
    assert everything == tab.registry_entries(slot.registry_type)
    assert everything and "off" in note


def test_the_toggle_is_not_saved_with_the_view(store):
    """It is an escape hatch for one cell, not a setting. Persisting it would mean a view
    silently loses its suggestions forever after one use — and the template would still be
    sitting in the bar looking like it should be working."""
    tab, written = _tab(store, saved={"suggest": "@instance"})
    tab._template_bar.enabled.setChecked(False)

    assert "suggest" in written[-1], "the template itself still belongs to the view"
    assert tab.renderer_config().get("suggest") == "@instance"
    again, _ = _tab(store, saved=written[-1])
    assert again._template_bar.suggesting() is True


def test_the_status_says_which_thing_is_suggesting_everything(store):
    """A column that opted out reports "suggests everything (opted out)". If the checkbox
    were not read first, unchecking the box would show that message while the box above is
    what is really doing it."""
    tab, _ = _tab(store, saved={"suggest": "@instance"})
    tab._overrides = {tab._slots[0].path: ""}      # this column opted out
    tab._template_bar.enabled.setChecked(False)

    tab._on_cell_changed(0, 0)
    assert "off" in tab._template_bar._status.text()


# --- acting on an instance from its own name -----------------------------------------
#
# The instance names are the grid's VERTICAL HEADER, which is a separate widget from the
# viewport. `setContextMenuPolicy` on the table does not reach it, so right-clicking the
# name — the one place you would obviously click to act on an instance — hit nothing, while
# right-clicking any cell in the same row worked. The bug is entirely in which widget is
# asked, so that is what these check.

def _menu_items(menu):
    return [a.text() for a in menu.actions() if a.text()]


def test_the_name_column_asks_for_a_menu_at_all(store):
    from PySide6.QtCore import QPoint, Qt
    tab, _ = _tab(store, saved={})
    header = tab._grid.verticalHeader()
    asked = []
    tab._popup = lambda menu, _where: asked.append(menu)

    assert header.contextMenuPolicy() == Qt.CustomContextMenu, \
        "the table's own policy stops at the viewport"
    header.customContextMenuRequested.emit(
        QPoint(5, header.sectionViewportPosition(0) + 2))
    assert asked, "the header asked nobody for a menu"


def test_right_clicking_a_name_offers_delete(store):
    from PySide6.QtCore import QPoint
    tab, _ = _tab(store, saved={})
    header = tab._grid.verticalHeader()
    row = 1
    pos = QPoint(5, header.sectionViewportPosition(row) + header.sectionSize(row) // 2)

    assert header.logicalIndexAt(pos) == row
    assert "Delete instance…" in _menu_items(tab._instance_menu(row))


def test_the_menu_names_the_row_you_clicked(store, monkeypatch):
    """Off-by-one here deletes someone else's work. The vertical header numbers sections in
    its own coordinate space, so this is not the same lookup the viewport does."""
    tab, _ = _tab(store, saved={})
    header = tab._grid.verticalHeader()
    deleted = []
    monkeypatch.setattr(tab, "_delete_instance", deleted.append)

    for row, instance in enumerate(tab._instances):
        tab._instance_menu(row).actions()[-1].trigger()
        assert deleted[-1] == instance.name
        from PySide6.QtCore import Qt
        assert header.model().headerData(row, Qt.Vertical) == instance.name


def test_the_row_is_selected_before_the_menu_opens(store, monkeypatch):
    """You should be able to see which instance you are about to delete. A header click
    alone does not move the grid's selection."""
    from PySide6.QtCore import QPoint
    tab, _ = _tab(store, saved={})
    tab._popup = lambda *_a: None                 # or exec blocks until someone clicks
    header = tab._grid.verticalHeader()
    tab._grid.selectRow(0)
    pos = QPoint(5, header.sectionViewportPosition(1) + header.sectionSize(1) // 2)

    tab._on_row_menu(pos)

    assert tab._grid.currentRow() == 1


def test_the_name_column_does_not_offer_cell_only_actions(store):
    """Suggestions are for a slot. The name column is not one, and an entry that quietly
    acted on whichever column happened to be first would be worse than its absence."""
    tab, _ = _tab(store, saved={})
    assert "Suggestions…" not in _menu_items(tab._instance_menu(0))
    assert "Suggestions…" in _menu_items(tab._instance_menu(0, cell=(0, 0)))
