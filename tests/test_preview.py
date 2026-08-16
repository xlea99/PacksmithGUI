"""Dry-run previews — design 3.3's "there is no separate real run".

§3.3 states the invariant that the whole staging design exists to make true:

> There is no separate "real run" — the bytes that commit are exactly the bytes of the
> final, zero-conflict dry-run.

A preview produced by a *simulation* of the run can disagree with the run that follows
it — the world moved, or the second execution took a different branch. A preview that IS
the run, held at the commit point, cannot. That is the property under test here, and it is
also why §7.4 refuses to expose a clock or randomness to actions: nondeterminism would let
the committed run differ from the one you approved.

The `ask` policy is post-1.0, but this invariant is not something to check when `ask` gets
built. By then a determinism leak would be a bug inside a convergence loop; now it is a
failing assertion.
"""
import pytest

from packsmith.core.files import FileStore
from packsmith.core.runner import preview_step, run_action
from packsmith.core.tags import TagStore

REG = "minecraft:item"
ENTRIES = ["minecraft:stone", "minecraft:dirt", "minecraft:sand"]


class Dump:
    registry = {REG: {"values": ENTRIES}}

    def attribute(self, *a):
        return None


@pytest.fixture
def tags(user_db):
    store = TagStore(user_db)
    store.define(REG, "remove", "bool")
    return store


@pytest.fixture
def files(user_db, tmp_path):
    root = tmp_path / "instance"
    root.mkdir()
    return FileStore(user_db, root)


def tag_everything(pack):
    for entry in pack.registry.entries(REG):
        pack.tags.write(REG, entry, "remove", True)
    pack.log("info", f"tagged {len(pack.registry.entries(REG))} entries")


def run(fn, tags, **kw):
    return run_action(fn, tag_store=tags, packdump=Dump(), action_ref="p:a", **kw)


def preview(fn, tags, **kw):
    return preview_step(fn, tag_store=tags, packdump=Dump(), action_ref="p:a", **kw)


# --- a preview is a run that was not promoted --------------------------------------------

def test_a_preview_reports_what_would_be_written(tags):
    result = preview(tag_everything, tags)
    assert result.ok
    assert [c["entry_id"] for c in result.changes] == sorted(ENTRIES)
    assert all(c["engine"] == "tag" and c["kind"] == "added" for c in result.changes)
    assert all(c["before"] is None and c["after"]["value"] is True
               for c in result.changes)
    assert result.summary() == {"tag": 3}


def test_a_preview_commits_nothing(tags):
    preview(tag_everything, tags)
    assert tags.query(REG, filters=[{"tag": "remove", "op": "exists"}]) == []


def test_a_preview_records_no_history(tags, user_db):
    """A run history listing things that never ran is worse than no history."""
    from packsmith.core.history import StepRunStore

    history = StepRunStore(user_db)
    preview(tag_everything, tags)
    assert history.list() == []


def test_the_action_really_executes(tags):
    """Not a static analysis: the body runs, which is why the answer can be trusted."""
    result = preview(tag_everything, tags)
    assert any("tagged 3 entries" in line for _level, line in result.log_lines)


def test_a_failing_action_previews_as_failed_and_writes_nothing(tags):
    def explode(pack):
        pack.tags.write(REG, "minecraft:stone", "remove", True)
        raise RuntimeError("boom")

    result = preview(explode, tags)
    assert not result.ok and "boom" in result.reason
    assert result.changes == [], "writes staged before the failure were reported"


# --- THE invariant: preview == what the run commits ---------------------------------------

def test_what_the_run_commits_is_what_the_preview_promised(tags):
    """§3.3's actual guarantee, asserted end to end."""
    promised = preview(tag_everything, tags)
    assert run(tag_everything, tags).status == "success"

    committed = sorted(tags.query(REG, filters=[{"tag": "remove", "op": "exists"}]))
    assert committed == sorted(c["entry_id"] for c in promised.changes)
    for change in promised.changes:
        assert tags.get_tag(REG, change["entry_id"], change["tag"]) \
            == change["after"]["value"]


def test_a_run_reports_the_same_changes_it_was_previewed_as(tags):
    """The record itself, not just the end state. A report has to be able to show either
    producer's output in one place, which is only true if they describe themselves the
    same way."""
    promised = preview(tag_everything, tags)
    result = run(tag_everything, tags)

    assert result.changes == promised.changes
    assert result.summary() == promised.summary() == {"tag": 3}


def test_previewing_twice_gives_identical_answers(tags):
    """Determinism, which is what §7.4's "no clock, no randomness" rule buys. A preview
    that varied between runs could not be approved — you would be approving one of them."""
    first, second = preview(tag_everything, tags), preview(tag_everything, tags)
    assert first.changes == second.changes


def test_a_preview_after_a_run_reports_the_writes_as_no_ops(tags):
    """Determinism is *given the same inputs*, not regardless of them. Once the run has
    committed, previewing again re-derives its answer from the CURRENT state.

    The action still intends to write every cell — so every cell is still listed — but each
    one is now classified `unchanged`, which is how a re-run of a removal job can say "197
    already removed, 3 newly removed" instead of claiming 200 edits."""
    run(tag_everything, tags)
    again = preview(tag_everything, tags)

    assert again.ok
    assert len(again.changes) == len(ENTRIES)
    assert {c["kind"] for c in again.changes} == {"unchanged"}
    assert again.summary() == {}, "no-ops were counted as changes"


# --- the other two engines stage into the preview too -------------------------------------

def test_file_writes_appear_in_the_preview_with_their_content(tags, files):
    def write_a_file(pack):
        pack.filesystem.resolve("config/generated.json").write('{"v": 1}')

    result = preview(write_a_file, tags, file_store=files)
    assert [(c["engine"], c["path"], c["kind"], c["after"]["value"])
            for c in result.changes] == [
        ("file", "config/generated.json", "added", '{"v": 1}')]
    assert not (files.root / "config" / "generated.json").exists(), "the file was written"


def test_a_file_preview_matches_what_the_run_writes(tags, files):
    def write_a_file(pack):
        pack.filesystem.resolve("config/generated.json").write('{"v": 1}')

    promised = preview(write_a_file, tags, file_store=files)
    run(write_a_file, tags, file_store=files)

    for change in promised.changes:
        assert files.read(change["path"]) == change["after"]["value"]


def test_a_files_hash_is_of_the_bytes_that_land(tags, files):
    """A committed run keeps the hash instead of the new bytes, so it has to be a hash of
    what actually reached disk — which is only a stable claim because writes no longer
    translate line endings (see `tests/test_fidelity.py`)."""
    from packsmith.core.files import content_hash

    def write_crlf(pack):
        pack.filesystem.resolve("config/win.toml").write("a = 1\r\nb = 2\r\n")

    promised = preview(write_crlf, tags, file_store=files)
    run(write_crlf, tags, file_store=files)

    on_disk = (files.root / "config" / "win.toml").read_bytes().decode("utf-8")
    assert promised.changes[0]["after"]["hash"] == content_hash(on_disk)
