"""Schema invariants the DDL can't state — design 9.2.

`PRAGMA foreign_keys = ON`, but three link columns added by migration carry no FK
constraint, and SQLite can't add one without rebuilding the table. That reads like an
oversight and is really two different situations, so both are pinned here instead:

- `step_runs.step_id` / `job_runs.job_name` are **historical** references. An FK would be
  actively wrong — it would either cascade-delete run history when a step is removed or
  refuse the removal. History is supposed to outlive the thing it describes.
- `blueprint_instances.orphaned_by` IS a live link, and its integrity is maintained by
  discipline at both deletion sites. Discipline is what tests are for.
"""
import pytest

from packsmith.core.blueprints import BlueprintStore
from packsmith.core.jobs import JobStore


# --- history outlives what it describes --------------------------------------------------

def _record(history, job_run_id, step_id):
    return history.record(action_ref="palette:fill", status="success", reason=None,
                          started_at="2026-08-10T00:00:00", finished_at="2026-08-10T00:00:01",
                          rollback_data={}, log_output=[],
                          job_run_id=job_run_id, step_id=step_id, position_in_run=0)


def test_run_history_survives_deleting_the_step_it_ran(user_db):
    from packsmith.core.history import JobRunStore, StepRunStore

    jobs = JobStore(user_db)
    job = jobs.create("nightly")
    step = jobs.add_action_step(job.id, "palette:fill", bindings={})
    runs, history = JobRunStore(user_db), StepRunStore(user_db)
    run_id = runs.start(job.id, job.name)
    _record(history, run_id, step.id)

    jobs.remove_step(step.id)

    recorded = history.for_job_run(run_id)
    assert len(recorded) == 1, "deleting a step erased the record that it ever ran"
    assert recorded[0]["status"] == "success"


def test_run_history_survives_deleting_the_job_itself(user_db):
    """`job_runs.job_name` is denormalized for exactly this."""
    from packsmith.core.history import JobRunStore, StepRunStore

    jobs = JobStore(user_db)
    job = jobs.create("nightly")
    runs, history = JobRunStore(user_db), StepRunStore(user_db)
    run_id = runs.start(job.id, job.name)
    _record(history, run_id, None)
    runs.finish(run_id, "success")

    jobs.delete(job.id)

    assert [r["job_name"] for r in runs.list()] == ["nightly"]
    assert len(history.for_job_run(run_id)) == 1


# --- the live link stays consistent ------------------------------------------------------

@pytest.fixture
def orphaning(user_db):
    """A retype that orphans an instance — the only thing that sets `orphaned_by`."""
    store = BlueprintStore(user_db)
    store.define("StoneType")
    store.add_slot("StoneType", "tier", "string")
    store.create_instance("StoneType", "granite")
    store.bind("StoneType", "granite", "tier", "shiny")
    store.retype_slot("StoneType", "tier", "number")
    return store


def _dangling(db) -> int:
    return db.fetch_one(
        "SELECT COUNT(*) AS n FROM blueprint_instances i "
        "WHERE i.orphaned_by IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM blueprint_mutations m WHERE m.id = i.orphaned_by)")["n"]


def test_the_fixture_actually_orphans_something(orphaning, user_db):
    assert orphaning.has_orphans("StoneType"), "fixture assumption"
    assert _dangling(user_db) == 0


def test_resolving_an_orphan_leaves_no_dangling_mutation_reference(orphaning, user_db):
    """Un-orphaning drops the mutation once nothing points at it. If the order were ever
    reversed, `orphaned_by` would point at a deleted row and the orphan JOIN would silently
    stop reporting it — an unresolved instance that looks resolved."""
    orphaning.rebind("StoneType", "granite", "tier", 3)

    assert _dangling(user_db) == 0
    assert not orphaning.has_orphans("StoneType")


def test_reverting_the_mutation_leaves_no_dangling_reference(orphaning, user_db):
    """The other deletion site: revert restores the old schema and deletes the mutation."""
    orphaning.revert("StoneType")
    assert _dangling(user_db) == 0
