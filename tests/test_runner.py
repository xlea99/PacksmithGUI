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
    assert "made.json" in recorded["files"]
