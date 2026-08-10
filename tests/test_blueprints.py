"""Blueprints, step 1: schemas, instances, bindings (design 3.2.2).

Tests are written around the case the design is written around — the stone palette. That
isn't decoration: "40+ stone types × cuts × forms" is the thing blueprints exist for, and a
model that can't express `StoneType:granite` cleanly has failed regardless of what a toy
schema proves.
"""
import pytest

from packsmith.core.blueprints import BlueprintStore, BlueprintError, Instance


class FakeDump:
    """Just enough registry for bind-time validation (3.2.2: "validated on bind")."""
    registry = {
        "minecraft:block": {"values": [
            "minecraft:granite", "minecraft:polished_granite",
            "minecraft:polished_granite_stairs", "minecraft:polished_granite_slab",
            "minecraft:andesite", "minecraft:polished_andesite",
            "quark:granite_bricks",
        ]},
        "minecraft:item": {"values": ["minecraft:stick"]},
    }


@pytest.fixture
def store(user_db):
    return BlueprintStore(user_db, packdump=FakeDump())


@pytest.fixture
def stone(store):
    """The schema from the design doc, near enough verbatim."""
    store.define("StoneType", "One stone and everything it can be cut into")
    store.add_slot("StoneType", "base_block", "registry", registry_type="minecraft:block")
    store.add_group("StoneType", "polished")
    for form in ("base", "stairs", "slab", "wall"):
        store.add_slot("StoneType", form, "registry", parent="polished",
                       registry_type="minecraft:block")
    store.add_group("StoneType", "bricks")
    store.add_slot("StoneType", "base", "registry", parent="bricks",
                   registry_type="minecraft:block")
    return store


# --- schemas ----------------------------------------------------------------

def test_a_schema_is_a_tree_of_groups_and_slots(stone):
    paths = [s.path for s in stone.slots("StoneType")]
    assert paths == [
        "base_block",
        "polished", "polished.base", "polished.stairs", "polished.slab", "polished.wall",
        "bricks", "bricks.base",
    ]
    assert stone.slot("StoneType", "polished").is_group
    assert not stone.slot("StoneType", "polished.stairs").is_group


def test_the_same_slot_name_can_live_in_different_groups(stone):
    """`polished.base` and `bricks.base` are different slots — the flat stub schema
    couldn't express this at all."""
    assert stone.slot("StoneType", "polished.base").id != \
        stone.slot("StoneType", "bricks.base").id


def test_sibling_names_must_be_unique(stone):
    with pytest.raises(BlueprintError, match="already has a slot"):
        stone.add_slot("StoneType", "stairs", "registry", parent="polished",
                       registry_type="minecraft:block")
    with pytest.raises(BlueprintError, match="already has a slot"):
        stone.add_slot("StoneType", "base_block", "string")


def test_nesting_is_unlimited(store):
    store.define("Deep")
    store.add_group("Deep", "a")
    store.add_group("Deep", "b", parent="a")
    store.add_group("Deep", "c", parent="a.b")
    store.add_slot("Deep", "leaf", "string", parent="a.b.c")
    assert store.slot("Deep", "a.b.c.leaf").depth == 3


def test_only_groups_can_contain_slots(stone):
    with pytest.raises(BlueprintError, match="not a group"):
        stone.add_slot("StoneType", "nope", "string", parent="base_block")


def test_renaming_a_slot_keeps_its_bindings(stone):
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "polished.stairs",
               "minecraft:polished_granite_stairs")
    stone.rename_slot("StoneType", "polished.stairs", "steps")
    assert stone.value_of("StoneType", "granite", "polished.steps") == \
        "minecraft:polished_granite_stairs"


def test_renaming_a_blueprint_keeps_everything(stone):
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite")
    stone.rename("StoneType", "Stone")
    assert stone.value_of("Stone", "granite", "base_block") == "minecraft:granite"


def test_an_unbound_slot_is_removed_silently(stone):
    stone.remove_slot("StoneType", "bricks.base")
    assert "bricks.base" not in [s.path for s in stone.slots("StoneType")]


# Removing a *bound* slot orphans the whole instance rather than deleting quietly. That
# rule and its four resolutions live in test_blueprint_evolution.py.


