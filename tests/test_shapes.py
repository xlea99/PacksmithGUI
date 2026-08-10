"""Structural typing for blueprint mappings — design 3.3's `required_shape`.

The rule under test everywhere here: a mapping declares a SHAPE, never a schema name, and
matching is **at-least** — the user's blueprint must contain what the action asked for and
is free to contain anything else.
"""
import pytest

from packsmith.core.bindings import (
    blueprint_mismatches, blueprints_fitting, best_guess_bindings, resolve_step,
    validate_blueprint_binding,
)
from packsmith.core.blueprints import BlueprintStore
from packsmith.core.packages import ActionManifest, MappingSlot, _parse_mappings
from packsmith.core.shapes import describe_shape, mismatches, parse_shape, satisfies


STONE_SHAPE = {
    "base_block": {"kind": "registry_entry", "registry_type": "minecraft:block"},
    "polished": {
        "kind": "group",
        "slots": {
            "base": {"kind": "registry_entry", "registry_type": "minecraft:block"},
            "stairs": {"kind": "registry_entry", "registry_type": "minecraft:block"},
        },
    },
}


@pytest.fixture
def store(user_db):
    return BlueprintStore(user_db)


@pytest.fixture
def stone(store):
    """A schema that satisfies STONE_SHAPE, plus extra the action never asked for."""
    store.define("MyRockKind")
    store.add_slot("MyRockKind", "base_block", "registry",
                   registry_type="minecraft:block")
    store.add_group("MyRockKind", "polished")
    for name in ("base", "stairs", "slab"):        # slab is extra — must not matter
        store.add_slot("MyRockKind", name, "registry", parent="polished",
                       registry_type="minecraft:block")
    store.add_slot("MyRockKind", "notes", "string")   # extra too
    return store


def shape():
    return parse_shape(STONE_SHAPE, where="test")


# --- the grammar ----------------------------------------------------------------------

def test_a_shape_flattens_to_dotted_paths():
    assert [r.path for r in shape()] == [
        "base_block", "polished", "polished.base", "polished.stairs"]


def test_groups_nest_without_limit():
    deep = parse_shape({"a": {"kind": "group", "slots": {
        "b": {"kind": "group", "slots": {
            "c": {"kind": "bool"}}}}}}, where="test")
    assert [r.path for r in deep] == ["a", "a.b", "a.b.c"]


