"""A blueprint slot that holds many values (design 3.2.2, 2026-08-29).

**Why it exists**, because "three nodes out of 761" is not the argument: a tadpole is not a
subspecies of a frog, it is the same organism at a juvenile stage. Modelling `minecraft:frog`
and `minecraft:tadpole` as separate nodes would assert a descent relationship that does not
exist, and a second slot called `mob_juvenile` hardcodes one life stage as schema — then
butterflies arrive with four, and Ancient Remnant is two variants rather than a life stage at
all. The fact is "this organism is represented by these N entity ids", and N is the honest
count.

Everything here guards a failure that is silent. Cardinality is a property of a slot the
reader may never have looked at, so the dangerous outcomes are all the ones where the wrong
thing happens confidently.
"""
import pytest

from packsmith.core.blueprints import BlueprintStore, BlueprintError
from packsmith.core.query import QueryError, evaluate
from packsmith.core.query.ast import Blueprint as BpScope, Id, Query, Slot, Cmp

ENT = "minecraft:entity_type"
MOBS = ["minecraft:frog", "minecraft:tadpole", "minecraft:zombie",
        "ambientadditions:leaf_frog", "ambientadditions:leaf_frog_tadpole"]


class Dump:
    registry = {ENT: {"values": MOBS}}

    def attribute(self, *a):
        return None


@pytest.fixture
def bp(user_db):
    store = BlueprintStore(user_db, Dump())
    store.define("Lifeform")
    store.add_slot("Lifeform", "mob", "registry", registry_type=ENT, cardinality="many")
    store.add_slot("Lifeform", "ancestor", "blueprint", ref_blueprint="Lifeform")
    for name in ("Swamp Frog", "Leaf Frog", "Zombie"):
        store.create_instance("Lifeform", name)
    return store


# --- holding several --------------------------------------------------------------------

def test_a_many_slot_keeps_every_value_in_order(bp):
    """Ordered, and the order is the binding. `[frog, tadpole]` and `[tadpole, frog]` are
    different bindings — position is in the primary key for exactly this reason."""
    bp.bind("Lifeform", "Swamp Frog", "mob", ["minecraft:frog", "minecraft:tadpole"])
    assert bp.bindings("Lifeform", "Swamp Frog")["mob"].values == \
        ("minecraft:frog", "minecraft:tadpole")


def test_rebinding_replaces_the_whole_list(bp):
    """Same promise a one-slot makes — a cell holds what you last put in it — which is what
    keeps an action re-asserting its output idempotent."""
    bp.bind("Lifeform", "Swamp Frog", "mob", ["minecraft:frog", "minecraft:tadpole"])
    bp.bind("Lifeform", "Swamp Frog", "mob", ["minecraft:zombie"])
    assert bp.bindings("Lifeform", "Swamp Frog")["mob"].values == ("minecraft:zombie",)


def test_a_shorter_list_leaves_no_tail(bp):
    """The bug an upsert keyed by position would have: rebinding two values over three
    updates the first two and leaves the third behind as a row nobody wrote."""
    bp.bind("Lifeform", "Swamp Frog", "mob",
            ["minecraft:frog", "minecraft:tadpole", "minecraft:zombie"])
    bp.bind("Lifeform", "Swamp Frog", "mob", ["minecraft:frog"])
    assert bp.bindings("Lifeform", "Swamp Frog")["mob"].values == ("minecraft:frog",)


def test_the_same_value_twice_is_refused(bp):
    with pytest.raises(BlueprintError, match="same value twice"):
        bp.bind("Lifeform", "Swamp Frog", "mob", ["minecraft:frog", "minecraft:frog"])


# --- the scalar accessor refuses rather than changing type -------------------------------

def test_value_raises_on_a_many_slot(bp):
    """**The load-bearing refusal.** Returning a list from `.value` would change the TYPE of
    a field based on schema the reader never looked at — in Starlark, which has no type
    checking. Five action files do things like `bound[path].startswith(...)`; under a silent
    list they produce a wrong plan rather than an error."""
    bp.bind("Lifeform", "Swamp Frog", "mob", ["minecraft:frog", "minecraft:tadpole"])
    binding = bp.bindings("Lifeform", "Swamp Frog")["mob"]

    with pytest.raises(BlueprintError, match="`.values`"):
        binding.value


