"""Renaming a tag — design 3.2.1's Tag Schema Evolution.

The rule the whole ceremony exists for: a rename "never touches data — assignments and
bindings reference the id. What it touches is *meaning*." Nothing breaks mechanically,
which is precisely why it needs a gate — a job step would otherwise keep running against a
tag whose meaning the user just changed, silently.

The split under test throughout: **loud when it acts, quiet when it observes.** Saved
Views follow a rename automatically; bound job steps stop until relinked.
"""
import pytest

from packsmith.core.bindings import binding_id, record_names, stale_bindings
from packsmith.core.jobs import JobStore
from packsmith.core.packages import ActionManifest, MappingSlot
from packsmith.core.query import Query, Registry, Tag, Cmp, And, Id, to_dict
from packsmith.core.tags import TagStore
from packsmith.core.views import ViewStore, rename_tag_in_views

REG = "minecraft:item"

MANIFEST = ActionManifest(
    package_name="removal", action_id="nuke", file="n.star", function="run",
    mappings={"target": MappingSlot(name="target", kind="tag", tag_type="bool",
                                    registry_type=REG)})


class Index:
    def get(self, ref):
        return {"removal:nuke": MANIFEST}[ref]


@pytest.fixture
def tags(user_db):
    store = TagStore(user_db)
    store.define(REG, "remove", "bool")
    store.assign(REG, "minecraft:stone", "remove", True)
    return store


@pytest.fixture
def views(user_db):
    return ViewStore(user_db)


@pytest.fixture
def bound(user_db, tags):
    """A job step bound to `remove`, recorded under that name."""
    jobs = JobStore(user_db)
    job = jobs.create("nightly")
    bindings = {"target": binding_id(MANIFEST.mappings["target"], "remove", tag_store=tags)}
    jobs.add_action_step(job.id, "removal:nuke", bindings=bindings,
                         bound_names=record_names(MANIFEST, bindings, tag_store=tags))
    return jobs, jobs.get(job.id)


# --- the rename itself ---------------------------------------------------------------

def test_renaming_keeps_every_assignment(tags):
    tags.rename(REG, "remove", "cull")
    assert tags.get_tag(REG, "minecraft:stone", "cull") is True
    assert tags.definition(REG, "remove") is None
    assert tags.definition(REG, "cull") is not None


def test_the_id_is_stable_across_a_rename(tags):
    """Identity is the id (§3.2.1); the name is a label. Everything else depends on this."""
    before = tags.definition(REG, "remove")["id"]
    tags.rename(REG, "remove", "cull")
    assert tags.definition(REG, "cull")["id"] == before


def test_renaming_onto_an_existing_name_refuses(tags):
    tags.define(REG, "keep", "bool")
    with pytest.raises(ValueError, match="already has a tag"):
        tags.rename(REG, "remove", "keep")
    assert tags.definition(REG, "remove") is not None, "the original was damaged"


def test_the_same_name_on_another_registry_is_not_a_collision(tags):
    tags.define("minecraft:block", "cull", "bool")
    tags.rename(REG, "remove", "cull")           # definitions are registry-scoped
    assert tags.definition(REG, "cull") is not None
    assert tags.definition("minecraft:block", "cull") is not None


def test_renaming_to_the_same_name_is_a_no_op(tags):
    assert tags.rename(REG, "remove", "remove") == []


def test_renaming_something_that_does_not_exist_is_an_error(tags):
    with pytest.raises(KeyError):
        tags.rename(REG, "nope", "whatever")


def test_an_empty_name_is_refused(tags):
    with pytest.raises(ValueError):
        tags.rename(REG, "remove", "   ")


# --- quiet when it observes: saved Views ----------------------------------------------

def test_saved_views_follow_a_rename_without_asking(tags, views):
    views.create("Removal Queue",
                 Query(scope=Registry(REG), select=[Id], filter=Cmp(Tag("remove"), "eq", True)))
    tags.rename(REG, "remove", "cull", view_store=views)

    payload = str(to_dict(views.all()[0].query))
    assert "cull" in payload and "remove" not in payload


