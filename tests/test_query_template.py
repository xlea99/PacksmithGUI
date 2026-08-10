"""Recommendation templates (design 5.3 Part B).

One query, authored once, resolved per cell. The test that matters most is the namespace
one: keeping the mod name in a value parameter turns a 6-candidate search into a
0-candidate one, and it fails *silently* — the cell just looks like nothing exists.
"""
import pytest

from packsmith.core.blueprints import BlueprintStore
from packsmith.core.query.evaluator import evaluate
from packsmith.core.query.language import format, parse
from packsmith.core.query.template import (
    UnresolvedParam, candidate_query, context_for, parameters, resolve, strip_namespace,
)
from tests.test_blueprints import FakeDump


class Dump(FakeDump):
    def attribute(self, _registry, _entry, _name):
        return None                      # these ids carry no display names

    registry = {
        "minecraft:block": {"values": [
            "minecraft:granite", "minecraft:polished_granite",
            "minecraft:polished_granite_stairs", "minecraft:polished_granite_slab",
            "quark:granite_bricks_wall", "stoneworks:granite_brick_wall",
            "minecraft:andesite", "minecraft:polished_andesite_stairs",
        ]},
    }


@pytest.fixture
def store(user_db):
    bp = BlueprintStore(user_db, packdump=Dump())
    bp.define("StoneType")
    bp.add_slot("StoneType", "base_block", "registry", registry_type="minecraft:block")
    bp.add_slot("StoneType", "notes", "string")
    bp.add_group("StoneType", "polished")
    for form in ("base", "stairs", "slab"):
        bp.add_slot("StoneType", form, "registry", parent="polished",
                    registry_type="minecraft:block")
    bp.create_instance("StoneType", "granite")
    bp.bind("StoneType", "granite", "base_block", "minecraft:granite")
    bp.create_instance("StoneType", "tuff")          # anchor deliberately unbound
    return bp


def candidates(store, template_text, instance, path, dump=None):
    slot = store.slot("StoneType", path)
    context = context_for(store, "StoneType", instance, slot)
    resolved = resolve(parse(template_text), context)
    query = candidate_query(resolved, slot)
    return [r.values["id"] for r in evaluate(query, packdump=dump or Dump()).rows]


# --- a template is still plain data ------------------------------------------

def test_parameters_need_no_new_ast_node():
    """A parameter is a token that starts with '@', so a template saves, prints and
    round-trips exactly like any other query."""
    node = parse("@b:base_block @slot")
    assert format(node) == "@b:base_block @slot"
    assert parse(format(node)) == node
    assert parameters(node) == ["@b:base_block", "@slot"]


def test_a_half_typed_parameter_is_not_turned_into_a_prefix():
    """The live-typing prefix rule must not mangle '@slot' into '@slot*'."""
    assert parse("@slot", typing=True) == parse("@slot")


# --- resolution --------------------------------------------------------------

def test_the_design_example_resolves_per_cell(store):
    """§5.3's shape: anchored on the base binding, parameterised over the instance."""
    slot = store.slot("StoneType", "polished.stairs")
    context = context_for(store, "StoneType", "granite", slot)
    resolved = resolve(parse("@b:base_block @slot"), context)
    assert resolved.clauses[0].value == ["granite", "polished", "stairs"]


def test_value_parameters_drop_the_namespace(store):
    """THE trap. `minecraft:granite` tokenises to {minecraft, granite}, and requiring
    'minecraft' excludes every cross-mod candidate — silently."""
    assert strip_namespace("minecraft:granite") == "granite"
    found = candidates(store, "@b:base_block brick wall", "granite", "polished.stairs")
    assert "quark:granite_bricks_wall" in found
    assert "stoneworks:granite_brick_wall" in found


def test_slot_expands_to_the_whole_path(store):
    """`polished.stairs` contributes BOTH tokens — the group is half the narrowing."""
    slot = store.slot("StoneType", "polished.stairs")
    context = context_for(store, "StoneType", "granite", slot)
    assert resolve(parse("@slot"), context).clauses[0].value == ["polished", "stairs"]


def test_instance_is_available_as_a_parameter(store):
    slot = store.slot("StoneType", "polished.stairs")
    context = context_for(store, "StoneType", "granite", slot)
    assert resolve(parse("@instance"), context).clauses[0].value == ["granite"]


