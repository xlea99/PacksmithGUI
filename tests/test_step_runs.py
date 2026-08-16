"""Running part of a job, and muting part of a job (design 3.3.2).

Both exist for the authoring loop: iterating on step 4 of 6 should not mean executing —
and re-applying — steps 1 to 3 every time, and getting past a broken step should not mean
deleting it and losing its bindings.

What is tested here is the set of consequences that fail **quietly**. A single-step run
that ran the wrong number of steps announces itself in the report; a pre-flight gate that
refuses for a reason belonging to a step you are not running looks exactly like a step you
genuinely need to fix, and a migration that reads existing steps as muted produces jobs
that run clean and do nothing at all.
"""
import sqlite3

import pytest

from packsmith.core.db import UserDB
from packsmith.core.files import FileStore
from packsmith.core.history import StepRunStore
from packsmith.core.job_runner import run_job
from packsmith.core.jobs import JobStore
from packsmith.core.packages import PackageIndex
from packsmith.core.tags import TagStore

REG = "minecraft:item"

MANIFEST = """
[package]
name = "chain"

[[actions]]
id = "mark"
file = "chain.star"
function = "mark"

[actions.mappings.flag]
kind = "tag"
tag_type = "bool"
registry_type = "minecraft:item"
access = "read_write"
conflict_policy = "overwrite"

[[actions]]
id = "harvest"
file = "chain.star"
function = "harvest"

[actions.mappings.flag]
kind = "tag"
tag_type = "bool"
registry_type = "minecraft:item"
access = "read"

[actions.mappings.note]
kind = "tag"
tag_type = "string"
registry_type = "minecraft:item"
access = "write"
conflict_policy = "overwrite"

[[actions]]
id = "explode"
file = "chain.star"
function = "explode"
"""

SOURCE = '''
def mark(pack):
    flag = pack.step.mappings["flag"]
    for entry in pack.registry.entries("minecraft:item"):
        pack.tags.write("minecraft:item", entry, flag, True)

def harvest(pack):
    flag = pack.step.mappings["flag"]
    note = pack.step.mappings["note"]
    found = pack.tags.query("minecraft:item", flag, True)
    for entry in found:
        pack.tags.write("minecraft:item", entry, note, "seen")

def explode(pack):
    fail("nope")
'''


class Dump:
    registry = {REG: {"values": ["rope", "stone"]}}

    def attribute(self, *a):
        return None


@pytest.fixture
def packages(tmp_path):
    root = tmp_path / "packages" / "chain"
    root.mkdir(parents=True)
    (root / "manifest.toml").write_text(MANIFEST, encoding="utf-8")
    (root / "chain.star").write_text(SOURCE, encoding="utf-8")
    return PackageIndex(tmp_path / "packages")


@pytest.fixture
def world(user_db, tmp_path):
    tags = TagStore(user_db)
    tags.define(REG, "flag", "bool")
    tags.define(REG, "note", "string")
    root = tmp_path / "instance"
    root.mkdir()
    return tags, FileStore(user_db, root), JobStore(user_db)


def chained_job(jobs, tags):
    """mark → harvest: the second step consumes the first step's output."""
    job = jobs.create("chain")
    flag = tags.definition(REG, "flag")["id"]
    note = tags.definition(REG, "note")["id"]
    jobs.add_action_step(job.id, "chain:mark", bindings={"flag": flag})
    jobs.add_action_step(job.id, "chain:harvest",
                         bindings={"flag": flag, "note": note})
    return jobs.get(job.id)


def go(job, world, packages, **kw):
    tags, files, jobs = world
    return run_job(job, job_store=jobs, package_index=packages, tag_store=tags,
                   packdump=Dump(), file_store=files, **kw)


# --- running one step ----------------------------------------------------------------------

def test_only_the_named_step_runs(world, packages):
    tags, _files, jobs = world
    job = chained_job(jobs, tags)

    result = go(job, world, packages, only_step=job.steps[0].id)

    assert [r.action_ref for r in result.step_results] == ["chain:mark"]
    assert result.only_step == job.steps[0].id


def test_a_single_step_run_reads_committed_state_not_a_pretend_predecessor(world, packages):
    """Running step 2 alone must see the world as it actually is.

    The shared staging buffer makes step 2 see step 1 *within one run* — that is the dry-run
    guarantee. It must not leak into the other direction: asked for step 2 by itself, step 1
    did not happen, so `harvest` should find only what is genuinely committed. Otherwise a
    single-step run would preview a world nobody is in.
    """
    tags, _files, jobs = world
    job = chained_job(jobs, tags)

    alone = go(job, world, packages, only_step=job.steps[1].id)

    assert alone.status == "success"
    assert alone.changes == [], "harvest found flags that nothing had written"

    go(job, world, packages, only_step=job.steps[0].id)     # now commit step 1
    after = go(job, world, packages, only_step=job.steps[1].id)

    assert sorted({c["entry_id"] for c in after.changes}) == ["rope", "stone"]


