"""Blueprint schema evolution and orphan resolution (design 3.2.2).

One principle governs all four mutations: *non-destructive changes propagate silently;
destructive changes orphan at the instance level and refuse to participate in actions until
the user explicitly resolves their intent.*

Add and rename are non-destructive. Remove and retype orphan the **whole instance**, not
merely the affected binding — schema destruction is significant enough that the user is
made to confirm intent before anything downstream proceeds.
"""
import pytest

from packsmith.core.blueprints import BlueprintStore, BlueprintError
from tests.test_blueprints import FakeDump


@pytest.fixture
def store(user_db):
    return BlueprintStore(user_db, packdump=FakeDump())


@pytest.fixture
def stone(store):
    store.define("StoneType")
    store.add_slot("StoneType", "base_block", "registry", registry_type="minecraft:block")
    store.add_group("StoneType", "polished")
    for form in ("base", "stairs", "slab", "wall"):
        store.add_slot("StoneType", form, "registry", parent="polished",
                       registry_type="minecraft:block")
    store.add_group("StoneType", "bricks")
    store.add_slot("StoneType", "base", "registry", parent="bricks",
                   registry_type="minecraft:block")
    store.create_instance("StoneType", "granite")
    store.bind("StoneType", "granite", "base_block", "minecraft:granite")
    store.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite")
    return store


# --- add: silent propagation ------------------------------------------------

def test_adding_a_slot_propagates_silently(stone):
    impact = stone.preview_add_slot("StoneType", "vertical_slab")
    assert impact.instances == 1 and not impact.destructive

    stone.add_slot("StoneType", "vertical_slab", "registry", parent="polished",
                   registry_type="minecraft:block")
    # Existing instances gain it unset; actions see None.
    assert stone.value_of("StoneType", "granite", "polished.vertical_slab") is None
    assert "polished.vertical_slab" in stone.gaps("StoneType", "granite")
    assert stone.orphans() == []


# --- rename: metadata only --------------------------------------------------

def test_renaming_a_slot_migrates_bindings_without_orphaning(stone):
    impact = stone.preview_rename_slot("StoneType", "polished.base")
    assert impact.bound == ("granite",) and not impact.destructive

    stone.rename_slot("StoneType", "polished.base", "block")
    assert stone.value_of("StoneType", "granite", "polished.block") == \
        "minecraft:polished_granite"
    assert stone.orphans() == []


# --- remove -----------------------------------------------------------------

def test_removing_an_unbound_slot_stays_silent(stone):
    assert not stone.preview_remove_slot("StoneType", "polished.wall").destructive
    stone.remove_slot("StoneType", "polished.wall")
    assert stone.orphans() == []


def test_removing_a_bound_slot_orphans_the_whole_instance(stone):
    impact = stone.remove_slot("StoneType", "polished.base")
    assert impact.destructive and impact.orphans == ("granite",)

    orphan = stone.orphans("StoneType")[0]
    assert orphan.instance == "granite" and orphan.reason == "remove_slot"
    assert stone.has_orphans("StoneType")
    # Orphaned entire: its other bindings survive, but the instance is locked out until
    # the user says what they meant.
    assert stone.value_of("StoneType", "granite", "base_block") == "minecraft:granite"


def test_removing_a_group_takes_its_children(stone):
    stone.remove_slot("StoneType", "polished")
    paths = [s.path for s in stone.slots("StoneType")]
    assert not [p for p in paths if p.startswith("polished")]
    assert stone.orphans("StoneType")[0].instance == "granite"


def test_only_instances_that_had_a_binding_are_orphaned(stone):
    stone.create_instance("StoneType", "andesite")
    stone.bind("StoneType", "andesite", "base_block", "minecraft:andesite")
    stone.remove_slot("StoneType", "polished.base")
    assert [o.instance for o in stone.orphans("StoneType")] == ["granite"]


def test_removal_orphans_cannot_be_rebound(stone):
    """Nothing can conjure back a slot that no longer exists, so "Change Problematic
    Values" doesn't apply — the same shape as a tag orphan whose entry is gone."""
    stone.remove_slot("StoneType", "polished.base")
    assert "rebind" not in stone.orphans("StoneType")[0].resolutions


# --- retype -----------------------------------------------------------------

def test_a_lossless_retype_can_auto_coerce(store):
    """3.2.2's own example: enum -> string. Every value survives, so orphaning would be
    "pure theater"."""
    store.define("N")
    store.add_slot("N", "tier", "enum", enum_values=["early", "late"])
    store.create_instance("N", "one")
    store.bind("N", "one", "tier", "late")

    impact = store.preview_retype_slot("N", "tier", "string")
    assert impact.can_coerce and impact.problematic == ()

    store.retype_slot("N", "tier", "string", auto_coerce=True)
    assert store.value_of("N", "one", "tier") == "late"
    assert store.orphans() == []


