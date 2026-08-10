"""The query engine v1: AST, evaluator (registry scope), serialization (design 3.2.4).

Covers the power ladder's v1 rung — filter/project/sort/limit/distinct over registry
entries x attributes x tags — plus the two load-bearing invariants: a query round-trips
through JSON as data, and editability follows from the query's shape.
"""
import json

import pytest

from packsmith.core.query import (
    Query, Registry, Blueprint, Id, Mod, Tag, Attribute, Slot,
    Cmp, Has, And, Or, Not, evaluate, to_dict, from_dict, QueryError,
)

REG = "minecraft:item"


class FakeDump:
    """Minimal packdump: a registry of entry ids + a localization attribute map."""

    def __init__(self, entries, localization=None):
        self.registry = {REG: {"values": list(entries)}}
        self._loc = localization or {}

    def attribute(self, registry_type, entry_id, name):
        if name != "localization":
            return None
        return self._loc.get((registry_type, entry_id))


@pytest.fixture
def world(tags):
    """A small, known world: five items across three mods, some tagged, one unlocalized."""
    entries = [
        "alexscaves:galena", "alexscaves:galena_ore", "quark:rope",
        "minecraft:iron_ore", "minecraft:diamond",
    ]
    loc = {
        (REG, "alexscaves:galena"): "Galena",
        (REG, "alexscaves:galena_ore"): "Galena Ore",
        (REG, "minecraft:iron_ore"): "Iron Ore",
        (REG, "quark:rope"): "Rope",
        # minecraft:diamond intentionally has no localization
    }
    tags.define(REG, "remove", "bool", default=False)
    tags.define(REG, "tier", "enum", ["early", "mid", "late"])
    tags.assign(REG, "alexscaves:galena", "remove", True, owner="user")
    tags.assign(REG, "quark:rope", "remove", True, owner="user")
    tags.assign(REG, "alexscaves:galena_ore", "tier", "mid", owner="user")
    tags.assign(REG, "minecraft:iron_ore", "tier", "early", owner="user")
    return FakeDump(entries, loc), tags


def _ids(result):
    return [r.values["id"] for r in result.rows]


# --- v1 power ladder: filter / project / sort / distinct / limit ------------

def test_filter_by_tag(world):
    dump, tags = world
    q = Query(scope=Registry(REG), filter=Cmp(Tag("remove"), "eq", True), select=[Id])
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert set(_ids(res)) == {"alexscaves:galena", "quark:rope"}
    assert res.column_names == ["id"]
    assert res.columns[0].type == "id"


def test_intrinsic_derivation_and_attribute_ordered(world):
    dump, tags = world
    q = Query(
        scope=Registry(REG),
        filter=Cmp(Mod, "eq", "alexscaves"),
        select=[Id, Attribute("localization")],
        order_by=[Attribute("localization")],
    )
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert _ids(res) == ["alexscaves:galena", "alexscaves:galena_ore"]  # "Galena" < "Galena Ore"
    assert res.rows[0].values["localization"] == "Galena"
    assert res.column_names == ["id", "localization"]


def test_has_and_regex_matches(world):
    dump, tags = world
    q = Query(
        scope=Registry(REG),
        filter=And([Has(Tag("tier")), Cmp(Id, "matches", r".*_ore$")]),
        select=[Id, Tag("tier")],
    )
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert set(_ids(res)) == {"alexscaves:galena_ore", "minecraft:iron_ore"}
    assert res.columns[1].type == "enum"   # tier's column type comes from its definition


def test_distinct_mods(world):
    dump, tags = world
    q = Query(scope=Registry(REG), select=[Mod], distinct=True, order_by=[Mod])
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert [r.values["mod"] for r in res.rows] == ["alexscaves", "minecraft", "quark"]


def test_limit(world):
    dump, tags = world
    q = Query(scope=Registry(REG), select=[Id], order_by=[Id], limit=2)
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert _ids(res) == ["alexscaves:galena", "alexscaves:galena_ore"]


def test_boolean_or_and_not(world):
    dump, tags = world
    # remove==true OR (not a minecraft mod)
    q = Query(
        scope=Registry(REG),
        filter=Or([Cmp(Tag("remove"), "eq", True), Not(Cmp(Mod, "eq", "minecraft"))]),
        select=[Id],
    )
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert set(_ids(res)) == {
        "alexscaves:galena", "alexscaves:galena_ore", "quark:rope",  # non-minecraft
        # (both remove-tagged items are already non-minecraft here)
    }


def test_in_operator(world):
    dump, tags = world
    q = Query(scope=Registry(REG), filter=Cmp(Mod, "in", ["quark", "minecraft"]), select=[Id])
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert set(_ids(res)) == {"quark:rope", "minecraft:iron_ore", "minecraft:diamond"}


def test_contains_case_insensitive(world):
    dump, tags = world
    q = Query(
        scope=Registry(REG),
        filter=Cmp(Attribute("localization"), "contains", "ORE", ci=True),
        select=[Id],
    )
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert set(_ids(res)) == {"alexscaves:galena_ore", "minecraft:iron_ore"}


# --- null semantics: comparisons only match entries with a value ------------

def test_unset_tag_excluded_from_comparisons(world):
    dump, tags = world
    eq = Query(scope=Registry(REG), filter=Cmp(Tag("tier"), "eq", "mid"), select=[Id])
    assert set(_ids(evaluate(eq, packdump=dump, tag_store=tags))) == {"alexscaves:galena_ore"}
    # neq excludes unset entries too — presence is expressed with Has, not neq
    neq = Query(scope=Registry(REG), filter=Cmp(Tag("tier"), "neq", "mid"), select=[Id])
    assert set(_ids(evaluate(neq, packdump=dump, tag_store=tags))) == {"minecraft:iron_ore"}