def test_reordering_siblings(stone):
    order = lambda: [s.path for s in stone.slots("StoneType")
                     if s.path.startswith("polished.")]
    assert order() == ["polished.base", "polished.stairs", "polished.slab",
                       "polished.wall"]
    stone.move_slot("StoneType", "polished.wall", position=0)
    assert order() == ["polished.wall", "polished.base", "polished.stairs",
                       "polished.slab"]
    stone.move_slot("StoneType", "polished.wall", position=99)      # clamps to the end
    assert order()[-1] == "polished.wall"


def test_moving_a_slot_into_another_group_keeps_its_binding(stone):
    """Bindings are keyed by slot id, so they follow the slot. The PATH changes; the data
    does not move and nothing is orphaned."""
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "polished.stairs",
               "minecraft:polished_granite_stairs")

    stone.move_slot("StoneType", "polished.stairs", parent="bricks")
    assert stone.value_of("StoneType", "granite", "bricks.stairs") == \
        "minecraft:polished_granite_stairs"
    assert stone.value_of("StoneType", "granite", "polished.stairs") is None
    assert stone.orphans() == []


def test_moving_a_slot_to_the_top_level(stone):
    stone.move_slot("StoneType", "polished.wall", parent=None)
    assert "wall" in [s.path for s in stone.slots("StoneType")]


def test_moving_a_group_carries_its_children(stone):
    stone.move_slot("StoneType", "polished", parent="bricks")
    paths = [s.path for s in stone.slots("StoneType")]
    assert "bricks.polished" in paths and "bricks.polished.stairs" in paths


def test_a_group_cannot_be_moved_inside_itself(stone):
    stone.add_group("StoneType", "deep", parent="polished")
    for target in ("polished", "polished.deep"):
        with pytest.raises(BlueprintError, match="inside itself"):
            stone.move_slot("StoneType", "polished", parent=target)


def test_a_slot_cannot_be_moved_into_a_value_slot(stone):
    with pytest.raises(BlueprintError, match="not a group"):
        stone.move_slot("StoneType", "polished.wall", parent="base_block")


def test_moving_into_a_group_that_already_has_the_name_is_refused(stone):
    with pytest.raises(BlueprintError, match="already has a slot"):
        stone.move_slot("StoneType", "polished.base", parent="bricks")  # bricks.base exists


def test_duplicating_a_group_clones_its_whole_subtree(stone):
    """A stone palette is cuts × forms — every cut wants the same set of forms, so the
    second cut should cost a duplicate rather than five dialogs."""
    stone.add_group("StoneType", "deep", parent="polished")
    stone.add_slot("StoneType", "inner", "string", parent="polished.deep")

    stone.duplicate_slot("StoneType", "polished", "tiles")
    paths = [s.path for s in stone.slots("StoneType")]
    for expected in ("tiles", "tiles.base", "tiles.stairs", "tiles.slab", "tiles.wall",
                     "tiles.deep", "tiles.deep.inner"):
        assert expected in paths
    assert stone.slot("StoneType", "tiles.base").registry_type == "minecraft:block"
    assert stone.slot("StoneType", "tiles.deep.inner").type == "string"


def test_duplicating_carries_the_full_slot_type(store):
    store.define("N")
    store.add_group("N", "grp")
    store.add_slot("N", "tier", "enum", parent="grp", enum_values=["early", "late"])
    store.duplicate_slot("N", "grp", "copy")
    assert store.slot("N", "copy.tier").enum_values == ("early", "late")


def test_duplicating_a_single_slot_works_too(stone):
    stone.duplicate_slot("StoneType", "polished.base", "base_two")
    assert stone.slot("StoneType", "polished.base_two").registry_type == "minecraft:block"


def test_a_duplicate_leaves_existing_instances_unset_not_orphaned(stone):
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite")
    stone.duplicate_slot("StoneType", "polished", "tiles")
    assert stone.orphans() == []
    assert "tiles.base" in stone.gaps("StoneType", "granite")


def test_a_group_cannot_be_duplicated_into_itself(stone):
    with pytest.raises(BlueprintError, match="into itself"):
        stone.duplicate_slot("StoneType", "polished", "inner", parent="polished")


def test_a_duplicate_name_clashes_like_any_other(stone):
    with pytest.raises(BlueprintError, match="already has a slot"):
        stone.duplicate_slot("StoneType", "polished", "bricks")


# --- typing -----------------------------------------------------------------

def test_registry_slots_are_validated_on_bind(stone):
    stone.create_instance("StoneType", "granite")
    with pytest.raises(BlueprintError, match="not in minecraft:block"):
        stone.bind("StoneType", "granite", "base_block", "minecraft:not_a_real_block")