def test_another_steps_relink_does_not_block_a_single_step_run(world, packages):
    """The narrowed gate, and the reason it is narrowed.

    §3.2.1's pre-flight gate is deliberately wide — one stale binding stops the whole job —
    because a job that halts midway is half-applied. A single-step run has no halves, so
    the wide gate would only be refusing the thing the user asked for on account of a step
    they did not ask about. That reads as "this step is broken" when it isn't, and it
    blocks the exact loop the feature exists to serve.
    """
    tags, _files, jobs = world
    job = chained_job(jobs, tags)
    flag = tags.definition(REG, "flag")["id"]
    # Step 1 now owes a relink: bound to the same tag, under a name it no longer has.
    jobs.update_step(job.steps[0].id, bound_names={"flag": "was_called_this"})
    job = jobs.get(job.id)

    whole = go(job, world, packages)
    assert whole.blocked, "the wide gate should still stop a whole-job run"

    single = go(job, world, packages, only_step=job.steps[1].id)
    assert not single.blocked
    assert [r.action_ref for r in single.step_results] == ["chain:harvest"]

    # And the gate still applies to the step that actually owes the relink.
    owed = go(job, world, packages, only_step=job.steps[0].id)
    assert owed.blocked, "the step that owes a relink must still refuse"

    del flag


def test_asking_for_a_step_that_is_not_in_the_job_says_so(world, packages):
    tags, _files, jobs = world
    job = chained_job(jobs, tags)

    result = go(job, world, packages, only_step=99999)

    assert result.status == "failed"
    assert "not in this job" in result.reason
    assert result.step_results == []


# --- muting --------------------------------------------------------------------------------

def test_a_muted_step_does_not_run(world, packages):
    tags, _files, jobs = world
    job = chained_job(jobs, tags)
    jobs.set_step_enabled(job.steps[0].id, False)

    result = go(jobs.get(job.id), world, packages)

    assert [r.action_ref for r in result.step_results] == ["chain:harvest"]
    assert tags.query(REG, filters=[{"tag": "flag", "op": "exists"}]) == []


def test_muting_the_broken_step_is_what_unblocks_the_job(world, packages):
    """The point of muting, and the thing that would make it useless.

    You mute a step precisely to get past it. If the pre-flight gate still counted a muted
    step's stale binding, muting would appear to do nothing — the job would go on refusing,
    naming a step the user has already switched off.
    """
    tags, _files, jobs = world
    job = chained_job(jobs, tags)
    jobs.update_step(job.steps[0].id, bound_names={"flag": "was_called_this"})

    assert go(jobs.get(job.id), world, packages).blocked

    jobs.set_step_enabled(job.steps[0].id, False)

    unblocked = go(jobs.get(job.id), world, packages)
    assert not unblocked.blocked
    assert unblocked.status == "success"


def test_a_muted_step_still_runs_when_asked_for_by_name(world, packages):
    """Muting says "not part of the sequence". Pointing at the row and choosing Run is not
    the sequence, and a menu item that silently did nothing would be worse than either."""
    tags, _files, jobs = world
    job = chained_job(jobs, tags)
    jobs.set_step_enabled(job.steps[0].id, False)

    result = go(jobs.get(job.id), world, packages, only_step=job.steps[0].id)

    assert [r.action_ref for r in result.step_results] == ["chain:mark"]


def test_muting_keeps_the_bindings(world, packages):
    """The whole reason this is a flag and not a delete."""
    tags, _files, jobs = world
    job = chained_job(jobs, tags)
    before = dict(job.steps[0].bindings)

    jobs.set_step_enabled(job.steps[0].id, False)
    jobs.set_step_enabled(job.steps[0].id, True)

    assert jobs.get(job.id).steps[0].bindings == before


# --- the migration -------------------------------------------------------------------------

def test_steps_written_before_the_column_existed_still_run(tmp_path):
    """The silent catastrophe.

    `enabled` arrived in schema v2. A database written before it has to come back as
    *enabled* — anything else mutes every step of every job the user already had, and a
    muted job runs cleanly and does nothing, which is the failure mode with no symptom.
    """
    path = tmp_path / "profile.db"
    db = UserDB(path)
    jobs = JobStore(db)
    job = jobs.create("legacy")
    jobs.add_action_step(job.id, "chain:mark", bindings={"flag": 1})
    db.close()

    # Rewind the file to what v1 actually looked like.
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE job_steps DROP COLUMN enabled")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    reopened = UserDB(path)
    try:
        steps = JobStore(reopened).get(job.id).steps
        assert len(steps) == 1, "the migration rebuilt the database and lost the job"
        assert steps[0].enabled is True
        assert steps[0].bindings == {"flag": 1}
    finally:
        reopened.close()


# --- what the editor reads -------------------------------------------------------------------

