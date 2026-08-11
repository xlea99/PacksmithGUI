"""Jobs — user-authored sequences of action steps (design 3.3.2).

A job is **Layer 2 data, not Layer 3 code**: "the same category of artifact as a view, a
tag definition, or a blueprint schema." It composes actions *by reference*
(``package:action_id``) and never sees their code, which is why jobs are entirely
independent of what language action bodies are written in.

Two step kinds: an **action step** (invoke an action with this step's own bindings and
config) and a **job step** (invoke another job — pure organizational nesting). All
per-invocation state lives on the step, never on the action ("Model B", §3.3.2), so one
action can appear in many steps with completely independent bindings.
"""
import json
import sqlite3
from dataclasses import dataclass, field

ON_ERROR = ("halt", "skip")

# Distinguishes "don't change on_error" from "set on_error to None (inherit the job default)".
_UNSET = object()


@dataclass
class JobStep:
    id: int
    job_id: int
    position: int
    kind: str                       # "action" | "job_ref"
    action_ref: str = None          # action steps
    ref_job_id: int = None          # job_ref steps
    on_error: str = None            # None = inherit the job's default
    bindings: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    # {slot: name | [names]} — what the bound TAGS were called when this step was saved.
    # Compared against their current names to decide whether a relink is owed (3.2.1).
    bound_names: dict = field(default_factory=dict)

    @property
    def is_action(self) -> bool:
        return self.kind == "action"


@dataclass
class Job:
    id: int
    name: str
    default_on_error: str = "halt"
    pinned: bool = False
    position: int = 0
    steps: list = field(default_factory=list)   # list[JobStep], in order

    def on_error_for(self, step: JobStep) -> str:
        """A step's effective policy: its own override, else the job's default."""
        return step.on_error or self.default_on_error


@dataclass
class FlatStep:
    """One action step in execution order, with the context needed to run it.

    Produced by :meth:`JobStore.flatten` for previews and step counts. Note that
    *execution* does not use a flat list — see the note on ``flatten``.
    """
    step: JobStep
    on_error: str
    job_name: str


class JobCycleError(ValueError):
    """A job-reference step would make a job (transitively) contain itself."""


