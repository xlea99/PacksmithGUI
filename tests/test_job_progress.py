"""Reporting where a run is up to — design 3.3.2.

Job runs are **synchronous by decision**, not by omission. §3.3 promises they are globally
serialized, and a blocked window is that promise enforced by physics rather than by a lock
somebody has to remember to hold — with the bonus that killing Packsmith mid-run is
survivable, because the in-flight step's writes were staged and never committed.

What a blocked window actually costs is not "you can't click": nothing would be safe to
click mid-run anyway, so an async version would spend its second half making the responsive
UI unusable again. It is that after a few seconds the OS marks the app Not Responding, and
at that moment *working* and *crashed* look identical. This reporting is what closes that
gap, and it is the whole reason the runner grew a callback.

The runner stays GUI-free: it calls a plain function and knows nothing about what that
function does.
"""
import pytest

from packsmith.core.job_runner import StepProgress, count_action_steps, run_job
from packsmith.core.jobs import JobStore
from packsmith.core.packages import ActionManifest
from packsmith.core.tags import TagStore


class Packages:
    """A package index over actions defined inline, so a run needs nothing on disk."""

    def __init__(self, **actions):
        self._fns = actions
        self._manifests = {ref: ActionManifest(package_name="p", action_id=ref.split(":")[1],
                                               file="a.star", function="run")
                           for ref in actions}

    @property
    def actions(self):
        return dict(self._manifests)

    def get(self, ref):
        return self._manifests[ref]

    def load_callable(self, ref):
        return self._fns[ref]


class Dump:
    registry = {"minecraft:item": {"values": ["minecraft:stone"]}}
    mods = {}

    def attribute(self, *a):
        return None


def noop(pack):
    pack.log("info", "did a thing")


def boom(pack):
    pack.fail("nope")


@pytest.fixture
def world(user_db):
    return JobStore(user_db), TagStore(user_db)


def run(jobs, tags, job, packages, **kwargs):
    return run_job(job, job_store=jobs, package_index=packages, tag_store=tags,
                   packdump=Dump(), **kwargs)


# --- counting, honestly ------------------------------------------------------------------

def test_the_total_counts_action_steps(world):
    jobs, _ = world
    job = jobs.create("j")
    for _ in range(3):
        jobs.add_action_step(job.id, "p:a", bindings={})
    assert count_action_steps(jobs.get(job.id), jobs) == 3


def test_the_total_follows_nested_jobs(world):
    """A job step expands, so counting only the outer steps would under-report — and a
    denominator smaller than the numerator is worse than no denominator."""
    jobs, _ = world
    inner = jobs.create("inner")
    for _ in range(2):
        jobs.add_action_step(inner.id, "p:a", bindings={})
    outer = jobs.create("outer")
    jobs.add_action_step(outer.id, "p:a", bindings={})
    jobs.add_job_step(outer.id, inner.id)

    assert count_action_steps(jobs.get(outer.id), jobs) == 3


def test_a_cyclic_reference_does_not_hang_the_count():
    """Defence in depth, and it needs a stubbed store to reach: `JobStore` rejects cycles
    at creation, so one cannot be built through the real API.

    Worth guarding anyway, for the same reason `_run_steps` carries its own `seen` set
    despite that rejection — the database is a file, and a cycle arriving from outside
    Packsmith should produce a wrong count, not an unkillable recursion.
    """
    class Step:
        is_action = False
        enabled = True          # a real JobStep always has one; a muted step isn't counted
        def __init__(self, ref): self.ref_job_id = ref

    class Job:
        def __init__(self, id, ref): self.id, self.steps = id, [Step(ref)]

    a, b = Job(1, 2), Job(2, 1)
    store = type("S", (), {"get": lambda s, i: {1: a, 2: b}[i]})()
    assert count_action_steps(a, store) == 0


# --- what gets reported --------------------------------------------------------------------

def test_every_step_is_announced_before_and_after(world):
    jobs, tags = world
    job = jobs.create("j")
    jobs.add_action_step(job.id, "p:a", bindings={})
    jobs.add_action_step(job.id, "p:b", bindings={})
    seen = []

    run(jobs, tags, jobs.get(job.id), Packages(**{"p:a": noop, "p:b": noop}),
        on_progress=seen.append)

    assert [(p.phase, p.position, p.action_ref) for p in seen] == [
        ("start", 1, "p:a"), ("done", 1, "p:a"),
        ("start", 2, "p:b"), ("done", 2, "p:b")]


