"""Blueprint ownership and action access (design 3.2.2, 3.3).

Three rules are under test, and they are the ones that keep the primitive honest:

* **Schemas are user-only.** Not by a guard — by absence. Starlark reaches nothing the
  host doesn't inject, so leaving schema methods off ``pack.blueprints`` *is* the
  enforcement.
* **Ownership is per binding**, so writing a slot someone else owns is resolved by the
  action's declared conflict policy — "identical to tag assignment conflicts", which is
  why it's the same code path with a different key.
* **A blueprint with orphaned instances refuses to be touched at all**, because the
  instance "does not silently degrade" — an action reading it would be reasoning about a
  shape that no longer describes the data.
"""
import pytest

from packsmith.core.bindings import policy_key
from packsmith.core.blueprints import BlueprintStore
from packsmith.core.pack import ActionFailure, Pack
from packsmith.core.runner import run_action
from packsmith.core.staging import BlueprintStaging, L2Staging
from packsmith.core.starlark_runtime import run_starlark
from tests.test_blueprints import FakeDump


@pytest.fixture
def blueprints(user_db):
    store = BlueprintStore(user_db, packdump=FakeDump())
    store.define("StoneType")
    store.add_slot("StoneType", "base_block", "registry", registry_type="minecraft:block")
    store.add_group("StoneType", "polished")
    for form in ("base", "stairs"):
        store.add_slot("StoneType", form, "registry", parent="polished",
                       registry_type="minecraft:block")
    store.create_instance("StoneType", "granite")
    return store


def make_pack(tags, blueprints, action_ref="pkg:act", policies=None):
    return Pack(staging=L2Staging(tags), tag_store=tags, packdump=FakeDump(),
                action_ref=action_ref, conflict_policies=policies,
                blueprint_staging=BlueprintStaging(blueprints),
                blueprint_store=blueprints)


def run(action_fn, tags, blueprints, **kw):
    return run_action(action_fn, tag_store=tags, packdump=FakeDump(),
                      action_ref=kw.pop("action_ref", "pkg:act"),
                      blueprint_store=blueprints, **kw)


# --- schemas are user-only --------------------------------------------------

def test_the_capability_exposes_no_way_to_change_a_schema(tags, blueprints):
    """3.2.2: "Actions cannot create, modify, rename, retype, or delete schemas." The
    enforcement is that these names do not exist on the object."""
    pack = make_pack(tags, blueprints)
    for forbidden in ("define", "add_slot", "add_group", "rename_slot", "remove_slot",
                      "retype_slot", "delete", "rename"):
        assert not hasattr(pack.blueprints, forbidden), forbidden


def test_starlark_cannot_reach_the_schema_either(tags, blueprints):
    pack = make_pack(tags, blueprints)
    src = """
def run(pack):
    return pack.blueprints.define("Sneaky")
"""
    with pytest.raises(Exception):
        run_starlark(src, pack)
    assert blueprints.names() == ["StoneType"]


# --- reads and writes -------------------------------------------------------

def test_an_action_reads_the_shape_and_the_gaps(tags, blueprints):
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite")
    pack = make_pack(tags, blueprints)
    assert pack.blueprints.slots("StoneType") == [
        "base_block", "polished.base", "polished.stairs"]
    assert pack.blueprints.gaps("StoneType", "granite") == [
        "polished.base", "polished.stairs"]


def test_an_action_binding_stamps_itself_as_owner(tags, blueprints):
    def action(pack):
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite")

    assert run(action, tags, blueprints).ok
    binding = blueprints.bindings("StoneType", "granite")["base_block"]
    assert binding.owner == "action" and binding.action_ref == "pkg:act"


def test_an_action_can_create_an_instance_and_fill_it(tags, blueprints):
    def action(pack):
        pack.blueprints.create("StoneType", "andesite")
        pack.blueprints.bind("StoneType", "andesite", "base_block", "minecraft:andesite")

    assert run(action, tags, blueprints).ok
    assert blueprints.instance("StoneType", "andesite").created_by == "pkg:act"
    assert blueprints.value_of("StoneType", "andesite", "base_block") == "minecraft:andesite"


def test_reads_see_the_steps_own_pending_writes(tags, blueprints):
    """Read-your-writes, same as tags. An action that fills a gap should see it close."""
    seen = {}

    def action(pack):
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite")
        seen["value"] = pack.blueprints.get("StoneType", "granite", "base_block")
        seen["gaps"] = pack.blueprints.gaps("StoneType", "granite")

    run(action, tags, blueprints)
    assert seen["value"] == "minecraft:granite"
    assert "base_block" not in seen["gaps"]


