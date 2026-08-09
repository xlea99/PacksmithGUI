"""Jobs: authoring sequences of action steps, and executing them (design 3.3.2).

The two things worth real scrutiny are **cycle rejection** (a job must not contain
itself) and **error propagation** — `halt` is scoped to the job it happened in, and the
parent's policy for that job-reference then decides whether the parent continues. That
rule is the reason execution recurses instead of walking a flat list.
"""
import pytest

from packsmith.core.jobs import JobStore, JobCycleError
from packsmith.core.job_runner import run_job
from packsmith.core.history import StepRunStore, JobRunStore
from packsmith.core.packages import ActionManifest, MappingSlot

REG = "minecraft:item"


@pytest.fixture
def jobs(user_db):
    return JobStore(user_db)


# --- a fake package index: actions that succeed or fail on demand -----------

class FakeIndex:
    """Stands in for PackageIndex. Actions named '*:boom' fail; others tag their entry."""

    def __init__(self, tags):
        self._tags = tags
        self.ran = []

    def get(self, action_ref):
        if action_ref == "pkg:missing":
            raise KeyError(action_ref)
        # A realistic write mapping: declares that it writes, and how it resolves a
        # conflict — mandatory for write access (design 3.3). Several steps here write the
        # same cell, so without `overwrite` the second would (correctly) refuse.
        slots = {"target": MappingSlot("target", tag_type="bool", registry_type=REG,
                                       required=True, access="write",
                                       conflict_policy="overwrite")}
        return ActionManifest(package_name="pkg", action_id=action_ref.split(":")[1],
                              file="a.py", function="run", mappings=slots)

    def load_callable(self, action_ref):
        ran = self.ran

        def action(pack):
            ran.append(action_ref)
            pack.log("info", f"ran {action_ref}")
            if action_ref.endswith(":boom"):
                pack.fail("boom")
            pack.tags.write(REG, "quark:rope", pack.step.mappings["target"], True)
        return action


class FakeDump:
    registry = {REG: {"values": ["quark:rope"]}}

    def attribute(self, registry_type, entry_id, name):
        return None


@pytest.fixture
def world(tags, jobs, user_db):
    tags.define(REG, "done", "bool", default=False)
    return {
        "tags": tags, "jobs": jobs, "index": FakeIndex(tags), "dump": FakeDump(),
        "history": StepRunStore(user_db), "job_history": JobRunStore(user_db),
    }


def _run(world, job):
    return run_job(job, job_store=world["jobs"], package_index=world["index"],
                   tag_store=world["tags"], packdump=world["dump"],
                   history=world["history"], job_history=world["job_history"])


# --- authoring --------------------------------------------------------------

def test_create_and_add_steps(jobs):
    job = jobs.create("Cleanup")
    jobs.add_action_step(job.id, "pkg:one", bindings={"target": "done"})
    jobs.add_action_step(job.id, "pkg:two", bindings={"target": "done"})
    steps = jobs.get(job.id).steps
    assert [s.action_ref for s in steps] == ["pkg:one", "pkg:two"]
    assert [s.position for s in steps] == [0, 1]


def test_step_carries_its_own_bindings_and_config(jobs):
    """Model B: per-invocation state lives on the step, so one action can appear twice
    with different bindings."""
    job = jobs.create("Twice")
    jobs.add_action_step(job.id, "pkg:one", bindings={"target": "a"}, config={"n": 1})
    jobs.add_action_step(job.id, "pkg:one", bindings={"target": "b"}, config={"n": 2})
    a, b = jobs.get(job.id).steps
    assert (a.bindings, a.config) == ({"target": "a"}, {"n": 1})
    assert (b.bindings, b.config) == ({"target": "b"}, {"n": 2})


def test_duplicate_job_name_raises(jobs):
    jobs.create("Only")
    with pytest.raises(ValueError):
        jobs.create("Only")


def test_reorder_and_remove_steps(jobs):
    job = jobs.create("Ordered")
    one = jobs.add_action_step(job.id, "pkg:one")
    two = jobs.add_action_step(job.id, "pkg:two")
    three = jobs.add_action_step(job.id, "pkg:three")
    jobs.reorder_steps(job.id, [three.id, one.id, two.id])
    assert [s.action_ref for s in jobs.get(job.id).steps] == ["pkg:three", "pkg:one", "pkg:two"]
    jobs.remove_step(one.id)
    assert [s.action_ref for s in jobs.get(job.id).steps] == ["pkg:three", "pkg:two"]


