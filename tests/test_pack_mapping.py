"""Binding an action to a datapack — design 3.3 (`kind = "pack"`) and 7.3.

§3.3 principle 5: *"An action never hardcodes references to user-owned artifacts."* A
datapack is exactly that — the user made it, the user named it, several exist, and which
one an action writes into decides load order and therefore which override wins. Yet §7.3's
own examples used to pass `pack_name="my_gen"` as a literal, because the original ruling
lumped datapacks in with file *paths*.

The ruling now separates them by one test: **does the user own and name it?**
`config/quark-common.toml` is mod convention and stays a literal; a datapack is identity
and gets a mapping slot.

A pack is the first slottable artifact from the FILE world rather than Layer 2, and the two
consequences of that are what most of this file pins down: the binding stores a *name*
(there is no id to store — Packsmith does not own the directory), and `conflict_policy` does
not apply (files hard-block, §6.1, rather than negotiating).
"""
import inspect
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.bindings import (
    best_guess_bindings, binding_id, binding_name, resolve_step)
from packsmith.core.capabilities import CapabilityError, PackTargets, resolve
from packsmith.core.files import FileStore
from packsmith.core.packages import ActionManifest, MappingSlot, _parse_mappings
from packsmith.core.runner import run_action
from packsmith.core.starlark_runtime import run_starlark
from packsmith.integrations.paxi import PaxiProvider

LOOT = "loot_tables/blocks/oak_leaves.json"


class Dump:
    """A pack with Paxi installed, as far as detection is concerned."""

    def __init__(self, mods=("paxi",)):
        self.mods = {m: {"mod_id": m} for m in mods}
        self.registry = {}

    def attribute(self, *a):
        return None


@pytest.fixture
def world(user_db, tmp_path):
    """An instance with Paxi and two datapacks, plus the seam that finds them."""
    root = tmp_path / "instance"
    paxi = PaxiProvider()
    for name in ("base", "tweaks"):
        paxi.create_pack(root, name)
    # Deliberately NOT alphabetical: with base-then-tweaks, a picker sorting by name would
    # pass a load-order assertion by coincidence and nobody would learn anything.
    paxi.set_load_order(root, ["tweaks", "base"])
    files = FileStore(user_db, root)
    targets = PackTargets(resolve(Dump(), loaders=(paxi,)), root)
    return files, paxi, targets


@pytest.fixture
def no_loader(user_db, tmp_path):
    """The same instance without the mod — nothing provides `datapacks.write`."""
    root = tmp_path / "bare"
    root.mkdir()
    files = FileStore(user_db, root)
    return files, PackTargets(resolve(Dump(mods=()), loaders=(PaxiProvider(),)), root)


def slot(**kwargs):
    kwargs.setdefault("kind", "pack")
    kwargs.setdefault("pack_kind", "datapacks")
    kwargs.setdefault("name", "output_pack")
    return MappingSlot(**kwargs)


def manifest(**slots):
    return ActionManifest(package_name="p", action_id="a", file="a.star", function="run",
                          mappings=slots)


# --- the manifest contract -----------------------------------------------------------------

def test_a_pack_mapping_needs_to_say_which_kind():
    """Untyped, the picker cannot know whether to offer datapacks or resource packs, and
    the action cannot know which capability it needs — the same reason `registry_entry`
    must declare a registry_type."""
    with pytest.raises(ValueError, match="pack_kind"):
        _parse_mappings({"out": {"kind": "pack"}})


def test_pack_kind_is_rejected_on_other_kinds():
    with pytest.raises(ValueError, match="only applies to 'pack'"):
        _parse_mappings({"t": {"kind": "tag", "pack_kind": "datapacks"}})


@pytest.mark.parametrize("kind", ["datapacks", "resourcepacks"])
def test_both_pack_kinds_parse(kind):
    parsed = _parse_mappings({"out": {"kind": "pack", "pack_kind": kind,
                                      "access": "write"}})
    assert parsed["out"].pack_kind == kind