def test_the_latest_run_of_a_step_is_the_one_reported(user_db):
    """The job editor's Last run column. Reporting an *older* run would be worse than
    reporting none: it looks current, so it quietly answers "did my change take effect?"
    with evidence from before the change."""
    history = StepRunStore(user_db)
    common = dict(action_ref="chain:mark", reason=None, started_at="2026-08-16T10:00:00",
                  rollback_data=None, log_output=None)
    history.record(status="failed", finished_at="2026-08-16T10:00:01", step_id=7, **common)
    newest = history.record(status="success", finished_at="2026-08-16T11:00:00",
                            step_id=7, **common)
    history.record(status="success", finished_at="2026-08-16T10:30:00", step_id=8, **common)

    latest = history.latest_for_steps([7, 8])

    assert latest[7]["id"] == newest
    assert latest[7]["status"] == "success"
    assert set(latest) == {7, 8}
    assert history.latest_for_steps([]) == {}
    assert history.latest_for_steps([404]) == {}


# --- running a prefix ------------------------------------------------------------------------

def test_a_prefix_run_builds_the_world_a_lone_step_never_sees(world, packages):
    """The reason this variant exists at all.

    A job's steps see each other — one staging set spans the run — so `harvest` reading
    what `mark` wrote is the *normal* behaviour of a real run. Running `harvest` alone
    against committed state therefore previews a world where `mark` never happened, which
    on any job whose steps chain is a world nobody is in.

    Asserted as a comparison against the whole job rather than against expected values:
    the claim is that a prefix ending at the last step IS the job, and a comparison catches
    a divergence in either direction.
    """
    tags, _files, jobs = world
    job = chained_job(jobs, tags)

    alone = go(job, world, packages, only_step=job.steps[1].id, dry_run=True)
    prefix = go(job, world, packages, through_step=job.steps[1].id, dry_run=True)
    whole = go(job, world, packages, dry_run=True)

    assert alone.changes == [], "step 2 alone should see a world where step 1 never ran"
    assert [r.action_ref for r in prefix.step_results] == ["chain:mark", "chain:harvest"]
    assert prefix.changes == whole.changes, "a prefix ending at the last step IS the job"


def test_a_prefix_stops_where_it_was_told(world, packages):
    tags, _files, jobs = world
    job = chained_job(jobs, tags)

    first = go(job, world, packages, through_step=job.steps[0].id)

    assert [r.action_ref for r in first.step_results] == ["chain:mark"]
    assert first.through_step == job.steps[0].id
    # It really committed, so the flags are on disk — which is what makes a prefix run a
    # way to *set up* for the step you are iterating on.
    assert tags.query(REG, filters=[{"tag": "flag", "op": "exists"}]) != []


def test_a_prefix_skips_muted_steps_inside_it(world, packages):
    """A prefix IS the sequence, so muting means what it means everywhere else. (The menu
    doesn't offer a prefix ENDING on a muted step; a muted step in the middle is ordinary.)"""
    tags, _files, jobs = world
    job = chained_job(jobs, tags)
    jobs.set_step_enabled(job.steps[0].id, False)

    result = go(jobs.get(job.id), world, packages, through_step=job.steps[1].id)

    assert [r.action_ref for r in result.step_results] == ["chain:harvest"]


def test_a_prefix_gate_ignores_steps_it_will_not_reach(world, packages):
    """The pre-flight gate narrows to the run, not the job — the same reasoning as a
    single-step run. A relink owed by step 2 must not block a run that stops at step 1."""
    tags, _files, jobs = world
    job = chained_job(jobs, tags)
    jobs.update_step(job.steps[1].id, bound_names={"flag": "was_called_this"})
    job = jobs.get(job.id)

    assert go(job, world, packages).blocked, "the whole job should still refuse"

    prefix = go(job, world, packages, through_step=job.steps[0].id)
    assert not prefix.blocked
    assert [r.action_ref for r in prefix.step_results] == ["chain:mark"]


def test_a_prefix_that_halts_reports_where_it_stopped(world, packages):
    """A partial run has to fail like the job does: stop, and say what never ran.

    `not_run` counts only steps inside the prefix — the ones this run was going to reach.
    Counting the whole job's tail would describe casualties of a run that was never asked
    for."""
    tags, _files, jobs = world
    job = jobs.create("boom", default_on_error="halt")
    flag = tags.definition(REG, "flag")["id"]
    note = tags.definition(REG, "note")["id"]
    jobs.add_action_step(job.id, "chain:explode", bindings={})
    second = jobs.add_action_step(job.id, "chain:harvest",
                                  bindings={"flag": flag, "note": note})
    jobs.add_action_step(job.id, "chain:mark", bindings={"flag": flag})

    result = go(jobs.get(job.id), world, packages, through_step=second.id)

    assert result.status == "failed"
    assert [r.action_ref for r in result.step_results] == ["chain:explode"]
    assert result.not_run == 1, "only the rest of the PREFIX was prevented, not the job"
