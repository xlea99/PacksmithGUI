"""Grouping and aggregation — the Power Ladder's report tier (design 3.2.4, 5.3).

§5.3 Part A concluded *"a report is a query"* and listed what that needs: *"provenance /
distribution ('entries per mod per registry') — aggregation"* and *"cross-mod duplicates —
fuzzy grouping."* This is that.

The headline case is the one the reference pack makes vivid: 29 different blocks all
called "Cave Painting". Finding those is `GROUP BY localization HAVING count > 1` — which
is why grouping, not the condition joins the Power Ladder pointed at, turned out to be the
short road to the duplicate-name finder.
"""
import json
import pytest

from packsmith.core.query.serde import from_dict, to_dict
from packsmith.core.query.ast import (
    Attribute, Cmp, Collect, Count, CountDistinct, Id, Mod, Query, Registry, Tag,
    QueryError,
)
from packsmith.core.query.evaluator import evaluate


class Dump:
    """Ids plus display names, which is all a duplicate-name report needs."""

    def __init__(self, named):
        self._named = dict(named)
        self.registry = {"minecraft:block": {"values": list(named)}}

    def attribute(self, _registry, entry_id, name):
        return self._named.get(entry_id) if name == "localization" else None


PACK = Dump({
    "quark:granite_bricks": "Granite Bricks",
    "stoneworks:granite_bricks": "Granite Bricks",
    "caverns:granite_bricks": "Granite Bricks",
    "quark:polished_tuff": "Polished Tuff",
    "trials:polished_tuff": "Polished Tuff",
    "minecraft:stone": "Stone",
})


def run(query, dump=PACK):
    return evaluate(query, packdump=dump, tag_store=None)


def rows(result):
    return [r.values for r in result.rows]


# --- the case the design is written about -----------------------------------

def test_the_duplicate_display_name_report():
    """"Which names are claimed by more than one block?" — 5 lines, no joins."""
    result = run(Query(
        scope=Registry("minecraft:block"),
        select=[Attribute("localization"), Count, Collect(Id)],
        group_by=[Attribute("localization")],
        having=Cmp(Count, "gt", 1),
        order_by=[Attribute("localization")],
    ))
    assert rows(result) == [
        {"localization": "Granite Bricks", "count": 3,
         "id_values": ["caverns:granite_bricks", "quark:granite_bricks",
                       "stoneworks:granite_bricks"]},
        {"localization": "Polished Tuff", "count": 2,
         "id_values": ["quark:polished_tuff", "trials:polished_tuff"]},
    ]


def test_counting_distinct_mods_not_just_rows():
    """"3 mods claim this name" is a different fact from "3 rows", and the more useful
    one — one mod shipping three variants isn't a collision."""
    result = run(Query(
        scope=Registry("minecraft:block"),
        select=[Attribute("localization"), CountDistinct(Mod)],
        group_by=[Attribute("localization")],
        having=Cmp(CountDistinct(Mod), "gt", 1),
        order_by=[Attribute("localization")]))
    assert rows(result) == [
        {"localization": "Granite Bricks", "distinct_mod": 3},
        {"localization": "Polished Tuff", "distinct_mod": 2},
    ]


def test_entries_per_mod_the_distribution_report():
    """§5.3's other named case: "provenance / distribution (entries per mod per
    registry)"."""
    result = run(Query(scope=Registry("minecraft:block"), select=[Mod, Count],
                       group_by=[Mod], order_by=[Mod]))
    assert rows(result) == [
        {"mod": "caverns", "count": 1}, {"mod": "minecraft", "count": 1},
        {"mod": "quark", "count": 2}, {"mod": "stoneworks", "count": 1},
        {"mod": "trials", "count": 1},
    ]


# --- semantics --------------------------------------------------------------

def test_grouped_rows_are_computed_and_therefore_not_editable():
    """No single entry backs a group, so a View must render it read-only. That falls out
    of the query's shape rather than being a flag someone sets."""
    result = run(Query(scope=Registry("minecraft:block"), select=[Mod, Count],
                       group_by=[Mod]))
    assert all(r.entry_id is None and not r.editable for r in result.rows)


def test_filter_runs_before_grouping_and_having_runs_after():
    result = run(Query(
        scope=Registry("minecraft:block"),
        select=[Attribute("localization"), Count],
        filter=Cmp(Mod, "neq", "caverns"),          # drops one granite brick first
        group_by=[Attribute("localization")],
        having=Cmp(Count, "gt", 1),
        order_by=[Attribute("localization")]))
    assert rows(result) == [
        {"localization": "Granite Bricks", "count": 2},
        {"localization": "Polished Tuff", "count": 2},
    ]