def test_only_the_matching_tag_is_rewritten(tags, views):
    views.create("Mixed", Query(scope=Registry(REG), select=[Id],
                                filter=Cmp(Tag("keep"), "eq", True)))
    assert rename_tag_in_views(views, "remove", "cull") == [], \
        "a View that never mentioned the tag was rewritten"


def test_the_rewrite_reaches_nested_nodes(tags, views):
    """Tags hide inside And/Or/Not, order_by and group_by — the walk is over the whole
    serialized document rather than a list of node types that can go stale."""
    views.create("Deep", Query(scope=Registry(REG), select=[Id],
                               filter=And([Cmp(Tag("remove"), "eq", True),
                                           Cmp(Tag("remove"), "neq", False)])))
    tags.rename(REG, "remove", "cull", view_store=views)
    assert "remove" not in str(to_dict(views.all()[0].query))


# --- loud when it acts: job steps ------------------------------------------------------

def test_a_bound_step_goes_stale_the_moment_the_tag_is_renamed(tags, bound):
    jobs, job = bound
    assert stale_bindings(job, package_index=Index(), tag_store=tags) == []

    tags.rename(REG, "remove", "cull")

    stale = stale_bindings(jobs.get(job.id), package_index=Index(), tag_store=tags)
    assert len(stale) == 1
    assert stale[0].was == "remove" and stale[0].now == "cull"
    assert "nightly step 1" in stale[0].describe()


def test_relinking_clears_it(tags, bound):
    jobs, job = bound
    tags.rename(REG, "remove", "cull")
    step = jobs.get(job.id).steps[0]

    jobs.relink_step(step.id, record_names(MANIFEST, step.bindings, tag_store=tags))

    assert stale_bindings(jobs.get(job.id), package_index=Index(), tag_store=tags) == []


def test_relinking_does_not_change_what_the_step_targets(tags, bound):
    """The step always pointed at the same id. Confirming is the whole gesture."""
    jobs, job = bound
    before = jobs.get(job.id).steps[0].bindings
    tags.rename(REG, "remove", "cull")
    step = jobs.get(job.id).steps[0]
    jobs.relink_step(step.id, record_names(MANIFEST, step.bindings, tag_store=tags))
    assert jobs.get(job.id).steps[0].bindings == before


def test_a_step_bound_to_a_different_tag_is_untouched(tags, bound):
    jobs, job = bound
    tags.define(REG, "other", "bool")
    tags.rename(REG, "other", "renamed")
    assert stale_bindings(jobs.get(job.id), package_index=Index(), tag_store=tags) == []


def test_steps_written_before_any_rename_existed_are_grandfathered(tags, user_db):
    """No recorded name means nothing to contradict — and rename did not exist to have
    been used, so treating those as stale would be inventing a problem."""
    jobs = JobStore(user_db)
    job = jobs.create("legacy")
    jobs.add_action_step(job.id, "removal:nuke", bindings={
        "target": binding_id(MANIFEST.mappings["target"], "remove", tag_store=tags)})
    tags.rename(REG, "remove", "cull")
    assert stale_bindings(jobs.get(job.id), package_index=Index(), tag_store=tags) == []


def test_renaming_back_un_stales_the_step_by_itself(tags, bound):
    """Derived, not flagged: the answer follows the data rather than an event log that
    would need its own cleanup."""
    jobs, job = bound
    tags.rename(REG, "remove", "cull")
    assert stale_bindings(jobs.get(job.id), package_index=Index(), tag_store=tags)

    tags.rename(REG, "cull", "remove")
    assert stale_bindings(jobs.get(job.id), package_index=Index(), tag_store=tags) == []


# --- the blast radius, stated before committing ----------------------------------------