def test_missing_attribute_is_none_not_id(world):
    dump, tags = world
    q = Query(scope=Registry(REG), filter=Cmp(Id, "eq", "minecraft:diamond"),
              select=[Id, Attribute("localization")])
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert res.rows[0].values["localization"] is None   # no id fallback in the raw attribute


# --- editability follows from shape -----------------------------------------

def test_entry_backed_rows_are_editable(world):
    dump, tags = world
    q = Query(scope=Registry(REG), filter=Cmp(Tag("remove"), "eq", True), select=[Id])
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert res.rows and all(r.editable and r.entry_id == r.values["id"] for r in res.rows)


def test_distinct_rows_are_computed_not_editable(world):
    dump, tags = world
    q = Query(scope=Registry(REG), select=[Mod], distinct=True)
    res = evaluate(q, packdump=dump, tag_store=tags)
    assert res.rows and all(r.entry_id is None and not r.editable for r in res.rows)


# --- a query is serializable data -------------------------------------------

def test_json_round_trip():
    q = Query(
        scope=Registry("minecraft:item"),
        filter=And([
            Has(Tag("tier")),
            Or([
                Cmp(Id, "matches", r".*_ore$"),
                Cmp(Attribute("localization"), "contains", "ORE", ci=True),
                Not(Cmp(Mod, "in", ["minecraft"])),
            ]),
        ]),
        select=[Id, Mod, Tag("tier"), Attribute("localization")],
        order_by=[Attribute("localization")],
        limit=25,
        distinct=True,
    )
    restored = from_dict(json.loads(json.dumps(to_dict(q))))
    assert restored == q


def test_round_trip_bare_intrinsics_are_singletons():
    q = Query(scope=Registry(REG), select=[Id, Mod])
    restored = from_dict(json.loads(json.dumps(to_dict(q))))
    assert restored.select[0] is Id and restored.select[1] is Mod


# --- guardrails -------------------------------------------------------------

def test_blueprint_scope_rejected_in_v1(world):
    dump, tags = world
    with pytest.raises(QueryError):
        evaluate(Query(scope=Blueprint("StoneType"), select=[Id]), packdump=dump, tag_store=tags)


def test_empty_select_rejected(world):
    dump, tags = world
    with pytest.raises(QueryError):
        evaluate(Query(scope=Registry(REG), select=[]), packdump=dump, tag_store=tags)


def test_unknown_op_rejected(world):
    dump, tags = world
    q = Query(scope=Registry(REG), filter=Cmp(Id, "sideways", "x"), select=[Id])
    with pytest.raises(QueryError):
        evaluate(q, packdump=dump, tag_store=tags)


# --- Has means ASSIGNED, not "has a value" (design 3.2.4; root cause R1) ----

def test_has_ignores_the_default_and_asks_whether_a_row_exists(world):
    """The catastrophic one. §3.2.4 defines Has as "the tag is assigned" and §3.2.1 keeps
    defaults out of the database — but the resolver sugars a pristine cell into the
    default, so asking existence through it made HAS true for EVERY entry. `remove` has a
    default, which is exactly why this hid: it's the flagship tag."""
    dump, tags = world
    q = Query(scope=Registry(REG), filter=Has(Tag("remove")), select=[Id])
    assert set(_ids(evaluate(q, packdump=dump, tag_store=tags))) == {
        "alexscaves:galena", "quark:rope"}


def test_not_has_finds_the_gaps_rather_than_nothing(world):
    """`NOT HAS` is the gap-finding idiom the engine is built around; it returned [] for
    every defaulted tag."""
    dump, tags = world
    q = Query(scope=Registry(REG), filter=Not(Has(Tag("remove"))), select=[Id])
    assert set(_ids(evaluate(q, packdump=dump, tag_store=tags))) == {
        "alexscaves:galena_ore", "minecraft:iron_ore", "minecraft:diamond"}


def test_has_and_not_has_partition_the_registry(world):
    dump, tags = world
    have = set(_ids(evaluate(Query(scope=Registry(REG), filter=Has(Tag("remove")),
                                   select=[Id]), packdump=dump, tag_store=tags)))
    havent = set(_ids(evaluate(Query(scope=Registry(REG), filter=Not(Has(Tag("remove"))),
                                     select=[Id]), packdump=dump, tag_store=tags)))
    everything = set(dump.registry[REG]["values"])
    assert have | havent == everything and not (have & havent)


def test_assigning_the_default_value_explicitly_still_counts_as_assigned(world):
    """"Explicitly false" and "pristine, defaulting to false" look identical through
    get_tag and are different states — only existence tells them apart."""
    dump, tags = world
    tags.assign(REG, "minecraft:diamond", "remove", False, owner="user")
    q = Query(scope=Registry(REG), filter=Has(Tag("remove")), select=[Id])
    assert "minecraft:diamond" in _ids(evaluate(q, packdump=dump, tag_store=tags))


def test_has_on_an_undefaulted_tag_was_always_correct(world):
    """The control that explains why this survived so long: with no default there is
    nothing to inflate, and `tier` has none."""
    dump, tags = world
    q = Query(scope=Registry(REG), filter=Has(Tag("tier")), select=[Id])
    assert set(_ids(evaluate(q, packdump=dump, tag_store=tags))) == {
        "alexscaves:galena_ore", "minecraft:iron_ore"}


def test_has_on_intrinsics_and_attributes_is_unchanged(world):
    """id/mod/attributes have no defaults, so presence there is still "resolved to
    something" — diamond has no localization."""
    dump, tags = world
    q = Query(scope=Registry(REG), filter=Not(Has(Attribute("localization"))), select=[Id])
    assert _ids(evaluate(q, packdump=dump, tag_store=tags)) == ["minecraft:diamond"]
