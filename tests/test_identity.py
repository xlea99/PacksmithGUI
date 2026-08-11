"""Identity by stable key, not by mutable string (audit root cause R4).

Three hosts, one disease: file ownership keyed on a raw path spelling, conflict policy
keyed on a bare artifact name, job bindings keyed on a renameable name. Each one turns
"which thing is this?" into "which string did the caller happen to use?".
"""
import pytest

from packsmith.core.bindings import conflict_policies_for, policy_key
from packsmith.core.files import FileStore, FileStaging, FileOwnershipError
from packsmith.core.packages import ActionManifest, MappingSlot

REG = "minecraft:item"


# --- FS-2: one disk file, one ownership record ------------------------------

@pytest.fixture
def files(tags, tmp_path):
    return FileStore(tags._db, tmp_path)


@pytest.mark.parametrize("spelling", [
    "config/foo.json",
    r'config\foo.json',
    "./config/foo.json",
    "config/./foo.json",
    "config/bar/../foo.json",
    "/config/foo.json",
])
def test_every_spelling_of_one_path_is_one_key(spelling):
    assert FileStore.key(spelling) == FileStore.key("config/foo.json")


def test_a_user_claim_blocks_an_action_whatever_spelling_it_uses(files):
    """The hard-block is an exact lookup, so a split key was a silently bypassed block."""
    files.claim("config/foo.json", owner="user")
    staging = FileStaging(files)
    with pytest.raises(FileOwnershipError):
        staging.write(r'config\foo.json', "{}", owner="action",
                      owner_action_ref="pkg:act")


def test_claiming_twice_under_different_spellings_is_one_record(files):
    files.claim("config/foo.json", owner="user")
    files.claim(r'config\foo.json', owner="action", owner_action_ref="pkg:act")
    assert len(files.all_ownership()) == 1
    assert files.ownership("config/foo.json")["kind"] == "action"


def test_releasing_uses_the_same_identity(files):
    files.claim(r'config\foo.json', owner="user")
    files.release("config/foo.json")
    assert files.ownership("config/foo.json") is None


# --- L3-8: a policy belongs to an artifact, not to a string -----------------

def _manifest(*slots):
    return ActionManifest(package_name="p", action_id="a", file="a.star", function="run",
                          mappings={s.name: s for s in slots})


def test_the_same_tag_name_on_two_registries_gets_two_policies():
    manifest = _manifest(
        MappingSlot(name="items", kind="tag", registry_type="minecraft:item",
                    access="write", conflict_policy="overwrite"),
        MappingSlot(name="blocks", kind="tag", registry_type="minecraft:block",
                    access="write", conflict_policy="skip"))
    policies = conflict_policies_for(manifest, {"items": "remove", "blocks": "remove"})
    assert policies == {
        policy_key("tag", "minecraft:item", "remove"): "overwrite",
        policy_key("tag", "minecraft:block", "remove"): "skip"}


def test_a_tag_and_a_blueprint_sharing_a_name_do_not_share_a_policy():
    """Different namespaces entirely — they had nothing in common but the string."""
    manifest = _manifest(
        MappingSlot(name="t", kind="tag", registry_type=REG,
                    access="write", conflict_policy="overwrite"),
        MappingSlot(name="b", kind="blueprint", access="write", conflict_policy="skip"))
    policies = conflict_policies_for(manifest, {"t": "palette", "b": "palette"})
    assert policies[policy_key("tag", REG, "palette")] == "overwrite"
    assert policies[policy_key("blueprint", None, "palette")] == "skip"


def test_two_slots_binding_one_artifact_with_different_policies_is_refused():
    """Last-one-wins would be exactly the silent default 3.3's no-default rule forbids."""
    manifest = _manifest(
        MappingSlot(name="a", kind="tag", registry_type=REG,
                    access="write", conflict_policy="overwrite"),
        MappingSlot(name="b", kind="tag", registry_type=REG,
                    access="write", conflict_policy="fail"))
    with pytest.raises(ValueError, match="different conflict policies"):
        conflict_policies_for(manifest, {"a": "remove", "b": "remove"})


def test_two_slots_agreeing_on_a_policy_is_fine():
    manifest = _manifest(
        MappingSlot(name="a", kind="tag", registry_type=REG,
                    access="write", conflict_policy="skip"),
        MappingSlot(name="b", kind="tag", registry_type=REG,
                    access="write", conflict_policy="skip"))
    assert conflict_policies_for(manifest, {"a": "remove", "b": "remove"}) == {
        policy_key("tag", REG, "remove"): "skip"}


# --- L2-3: bindings point at ids, so a rename cannot retarget them -----------

from packsmith.core.bindings import (
    binding_id, binding_name, best_guess_bindings, resolve_step,
)
from packsmith.core.blueprints import BlueprintStore


@pytest.fixture
def stone(user_db):
    store = BlueprintStore(user_db)
    store.define("StoneType")
    store.add_slot("StoneType", "base_block", "registry",
                   registry_type="minecraft:block")
    store.create_instance("StoneType", "granite")
    return store


def _bp_manifest():
    return _manifest(MappingSlot(name="palette", kind="blueprint"))


