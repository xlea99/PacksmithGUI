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


# --- the picker ---------------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def dialog_for(targets, bound=None):
    from packsmith.core.jobs import JobStep
    from packsmith.gui.job_editor import StepEditorDialog

    step = JobStep(id=1, job_id=1, position=0, kind="action", action_ref="p:a",
                   bindings={"out": bound} if bound else {})
    return StepEditorDialog(manifest(out=slot()), None, step, packdump=Dump(),
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
    dialog = dialog_for(targets, bound="tweaks")
    assert dialog._mapping_widgets["out"].currentData() == "tweaks"
    dialog.accept()
    assert dialog.result_bindings["out"] == "tweaks", "opening and accepting lost the binding"


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
