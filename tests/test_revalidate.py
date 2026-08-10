"""Mapping re-validation on schema mutation — design 3.2.2.

"The user makes an informed choice **before the mutation commits.**" So the question under
test is always asked about a *projection*: what would this break, answered without breaking
anything.
"""
import pytest

from packsmith.core.blueprints import BlueprintStore
from packsmith.core.jobs import JobStore
from packsmith.core.packages import ActionManifest, MappingSlot
from packsmith.core.revalidate import (
    steps_declaring_values,
    broken_steps, currently_broken, project_removal, project_rename, project_retype,
    summarise,
)
from packsmith.core.shapes import parse_shape

SHAPE = {
    "base_block": {"kind": "registry_entry", "registry_type": "minecraft:block"},
    "polished": {
        "kind": "group",
        "slots": {"stairs": {"kind": "registry_entry",
                             "registry_type": "minecraft:block"}},
    },
}


class FakeIndex:
    """Just enough package index: ref -> manifest, KeyError when uninstalled."""

    def __init__(self, manifests):
        self._manifests = manifests

    def get(self, ref):
        return self._manifests[ref]


@pytest.fixture
def stone(user_db):
    store = BlueprintStore(user_db)
    store.define("StoneType")
    store.add_slot("StoneType", "base_block", "registry", registry_type="minecraft:block")
    store.add_group("StoneType", "polished")
    for name in ("stairs", "slab"):
        store.add_slot("StoneType", name, "registry", parent="polished",
                       registry_type="minecraft:block")
    return store


@pytest.fixture
def jobs(user_db):
    return JobStore(user_db)


@pytest.fixture
def index():
    slot = MappingSlot(name="stones", kind="blueprint",
                       required_shape=parse_shape(SHAPE, where="test"))
    return FakeIndex({"palette:fill": ActionManifest(
        package_name="palette", action_id="fill", file="f.star", function="run",
        mappings={"stones": slot})})


@pytest.fixture
def bound_job(jobs):
    job = jobs.create("stone_job")
    jobs.add_action_step(job.id, "palette:fill", bindings={"stones": "StoneType"})
    return job


# --- projections don't touch the database ----------------------------------------------

def test_projecting_a_removal_takes_the_subtree(stone):
    projected = project_removal(stone.slots("StoneType"), "polished")
    assert [s.path for s in projected] == ["base_block"]
    # ...and the real schema is untouched
    assert [s.path for s in stone.slots("StoneType")] == [
        "base_block", "polished", "polished.stairs", "polished.slab"]


def test_projecting_a_rename_moves_descendants(stone):
    projected = project_rename(stone.slots("StoneType"), "polished", "buffed")
    assert [s.path for s in projected] == [
        "base_block", "buffed", "buffed.stairs", "buffed.slab"]
    assert [s.path for s in stone.slots("StoneType")][1] == "polished"


def test_projecting_a_retype_changes_only_that_slot(stone):
    projected = project_retype(stone.slots("StoneType"), "base_block", "string")
    by_path = {s.path: s for s in projected}
    assert by_path["base_block"].type == "string"
    assert by_path["base_block"].registry_type is None
    assert by_path["polished.stairs"].type == "registry"
    assert stone.slot("StoneType", "base_block").type == "registry"


# --- what breaks -----------------------------------------------------------------------

def test_removing_a_required_slot_names_the_step(stone, jobs, index, bound_job):
    broken = broken_steps("StoneType",
                          project_removal(stone.slots("StoneType"), "polished.stairs"),
                          job_store=jobs, package_index=index)
    assert len(broken) == 1
    assert broken[0].describe() == "stone_job step 1 (palette:fill)"
    assert broken[0].problems == (
        "no slot 'polished.stairs' (needs a minecraft:block entry)",)


def test_removing_a_slot_nobody_asked_for_breaks_nothing(stone, jobs, index, bound_job):
    assert broken_steps("StoneType",
                        project_removal(stone.slots("StoneType"), "polished.slab"),
                        job_store=jobs, package_index=index) == []


def test_renaming_a_required_slot_breaks_the_contract(stone, jobs, index, bound_job):
    """The case a binding-count check misses entirely: bindings migrate cleanly, and the
    shape contract still breaks, because a mapping asks for a slot by path."""
    broken = broken_steps("StoneType",
                          project_rename(stone.slots("StoneType"), "polished", "buffed"),
                          job_store=jobs, package_index=index)
    assert [p for b in broken for p in b.problems] == [
        "no slot 'polished' (needs a group)",
        "no slot 'polished.stairs' (needs a minecraft:block entry)"]


def test_retyping_breaks_the_contract_with_no_data_at_risk(stone, jobs, index, bound_job):
    broken = broken_steps("StoneType",
                          project_retype(stone.slots("StoneType"), "base_block", "string"),
                          job_store=jobs, package_index=index)
    assert broken[0].problems == (
        "'base_block' holds a string value, but the action needs a minecraft:block entry",)


