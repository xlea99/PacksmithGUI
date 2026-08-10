"""Run history + rollback: record a step, then reverse it — both engines (design 3.3)."""
import pytest

from packsmith.core.bindings import policy_key

from packsmith.core.files import FileStore
from packsmith.core.history import StepRunStore, rollback_step
from packsmith.core.runner import run_action

REG = "minecraft:item"


class FakeDump:
    registry = {}

    def attribute(self, registry_type, entry_id, name):
        return None


@pytest.fixture
def history(user_db):
    return StepRunStore(user_db)


@pytest.fixture
def env(tags):
    tags.define(REG, "remove", "bool", default=False)
    tags.define(REG, "queued", "bool", default=False)
    tags.assign(REG, "quark:rope", "remove", True, owner="user")
    return tags, FakeDump()


def _mark(pack):
    """Reads remove==True items, writes queued=True on them (action-owned)."""
    for eid in pack.tags.query(REG, "remove", True):
        pack.tags.write(REG, eid, "queued", True)
    pack.log("info", "marked")


# --- recording -------------------------------------------------------------

def test_run_is_recorded(env, history):
    tags, dump = env
    result = run_action(_mark, tag_store=tags, packdump=dump, action_ref="demo:mark", history=history)
    assert result.run_id is not None
    run = history.get(result.run_id)
    assert run["action_ref"] == "demo:mark" and run["status"] == "success"
    assert history.list()[0]["id"] == result.run_id


def test_failed_run_recorded_with_empty_rollback(env, history):
    tags, dump = env

    def bad(pack):
        pack.tags.write(REG, "quark:rope", "queued", True)
        pack.fail("no")

    result = run_action(bad, tag_store=tags, packdump=dump, action_ref="demo:bad", history=history)
    assert not result.ok
    assert history.get(result.run_id)["status"] == "failed"
    assert tags.get_ownership(REG, "quark:rope", "queued") is None   # never landed


# --- L2 rollback -----------------------------------------------------------

def test_rollback_undoes_write_to_pristine(env, history):
    tags, dump = env
    result = run_action(_mark, tag_store=tags, packdump=dump, action_ref="demo:mark", history=history)
    assert tags.get_ownership(REG, "quark:rope", "queued") == {"kind": "action", "action_ref": "demo:mark"}

    rollback_step(result.run_id, tag_store=tags, history=history)
    assert tags.get_ownership(REG, "quark:rope", "queued") is None   # back to pristine
    assert tags.get_tag(REG, "quark:rope", "queued") is False


def test_rollback_restores_prior_value_and_owner(env, history):
    tags, dump = env
    tags.assign(REG, "quark:rope", "queued", False, owner="user")    # prior: user-owned False
    # Taking a user-owned cell requires the action to have declared `overwrite` (design 3.3).
    result = run_action(_mark, tag_store=tags, packdump=dump, action_ref="demo:mark",
                        history=history, conflict_policies={policy_key("tag", "minecraft:item", "queued"): "overwrite"})
    assert tags.get_tag(REG, "quark:rope", "queued") is True         # action took it over
    assert tags.get_ownership(REG, "quark:rope", "queued")["kind"] == "action"

    rollback_step(result.run_id, tag_store=tags, history=history)
    assert tags.get_tag(REG, "quark:rope", "queued") is False
    assert tags.get_ownership(REG, "quark:rope", "queued") == {"kind": "user", "action_ref": None}


# --- file rollback ---------------------------------------------------------

def test_rollback_deletes_a_newly_created_file(user_db, tmp_path, tags, history):
    root = tmp_path / "instance"; root.mkdir()
    fs = FileStore(user_db, root)

    def write_action(pack):
        pack.filesystem.resolve("config/obliterator.json").write("{}")

    result = run_action(write_action, tag_store=tags, packdump=FakeDump(),
                        action_ref="removal:nuke", file_store=fs, history=history)
    assert (root / "config" / "obliterator.json").exists()

    rollback_step(result.run_id, tag_store=tags, history=history, file_store=fs)
    assert not (root / "config" / "obliterator.json").exists()       # deleted
    assert fs.ownership("config/obliterator.json") is None


def test_rollback_restores_prior_file_content(user_db, tmp_path, tags, history):
    """The common real case: a vanilla config file nobody has claimed (untouched, so the
    action may take it), modified by an action, then rolled back to its original bytes."""
    root = tmp_path / "instance"; root.mkdir()
    fs = FileStore(user_db, root)
    (root / "config").mkdir()
    (root / "config" / "x.json").write_text("OLD", encoding="utf-8")   # on disk, untouched

    def write_action(pack):
        pack.filesystem.resolve("config/x.json").write("NEW")

    result = run_action(write_action, tag_store=tags, packdump=FakeDump(),
                        action_ref="removal:nuke", file_store=fs, history=history)
    assert (root / "config" / "x.json").read_text(encoding="utf-8") == "NEW"
    assert fs.ownership("config/x.json")["kind"] == "action"           # claimed by writing

    rollback_step(result.run_id, tag_store=tags, history=history, file_store=fs)
    assert (root / "config" / "x.json").read_text(encoding="utf-8") == "OLD"   # restored
    assert fs.ownership("config/x.json") is None                       # untouched again


def test_rollback_needs_file_store_when_files_were_written(user_db, tmp_path, tags, history):
    root = tmp_path / "instance"; root.mkdir()
    fs = FileStore(user_db, root)

    def write_action(pack):
        pack.filesystem.resolve("f.json").write("{}")

    result = run_action(write_action, tag_store=tags, packdump=FakeDump(),
                        action_ref="x:y", file_store=fs, history=history)
    with pytest.raises(ValueError):
        rollback_step(result.run_id, tag_store=tags, history=history)   # no file_store given