def test_value_still_works_for_a_one_slot(bp):
    """The other 99%. Every existing consumer has to keep working untouched."""
    bp.bind("Lifeform", "Zombie", "mob", ["minecraft:zombie"])
    bp.bind("Lifeform", "Leaf Frog", "ancestor", "Zombie")
    assert bp.bindings("Lifeform", "Leaf Frog")["ancestor"].value is not None


# --- cardinality is enforced in both directions ------------------------------------------

def test_a_scalar_into_a_many_slot_is_refused(bp):
    """Not coerced to a one-item list. The caller meant something the schema does not say,
    and guessing which is how a wrong binding gets written confidently."""
    with pytest.raises(BlueprintError, match="bind a list"):
        bp.bind("Lifeform", "Swamp Frog", "mob", "minecraft:frog")


def test_a_list_into_a_one_slot_is_refused(bp):
    with pytest.raises(BlueprintError, match="holds one value"):
        bp.bind("Lifeform", "Swamp Frog", "ancestor", ["Zombie", "Leaf Frog"])


def test_an_unknown_cardinality_is_refused(bp):
    with pytest.raises(BlueprintError, match="'one' or 'many'"):
        bp.add_slot("Lifeform", "oops", "string", cardinality="several")


# --- querying a many-slot ----------------------------------------------------------------

def ids(bp, node):
    result = evaluate(Query(scope=BpScope("Lifeform"), filter=node, select=[Id]),
                      packdump=Dump(), blueprint_store=bp)
    return sorted(r.values["id"] for r in result.rows)


def test_contains_is_membership_not_substring(bp):
    """**The failure this feature would otherwise have shipped with.** `contains` used to
    `str()` anything that was not a string, so a list was searched as its repr — right often
    enough to look correct, and wrong exactly here: `ambientadditions:leaf_frog` is a prefix
    of `ambientadditions:leaf_frog_tadpole`, and Leaf Frog is one of the three organisms the
    whole feature exists for."""
    bp.bind("Lifeform", "Leaf Frog", "mob", ["ambientadditions:leaf_frog_tadpole"])

    assert ids(bp, Cmp(Slot("mob"), "contains", "ambientadditions:leaf_frog")) == []
    assert ids(bp, Cmp(Slot("mob"), "contains",
                       "ambientadditions:leaf_frog_tadpole")) == ["Leaf Frog"]


def test_contains_finds_any_element(bp):
    bp.bind("Lifeform", "Swamp Frog", "mob", ["minecraft:frog", "minecraft:tadpole"])
    assert ids(bp, Cmp(Slot("mob"), "contains", "minecraft:tadpole")) == ["Swamp Frog"]


@pytest.mark.parametrize("op", ["eq", "neq", "gt", "lt", "gte", "lte", "in", "not_in"])
def test_ambiguous_operators_are_refused(bp, op):
    """`==` was the live question. Exact-list-equivalence is algebraically correct and
    almost never intended — somebody typing `mob == "frog"` is asking whether the frog is in
    there. Answering a different question correctly and silently is the failure this project
    keeps removing, so it errors and names `contains` instead. It stays upgradeable: exact
    equivalence can be added later and cannot be removed once views depend on it."""
    bp.bind("Lifeform", "Swamp Frog", "mob", ["minecraft:frog"])
    value = ["minecraft:frog"] if op in ("in", "not_in") else "minecraft:frog"

    with pytest.raises(QueryError, match="many values"):
        ids(bp, Cmp(Slot("mob"), op, value))


def test_has_still_answers_presence(bp):
    from packsmith.core.query.ast import Has
    bp.bind("Lifeform", "Swamp Frog", "mob", ["minecraft:frog"])
    assert ids(bp, Has(Slot("mob"))) == ["Swamp Frog"]
