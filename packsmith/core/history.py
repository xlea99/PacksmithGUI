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
        """Flag a step as reversed (design 3.3.3's `rolled_back` status). Its rollback
        data is cleared with it, so the same step can't be rolled back twice."""
        self._db.execute(
            "UPDATE step_runs SET status = 'rolled_back', rollback_data = ? WHERE id = ?",
            (json.dumps({"l2": [], "files": {}}), run_id))

    def for_job_run(self, job_run_id: int) -> list[dict]:
        return [dict(r) for r in self._db.fetch_all(
            "SELECT * FROM step_runs WHERE job_run_id = ? ORDER BY position_in_run, id",
            (job_run_id,))]

    def get(self, run_id: int):
        row = self._db.fetch_one("SELECT * FROM step_runs WHERE id = ?", (run_id,))
        return dict(row) if row else None

    def list(self) -> list[dict]:
        return [dict(r) for r in self._db.fetch_all("SELECT * FROM step_runs ORDER BY id DESC")]


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


def rollback_step(run_id: int, *, tag_store, history, file_store=None):
    """Reverse a committed step, restoring both engines to their pre-step state.

    Requires a ``file_store`` if the step wrote any files. Raises KeyError for an
    unknown run.
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
    for path, snap in file_snapshots.items():
        file_store.restore(path, snap["content"], snap["ownership"])