# --- the candidate search ----------------------------------------------------

def test_a_template_finds_the_right_candidates(store):
    assert candidates(store, "@b:base_block @slot", "granite", "polished.stairs") == \
        ["minecraft:polished_granite_stairs"]
    assert candidates(store, "@b:base_block @slot", "granite", "polished.slab") == \
        ["minecraft:polished_granite_slab"]


def test_the_scope_comes_from_the_slot_not_the_author(store):
    """A slot already declares what it accepts. §5.3: the type is the wall, the
    recommendation only ranks what's inside it — it "must never restrict"."""
    slot = store.slot("StoneType", "polished.stairs")
    query = candidate_query(resolve(parse("granite"), context_for(
        store, "StoneType", "granite", slot)), slot)
    assert query.scope.type == "minecraft:block"


def test_a_non_registry_slot_has_nothing_to_search(store):
    slot = store.slot("StoneType", "notes")
    with pytest.raises(UnresolvedParam, match="no registry to search"):
        candidate_query(parse("granite"), slot)


# --- unresolvable templates --------------------------------------------------

def test_an_unbound_anchor_is_reported_not_guessed(store):
    """tuff has no base_block yet. The bar goes red; the cell simply has no suggestions.
    Falling back to @slot alone would suggest every stairs block in the pack."""
    with pytest.raises(UnresolvedParam, match="isn't bound on this row yet"):
        candidates(store, "@b:base_block @slot", "tuff", "polished.stairs")


def test_an_unknown_parameter_says_so(store):
    slot = store.slot("StoneType", "polished.stairs")
    with pytest.raises(UnresolvedParam, match="unknown parameter"):
        resolve(parse("@nonsense"), context_for(store, "StoneType", "granite", slot))


def test_a_malformed_parameter_is_never_treated_as_a_literal(store):
    """`@b:` is a typo, not a search for the text "@b:". Silently searching for it would
    return nothing and look like "no candidates exist"."""
    slot = store.slot("StoneType", "polished.stairs")
    context = context_for(store, "StoneType", "granite", slot)
    for broken in ("@b:", "@1nvalid"):        # a bare "@" fails earlier, at parse
        with pytest.raises(UnresolvedParam, match="not a valid parameter"):
            resolve(parse(broken), context)


def test_literal_tokens_mix_with_parameters(store):
    assert candidates(store, "@b:base_block polished", "granite", "polished.base") == [
        "minecraft:polished_granite", "minecraft:polished_granite_slab",
        "minecraft:polished_granite_stairs"]


# --- @group / @leaf: why one template isn't always enough --------------------

def test_group_and_leaf_split_what_slot_joins(store):
    slot = store.slot("StoneType", "polished.stairs")
    context = context_for(store, "StoneType", "granite", slot)
    assert resolve(parse("@group"), context).clauses[0].value == ["polished"]
    assert resolve(parse("@leaf"), context).clauses[0].value == ["stairs"]


def test_slot_is_too_literal_for_a_base_column_and_group_fixes_it(store):
    """Measured on the real pack: `@slot` gives 0 candidates for every `*.base` column,
    because a cut's base form is named `polished_granite`, never `polished_granite_base`.
    That's the case per-column overrides exist for, and `@group` is how you write one."""
    assert candidates(store, "@b:base_block @slot", "granite", "polished.base") == []
    assert candidates(store, "@b:base_block @group", "granite", "polished.base") == [
        "minecraft:polished_granite", "minecraft:polished_granite_slab",
        "minecraft:polished_granite_stairs"]


def test_group_on_a_top_level_column_says_so(store):
    slot = store.slot("StoneType", "base_block")
    context = context_for(store, "StoneType", "granite", slot)
    with pytest.raises(UnresolvedParam, match="isn't inside a group"):
        resolve(parse("@group"), context)


def test_col_is_not_a_parameter(store):
    """A column is presentation — the same blueprint through §5.2's node view has nodes,
    not columns — while the SLOT being filled is schema."""
    slot = store.slot("StoneType", "polished.stairs")
    context = context_for(store, "StoneType", "granite", slot)
    with pytest.raises(UnresolvedParam, match="unknown parameter"):
        resolve(parse("@col"), context)