def test_collect_can_be_capped():
    result = run(Query(scope=Registry("minecraft:block"),
                       select=[Attribute("localization"), Collect(Id, limit=2)],
                       group_by=[Attribute("localization")],
                       having=Cmp(Count, "gt", 2)))
    assert rows(result)[0]["id_values"] == ["caverns:granite_bricks",
                                            "quark:granite_bricks"]


def test_grouping_by_several_fields():
    result = run(Query(scope=Registry("minecraft:block"),
                       select=[Mod, Attribute("localization"), Count],
                       group_by=[Mod, Attribute("localization")],
                       order_by=[Mod]))
    assert len(result.rows) == 6          # nothing collides once mod is in the key


def test_column_types_reach_the_renderer():
    result = run(Query(scope=Registry("minecraft:block"),
                       select=[Mod, Count, CountDistinct(Id), Collect(Id)],
                       group_by=[Mod]))
    assert {c.name: c.type for c in result.columns} == {
        "mod": "string", "count": "number", "distinct_id": "number", "id_values": "list"}


def test_limit_applies_to_groups_not_rows():
    result = run(Query(scope=Registry("minecraft:block"), select=[Mod, Count],
                       group_by=[Mod], order_by=[Mod], limit=2))
    assert [r.values["mod"] for r in result.rows] == ["caverns", "minecraft"]


# --- guardrails -------------------------------------------------------------

def test_selecting_something_neither_grouped_nor_aggregated_is_refused():
    """A group has no single id, so there is no honest value to put in the cell."""
    with pytest.raises(QueryError, match="neither grouped nor aggregated"):
        run(Query(scope=Registry("minecraft:block"), select=[Id, Count], group_by=[Mod]))


def test_ordering_a_grouped_query_by_an_unselected_field_is_refused():
    with pytest.raises(QueryError, match="isn't selected"):
        run(Query(scope=Registry("minecraft:block"), select=[Mod, Count],
                  group_by=[Mod], order_by=[Attribute("localization")]))


def test_an_ungrouped_query_is_unaffected():
    result = run(Query(scope=Registry("minecraft:block"), select=[Id], order_by=[Id],
                       limit=2))
    assert [r.values["id"] for r in result.rows] == ["caverns:granite_bricks",
                                                     "minecraft:stone"]
    assert all(r.editable for r in result.rows)


# --- serde (design 3.2.4: "queries round-trip through JSON with zero code") -------------

def _round_trip(query):
    return from_dict(json.loads(json.dumps(to_dict(query))))


def test_an_aggregate_query_round_trips():
    """It encoded fine and died on the way back in — at load time, in a later session."""
    q = Query(scope=Registry("minecraft:item"),
              select=[Attribute("localization"), Count,
                      CountDistinct(Tag("mod")), Collect(Id, limit=5)],
              group_by=[Attribute("localization")],
              having=Cmp(Count, "gt", 1))
    assert _round_trip(q) == q


def test_count_decodes_to_the_singleton_not_a_copy():
    """The evaluator compares aggregates by identity in places; a fresh _Count() would
    still be equal, but keeping identity means neither style can break."""
    assert _round_trip(Query(scope=Registry("r"), select=[Count])).select[0] is Count


@pytest.mark.parametrize("node", [
    Count,
    CountDistinct(Id),
    CountDistinct(Tag("mod")),
    Collect(Id),
    Collect(Attribute("localization"), limit=3),
])
def test_each_aggregate_node_round_trips(node):
    q = Query(scope=Registry("minecraft:item"), select=[node], group_by=[Id])
    assert _round_trip(q) == q


def test_every_ast_node_round_trips():
    """The guard for the *class* of bug, not this instance of it.

    `_enc` encodes any dataclass; `_dec` used to consult a hand-written list. So a new node
    was silently encodable-but-not-decodable until someone saved one and reopened the app.
    This walks the AST module itself, so the next tier up the Power Ladder cannot repeat it.
    """
    import dataclasses
    from packsmith.core.query import ast as ast_module
    from packsmith.core.query.serde import _NODE_TYPES

    declared = {name for name, obj in vars(ast_module).items()
                if isinstance(obj, type) and dataclasses.is_dataclass(obj)}
    missing = declared - set(_NODE_TYPES)
    assert not missing, f"AST nodes the decoder cannot rebuild: {sorted(missing)}"