def test_a_registry_slot_refuses_an_entry_from_the_wrong_registry(stone):
    stone.create_instance("StoneType", "granite")
    with pytest.raises(BlueprintError, match="not in minecraft:block"):
        stone.bind("StoneType", "granite", "base_block", "minecraft:stick")


def test_scalar_slots(store):
    """Slot Typing in 3.2.2 lists only registry and blueprint types, but its own retype
    rules talk about int/float/enum/string coercion — the user's call was that scalars are
    real and that section is stale."""
    store.define("Notes")
    store.add_slot("Notes", "label", "string")
    store.add_slot("Notes", "weight", "number")
    store.add_slot("Notes", "vanilla", "bool")
    store.add_slot("Notes", "tier", "enum", enum_values=["early", "late"])
    store.create_instance("Notes", "one")

    store.bind("Notes", "one", "label", "hello")
    store.bind("Notes", "one", "weight", "3")
    store.bind("Notes", "one", "vanilla", True)
    store.bind("Notes", "one", "tier", "late")

    assert store.value_of("Notes", "one", "label") == "hello"
    assert store.value_of("Notes", "one", "weight") == 3
    assert store.value_of("Notes", "one", "vanilla") is True
    assert store.value_of("Notes", "one", "tier") == "late"

    with pytest.raises(BlueprintError, match="not a number"):
        store.bind("Notes", "one", "weight", "heavy")
    with pytest.raises(BlueprintError, match="not a value of"):
        store.bind("Notes", "one", "tier", "middling")


def test_a_slot_only_keeps_the_qualifier_its_type_uses(stone):
    """A caller that fills in every field and lets the type decide is behaving reasonably.
    Storing the leftovers is not: a stray ref_blueprint on a registry slot is a REAL
    foreign key, and it later refuses to let the blueprint be deleted."""
    stone.add_slot("StoneType", "kitchen_sink", "registry",
                   registry_type="minecraft:block", ref_blueprint="StoneType",
                   enum_values=["a", "b"])
    slot = stone.slot("StoneType", "kitchen_sink")
    assert slot.registry_type == "minecraft:block"
    assert slot.ref_blueprint is None and slot.enum_values == ()
    stone.delete("StoneType")            # would raise IntegrityError with the stray ref


def test_a_self_referencing_blueprint_can_still_be_deleted(store):
    """CladeNode.parent: CladeNode is legal (3.2.2), so deleting it must work — its own
    slots go with it, even though the reference is RESTRICT for everyone else."""
    store.define("CladeNode")
    store.add_slot("CladeNode", "parent", "blueprint", ref_blueprint="CladeNode")
    store.create_instance("CladeNode", "animalia")
    store.delete("CladeNode")
    assert store.names() == []


def test_retyping_drops_the_old_types_qualifier(store):
    store.define("N")
    store.add_slot("N", "thing", "registry", registry_type="minecraft:block")
    store.retype_slot("N", "thing", "string")
    assert store.slot("N", "thing").registry_type is None


def test_a_slot_type_must_carry_what_it_needs(store):
    store.define("Bad")
    with pytest.raises(BlueprintError, match="names no registry"):
        store.add_slot("Bad", "x", "registry")
    with pytest.raises(BlueprintError, match="names no blueprint"):
        store.add_slot("Bad", "x", "blueprint")
    with pytest.raises(BlueprintError, match="declares no values"):
        store.add_slot("Bad", "x", "enum")
    with pytest.raises(BlueprintError, match="Unknown slot type"):
        store.add_slot("Bad", "x", "registry_entry")


# --- references -------------------------------------------------------------

def test_a_slot_can_reference_another_blueprints_instance(stone):
    stone.define("Village")
    stone.add_slot("Village", "stone_type", "blueprint", ref_blueprint="StoneType")
    stone.create_instance("StoneType", "granite")
    stone.create_instance("Village", "canyon")

    stone.bind("Village", "canyon", "stone_type", "granite")
    assert stone.value_of("Village", "canyon", "stone_type") == "StoneType:granite"


def test_a_reference_is_a_pointer_not_a_copy(stone):
    """3.2.2: "If the referenced instance is updated, the reference sees the change
    automatically." Storing the id is what delivers that through a rename."""
    stone.define("Village")
    stone.add_slot("Village", "stone_type", "blueprint", ref_blueprint="StoneType")
    stone.create_instance("StoneType", "granite")
    stone.create_instance("Village", "canyon")
    stone.bind("Village", "canyon", "stone_type", "granite")

    stone.rename_instance("StoneType", "granite", "pink_granite")
    assert stone.value_of("Village", "canyon", "stone_type") == "StoneType:pink_granite"