class JobStore:
    """CRUD over jobs and their steps."""

    def __init__(self, db):
        self._db = db

    # --- jobs --------------------------------------------------------------

    def all(self) -> list[Job]:
        rows = self._db.fetch_all("SELECT * FROM jobs ORDER BY position, id")
        return [self._row_to_job(r) for r in rows]

    def get(self, job_id: int) -> Job | None:
        row = self._db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._row_to_job(row) if row else None

    def get_by_name(self, name: str) -> Job | None:
        row = self._db.fetch_one("SELECT * FROM jobs WHERE name = ?", (name,))
        return self._row_to_job(row) if row else None

    @property
    def count(self) -> int:
        row = self._db.fetch_one("SELECT COUNT(*) AS n FROM jobs")
        return row["n"] if row else 0

    def create(self, name: str, *, default_on_error: str = "halt") -> Job:
        name = (name or "").strip()
        if not name:
            raise ValueError("A job needs a name")
        if default_on_error not in ON_ERROR:
            raise ValueError(f"Invalid on_error: '{default_on_error}'")
        position = self._next_position("jobs", "position")
        try:
            cur = self._db.execute(
                "INSERT INTO jobs (name, default_on_error, pinned, position) VALUES (?, ?, 0, ?)",
                (name, default_on_error, position))
        except sqlite3.IntegrityError:
            raise ValueError(f"A job named '{name}' already exists")
        return Job(id=cur.lastrowid, name=name, default_on_error=default_on_error,
                   position=position)

    def rename(self, job_id: int, name: str):
        name = (name or "").strip()
        if not name:
            raise ValueError("A job needs a name")
        try:
            self._db.execute("UPDATE jobs SET name = ? WHERE id = ?", (name, job_id))
        except sqlite3.IntegrityError:
            raise ValueError(f"A job named '{name}' already exists")

    def set_pinned(self, job_id: int, pinned: bool):
        self._db.execute("UPDATE jobs SET pinned = ? WHERE id = ?", (1 if pinned else 0, job_id))

    def set_default_on_error(self, job_id: int, policy: str):
        if policy not in ON_ERROR:
            raise ValueError(f"Invalid on_error: '{policy}'")
        self._db.execute("UPDATE jobs SET default_on_error = ? WHERE id = ?", (policy, job_id))

    def delete(self, job_id: int):
        self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def reorder(self, job_ids: list[int]):
        for position, job_id in enumerate(job_ids):
            self._db.execute("UPDATE jobs SET position = ? WHERE id = ?", (position, job_id))

    # --- steps -------------------------------------------------------------

    def steps_of(self, job_id: int) -> list[JobStep]:
        rows = self._db.fetch_all(
            "SELECT * FROM job_steps WHERE job_id = ? ORDER BY position, id", (job_id,))
        return [self._row_to_step(r) for r in rows]

    def add_action_step(self, job_id: int, action_ref: str, *, bindings: dict = None,
                        config: dict = None, on_error: str = None,
                        bound_names: dict = None) -> JobStep:
        return self._add_step(job_id, "action", action_ref=action_ref, bindings=bindings,
                              config=config, on_error=on_error, bound_names=bound_names)

    def add_job_step(self, job_id: int, ref_job_id: int, *, on_error: str = None) -> JobStep:
        """Nest another job. Rejected at creation time if it would create a cycle
        (design 3.3.2) — a job cannot contain itself, directly or transitively."""
        if ref_job_id == job_id:
            raise JobCycleError("A job cannot reference itself")
        if self._reaches(ref_job_id, job_id):
            raise JobCycleError(
                f"'{self._name_of(ref_job_id)}' already leads back to "
                f"'{self._name_of(job_id)}' — that would be a cycle")
        return self._add_step(job_id, "job_ref", ref_job_id=ref_job_id, on_error=on_error)

    def update_step(self, step_id: int, *, bindings=None, config=None, on_error=_UNSET,
                    bound_names=None):
        if bound_names is not None:
            self._db.execute("UPDATE job_steps SET bound_names = ? WHERE id = ?",
                             (json.dumps(bound_names) if bound_names else None, step_id))
        if bindings is not None:
            self._db.execute("UPDATE job_steps SET bindings = ? WHERE id = ?",
                             (json.dumps(bindings), step_id))
        if config is not None:
            self._db.execute("UPDATE job_steps SET config = ? WHERE id = ?",
                             (json.dumps(config), step_id))
        if on_error is not _UNSET:
            if on_error is not None and on_error not in ON_ERROR:
                raise ValueError(f"Invalid on_error: '{on_error}'")
            self._db.execute("UPDATE job_steps SET on_error = ? WHERE id = ?",
                             (on_error, step_id))

    def relink_step(self, step_id: int, bound_names: dict):
        """Accept a step's current bindings under their new names (design 3.2.1).

        The gesture the rename ceremony asks for. Nothing about *what* the step targets
        changes — it was always the same tag id — so this rewrites only the record of what
        that tag was called, which is what the run-time comparison consults. Confirming is
        the point: the user is asserting the step still means what they want it to mean.
        """
        self._db.execute("UPDATE job_steps SET bound_names = ? WHERE id = ?",
                         (json.dumps(bound_names) if bound_names else None, step_id))

    def remove_step(self, step_id: int):
        self._db.execute("DELETE FROM job_steps WHERE id = ?", (step_id,))

    def reorder_steps(self, job_id: int, step_ids: list[int]):
        for position, step_id in enumerate(step_ids):
            self._db.execute("UPDATE job_steps SET position = ? WHERE id = ? AND job_id = ?",
                             (position, step_id, job_id))

    # --- execution planning ------------------------------------------------

    def flatten(self, job_id: int) -> list[FlatStep]:
        """The action steps this job would run, in order, with job-references expanded.

        For **previews and counts**. Execution deliberately does *not* consume a flat
        list: §3.3.2 scopes a `halt` to the job it occurred in and then lets the parent's
        policy for that job-reference decide whether to continue. Flattening discards the
        nesting that rule depends on, so the runner recurses instead.
        """
        out = []
        self._flatten_into(job_id, out, seen=set())
        return out

    def _flatten_into(self, job_id, out, seen):
        if job_id in seen:      # defensive: creation prevents cycles, but never loop forever
            return
        seen = seen | {job_id}
        job = self.get(job_id)
        if job is None:
            return
        for step in job.steps:
            if step.is_action:
                out.append(FlatStep(step, job.on_error_for(step), job.name))
            elif step.ref_job_id is not None:
                self._flatten_into(step.ref_job_id, out, seen)

    # --- helpers -----------------------------------------------------------

    def _add_step(self, job_id, kind, *, action_ref=None, ref_job_id=None,
                  bindings=None, config=None, on_error=None, bound_names=None) -> JobStep:
        if on_error is not None and on_error not in ON_ERROR:
            raise ValueError(f"Invalid on_error: '{on_error}'")
        if self.get(job_id) is None:
            raise ValueError(f"No job with id {job_id}")
        row = self._db.fetch_one(
            "SELECT MAX(position) AS p FROM job_steps WHERE job_id = ?", (job_id,))
        position = (row["p"] + 1) if row and row["p"] is not None else 0
        cur = self._db.execute(
            """INSERT INTO job_steps (job_id, position, kind, action_ref, ref_job_id,
                                      on_error, bindings, config, bound_names)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, position, kind, action_ref, ref_job_id, on_error,
             json.dumps(bindings or {}), json.dumps(config or {}),
             json.dumps(bound_names) if bound_names else None))
        return JobStep(id=cur.lastrowid, job_id=job_id, position=position, kind=kind,
                       action_ref=action_ref, ref_job_id=ref_job_id, on_error=on_error,
                       bindings=dict(bindings or {}), config=dict(config or {}),
                       bound_names=dict(bound_names or {}))

    def _reaches(self, from_job_id: int, target_job_id: int) -> bool:
        """Is target reachable by following job-references out of from_job?"""
        stack, seen = [from_job_id], set()
        while stack:
            current = stack.pop()
            if current == target_job_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            for row in self._db.fetch_all(
                    "SELECT ref_job_id FROM job_steps WHERE job_id = ? AND kind = 'job_ref'",
                    (current,)):
                if row["ref_job_id"] is not None:
                    stack.append(row["ref_job_id"])
        return False

    def _name_of(self, job_id) -> str:
        job = self.get(job_id)
        return job.name if job else f"#{job_id}"

    def _next_position(self, table, column) -> int:
        row = self._db.fetch_one(f"SELECT MAX({column}) AS p FROM {table}")
        return (row["p"] + 1) if row and row["p"] is not None else 0

    def _row_to_job(self, row) -> Job:
        return Job(
            id=row["id"], name=row["name"],
            default_on_error=row["default_on_error"],
            pinned=bool(row["pinned"]), position=row["position"],
            steps=self.steps_of(row["id"]),
        )

    @staticmethod
    def _row_to_step(row) -> JobStep:
        return JobStep(
            id=row["id"], job_id=row["job_id"], position=row["position"], kind=row["kind"],
            action_ref=row["action_ref"], ref_job_id=row["ref_job_id"],
            on_error=row["on_error"],
            bindings=json.loads(row["bindings"]) if row["bindings"] else {},
            config=json.loads(row["config"]) if row["config"] else {},
            bound_names=json.loads(row["bound_names"]) if row["bound_names"] else {},
        )
