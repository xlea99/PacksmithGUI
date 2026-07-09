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


class StepRunStore:
    """Records and reads back ``step_runs``."""

    def __init__(self, db):
        self._db = db

    def record(self, *, action_ref, status, reason, started_at, finished_at,
               rollback_data, log_output) -> int:
        cur = self._db.execute(
            """INSERT INTO step_runs
                   (action_ref, status, reason, started_at, finished_at, rollback_data, log_output)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (action_ref, status, reason, started_at, finished_at,
             json.dumps(rollback_data), json.dumps(log_output)),
        )
        return cur.lastrowid

    def get(self, run_id: int):
        row = self._db.fetch_one("SELECT * FROM step_runs WHERE id = ?", (run_id,))
        return dict(row) if row else None

    def list(self) -> list[dict]:
        return [dict(r) for r in self._db.fetch_all("SELECT * FROM step_runs ORDER BY id DESC")]


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
