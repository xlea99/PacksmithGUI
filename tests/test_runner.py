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