def test_a_writing_pack_mapping_needs_no_conflict_policy():
    """Every other write mapping must declare one. Conflict policies govern the
    CLOSED-world engine, where two actions contest ownership of a tag; files use the
    open-world engine, which hard-blocks instead of negotiating (§6.1). Demanding a policy
    here would ask the author to choose between options that don't exist."""
    parsed = _parse_mappings({"out": {"kind": "pack", "pack_kind": "datapacks",
                                      "access": "write"}})
    assert parsed["out"].conflict_policy is None


def test_a_tag_mapping_still_must_declare_one():
    """Guards the exemption: it must be a carve-out for packs, not a hole in the rule."""
    with pytest.raises(ValueError, match="conflict_policy"):
        _parse_mappings({"t": {"kind": "tag", "access": "write"}})


def test_declaring_a_policy_on_a_pack_is_an_error():
    """Silently ignoring it would let an author believe a rule was in force that isn't."""
    with pytest.raises(ValueError, match="cannot declare a conflict_policy"):
        _parse_mappings({"out": {"kind": "pack", "pack_kind": "datapacks",
                                 "access": "write", "conflict_policy": "overwrite"}})


# --- the binding stores a name, not an id --------------------------------------------------

def test_the_binding_is_the_pack_name(world):
    """Layer 2 artifacts carry database ids so a rename can't break a binding. A pack's
    identity IS its directory name, and Packsmith does not own that directory — so there
    is no id to store, and this is the one mapping kind where a rename outside Packsmith
    genuinely breaks the binding."""
    assert binding_id(slot(), "tweaks") == "tweaks"
    assert binding_name(slot(), "tweaks") == "tweaks"


# --- resolving a step ----------------------------------------------------------------------

def test_a_bound_pack_resolves_to_its_name(world):
    _, _, targets = world
    mappings, _ = resolve_step(manifest(out=slot()), bindings={"out": "tweaks"},
                               config={}, tag_store=None, pack_targets=targets)
    assert mappings["out"] == "tweaks"


def test_a_pack_that_no_longer_exists_refuses_the_step(world):
    """Re-checked at resolve time, not only when bound: the pack is a directory anything
    can delete between binding this step and pressing play, and a write into a pack that
    isn't there is a silent no-op rather than an error Minecraft reports."""
    files, paxi, targets = world
    import shutil
    shutil.rmtree(paxi.datapack_root(files.root) / "tweaks")

    with pytest.raises(ValueError, match="no datapack called 'tweaks'"):
        resolve_step(manifest(out=slot()), bindings={"out": "tweaks"}, config={},
                     tag_store=None, pack_targets=targets)


def test_with_no_loader_the_step_says_so_rather_than_failing_obscurely(no_loader):
    """§8.1 is the fix, so the message points there instead of reporting a missing folder."""
    _, targets = no_loader
    with pytest.raises(ValueError, match="no pack loader is installed"):
        resolve_step(manifest(out=slot()), bindings={"out": "tweaks"}, config={},
                     tag_store=None, pack_targets=targets)


def test_an_unbound_required_pack_blocks_the_step(world):
    _, _, targets = world
    with pytest.raises(ValueError, match="required mapping 'out' is unbound"):
        resolve_step(manifest(out=slot()), bindings={}, config={}, tag_store=None,
                     pack_targets=targets)


# --- best-guess fill (3.3) -------------------------------------------------------------------

def test_the_author_hint_wins_when_the_user_has_that_pack(world):
    _, _, targets = world
    guessed = best_guess_bindings(manifest(out=slot(likely_name="tweaks")), tag_store=None,
                                  pack_targets=targets)
    assert guessed["out"] == "tweaks"


