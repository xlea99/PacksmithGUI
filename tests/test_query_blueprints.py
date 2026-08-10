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
    And, Attribute, Blueprint, Cmp, Has, Id, Mod, Not, Query, Registry, Slot, Tag,
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
