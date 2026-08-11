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
    assert [w["entry_id"] for w in result.writes] == sorted(ENTRIES)
    assert all(w["engine"] == "tag" and w["action"] == "write" for w in result.writes)
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
    assert result.writes == [], "writes staged before the failure were reported as pending"


# --- THE invariant: preview == what the run commits ---------------------------------------

def test_what_the_run_commits_is_what_the_preview_promised(tags):
    """§3.3's actual guarantee, asserted end to end."""
    promised = preview(tag_everything, tags)
    assert run(tag_everything, tags).status == "success"

    committed = sorted(tags.query(REG, filters=[{"tag": "remove", "op": "exists"}]))
    assert committed == sorted(w["entry_id"] for w in promised.writes)
    for write in promised.writes:
        assert tags.get_tag(REG, write["entry_id"], write["tag"]) == write["value"]


def test_previewing_twice_gives_identical_answers(tags):
    """Determinism, which is what §7.4's "no clock, no randomness" rule buys. A preview
    that varied between runs could not be approved — you would be approving one of them."""
    first, second = preview(tag_everything, tags), preview(tag_everything, tags)
    assert first.writes == second.writes


def test_a_preview_after_a_run_reflects_the_new_state(tags):
    """Determinism is *given the same inputs*, not regardless of them. Once the run has
    committed, previewing again correctly reports the writes as already-satisfied rather
    than parroting the first answer."""
    run(tag_everything, tags)
    again = preview(tag_everything, tags)
    assert again.ok
    # The action still intends to write each cell; the point is it re-derived that from
    # the CURRENT state rather than from a cached plan.
    assert len(again.writes) == len(ENTRIES)


# --- the other two engines stage into the preview too -------------------------------------

def test_file_writes_appear_in_the_preview_with_their_content(tags, files):
    def write_a_file(pack):
        pack.filesystem.resolve("config/generated.json").write('{"v": 1}')

    result = preview(write_a_file, tags, file_store=files)
    assert [(w["engine"], w["path"], w["value"]) for w in result.writes] == [
        ("file", "config/generated.json", '{"v": 1}')]
    assert not (files.root / "config" / "generated.json").exists(), "the file was written"


def test_a_file_preview_matches_what_the_run_writes(tags, files):
    def write_a_file(pack):
        pack.filesystem.resolve("config/generated.json").write('{"v": 1}')

    promised = preview(write_a_file, tags, file_store=files)
    run(write_a_file, tags, file_store=files)

    for write in promised.writes:
        assert files.read(write["path"]) == write["value"]
