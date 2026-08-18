"""Conflict policy and the file hard-block — the two ownership engines under pressure
(design 3.2.1 / 3.3 / 6.0 / 6.1).

They look alike and behave differently on purpose. **L2 is closed-world** — Packsmith is
the only writer, so a conflict is negotiable by declared policy (overwrite / skip / fail).
**Files are open-world** — the game, mod updates, and other editors also write them, so a
user-owned file is hard-blocked, not negotiated.
"""
import pytest

from packsmith.core.files import FileStore, FileStaging, FileOwnershipError
from packsmith.core.packages import load_package
from packsmith.core.bindings import conflict_policies_for, policy_key
from packsmith.core.runner import run_action

REG = "minecraft:item"


class FakeDump:
    registry = {REG: {"values": ["quark:rope"]}}

    def attribute(self, registry_type, entry_id, name):
        return None


@pytest.fixture
def env(tags):
    tags.define(REG, "queued", "bool", default=False)
    return tags, FakeDump()


def _writer(value=True):
    def action(pack):
        pack.tags.write(REG, "quark:rope", "queued", value)
    return action


def _run(env, *, action_ref="pkg:writer", policies=None, fn=None):
    tags, dump = env
    return run_action(fn or _writer(), tag_store=tags, packdump=dump,
                      action_ref=action_ref, conflict_policies=policies)


# --- no conflict ------------------------------------------------------------

def test_writing_a_pristine_cell_needs_no_policy(env):
    tags, _ = env
    assert _run(env).ok
    assert tags.get_tag(REG, "quark:rope", "queued") is True


def test_rewriting_a_cell_this_action_already_owns_is_not_a_conflict(env):
    """An action is the authoritative producer of its own state and re-asserts it every
    run — that must not count as a conflict with itself."""
    tags, _ = env
    assert _run(env).ok
    result = _run(env, fn=_writer(False))          # same action_ref, no policy declared
    assert result.ok
    assert tags.get_tag(REG, "quark:rope", "queued") is False


# --- the three MVP policies -------------------------------------------------

def test_overwrite_takes_the_cell_and_logs_it(env):
    tags, _ = env
    tags.assign(REG, "quark:rope", "queued", False, owner="user")
    result = _run(env, policies={policy_key("tag", REG, "queued"): "overwrite"})
    assert result.ok
    assert tags.get_tag(REG, "quark:rope", "queued") is True
    assert tags.get_ownership(REG, "quark:rope", "queued")["kind"] == "action"
    assert any("overwrite" in message for _, message in result.log_lines)


def test_skip_leaves_the_cell_alone_and_logs_it(env):
    tags, _ = env
    tags.assign(REG, "quark:rope", "queued", False, owner="user")
    result = _run(env, policies={policy_key("tag", REG, "queued"): "skip"})
    assert result.ok                                     # the step still succeeds
    assert tags.get_tag(REG, "quark:rope", "queued") is False
    assert tags.get_ownership(REG, "quark:rope", "queued") == {"kind": "user", "action_ref": None}
    assert any("skip" in message for _, message in result.log_lines)


def test_fail_halts_the_step_and_commits_nothing(env):
    tags, _ = env
    tags.assign(REG, "quark:rope", "queued", False, owner="user")
    result = _run(env, policies={policy_key("tag", REG, "queued"): "fail"})
    assert not result.ok
    assert "owned by the user" in result.reason
    assert tags.get_tag(REG, "quark:rope", "queued") is False
    assert tags.get_ownership(REG, "quark:rope", "queued")["kind"] == "user"


def test_an_undeclared_policy_refuses_rather_than_guessing(env):
    """Design 3.3 gives conflict policy no default; an action writing someone else's cell
    without declaring one is outside its contract and is refused."""
    tags, _ = env
    tags.assign(REG, "quark:rope", "queued", False, owner="user")
    result = _run(env)                                   # no policies at all
    assert not result.ok
    assert "declares no conflict policy" in result.reason
    assert tags.get_ownership(REG, "quark:rope", "queued")["kind"] == "user"


def test_ask_is_declined_clearly_until_it_is_implemented(env):
    tags, _ = env
    tags.assign(REG, "quark:rope", "queued", False, owner="user")
    result = _run(env, policies={policy_key("tag", REG, "queued"): "ask"})
    assert not result.ok
    assert "does not support" in result.reason


def test_conflict_with_another_action_uses_the_same_rules(env):
    tags, _ = env
    assert _run(env, action_ref="first:act").ok
    blocked = _run(env, action_ref="second:act", fn=_writer(False))
    assert not blocked.ok                                # no policy declared
    allowed = _run(env, action_ref="second:act", fn=_writer(False),
                   policies={policy_key("tag", REG, "queued"): "overwrite"})
    assert allowed.ok
    assert tags.get_ownership(REG, "quark:rope", "queued")["action_ref"] == "second:act"


# --- manifests must declare it ---------------------------------------------

_MANIFEST = """
{
  "package": { "name": "demo" },
  "actions": [
    {
      "id": "act", "file": "a.py", "function": "run",
      "mappings": {
        "target": {
          "kind": "tag", "tag_type": "bool", "registry_type": "minecraft:item",
          "access": "%s",
          %s
        },
      },
    },
  ],
}
"""


