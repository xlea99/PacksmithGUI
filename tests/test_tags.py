"""Core tag behaviour: definitions, assignment, retrieval, casting, cascades."""
import json

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


# --- identity: the id is the tag, the name is a label (design 3.2.1) -------

def test_definition_carries_a_stable_id(tags):
    tag_id = tags.define(REG, "tier", "enum", ["early", "mid"])
    assert tags.definition(REG, "tier")["id"] == tag_id


def test_same_name_on_two_registries_gets_distinct_ids(tags):
    item_id = tags.define(REG, "tier", "enum", ["early"])
    block_id = tags.define("minecraft:block", "tier", "number")
    assert item_id != block_id


def test_assignments_follow_the_id_not_the_name(tags, user_db):
    """The payoff of id-identity: renaming the definition moves no assignment rows.
    (The rename *ceremony* isn't built yet, so this writes the label directly — the
    point is that the data doesn't care.)"""
    tags.define(REG, "remove", "bool", default=False)
    tags.assign(REG, "quark:rope", "remove", True, owner="user")

    user_db.execute("UPDATE tag_definitions SET name = 'hide' WHERE registry_type = ? AND name = ?",
                    (REG, "remove"))
    tags._build_definitions()

    assert tags.get_tag(REG, "quark:rope", "hide") is True
    assert tags.get_ownership(REG, "quark:rope", "hide") == {"kind": "user", "action_ref": None}
    assert tags.definition(REG, "remove") is None


# --- orphan detection: two causes, one state (design 3.2.1) ----------------

class _Dump:
    def __init__(self, entries):
        self.registry = {REG: {"values": list(entries)}}


def test_orphan_when_entry_left_the_packdump(tags):
    tags.define(REG, "remove", "bool")
    tags.assign(REG, "quark:rope", "remove", True)
    tags.assign(REG, "minecraft:diamond", "remove", True)
    orphans = tags.orphaned_tags(_Dump(["minecraft:diamond"]))
    assert orphans == {REG: ["quark:rope"]}


def test_orphan_when_enum_value_left_the_definition(tags):
    """The second cause: the entry still exists, but its value no longer does."""
    tags.define(REG, "tier", "enum", ["early", "mid", "late"])
    tags.assign(REG, "quark:rope", "tier", "late")
    tags.assign(REG, "minecraft:diamond", "tier", "mid")
    dump = _Dump(["quark:rope", "minecraft:diamond"])
    assert tags.orphaned_tags(dump) == {}          # nothing wrong yet

    tags.set_enum_values(REG, "tier", ["early", "mid"])

    assert tags.orphaned_tags(dump) == {REG: ["quark:rope"]}


def test_orphans_carry_their_reason(tags):
    tags.define(REG, "tier", "enum", ["early", "late"])
    tags.assign(REG, "quark:rope", "tier", "late")      # will go stale
    tags.assign(REG, "gone:item", "tier", "early")      # entry not in the dump
    tags.set_enum_values(REG, "tier", ["early"])

    orphans = {o.entry_id: o for o in tags.find_orphans(_Dump(["quark:rope"]))}
    assert orphans["quark:rope"].reason == "stale_value"
    assert orphans["quark:rope"].value == "late"
    assert orphans["gone:item"].reason == "missing_entry"


# --- enum value evolution (design 3.2.1) -----------------------------------

def test_preview_reports_the_blast_radius_without_changing_anything(tags):
    tags.define(REG, "tier", "enum", ["early", "mid", "late"], default="late")
    tags.assign(REG, "a", "tier", "late")
    tags.assign(REG, "b", "tier", "late")
    tags.assign(REG, "c", "tier", "mid")

    preview = tags.preview_enum_change(REG, "tier", ["early", "mid", "end"])
    assert preview["removed"] == ["late"]
    assert preview["added"] == ["end"]
    assert preview["orphan_count"] == 2
    assert preview["orphaned"] == {"late": ["a", "b"]}
    assert preview["default_cleared"] is True
    # ...and nothing actually moved
    assert tags.definition(REG, "tier")["values"] == ["early", "mid", "late"]
    assert tags.get_tag(REG, "a", "tier") == "late"


