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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from packsmith.core.bindings import (
    resolve_step, conflict_policies_for, stale_bindings)
from packsmith.core.runner import Buffers, run_action, StepResult
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
    dry_run: bool = False
    reason: str = None                # why the run never started, when nothing else says
    only_step: int = None             # set when this was a deliberate single-step run
    through_step: int = None          # set when this ran the job's first N steps

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def failed_steps(self) -> list:
        return [r for r in self.step_results if not r.ok]

    @property
    def changes(self) -> list:
        """Every change the run made, in step order. A dry run's list is what a real run's
        would be — that equivalence is the point, and `tests/test_dry_run.py` asserts it."""
        return [change for result in self.step_results for change in result.changes]

    def summary(self) -> dict:
        """How many real changes per engine — no-ops excluded, since "the action asserted a
        cell that was already right" is not something that happened to the pack."""
        counts = {}
        for change in self.changes:
            if change["kind"] != "unchanged":
                counts[change["engine"]] = counts.get(change["engine"], 0) + 1
        return counts


def describe_summary(counts: dict) -> str:
    """A run's headline, in words. "nothing" is a real and useful answer — a re-run of an
    already-applied job changing nothing is how you learn it is idempotent."""
    nouns = {"tag": "tag", "blueprint": "blueprint binding", "file": "file"}
    parts = [f"{n} {nouns.get(engine, engine)}{'s' if n != 1 else ''}"
             for engine, n in sorted(counts.items()) if n]
    return ", ".join(parts) if parts else "nothing"


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


def _narrow(job, *, only_step=None, through_step=None):
    """``job`` cut down to the steps a partial run should execute, or None if the named
    step is not in it.

    A copy rather than a mutation, and a whole Job rather than a list of steps, so
    everything downstream — the recursion, the error policies, `on_error_for`, the
    nested-job case — keeps working on the shape it already understands.
    """
    if only_step is None and through_step is None:
        return job
    if only_step is not None:
        found = next((s for s in job.steps if s.id == only_step), None)
        if found is None:
            return None
        # A muted step asked for BY NAME still runs. Muting says "not part of the
        # sequence", and running this one is not the sequence — it is a person pointing at
        # a row. The alternative is a menu item that silently does nothing, and testing the
        # step you just switched off is a normal thing to want.
        return replace(job, steps=[replace(found, enabled=True)])

    index = next((i for i, s in enumerate(job.steps) if s.id == through_step), None)
    if index is None:
        return None
    # A prefix, with muting left alone — this IS the sequence, so a muted step inside it is
    # skipped exactly as the whole job would skip it. (The menu does not offer this on a
    # muted step: "run the sequence up to and including a step you switched off" is a
    # contradiction, so it is not a question worth answering here.)
    return replace(job, steps=list(job.steps[:index + 1]))


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
        if not step.enabled:
            continue        # muted: never part of "step 3 of 7"
        if step.is_action:
            total += 1
        elif step.ref_job_id:
            total += count_action_steps(job_store.get(step.ref_job_id), job_store, seen)
    return total


