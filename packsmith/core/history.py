"""Run history and rollback (design 3.3, 6.1).

Every action step the runner executes is recorded as a ``step_runs`` row, along with
the inverse of everything it committed — prior L2 (value, owner) per cell and prior
file (content, ownership) per path. That record is enough to reverse a committed step:
L2 by re-asserting the prior values, files by restoring the prior bytes (or deleting a
file that didn't exist before).

Surfacing this in the UI is a later slice; the recording + the rollback primitive live
here now.
"""
import json
from datetime import datetime, timezone


class StepRunStore:
    """Records and reads back ``step_runs``."""

    def __init__(self, db):
        self._db = db

    def record(self, *, action_ref, status, reason, started_at, finished_at,
               rollback_data, log_output,
               job_run_id=None, step_id=None, position_in_run=None) -> int:
        """Record one executed action step. The job_* fields link it to the run it was
        part of (design 3.3.3); they're None for a standalone action run."""
        cur = self._db.execute(
            """INSERT INTO step_runs
                   (action_ref, status, reason, started_at, finished_at, rollback_data,
                    log_output, job_run_id, step_id, position_in_run)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_ref, status, reason, started_at, finished_at,
             json.dumps(rollback_data), json.dumps(log_output),
             job_run_id, step_id, position_in_run),
        )
        return cur.lastrowid

    def mark_rolled_back(self, run_id: int):
        """Flag a step as reversed (design 3.3.3's `rolled_back` status). Its **inverse** is
        cleared with it, so the same step can't be rolled back twice.

        The change record survives. History is a record of what happened, and undoing a step
        does not unhappen it — a report of a rolled-back run still has to be able to say
        what it did, which is most of why you would open one.
        """
        row = self.get(run_id) or {}
        try:
            kept = json.loads(row.get("rollback_data") or "{}").get("changes", [])
        except (TypeError, ValueError):
            kept = []
        self._db.execute(
            "UPDATE step_runs SET status = 'rolled_back', rollback_data = ? WHERE id = ?",
            (json.dumps({"l2": [], "files": {}, "blueprints": [], "changes": kept}),
             run_id))

    def for_job_run(self, job_run_id: int) -> list[dict]:
        return [dict(r) for r in self._db.fetch_all(
            "SELECT * FROM step_runs WHERE job_run_id = ? ORDER BY position_in_run, id",
            (job_run_id,))]

    def get(self, run_id: int):
        row = self._db.fetch_one("SELECT * FROM step_runs WHERE id = ?", (run_id,))
        return dict(row) if row else None

    def list(self) -> list[dict]:
        return [dict(r) for r in self._db.fetch_all("SELECT * FROM step_runs ORDER BY id DESC")]

    def latest_for_steps(self, step_ids) -> dict:
        """The most recent run of each of these job steps, as ``{step_id: row}``.

        What the job editor puts on each row, and the reason is the loop it serves: the
        editor is where you *change* a step, so it is where "what did this do last time"
        belongs. Sending the user to another surface to find that out is what makes an
        editing screen feel like a form rather than a workbench.

        Highest id wins rather than latest `finished_at`: ids are monotonic and always
        present, while a run killed mid-flight never gets a finish time and would then
        outrank nothing.
        """
        wanted = [int(i) for i in step_ids if i is not None]
        if not wanted:
            return {}
        holes = ",".join("?" * len(wanted))
        rows = self._db.fetch_all(
            f"SELECT * FROM step_runs WHERE step_id IN ({holes}) "
            f"AND id IN (SELECT MAX(id) FROM step_runs WHERE step_id IN ({holes}) "
            f"GROUP BY step_id)", tuple(wanted) * 2)
        return {row["step_id"]: dict(row) for row in rows}


class JobRunStore:
    """Records job executions (design 3.3.3). ``job_name`` is denormalized so history
    survives the job being deleted — history is a record of what happened, not a pointer
    to what still exists."""

    def __init__(self, db):
        self._db = db

    def start(self, job_id, job_name) -> int:
        cur = self._db.execute(
            "INSERT INTO job_runs (job_id, job_name, started_at, status) VALUES (?, ?, ?, 'running')",
            (job_id, job_name, datetime.now(timezone.utc).isoformat()))
        return cur.lastrowid

    def finish(self, run_id: int, status: str):
        self._db.execute(
            "UPDATE job_runs SET finished_at = ?, status = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), status, run_id))

    def get(self, run_id: int):
        row = self._db.fetch_one("SELECT * FROM job_runs WHERE id = ?", (run_id,))
        return dict(row) if row else None

    def list(self) -> list[dict]:
        return [dict(r) for r in self._db.fetch_all(
            "SELECT * FROM job_runs ORDER BY id DESC")]


def _root_store(files, root):
    """The store for one root. `files` may be a `FileRoots` or a bare `FileStore`, because
    plenty of callers legitimately have only the instance and no registry."""
    if root and hasattr(files, "store"):
        return files.store(root)
    return files


def rollback_step(run_id: int, *, tag_store, history, file_store=None,
                  blueprint_store=None):
    """Reverse a committed step, restoring every engine to its pre-step state.

    Requires a ``file_store`` if the step wrote any files, and a ``blueprint_store`` if it
    touched blueprints — a step whose writes can't all be undone must refuse rather than
    half-undo, or "rolled_back" becomes a lie about the state of the pack.

    Raises KeyError for an unknown run.
    """
    run = history.get(run_id)
    if run is None:
        raise KeyError(f"No step run with id {run_id}")
    data = json.loads(run["rollback_data"])

    # L2: re-assert each cell's prior state.
    for inv in data.get("l2", []):
        registry_type, entry_id, tag_name = inv["key"]
        if inv["existed"]:
            tag_store.assign(registry_type, entry_id, tag_name, inv["value"],
                             owner=inv["owner_kind"], owner_action_ref=inv["owner_ref"])
        else:
            tag_store.unassign(registry_type, entry_id, tag_name)

    # Files: restore each path's prior bytes + ownership (or delete if it didn't exist).
    file_snapshots = data.get("files", {})
    if file_snapshots and file_store is None:
        raise ValueError("this step wrote files; rollback needs a file_store")
    for stored_key, snap in file_snapshots.items():
        # The key is root-qualified since 6.6 and was a bare path before it; the path is
        # carried inside now, so the key never has to be parsed back apart.
        path = snap.get("path", stored_key)
        # The root travels with the snapshot (design 6.6). Restoring by path alone would
        # put the bytes back under the instance regardless of where they came from — and a
        # record written before roots existed has none, which reads as the instance,
        # which is the only place it could have been.
        _root_store(file_store, snap.get("root")).restore(
            path, snap["content"], snap["ownership"])

    # Blueprints: same shape as L2, plus instances the step brought into existence.
    blueprint_inverse = data.get("blueprints", [])
    if blueprint_inverse and blueprint_store is None:
        raise ValueError("this step wrote blueprints; rollback needs a blueprint_store")
    # Bindings before instances, because an instance the step created has to be emptied
    # before it can be removed — and the bindings that emptied it are in this same list.
    for inv in [i for i in blueprint_inverse if i.get("kind") == "binding"]:
        blueprint, instance, slot_path = inv["key"]
        if inv["existed"]:
            blueprint_store.bind(blueprint, instance, slot_path, inv["value"],
                                 owner=inv["owner_kind"], action_ref=inv["owner_ref"])
        else:
            blueprint_store.unbind(blueprint, instance, slot_path)
    for inv in [i for i in blueprint_inverse if i.get("kind") == "instance"]:
        blueprint, instance = inv["key"]
        if not inv["existed"]:
            blueprint_store.delete_instance(blueprint, instance)