def test_a_step_bound_to_a_different_schema_is_not_affected(stone, jobs, index):
    stone.define("WoodType")
    job = jobs.create("wood_job")
    jobs.add_action_step(job.id, "palette:fill", bindings={"stones": "WoodType"})
    assert broken_steps("StoneType",
                        project_removal(stone.slots("StoneType"), "polished.stairs"),
                        job_store=jobs, package_index=index) == []


def test_an_uninstalled_package_is_skipped_not_crashed(stone, jobs, bound_job):
    assert broken_steps("StoneType",
                        project_removal(stone.slots("StoneType"), "polished.stairs"),
                        job_store=jobs, package_index=FakeIndex({})) == []


def test_every_bound_step_is_named_not_just_the_first(stone, jobs, index):
    for name in ("alpha", "beta", "gamma"):
        job = jobs.create(name)
        jobs.add_action_step(job.id, "palette:fill", bindings={"stones": "StoneType"})
    broken = broken_steps("StoneType",
                          project_removal(stone.slots("StoneType"), "polished.stairs"),
                          job_store=jobs, package_index=index)
    assert sorted(b.job_name for b in broken) == ["alpha", "beta", "gamma"]


# --- the flag, and the sentence --------------------------------------------------------

def test_nothing_is_currently_broken_until_the_schema_actually_changes(
        stone, jobs, index, bound_job):
    assert currently_broken(stone, job_store=jobs, package_index=index) == []
    stone.remove_slot("StoneType", "polished.stairs")
    still = currently_broken(stone, job_store=jobs, package_index=index)
    assert [b.describe() for b in still] == ["stone_job step 1 (palette:fill)"]


def test_the_summary_names_steps_rather_than_counting_them(stone, jobs, index, bound_job):
    broken = broken_steps("StoneType",
                          project_removal(stone.slots("StoneType"), "polished.stairs"),
                          job_store=jobs, package_index=index)
    text = summarise(broken, mutation="removal")
    assert "will invalidate 1 job step" in text
    assert "stone_job step 1 (palette:fill)" in text
    assert "refuse to run" in text


def test_no_breakage_is_an_empty_string_so_callers_can_just_test_it():
    assert summarise([], mutation="rename") == ""


# --- enum-value removal: BOTH blast radii (design 3.2.1) --------------------

ENUM_MANIFEST = ActionManifest(
    package_name="palette", action_id="standardize", file="s.star", function="run",
    mappings={"tier": MappingSlot(name="tier", kind="tag", tag_type="enum",
                                  registry_type="minecraft:item",
                                  requires_values=("early", "late"))})


@pytest.fixture
def enum_world(user_db):
    from packsmith.core.tags import TagStore
    tags = TagStore(user_db)
    tags.define("minecraft:item", "tier", "enum", enum_values=["early", "mid", "late"])
    jobs = JobStore(user_db)
    job = jobs.create("stone_job")
    from packsmith.core.bindings import binding_id
    jobs.add_action_step(job.id, "palette:standardize", bindings={
        "tier": binding_id(ENUM_MANIFEST.mappings["tier"], "tier", tag_store=tags)})
    return tags, jobs, FakeIndex({"palette:standardize": ENUM_MANIFEST})


def test_removing_a_declared_value_names_the_step(enum_world):
    """3.2.1's own example: "23 assignments use it, and 1 job step (stone_job step 2 →
    palette:standardize) declares it"."""
    tags, jobs, index = enum_world
    hits = steps_declaring_values("minecraft:item", "tier", ["late"],
                                  job_store=jobs, package_index=index, tag_store=tags)
    assert [h.describe() for h in hits] == ["stone_job step 1 (palette:standardize)"]
    assert hits[0].problems == ("declares 'late'",)


def test_removing_a_value_nobody_declared_breaks_nothing(enum_world):
    tags, jobs, index = enum_world
    assert steps_declaring_values("minecraft:item", "tier", ["mid"], job_store=jobs,
                                  package_index=index, tag_store=tags) == []


def test_a_step_bound_to_a_different_tag_is_not_affected(enum_world):
    tags, jobs, index = enum_world
    tags.define("minecraft:item", "phase", "enum", enum_values=["early", "late"])
    assert steps_declaring_values("minecraft:item", "phase", ["late"], job_store=jobs,
                                  package_index=index, tag_store=tags) == []


def test_the_same_tag_name_on_another_registry_is_not_affected(enum_world):
    """`requires_values` is scoped by the mapping's registry, like everything else."""
    tags, jobs, index = enum_world
    assert steps_declaring_values("minecraft:block", "tier", ["late"], job_store=jobs,
                                  package_index=index, tag_store=tags) == []
