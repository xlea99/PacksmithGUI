"""The change record — one shape, every engine, both producers (design 3.3).

A dry run and a real run have to describe themselves identically or a report cannot serve
both, and the whole proposition of the report is that the preview *is* the run. This file
covers the classification that shape rests on.

Most of it is about the two kinds a value comparison alone would get wrong: `claimed`,
where nothing visible changed but ownership moved, and `unchanged`, where the action wrote
and the cell was already right. Both are silent when wrong — the record just quietly says
something untrue about a cell nobody reopened.
"""
import json

import pytest

from packsmith.core.files import FileStore, FileStaging, content_hash
from packsmith.core.history import StepRunStore
from packsmith.core.runner import run_action
from packsmith.core.staging import L2Staging, classify

REG = "minecraft:item"
ACTION = "p:a"


class Dump:
    registry = {REG: {"values": ["rope", "stone"]}}

    def attribute(self, *a):
        return None


@pytest.fixture
def store(tags):
    tags.define(REG, "remove", "bool")
    return tags


def only(changes):
    assert len(changes) == 1, changes
    return changes[0]


# --- the classification -------------------------------------------------------------------

def test_a_write_to_a_pristine_cell_is_added(store):
    staging = L2Staging(store)
    staging.write(REG, "rope", "remove", True, owner="user")

    change = only(staging.changes())
    assert change["kind"] == "added"
    assert change["before"] is None
    assert change["after"]["value"] is True


def test_a_different_value_is_changed(store):
    store.assign(REG, "rope", "remove", False)
    staging = L2Staging(store)
    staging.write(REG, "rope", "remove", True, owner="user")

    change = only(staging.changes())
    assert change["kind"] == "changed"
    assert (change["before"]["value"], change["after"]["value"]) == (False, True)


def test_clearing_a_cell_is_removed(store):
    store.assign(REG, "rope", "remove", True)
    staging = L2Staging(store)
    staging.delete(REG, "rope", "remove")

    change = only(staging.changes())
    assert change["kind"] == "removed"
    assert change["after"] is None


def test_the_same_value_under_a_new_owner_is_claimed(store):
    """The one a value comparison misses entirely.

    §3.2.1 makes ownership a property of the assignment's existence, so an action writing
    `true` over a user-owned `true` has changed nothing you can see and everything about
    what happens next — that cell is the action's now and it will keep rewriting it. A
    report calling this "no change" would be silent at exactly the point ownership matters.
    """
    store.assign(REG, "rope", "remove", True, owner="user")
    staging = L2Staging(store)
    staging.write(REG, "rope", "remove", True, owner="action", owner_action_ref=ACTION)

    change = only(staging.changes())
    assert change["kind"] == "claimed"
    assert change["before"]["owner"] == "user"
    assert change["after"]["action_ref"] == ACTION


def test_rewriting_your_own_value_is_unchanged(store):
    store.assign(REG, "rope", "remove", True, owner="action", owner_action_ref=ACTION)
    staging = L2Staging(store)
    staging.write(REG, "rope", "remove", True, owner="action", owner_action_ref=ACTION)

    assert only(staging.changes())["kind"] == "unchanged"


def test_a_no_op_is_still_listed(store):
    """Kept rather than dropped. "197 already removed, 3 newly removed" is a better answer
    than a silent list of 3 — and keeping every staged write is what lets a caller check
    that a run and its preview staged the same SET of cells, not merely reached the same
    end state."""
    store.assign(REG, "rope", "remove", True, owner="user")
    staging = L2Staging(store)
    staging.write(REG, "rope", "remove", True, owner="user")

    assert len(staging.changes()) == 1


def test_clearing_an_already_pristine_cell_changes_nothing(store):
    staging = L2Staging(store)
    staging.delete(REG, "rope", "remove")

    assert only(staging.changes())["kind"] == "unchanged"


def test_a_defaulted_cell_is_absent_not_its_default(tags):
    """`before is None` means **no assignment**, which is not the same as a cell displaying
    its default (§3.2.1). Collapsing them would classify the first explicit `true` on a
    default-true tag as `unchanged` — the decision recorded, and the report saying nothing
    happened."""
    tags.define(REG, "flagged", "bool", default=True)
    staging = L2Staging(tags)
    staging.write(REG, "rope", "flagged", True, owner="user")

    change = only(staging.changes())
    assert change["before"] is None
    assert change["kind"] == "added"