def test_string_to_enum_is_lossless_only_when_every_value_fits(store):
    """The design's other example: "string -> enum where all values are in the new enum's
    set"."""
    store.define("N")
    store.add_slot("N", "tier", "string")
    store.create_instance("N", "ok")
    store.bind("N", "ok", "tier", "early")
    assert store.preview_retype_slot("N", "tier", "enum",
                                     enum_values=["early", "late"]).can_coerce

    store.create_instance("N", "bad")
    store.bind("N", "bad", "tier", "sideways")
    impact = store.preview_retype_slot("N", "tier", "enum", enum_values=["early", "late"])
    assert not impact.can_coerce
    assert impact.problematic == (("bad", "sideways"),)


def test_auto_coerce_is_refused_when_it_would_lose_data(store):
    store.define("N")
    store.add_slot("N", "amount", "string")
    store.create_instance("N", "one")
    store.bind("N", "one", "amount", "heaps")
    with pytest.raises(BlueprintError, match="without loss"):
        store.retype_slot("N", "amount", "number", auto_coerce=True)


def test_retype_orphans_by_default_even_when_coercible(store):
    """"Retype & Orphan" stays on the menu for a lossless change — an explicit choice to
    re-bind by hand. Default and strictness are preserved."""
    store.define("N")
    store.add_slot("N", "tier", "enum", enum_values=["early", "late"])
    store.create_instance("N", "one")
    store.bind("N", "one", "tier", "late")

    store.retype_slot("N", "tier", "string")        # auto_coerce not requested
    assert [o.instance for o in store.orphans("N")] == ["one"]


def test_a_problematic_value_is_displaced_and_rebindable(store):
    store.define("N")
    store.add_slot("N", "amount", "string")
    store.create_instance("N", "one")
    store.bind("N", "one", "amount", "heaps")

    store.retype_slot("N", "amount", "number")
    orphan = store.orphans("N")[0]
    assert orphan.reason == "retype_slot"
    assert orphan.problematic == ("amount",)
    assert "rebind" in orphan.resolutions
    assert store.value_of("N", "one", "amount") is None

    store.rebind("N", "one", "amount", 12)
    assert store.value_of("N", "one", "amount") == 12
    assert store.orphans("N") == []                  # un-orphans automatically


def test_a_surviving_value_is_re_encoded_not_displaced(store):
    """Stored form is type-specific — 3 as a number is "3.0", as a string it's "3".

    Exercised through auto-coerce, which is the path that converts. (It used to be
    reachable by the default path too, but that path is "Retype & Orphan", which 3.2.2
    defines as the user declining conversion — see the test below.)
    """
    store.define("N")
    store.add_slot("N", "amount", "string")
    store.create_instance("N", "one")
    store.bind("N", "one", "amount", "3")
    store.retype_slot("N", "amount", "number", auto_coerce=True)
    assert store.value_of("N", "one", "amount") == 3


def test_retype_and_orphan_does_not_convert_what_it_orphans(store):
    """3.2.2: "*Retype & Orphan* — apply the type change, orphan all instances anyway
    (explicit user choice to re-bind manually)."

    Converting the survivors anyway contradicts both the choice and the label: the user is
    told to re-bind, there is nothing left to re-bind, and the originals are gone. The
    values go to limbo instead, where Discard / Preserve / re-bind can reach them.
    """
    store.define("N")
    store.add_slot("N", "amount", "number")
    store.create_instance("N", "one")
    store.bind("N", "one", "amount", 3)

    impact = store.preview_retype_slot("N", "amount", "string")
    assert impact.can_coerce, "everything COULD convert — the user chose not to"

    store.retype_slot("N", "amount", "string", auto_coerce=False)
    assert store.value_of("N", "one", "amount") is None, "it was converted anyway"
    assert [l.value for l in store.limbo("N", "one")] == ["3.0"], "the original is kept"
    assert [o.instance for o in store.orphans("N")] == ["one"]


def test_retyping_between_registries_is_checked_against_the_packdump(store):
    store.define("N")
    store.add_slot("N", "thing", "registry", registry_type="minecraft:block")
    store.create_instance("N", "one")
    store.bind("N", "one", "thing", "minecraft:granite")
    impact = store.preview_retype_slot("N", "thing", "registry",
                                       registry_type="minecraft:item")
    assert not impact.can_coerce            # granite is a block, not an item