def test_the_preview_names_both_consequences(tags, views, bound):
    jobs, job = bound
    views.create("Queue", Query(scope=Registry(REG), select=[Id], filter=Cmp(Tag("remove"), "eq", True)))

    preview = tags.preview_rename(REG, "remove", "cull", view_store=views,
                                  job_store=jobs, package_index=Index())

    assert preview["views"] == ["Queue"]
    assert preview["steps"] == ["nightly step 1 (removal:nuke)"]


def test_the_preview_changes_nothing(tags, views, bound):
    jobs, _ = bound
    tags.preview_rename(REG, "remove", "cull", view_store=views, job_store=jobs,
                        package_index=Index())
    assert tags.definition(REG, "remove") is not None


# --- the gate: a job with a relink owed will not start ---------------------------------

def _run(job, tags, packdump=None):
    from packsmith.core.job_runner import run_job
    return run_job(job, job_store=None, package_index=Index(), tag_store=tags,
                   packdump=packdump)


def test_a_job_with_a_stale_binding_refuses_to_start(tags, bound):
    """§3.2.1: the step "refuses to run until the user explicitly relinks it"."""
    jobs, job = bound
    tags.rename(REG, "remove", "cull")

    result = _run(jobs.get(job.id), tags)

    assert result.status == "failed"
    assert result.blocked and result.blocked[0].was == "remove"


def test_nothing_is_applied_by_a_blocked_job(tags, bound):
    """The reason the gate is pre-flight rather than per-step: a job that dies at step 3
    has already applied steps 1 and 2. Refusing before the first step means the run never
    starts, so there is nothing half-applied to reason about."""
    jobs, job = bound
    tags.rename(REG, "remove", "cull")

    result = _run(jobs.get(job.id), tags)

    assert result.step_results == [], "a step ran despite the job being blocked"
    assert result.run_id is None, "a run was recorded for a job that never started"
    assert result.not_run == len(jobs.get(job.id).steps)


def test_relinking_lets_it_start_again(tags, bound):
    jobs, job = bound
    tags.rename(REG, "remove", "cull")
    step = jobs.get(job.id).steps[0]
    jobs.relink_step(step.id, record_names(MANIFEST, step.bindings, tag_store=tags))

    assert _run(jobs.get(job.id), tags).blocked == []


def test_an_untouched_job_is_not_gated(tags, bound):
    jobs, job = bound
    assert _run(jobs.get(job.id), tags).blocked == []


# --- editing a step is itself an assertion of meaning ----------------------------------

def test_saving_a_step_through_the_editor_clears_the_relink(tags, bound):
    """Re-picking in the step editor is the other way to relink: saving a step asserts it
    means what you want, which is the same assertion the Errors panel asks for."""
    jobs, job = bound
    tags.rename(REG, "remove", "cull")
    step = jobs.get(job.id).steps[0]

    jobs.update_step(step.id, bindings=step.bindings,
                     bound_names=record_names(MANIFEST, step.bindings, tag_store=tags))

    assert stale_bindings(jobs.get(job.id), package_index=Index(), tag_store=tags) == []


def test_a_step_bound_after_the_rename_is_never_stale(tags, user_db):
    jobs = JobStore(user_db)
    tags.rename(REG, "remove", "cull")
    job = jobs.create("later")
    bindings = {"target": binding_id(MANIFEST.mappings["target"], "cull", tag_store=tags)}
    jobs.add_action_step(job.id, "removal:nuke", bindings=bindings,
                         bound_names=record_names(MANIFEST, bindings, tag_store=tags))
    assert stale_bindings(jobs.get(job.id), package_index=Index(), tag_store=tags) == []


# --- knowing WITHOUT running (design 3.2.1, surfaced in the UI) -------------------------