@pytest.mark.parametrize("raw, message", [
    ({"x": "nope"}, "must be a table"),
    ({"x": {}}, "declares no kind"),
    ({"x": {"kind": "wat"}}, "unknown kind"),
    ({"x": {"kind": "group"}}, "declares no slots"),
    ({"x": {"kind": "group", "slots": {}}}, "declares no slots"),
    ({"x": {"kind": "registry_entry"}}, "no registry_type"),
])
def test_a_malformed_shape_is_refused_at_load_time(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_shape(raw, where="test")


def test_a_shape_on_a_tag_mapping_is_an_author_error():
    with pytest.raises(ValueError, match="only applies to blueprint mappings"):
        _parse_mappings({"t": {"kind": "tag", "required_shape": STONE_SHAPE}})


def test_a_blueprint_mapping_carries_its_parsed_shape():
    mappings = _parse_mappings({"stones": {"kind": "blueprint",
                                           "required_shape": STONE_SHAPE}})
    assert describe_shape(mappings["stones"].required_shape) == (
        "base_block, polished.base, polished.stairs")


# --- matching is at-least --------------------------------------------------------------

def test_extra_slots_and_groups_are_none_of_the_actions_business(stone):
    assert satisfies(shape(), stone.slots("MyRockKind"))


def test_the_schemas_name_is_irrelevant(stone):
    """3.3: 'MyRockKind, StoneSchema42, Granite-Style — all fine as long as the
    structure matches.'"""
    stone.rename("MyRockKind", "Granite-Style")
    assert satisfies(shape(), stone.slots("Granite-Style"))


def test_a_missing_slot_is_named(stone):
    stone.remove_slot("MyRockKind", "polished.stairs")
    assert mismatches(shape(), stone.slots("MyRockKind")) == [
        "no slot 'polished.stairs' (needs a minecraft:block entry)"]


def test_the_wrong_registry_is_named(store):
    store.define("Wrong")
    store.add_slot("Wrong", "base_block", "registry", registry_type="minecraft:item")
    store.add_group("Wrong", "polished")
    for name in ("base", "stairs"):
        store.add_slot("Wrong", name, "registry", parent="polished",
                       registry_type="minecraft:block")
    assert mismatches(shape(), store.slots("Wrong")) == [
        "'base_block' takes minecraft:item, but the action needs minecraft:block"]


def test_the_wrong_type_is_named(store):
    store.define("Wrong")
    store.add_slot("Wrong", "base_block", "string")
    store.add_group("Wrong", "polished")
    for name in ("base", "stairs"):
        store.add_slot("Wrong", name, "registry", parent="polished",
                       registry_type="minecraft:block")
    assert mismatches(shape(), store.slots("Wrong")) == [
        "'base_block' holds a string value, but the action needs a minecraft:block entry"]


def test_a_slot_where_a_group_belongs_is_named(store):
    store.define("Flat")
    store.add_slot("Flat", "base_block", "registry", registry_type="minecraft:block")
    store.add_slot("Flat", "polished", "string")
    problems = mismatches(shape(), store.slots("Flat"))
    assert "'polished' is a slot, but the action needs a group" in problems


def test_enum_values_are_at_least(store):
    store.define("Tiered")
    store.add_slot("Tiered", "tier", "enum", enum_values=["early", "mid"])
    need = parse_shape({"tier": {"kind": "enum", "values": ["early", "late"]}},
                       where="test")
    assert mismatches(need, store.slots("Tiered")) == [
        "'tier' is missing enum value(s): late"]
    store.retype_slot("Tiered", "tier", "enum", enum_values=["early", "mid", "late"])
    assert satisfies(need, store.slots("Tiered"))     # extra 'mid' is fine


# --- binding --------------------------------------------------------------------------

def _slot(**kw):
    return MappingSlot(name="stones", kind="blueprint", **kw)


def test_fitting_schemas_are_offered_and_misfits_explain_themselves(stone):
    stone.define("Nope")
    stone.add_slot("Nope", "base_block", "string")
    slot = _slot(required_shape=shape())
    assert blueprints_fitting(slot, stone) == ["MyRockKind"]
    assert blueprint_mismatches(slot, "Nope", stone)     # non-empty = a reason to show


def test_binding_a_misfit_says_what_is_wrong(stone):
    stone.remove_slot("MyRockKind", "polished.stairs")
    with pytest.raises(ValueError, match="doesn't fit the shape"):
        validate_blueprint_binding(_slot(required_shape=shape()), "MyRockKind", stone)


def test_a_mapping_with_no_shape_accepts_any_schema(stone):
    """Shape is optional; an action that inspects the schema itself still works."""
    assert blueprints_fitting(_slot(), stone) == ["MyRockKind"]
    validate_blueprint_binding(_slot(), "MyRockKind", stone)


def test_best_guess_never_suggests_a_schema_that_does_not_fit(stone):
    stone.define("AAA_Decoy")           # sorts first; would win a naive "first available"
    stone.add_slot("AAA_Decoy", "base_block", "string")
    manifest = ActionManifest(package_name="p", action_id="a", file="a.star",
                              function="run",
                              mappings={"stones": _slot(required_shape=shape())})
    assert best_guess_bindings(manifest, None, stone) == {"stones": "MyRockKind"}


def test_a_schema_that_drifts_after_binding_fails_the_step(stone):
    """The re-check that matters: schemas are live, and a step bound last week can be
    invalidated by a rename today."""
    manifest = ActionManifest(package_name="p", action_id="a", file="a.star",
                              function="run",
                              mappings={"stones": _slot(required_shape=shape())})
    mappings, _ = resolve_step(manifest, bindings={"stones": "MyRockKind"}, config={},
                               tag_store=None, blueprint_store=stone)
    assert mappings == {"stones": "MyRockKind"}

    stone.rename_slot("MyRockKind", "polished.stairs", "steps")
    with pytest.raises(ValueError, match="no slot 'polished.stairs'"):
        resolve_step(manifest, bindings={"stones": "MyRockKind"}, config={},
                     tag_store=None, blueprint_store=stone)


# --- blueprint_instance mappings and cardinality (design 3.3) --------------------------

def _instances(store, blueprint, *names):
    for name in names:
        store.create_instance(blueprint, name)
    return store


def test_an_instance_ref_resolves_by_lookup_not_by_splitting(stone):
    """Names may contain ':' — only dots and blanks are banned — so 'A:B:C' is genuinely
    ambiguous and has to be resolved against what exists."""
    from packsmith.core.bindings import parse_instance_ref
    stone.create_instance("MyRockKind", "gran:ite")
    assert parse_instance_ref("MyRockKind:gran:ite", stone) == ("MyRockKind", "gran:ite")
    assert parse_instance_ref("MyRockKind:nope", stone) is None
    assert parse_instance_ref("nonsense", stone) is None


def test_instances_are_judged_by_their_schemas_shape(stone):
    from packsmith.core.bindings import instance_mismatches, instances_fitting
    _instances(stone, "MyRockKind", "granite", "andesite")
    stone.define("Nope")
    stone.add_slot("Nope", "base_block", "string")
    stone.create_instance("Nope", "wat")

    slot = MappingSlot(name="stones", kind="blueprint_instance",
                       required_shape=shape())
    # instances come back in the store's order, which is alphabetical
    assert instances_fitting(slot, stone) == ["MyRockKind:andesite", "MyRockKind:granite"]
    assert instance_mismatches(slot, "Nope:wat", stone)
    assert instance_mismatches(slot, "MyRockKind:ghost", stone) == [
        "no instance 'MyRockKind:ghost'"]


def test_many_can_mix_instances_from_different_schemas(stone):
    """3.3: 'instances can come from multiple user-schemas as long as each satisfies the
    declared required_shape — the action's contract is about shape, not schema identity.'"""
    from packsmith.core.bindings import instances_fitting
    _instances(stone, "MyRockKind", "granite")
    stone.define("StoneSchema42")           # same shape, different name
    stone.add_slot("StoneSchema42", "base_block", "registry",
                   registry_type="minecraft:block")
    stone.add_group("StoneSchema42", "polished")
    for n in ("base", "stairs"):
        stone.add_slot("StoneSchema42", n, "registry", parent="polished",
                       registry_type="minecraft:block")
    stone.create_instance("StoneSchema42", "tuff")

    slot = MappingSlot(name="stones", kind="blueprint_instance", cardinality="many",
                       required_shape=shape())
    assert instances_fitting(slot, stone) == ["MyRockKind:granite", "StoneSchema42:tuff"]


def _manifest(slot):
    """Keyed by the slot's own name — the manifest key and the mapping name are the same
    thing, and hardcoding one made every non-"stones" mapping read as unbound."""
    return ActionManifest(package_name="p", action_id="a", file="a.star", function="run",
                          mappings={slot.name: slot})


def test_an_instance_reaches_the_action_already_split(stone):
    """Every blueprint API takes blueprint and instance separately, so handing the action a
    ref string would be giving back a problem we already solved."""
    _instances(stone, "MyRockKind", "granite")
    slot = MappingSlot(name="stones", kind="blueprint_instance", required_shape=shape())
    mappings, _ = resolve_step(_manifest(slot), bindings={"stones": "MyRockKind:granite"},
                               config={}, tag_store=None, blueprint_store=stone)
    assert mappings["stones"] == {"blueprint": "MyRockKind", "instance": "granite",
                                  "ref": "MyRockKind:granite"}


def test_many_resolves_to_a_list_and_one_does_not(stone):
    _instances(stone, "MyRockKind", "granite", "andesite")
    many = MappingSlot(name="stones", kind="blueprint_instance", cardinality="many",
                       required_shape=shape())
    mappings, _ = resolve_step(
        _manifest(many),
        bindings={"stones": ["MyRockKind:granite", "MyRockKind:andesite"]},
        config={}, tag_store=None, blueprint_store=stone)
    assert [m["instance"] for m in mappings["stones"]] == ["granite", "andesite"]

    one = MappingSlot(name="stones", kind="blueprint_instance", required_shape=shape())
    with pytest.raises(ValueError, match="takes one artifact but 2 are bound"):
        resolve_step(_manifest(one),
                     bindings={"stones": ["MyRockKind:granite", "MyRockKind:andesite"]},
                     config={}, tag_store=None, blueprint_store=stone)


def test_an_empty_many_selection_is_unbound(stone):
    """3.3 calls `many` 'zero or more', and required mappings 'block execution if
    unbound' — a required slot with nothing selected cannot do its job."""
    required = MappingSlot(name="stones", kind="blueprint_instance", cardinality="many")
    with pytest.raises(ValueError, match="required mapping 'stones' is unbound"):
        resolve_step(_manifest(required), bindings={"stones": []}, config={},
                     tag_store=None, blueprint_store=stone)

    optional = MappingSlot(name="stones", kind="blueprint_instance", cardinality="many",
                           required=False)
    mappings, _ = resolve_step(_manifest(optional), bindings={"stones": []}, config={},
                               tag_store=None, blueprint_store=stone)
    assert mappings["stones"] == []


def test_one_misfit_in_a_many_binding_fails_the_whole_step(stone):
    _instances(stone, "MyRockKind", "granite")
    stone.define("Nope")
    stone.add_slot("Nope", "base_block", "string")
    stone.create_instance("Nope", "wat")
    slot = MappingSlot(name="stones", kind="blueprint_instance", cardinality="many",
                       required_shape=shape())
    with pytest.raises(ValueError, match="doesn't fit the shape"):
        resolve_step(_manifest(slot),
                     bindings={"stones": ["MyRockKind:granite", "Nope:wat"]},
                     config={}, tag_store=None, blueprint_store=stone)


def test_conflict_policy_keys_by_blueprint_even_for_instance_mappings(stone):
    """`_may_write` asks per blueprint, so two instances of one schema are one policy."""
    from packsmith.core.bindings import conflict_policies_for
    _instances(stone, "MyRockKind", "granite", "andesite")
    slot = MappingSlot(name="stones", kind="blueprint_instance", cardinality="many",
                       access="read_write", conflict_policy="skip", required_shape=shape())
    manifest = _manifest(slot)
    mappings, _ = resolve_step(
        manifest, bindings={"stones": ["MyRockKind:granite", "MyRockKind:andesite"]},
        config={}, tag_store=None, blueprint_store=stone)
    assert conflict_policies_for(manifest, mappings) == {"MyRockKind": "skip"}


def test_best_guess_for_many_offers_everything_that_qualifies(stone):
    _instances(stone, "MyRockKind", "granite", "andesite")
    slot = MappingSlot(name="stones", kind="blueprint_instance", cardinality="many",
                       required_shape=shape())
    assert best_guess_bindings(_manifest(slot), None, stone) == {
        "stones": ["MyRockKind:andesite", "MyRockKind:granite"]}


def test_an_invalid_cardinality_is_refused_at_load_time():
    with pytest.raises(ValueError, match="invalid cardinality"):
        _parse_mappings({"x": {"kind": "blueprint", "cardinality": "several"}})


# --- registry_entry mappings (design 3.3) ----------------------------------------------

class _Dump:
    registry = {"minecraft:block": {"values": ["minecraft:granite", "minecraft:stone"]}}


def test_a_registry_entry_mapping_must_declare_its_registry():
    with pytest.raises(ValueError, match="declares no registry_type"):
        _parse_mappings({"anchor": {"kind": "registry_entry"}})
    parsed = _parse_mappings({"anchor": {"kind": "registry_entry",
                                         "registry_type": "minecraft:block"}})
    assert parsed["anchor"].registry_type == "minecraft:block"


def test_a_registry_entry_binding_is_checked_against_the_pack():
    slot = MappingSlot(name="anchor", kind="registry_entry",
                       registry_type="minecraft:block")
    manifest = _manifest(slot)
    mappings, _ = resolve_step(manifest, bindings={"anchor": "minecraft:granite"},
                               config={}, tag_store=None, packdump=_Dump())
    assert mappings["anchor"] == "minecraft:granite"

    with pytest.raises(ValueError, match="is not in minecraft:block"):
        resolve_step(manifest, bindings={"anchor": "modded:nonsense"}, config={},
                     tag_store=None, packdump=_Dump())


def test_registry_entries_honour_cardinality_like_every_other_kind():
    many = MappingSlot(name="anchors", kind="registry_entry", cardinality="many",
                       registry_type="minecraft:block")
    mappings, _ = resolve_step(
        _manifest(many), bindings={"anchors": ["minecraft:granite", "minecraft:stone"]},
        config={}, tag_store=None, packdump=_Dump())
    assert mappings["anchors"] == ["minecraft:granite", "minecraft:stone"]


def test_a_shape_on_a_registry_entry_mapping_is_an_author_error():
    with pytest.raises(ValueError, match="only applies to blueprint mappings"):
        _parse_mappings({"anchor": {"kind": "registry_entry",
                                    "registry_type": "minecraft:block",
                                    "required_shape": STONE_SHAPE}})


def test_best_guess_only_offers_a_likely_entry_the_pack_actually_has():
    present = MappingSlot(name="anchor", kind="registry_entry",
                          registry_type="minecraft:block",
                          likely_name="minecraft:granite")
    absent = MappingSlot(name="anchor", kind="registry_entry",
                         registry_type="minecraft:block",
                         likely_name="modded:nonsense")
    assert best_guess_bindings(_manifest(present), None, packdump=_Dump()) == {
        "anchor": "minecraft:granite"}
    assert best_guess_bindings(_manifest(absent), None, packdump=_Dump()) == {
        "anchor": None}
