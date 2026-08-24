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
    # What the step did, in the same shape a preview reports — see `staging.classify`.
    # Empty for a failed step, which committed nothing.
    changes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def summary(self) -> dict:
        """How many changes per engine, ignoring the ones that turned out to be no-ops."""
        counts = {}
        for change in self.changes:
            if change["kind"] != "unchanged":
                counts[change["engine"]] = counts.get(change["engine"], 0) + 1
        return counts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Buffers:
    """The three staging buffers one step writes through, held together.

    Normally built per step and thrown away with it. A **dry run** builds one set for the
    whole job instead and never commits, so step 2 reads step 1's staged writes exactly as
    it would read step 1's committed ones — every read on `pack` is staged-first, so the
    action cannot tell which it got. That is what makes a multi-step dry run 1:1 with a real
    run rather than approximately so.

    A **real** run keeps its per-step buffers, deliberately. Sharing would buy it nothing —
    each step commits, which empties the buffer — and would break one thing: `snapshots` on
    the file buffer accumulates rather than resetting, so step 2's recorded rollback data
    would include step 1's files and undoing step 2 would quietly undo step 1 as well.
    """
    l2: L2Staging
    blueprints: object = None
    files: object = None

    @classmethod
    def build(cls, *, tag_store, blueprint_store=None, file_store=None) -> "Buffers":
        return cls(
            l2=L2Staging(tag_store),
            blueprints=BlueprintStaging(blueprint_store) if blueprint_store is not None
            else None,
            files=FileStaging(file_store) if file_store is not None else None,
        )

    def _each(self):
        return [b for b in (self.l2, self.blueprints, self.files) if b is not None]

    def begin_step(self) -> dict:
        return {id(b): b.begin_step() for b in self._each()}

    def rollback(self, savepoint):
        for buffer in self._each():
            buffer.rollback(savepoint[id(buffer)])

    def changes(self, since=None) -> list:
        out = self.l2.changes(None if since is None else since[id(self.l2)])
        for buffer in (self.blueprints, self.files):
            if buffer is not None:
                out += buffer.changes(None if since is None else since[id(buffer)])
        return out

    def discard(self):
        for buffer in self._each():
            buffer.discard()


def run_action(action_fn, *, tag_store, packdump, action_ref,
               mappings=None, config=None, file_store=None, history=None,
               history_context=None, conflict_policies=None,
               blueprint_store=None, pack_targets=None, package_dir=None,
               buffers=None, commit=True) -> StepResult:
    """Run a single action callable through the staging lifecycle. Returns a
    StepResult; never raises for a failing action — failures are captured.

    If ``file_store`` is given, the action can also write files via ``pack.filesystem``
    (open-world engine); both engines commit or discard together. If ``blueprint_store``
    is given, ``pack.blueprints`` is available and stages alongside tags — both are the
    closed-world Layer 2 engine, so they share the step's all-or-nothing semantics. If
    ``history`` (a StepRunStore) is given, the run is recorded — with rollback data on
    success — and the new run id comes back on the result."""
    started_at = _now()
    if buffers is None:
        buffers = Buffers.build(tag_store=tag_store, blueprint_store=blueprint_store,
                                file_store=file_store)
    l2, files, blueprints = buffers.l2, buffers.files, buffers.blueprints
    # Opens the step: resets each buffer's record of what the CURRENT step touched, and
    # snapshots what earlier steps left staged. Used for two things — undoing just this
    # step if it fails, and giving `changes()` the `before` side for a cell an earlier step
    # already wrote. On a per-step buffer the snapshot is empty and both reduce to what
    # they did before shared buffers existed.
    savepoint = buffers.begin_step()
    pack = Pack(staging=l2, file_staging=files, tag_store=tag_store, packdump=packdump,
                action_ref=action_ref, mappings=mappings, config=config,
                conflict_policies=conflict_policies,
                blueprint_staging=blueprints, blueprint_store=blueprint_store,
                pack_targets=pack_targets, package_dir=package_dir)
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
        """Undo just this step. On a shared buffer that means rolling back to the
        savepoint, not emptying it — everything earlier steps staged has to survive."""
        buffers.rollback(savepoint)

    status, reason = "success", None
    partial = False
    changes = []
    try:
        action_fn(pack)
        # Classified BEFORE anything commits, because every `before` side is read from the
        # store — once `files.commit()` has written, the prior bytes are gone and a change
        # record built afterwards would compare a file against itself.
        changes = buffers.changes(savepoint)
        if not commit:
            # A dry step is over here: its writes stay staged for the next step to read,
            # and there is nothing to promote.
            return StepResult(action_ref, "success", log_lines=pack.log_lines,
                              changes=changes)
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
    # Only on a clean commit. The classification was made before the flush and describes
    # what the step INTENDED; on a partial commit only some of it landed, so recording it
    # would have the report claim writes that were rolled back.
    if status != "success":
        changes = []
    rollback_data = (
        {"l2": l2.inverse, "files": files.snapshots if files is not None else {},
         "blueprints": blueprints.inverse if blueprints is not None else []}
        if keep_rollback else {"l2": [], "files": {}, "blueprints": []}
    )
    # Filed alongside the inverse rather than in a column of its own, so this needs no
    # SCHEMA_VERSION bump and no migration — `rollback_step` reads only the three keys it
    # knows, by name. The file bytes are dropped: a committed run can read the current file
    # off disk and use the hash to say whether it still matches, and persisting every
    # written file twice is what §1.1's note about the quick-and-dirty snapshot store warns
    # against. A dry run keeps them, because for a dry run there is nothing on disk to read.
    rollback_data["changes"] = [_without_file_bytes(c) for c in changes]
    run_id = None
    if history is not None:
        run_id = history.record(
            action_ref=action_ref, status=status, reason=reason,
            started_at=started_at, finished_at=_now(),
            rollback_data=rollback_data, log_output=pack.log_lines,
            **(history_context or {}),
        )
    return StepResult(action_ref, status, reason=reason, log_lines=pack.log_lines,
                      run_id=run_id, changes=changes)