# --- files ---------------------------------------------------------------------------------

@pytest.fixture
def files(user_db, tmp_path):
    root = tmp_path / "instance"
    root.mkdir()
    return FileStore(user_db, root)


def test_a_new_file_is_added_and_carries_its_hash(files):
    staging = FileStaging(files)
    staging.write("a.json", "{}", owner="user")

    change = only(staging.changes())
    assert change["kind"] == "added"
    assert change["before"] is None
    assert change["after"]["hash"] == content_hash("{}")


def test_rewriting_a_file_with_identical_bytes_is_unchanged(files):
    files.write("a.json", "{}", owner="user")
    staging = FileStaging(files)
    staging.write("a.json", "{}", owner="user")

    change = only(staging.changes())
    assert change["kind"] == "unchanged"
    assert change["before"]["value"] == "{}"


# --- what a committed run keeps ------------------------------------------------------------

def test_a_committed_run_records_its_changes(store, user_db, files):
    def act(pack):
        pack.tags.write(REG, "rope", "remove", True)
        pack.filesystem.resolve("out.json").write('{"v": 1}')

    history = StepRunStore(user_db)
    result = run_action(act, tag_store=store, packdump=Dump(), action_ref=ACTION,
                        file_store=files, history=history)
    assert result.ok

    recorded = json.loads(history.get(result.run_id)["rollback_data"])
    kinds = {c["engine"]: c["kind"] for c in recorded["changes"]}
    assert kinds == {"tag": "added", "file": "added"}
    # Rollback still reads only the three keys it knows, so this needed no migration.
    assert set(recorded) == {"l2", "files", "blueprints", "changes"}


def test_a_written_files_bytes_are_not_stored_twice(store, user_db, files):
    """The snapshot store already holds every prior file whole, and §1.1 flags it as
    deliberately quick-and-dirty pending the content-addressed blob store — so persisting
    the NEW bytes beside it would double exactly the thing that note warns about. The hash
    is enough to tell a diff you can trust from one where the file has moved on."""
    def act(pack):
        pack.filesystem.resolve("out.json").write('{"v": 1}')

    history = StepRunStore(user_db)
    result = run_action(act, tag_store=store, packdump=Dump(), action_ref=ACTION,
                        file_store=files, history=history)

    recorded = json.loads(history.get(result.run_id)["rollback_data"])
    after = only(recorded["changes"])["after"]
    assert "value" not in after, "the new file bytes were persisted as well as the hash"
    assert after["hash"] == content_hash('{"v": 1}')
    # ...and in memory, where a dry run needs them to diff against, they are present.
    assert result.changes[0]["after"]["value"] == '{"v": 1}'


def test_a_failed_step_records_no_changes(store, user_db):
    def explode(pack):
        pack.tags.write(REG, "rope", "remove", True)
        raise RuntimeError("boom")

    history = StepRunStore(user_db)
    result = run_action(explode, tag_store=store, packdump=Dump(), action_ref=ACTION,
                        history=history)

    assert not result.ok and result.changes == []
    assert json.loads(history.get(result.run_id)["rollback_data"])["changes"] == []


def test_rolling_back_keeps_the_record_of_what_happened(store, user_db):
    """History is a record of what happened, and undoing a step does not unhappen it. A
    report of a rolled-back run still has to say what it did — which is most of why you
    would open one."""
    def act(pack):
        pack.tags.write(REG, "rope", "remove", True)

    history = StepRunStore(user_db)
    result = run_action(act, tag_store=store, packdump=Dump(), action_ref=ACTION,
                        history=history)
    history.mark_rolled_back(result.run_id)

    recorded = json.loads(history.get(result.run_id)["rollback_data"])
    assert recorded["l2"] == [], "the inverse should be spent"
    assert len(recorded["changes"]) == 1, "the record of what it did was thrown away"


# --- the classifier on its own ---------------------------------------------------------

@pytest.mark.parametrize("before,after,expected", [
    (None, {"value": 1, "owner": "user", "action_ref": None}, "added"),
    ({"value": 1, "owner": "user", "action_ref": None}, None, "removed"),
    (None, None, "unchanged"),
])
def test_absence_decides_before_value_does(before, after, expected):
    assert classify(before, after) == expected
