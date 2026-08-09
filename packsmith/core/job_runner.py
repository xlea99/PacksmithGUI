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

Runs are synchronous for now. §3.3.2 wants them on a background thread with a global
queue; that waits until something actually runs long enough to hurt (SQLite connections
are per-thread, so it isn't free).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from packsmith.core.bindings import resolve_step, conflict_policies_for
from packsmith.core.runner import run_action, StepResult


@dataclass
class JobResult:
    job_id: int
    job_name: str
    status: str                       # "success" | "partial" | "failed"
    step_results: list = field(default_factory=list)   # list[StepResult], in run order
    run_id: int | None = None         # job_runs id, if recorded
    not_run: int = 0                  # steps never reached because something halted

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def failed_steps(self) -> list:
        return [r for r in self.step_results if not r.ok]


def run_job(job, *, job_store, package_index, tag_store, packdump,
            file_store=None, history=None, job_history=None) -> JobResult:
    """Run every step of ``job`` in order, honouring each step's error policy.

    Returns a JobResult; never raises for a failing step — failures are captured, exactly
    like ``run_action``. ``history`` records each action step; ``job_history`` records the
    run itself and links the steps to it.
    """
    run_id = job_history.start(job.id, job.name) if job_history is not None else None
    ctx = _Context(job_store=job_store, package_index=package_index, tag_store=tag_store,
                   packdump=packdump, file_store=file_store, history=history,
                   job_run_id=run_id)

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
            result = _run_action_step(step, ctx)
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
            rollback_data={"l2": [], "files": {}}, log_output=[],
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
        mappings, config = resolve_step(manifest, bindings=step.bindings,
                                        config=step.config, tag_store=ctx.tag_store)
    except ValueError as e:
        return _record_failure(ctx, step, action_ref, f"step is not runnable: {e}")

    ctx.position += 1
    return run_action(
        fn, tag_store=ctx.tag_store, packdump=ctx.packdump, action_ref=action_ref,
        mappings=mappings, config=config, file_store=ctx.file_store, history=ctx.history,
        conflict_policies=conflict_policies_for(manifest, mappings),
        history_context={"job_run_id": ctx.job_run_id, "step_id": step.id,
                         "position_in_run": ctx.position},
    )