def test_deleting_a_job_takes_its_steps(jobs, user_db):
    job = jobs.create("Doomed")
    jobs.add_action_step(job.id, "pkg:one")
    jobs.delete(job.id)
    assert user_db.fetch_one("SELECT COUNT(*) AS n FROM job_steps")["n"] == 0


def test_pinning_round_trips(jobs):
    job = jobs.create("Pin me")
    jobs.set_pinned(job.id, True)
    assert jobs.get(job.id).pinned is True


def test_step_policy_defaults_to_the_job_policy(jobs):
    job = jobs.create("Policy", default_on_error="skip")
    step = jobs.add_action_step(job.id, "pkg:one")
    override = jobs.add_action_step(job.id, "pkg:two", on_error="halt")
    job = jobs.get(job.id)
    assert job.on_error_for(job.steps[0]) == "skip"      # inherited
    assert job.on_error_for(job.steps[1]) == "halt"      # overridden


# --- cycles (design 3.3.2: rejected at creation time) ----------------------

def test_a_job_cannot_reference_itself(jobs):
    job = jobs.create("Self")
    with pytest.raises(JobCycleError):
        jobs.add_job_step(job.id, job.id)


def test_transitive_cycles_are_rejected(jobs):
    a, b, c = jobs.create("A"), jobs.create("B"), jobs.create("C")
    jobs.add_job_step(a.id, b.id)
    jobs.add_job_step(b.id, c.id)
    with pytest.raises(JobCycleError):
        jobs.add_job_step(c.id, a.id)      # would close the loop


def test_diamond_nesting_is_allowed(jobs):
    """Two jobs referencing the same child is not a cycle."""
    a, b, shared = jobs.create("A"), jobs.create("B"), jobs.create("Shared")
    jobs.add_job_step(a.id, shared.id)
    jobs.add_job_step(b.id, shared.id)
    jobs.add_job_step(a.id, b.id)          # still acyclic
    assert len(jobs.get(a.id).steps) == 2


# --- flattening -------------------------------------------------------------

def test_flatten_expands_nested_jobs_in_order(jobs):
    inner = jobs.create("Inner")
    jobs.add_action_step(inner.id, "pkg:i1")
    jobs.add_action_step(inner.id, "pkg:i2")
    outer = jobs.create("Outer")
    jobs.add_action_step(outer.id, "pkg:o1")
    jobs.add_job_step(outer.id, inner.id)
    jobs.add_action_step(outer.id, "pkg:o2")

    assert [f.step.action_ref for f in jobs.flatten(outer.id)] == [
        "pkg:o1", "pkg:i1", "pkg:i2", "pkg:o2"]


def test_flatten_resolves_each_step_against_its_own_job_policy(jobs):
    inner = jobs.create("Inner", default_on_error="skip")
    jobs.add_action_step(inner.id, "pkg:i1")
    outer = jobs.create("Outer", default_on_error="halt")
    jobs.add_action_step(outer.id, "pkg:o1")
    jobs.add_job_step(outer.id, inner.id)

    flat = {f.step.action_ref: f.on_error for f in jobs.flatten(outer.id)}
    assert flat == {"pkg:o1": "halt", "pkg:i1": "skip"}


# --- execution --------------------------------------------------------------

def test_all_steps_run_and_commit(world):
    job = world["jobs"].create("Happy")
    world["jobs"].add_action_step(job.id, "pkg:one", bindings={"target": "done"})
    world["jobs"].add_action_step(job.id, "pkg:two", bindings={"target": "done"})

    result = _run(world, world["jobs"].get(job.id))
    assert result.status == "success"
    assert world["index"].ran == ["pkg:one", "pkg:two"]
    assert world["tags"].get_tag(REG, "quark:rope", "done") is True


def test_halt_stops_the_run_but_keeps_earlier_writes(world):
    """Jobs are not transactions: step 1 stays committed when step 2 halts."""
    job = world["jobs"].create("Halting")
    world["jobs"].add_action_step(job.id, "pkg:one", bindings={"target": "done"})
    world["jobs"].add_action_step(job.id, "pkg:boom", bindings={"target": "done"},
                                  on_error="halt")
    world["jobs"].add_action_step(job.id, "pkg:three", bindings={"target": "done"})

    result = _run(world, world["jobs"].get(job.id))
    assert result.status == "failed"
    assert world["index"].ran == ["pkg:one", "pkg:boom"]     # three never ran
    assert result.not_run == 1
    assert world["tags"].get_tag(REG, "quark:rope", "done") is True   # step 1 committed