def test_the_start_comes_before_the_step_actually_runs(world):
    """The point of reporting `start` at all: on a slow job the thing you need to know is
    *which* step is slow, and afterwards is too late to learn it."""
    jobs, tags = world
    job = jobs.create("j")
    jobs.add_action_step(job.id, "p:slow", bindings={})
    order = []

    def slow(pack):
        order.append("ran")

    run(jobs, tags, jobs.get(job.id), Packages(**{"p:slow": slow}),
        on_progress=lambda p: order.append(p.phase))

    assert order == ["start", "ran", "done"]


def test_the_total_is_carried_on_every_report(world):
    jobs, tags = world
    job = jobs.create("j")
    for _ in range(2):
        jobs.add_action_step(job.id, "p:a", bindings={})
    seen = []

    run(jobs, tags, jobs.get(job.id), Packages(**{"p:a": noop}), on_progress=seen.append)
    assert {p.total for p in seen} == {2}


def test_the_result_rides_along_on_done(world):
    """So the caller can stream a step's log lines as it finishes rather than batching them
    all at the end — which is what makes a long run read like a log."""
    jobs, tags = world
    job = jobs.create("j")
    jobs.add_action_step(job.id, "p:a", bindings={})
    seen = []

    run(jobs, tags, jobs.get(job.id), Packages(**{"p:a": noop}), on_progress=seen.append)

    start, done = seen
    assert start.result is None
    assert done.result.ok and done.result.log_lines == [("info", "did a thing")]


def test_a_failing_step_is_still_reported_as_done(world):
    """Otherwise the last thing the user sees is "Running step 1…" for a step that already
    failed — the display would hang where the run did not."""
    jobs, tags = world
    job = jobs.create("j")
    jobs.add_action_step(job.id, "p:boom", bindings={})
    seen = []

    run(jobs, tags, jobs.get(job.id), Packages(**{"p:boom": boom}), on_progress=seen.append)

    assert [p.phase for p in seen] == ["start", "done"]
    assert not seen[-1].result.ok


def test_a_step_that_never_loads_is_still_reported(world):
    """A missing action fails before the runner reaches it. Reporting only steps that got
    as far as executing would leave the display stuck on a step that never started."""
    jobs, tags = world
    job = jobs.create("j")
    jobs.add_action_step(job.id, "p:gone", bindings={})
    seen = []

    run(jobs, tags, jobs.get(job.id), Packages(), on_progress=seen.append)
    assert [p.phase for p in seen] == ["start", "done"]


def test_nested_job_steps_are_reported_in_a_single_sequence(world):
    """Positions run 1..n across the whole tree, because that is what the user is watching —
    a counter that restarted inside a nested job would read as the run going backwards."""
    jobs, tags = world
    inner = jobs.create("inner")
    jobs.add_action_step(inner.id, "p:b", bindings={})
    outer = jobs.create("outer")
    jobs.add_action_step(outer.id, "p:a", bindings={})
    jobs.add_job_step(outer.id, inner.id)
    seen = []

    run(jobs, tags, jobs.get(outer.id), Packages(**{"p:a": noop, "p:b": noop}),
        on_progress=seen.append)

    assert [(p.position, p.action_ref) for p in seen if p.phase == "start"] == [
        (1, "p:a"), (2, "p:b")]


# --- the observer must never be able to break the run ----------------------------------------

def test_a_handler_that_raises_does_not_stop_the_job(world):
    """The trade this guards is catastrophic: a job left half-applied because a log widget
    threw. Staging protects against *actions* failing, not against the watcher failing."""
    jobs, tags = world
    job = jobs.create("j")
    jobs.add_action_step(job.id, "p:a", bindings={})
    jobs.add_action_step(job.id, "p:a", bindings={})

    def hostile(progress):
        raise RuntimeError("the log widget exploded")

    result = run(jobs, tags, jobs.get(job.id), Packages(**{"p:a": noop}),
                 on_progress=hostile)

    assert result.status == "success"
    assert len(result.step_results) == 2


def test_no_handler_at_all_is_the_normal_case(world):
    """Every existing caller passes nothing, and a run without an observer must behave
    exactly as it did before the callback existed."""
    jobs, tags = world
    job = jobs.create("j")
    jobs.add_action_step(job.id, "p:a", bindings={})
    assert run(jobs, tags, jobs.get(job.id), Packages(**{"p:a": noop})).status == "success"


# --- the runner stays GUI-free ----------------------------------------------------------------

def test_the_core_runner_does_not_import_qt():
    """The callback is a plain function; "repaint the window" lives in the GUI's handler.
    If the runner ever reaches for Qt directly, the core stops being headless-testable."""
    import ast
    import pathlib

    source = pathlib.Path("packsmith/core/job_runner.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "PySide6" not in imported