def test_a_reference_slot_refuses_the_wrong_blueprint(stone):
    stone.define("Wood")
    stone.define("Village")
    stone.add_slot("Village", "stone_type", "blueprint", ref_blueprint="StoneType")
    stone.create_instance("Wood", "oak")
    stone.create_instance("Village", "canyon")
    with pytest.raises(BlueprintError, match="takes a StoneType"):
        stone.bind("Village", "canyon", "stone_type", "Wood:oak")


def test_a_reference_needs_the_instance_to_exist(stone):
    stone.define("Village")
    stone.add_slot("Village", "stone_type", "blueprint", ref_blueprint="StoneType")
    stone.create_instance("Village", "canyon")
    with pytest.raises(BlueprintError, match="no instance 'granite'"):
        stone.bind("Village", "canyon", "stone_type", "granite")


def test_schema_level_cycles_are_allowed(store):
    """3.2.2: a CladeNode whose `descendants` slot is a CladeNode is "legitimate and
    powerful" — recursive trees like cladograms. The user's phylogeny case."""
    store.define("CladeNode")
    store.add_slot("CladeNode", "name", "string")
    store.add_slot("CladeNode", "parent", "blueprint", ref_blueprint="CladeNode")

    store.create_instance("CladeNode", "animalia")
    store.create_instance("CladeNode", "chordata")
    store.bind("CladeNode", "chordata", "parent", "animalia")
    assert store.value_of("CladeNode", "chordata", "parent") == "CladeNode:animalia"


def test_a_referenced_blueprint_cannot_be_deleted_out_from_under_a_schema(stone):
    stone.define("Village")
    stone.add_slot("Village", "stone_type", "blueprint", ref_blueprint="StoneType")
    with pytest.raises(BlueprintError, match="referenced by slots on: Village"):
        stone.delete("StoneType")


def test_a_referenced_instance_cannot_be_deleted_out_from_under_a_binding(stone):
    stone.define("Village")
    stone.add_slot("Village", "stone_type", "blueprint", ref_blueprint="StoneType")
    stone.create_instance("StoneType", "granite")
    stone.create_instance("Village", "canyon")
    stone.bind("Village", "canyon", "stone_type", "granite")
    with pytest.raises(BlueprintError, match="bound into: Village:canyon"):
        stone.delete_instance("StoneType", "granite")


# --- instances and gaps -----------------------------------------------------

def test_gaps_are_the_point(stone):
    """"Empty slots = missing content that needs to be generated or sourced." This is what
    the whole primitive is for."""
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite")
    stone.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite")
    stone.bind("StoneType", "granite", "polished.stairs",
               "minecraft:polished_granite_stairs")
    stone.bind("StoneType", "granite", "polished.slab", "minecraft:polished_granite_slab")
    stone.bind("StoneType", "granite", "bricks.base", "quark:granite_bricks")

    assert stone.gaps("StoneType", "granite") == ["polished.wall"]


def test_the_arbitrary_coverage_the_design_complains_about(stone):
    """Granite has bricks (via Quark) and andesite doesn't; neither has a wall. Seeing that
    at a glance is the entire pitch."""
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite")
    stone.bind("StoneType", "granite", "bricks.base", "quark:granite_bricks")

    stone.create_instance("StoneType", "andesite")
    stone.bind("StoneType", "andesite", "base_block", "minecraft:andesite")
    stone.bind("StoneType", "andesite", "polished.base", "minecraft:polished_andesite")

    assert "bricks.base" in stone.gaps("StoneType", "andesite")
    assert "polished.base" in stone.gaps("StoneType", "granite")


def test_one_entry_can_appear_in_many_instances(stone):
    """3.2.2: cross-reference is unrestricted and untracked."""
    stone.define("Building")
    stone.add_slot("Building", "material", "registry", registry_type="minecraft:block")
    stone.create_instance("StoneType", "granite")
    stone.create_instance("Building", "granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite")
    stone.bind("Building", "granite", "material", "minecraft:granite")
    assert stone.value_of("Building", "granite", "material") == "minecraft:granite"


def test_rebinding_replaces_rather_than_duplicates(stone):
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:andesite")
    assert stone.value_of("StoneType", "granite", "base_block") == "minecraft:andesite"
    assert len(stone.bindings("StoneType", "granite")) == 1


