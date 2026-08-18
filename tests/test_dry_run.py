"""A dry run is the run, unpromoted (design 3.3).

The claim this file exists to prove:

> Given the same inputs, a dry run and a real run produce **the same set of outputs**, no
> matter how many steps. The only difference is what happens to those outputs.

The load-bearing part is *no matter how many steps*. A preview built by running each step
against the committed store would have step 2 read a world where step 1 never happened — so
a job whose second step consumes its first step's output would preview as something it is
not. One staging set spans the whole job instead, and every read on `pack` is staged-first,
so an action cannot tell a staged predecessor from a committed one.

The tests are written as **comparisons**, not as expected values. An expected-value test
pins one path and lets the other drift; comparing the two catches a divergence in either.
"""
import pytest

from packsmith.core.files import FileStore
from packsmith.core.job_runner import run_job
from packsmith.core.jobs import JobStore
from packsmith.core.packages import PackageIndex
from packsmith.core.tags import TagStore

REG = "minecraft:item"
ENTRIES = ["rope", "stone", "torch"]

MANIFEST = """
{
  "package": { "name": "chain" },
  "actions": [
    {
      "id": "mark", "file": "chain.star", "function": "mark",
      "mappings": {
        "flag": {
          "kind": "tag", "tag_type": "bool", "registry_type": "minecraft:item",
          "access": "read_write", "conflict_policy": "overwrite",
        },
      },
    },
    {
      "id": "harvest", "file": "chain.star", "function": "harvest",
      "mappings": {
        "flag": {
          "kind": "tag", "tag_type": "bool", "registry_type": "minecraft:item",
          "access": "read",
        },
        "note": {
          "kind": "tag", "tag_type": "string", "registry_type": "minecraft:item",
          "access": "write", "conflict_policy": "overwrite",
        },
      },
    },
    { "id": "explode", "file": "chain.star", "function": "explode" },
    { "id": "wipe", "file": "chain.star", "function": "wipe" },
  ],
}
"""

# `harvest` reads what `mark` wrote — the whole point. If a dry run's step 2 could not see
# step 1's writes, `harvest` would find nothing and write nothing.
SOURCE = '''
def mark(pack):
    flag = pack.step.mappings["flag"]
    for entry in pack.registry.entries("minecraft:item"):
        if entry != "torch":
            pack.tags.write("minecraft:item", entry, flag, True)

def harvest(pack):
    flag = pack.step.mappings["flag"]
    note = pack.step.mappings["note"]
    found = pack.tags.query("minecraft:item", flag, True)
    pack.log("info", "harvest saw %d" % len(found))
    for entry in found:
        pack.tags.write("minecraft:item", entry, note, "seen")
    pack.filesystem.resolve("out/harvest.json").write('{"n": %d}' % len(found))

def explode(pack):
    pack.tags.write("minecraft:item", "rope", "note", "never")
    fail("nope")

# Claims the cell and clears it, in one step — an action may only delete what it owns, so
# two steps of THIS action are the only way to stage the same delete twice.
def wipe(pack):
    pack.tags.write("minecraft:item", "rope", "note", "x")
    pack.tags.clear("minecraft:item", "rope", "note")
'''


class Dump:
    registry = {REG: {"values": ENTRIES}}

    def attribute(self, *a):
        return None


@pytest.fixture
def packages(tmp_path):
    root = tmp_path / "packages" / "chain"
    root.mkdir(parents=True)
    (root / "manifest.json5").write_text(MANIFEST, encoding="utf-8")
    (root / "chain.star").write_text(SOURCE, encoding="utf-8")
    return PackageIndex(tmp_path / "packages")


@pytest.fixture
def world(user_db, tmp_path):
    """A fresh profile: two tags, a file root, and an empty job store."""
    tags = TagStore(user_db)
    tags.define(REG, "flag", "bool")
    tags.define(REG, "note", "string")
    root = tmp_path / "instance"
    root.mkdir()
    return tags, FileStore(user_db, root), JobStore(user_db), root


