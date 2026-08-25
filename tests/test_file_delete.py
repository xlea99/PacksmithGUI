"""Removing a file an action wrote — the other half of idempotent generation.

A generator that only ever writes is not idempotent: drop a stone from a blueprint and its
six hundred files stay in the mod, compiled forever, describing a block nothing registers.
The fix is a sweep — write what should exist, then delete what you own here and did not
write this run — and that is only safe to hand an author if "what you own" is exact and
"delete" cannot reach past it.

So the interesting tests are the refusals, not the removals. Deleting is the one operation
where a bug costs somebody files, and every guard here is one that would be invisible until
it had already run.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.files import FileOwnershipError, FileStaging, FileStore

MINE = "p:generate"
THEIRS = "p:other"


@pytest.fixture
def store(user_db, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    return FileStore(user_db, root)


@pytest.fixture
def staging(store):
    return FileStaging(store)


def written(staging, path, content="x", *, owner="action", ref=MINE):
    staging.write(path, content, owner=owner, owner_action_ref=ref)
    staging.commit()


# --- the happy path ----------------------------------------------------------------------

def test_an_action_can_remove_its_own_file(staging, store):
    written(staging, "a/one.json")
    assert store.exists("a/one.json")

    staging.delete("a/one.json", owner_action_ref=MINE)
    staging.commit()

    assert not store.exists("a/one.json")
    assert store.ownership("a/one.json") is None, "the ownership row outlived the file"


def test_deleting_something_that_is_not_there_is_a_no_op(staging):
    """A sweep computes what it wants and removes the difference. The difference is
    allowed to be already gone — erroring would make the second run of a clean generator
    fail for being clean."""
    staging.delete("never/existed.json", owner_action_ref=MINE)
    staging.commit()          # must not raise


# --- the refusals, which are the point ----------------------------------------------------

def test_it_refuses_to_delete_a_file_the_user_owns(staging, store):
    """§6.1's hard-block, and stronger here than for a write: a user-owned file is one
    somebody edited on purpose."""
    written(staging, "yours.json", owner="user", ref=None)

    with pytest.raises(FileOwnershipError) as caught:
        staging.delete("yours.json", owner_action_ref=MINE)

    assert "owned by you" in str(caught.value)
    assert store.exists("yours.json")


def test_it_refuses_to_delete_another_ACTION_s_file(staging, store):
    """Stricter than `write`, deliberately, and this is the gap that matters.

    A write may take a file from another action — the loser re-runs and takes it back. A
    delete cannot be undone that way by anyone but its author, and it returns the path to
    "nobody ever wrote this", which §3.2.1 reserves for the user. So a sweep can never
    reach a file belonging to some other generator sharing the same tree.
    """
    written(staging, "theirs.json", ref=THEIRS)

    with pytest.raises(FileOwnershipError) as caught:
        staging.delete("theirs.json", owner_action_ref=MINE)

    assert THEIRS in str(caught.value)
    assert store.exists("theirs.json")


def test_it_refuses_to_delete_an_untracked_file(staging, store):
    """The case that reads either way, and the one I got wrong first.

    `_refuse_foreign_delete` returns quietly when nothing owns a cell, because for a TAG
    "no row" means pristine and there is nothing to clear. A file is not a row: no
    ownership record plus a file on disk means something arrived by a door Packsmith does
    not watch. "Nobody owns it" is then the strongest reason to leave it alone — there is
    no record of who would lose it.
    """
    (store.root / "stranger.json").write_text("{}", encoding="utf-8")
    assert store.ownership("stranger.json") is None

    with pytest.raises(FileOwnershipError) as caught:
        staging.delete("stranger.json", owner_action_ref=MINE)

    assert "not tracked" in str(caught.value)
    assert store.exists("stranger.json")


def test_taking_a_file_then_deleting_it_is_refused(staging, store):
    """The laundering case. Each act is legal alone — a write may take, and an action may
    retract its own work — but together they turn another action's file into one that never
    existed, which no single call would have allowed."""
    written(staging, "contested.json", ref=THEIRS)

    fresh = FileStaging(store)
    fresh.write("contested.json", "mine now", owner="action", owner_action_ref=MINE)
    with pytest.raises(FileOwnershipError) as caught:
        fresh.delete("contested.json", owner_action_ref=MINE)

    assert "taken from its previous owner" in str(caught.value)


# --- staged-first reads ------------------------------------------------------------------

def test_a_deleted_file_reads_as_gone_before_commit(staging, store):
    """§7.4: every read on `pack` is staged-first, with no exception for the awkward ones.
    An action that removes a file and then asks is entitled to its own answer."""
    written(staging, "gone.json")
    fresh = FileStaging(store)
    fresh.delete("gone.json", owner_action_ref=MINE)

    assert fresh.exists("gone.json") is False
    assert fresh.read("gone.json") is None
    assert fresh.ownership("gone.json") is None
    assert store.exists("gone.json"), "nothing should have reached disk yet"


def test_writing_over_a_file_deleted_this_step_brings_it_back(staging, store):
    written(staging, "flip.json", "old")
    fresh = FileStaging(store)
    fresh.delete("flip.json", owner_action_ref=MINE)
    fresh.write("flip.json", "new", owner="action", owner_action_ref=MINE)
    fresh.commit()

    assert store.read("flip.json") == "new"


# --- dry run and the report ---------------------------------------------------------------

def test_a_deletion_is_a_change_record_with_no_after(staging, store):
    """`after is None` is how absence is spelled (§3.3). The run report already reads it
    that way — it greys out Open and Reveal for exactly this case."""
    written(staging, "doomed.json", "bye")
    fresh = FileStaging(store)
    fresh.delete("doomed.json", owner_action_ref=MINE)

    change = fresh.changes()[0]
    assert change["kind"] == "removed"
    assert change["after"] is None
    assert change["before"]["value"] == "bye", "the preview must carry what would be lost"
    assert change["path"] == "doomed.json"


def test_a_dry_run_deletes_nothing(staging, store):
    """The whole safety story. `discard` is what a dry run does instead of committing."""
    written(staging, "safe.json", "still here")
    fresh = FileStaging(store)
    fresh.delete("safe.json", owner_action_ref=MINE)
    assert fresh.changes()[0]["kind"] == "removed"
    fresh.discard()

    assert store.exists("safe.json")
    assert store.read("safe.json") == "still here"


# --- rollback -----------------------------------------------------------------------------

def test_a_committed_deletion_can_be_rolled_back(staging, store):
    """`FileStore.restore` already rewrites prior bytes from a snapshot, so the inverse of
    a delete is the inverse of a write with the sides swapped — it needed nothing new, and
    this is the test that says so rather than assuming it."""
    written(staging, "recoverable.json", "precious")
    fresh = FileStaging(store)
    fresh.delete("recoverable.json", owner_action_ref=MINE)
    fresh.commit()
    assert not store.exists("recoverable.json")

    snapshot = fresh.snapshots["minecraft::recoverable.json"]
    store.restore(snapshot["path"], snapshot["content"], snapshot["ownership"])

    assert store.read("recoverable.json") == "precious"
    assert store.ownership("recoverable.json")["action_ref"] == MINE, \
        "ownership must come back too, or the next run sees an untracked file"


def test_the_snapshot_carries_the_root(staging, store):
    written(staging, "rooted.json")
    fresh = FileStaging(store)
    fresh.delete("rooted.json", owner_action_ref=MINE)
    fresh.commit()

    snapshot = fresh.snapshots["minecraft::rooted.json"]
    assert snapshot["root"] == "minecraft"
    assert snapshot["path"] == "rooted.json"


# --- the sweep, end to end ----------------------------------------------------------------

def test_a_sweep_removes_only_what_this_action_stopped_writing(staging, store):
    """The whole feature, as a generator would use it: rhyolite is dropped from a blueprint,
    and its files go with it — while a co-tenant generator's output is untouched because it
    is not in `owned_by` at all."""
    for path in ("gen/rhyolite.json", "gen/argillite.json"):
        staging.write(path, "{}", owner="action", owner_action_ref=MINE)
    staging.write("gen/wood.json", "{}", owner="action", owner_action_ref=THEIRS)
    staging.write("gen/handmade.json", "{}", owner="user")
    staging.commit()

    wanted = {"gen/argillite.json"}
    sweep = FileStaging(store)
    for path in sweep.owned_by(MINE):
        if path not in wanted:
            sweep.delete(path, owner_action_ref=MINE)
    sweep.commit()

    assert store.exists("gen/argillite.json"), "still wanted"
    assert not store.exists("gen/rhyolite.json"), "dropped and not swept"
    assert store.exists("gen/wood.json"), "another ACTION's file was swept"
    assert store.exists("gen/handmade.json"), "a USER's file was swept"


def test_owned_by_never_lists_another_actions_files(staging):
    staging.write("mine.json", "{}", owner="action", owner_action_ref=MINE)
    staging.write("theirs.json", "{}", owner="action", owner_action_ref=THEIRS)
    staging.write("yours.json", "{}", owner="user")
    staging.commit()

    assert FileStaging(staging._store).owned_by(MINE) == ["mine.json"]


# --- the diff, where two opposite states used to collapse ---------------------------------

def test_a_removal_and_an_unpersisted_write_are_different_records():
    """`_open_file_diff` read both as `after is None` and resolved both by reading the disk.

    They are opposites. A removal has NO `after` side at all — gone is the answer. A
    historical write has an `after` whose `value` was dropped by `_without_file_bytes` on
    the way into `step_runs`, keeping only the hash — there, the disk IS the answer.

    Collapsed, a DRY RUN of a deletion read its "after" off a file still sitting on disk and
    showed identical text on both sides: a preview reporting that deleting a file changes
    nothing. This pins the shapes apart at the source, where the distinction lives.
    """
    from packsmith.core.runner import _without_file_bytes

    written_change = {"engine": "file", "kind": "changed", "path": "a.json",
                      "before": None,
                      "after": {"value": "{}", "owner": "action",
                                "action_ref": MINE, "hash": "abc"}}
    slimmed = _without_file_bytes(written_change)
    assert slimmed["after"] is not None, "a write keeps its after side"
    assert slimmed["after"].get("value") is None, "…but loses the bytes"
    assert slimmed["after"]["hash"] == "abc", "…and keeps the hash that identifies them"

    removed_change = {"engine": "file", "kind": "removed", "path": "a.json",
                      "before": {"value": "{}", "owner": "action", "action_ref": MINE},
                      "after": None}
    assert _without_file_bytes(removed_change)["after"] is None, \
        "a removal has no after side to slim, and must not grow one"