def test_adding_a_value_is_free(tags):
    tags.define(REG, "tier", "enum", ["early", "late"])
    tags.assign(REG, "a", "tier", "late")
    tags.set_enum_values(REG, "tier", ["early", "late", "end"])
    assert tags.definition(REG, "tier")["values"] == ["early", "late", "end"]
    assert tags.orphaned_tags(_Dump(["a"])) == {}     # nothing disturbed


def test_reordering_preserves_the_new_order(tags):
    tags.define(REG, "tier", "enum", ["early", "mid", "late"])
    tags.set_enum_values(REG, "tier", ["late", "mid", "early"])
    assert tags.definition(REG, "tier")["values"] == ["late", "mid", "early"]


def test_removing_a_value_orphans_rather_than_deletes(tags):
    """The assignment survives — deliberately. The user resolves it, not the system."""
    tags.define(REG, "tier", "enum", ["early", "late"])
    tags.assign(REG, "a", "tier", "late", owner="user")
    tags.set_enum_values(REG, "tier", ["early"])
    assert tags.get_tag(REG, "a", "tier") == "late"                 # data still there
    assert tags.get_ownership(REG, "a", "tier") is not None
    assert len(tags.find_orphans(_Dump(["a"]))) == 1


def test_removing_the_default_value_clears_the_default(tags):
    tags.define(REG, "tier", "enum", ["early", "late"], default="late")
    tags.set_enum_values(REG, "tier", ["early"])
    assert tags.definition(REG, "tier")["default_value"] is None
    assert tags.default_for(REG, "tier") is None


def test_enum_values_cannot_be_emptied(tags):
    tags.define(REG, "tier", "enum", ["early"])
    with pytest.raises(ValueError):
        tags.set_enum_values(REG, "tier", [])


def test_set_enum_values_rejects_non_enum_tags(tags):
    tags.define(REG, "notes", "string")
    with pytest.raises(ValueError):
        tags.set_enum_values(REG, "notes", ["a", "b"])


# --- orphan resolutions ----------------------------------------------------

def test_resolution_clear_deletes_the_orphaned_assignments(tags):
    tags.define(REG, "tier", "enum", ["early", "late"])
    tags.assign(REG, ["a", "b"], "tier", "late")
    tags.assign(REG, "c", "tier", "early")
    tags.set_enum_values(REG, "tier", ["early"])

    assert tags.clear_value(REG, "tier", "late") == 2
    assert tags.get_ownership(REG, "a", "tier") is None
    assert tags.get_tag(REG, "c", "tier") == "early"       # untouched
    assert tags.find_orphans(_Dump(["a", "b", "c"])) == []


def test_resolution_reassign_moves_them_to_a_valid_value(tags):
    tags.define(REG, "tier", "enum", ["early", "late"])
    tags.assign(REG, ["a", "b"], "tier", "late", owner="action", owner_action_ref="x:y")
    tags.set_enum_values(REG, "tier", ["early"])

    assert tags.reassign_value(REG, "tier", "late", "early") == 2
    assert tags.get_tag(REG, "a", "tier") == "early"
    # ownership is preserved — reassigning resolves a problem, it doesn't claim the cell
    assert tags.get_ownership(REG, "a", "tier") == {"kind": "action", "action_ref": "x:y"}
    assert tags.find_orphans(_Dump(["a", "b"])) == []


def test_resolution_reassign_rejects_an_invalid_target(tags):
    tags.define(REG, "tier", "enum", ["early", "late"])
    tags.assign(REG, "a", "tier", "late")
    tags.set_enum_values(REG, "tier", ["early"])
    with pytest.raises(ValueError):
        tags.reassign_value(REG, "tier", "late", "nonsense")