def test_a_broken_binding_names_the_tag_it_lost(tags, user_db):
    """"The bound tag does not exist" is true and useless. The user's question is *which*
    tag — and after a rename the answer is the one they just renamed."""
    from packsmith.core.bindings import resolve_step

    jobs = JobStore(user_db)
    job = jobs.create("legacy")
    # A legacy NAME-based binding: a rename genuinely breaks these, unlike id-based ones.
    jobs.add_action_step(job.id, "removal:nuke", bindings={"target": "remove"})
    tags.rename(REG, "remove", "remove_hehe")

    step = jobs.get(job.id).steps[0]
    with pytest.raises(ValueError, match="'remove'"):
        resolve_step(MANIFEST, bindings=step.bindings, config={}, tag_store=tags)


def test_problems_are_knowable_without_running_anything(tags, bound):
    from packsmith.core.bindings import step_problems

    jobs, job = bound
    assert step_problems(jobs.get(job.id), package_index=Index(), tag_store=tags) == []

    tags.rename(REG, "remove", "cull")

    problems = step_problems(jobs.get(job.id), package_index=Index(), tag_store=tags)
    assert [p.kind for p in problems] == ["relink"]
    assert problems[0].needs_relink
    assert "was bound to 'remove'" in problems[0].detail


def test_a_legacy_binding_reads_as_broken_not_as_a_relink(tags, user_db):
    """The two need different things from the user: a relink is one click, a broken
    binding needs a real re-bind. Merging them would promise a fix that doesn't work."""
    from packsmith.core.bindings import step_problems

    jobs = JobStore(user_db)
    job = jobs.create("legacy")
    jobs.add_action_step(job.id, "removal:nuke", bindings={"target": "remove"})
    tags.rename(REG, "remove", "cull")

    problems = step_problems(jobs.get(job.id), package_index=Index(), tag_store=tags)
    assert [p.kind for p in problems] == ["broken"]
    assert not problems[0].needs_relink
    assert "'remove'" in problems[0].detail


def test_an_ordinary_stale_step_reports_exactly_one_problem(tags, bound):
    """An id-bound step still *resolves* after a rename — the id is fine — so the relink
    is the only thing wrong and the only thing said."""
    from packsmith.core.bindings import step_problems

    jobs, job = bound
    tags.rename(REG, "remove", "cull")
    assert len(step_problems(jobs.get(job.id), package_index=Index(), tag_store=tags)) == 1


def test_a_step_that_is_both_renamed_and_broken_says_both(user_db):
    """The trap in collapsing these: relinking would NOT fix a missing required value, so
    reporting only the relink offers a one-click fix that leaves the step unrunnable."""
    from packsmith.core.bindings import step_problems

    manifest = ActionManifest(
        package_name="p", action_id="a", file="a.star", function="run",
        mappings={"tier": MappingSlot(name="tier", kind="tag", tag_type="enum",
                                      registry_type=REG, requires_values=("late",))})

    class OneAction:
        def get(self, ref):
            return {"p:a": manifest}[ref]

    tags = TagStore(user_db)
    tags.define(REG, "tier", "enum", enum_values=["early", "late"])
    jobs = JobStore(user_db)
    job = jobs.create("j")
    bindings = {"tier": binding_id(manifest.mappings["tier"], "tier", tag_store=tags)}
    jobs.add_action_step(job.id, "p:a", bindings=bindings,
                         bound_names=record_names(manifest, bindings, tag_store=tags))

    tags.rename(REG, "tier", "phase")
    tags.set_enum_values(REG, "phase", ["early"])       # drops a value the action declares

    kinds = [p.kind for p in step_problems(jobs.get(job.id), package_index=OneAction(),
                                           tag_store=tags)]
    assert sorted(kinds) == ["broken", "relink"],         "a relink was offered as if it would fix a step it cannot fix"


def test_an_uninstalled_action_is_a_problem_too(tags, user_db):
    from packsmith.core.bindings import step_problems

    jobs = JobStore(user_db)
    job = jobs.create("orphaned")
    jobs.add_action_step(job.id, "gone:missing", bindings={})
    problems = step_problems(jobs.get(job.id), package_index=Index(), tag_store=tags)
    assert [p.kind for p in problems] == ["broken"]
    assert "not installed" in problems[0].detail