def test_with_several_packs_and_no_hint_nothing_is_guessed(world):
    """Which pack an override lands in decides load order — a real decision, and guessing
    it is worse than asking. 3.3 only pre-selects when exactly one candidate exists."""
    _, _, targets = world
    guessed = best_guess_bindings(manifest(out=slot()), tag_store=None, pack_targets=targets)
    assert guessed["out"] is None


def test_a_single_pack_is_preselected(world):
    """3.3: "if exactly one compatible artifact exists, pre-select it"."""
    files, paxi, targets = world
    import shutil
    shutil.rmtree(paxi.datapack_root(files.root) / "base")

    guessed = best_guess_bindings(manifest(out=slot()), tag_store=None, pack_targets=targets)
    assert guessed["out"] == "tweaks"


# --- pack.datapacks / pack.resourcepacks (7.3) -----------------------------------------------

def run_with(files, targets, body, mappings=None):
    return run_action(body, tag_store=_NoTags(), packdump=Dump(), action_ref="p:a",
                      file_store=files, mappings=mappings or {}, pack_targets=targets)


class _NoTags:
    """Just enough tag store for the runner's staging to exist."""
    _db = None

    def all_definitions(self):
        return {}


def test_an_action_writes_into_the_pack_the_user_bound(world):
    """The whole point: the action names a *slot*, the user names the pack."""
    files, paxi, targets = world

    def body(pack):
        pack.datapacks.resolve(pack=pack.step.mappings["out"], namespace="minecraft",
                               path=LOOT).write('{"pools": []}')

    result = run_with(files, targets, body, mappings={"out": "tweaks"})
    assert result.ok, result.reason
    landed = paxi.datapack_root(files.root) / "tweaks" / "data" / "minecraft" / LOOT
    assert landed.read_text() == '{"pools": []}'


def test_the_namespace_path_is_reproduced_exactly(world):
    """An override works by sitting at the same namespace path the mod uses. Change the
    path and it becomes an unrelated file the game never reads."""
    files, paxi, targets = world

    def body(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="quark", path=LOOT).write("{}")

    assert run_with(files, targets, body).ok
    target = paxi.datapack_root(files.root) / "tweaks" / "data" / "quark" / LOOT
    inside = target.relative_to(paxi.datapack_root(files.root) / "tweaks").as_posix()
    assert inside == f"data/quark/{LOOT}"


def test_resource_packs_land_under_assets(world):
    """Minecraft's own structure decides the root, not the loader: `data/` is datapack
    territory and `assets/` is resource pack territory."""
    files, paxi, targets = world
    paxi.create_pack(files.root, "textures", kind="resourcepacks")

    def body(pack):
        pack.resourcepacks.resolve(pack="textures", namespace="minecraft",
                                   path="textures/block/stone.png").write("x")

    assert run_with(files, targets, body).ok
    assert (paxi.resourcepack_root(files.root) / "textures" / "assets" / "minecraft"
            / "textures" / "block" / "stone.png").exists()