def test_skip_continues_past_a_failure_and_reports_partial(world):
    job = world["jobs"].create("Skipping", default_on_error="skip")
    world["jobs"].add_action_step(job.id, "pkg:boom", bindings={"target": "done"})
    world["jobs"].add_action_step(job.id, "pkg:two", bindings={"target": "done"})

    result = _run(world, world["jobs"].get(job.id))
    assert result.status == "partial"
    assert world["index"].ran == ["pkg:boom", "pkg:two"]
    assert len(result.failed_steps) == 1


def test_a_halt_inside_a_nested_job_is_scoped_to_that_job(world):
    """The propagation rule: the inner job halts, but the parent's `skip` policy for the
    job-reference lets the parent carry on. This is why execution recurses."""
    jobs = world["jobs"]
    inner = jobs.create("Inner", default_on_error="halt")
    jobs.add_action_step(inner.id, "pkg:boom", bindings={"target": "done"})
    jobs.add_action_step(inner.id, "pkg:never", bindings={"target": "done"})

    outer = jobs.create("Outer")
    jobs.add_action_step(outer.id, "pkg:first", bindings={"target": "done"})
    jobs.add_job_step(outer.id, inner.id, on_error="skip")
    jobs.add_action_step(outer.id, "pkg:last", bindings={"target": "done"})

    result = _run(world, jobs.get(outer.id))
    assert world["index"].ran == ["pkg:first", "pkg:boom", "pkg:last"]
    assert "pkg:never" not in world["index"].ran     # inner halted
    assert result.status == "partial"                # outer completed, with a failure


def test_a_halting_job_reference_stops_the_parent(world):
    jobs = world["jobs"]
    inner = jobs.create("Inner")
    jobs.add_action_step(inner.id, "pkg:boom", bindings={"target": "done"})

    outer = jobs.create("Outer")
    jobs.add_job_step(outer.id, inner.id, on_error="halt")
    jobs.add_action_step(outer.id, "pkg:last", bindings={"target": "done"})

    result = _run(world, jobs.get(outer.id))
    assert result.status == "failed"
    assert "pkg:last" not in world["index"].ran


def test_unbound_required_mapping_is_a_step_failure_not_a_crash(world):
    job = world["jobs"].create("Unbound")
    world["jobs"].add_action_step(job.id, "pkg:one")      # no bindings at all
    result = _run(world, world["jobs"].get(job.id))
    assert result.status == "failed"
    assert "not runnable" in result.failed_steps[0].reason


def test_a_missing_action_is_a_step_failure(world):
    job = world["jobs"].create("Missing")
    world["jobs"].add_action_step(job.id, "pkg:missing", bindings={"target": "done"})
    result = _run(world, world["jobs"].get(job.id))
    assert result.status == "failed"
    assert "could not load" in result.failed_steps[0].reason


# --- history ----------------------------------------------------------------

def test_the_run_is_recorded_and_its_steps_link_back_to_it(world):
    job = world["jobs"].create("Recorded")
    world["jobs"].add_action_step(job.id, "pkg:one", bindings={"target": "done"})
    world["jobs"].add_action_step(job.id, "pkg:two", bindings={"target": "done"})

    result = _run(world, world["jobs"].get(job.id))
    run = world["job_history"].get(result.run_id)
    assert run["status"] == "success" and run["job_name"] == "Recorded"
    assert run["finished_at"] is not None

    steps = world["history"].for_job_run(result.run_id)
    assert [s["action_ref"] for s in steps] == ["pkg:one", "pkg:two"]
    assert [s["position_in_run"] for s in steps] == [1, 2]


def test_history_survives_the_job_being_deleted(world):
    job = world["jobs"].create("Ephemeral")
    world["jobs"].add_action_step(job.id, "pkg:one", bindings={"target": "done"})
    result = _run(world, world["jobs"].get(job.id))

    world["jobs"].delete(job.id)
    run = world["job_history"].get(result.run_id)
    assert run is not None and run["job_name"] == "Ephemeral"   # name kept, FK nulled