def test_a_binding_survives_a_rename(stone):
    """The whole point of 3.2.1's identity table: 'stable across renames'."""
    manifest = _bp_manifest()
    bindings = {"palette": binding_id(manifest.mappings["palette"], "StoneType",
                                      blueprint_store=stone)}
    stone.rename("StoneType", "Rocks")
    mappings, _ = resolve_step(manifest, bindings=bindings, config={},
                               tag_store=None, blueprint_store=stone)
    assert mappings["palette"] == "Rocks", "the binding followed the rename"


def test_a_rename_then_recreate_cannot_hijack_a_binding(stone):
    """The failure §3.2.1 designs against, and which was live before this: rename away,
    create something new under the old name, and a name-bound step silently retargets to
    the impostor while the real data sits under the new name."""
    manifest = _bp_manifest()
    bindings = {"palette": binding_id(manifest.mappings["palette"], "StoneType",
                                      blueprint_store=stone)}
    stone.rename("StoneType", "Rocks")
    stone.define("StoneType")                      # a NEW, unrelated schema
    stone.add_slot("StoneType", "base_block", "registry",
                   registry_type="minecraft:block")

    mappings, _ = resolve_step(manifest, bindings=bindings, config={},
                               tag_store=None, blueprint_store=stone)
    assert mappings["palette"] == "Rocks"
    assert [i.name for i in stone.instances(mappings["palette"])] == ["granite"]


def test_a_deleted_artifact_fails_loudly_rather_than_retargeting(stone):
    manifest = _bp_manifest()
    bindings = {"palette": binding_id(manifest.mappings["palette"], "StoneType",
                                      blueprint_store=stone)}
    stone.delete("StoneType")
    with pytest.raises(ValueError, match="no longer exists"):
        resolve_step(manifest, bindings=bindings, config={}, tag_store=None,
                     blueprint_store=stone)


def test_legacy_name_bindings_still_resolve(stone):
    """Existing job rows hold names. They keep working — no migration script, no data
    rewritten out from under a profile that never opens the job editor."""
    manifest = _bp_manifest()
    mappings, _ = resolve_step(manifest, bindings={"palette": "StoneType"}, config={},
                               tag_store=None, blueprint_store=stone)
    assert mappings["palette"] == "StoneType"


def test_a_legacy_name_binding_is_still_vulnerable_and_that_is_expected(stone):
    """Documents the limit of the fallback: a name binding written before ids cannot be
    made rename-safe retroactively. Re-binding the step upgrades it."""
    manifest = _bp_manifest()
    stone.rename("StoneType", "Rocks")
    with pytest.raises(ValueError, match="no blueprint named 'StoneType'"):
        resolve_step(manifest, bindings={"palette": "StoneType"}, config={},
                     tag_store=None, blueprint_store=stone)


def test_tag_bindings_store_ids_too(tags):
    tags.define(REG, "remove", "bool", default=False)
    slot = MappingSlot(name="source", kind="tag", tag_type="bool", registry_type=REG)
    stored = binding_id(slot, "remove", tag_store=tags)
    assert stored == tags.definition(REG, "remove")["id"]
    assert binding_name(slot, stored, tag_store=tags) == "remove"


def test_registry_entry_bindings_stay_strings(tags):
    """An L1 entry id IS its identity — immutable, and not ours to surrogate."""
    slot = MappingSlot(name="anchor", kind="registry_entry",
                       registry_type="minecraft:block")
    assert binding_id(slot, "minecraft:granite") == "minecraft:granite"
    assert binding_name(slot, "minecraft:granite") == "minecraft:granite"


def test_the_canonical_key_is_portable_across_platforms():
    """`os.path.normcase` on Windows also rewrites "/" to "\\", which would undo the
    separator normalisation and make stored keys platform-specific — a profile is a folder
    a user can move between machines."""
    key = FileStore.key("config/foo.json")
    assert "\\" not in key
    assert key.count("/") == 1


def test_one_file_staged_under_two_spellings_yields_one_snapshot(files, tmp_path):
    """Two snapshots means the second holds the FIRST write's content, so rolling back
    restores mid-step state instead of the file as it was."""
    from packsmith.core.files import FileStaging
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "foo.json").write_text("ORIGINAL", encoding="utf-8")

    staging = FileStaging(files)
    staging.write("config/foo.json", "first", owner="action", owner_action_ref="p:a")
    staging.write(r"config\foo.json", "second", owner="action", owner_action_ref="p:a")
    staging.commit()

    assert len(staging.snapshots) == 1
    assert list(staging.snapshots.values())[0]["content"] == "ORIGINAL"
    assert files.read("config/foo.json") == "second"


def test_staged_membership_sees_the_alternate_spelling(files, tmp_path):
    """`file_must_exist` counts files this step already staged — under either spelling."""
    from packsmith.core.files import FileStaging
    staging = FileStaging(files)
    staging.write("config/new.json", "{}", owner="action", owner_action_ref="p:a")
    staging.write(r"config\new.json", "{}", owner="action", owner_action_ref="p:a",
                  file_must_exist=True)          # must not raise
    assert len(staging._pending) == 1
