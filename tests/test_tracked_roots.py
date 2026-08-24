"""Tracked roots — writing somewhere that is not the instance (design 6.6).

Everything in §6 assumed one place. That held while Packsmith's job ended at the instance,
and stops the moment the pack's content is *generated*: a glue mod's assets are authored in
a Gradle project that will never live inside a Minecraft instance.

The failures guarded here are the quiet ones, and they share a shape — **a write that goes
somewhere other than where it was aimed**:

* `file_ownership.path` was a bare `TEXT PRIMARY KEY`, so the same relative path under two
  roots was literally one row. A user claim in the repo would have blocked an action writing
  the instance, and rolling one back would have restored the other's bytes.
* Rollback data is keyed by path. Restoring by path alone puts the bytes back under the
  instance regardless of where they came from.
* Adding a root that overlaps another gives one file two identities, and the hard-block
  stops composing: blocked under one name, writable under the other.
"""
import json

import pytest

from packsmith.core.db import UserDB
from packsmith.core.files import FileStaging, FileStore
from packsmith.core.history import StepRunStore, rollback_step
from packsmith.core.pack import Pack
from packsmith.core.roots import FileRoots, RootError
from packsmith.core.runner import run_action
from packsmith.core.tags import TagStore

SAME = "assets/deep_end/lang/en_us.json"


@pytest.fixture
def world(tmp_path):
    instance = tmp_path / "instance"
    repo = tmp_path / "tweaks"
    instance.mkdir()
    repo.mkdir()
    db = UserDB(tmp_path / "p.db")
    roots = FileRoots(db, instance, protected=[tmp_path / "userdata"])
    roots.add("tweaks", repo)
    return {"db": db, "roots": roots, "instance": instance, "repo": repo,
            "tags": TagStore(db), "history": StepRunStore(db), "tmp": tmp_path}


def run(world, body, *, commit=True, history=None):
    return run_action(body, tag_store=world["tags"], packdump=None, action_ref="p:a",
                      file_store=world["roots"], history=history, commit=commit)


# --- the same path under two roots is two files ------------------------------------------

def test_one_path_two_roots_two_files(world):
    """The correctness hole `(root_id, path)` closes. As one `TEXT PRIMARY KEY` these were
    a single ownership row, so the second write silently inherited the first's owner."""
    def body(pack):
        pack.filesystem.resolve(SAME).write("instance")
        pack.filesystem.resolve(SAME, root="tweaks").write("repo")

    assert run(world, body).ok
    assert (world["instance"] / SAME).read_text(encoding="utf-8") == "instance"
    assert (world["repo"] / SAME).read_text(encoding="utf-8") == "repo"


def test_ownership_is_recorded_per_root(world):
    def body(pack):
        pack.filesystem.resolve(SAME, root="tweaks").write("repo")

    assert run(world, body).ok
    assert world["roots"].store("tweaks").ownership(SAME) == {
        "kind": "action", "action_ref": "p:a"}
    assert world["roots"].store().ownership(SAME) is None, \
        "the instance was never written and must not have inherited a claim"


def test_a_user_claim_in_one_root_does_not_block_the_other(world):
    """The hard-block is an exact lookup. Sharing a key made a claim in the repo silently
    refuse an action writing the instance — a block nobody asked for, on a file nobody
    claimed."""
    world["roots"].store("tweaks").claim(SAME, owner="user")

    def body(pack):
        pack.filesystem.resolve(SAME).write("instance")

    assert run(world, body).ok
    assert (world["instance"] / SAME).read_text(encoding="utf-8") == "instance"


def test_a_user_claim_still_blocks_its_own_root(world):
    world["roots"].store("tweaks").claim(SAME, owner="user")

    def body(pack):
        pack.filesystem.resolve(SAME, root="tweaks").write("nope")

    assert not run(world, body).ok


# --- rollback goes back to the right place --------------------------------------------------

def test_rollback_restores_the_root_it_came_from(world):
    (world["repo"] / "assets/deep_end/lang").mkdir(parents=True)
    (world["repo"] / SAME).write_text("original", encoding="utf-8")

    def body(pack):
        pack.filesystem.resolve(SAME, root="tweaks").write("generated")

    result = run(world, body, history=world["history"])
    assert (world["repo"] / SAME).read_text(encoding="utf-8") == "generated"

    rollback_step(result.run_id, tag_store=world["tags"], history=world["history"],
                  file_store=world["roots"])

    assert (world["repo"] / SAME).read_text(encoding="utf-8") == "original"
    assert not (world["instance"] / SAME).exists(), "it restored into the wrong root"


def test_the_snapshot_key_distinguishes_roots(world):
    """One key for both would give a pair of files a single snapshot, and the rollback would
    restore one file's bytes over the other."""
    def body(pack):
        pack.filesystem.resolve(SAME).write("a")
        pack.filesystem.resolve(SAME, root="tweaks").write("b")

    result = run(world, body, history=world["history"])
    stored = json.loads(world["history"].get(result.run_id)["rollback_data"])["files"]

    assert len(stored) == 2, stored
    assert {snap["root"] for snap in stored.values()} == {"minecraft", "tweaks"}
    assert all(snap["path"] == SAME for snap in stored.values())