def test_a_failed_step_leaves_no_blueprint_data_behind(tags, blueprints):
    def action(pack):
        pack.blueprints.create("StoneType", "andesite")
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite")
        pack.fail("changed my mind")

    result = run(action, tags, blueprints)
    assert not result.ok
    assert [i.name for i in blueprints.instances("StoneType")] == ["granite"]
    assert blueprints.bindings("StoneType", "granite") == {}


def test_binding_into_a_nonexistent_instance_fails_the_step(tags, blueprints):
    def action(pack):
        pack.blueprints.bind("StoneType", "nope", "base_block", "minecraft:granite")

    result = run(action, tags, blueprints)
    assert not result.ok and "no instance" in result.reason


# --- conflict policy --------------------------------------------------------

def test_writing_a_slot_the_user_owns_without_a_policy_is_refused(tags, blueprints):
    """No declared policy means the action is writing outside its contract. Design 3.3 has
    no default for a reason — refuse, loudly."""
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite",
                    owner="user")

    def action(pack):
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:andesite")

    result = run(action, tags, blueprints)
    assert not result.ok and "declares no conflict policy" in result.reason
    assert blueprints.value_of("StoneType", "granite", "base_block") == "minecraft:granite"


def test_overwrite_takes_the_slot_and_says_so(tags, blueprints):
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite",
                    owner="user")

    def action(pack):
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:andesite")

    result = run(action, tags, blueprints,
                 conflict_policies={policy_key("blueprint", None, "StoneType"): "overwrite"})
    assert result.ok
    assert blueprints.value_of("StoneType", "granite", "base_block") == "minecraft:andesite"
    assert any("conflict policy: overwrite" in m for _lvl, m in result.log_lines)


def test_skip_leaves_it_alone(tags, blueprints):
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite",
                    owner="user")

    def action(pack):
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:andesite")

    result = run(action, tags, blueprints, conflict_policies={policy_key("blueprint", None, "StoneType"): "skip"})
    assert result.ok
    assert blueprints.value_of("StoneType", "granite", "base_block") == "minecraft:granite"


def test_fail_discards_the_whole_step(tags, blueprints):
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite",
                    owner="user")

    def action(pack):
        pack.blueprints.bind("StoneType", "granite", "polished.base",
                             "minecraft:polished_granite")
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:andesite")

    result = run(action, tags, blueprints, conflict_policies={policy_key("blueprint", None, "StoneType"): "fail"})
    assert not result.ok
    # the earlier, uncontested write went down with the step
    assert "polished.base" not in blueprints.bindings("StoneType", "granite")


def test_re_asserting_its_own_binding_is_not_a_conflict(tags, blueprints):
    """An action is the authoritative producer of its own state and re-runs constantly."""
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite",
                    owner="action", action_ref="pkg:act")

    def action(pack):
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:andesite")

    assert run(action, tags, blueprints).ok       # no policy declared, still fine
    assert blueprints.value_of("StoneType", "granite", "base_block") == "minecraft:andesite"


def test_ownership_transfers_slot_by_slot(tags, blueprints):
    """3.2.2: an action-created instance "can have its bindings individually overwritten
    by the user, transferring ownership slot-by-slot"."""
    def action(pack):
        pack.blueprints.create("StoneType", "andesite")
        pack.blueprints.bind("StoneType", "andesite", "base_block", "minecraft:andesite")
        pack.blueprints.bind("StoneType", "andesite", "polished.base",
                             "minecraft:polished_andesite")

    run(action, tags, blueprints)
    blueprints.bind("StoneType", "andesite", "polished.base",
                    "minecraft:polished_granite", owner="user")

    bindings = blueprints.bindings("StoneType", "andesite")
    assert bindings["base_block"].owner == "action"
    assert bindings["polished.base"].owner == "user"


# --- orphans lock actions out -----------------------------------------------

def test_an_orphaned_blueprint_refuses_to_be_touched(tags, blueprints):
    """"Any action that touches a blueprint with orphaned instances refuses to run until
    the orphans are resolved. The instance does not silently degrade.\""""
    blueprints.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite")
    blueprints.remove_slot("StoneType", "polished.base")

    def action(pack):
        return pack.blueprints.instances("StoneType")

    result = run(action, tags, blueprints)
    assert not result.ok
    assert "orphaned instance" in result.reason and "granite" in result.reason


