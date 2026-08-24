"""Asking a registry entry a question about a blueprint (design 3.2.4).

Every other field crosses L1 -> L2 in one direction: the row is a registry entry and the
question is about its tags. `Slot` refuses under a registry scope, correctly — a slot
belongs to one instance and an entry belongs to none.

`BoundIn` and `Mentions` cross the other way, and are well-formed where `Slot` is not
because neither asks *which* instance. They ask a yes/no question about the blueprint taken
whole: is this id claimed anywhere in it, does its name belong to the vocabulary of its
instances. Together they express the question the primitive implies but could not ask —

    MENTIONS "StoneType" AND NOT BOUND_IN "StoneType"

"everything that looks like it belongs to one of my stones, that I did not choose" — which
is the list of blocks to hide, and it has no other expression.

The failure that would HIDE here is a **quietly wrong set**. A view that returns 2,135 rows
where 1,461 are right looks perfectly healthy; you would work through it for an hour before
noticing `cobblestone_stove` is not one of your stones. So most of this file is about the
boundaries of the match, not about it working at all.
"""
import pytest

from packsmith.core.blueprints import BlueprintStore
from packsmith.core.query.ast import (
    And, BoundIn, Cmp, Id, Mentions, Not, Query, QueryError, Registry, Tag)
from packsmith.core.query.evaluator import evaluate
from packsmith.core.query.language import format as format_query, parse
from packsmith.core.tags import TagStore


class Dump:
    """A registry with the shapes that actually caused trouble in the real pack: an
    instance name that is a substring of unrelated blocks (`stone` inside `cobblestone`),
    a two-word instance name, and a mod naming a stone something else entirely."""
    registry = {
        "minecraft:item": {"values": [
            "minecraft:stone", "minecraft:stone_stairs", "minecraft:cobblestone",
            "minecraft:sandstone", "minecraft:end_stone", "bakery:cobblestone_stove",
            "minecraft:granite", "quark:granite_pillar", "stoneworks:granite_pillar",
            "caverns_and_chasms:granite_pillar", "quark:granite_bricks",
            "cataclysm:azure_seastone", "cataclysm:azure_seastone_wall",
            "alexscaves:limestone", "alexscaves:limestone_slab",
            "minecraft:oak_planks", "quark:blue_flower",
        ]},
    }

    def attribute(self, *_args, **_kwargs):
        return None


@pytest.fixture
def store(user_db):
    bp = BlueprintStore(user_db, packdump=Dump())
    bp.define("StoneType")
    bp.add_slot("StoneType", "base", "registry", registry_type="minecraft:item")
    bp.add_group("StoneType", "pillar")
    bp.add_slot("StoneType", "base", "registry", parent="pillar",
                registry_type="minecraft:item")
    bp.add_slot("StoneType", "search_terms", "string")

    bp.create_instance("StoneType", "stone")
    bp.bind("StoneType", "stone", "base", "minecraft:stone")

    bp.create_instance("StoneType", "granite")
    bp.bind("StoneType", "granite", "base", "minecraft:granite")
    bp.bind("StoneType", "granite", "pillar.base", "caverns_and_chasms:granite_pillar")

    bp.create_instance("StoneType", "azure_seastone")
    bp.bind("StoneType", "azure_seastone", "base", "cataclysm:azure_seastone")

    bp.create_instance("StoneType", "primestone")     # its blocks are named `limestone`
    return bp


def ids(store, tags=None, **kwargs):
    query = Query(scope=Registry("minecraft:item"), select=[Id], order_by=[Id], **kwargs)
    result = evaluate(query, packdump=Dump(), tag_store=tags, blueprint_store=store)
    return [row.values["id"] for row in result.rows]


# --- BoundIn ---------------------------------------------------------------------------

def test_bound_in_finds_exactly_what_the_blueprint_claimed(store):
    assert ids(store, filter=BoundIn("StoneType")) == [
        "cataclysm:azure_seastone", "caverns_and_chasms:granite_pillar",
        "minecraft:granite", "minecraft:stone"]