def test_bindings_carry_their_owner(stone):
    """Per-binding ownership (3.2.2), the column step 3 enforces against."""
    stone.create_instance("StoneType", "granite", created_by="classifier:run")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite",
               owner="action", action_ref="classifier:run")
    stone.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite")

    bindings = stone.bindings("StoneType", "granite")
    assert bindings["base_block"].owner == "action"
    assert bindings["base_block"].action_ref == "classifier:run"
    assert bindings["polished.base"].owner == "user"
    assert stone.instance("StoneType", "granite").created_by == "classifier:run"


def test_a_group_cannot_be_bound(stone):
    stone.create_instance("StoneType", "granite")
    with pytest.raises(BlueprintError, match="is a group"):
        stone.bind("StoneType", "granite", "polished", "minecraft:granite")


def test_instances_and_blueprints_are_addressable(stone):
    stone.create_instance("StoneType", "granite")
    assert stone.instance("StoneType", "granite").ref == "StoneType:granite"
    assert stone.names() == ["StoneType"]


def test_deleting_a_blueprint_takes_its_instances_and_bindings(stone):
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite")
    stone.delete("StoneType")
    assert stone.names() == []
    with pytest.raises(BlueprintError, match="No blueprint"):
        stone.instances("StoneType")


def test_names_are_labels_but_still_have_to_be_usable(store):
    store.define("Ok")
    for bad in ("", "   ", "has.dot"):
        with pytest.raises(BlueprintError):
            store.add_slot("Ok", bad, "string")
    with pytest.raises(BlueprintError, match="whitespace"):
        store.define(" padded")


def test_binding_an_instance_object_works(stone):
    stone.define("Village")
    stone.add_slot("Village", "stone_type", "blueprint", ref_blueprint="StoneType")
    granite = stone.create_instance("StoneType", "granite")
    stone.create_instance("Village", "canyon")
    stone.bind("Village", "canyon", "stone_type", granite)
    assert isinstance(granite, Instance)
    assert stone.value_of("Village", "canyon", "stone_type") == "StoneType:granite"


def test_claiming_takes_ownership_without_touching_the_value(stone):
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite",
               owner="action", action_ref="palette:fill")
    before = stone.bindings("StoneType", "granite")["base_block"]
    assert (before.owner, before.action_ref) == ("action", "palette:fill")

    claimed = stone.claim("StoneType", "granite", "base_block")
    after = stone.bindings("StoneType", "granite")["base_block"]
    assert (after.owner, after.action_ref) == ("user", None)
    assert after.value == before.value == claimed.value


def test_claiming_is_idempotent_and_needs_something_to_claim(stone):
    stone.create_instance("StoneType", "granite")
    with pytest.raises(BlueprintError, match="nothing bound"):
        stone.claim("StoneType", "granite", "base_block")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite")
    assert stone.claim("StoneType", "granite", "base_block").owner == "user"
    assert stone.claim("StoneType", "granite", "base_block").owner == "user"


def test_ownership_is_per_binding_not_per_instance(stone):
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite",
               owner="action", action_ref="palette:fill")
    stone.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite",
               owner="action", action_ref="palette:fill")
    stone.claim("StoneType", "granite", "base_block")
    owners = {p: b.owner for p, b in stone.bindings("StoneType", "granite").items()}
    assert owners == {"base_block": "user", "polished.base": "action"}


# --- instance cycles (3.2.2 Live Question, resolved: reject at bind time) --------------

@pytest.fixture
def linked(store):
    """Two schemas that can point at each other, so a cycle is constructible."""
    store.define("StoneType")
    store.define("Village")
    store.add_slot("Village", "stone", "blueprint", ref_blueprint="StoneType")
    store.add_slot("StoneType", "home", "blueprint", ref_blueprint="Village")
    store.create_instance("Village", "canyon")
    store.create_instance("StoneType", "granite")
    return store


def test_a_schema_may_reference_itself(store):
    """3.2.2: a recursive CladeNode 'is a legitimate and powerful pattern'. The SHAPE is
    just a type definition; only instance loops are refused."""
    store.define("CladeNode")
    store.add_slot("CladeNode", "parent", "blueprint", ref_blueprint="CladeNode")
    store.create_instance("CladeNode", "mammalia")
    store.create_instance("CladeNode", "primates")
    store.bind("CladeNode", "primates", "parent", "CladeNode:mammalia")
    assert store.value_of("CladeNode", "primates", "parent") == "CladeNode:mammalia"


