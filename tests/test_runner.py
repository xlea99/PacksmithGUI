"""The action runner: atomic step execution — commit on success, discard on
fail() or any error (design 3.3)."""
import pytest

from packsmith.core.runner import run_action

REG = "minecraft:item"


class FakeDump:
    def __init__(self, items):
        self.registry = {"minecraft:item": {"values": list(items)}}

    def attribute(self, registry_type, entry_id, name):
        return None


def _mark(pack):
    """A small test action: for every entry where the bound `source` tag is True,
    stamp the bound `target` tag True (action-owned)."""
    marked = 0
    for eid in pack.tags.query(REG, pack.step.mappings["source"], True):
        pack.tags.write(REG, eid, pack.step.mappings["target"], True)
        marked += 1
    pack.log("info", f"marked {marked} entries")


@pytest.fixture
def env(tags):
    """A TagStore with `remove` and `queued` bool tags, two items pre-marked
    remove=True by the user, plus a fake packdump."""
    tags.define(REG, "remove", "bool", default=False)
    tags.define(REG, "queued", "bool", default=False)
    tags.assign(REG, "quark:rope", "remove", True, owner="user")
    tags.assign(REG, "quark:torch", "remove", True, owner="user")
    dump = FakeDump(["quark:rope", "minecraft:diamond", "quark:torch"])
    return tags, dump


def _run_mark(tags, dump):
    return run_action(_mark, tag_store=tags, packdump=dump, action_ref="test:mark",
                      mappings={"source": "remove", "target": "queued"})


def test_action_writes_action_owned_tags(env):
    tags, dump = env
    result = _run_mark(tags, dump)
    assert result.ok
    # queued=True and action-owned, on exactly the two remove items
    for eid in ("quark:rope", "quark:torch"):
        assert tags.get_tag(REG, eid, "queued") is True
        assert tags.get_ownership(REG, eid, "queued") == {"kind": "action", "action_ref": "test:mark"}
    # the unmarked item stays pristine
    assert tags.get_ownership(REG, "minecraft:diamond", "queued") is None


def test_fail_discards_every_write(env):
    tags, dump = env

    def bad_action(pack):
        pack.tags.write(REG, "quark:rope", "queued", True)  # staged...
        pack.fail("changed my mind")                        # ...then bail

    result = run_action(bad_action, tag_store=tags, packdump=dump, action_ref="test:bad")
    assert not result.ok
    assert result.reason == "changed my mind"
    assert tags.get_ownership(REG, "quark:rope", "queued") is None  # nothing committed


def test_unexpected_error_discards_and_reports(env):
    tags, dump = env

    def crashy(pack):
        pack.tags.write(REG, "quark:rope", "queued", True)
        raise ValueError("boom")

    result = run_action(crashy, tag_store=tags, packdump=dump, action_ref="test:crashy")
    assert not result.ok
    assert "boom" in result.reason
    assert tags.get_ownership(REG, "quark:rope", "queued") is None  # discarded, app didn't crash


def test_log_lines_captured(env):
    tags, dump = env
    result = _run_mark(tags, dump)
    assert any("marked 2" in msg for _, msg in result.log_lines)


# --- commit atomicity (design 3.3: "a failed step touches nothing, for free") ----------

class _ExplodingTags:
    """A tag store that dies partway through a flush, like a constraint violation would."""

    def __init__(self, real, die_on):
        self._real = real
        self._db = real._db
        self._die_on = die_on
        self.written = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def assign(self, registry_type, entry_id, tag_name, value, **kw):
        if entry_id == self._die_on:
            raise RuntimeError("constraint blew up mid-flush")
        self.written.append(entry_id)
        return self._real.assign(registry_type, entry_id, tag_name, value, **kw)