def test_not_bound_in_is_the_complement(store):
    every = ids(store)
    claimed = ids(store, filter=BoundIn("StoneType"))
    assert ids(store, filter=Not(BoundIn("StoneType"))) == \
        [e for e in every if e not in claimed]


def test_a_slot_narrows_it_to_one_decision(store):
    """"What else could have been granite's pillar" is a different question from "what did
    I pass over entirely", and it is the one you ask when second-guessing one cell."""
    assert ids(store, filter=BoundIn("StoneType", "pillar.base")) == \
        ["caverns_and_chasms:granite_pillar"]
    leftover_pillars = ids(store, filter=And([
        Not(BoundIn("StoneType", "pillar.base")), Cmp(Id, "contains", "pillar")]))
    assert leftover_pillars == ["quark:granite_pillar", "stoneworks:granite_pillar"]


# --- Mentions: the boundaries of the match ----------------------------------------------

def test_mentions_matches_whole_words_not_substrings(store):
    """The measured failure: `stone` is a real instance name, and substring matching drags
    in cobblestone, sandstone and cobblestone_stove — 674 false positives out of 2,135 in
    the real pack, a third of the answer being garbage."""
    found = ids(store, filter=Mentions("StoneType"))

    assert "minecraft:stone" in found and "minecraft:stone_stairs" in found
    for wrong in ("minecraft:cobblestone", "minecraft:sandstone",
                  "bakery:cobblestone_stove"):
        assert wrong not in found, f"{wrong} is not one of your stones"


def test_a_neighbouring_stone_is_included_on_purpose(store):
    """`end_stone` carries `stone` as a whole word, so it matches — and should.

    Token matching cannot tell `stone_stairs` (yours) from `end_stone` (not yours); both are
    the instance name plus one word. Given that, **over-inclusion is the safe direction**:
    this list exists to be reviewed, and being shown a block you then skip costs a glance,
    while never being shown one means it stays in the game forever without a decision. In
    the real pack the same rule pulls in `smooth_limestone` and `cobbled_permafrost` —
    unclaimed variants of real stones, and exactly what the view is for.
    """
    assert "minecraft:end_stone" in ids(store, filter=Mentions("StoneType"))


def test_a_two_word_instance_name_matches_as_both_words(store):
    """`azure_seastone` must not match on either half alone — a blueprint full of
    `*_seastone` variants would otherwise all collapse together."""
    found = ids(store, filter=Mentions("StoneType"))
    assert "cataclysm:azure_seastone_wall" in found


def test_mentions_ignores_what_is_not_a_stone_at_all(store):
    found = ids(store, filter=Mentions("StoneType"))
    assert "minecraft:oak_planks" not in found
    assert "quark:blue_flower" not in found


def test_an_instance_whose_blocks_are_named_otherwise_is_missed(store):
    """The honest limit, and the reason the alias slot exists. `primestone`'s blocks are
    all `alexscaves:limestone` — nothing about the name reaches them."""
    assert "alexscaves:limestone" not in ids(store, filter=Mentions("StoneType"))


def test_an_alias_slot_widens_the_net(store):
    store.bind("StoneType", "primestone", "search_terms", "limestone")

    found = ids(store, filter=Mentions("StoneType", "search_terms"))

    assert "alexscaves:limestone" in found
    assert "alexscaves:limestone_slab" in found


def test_aliases_add_and_never_replace(store):
    """A wider net costs nothing — the question is whether ANY instance mentions the entry
    — while a wrong exclusion costs a block going unnoticed, which is the whole point."""
    store.bind("StoneType", "primestone", "search_terms", "limestone")
    with_alias = ids(store, filter=Mentions("StoneType", "search_terms"))

    assert set(ids(store, filter=Mentions("StoneType"))) <= set(with_alias)
    assert "minecraft:granite" in with_alias, "the plain names still match"