def test_an_instance_cannot_reference_itself(store):
    store.define("CladeNode")
    store.add_slot("CladeNode", "parent", "blueprint", ref_blueprint="CladeNode")
    store.create_instance("CladeNode", "mammalia")
    with pytest.raises(BlueprintError, match="cycle"):
        store.bind("CladeNode", "mammalia", "parent", "CladeNode:mammalia")


def test_a_two_step_cycle_is_refused_and_the_path_is_named(linked):
    linked.bind("Village", "canyon", "stone", "StoneType:granite")
    with pytest.raises(BlueprintError) as caught:
        linked.bind("StoneType", "granite", "home", "Village:canyon")
    assert "StoneType:granite → Village:canyon → StoneType:granite" in str(caught.value)


def test_a_long_chain_is_fine_until_it_closes(store):
    store.define("Node")
    store.add_slot("Node", "next", "blueprint", ref_blueprint="Node")
    for name in ("a", "b", "c", "d"):
        store.create_instance("Node", name)
    store.bind("Node", "a", "next", "Node:b")
    store.bind("Node", "b", "next", "Node:c")
    store.bind("Node", "c", "next", "Node:d")
    assert store.value_of("Node", "c", "next") == "Node:d"
    with pytest.raises(BlueprintError, match="Node:d → Node:a → Node:b → Node:c → Node:d"):
        store.bind("Node", "d", "next", "Node:a")


def test_two_instances_may_point_at_the_same_target(store):
    """Sharing a target is a diamond, not a cycle, and must stay legal."""
    store.define("Node")
    store.add_slot("Node", "next", "blueprint", ref_blueprint="Node")
    for name in ("a", "b", "shared"):
        store.create_instance("Node", name)
    store.bind("Node", "a", "next", "Node:shared")
    store.bind("Node", "b", "next", "Node:shared")
    assert store.cycles() == []


def test_cycles_reports_what_bind_time_refusal_kept_out(linked):
    assert linked.cycles() == []


# --- binding orphans: derived, surfaced, never locking ---------------------------------

class ShrinkingDump:
    """A packdump you can shrink, for "the entry left" cases. Distinct from the module's
    FakeDump, which is the fixed registry every other test binds against."""

    def __init__(self, **registries):
        self.registry = {k.replace("_", ":", 1): {"values": set(v)}
                         for k, v in registries.items()}


def test_a_binding_whose_entry_left_the_packdump_is_an_orphan(stone):
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite")
    dump = ShrinkingDump(minecraft_block=["minecraft:granite"])
    assert stone.find_binding_orphans(dump) == []

    gone = ShrinkingDump(minecraft_block=["minecraft:stone"])
    orphans = stone.find_binding_orphans(gone)
    assert len(orphans) == 1
    assert orphans[0].reason == "missing_entry"
    assert orphans[0].detail == "'minecraft:granite' is no longer in minecraft:block"


def test_binding_orphans_are_derived_not_stored(stone):
    """Mirrors tags: 'derived, never stored, so it can't go stale'. The instance is NOT
    orphaned and stays usable — a vanished entry is the packdump's doing, not a schema
    decision the user made."""
    stone.create_instance("StoneType", "granite")
    stone.bind("StoneType", "granite", "base_block", "minecraft:granite")
    assert stone.find_binding_orphans(ShrinkingDump(minecraft_block=[]))
    assert stone.orphans() == []                       # instance is not locked
    assert not stone.has_orphans("StoneType")
    # and it goes away by itself when the entry comes back
    assert stone.find_binding_orphans(
        ShrinkingDump(minecraft_block=["minecraft:granite"])) == []


def test_a_live_reference_is_never_reported_as_an_orphan(linked):
    linked.bind("Village", "canyon", "stone", "StoneType:granite")
    assert linked.find_binding_orphans(ShrinkingDump()) == []


def test_a_referenced_instance_cannot_be_deleted_out_from_under_the_pointer(linked):
    """Why `missing_instance` is a safety net rather than a normal state: the store
    refuses the delete that would create one. The check stays for a hand-edited database,
    but the invariant is enforced at the edge."""
    linked.bind("Village", "canyon", "stone", "StoneType:granite")
    with pytest.raises(BlueprintError, match="bound into: Village:canyon"):
        linked.delete_instance("StoneType", "granite")
    linked.unbind("Village", "canyon", "stone")
    linked.delete_instance("StoneType", "granite")       # now it's free to go