def _without_file_bytes(change: dict) -> dict:
    """The same record with a written file's new content dropped, keeping its hash."""
    if change["engine"] != "file" or not change.get("after"):
        return change
    slimmed = dict(change)
    slimmed["after"] = {k: v for k, v in change["after"].items() if k != "value"}
    return slimmed


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
    changes: list = field(default_factory=list)    # engine-tagged, sorted, classified
    log_lines: list = field(default_factory=list)
    reason: str = None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def summary(self) -> dict:
        """How many changes per engine, ignoring the ones that turned out to be no-ops —
        the headline a preview UI would show."""
        counts = {}
        for change in self.changes:
            if change["kind"] != "unchanged":
                counts[change["engine"]] = counts.get(change["engine"], 0) + 1
        return counts


def preview_step(action_fn, *, tag_store, packdump, action_ref,
                 mappings=None, config=None, file_store=None,
                 conflict_policies=None, blueprint_store=None,
                 pack_targets=None, buffers=None) -> StepPreview:
    """Run a step and report what it would write, committing nothing.

    Deliberately NOT a separate execution path — it is literally `run_action` with
    ``commit=False``, because a preview produced by different code is a preview that can
    disagree with the run. Nothing is recorded to history either: the step didn't happen,
    and a run history that lists things that never ran is worse than no history.

    Pass ``buffers`` to preview a step **within a job**, so its writes stay staged for the
    next step to read. Without one, this previews a single step and drops what it staged.

    Two properties are worth testing against this and are (see `tests/test_preview.py`):
    running it twice from the same state must produce identical writes, and what a real
    run commits must match what the preview said. §7.4 forbids a clock and randomness in
    the capability catalog precisely to keep both true.
    """
    own = buffers is None
    if own:
        buffers = Buffers.build(tag_store=tag_store, blueprint_store=blueprint_store,
                                file_store=file_store)
    result = run_action(
        action_fn, tag_store=tag_store, packdump=packdump, action_ref=action_ref,
        mappings=mappings, config=config, file_store=file_store,
        conflict_policies=conflict_policies, blueprint_store=blueprint_store,
        pack_targets=pack_targets, buffers=buffers, commit=False, history=None)
    if own:
        # Discard AFTER reading: the buffer was the answer, and dropping it first would
        # leave nothing to report. A caller that supplied its own keeps it — that is how a
        # job-wide dry run accumulates.
        buffers.discard()
    return StepPreview(action_ref=action_ref, status=result.status, reason=result.reason,
                       changes=result.changes, log_lines=result.log_lines)
