"""Core tag behaviour: definitions, assignment, retrieval, casting, cascades."""
import pytest

REG = "minecraft:item"


def test_define_and_list(tags):
    tags.define(REG, "tier", "enum", ["early", "mid", "late"])
    tags.define(REG, "banned", "bool")
    assert set(tags.definitions_for(REG).keys()) == {"tier", "banned"}


def test_define_duplicate_raises(tags):
    tags.define(REG, "banned", "bool")
    with pytest.raises(ValueError):
        tags.define(REG, "banned", "bool")


def test_enum_requires_values(tags):
    with pytest.raises(ValueError):
        tags.define(REG, "tier", "enum")


def test_enum_values_on_non_enum_raises(tags):
    with pytest.raises(ValueError):
        tags.define(REG, "banned", "bool", enum_values=["a", "b"])


def test_assign_and_get(tags):
    tags.define(REG, "tier", "enum", ["early", "mid", "late"])
    tags.assign(REG, "minecraft:diamond", "tier", "mid")
    assert tags.get_tag(REG, "minecraft:diamond", "tier") == "mid"


def test_get_unset_without_default_is_none(tags):
    tags.define(REG, "notes", "string")
    assert tags.get_tag(REG, "minecraft:diamond", "notes") is None


def test_default_returned_when_unset(tags):
    tags.define(REG, "weight", "number", default=1)
    assert tags.get_tag(REG, "minecraft:diamond", "weight") == 1


def test_number_casts_to_int_and_float(tags):
    tags.define(REG, "weight", "number")
    tags.assign(REG, "a", "weight", 5)
    tags.assign(REG, "b", "weight", 2.5)
    val_a = tags.get_tag(REG, "a", "weight")
    assert val_a == 5 and isinstance(val_a, int)
    assert tags.get_tag(REG, "b", "weight") == 2.5


def test_bool_casts_back_to_bool(tags):
    tags.define(REG, "banned", "bool")
    tags.assign(REG, "a", "banned", True)
    assert tags.get_tag(REG, "a", "banned") is True


def test_assign_validates_value_type(tags):
    tags.define(REG, "banned", "bool")
    with pytest.raises(ValueError):
        tags.assign(REG, "a", "banned", "not a bool")


def test_assign_undefined_tag_raises(tags):
    with pytest.raises(ValueError):
        tags.assign(REG, "a", "ghost", True)


def test_enum_rejects_out_of_range_value(tags):
    tags.define(REG, "tier", "enum", ["early", "mid"])
    with pytest.raises(ValueError):
        tags.assign(REG, "a", "tier", "cosmic")


def test_bulk_assign(tags):
    tags.define(REG, "tier", "enum", ["early", "mid"])
    tags.assign(REG, ["a", "b", "c"], "tier", "early")
    for e in ["a", "b", "c"]:
        assert tags.get_tag(REG, e, "tier") == "early"


def test_undefine_cascades_to_assignments(tags):
    tags.define(REG, "tier", "enum", ["early"])
    tags.assign(REG, "a", "tier", "early")
    tags.undefine(REG, "tier")
    assert "tier" not in tags.definitions_for(REG)
    assert tags.get_tag(REG, "a", "tier") is None  # FK cascade removed the row


def test_get_all_tags(tags):
    tags.define(REG, "tier", "enum", ["mid"])
    tags.define(REG, "banned", "bool")
    tags.assign(REG, "a", "tier", "mid")
    tags.assign(REG, "a", "banned", True)
    assert tags.get_all_tags(REG, "a") == {"tier": "mid", "banned": True}


# --- registry scoping (design 3.2.1) ---------------------------------------

def test_same_name_tag_is_independent_across_registries(tags):
    """A tag name is a wholly separate definition per registry: different type,
    enum values, and default. The load-bearing invariant behind the query engine's
    per-registry field catalog."""
    BLOCK = "minecraft:block"
    tags.define(REG, "tier", "enum", ["early", "mid", "late"])   # item tier: an enum
    tags.define(BLOCK, "tier", "number", default=0)              # block tier: a number, same name

    assert tags.definition(REG, "tier")["type"] == "enum"
    assert tags.definition(BLOCK, "tier")["type"] == "number"

    # Assignments don't leak across the boundary
    tags.assign(REG, "minecraft:diamond", "tier", "mid")
    tags.assign(BLOCK, "minecraft:stone", "tier", 3)
    assert tags.get_tag(REG, "minecraft:diamond", "tier") == "mid"
    assert tags.get_tag(BLOCK, "minecraft:stone", "tier") == 3
    # The block default applies on the block side only
    assert tags.get_tag(BLOCK, "minecraft:dirt", "tier") == 0
    assert tags.get_tag(REG, "minecraft:diamond_ore", "tier") is None


def test_undefine_is_registry_scoped(tags):
    """Undefining a tag on one registry leaves the same-named tag on another intact."""
    BLOCK = "minecraft:block"
    tags.define(REG, "hidden", "bool")
    tags.define(BLOCK, "hidden", "bool")
    tags.undefine(REG, "hidden")
    assert tags.definition(REG, "hidden") is None
    assert tags.definition(BLOCK, "hidden") is not None


def test_same_name_different_registry_is_not_a_duplicate(tags):
    """Defining a name on a second registry is NOT the 'already exists' error —
    the composite (registry_type, name) key makes them distinct rows."""
    BLOCK = "minecraft:block"
    tags.define(REG, "weight", "number")
    tags.define(BLOCK, "weight", "number")   # must not raise
    with pytest.raises(ValueError):
        tags.define(REG, "weight", "number")  # THIS is the real duplicate