def run_job(job, *, job_store, package_index, tag_store, packdump,
            file_store=None, history=None, job_history=None,
            blueprint_store=None, pack_targets=None, on_progress=None,
            dry_run=False, only_step=None, through_step=None) -> JobResult:
    """Run every step of ``job`` in order, honouring each step's error policy.

    Returns a JobResult; never raises for a failing step — failures are captured, exactly
    like ``run_action``. ``history`` records each action step; ``job_history`` records the
    run itself and links the steps to it.

    ``dry_run=True`` walks the identical path and promotes nothing. **The steps still see
    each other**: one set of staging buffers spans the whole job, so step 2 reads step 1's
    staged writes exactly as it would read step 1's committed ones — every read on `pack`
    is staged-first, so an action cannot tell which it got. A failing step rolls back to its
    own savepoint, leaving earlier steps intact, which is what a real run's per-step commits
    achieve. Nothing is recorded to history, because nothing happened.

    Two ways to run part of a job, and they answer different questions:

    ``only_step`` runs exactly one step, by id, against **committed** state. The authoring
    loop: editing step 4 of 6 should not mean executing — and re-applying — steps 1 to 3
    every time you want to see what 4 does.

    ``through_step`` runs the job from the top **up to and including** that step. This is
    the one that reproduces a real run, and the distinction matters more than it looks: a
    job's steps see each other (one staging set spans the run), so a lone step 4 previews
    a world where steps 1 to 3 never happened. On any job whose steps chain, that is a
    world nobody is in. ``only_step`` is *"the world is already how I want it, re-run this
    action"*; ``through_step`` is *"build the world from the top, then run this"*.

    Neither is a second execution model. §3.3 relegated standalone runs on the grounds that
    *"a saved job step IS a run configuration"*, and both run that configuration through the
    identical path, with the same staging, the same policies and the same records.
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
    #
    # A PARTIAL run narrows the gate to what it is actually about to run, and the wide
    # gate's own reasoning is why: it exists to stop a job half-applying, so it should cover
    # the steps in the run and no others. Blocking a deliberate one-step run because a step
    # further down owes a relink refuses the thing the user asked for over a fact about
    # something they didn't.
    scope = _narrow(job, only_step=only_step, through_step=through_step)
    if scope is None:
        missing = only_step if only_step is not None else through_step
        return JobResult(job_id=job.id, job_name=job.name, status="failed",
                         reason=f"step {missing} is not in this job", dry_run=dry_run)
    blocked = stale_bindings(scope, package_index=package_index, tag_store=tag_store)
    if blocked:
        return JobResult(job_id=job.id, job_name=job.name, status="failed",
                         not_run=len(scope.steps), blocked=blocked, dry_run=dry_run)
    job = scope

    if dry_run:
        # A dry run records nothing. §3.3's own reasoning for previews: "a run history that
        # lists things that never ran is worse than no history."
        history = job_history = None
    run_id = job_history.start(job.id, job.name) if job_history is not None else None
    ctx = _Context(job_store=job_store, package_index=package_index, tag_store=tag_store,
                   packdump=packdump, file_store=file_store, history=history,
                   job_run_id=run_id, blueprint_store=blueprint_store,
                   pack_targets=pack_targets, on_progress=on_progress,
                   total_steps=count_action_steps(job, job_store),
                   buffers=Buffers.build(tag_store=tag_store,
                                         blueprint_store=blueprint_store,
                                         file_store=file_store) if dry_run else None)

    try:
        status, _halted = _run_steps(job, ctx, seen=frozenset({job.id}))
    finally:
        if ctx.buffers is not None:
            # Whatever happens, nothing staged survives the call. The change records were
            # taken per step and are already on the results.
            ctx.buffers.discard()
    if job_history is not None:
        job_history.finish(run_id, status)
    return JobResult(job_id=job.id, job_name=job.name, status=status,
                     step_results=ctx.results, run_id=run_id, not_run=ctx.not_run,
                     dry_run=dry_run, only_step=only_step, through_step=through_step)


def _roots_of(files):
    """The tracked-root registry, if this run was given one (design 6.6).

    Duck-typed rather than a separate context field: `file_store` is already either a
    single `FileStore` or a `FileRoots`, because `FileStaging` accepts both — and a step
    holding one store legitimately has no registry, which is what makes a `folder` mapping
    refuse rather than resolve to somewhere it was not aimed.
    """
    return files if hasattr(files, "names") else None


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
    # Set only for a dry run: one staging set for the whole job, so steps see each other.
    # None means every step builds and commits its own, which is a real run.
    buffers: object = None
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
        if not step.enabled:
            # Muted (§3.3.2). Not counted as `not_run` either: that number means "never
            # reached because something halted", and reporting a step the user switched
            # off as a casualty of a failure would misdescribe both.
            continue
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
    """How many steps a halt here prevented. Muted steps are not among them — they were
    never going to run, so counting them would inflate the casualty list of a failure."""
    return sum(1 for step in job.steps[index + 1:] if step.enabled)


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


def _package_dir(ctx, manifest):
    """Where the action's package lives, or None if the index cannot say.

    Only `copy_from` needs it, and it reports its own absence clearly — so an index that
    does not answer (a Python-callable action, a test double) should lose that one method
    rather than fail the step. Asked defensively rather than by growing the interface every
    caller has to implement.
    """
    try:
        return ctx.package_index.package(manifest.package_name).root
    except (AttributeError, KeyError, TypeError):
        return None


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
                                        file_roots=_roots_of(ctx.file_store),
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
        package_dir=_package_dir(ctx, manifest),
        conflict_policies=conflict_policies_for(manifest, mappings),
        buffers=ctx.buffers, commit=ctx.buffers is None,
        history_context={"job_run_id": ctx.job_run_id, "step_id": step.id,
                         "position_in_run": ctx.position},
    )