def _write_package(tmp_path, access, policy_line):
    (tmp_path / "manifest.json5").write_text(_MANIFEST % (access, policy_line), encoding="utf-8")
    return tmp_path


def test_write_mapping_without_a_policy_is_rejected_at_load(tmp_path):
    with pytest.raises(ValueError, match="conflict_policy"):
        load_package(_write_package(tmp_path, "write", ""))


def test_read_mapping_needs_no_policy(tmp_path):
    package = load_package(_write_package(tmp_path, "read", ""))
    assert package.actions[0].mappings["target"].conflict_policy is None


def test_invalid_policy_is_rejected_at_load(tmp_path):
    with pytest.raises(ValueError, match="invalid conflict_policy"):
        load_package(_write_package(tmp_path, "write", '"conflict_policy": "maybe",'))


def test_declared_policy_maps_onto_the_bound_tag(tmp_path):
    package = load_package(_write_package(tmp_path, "write", '"conflict_policy": "skip",'))
    manifest = package.actions[0]
    assert conflict_policies_for(manifest, {"target": "queued"}) == {policy_key("tag", REG, "queued"): "skip"}


# --- the file engine: hard block, not policy --------------------------------

@pytest.fixture
def store(user_db, tmp_path):
    root = tmp_path / "instance"
    root.mkdir()
    return FileStore(user_db, root), root


def test_a_user_owned_file_is_hard_blocked(store):
    fs, root = store
    (root / "f.txt").write_text("mine", encoding="utf-8")
    fs.claim("f.txt")                                    # the user owns it

    staging = FileStaging(fs)
    with pytest.raises(FileOwnershipError, match="owned by you"):
        staging.write("f.txt", "theirs", owner="action", owner_action_ref="pkg:act")
    assert (root / "f.txt").read_text(encoding="utf-8") == "mine"


def test_the_block_fails_the_whole_step_so_nothing_partial_lands(store, tags):
    """Loud by design: the action doesn't get to skip the file and carry on believing it
    wrote — the step fails and every other write it staged is discarded."""
    fs, root = store
    (root / "blocked.txt").write_text("mine", encoding="utf-8")
    fs.claim("blocked.txt")

    def action(pack):
        pack.filesystem.resolve("other.txt").write("written first")
        pack.filesystem.resolve("blocked.txt").write("nope")

    result = run_action(action, tag_store=tags, packdump=FakeDump(),
                        action_ref="pkg:act", file_store=fs)
    assert not result.ok
    assert "blocked.txt" in result.reason
    assert not (root / "other.txt").exists()             # the earlier write rolled back too


def test_untouched_and_action_owned_files_are_writable(store):
    """Only *user*-owned blocks. An untouched file is free to claim, and an action may
    take over a file another action owns."""
    fs, root = store
    staging = FileStaging(fs)
    staging.write("fresh.txt", "a", owner="action", owner_action_ref="pkg:one")
    staging.commit()
    assert fs.ownership("fresh.txt")["action_ref"] == "pkg:one"

    staging = FileStaging(fs)
    staging.write("fresh.txt", "b", owner="action", owner_action_ref="pkg:two")
    staging.commit()
    assert fs.ownership("fresh.txt")["action_ref"] == "pkg:two"


def test_the_user_can_always_write_their_own_file(store):
    fs, root = store
    (root / "f.txt").write_text("x", encoding="utf-8")
    fs.claim("f.txt")
    staging = FileStaging(fs)
    staging.write("f.txt", "edited", owner="user")       # not an action — no block
    staging.commit()
    assert (root / "f.txt").read_text(encoding="utf-8") == "edited"


def test_one_action_taking_a_file_from_another_is_allowed_but_logged(tags, tmp_path):
    """Files are open-world, so §6.1 hard-blocks only the USER — action-to-action takeover
    is correct to allow. Silent is the problem: with declaration-time detection deferred,
    two actions fighting over one file is otherwise invisible and simply last-run-wins,
    while the L2 engine logs exactly this case."""
    from packsmith.core.files import FileStore, FileStaging
    store = FileStore(tags._db, tmp_path)
    store.write("shared.json", "{}", owner="action", owner_action_ref="alpha:write")

    said = []
    staging = FileStaging(store, log=lambda level, msg: said.append((level, msg)))
    staging.write("shared.json", "new", owner="action", owner_action_ref="beta:write")

    assert said, "the takeover was silent"
    level, message = said[0]
    assert level == "info"
    assert "alpha:write" in message and "shared.json" in message


def test_an_action_rewriting_its_own_file_says_nothing(tags, tmp_path):
    """Re-asserting your own output is not a conflict and must not be noise."""
    from packsmith.core.files import FileStore, FileStaging
    store = FileStore(tags._db, tmp_path)
    store.write("mine.json", "{}", owner="action", owner_action_ref="alpha:write")
    said = []
    staging = FileStaging(store, log=lambda level, msg: said.append((level, msg)))
    staging.write("mine.json", "new", owner="action", owner_action_ref="alpha:write")
    assert said == []