# --- what the report says ---------------------------------------------------------------------

def test_a_change_names_its_root(world):
    def body(pack):
        pack.filesystem.resolve(SAME, root="tweaks").write("x")

    change = run(world, body, commit=False).changes[0]
    assert change["root"] == "tweaks"
    assert change["path"] == SAME


def test_a_handle_says_where_it_lives(world):
    seen = {}

    def body(pack):
        handle = pack.filesystem.resolve(SAME, root="tweaks")
        seen["root"], seen["path"] = handle.root, handle.path

    assert run(world, body, commit=False).ok
    assert seen == {"root": "tweaks", "path": SAME}


# --- guards: each one is a correctness requirement ----------------------------------------------

def test_an_unknown_root_refuses_rather_than_falling_back(world):
    """Falling back to the instance is the whole failure this prevents — a write aimed at a
    repo landing in the pack, reported as success."""
    def body(pack):
        pack.filesystem.resolve(SAME, root="nope").write("x")

    assert not run(world, body).ok
    assert not (world["instance"] / SAME).exists()


def test_overlapping_roots_are_refused(world):
    """One file, two identities, and a hard-block that stops composing."""
    (world["repo"] / "src").mkdir()
    outer = world["tmp"] / "nested"
    (outer / "inside").mkdir(parents=True)
    world["roots"].add("nested", outer / "inside")

    with pytest.raises(RootError, match="overlaps"):
        world["roots"].add("inner", world["repo"] / "src")     # inside an existing root
    with pytest.raises(RootError, match="overlaps"):
        world["roots"].add("outer", outer)                     # containing an existing root
    with pytest.raises(RootError, match="overlaps"):
        world["roots"].add("again", world["repo"])             # the same folder twice


def test_packsmith_own_data_is_unreachable(world):
    """§6.2's guard, and the subtle half: checking `path != userdata` is not enough, because
    adding userdata's PARENT defeats it. Reaching `.star` sources through the browser would
    edit code by a door that applies none of §3.3.1's provenance rules."""
    userdata = world["tmp"] / "userdata"
    userdata.mkdir()

    with pytest.raises(RootError, match="Packsmith's own data"):
        world["roots"].add("mine", userdata)


def test_the_instance_cannot_be_removed_or_renamed(world):
    with pytest.raises(RootError, match="cannot be removed"):
        world["roots"].remove("minecraft")
    with pytest.raises(RootError, match="cannot be renamed"):
        world["roots"].rename("minecraft", "mc")


def test_a_name_is_a_label_so_renaming_keeps_ownership(world):
    """Design 3.2.1's rule, which 3.2.2 relearned the hard way with instance names. Rows key
    on the id, so a rename is one row and orphans nothing."""
    def body(pack):
        pack.filesystem.resolve(SAME, root="tweaks").write("x")

    assert run(world, body).ok
    world["roots"].rename("tweaks", "glue")

    assert world["roots"].store("glue").ownership(SAME) == {
        "kind": "action", "action_ref": "p:a"}


def test_untracking_forgets_the_claims_but_keeps_the_files(world):
    def body(pack):
        pack.filesystem.resolve(SAME, root="tweaks").write("x")

    assert run(world, body).ok
    world["roots"].remove("tweaks")

    assert (world["repo"] / SAME).exists(), "the files are the user's, not Packsmith's"
    assert world["db"].fetch_all("SELECT * FROM file_ownership") == [], \
        "a claim on a folder Packsmith can no longer see is not a claim"


def test_a_bad_name_is_refused(world):
    for bad in ("", "Tweaks", "9lives", "my folder", "minecraft"):
        with pytest.raises(RootError):
            world["roots"].add(bad, world["tmp"])


# --- old profiles ---------------------------------------------------------------------------------

def test_a_profile_with_no_extra_roots_behaves_exactly_as_before(tmp_path):
    """The migration's real promise. Every existing profile has one root, every existing
    action resolves unqualified paths, and none of them should notice this happened."""
    instance = tmp_path / "instance"
    instance.mkdir()
    db = UserDB(tmp_path / "p.db")
    store = FileStore(db, instance)
    store.write("config/x.json", "{}", owner="user")

    roots = FileRoots(db, instance)
    assert roots.names() == ["minecraft"]
    assert roots.store().ownership("config/x.json") == {"kind": "user", "action_ref": None}


def test_a_step_given_one_store_says_so_rather_than_guessing(tmp_path):
    """Most callers legitimately have a single store and no registry. Asking one for another
    root must refuse, not quietly answer about the instance."""
    instance = tmp_path / "instance"
    instance.mkdir()
    db = UserDB(tmp_path / "p.db")
    staging = FileStaging(FileStore(db, instance))
    pack = Pack(staging=None, tag_store=TagStore(db), packdump=None, action_ref="p:a",
                file_staging=staging)

    with pytest.raises(ValueError, match="bind the folder"):
        pack.filesystem.resolve(SAME, root="tweaks").write("x")