def chained_job(jobs, tags, *, on_error=None):
    """mark → harvest: the second step consumes the first step's output."""
    job = jobs.create("chain", default_on_error="halt")
    flag = tags.definition(REG, "flag")["id"]
    note = tags.definition(REG, "note")["id"]
    jobs.add_action_step(job.id, "chain:mark", bindings={"flag": flag})
    jobs.add_action_step(job.id, "chain:harvest",
                         bindings={"flag": flag, "note": note}, on_error=on_error)
    return jobs.get(job.id)


def go(job, world, packages, **kw):
    tags, files, jobs, _root = world
    return run_job(job, job_store=jobs, package_index=packages, tag_store=tags,
                   packdump=Dump(), file_store=files, **kw)


# --- THE test ------------------------------------------------------------------------------

def test_a_dry_run_and_a_real_run_agree_step_for_step(world, packages):
    """The equivalence, asserted on the records themselves rather than on the end state.

    Two runs from the same starting point: one previewed, one committed. Every step's
    change list must match, which means step 2 saw step 1 in both.
    """
    tags, _files, jobs, _root = world
    job = chained_job(jobs, tags)

    dry = go(job, world, packages, dry_run=True)
    real = go(job, world, packages)

    assert dry.status == real.status == "success"
    assert len(dry.step_results) == len(real.step_results) == 2
    for previewed, committed in zip(dry.step_results, real.step_results):
        assert previewed.action_ref == committed.action_ref
        assert previewed.changes == committed.changes
    assert dry.changes == real.changes


def test_the_second_step_sees_the_first(world, packages):
    """The property the shared buffer exists for. `harvest` queries for what `mark` wrote;
    with a per-step buffer it would find nothing and the whole second step would be empty."""
    tags, _files, jobs, _root = world
    job = chained_job(jobs, tags)

    dry = go(job, world, packages, dry_run=True)

    harvest = dry.step_results[1]
    assert any("harvest saw 2" in line for _level, line in harvest.log_lines)
    notes = [c for c in harvest.changes if c.get("tag") == "note"]
    assert sorted(c["entry_id"] for c in notes) == ["rope", "stone"]


def test_a_dry_run_touches_nothing(world, packages):
    tags, files, jobs, root = world
    job = chained_job(jobs, tags)

    go(job, world, packages, dry_run=True)

    assert tags.query(REG, filters=[{"tag": "flag", "op": "exists"}]) == []
    assert tags.query(REG, filters=[{"tag": "note", "op": "exists"}]) == []
    assert not (root / "out" / "harvest.json").exists()


def test_a_dry_run_records_no_history(world, packages, user_db):
    from packsmith.core.history import JobRunStore, StepRunStore

    tags, _files, jobs, _root = world
    steps, runs = StepRunStore(user_db), JobRunStore(user_db)
    go(chained_job(jobs, tags), world, packages, dry_run=True,
       history=steps, job_history=runs)

    assert steps.list() == [] and runs.list() == []


# --- failure, which is where the savepoint earns its place ---------------------------------

def failing_job(jobs, tags, *, on_error):
    """mark → explode: step 2 always fails, so step 1's fate is the question."""
    job = jobs.create("boom", default_on_error=on_error)
    flag = tags.definition(REG, "flag")["id"]
    jobs.add_action_step(job.id, "chain:mark", bindings={"flag": flag})
    jobs.add_action_step(job.id, "chain:explode")
    return jobs.get(job.id)