def test_several_alias_terms_in_one_cell(store):
    store.bind("StoneType", "primestone", "search_terms", "limestone oak")
    found = ids(store, filter=Mentions("StoneType", "search_terms"))
    assert "alexscaves:limestone" in found and "minecraft:oak_planks" in found


def test_an_empty_alias_cell_changes_nothing(store):
    """Absent means "the name is enough", which is true for almost every instance — 98% of
    them, measured. An empty cell must not narrow anything."""
    assert ids(store, filter=Mentions("StoneType", "search_terms")) == \
        ids(store, filter=Mentions("StoneType"))


# --- the view this all exists for --------------------------------------------------------

def test_the_leftover_view(store, user_db):
    """Everything that looks like one of your stones, that you did not choose. The `remove`
    tag rides along so the view is where you *decide*, not just where you look."""
    tags = TagStore(user_db)
    tags.define("minecraft:item", "remove", "bool")
    tags.assign("minecraft:item", "stoneworks:granite_pillar", "remove", True)

    query = Query(scope=Registry("minecraft:item"), select=[Id, Tag("remove")],
                  filter=parse('MENTIONS "StoneType" AND NOT BOUND_IN "StoneType"'),
                  order_by=[Id])
    result = evaluate(query, packdump=Dump(), tag_store=tags, blueprint_store=store)
    rows = {r.values["id"]: r.values["remove"] for r in result.rows}

    assert "quark:granite_pillar" in rows, "an alternative you passed over"
    assert rows["stoneworks:granite_pillar"] is True, "a decision already made"
    assert "caverns_and_chasms:granite_pillar" not in rows, "this one you chose"
    assert "minecraft:cobblestone" not in rows, "not one of your stones"
    assert "minecraft:oak_planks" not in rows


# --- it is data, so it has to survive being written down ---------------------------------

@pytest.mark.parametrize("text", [
    'MENTIONS "StoneType"',
    'BOUND_IN "StoneType"',
    'MENTIONS ("StoneType", "search_terms")',
    'BOUND_IN ("StoneType", "pillar.base")',
    'MENTIONS "StoneType" AND NOT BOUND_IN "StoneType"',
])
def test_round_trips_through_text_and_json(text):
    """A View stores its query as JSON and shows it as text; a node that cannot make both
    trips is unsaveable, and finds out at load time in a later session."""
    from packsmith.core.query import serde

    node = parse(text)
    assert format_query(node) == text
    assert parse(format_query(node)) == node
    assert serde.from_dict(serde.to_dict(node)) == node


# --- refusing clearly ---------------------------------------------------------------------

def test_without_a_blueprint_store_it_says_so(store):
    """Rather than matching nothing, which reads as "you have no leftovers" — the most
    reassuring possible way to be wrong."""
    query = Query(scope=Registry("minecraft:item"), select=[Id],
                  filter=BoundIn("StoneType"))
    with pytest.raises(QueryError, match="needs a blueprint store"):
        evaluate(query, packdump=Dump())


def test_an_unknown_blueprint_is_an_error_not_an_empty_set(store):
    query = Query(scope=Registry("minecraft:item"), select=[Id],
                  filter=Mentions("NoSuchBlueprint"))
    with pytest.raises(Exception):
        evaluate(query, packdump=Dump(), blueprint_store=store)


# --- cost -----------------------------------------------------------------------------------

def test_the_binding_set_is_built_once_per_query_not_once_per_row(store):
    """18,639 rows each asking the store the same question is the shape of every performance
    bug this codebase has had. Counted rather than timed: a timing assertion passes with the
    optimisation reverted whenever the machine is fast enough."""
    calls = []
    original = store.all_bindings
    store.all_bindings = lambda name: (calls.append(name), original(name))[1]

    ids(store, filter=And([Mentions("StoneType"), Not(BoundIn("StoneType"))]))

    assert len(calls) <= 2, f"read the bindings {len(calls)} times for one query"