def test_reads_are_locked_out_too_not_just_writes(tags, blueprints):
    blueprints.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite")
    blueprints.remove_slot("StoneType", "polished.base")
    pack = make_pack(tags, blueprints)
    for call in (lambda: pack.blueprints.get("StoneType", "granite", "base_block"),
                 lambda: pack.blueprints.gaps("StoneType", "granite"),
                 lambda: pack.blueprints.slots("StoneType"),
                 lambda: pack.blueprints.has("StoneType", "granite")):
        with pytest.raises(ActionFailure, match="orphaned"):
            call()


def test_resolving_the_orphan_unlocks_actions_again(tags, blueprints):
    blueprints.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite")
    blueprints.remove_slot("StoneType", "polished.base")
    blueprints.discard("StoneType", "granite")

    def action(pack):
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite")

    assert run(action, tags, blueprints).ok


def test_another_blueprint_is_unaffected(tags, blueprints):
    """The lock is per blueprint, not global — one broken schema shouldn't stop everything."""
    blueprints.define("Wood")
    blueprints.add_slot("Wood", "log", "registry", registry_type="minecraft:block")
    blueprints.create_instance("Wood", "oak")
    blueprints.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite")
    blueprints.remove_slot("StoneType", "polished.base")

    def action(pack):
        pack.blueprints.bind("Wood", "oak", "log", "minecraft:granite")

    assert run(action, tags, blueprints).ok


# --- through real Starlark --------------------------------------------------

def test_the_whole_thing_works_from_starlark(tags, blueprints):
    """The capability has to survive the language boundary, not just the Python API."""
    src = """
def run(pack):
    pack.blueprints.create("StoneType", "andesite")
    pack.blueprints.bind("StoneType", "andesite", "base_block", "minecraft:andesite")
    missing = pack.blueprints.gaps("StoneType", "andesite")
    pack.log("info", "andesite still needs %d slots" % len(missing))
    return missing
"""
    result = {}

    def action(pack):
        result["gaps"] = run_starlark(src, pack)

    run_result = run(action, tags, blueprints)
    assert run_result.ok
    assert result["gaps"] == ["polished.base", "polished.stairs"]
    assert blueprints.value_of("StoneType", "andesite", "base_block") == "minecraft:andesite"
    assert any("still needs 2 slots" in m for _lvl, m in run_result.log_lines)


def test_the_capability_explains_itself_when_absent(tags):
    """A step built without a blueprint store must fail with a sentence, not a NameError
    on an uninjected symbol."""
    pack = Pack(staging=L2Staging(tags), tag_store=tags, packdump=FakeDump(),
                action_ref="pkg:act")
    assert pack.blueprints is None
    src = 'def run(pack):\n    pack.blueprints.create("X", "y")\n'
    with pytest.raises(Exception, match="not available"):
        run_starlark(src, pack)


# --- the shape an action can actually reason about ---------------------------
#
# Iterating slots is useless without knowing what each one ACCEPTS. Everything
# interesting an action does with a blueprint — searching a registry to fill a gap,
# validating, reporting — starts by asking the slot what it wants.

def test_an_action_can_inspect_a_slots_type(tags, blueprints):
    pack = make_pack(tags, blueprints)
    info = pack.blueprints.slot("StoneType", "polished.base")
    assert info["type"] == "registry"
    assert info["registry_type"] == "minecraft:block"
    assert info["group"] == "polished"
    assert info["name"] == "base"


def test_slot_metadata_survives_starlark_as_a_struct(tags, blueprints):
    """`slot.registry_type`, not `slot["registry_type"]` — add_callable can only return
    plain values, so the prelude promotes the dict to a struct."""
    pack = make_pack(tags, blueprints)
    src = """
def run(pack):
    info = pack.blueprints.slot("StoneType", "polished.base")
    return [info.type, info.registry_type, info.group, info.name]
"""
    assert run_starlark(src, pack) == ["registry", "minecraft:block", "polished", "base"]


def test_bindings_reads_a_whole_instance_at_once(tags, blueprints):
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite")
    blueprints.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite")
    pack = make_pack(tags, blueprints)
    assert pack.blueprints.bindings("StoneType", "granite") == {
        "base_block": "minecraft:granite",
        "polished.base": "minecraft:polished_granite",
    }


def test_bindings_is_staging_aware(tags, blueprints):
    seen = {}

    def action(pack):
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite")
        seen["mid"] = pack.blueprints.bindings("StoneType", "granite")

    run(action, tags, blueprints)
    assert seen["mid"] == {"base_block": "minecraft:granite"}