def test_an_l2_flush_that_fails_halfway_leaves_nothing_behind(tags, tmp_path):
    """The bug: UserDB.execute committed per statement, so a loop of assigns was a loop of
    transactions and the ones before the failure stuck."""
    tags.define(REG, "remove", "bool", default=False)
    exploding = _ExplodingTags(tags, die_on="minecraft:diamond")

    def action(pack):
        pack.tags.write(REG, "quark:rope", "remove", True)
        pack.tags.write(REG, "minecraft:diamond", "remove", True)

    result = run_action(action, tag_store=exploding, packdump=FakeDump(["quark:rope", "minecraft:diamond"]),
                        action_ref="pkg:act")
    assert not result.ok and "commit failed" in result.reason
    # the write that "succeeded" before the explosion must not have survived
    assert tags.get_ownership(REG, "quark:rope", "remove") is None
    assert tags.get_ownership(REG, "minecraft:diamond", "remove") is None


def test_a_missing_required_file_is_caught_before_any_file_is_written(tags, tmp_path):
    """`file_must_exist` used to be checked inside the write loop, so the third file
    failing left the first two on disk and stamped."""
    from packsmith.core.files import FileStore
    store = FileStore(tags._db, tmp_path)
    (tmp_path / "exists.json").write_text("{}", encoding="utf-8")

    def action(pack):
        pack.filesystem.resolve("first.json").write("{}")
        pack.filesystem.resolve("exists.json").write("{}")
        handle = pack.filesystem.resolve("absent.json")
        handle.write("{}", file_must_exist=True)

    result = run_action(action, tag_store=tags, packdump=FakeDump([]),
                        action_ref="pkg:act", file_store=store)
    assert not result.ok
    assert not (tmp_path / "first.json").exists(), "wrote a file before pre-flighting"


def test_a_partial_commit_keeps_its_rollback_record(tags, tmp_path):
    """If files did land before the database failed, the run must still record how to undo
    them — recording an empty rollback strands them with the snapshots thrown away."""
    from packsmith.core.files import FileStore
    from packsmith.core.history import StepRunStore
    tags.define(REG, "remove", "bool", default=False)
    store = FileStore(tags._db, tmp_path)
    history = StepRunStore(tags._db)
    exploding = _ExplodingTags(tags, die_on="quark:rope")

    def action(pack):
        pack.filesystem.resolve("made.json").write('{"v": 1}')
        pack.tags.write(REG, "quark:rope", "remove", True)

    result = run_action(action, tag_store=exploding, packdump=FakeDump(["quark:rope"]),
                        action_ref="pkg:act", file_store=store, history=history)
    assert not result.ok
    assert (tmp_path / "made.json").exists(), "the file did land"
    assert "roll this step back" in result.reason
    import json
    recorded = json.loads(history.get(result.run_id)["rollback_data"])
    assert recorded["files"], "the way back was thrown away"
    assert "minecraft::made.json" in recorded["files"], recorded["files"]


def test_a_rolled_back_transaction_records_nothing_to_undo(tags, tmp_path):
    """The staging buffers build their inverse AS THEY GO, so on a mid-flush failure they
    describe writes SQLite has already rolled back. Recording those makes a later rollback
    try to undo what never happened."""
    from packsmith.core.blueprints import BlueprintStore
    from packsmith.core.files import FileStore
    from packsmith.core.history import StepRunStore

    class LateFail:
        """Creating the instance works; the bind after it explodes."""
        def __init__(self, real):
            self._real, self._db = real, real._db

        def __getattr__(self, name):
            return getattr(self._real, name)

        def bind(self, *a, **k):
            raise RuntimeError("boom after the instance was created")

    tags.define(REG, "remove", "bool", default=False)
    bps = BlueprintStore(tags._db)
    bps.define("StoneType")
    bps.add_slot("StoneType", "base_block", "registry", registry_type="minecraft:block")
    history = StepRunStore(tags._db)
    store = FileStore(tags._db, tmp_path)

    def action(pack):
        pack.filesystem.resolve("made.json").write("{}")
        pack.tags.write(REG, "quark:rope", "remove", True)
        pack.blueprints.create("StoneType", "granite")
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite")

    result = run_action(action, tag_store=tags, packdump=FakeDump(["quark:rope"]),
                        action_ref="pkg:act", file_store=store,
                        blueprint_store=LateFail(bps), history=history)
    assert not result.ok
    import json
    data = json.loads(history.get(result.run_id)["rollback_data"])
    assert data["files"], "the file DID land and must stay undoable"
    assert data["l2"] == [], "the database rolled back; nothing to undo there"
    assert data["blueprints"] == []


