"""Executing a job (design 3.3.2).

The load-bearing simplification, straight from the design: **jobs are not transactions.**
Each *action step* is its own independent transaction (which ``run_action`` already
provides), so running a job is sequencing, not distributed-transaction management. If
step 2 of 4 fails under ``halt``, step 1's writes stay committed and steps 3–4 don't run
— the user sees the exact stopping point and can roll back step 1 individually.

Execution **recurses** rather than consuming a flat step list, because §3.3.2 scopes a
`halt` to the job it happened in: a failure inside a referenced job stops *that* job, and
then the parent's `on_error` for that job-reference step decides whether the parent
carries on. A flat list throws away the nesting that rule needs.

Runs are synchronous for now, and it is worth being precise about what that does and
doesn't satisfy. §3.3 asks for two things: only one step executing at a time, and *"if the
user triggers a second job while one is already running, the new run is queued"*. The first
holds — but by paralysis, not by mechanism: the UI thread blocks, so a second trigger
cannot arrive. **There is no queue.** §3.3.2 wants a background thread with a global one;
that waits until something actually runs long enough to hurt (SQLite connections are
per-thread, so it isn't free), and whoever builds it is implementing the queue for the
first time rather than moving an existing one.

Synchronous has since been **decided rather than merely tolerated** (Open Questions, async
job runs): a run cannot safely permit a packdump adopt, a profile switch, a second job or
L2 edits, so threading would buy a responsive window with nothing safe to do in it. What
blocking genuinely costs is that the OS marks the app Not Responding, at which point working
and crashed look identical — and that is what ``on_progress`` addresses, without any of the
concurrency. The runner stays GUI-free: it calls a plain function and never learns that the
GUI's handler repaints.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from packsmith.core.bindings import (
    resolve_step, conflict_policies_for, stale_bindings)
from packsmith.core.runner import run_action, StepResult
from packsmith.common.logging import log


@dataclass
class JobResult:
    job_id: int
    job_name: str
    status: str                       # "success" | "partial" | "failed"
    step_results: list = field(default_factory=list)   # list[StepResult], in run order
    run_id: int | None = None         # job_runs id, if recorded
    not_run: int = 0                  # steps never reached because something halted
    blocked: list = field(default_factory=list)   # StaleBinding: relinks owed (3.2.1)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def failed_steps(self) -> list:
        return [r for r in self.step_results if not r.ok]


@dataclass(frozen=True)
class StepProgress:
    """Reported as each action step begins and ends (design 3.3.2).

    Exists so a caller can say *what is happening* while it happens. The runner stays
    GUI-free — it calls a plain function and knows nothing about what that function does;
    the GUI's handler is where "repaint the window" lives.

    ``phase`` is "start" before the step runs and "done" after, and both are reported: the
    interesting moment for a slow step is the one *before* it, because that is when you find
    out which step is slow.
    """
    phase: str                  # "start" | "done"
    position: int               # 1-based, counting action steps only
    total: int                  # action steps reachable from this job, 0 if unknown
    action_ref: str
    result: object = None       # the StepResult, on "done"


def count_action_steps(job, job_store, seen=frozenset()) -> int:
    """How many action steps this job can reach, following nested jobs once each.

    A *total*, not a prediction: a step that halts the run means fewer actually execute.
    Reporting "3 of 7" and stopping at 4 is honest; claiming 4 was the total all along
    would not be.
    """
    if job is None or job.id in seen:
        return 0
    seen = seen | {job.id}
    total = 0
    for step in job.steps:
        if step.is_action:
            total += 1
        elif step.ref_job_id:
            total += count_action_steps(job_store.get(step.ref_job_id), job_store, seen)
    return total


def run_job(job, *, job_store, package_index, tag_store, packdump,
            file_store=None, history=None, job_history=None,
            blueprint_store=None, pack_targets=None, on_progress=None) -> JobResult:
    """Run every step of ``job`` in order, honouring each step's error policy.

    Returns a JobResult; never raises for a failing step — failures are captured, exactly
    like ``run_action``. ``history`` records each action step; ``job_history`` records the
    run itself and links the steps to it.
    """
    # PRE-FLIGHT, before a run is even recorded. §3.2.1 requires a step bound to a renamed
    # tag to refuse until relinked; checking that per-step as we reach it would mean steps
    # 1 and 2 have already applied their changes when step 3 refuses — a half-applied job,
    # which is the exact state the staging and rollback design exists to prevent. Learning
    # "won't start, here's why" is strictly better than stopping two-thirds of the way in.
    #
    # This is deliberately wider than 3.2.1's letter, which gates the step: one stale
    # binding blocks the whole job, including its unrelated steps. That trade is recorded
    # in 3.2.1 — a job is the unit you press play on.
    blocked = stale_bindings(job, package_index=package_index, tag_store=tag_store)
    if blocked:
        return JobResult(job_id=job.id, job_name=job.name, status="failed",
                         not_run=len(job.steps), blocked=blocked)

    run_id = job_history.start(job.id, job.name) if job_history is not None else None
    ctx = _Context(job_store=job_store, package_index=package_index, tag_store=tag_store,
                   packdump=packdump, file_store=file_store, history=history,
                   job_run_id=run_id, blueprint_store=blueprint_store,
                   pack_targets=pack_targets, on_progress=on_progress,
                   total_steps=count_action_steps(job, job_store))

    status, _halted = _run_steps(job, ctx, seen=frozenset({job.id}))
    if job_history is not None:
        job_history.finish(run_id, status)
    return JobResult(job_id=job.id, job_name=job.name, status=status,
                     step_results=ctx.results, run_id=run_id, not_run=ctx.not_run)


@dataclass
class _Context:
    """Everything the recursion carries: services, plus the accumulating run state."""
    job_store: object
    package_index: object
    tag_store: object
    packdump: object
    file_store: object = None
    history: object = None
    job_run_id: int = None
    blueprint_store: object = None
    # Which datapacks/resource packs exist, per the active loader (design 3.3 / 8.1).
    pack_targets: object = None
    on_progress: object = None
    total_steps: int = 0
    # Counted separately from `position`, which is a history column and is also bumped by
    # steps that never ran. Progress is about what the user is watching.
    reported: int = 0
    results: list = field(default_factory=list)
    position: int = 0
    not_run: int = 0


def _run_steps(job, ctx, seen) -> tuple[str, bool]:
    """Run one job's steps. Returns (status, halted) where status is
    success | partial | failed for *this* job."""
    any_failure = False

    for index, step in enumerate(job.steps):
        policy = job.on_error_for(step)

        if step.is_action:
            _report(ctx, "start", step.action_ref)
            result = _run_action_step(step, ctx)
            _report(ctx, "done", step.action_ref, result)
            ctx.results.append(result)
            if not result.ok:
                any_failure = True
                if policy == "halt":
                    ctx.not_run += _remaining(job, index)
                    return "failed", True
            continue

        # A job-reference step: run the nested job, then apply THIS step's policy to its
        # outcome (§3.3.2's propagation rule).
        sub_job = ctx.job_store.get(step.ref_job_id) if step.ref_job_id else None
        if sub_job is None or sub_job.id in seen:
            # Missing or cyclic reference — treat as a failed step rather than exploding.
            ctx.results.append(StepResult(
                action_ref=f"job:{step.ref_job_id}", status="failed",
                reason=("job reference forms a cycle" if sub_job else
                        f"referenced job {step.ref_job_id} no longer exists")))
            any_failure = True
            if policy == "halt":
                ctx.not_run += _remaining(job, index)
                return "failed", True
            continue

        sub_status, _ = _run_steps(sub_job, ctx, seen | {sub_job.id})
        if sub_status == "failed":
            any_failure = True
            if policy == "halt":
                ctx.not_run += _remaining(job, index)
                return "failed", True
        elif sub_status == "partial":
            any_failure = True

    return ("partial" if any_failure else "success"), False


def _report(ctx, phase: str, action_ref: str, result=None) -> None:
    """Tell the caller where the run is up to.

    Best-effort on purpose: a handler that raises must never take the run down with it. A
    job left half-applied because a log widget threw would be a catastrophic trade for a
    cosmetic feature, and the staging design's promises are about *actions* failing, not
    about the observer failing.
    """
    if phase == "start":
        ctx.reported += 1
    if ctx.on_progress is None:
        return
    try:
        ctx.on_progress(StepProgress(
            phase=phase, position=ctx.reported, total=ctx.total_steps,
            action_ref=action_ref or "?", result=result))
    except Exception:
        log.warning("A job progress handler raised; the run continues", exc_info=True)


def _remaining(job, index) -> int:
    return max(0, len(job.steps) - index - 1)


def _record_failure(ctx, step, action_ref, reason) -> StepResult:
    """A step that never got as far as running still belongs in the run's history —
    otherwise a job reports 'failed' with nothing under it to explain why."""
    run_id = None
    if ctx.history is not None:
        ctx.position += 1
        now = datetime.now(timezone.utc).isoformat()
        run_id = ctx.history.record(
            action_ref=action_ref, status="failed", reason=reason,
            started_at=now, finished_at=now,
            rollback_data={"l2": [], "files": {}, "blueprints": []}, log_output=[],
            job_run_id=ctx.job_run_id, step_id=step.id, position_in_run=ctx.position,
        )
    return StepResult(action_ref=action_ref, status="failed", reason=reason, run_id=run_id)


def _run_action_step(step, ctx) -> StepResult:
    """Resolve a step's action and bindings, then run it. Resolution problems (an unbound
    required mapping, a missing action) are step failures, not crashes — a job with one
    bad step should report that step and keep its policy, not take down the run."""
    action_ref = step.action_ref
    try:
        manifest = ctx.package_index.get(action_ref)
        fn = ctx.package_index.load_callable(action_ref)
    except (KeyError, AttributeError, FileNotFoundError) as e:
        return _record_failure(ctx, step, action_ref or "?", f"could not load action: {e}")
    try:
        mappings, config = resolve_step(manifest, blueprint_store=ctx.blueprint_store,
                                        bindings=step.bindings, packdump=ctx.packdump,
                                        config=step.config, tag_store=ctx.tag_store,
                                        pack_targets=ctx.pack_targets)
    except ValueError as e:
        return _record_failure(ctx, step, action_ref, f"step is not runnable: {e}")

    ctx.position += 1
    return run_action(
        fn, tag_store=ctx.tag_store, packdump=ctx.packdump, action_ref=action_ref,
        mappings=mappings, config=config, file_store=ctx.file_store, history=ctx.history,
        blueprint_store=ctx.blueprint_store, pack_targets=ctx.pack_targets,
        conflict_policies=conflict_policies_for(manifest, mappings),
        history_context={"job_run_id": ctx.job_run_id, "step_id": step.id,
                         "position_in_run": ctx.position},
    )
