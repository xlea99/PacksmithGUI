"""The action runner — executes one action through the staging lifecycle (design 3.3).

An action step is atomic: it either fully happens or fully doesn't. The runner
builds a fresh per-step staging buffer and a `pack`, calls the action, and then:

  * clean return         -> commit the staging (all writes land, ownership stamped)
  * `pack.fail(reason)`  -> discard the staging (nothing touched), report the reason
  * any other exception  -> discard the staging, report it as a failure

Resolving an `action_ref` to a callable lives in `packages.PackageIndex.load_callable`
(the one language-specific seam — it returns a closure that evaluates the action's
Starlark). The runner takes an already-loaded callable, so it never sees the language.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from packsmith.core.staging import BlueprintStaging, L2Staging
from packsmith.core.files import FileStaging
from packsmith.core.pack import Pack, ActionFailure


@dataclass
class StepResult:
    """The outcome of running one action step."""
    action_ref: str
    status: str                      # "success" | "failed"
    reason: str | None = None        # failure reason (from fail() or the exception)
    log_lines: list = field(default_factory=list)
    run_id: int | None = None        # step_runs id, if the run was recorded to history

    @property
    def ok(self) -> bool:
        return self.status == "success"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_action(action_fn, *, tag_store, packdump, action_ref,
               mappings=None, config=None, file_store=None, history=None,
               history_context=None, conflict_policies=None,
               blueprint_store=None) -> StepResult:
    """Run a single action callable through the staging lifecycle. Returns a
    StepResult; never raises for a failing action — failures are captured.

    If ``file_store`` is given, the action can also write files via ``pack.filesystem``
    (open-world engine); both engines commit or discard together. If ``blueprint_store``
    is given, ``pack.blueprints`` is available and stages alongside tags — both are the
    closed-world Layer 2 engine, so they share the step's all-or-nothing semantics. If
    ``history`` (a StepRunStore) is given, the run is recorded — with rollback data on
    success — and the new run id comes back on the result."""
    started_at = _now()
    l2 = L2Staging(tag_store)
    files = FileStaging(file_store) if file_store is not None else None
    blueprints = BlueprintStaging(blueprint_store) if blueprint_store is not None else None
    pack = Pack(staging=l2, file_staging=files, tag_store=tag_store, packdump=packdump,
                action_ref=action_ref, mappings=mappings, config=config,
                conflict_policies=conflict_policies,
                blueprint_staging=blueprints, blueprint_store=blueprint_store)

    def _discard():
        l2.discard()
        if files is not None:
            files.discard()
        if blueprints is not None:
            blueprints.discard()

    status, reason = "success", None
    try:
        action_fn(pack)
        # Commit — file (open-world) engine first, so a commit-time failure (e.g.
        # file_must_exist) leaves L2 uncommitted and cleanly discardable. Cross-engine
        # atomicity beyond this is the deferred crash-recovery concern (design 3.3);
        # the store-by-path snapshots exist so it can be recovered later.
        try:
            if files is not None:
                files.commit()
            l2.commit()
            if blueprints is not None:
                blueprints.commit()
        except Exception as e:
            _discard()
            status, reason = "failed", f"commit failed: {e}"
    except ActionFailure as e:
        _discard()
        status, reason = "failed", e.reason
    except Exception as e:  # a bug in the action — discard and surface, never crash the app
        _discard()
        status, reason = "failed", f"{type(e).__name__}: {e}"

    rollback_data = (
        {"l2": l2.inverse, "files": files.snapshots if files is not None else {},
         "blueprints": blueprints.inverse if blueprints is not None else []}
        if status == "success" else {"l2": [], "files": {}, "blueprints": []}
    )
    run_id = None
    if history is not None:
        run_id = history.record(
            action_ref=action_ref, status=status, reason=reason,
            started_at=started_at, finished_at=_now(),
            rollback_data=rollback_data, log_output=pack.log_lines,
            **(history_context or {}),
        )
    return StepResult(action_ref, status, reason=reason, log_lines=pack.log_lines, run_id=run_id)