def test_the_partial_rollback_the_reason_promises_actually_works(tags, tmp_path):
    """The failure reason says "roll this step back to undo them" — so it has to run
    cleanly, not restore the files and then raise on a stale blueprint entry."""
    from packsmith.core.blueprints import BlueprintStore
    from packsmith.core.files import FileStore
    from packsmith.core.history import StepRunStore, rollback_step

    class LateFail:
        def __init__(self, real):
            self._real, self._db = real, real._db

        def __getattr__(self, name):
            return getattr(self._real, name)

        def bind(self, *a, **k):
            raise RuntimeError("boom")

    bps = BlueprintStore(tags._db)
    bps.define("StoneType")
    bps.add_slot("StoneType", "base_block", "registry", registry_type="minecraft:block")
    history = StepRunStore(tags._db)
    store = FileStore(tags._db, tmp_path)

    def action(pack):
        pack.filesystem.resolve("made.json").write("{}")
        pack.blueprints.create("StoneType", "granite")
        pack.blueprints.bind("StoneType", "granite", "base_block", "minecraft:granite")

    result = run_action(action, tag_store=tags, packdump=FakeDump([]),
                        action_ref="pkg:act", file_store=store,
                        blueprint_store=LateFail(bps), history=history)
    assert "roll this step back" in result.reason
    assert (tmp_path / "made.json").exists()

    rollback_step(result.run_id, tag_store=tags, history=history, file_store=store,
                  blueprint_store=bps)
    assert not (tmp_path / "made.json").exists(), "the promised undo did not happen"


def test_a_missing_required_file_leaves_no_half_applied_step(tags, tmp_path):
    """FS-4: a `file_must_exist` failure used to happen mid-commit, after earlier files in
    the same step had already been written — and the snapshots that could undo them were
    discarded, so the step reported "failed" over a changed disk.

    Both of the fixes that section suggested are in place, and the first makes the second
    moot for this case: the check now runs at STAGING time (§7.3, where the hard-block
    already lived), so the step dies before a single byte is written. The other kind of
    commit-time failure — one that genuinely gets partway — keeps its rollback record; see
    `test_a_partial_commit_keeps_its_rollback_record`.
    """
    import json
    from packsmith.core.files import FileStore
    from packsmith.core.history import StepRunStore

    store = FileStore(tags._db, tmp_path)
    history = StepRunStore(tags._db)
    (tmp_path / "second.json").write_text("ORIGINAL", encoding="utf-8")

    def action(pack):
        pack.filesystem.resolve("first.json").write("made")
        pack.filesystem.resolve("second.json").write("changed", file_must_exist=True)
        pack.filesystem.resolve("third.json").write("x", file_must_exist=True)

    result = run_action(action, tag_store=tags, packdump=FakeDump([]),
                        action_ref="pkg:act", file_store=store, history=history)

    assert not result.ok and "third.json" in result.reason
    assert not (tmp_path / "first.json").exists(), "an earlier file in the step landed"
    assert (tmp_path / "second.json").read_text() == "ORIGINAL", "an existing file changed"
    assert json.loads(history.get(result.run_id)["rollback_data"])["files"] == {}


def test_a_file_commit_failure_reports_itself_not_an_internal_error(tags, tmp_path):
    """A failure in `files.commit()` happens BEFORE the database transaction is opened, so
    the handler that inspects that transaction must not assume it exists. It did, and the
    UnboundLocalError replaced the real reason — hiding what actually went wrong behind an
    internal one."""
    from packsmith.core.files import FileStore

    class Exploding(FileStore):
        def write(self, *a, **k):
            raise PermissionError("disk says no")

    store = Exploding(tags._db, tmp_path)

    def action(pack):
        pack.filesystem.resolve("out.json").write("{}")

    result = run_action(action, tag_store=tags, packdump=FakeDump([]),
                        action_ref="pkg:act", file_store=store)
    assert not result.ok
    assert "disk says no" in result.reason
    assert "UnboundLocalError" not in result.reason