def test_what_an_action_writes_is_owned_by_that_action(world):
    """§6.1: the write path IS the ownership claim, and routing through a provider must
    not lose that."""
    files, paxi, targets = world

    def body(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft", path=LOOT).write("{}")

    assert run_with(files, targets, body).ok
    target = paxi.datapack_root(files.root) / "tweaks" / "data" / "minecraft" / LOOT
    rel = target.relative_to(files.root).as_posix()
    assert files.ownership(rel)["kind"] == "action"


def test_a_failed_step_writes_no_pack_file(world):
    """Provider-routed writes stage like every other write, so they discard together."""
    files, paxi, targets = world

    def body(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft", path=LOOT).write("{}")
        pack.fail("changed my mind")

    assert not run_with(files, targets, body).ok
    # `data/` itself exists — `create_pack` makes it — so the file is what must be absent.
    assert not (paxi.datapack_root(files.root) / "tweaks" / "data" / "minecraft"
                / LOOT).exists()


def test_writing_into_a_pack_that_is_not_there_is_refused(world):
    """A folder with no `pack.mcmeta` is not loaded by Minecraft at all, so this would
    succeed, change nothing in-game, and look exactly like it worked."""
    files, _, targets = world

    def body(pack):
        pack.datapacks.resolve(pack="imaginary", namespace="minecraft", path=LOOT).write("{}")

    result = run_with(files, targets, body)
    assert not result.ok
    assert "no datapack called 'imaginary'" in result.reason


def test_without_a_loader_the_namespace_exists_and_explains_itself(no_loader):
    """`pack.datapacks` is always present. Leaving it off would raise AttributeError, which
    tells an author nothing about what to install."""
    files, targets = no_loader

    def body(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft", path=LOOT).write("{}")

    result = run_with(files, targets, body)
    assert not result.ok
    assert "pack loader" in result.reason


def test_an_action_can_ask_whether_a_capability_is_there(world, no_loader):
    """§7.4's introspection, for actions that declare a capability optional."""
    files, _, targets = world
    seen = {}

    def body(pack):
        seen["yes"] = pack.capabilities.has("datapacks.write")
        seen["no"] = pack.capabilities.has("kubejs.script_write")

    assert run_with(files, targets, body).ok
    assert seen == {"yes": True, "no": False}


# --- the same thing, through actual Starlark ----------------------------------------------------
#
# Everything above hands `run_action` a plain Python callable — §1.1's MVP language seam.
# It is still real code, but it is no longer the path any action takes, and for nine
# commits that difference hid the fact that `pack.datapacks` was absent from the Starlark
# prelude entirely: the whole subsystem was built, wired into the GUI and covered by the
# tests above, and unreachable from the only language that writes actions. A `def body(pack)`
# that reads like Starlark is what made it invisible.
#
# `test_pack_surface.py` now pins the SHAPE — every member of `Pack` is reachable from
# Starlark. These pin the BEHAVIOUR through the same boundary an author crosses.


def starlark_body(source):
    """The shape `PackageIndex.load_callable` produces: a closure the runner can call with
    `pack`, whose body happens to be Starlark. Deliberately built the same way, so what is
    exercised here is what production does."""
    def invoke(pack):
        return run_starlark(source, pack)
    return invoke


def test_a_starlark_action_writes_into_the_pack_the_user_bound(world):
    """The flagship assertion of this file, through the boundary that matters.

    Written with the `pack=` keyword on purpose: §3.3 and §7.3 both spell the call that
    way, so the prelude's parameter has to be *named* `pack` for the documented form to
    parse — even though it shadows the `pack` struct inside that function.
    """
    files, paxi, targets = world
    src = f"""
def run(pack):
    pack.datapacks.resolve(
        pack = pack.step.mappings["out"], namespace = "minecraft", path = "{LOOT}"
    ).write('{{"pools": []}}')
"""
    result = run_with(files, targets, starlark_body(src), mappings={"out": "tweaks"})
    assert result.ok, result.reason
    landed = paxi.datapack_root(files.root) / "tweaks" / "data" / "minecraft" / LOOT
    assert landed.read_text() == '{"pools": []}'


def test_a_starlark_action_can_branch_on_a_capability(world):
    """§7.6's capability-branching pattern. Without `pack.capabilities` in the prelude an
    `optional = true` declaration was unusable — the action had no way to ask."""
    files, _, targets = world
    src = """
def run(pack):
    if pack.capabilities.has("datapacks.write"):
        pack.log("info", "have it")
    if not pack.capabilities.has("kubejs.script_write"):
        pack.log("info", "skipping kubejs")
    return True
"""
    result = run_with(files, targets, starlark_body(src))
    assert result.ok, result.reason
    assert [line for _level, line in result.log_lines] == ["have it", "skipping kubejs"]


def test_the_missing_loader_message_survives_the_starlark_boundary(no_loader):
    """The routing is done on the host so its refusals keep §8.1's wording. An author
    reading a raw Starlark traceback would learn nothing about what to install."""
    files, targets = no_loader
    src = f"""
def run(pack):
    pack.datapacks.resolve(pack = "tweaks", namespace = "minecraft",
                           path = "{LOOT}").write("{{}}")
"""
    result = run_with(files, targets, starlark_body(src))
    assert not result.ok
    assert "pack loader" in result.reason


# --- the picker ---------------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def dialog_for(targets, bound=None):
    from packsmith.core.jobs import JobStep
    from packsmith.gui.job_editor import StepForm

    step = JobStep(id=1, job_id=1, position=0, kind="action", action_ref="p:a",
                   bindings={"out": bound} if bound else {})
    return StepForm(manifest(out=slot()), None, step, packdump=Dump(),
                    pack_targets=targets)


def test_the_picker_lists_the_packs_in_load_order(world, qapp):
    """Order matters and is not alphabetical: later packs override earlier ones, so the
    list is the loader's own order — the same thing the save-as-override dialog shows."""
    _, _, targets = world
    dialog = dialog_for(targets)          # held: dropping it deletes the C++ widget
    combo = dialog._mapping_widgets["out"]
    assert [combo.itemText(i) for i in range(combo.count())] == ["tweaks", "base"]


def test_a_bound_pack_comes_back_selected(world, qapp):
    _, _, targets = world
    form = dialog_for(targets, bound="tweaks")
    assert form._mapping_widgets["out"].currentData() == "tweaks"
    bindings, _config, _on_error = form.read()
    assert bindings["out"] == "tweaks", "showing and reading back lost the binding"


def test_no_loader_and_no_packs_are_different_messages(world, no_loader, qapp):
    """Collapsing them would send the user to install a mod they already have, or to make
    a datapack they have nowhere to put."""
    files, paxi, targets = world
    import shutil
    for name in ("base", "tweaks"):
        shutil.rmtree(paxi.datapack_root(files.root) / name)
    with_loader = dialog_for(targets)     # both held: dropping either deletes its widget
    empty = with_loader._mapping_widgets["out"]

    _, bare = no_loader
    without_loader = dialog_for(bare)
    missing = without_loader._mapping_widgets["out"]

    assert "no datapacks yet" in empty.itemText(0)
    assert "no pack loader" in missing.itemText(0)
    assert not empty.isEnabled() and not missing.isEnabled()


# --- the two bugs configuring a pack step exposed -----------------------------------

def _editor(world, user_db, mappings):
    from packsmith.core.jobs import JobStore
    from packsmith.core.tags import TagStore
    from packsmith.gui.job_editor import JobEditorTab

    _files, _paxi, targets = world
    tags = TagStore(user_db)
    tags.define("minecraft:item", "remove", "bool")      # exactly one candidate
    jobs = JobStore(user_db)
    job = jobs.create("removal")

    class Index:
        actions = {"p:a": manifest(**mappings)}
        def get(self, ref):
            return self.actions[ref]

    tab = JobEditorTab(jobs.get(job.id), job_store=jobs, package_index=Index(),
                       tag_store=tags, packdump=Dump(), pack_targets=targets)
    tab._picking = "action"          # what the picker sets before it opens
    tab._on_picked("p:a")
    return jobs.steps_of(job.id)[0]


def test_a_new_step_is_created_already_bound(world, qapp, user_db):
    """3.3's best-guess fill "pre-selects" a candidate, and the form duly showed one — but
    nothing wrote it. A step's bindings stayed empty until a widget emitted a *change*, so
    opening the dropdown and clicking the single tag already highlighted committed nothing:
    the index never moved, so no signal fired. The only way to make it stick was to edit
    some unrelated field, which is a strange thing to have to discover.
    """
    tag = MappingSlot(name="target", kind="tag", tag_type="bool",
                      registry_type="minecraft:item")
    step = _editor(world, user_db, {"target": tag})

    assert step.bindings.get("target") is not None,         "the suggestion the form displays has to be the one the step stores"
    assert step.bound_names.get("target") == "remove",         "and its name is recorded, or a later rename cannot be detected as stale"


def test_a_pack_slot_is_bound_when_the_hint_names_a_real_pack(world, qapp, user_db):
    """The `pack` half of the same fill. `likely_name` wins when the user actually has a
    pack by that name."""
    step = _editor(world, user_db, {"out": slot(likely_name="tweaks")})
    assert step.bindings.get("out") == "tweaks"


def test_several_packs_are_left_for_the_user_to_choose(world, qapp, user_db):
    """Deliberately NOT guessed. Which pack an override lands in decides load order and
    therefore which override wins — guessing that is worse than asking."""
    step = _editor(world, user_db, {"out": slot()})
    assert "out" not in step.bindings


def test_the_job_editor_can_see_the_pack_loader(world, qapp, user_db):
    """Reported: every `pack` mapping read "no pack loader installed" while the Files
    panel's smart mode listed Paxi's folders happily. The picker cannot tell an absent
    loader from an absent resolution table, and the editor was simply never handed one —
    so a step you could not configure would have run fine once bound another way.
    """
    from packsmith.gui.main_window import MainWindow

    source = inspect.getsource(MainWindow._open_job_editor)
    assert "pack_targets=" in source, \
        "the job editor builds every mapping picker, including the pack one"


def test_writing_into_a_disabled_pack_is_refused(world):
    """The hole the disable feature exposed, and the reason it had to be built into the
    capability rather than bolted on: the guard checked only that the DIRECTORY was there,
    so a pack whose manifest had been renamed aside passed it. The write landed on disk,
    the run reported success, and the game read none of it — the exact silent no-op the
    guard's own message warns about.
    """
    files, paxi, targets = world
    paxi.disable(files.root, "tweaks", "datapacks")

    def body(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft", path=LOOT).write("{}")

    result = run_with(files, targets, body)
    assert not result.ok
    assert "disabled" in result.reason
    assert not (paxi.datapack_root(files.root) / "tweaks" / "data" / "minecraft"
                / LOOT).exists()


def test_a_disabled_pack_is_not_offered_in_the_picker(world, qapp):
    """It cannot be bound, for the same reason it cannot be written: a step pointing at a
    pack the game does not read is a run that succeeds and changes nothing."""
    files, paxi, targets = world
    paxi.disable(files.root, "tweaks", "datapacks")

    dialog = dialog_for(targets)          # held: dropping it deletes the C++ widget
    combo = dialog._mapping_widgets["out"]
    assert [combo.itemText(i) for i in range(combo.count())] == ["base"]


# --- what did I write last time? (design 6.1's ownership, read back) -------------------
#
# Actions keep needing this — a pack that is a projection of a tag has to clear what the
# tag no longer says. The answer is DERIVED from `file_ownership` rather than remembered in
# a sidecar, for the reason §3.2.1 gives for orphans: a record kept beside the truth goes
# stale, and a derived one cannot.

def test_an_action_can_ask_what_it_owns_in_a_pack(world):
    files, paxi, targets = world

    def write(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft",
                               path=LOOT).write("{}")

    assert run_with(files, targets, write).ok

    landed = paxi.datapack_root(files.root) / "tweaks" / "data" / "minecraft" / LOOT
    expected = landed.relative_to(files.root).as_posix()

    owned = _returned(files, targets, lambda pack: pack.datapacks.owned("tweaks"))
    assert [handle.path for handle in owned] == [expected]


def _returned(files, targets, body):
    """`run_action` reports status, not the callable's return value — so capture it."""
    captured = []
    def wrapper(pack):
        captured.append(body(pack))
    run_with(files, targets, wrapper)
    return captured[0]


def test_owning_is_scoped_to_the_pack_that_was_asked_for(world):
    """The footgun this signature exists to close. Ownership is per ACTION — it must be,
    or two steps of one action would permanently steal files from each other — so an
    unscoped listing would hand a step every file the same action wrote in OTHER steps.
    An action clearing "everything I own that I didn't write this run" would then blank a
    sibling step's output, silently.
    """
    files, _paxi, targets = world

    def write_both(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft", path=LOOT).write("{}")
        pack.datapacks.resolve(pack="base", namespace="minecraft", path=LOOT).write("{}")

    assert run_with(files, targets, write_both).ok

    mine = [handle.path for handle in
            _returned(files, targets, lambda pack: pack.datapacks.owned("tweaks"))]
    assert all("/tweaks/" in path for path in mine), mine
    assert mine, "the pack it asked about should not be empty"


def test_another_actions_files_are_not_mine(world):
    files, _paxi, targets = world

    def write(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft", path=LOOT).write("{}")

    run_action(write, tag_store=_NoTags(), packdump=Dump(), action_ref="somebody:else",
               file_store=files, mappings={}, pack_targets=targets)

    assert _returned(files, targets, lambda pack: pack.datapacks.owned("tweaks")) == []


def test_a_file_staged_this_step_counts_as_owned(world):
    """§7.4: every read on `pack` is staged-first, with no exceptions for the awkward ones.
    An action that writes a file and then asks what it owns must see it."""
    files, _paxi, targets = world

    def write_then_ask(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft", path=LOOT).write("{}")
        return pack.datapacks.owned("tweaks")

    assert _returned(files, targets, write_then_ask), "a staged write was invisible"


# --- what `owned` hands back (design 6.1, 7.3) ---------------------------------------
#
# It used to be paths, and every caller then fed each one back through
# `pack.filesystem.resolve(path)` to do anything with it. That round trip is only correct
# while there is exactly ONE root a bare path could mean — the moment a second tracked root
# exists, the same relative path names two different files and the re-resolve silently
# picks the wrong one. A handle never stopped knowing where it lives.

def test_owned_hands_back_handles_not_paths(world):
    """The contract. A path has to be re-resolved by hand to be used; a handle does not,
    and cannot be re-resolved into the wrong place."""
    files, paxi, targets = world

    def write(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft", path=LOOT).write("{}")

    assert run_with(files, targets, write).ok
    owned = _returned(files, targets, lambda pack: pack.datapacks.owned("tweaks"))

    assert owned, "nothing came back"
    handle = owned[0]
    assert hasattr(handle, "path") and hasattr(handle, "root")
    assert not isinstance(handle, str), "a bare path needs re-resolving to be used"


def test_a_returned_handle_can_write_without_being_resolved_again(world):
    """The reason the change is worth a migration: the loop that clears stale output is
    `for handle in owned(...): handle.write_json({})`, with no second lookup in it."""
    files, paxi, targets = world

    def write(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft", path=LOOT).write("x")

    assert run_with(files, targets, write).ok

    def clear(pack):
        for handle in pack.datapacks.owned("tweaks"):
            handle.write("{}")

    assert run_with(files, targets, clear).ok
    landed = paxi.datapack_root(files.root) / "tweaks" / "data" / "minecraft" / LOOT
    assert landed.read_text(encoding="utf-8") == "{}"


def test_a_handle_says_which_root_it_is_under(world):
    """One root exists today, so this is a constant — and it is here so that the day a
    second one exists, nothing that already reads it has to change."""
    files, _paxi, targets = world

    def write(pack):
        pack.datapacks.resolve(pack="tweaks", namespace="minecraft", path=LOOT).write("{}")

    assert run_with(files, targets, write).ok
    owned = _returned(files, targets, lambda pack: pack.datapacks.owned("tweaks"))
    assert owned[0].root == "minecraft"