def test_an_action_can_list_blueprints(tags, blueprints):
    assert make_pack(tags, blueprints).blueprints.names() == ["StoneType"]


def test_the_autofill_action_the_design_asks_for(tags, blueprints):
    """§3.2.2's Live Question, written as an author would write it: fill empty slots by
    naming convention. This is the test that proves the capability is *workable*, not just
    present."""
    blueprints.create_instance("StoneType", "andesite")
    src = """
def run(pack):
    schema = pack.step.mappings["palette"]
    filled = 0
    for instance in pack.blueprints.instances(schema):
        for path in pack.blueprints.gaps(schema, instance):
            info = pack.blueprints.slot(schema, path)
            if info.type != "registry":
                continue
            guess = "minecraft:" + instance
            if info.group:
                guess = "minecraft:" + info.group + "_" + instance
            if info.name != "base":
                guess = guess + "_" + info.name
            if pack.registry.has(info.registry_type, guess):
                pack.blueprints.bind(schema, instance, path, guess)
                filled = filled + 1
    return filled
"""
    filled = {}

    def action(pack):
        filled["n"] = run_starlark(src, pack)

    result = run(action, tags, blueprints, mappings={"palette": "StoneType"},
                 conflict_policies={policy_key("blueprint", None, "StoneType"): "overwrite"})
    assert result.ok, result.reason
    assert filled["n"] == 3          # polished_granite, its stairs, polished_andesite
    assert blueprints.value_of("StoneType", "granite", "polished.base") == \
        "minecraft:polished_granite"
    assert blueprints.value_of("StoneType", "granite", "polished.stairs") == \
        "minecraft:polished_granite_stairs"


# --- binding a blueprint into a step -----------------------------------------

def test_a_manifest_can_declare_a_blueprint_mapping(tmp_path, blueprints):
    """Without this an action must hardcode the user's schema name, which is exactly what
    design 3.3's mapping system exists to prevent."""
    from packsmith.core.bindings import resolve_step
    from packsmith.core.packages import load_package

    root = tmp_path / "palette"
    root.mkdir()
    (root / "manifest.json5").write_text('''{
      "package": { "name": "palette" },
      "actions": [
        {
          "id": "autofill", "file": "a.star", "function": "run",
          "mappings": {
            "palette": { "kind": "blueprint", "description": "the schema to fill" },
          },
        },
      ],
    }''', encoding="utf-8")
    manifest = load_package(root).actions[0]
    assert manifest.mappings["palette"].kind == "blueprint"

    mappings, _config = resolve_step(manifest, bindings={"palette": "StoneType"},
                                     config={}, tag_store=None,
                                     blueprint_store=blueprints)
    assert mappings == {"palette": "StoneType"}


def test_binding_a_blueprint_that_does_not_exist_is_refused(tmp_path, blueprints):
    from packsmith.core.bindings import resolve_step
    from packsmith.core.packages import ActionManifest, MappingSlot

    manifest = ActionManifest(package_name="p", action_id="a", file="a.star",
                              function="run",
                              mappings={"palette": MappingSlot(name="palette",
                                                               kind="blueprint")})
    with pytest.raises(ValueError, match="no blueprint named 'Ghost'"):
        resolve_step(manifest, bindings={"palette": "Ghost"}, config={},
                     tag_store=None, blueprint_store=blueprints)


def test_an_unknown_mapping_kind_is_rejected_at_load(tmp_path):
    from packsmith.core.packages import load_package
    root = tmp_path / "bad"
    root.mkdir()
    (root / "manifest.json5").write_text(
        '{"package": {"name": "bad"},'
        ' "actions": [{"id": "a", "file": "a.star", "function": "run",'
        ' "mappings": {"thing": {"kind": "vibes"}}}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown kind 'vibes'"):
        load_package(root)


# --- deleting is not writing (design 3.2.1, applied to bindings) -------------

def test_an_action_may_unbind_what_it_owns(tags, blueprints):
    pack = make_pack(tags, blueprints, policies={policy_key("blueprint", None, "StoneType"): "overwrite"})
    pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite")
    pack.blueprints._staging.commit()
    pack.blueprints.unbind("StoneType", "granite", "base_block")
    pack.blueprints._staging.commit()
    assert blueprints.value_of("StoneType", "granite", "base_block") is None


def test_an_action_cannot_unbind_what_the_user_owns(tags, blueprints):
    """The binding analog of 3.2.1's "actions cannot fully delete an assignment". An
    action that can erase a user's binding can destroy the record of a decision instead
    of superseding it."""
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite",
                    owner="user")
    pack = make_pack(tags, blueprints)
    with pytest.raises(ActionFailure, match="may not delete what it does not own"):
        pack.blueprints.unbind("StoneType", "granite", "base_block")
    assert blueprints.value_of("StoneType", "granite", "base_block") == "minecraft:granite"


