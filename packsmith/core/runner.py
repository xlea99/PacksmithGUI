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
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone

from packsmith.common.logging import log
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
    if files is not None:
        files.logs_to(pack.log)     # its notices belong with the action's own output

    def _joint_transaction(l2_staging, blueprint_staging):
        """One transaction spanning every distinct database these stores write through.

        Deduplicated by identity, because tags and blueprints normally share one UserDB —
        and then this is genuinely one COMMIT across both, which is what makes an L2 flush
        that fails halfway leave nothing behind.
        """
        stack = ExitStack()
        stack.covered = []      # which databases this actually wrapped
        seen = stack.covered
        # Duck-typed through private attributes, so a rename would silently drop the
        # transaction and put per-statement commits back — the exact bug L3-2 fixed, with
        # no test failing. Log it instead of degrading quietly.
        for label, store in (("l2", getattr(l2_staging, "_tags", None)),
                             ("blueprints", getattr(blueprint_staging, "_store", None))):
            if store is None:
                if label == "l2" or blueprint_staging is not None:
                    log.warning("No %s store behind its staging — the step's flush will "
                                "not be transactional", label)
                continue
            db = getattr(store, "_db", None)
            if db is None or not hasattr(db, "transaction"):
                log.warning("The %s store exposes no transactional database — the step's "
                            "flush will not be atomic", label)
                continue
            if any(db is other for other in seen):
                continue
            seen.append(db)
            stack.enter_context(db.transaction())
        return stack

    def _discard():
        l2.discard()
        if files is not None:
            files.discard()
        if blueprints is not None:
            blueprints.discard()

    status, reason = "success", None
    partial = False
    try:
        action_fn(pack)
        # Commit — file (open-world) engine first, so a commit-time failure leaves the
        # database untouched and cleanly discardable. Files pre-flight their existence
        # requirements, so by the time bytes are written the likely failures are gone.
        transaction = None      # bound before use: files.commit() can raise first, and
        try:                    # the handler below inspects this to decide what landed
            if files is not None:
                files.commit()
            # L2 and blueprints flush inside ONE transaction. They usually share a
            # connection, in which case this is a single COMMIT across both; when they
            # don't, each is still individually all-or-nothing instead of per-statement.
            transaction = _joint_transaction(l2, blueprints)
            with transaction:
                l2.commit()
                if blueprints is not None:
                    blueprints.commit()
        except Exception as e:
            # A staging buffer builds its inverse AS IT GOES, so on a mid-flush failure it
            # describes writes that SQLite has just rolled back. Recording those would make
            # a later rollback try to undo things that never happened — and the blueprint
            # replay does that by DELETING an instance it believes the step created, which
            # raises after L2 and files have already been restored. Under a transaction
            # nothing on the database side landed, so the honest record is empty.
            if getattr(transaction, "covered", None):
                l2.inverse = []
                if blueprints is not None:
                    blueprints.inverse = []
            # Buffers are dropped, but NOT the inverse records: whatever did land is
            # exactly what those describe, and they are the only way back. The database
            # side rolls itself back; files cannot, so anything already written stays and
            # has to remain undoable.
            partial = files is not None and bool(files.snapshots)
            _discard()
            status, reason = "failed", f"commit failed: {e}"
            if partial:
                reason += (f" — {len(files.snapshots)} file(s) were already written; "
                           f"roll this step back to undo them")
    except ActionFailure as e:
        _discard()
        status, reason = "failed", e.reason
    except Exception as e:  # a bug in the action — discard and surface, never crash the app
        _discard()
        status, reason = "failed", f"{type(e).__name__}: {e}"

    # A failed step normally touches nothing, so it records nothing to undo. The exception
    # is a commit that got partway: recording an empty rollback there would strand the
    # changes that did land, with the snapshots that could restore them thrown away.
    keep_rollback = status == "success" or partial
    rollback_data = (
        {"l2": l2.inverse, "files": files.snapshots if files is not None else {},
         "blueprints": blueprints.inverse if blueprints is not None else []}
        if keep_rollback else {"l2": [], "files": {}, "blueprints": []}
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


@dataclass
class StepPreview:
    """What a step WOULD do, without having done it (design 3.3).

    The action really ran to produce this — its body executed against the same staging
    buffers a committed run uses; the buffers were simply dropped instead of promoted.
    That is the point rather than an implementation note: §3.3 requires that "the bytes
    that commit are exactly the bytes of the final, zero-conflict dry-run", which is only
    true if the preview IS the run rather than a simulation of it.
    """
    action_ref: str
    status: str                      # "success" | "failed" — could this step even get here
    writes: list = field(default_factory=list)     # engine-tagged, sorted, plain data
    log_lines: list = field(default_factory=list)
    reason: str = None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def summary(self) -> dict:
        """How many changes per engine — the headline a preview UI would show."""
        counts = {}
        for write in self.writes:
            counts[write["engine"]] = counts.get(write["engine"], 0) + 1
        return counts


def preview_step(action_fn, *, tag_store, packdump, action_ref,
                 mappings=None, config=None, file_store=None,
                 conflict_policies=None, blueprint_store=None) -> StepPreview:
    """Run a step and report what it would write, committing nothing.

    Deliberately NOT a separate execution path: this is `run_action`'s own lifecycle with
    the promotion withheld, because a preview produced by different code is a preview that
    can disagree with the run. Nothing is recorded to history either — the step didn't
    happen, and a run history that lists things that never ran is worse than no history.

    Two properties are worth testing against this and are (see `tests/test_preview.py`):
    running it twice from the same state must produce identical writes, and what a real
    run commits must match what the preview said. §7.4 forbids a clock and randomness in
    the capability catalog precisely to keep both true.
    """
    l2 = L2Staging(tag_store)
    files = FileStaging(file_store) if file_store is not None else None
    blueprints = BlueprintStaging(blueprint_store) if blueprint_store is not None else None
    pack = Pack(staging=l2, file_staging=files, tag_store=tag_store, packdump=packdump,
                action_ref=action_ref, mappings=mappings, config=config,
                conflict_policies=conflict_policies,
                blueprint_staging=blueprints, blueprint_store=blueprint_store)
    if files is not None:
        files.logs_to(pack.log)

    status, reason = "success", None
    try:
        action_fn(pack)
    except ActionFailure as e:
        status, reason = "failed", e.reason
    except Exception as e:
        status, reason = "failed", f"{type(e).__name__}: {e}"

    # A failed step commits nothing, so its preview must promise nothing. Reporting the
    # writes it managed to stage before dying would be more informative and would break the
    # one guarantee this exists for — `writes` is what a run would COMMIT, and a failed run
    # commits none of them. The log lines carry the diagnostic story instead.
    writes = []
    if status == "success":
        writes = list(l2.pending())
        if blueprints is not None:
            writes += blueprints.pending()
        if files is not None:
            writes += files.pending()

    # Discard AFTER reading: the buffers are the answer, and dropping them first would
    # leave nothing to report.
    l2.discard()
    if files is not None:
        files.discard()
    if blueprints is not None:
        blueprints.discard()

    return StepPreview(action_ref=action_ref, status=status, reason=reason,
                       writes=writes, log_lines=pack.log_lines)