def test_resolution_restore_un_orphans_everything(tags):
    """'Restore' is just putting the value back — the assignments were never destroyed."""
    tags.define(REG, "tier", "enum", ["early", "late"])
    tags.assign(REG, "a", "tier", "late")
    tags.set_enum_values(REG, "tier", ["early"])
    assert len(tags.find_orphans(_Dump(["a"]))) == 1

    tags.set_enum_values(REG, "tier", ["early", "late"])
    assert tags.find_orphans(_Dump(["a"])) == []
    assert tags.get_tag(REG, "a", "tier") == "late"


def test_same_name_different_registry_is_not_a_duplicate(tags):
    """Defining a name on a second registry is NOT the 'already exists' error —
    the composite (registry_type, name) key makes them distinct rows."""
    BLOCK = "minecraft:block"
    tags.define(REG, "weight", "number")
    tags.define(BLOCK, "weight", "number")   # must not raise
    with pytest.raises(ValueError):
        tags.define(REG, "weight", "number")  # THIS is the real duplicate


# --- orphan detection runs on everything, so it has to be cheap -----------------------
#
# `find_orphans` walks every assignment in the profile and is called whenever anything
# might have changed — including a blueprint edit, which cannot create a tag orphan at all.
# On a real pack (2,198 assignments, 18,639 items) it took 131 ms, most of the pause after
# committing a blueprint cell. Two causes, both the same shape as the linear scan once
# fixed in `_Registry.has`: `registry["values"]` is a LIST, and `definition()` was queried
# once per row for one of five definitions.

class _BigDump:
    def __init__(self, entries):
        self.registry = {"minecraft:item": {"values": entries}}

    def attribute(self, *_a):
        return None


class _CountingValues(list):
    """A registry's value list that records every membership test made against it."""

    def __init__(self, values):
        super().__init__(values)
        self.contains_calls = 0

    def __contains__(self, item):
        self.contains_calls += 1
        return super().__contains__(item)


def test_orphan_detection_does_not_rescan_the_registry_per_assignment(tags):
    """Counted rather than timed. A stopwatch here is both flaky and weak — a list scan of
    50,000 entries still finishes fast enough to slip under any budget loose enough to be
    reliable, which is exactly what the first version of this test did. What matters is the
    SHAPE: the dump's list must be read into a set once, never asked `in` per assignment.
    """
    values = _CountingValues(f"mod:item_{i}" for i in range(2_000))
    tags.define("minecraft:item", "remove", "bool")
    with tags._db.transaction():
        tags.assign("minecraft:item", list(values)[:500], "remove", True, owner="user")

    assert tags.find_orphans(_BigDump(values)) == []
    assert values.contains_calls == 0, (
        f"the dump's list was searched {values.contains_calls} times — membership belongs "
        f"to a set built once, not a scan per assignment")


def test_orphan_detection_reads_each_definition_once(tags):
    """The other half. `definition()` is a database round trip, and there are five of them
    in a profile — not one per assignment."""
    entries = [f"mod:item_{i}" for i in range(200)]
    tags.define("minecraft:item", "remove", "bool")
    with tags._db.transaction():
        tags.assign("minecraft:item", entries, "remove", True, owner="user")

    calls = []
    original = tags.definition
    tags.definition = lambda *a, **k: (calls.append(a), original(*a, **k))[1]
    try:
        tags.find_orphans(_BigDump(entries))
    finally:
        tags.definition = original

    assert len(calls) <= 2, f"one lookup per assignment is back ({len(calls)} calls)"


def test_a_missing_entry_is_still_reported(tags):
    """The guard on the optimisation: making it fast must not make it blind."""
    tags.define("minecraft:item", "remove", "bool")
    tags.assign("minecraft:item", "mod:gone", "remove", True, owner="user")

    found = tags.find_orphans(_BigDump(["mod:still_here"]))

    assert [o.entry_id for o in found] == ["mod:gone"]
    assert found[0].reason == "missing_entry"