def test_overwrite_policy_does_not_license_an_unbind(tags, blueprints):
    """`overwrite` says "I may take this slot", not "I may empty it". A gap is the OUTPUT
    of this primitive (3.2.2), so manufacturing one is a real change of meaning, not a
    lesser form of writing."""
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite",
                    owner="user")
    pack = make_pack(tags, blueprints, policies={policy_key("blueprint", None, "StoneType"): "overwrite"})

    pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:andesite")
    pack.blueprints._staging.commit()                      # taking it: allowed
    assert blueprints.value_of("StoneType", "granite", "base_block") == "minecraft:andesite"

    blueprints.bind("StoneType", "granite", "polished.base", "minecraft:polished_granite",
                    owner="user")
    with pytest.raises(ActionFailure, match="may not delete"):
        pack.blueprints.unbind("StoneType", "granite", "polished.base")


def test_an_action_cannot_unbind_another_actions_binding(tags, blueprints):
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite",
                    owner="action", action_ref="other:thing")
    pack = make_pack(tags, blueprints)
    with pytest.raises(ActionFailure, match="'other:thing'"):
        pack.blueprints.unbind("StoneType", "granite", "base_block")


# --- rollback (design 3.3: "the user can roll back an individual step's writes") --------

def test_rolling_back_undoes_bindings_and_the_instances_the_step_created(tags, blueprints):
    """The records were always written; only the replay was missing, so a rolled-back step
    left its blueprint half committed while claiming to be reversed."""
    from packsmith.core.history import StepRunStore, rollback_step
    history = StepRunStore(tags._db)
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite",
                    owner="user")

    def action(pack):
        pack.blueprints.create("StoneType", "andesite")
        pack.blueprints.bind("StoneType", "andesite", "base_block", "minecraft:andesite")
        pack.blueprints.bind("StoneType", "granite", "polished.base",
                             "minecraft:polished_granite")

    result = run(action, tags, blueprints, history=history,
                 conflict_policies={policy_key("blueprint", None, "StoneType"): "overwrite"})
    assert result.ok
    assert "andesite" in [i.name for i in blueprints.instances("StoneType")]
    assert blueprints.value_of("StoneType", "granite", "polished.base") is not None

    rollback_step(result.run_id, tag_store=tags, history=history,
                  blueprint_store=blueprints)
    assert "andesite" not in [i.name for i in blueprints.instances("StoneType")]
    assert blueprints.value_of("StoneType", "granite", "polished.base") is None
    # ...and what the user owned before the step is exactly as it was
    binding = blueprints.bindings("StoneType", "granite")["base_block"]
    assert (binding.value, binding.owner) == ("minecraft:granite", "user")


def test_rollback_restores_a_binding_the_step_overwrote(tags, blueprints):
    from packsmith.core.history import StepRunStore, rollback_step
    history = StepRunStore(tags._db)
    blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite",
                    owner="user")

    result = run(lambda pack: pack.blueprints.bind(
        "StoneType", "granite", "base_block", "minecraft:andesite"),
        tags, blueprints, history=history,
        conflict_policies={policy_key("blueprint", None, "StoneType"): "overwrite"})
    assert blueprints.value_of("StoneType", "granite", "base_block") == "minecraft:andesite"

    rollback_step(result.run_id, tag_store=tags, history=history,
                  blueprint_store=blueprints)
    binding = blueprints.bindings("StoneType", "granite")["base_block"]
    assert (binding.value, binding.owner) == ("minecraft:granite", "user")


def test_rollback_refuses_rather_than_half_undoing(tags, blueprints):
    """Without a blueprint store the tag half would land and the blueprint half wouldn't,
    and the step would still be marked rolled_back."""
    from packsmith.core.history import StepRunStore, rollback_step
    history = StepRunStore(tags._db)
    result = run(lambda pack: pack.blueprints.bind(
        "StoneType", "granite", "base_block", "minecraft:granite"),
        tags, blueprints, history=history)
    with pytest.raises(ValueError, match="needs a blueprint_store"):
        rollback_step(result.run_id, tag_store=tags, history=history)