@pytest.mark.parametrize("policy", ["halt", "skip"])
def test_a_failing_step_costs_only_itself(world, packages, policy):
    """A real run leaves step 1 committed and discards step 2. A shared buffer has to do the
    same, or `discard` would take step 1 with it — which is the savepoint's entire job.

    Asserted by comparison again: whatever the real run reports for step 1, the dry run must
    report the same, under both error policies.
    """
    tags, _files, jobs, _root = world
    job = failing_job(jobs, tags, on_error=policy)

    dry = go(job, world, packages, dry_run=True)
    real = go(job, world, packages)

    assert dry.status == real.status
    assert [r.status for r in dry.step_results] == [r.status for r in real.step_results]
    assert dry.step_results[0].changes == real.step_results[0].changes
    assert dry.step_results[0].changes, "step 1's staged writes were rolled back with step 2's"
    assert dry.step_results[1].changes == [] == real.step_results[1].changes


def test_a_failed_step_does_not_take_the_earlier_ones_down_with_it(world, packages):
    """The savepoint's actual job, and it takes **three** steps to see.

    With two, step 1's change record is captured before step 2 even runs, so emptying the
    whole buffer on step 2's failure leaves no trace — the assertion passes and the bug
    ships. A third step is what asks the question out loud: after step 2 fails under
    `skip`, does step 3 still see step 1?

    A real run answers yes trivially, because step 1 committed. A shared buffer only
    answers yes if a failing step rolls back to its own savepoint instead of discarding.
    """
    tags, _files, jobs, _root = world
    flag = tags.definition(REG, "flag")["id"]
    note = tags.definition(REG, "note")["id"]
    job = jobs.create("three", default_on_error="skip")
    jobs.add_action_step(job.id, "chain:mark", bindings={"flag": flag})
    jobs.add_action_step(job.id, "chain:explode")
    jobs.add_action_step(job.id, "chain:harvest",
                         bindings={"flag": flag, "note": note})
    job = jobs.get(job.id)

    dry = go(job, world, packages, dry_run=True)
    real = go(job, world, packages)

    harvest_dry, harvest_real = dry.step_results[2], real.step_results[2]
    assert any("harvest saw 2" in line for _l, line in harvest_real.log_lines)
    assert any("harvest saw 2" in line for _l, line in harvest_dry.log_lines), \
        "step 3 lost sight of step 1 when step 2 failed"
    assert harvest_dry.changes == harvest_real.changes


def test_two_steps_deleting_one_cell_are_both_reported(world, packages):
    """The sentinel trap.

    A staged delete is one shared `_DELETE` object, so deciding "did this step touch that
    key?" by comparing against the savepoint by identity answers **no** when two steps
    delete the same cell — the second step's record vanishes from a dry run while a real
    run, whose buffers are fresh per step, reports it.

    Reaching it needs the same action ref in both steps, because an action may only delete
    what it owns; `wipe` claims the cell and clears it, twice.
    """
    tags, _files, jobs, _root = world
    job = jobs.create("wipe twice", default_on_error="halt")
    jobs.add_action_step(job.id, "chain:wipe")
    jobs.add_action_step(job.id, "chain:wipe")
    job = jobs.get(job.id)

    dry = go(job, world, packages, dry_run=True)
    real = go(job, world, packages)

    assert [len(s.changes) for s in dry.step_results] == [1, 1]
    assert dry.changes == real.changes


def test_a_failing_step_leaves_nothing_of_its_own_staged(world, packages):
    """`explode` writes a note and then fails. Neither run may report that write, and the
    dry run must not leave it visible to a later step either."""
    tags, _files, jobs, _root = world
    dry = go(failing_job(jobs, tags, on_error="skip"), world, packages, dry_run=True)

    assert not any(c.get("tag") == "note" for c in dry.changes)


# --- a re-run says so ----------------------------------------------------------------------

def test_previewing_an_already_applied_job_reports_no_ops(world, packages):
    """Determinism is *given the same inputs*. Once the job has run, previewing it again
    re-derives its answer from the current state — every cell still written, every one now
    classified `unchanged`."""
    tags, _files, jobs, _root = world
    job = chained_job(jobs, tags)
    go(job, world, packages)

    again = go(job, world, packages, dry_run=True)

    assert again.status == "success"
    assert again.changes, "the steps stopped reporting what they write"
    assert {c["kind"] for c in again.changes} == {"unchanged"}
