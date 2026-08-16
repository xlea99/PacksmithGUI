"""What a run did, ready to render (design 3.3, 4.1).

The GUI's Run Report tab reads **this**, not a `JobResult` and not `step_runs` rows — so it
has exactly one shape to draw and cannot grow a branch for "the historical kind". Two
adapters build it:

* :func:`from_result` — a run that just happened, dry or real, straight off the runner.
* :func:`from_history` — a run read back out of `step_runs`, where the file bytes were
  dropped at persist time and only the hashes survive.

Keeping the adapters here rather than in the widget is the point. A report assembled inside
a Qt class could only be tested by building a window, and the interesting part — which
changes belong to which step, what a rolled-back run still remembers — is plain data.
"""
import json
from dataclasses import dataclass, field


@dataclass
class StepReport:
    action_ref: str
    status: str
    reason: str = None
    changes: list = field(default_factory=list)
    run_id: int = None                  # step_runs id — None for a dry run
    can_roll_back: bool = False

    @property
    def summary(self) -> dict:
        counts = {}
        for change in self.changes:
            if change["kind"] != "unchanged":
                counts[change["engine"]] = counts.get(change["engine"], 0) + 1
        return counts


@dataclass
class RunReport:
    job_name: str
    status: str
    dry_run: bool = False
    finished_at: str = ""
    not_run: int = 0
    steps: list = field(default_factory=list)     # list[StepReport]
    # What opens this report, so two views of one run share a tab. A dry run has no id, and
    # two previews of the same job ARE different answers — hence the counter rather than
    # the job id.
    key: tuple = ("run", None)

    @property
    def changes(self) -> list:
        return [change for step in self.steps for change in step.changes]

    def by_engine(self, engine: str) -> list:
        return [c for c in self.changes if c["engine"] == engine]

    def summary(self) -> dict:
        counts = {}
        for change in self.changes:
            if change["kind"] != "unchanged":
                counts[change["engine"]] = counts.get(change["engine"], 0) + 1
        return counts

    def blueprint_groups(self) -> list:
        """Blueprint changes as ``[((blueprint, instance), [changes])]``, in first-seen
        order — which is step order, and therefore the order things happened."""
        groups, order = {}, []
        for change in self.by_engine("blueprint"):
            key = (change["blueprint"], change["instance"])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(change)
        return [(key, groups[key]) for key in order]


def from_result(result, *, finished_at: str = "", key=None, label: str = None) -> RunReport:
    """A run that just happened. `JobResult` already carries everything.

    ``label`` overrides the heading only. A single-step run reads better as
    *"Full Removal · step 3"* than as the job's bare name, but the job's real name is what
    history stores and what a rollback is described against, so the override stops at the
    display and never reaches `JobResult`.
    """
    return RunReport(
        job_name=label or result.job_name,
        status=result.status,
        dry_run=getattr(result, "dry_run", False),
        finished_at=finished_at,
        not_run=result.not_run,
        steps=[StepReport(action_ref=step.action_ref, status=step.status,
                          reason=step.reason, changes=list(step.changes),
                          run_id=step.run_id,
                          # A dry run has no run_id and nothing to reverse. A real one is
                          # reversible while its inverse is intact, which is exactly what
                          # `run_id` being set means at this point.
                          can_roll_back=not getattr(result, "dry_run", False)
                          and step.run_id is not None)
               for step in result.step_results],
        key=key or ("run", None),
    )


def from_history(job_run, step_rows) -> RunReport:
    """A run read back from `step_runs`.

    The change records live inside `rollback_data` (filed there so recording them needed no
    schema migration), and a row written before that key existed simply has no changes to
    show — which is honest for an old run rather than a reason to fail.
    """
    steps = []
    for row in step_rows:
        try:
            stored = json.loads(row.get("rollback_data") or "{}")
        except (TypeError, ValueError):
            stored = {}
        # Reversible while its inverse is intact. `mark_rolled_back` spends the inverse and
        # keeps the change record, so an already-undone step still shows what it did and
        # cannot be undone twice — which is the same test the Job Results panel makes.
        inverse = any(stored.get(key) for key in ("l2", "files", "blueprints"))
        steps.append(StepReport(
            action_ref=row.get("action_ref") or "?",
            status=row.get("status") or "?",
            reason=row.get("reason"),
            changes=stored.get("changes") or [],
            run_id=row.get("id"),
            can_roll_back=bool(inverse),
        ))
    return RunReport(
        job_name=(job_run or {}).get("job_name") or "Run",
        status=(job_run or {}).get("status") or "?",
        dry_run=False,                     # a dry run is never recorded
        finished_at=(job_run or {}).get("finished_at") or "",
        steps=steps,
        key=("run", (job_run or {}).get("id")),
    )