# --- the four resolutions ---------------------------------------------------

def test_discard_deletes_the_orphaned_bindings(stone):
    stone.remove_slot("StoneType", "polished.base")
    stone.discard("StoneType", "granite")
    assert stone.orphans() == []
    assert stone.limbo("StoneType", "granite") == []


def test_preserve_keeps_the_data_but_hides_it(stone):
    """"Retained in the database, useful if the slot might return" — but never exposed to
    actions, which is why limbo is its own table rather than a flag on the binding."""
    stone.remove_slot("StoneType", "polished.base")
    stone.preserve("StoneType", "granite")

    assert stone.orphans() == []
    kept = stone.limbo("StoneType", "granite")
    assert [b.slot_path for b in kept] == ["polished.base"]
    assert kept[0].value == "minecraft:polished_granite"
    assert "polished.base" not in stone.bindings("StoneType", "granite")


def test_revert_snaps_the_whole_blueprint_back(stone):
    stone.create_instance("StoneType", "andesite")
    stone.bind("StoneType", "andesite", "polished.base", "minecraft:polished_andesite")
    stone.remove_slot("StoneType", "polished.base")
    assert len(stone.orphans("StoneType")) == 2

    stone.revert("StoneType")
    assert stone.orphans() == []
    assert stone.value_of("StoneType", "granite", "polished.base") == \
        "minecraft:polished_granite"
    assert stone.value_of("StoneType", "andesite", "polished.base") == \
        "minecraft:polished_andesite"


def test_revert_restores_a_whole_removed_group(stone):
    stone.bind("StoneType", "granite", "bricks.base", "quark:granite_bricks")
    stone.remove_slot("StoneType", "polished")

    stone.revert("StoneType")
    paths = [s.path for s in stone.slots("StoneType")]
    assert "polished" in paths and "polished.stairs" in paths
    assert stone.value_of("StoneType", "granite", "polished.base") == \
        "minecraft:polished_granite"


def test_revert_undoes_a_retype(store):
    store.define("N")
    store.add_slot("N", "amount", "string")
    store.create_instance("N", "one")
    store.bind("N", "one", "amount", "heaps")

    store.retype_slot("N", "amount", "number")
    store.revert("N")
    assert store.slot("N", "amount").type == "string"
    assert store.value_of("N", "one", "amount") == "heaps"
    assert store.orphans() == []


def test_revert_is_only_one_step_back(stone):
    """The user's ruling. A second destructive change while orphans are outstanding is
    refused — "snap back to the pre-mutation state" stops meaning anything once two are
    stacked."""
    stone.remove_slot("StoneType", "polished.base")
    with pytest.raises(BlueprintError, match="unresolved orphaned instances"):
        stone.remove_slot("StoneType", "base_block")

    stone.discard("StoneType", "granite")
    stone.remove_slot("StoneType", "base_block")     # allowed once resolved


def test_a_resolved_mutation_stops_being_revertible(stone):
    stone.remove_slot("StoneType", "polished.base")
    stone.discard("StoneType", "granite")
    with pytest.raises(BlueprintError, match="no schema change to revert"):
        stone.revert("StoneType")


def test_a_retype_is_blocked_while_orphans_are_outstanding(stone):
    stone.remove_slot("StoneType", "polished.base")
    with pytest.raises(BlueprintError, match="unresolved orphaned instances"):
        stone.retype_slot("StoneType", "base_block", "string")


# --- surfacing --------------------------------------------------------------

def test_orphans_are_visible_across_all_blueprints(stone):
    """The Errors panel wants one list, the way tag orphans already work."""
    stone.define("Other")
    stone.add_slot("Other", "x", "string")
    stone.create_instance("Other", "thing")
    stone.bind("Other", "thing", "x", "hi")

    stone.remove_slot("StoneType", "polished.base")
    stone.remove_slot("Other", "x")
    assert {o.blueprint for o in stone.orphans()} == {"StoneType", "Other"}
    assert stone.has_orphans("StoneType") and stone.has_orphans("Other")


def test_resolutions_can_be_mixed_across_instances(stone):
    stone.create_instance("StoneType", "andesite")
    stone.bind("StoneType", "andesite", "polished.base", "minecraft:polished_andesite")
    stone.remove_slot("StoneType", "polished.base")

    stone.discard("StoneType", "granite")
    stone.revert("StoneType")           # andesite still orphaned, so revert is available
    assert stone.orphans() == []
    assert stone.value_of("StoneType", "andesite", "polished.base") == \
        "minecraft:polished_andesite"
