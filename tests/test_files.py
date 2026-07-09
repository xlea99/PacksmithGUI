"""File writes + whole-file ownership — the open-world engine (design 6.0/6.1)."""
import pytest

from packsmith.core.files import FileStore, FileStaging
from packsmith.core.pack import _FileHandle
from packsmith.core.runner import run_action


class FakeDump:
    registry = {}

    def attribute(self, registry_type, entry_id, name):
        return None


@pytest.fixture
def store(user_db, tmp_path):
    root = tmp_path / "instance"
    root.mkdir()
    return FileStore(user_db, root), root


# --- FileStore -------------------------------------------------------------

def test_write_creates_file_and_records_ownership(store):
    fs, root = store
    prior = fs.write("config/obliterator.json", "{}", owner="action", owner_action_ref="removal:nuke")
    assert prior is None                                        # nothing was there before
    assert (root / "config" / "obliterator.json").read_text(encoding="utf-8") == "{}"
    assert fs.ownership("config/obliterator.json") == {"kind": "action", "action_ref": "removal:nuke"}


def test_write_returns_prior_content_for_snapshot(store):
    fs, root = store
    fs.write("a.txt", "one", owner="user")
    prior = fs.write("a.txt", "two", owner="action", owner_action_ref="x:y")
    assert prior == "one"
    assert fs.read("a.txt") == "two"


def test_read_and_exists(store):
    fs, root = store
    assert fs.read("nope.txt") is None and not fs.exists("nope.txt")
    fs.write("here.txt", "hi", owner="user")
    assert fs.read("here.txt") == "hi" and fs.exists("here.txt")


def test_untouched_file_has_no_ownership(store):
    fs, root = store
    (root / "manual.txt").write_text("hand-made", encoding="utf-8")
    assert fs.exists("manual.txt")
    assert fs.ownership("manual.txt") is None                   # on disk but untracked


def test_path_escaping_instance_root_is_rejected(store):
    fs, _ = store
    with pytest.raises(ValueError):
        fs.write("../escape.txt", "nope", owner="user")


def test_file_must_exist_guard(store):
    fs, _ = store
    with pytest.raises(FileNotFoundError):
        fs.write("ghost.txt", "x", owner="action", owner_action_ref="x:y", file_must_exist=True)


# --- FileStaging -----------------------------------------------------------

def test_staging_defers_write_until_commit(store):
    fs, root = store
    st = FileStaging(fs)
    st.write("f.txt", "staged", owner="action", owner_action_ref="x:y")
    assert not (root / "f.txt").exists()                        # not on disk yet
    assert st.read("f.txt") == "staged"                         # read-your-writes
    assert st.ownership("f.txt") == {"kind": "action", "action_ref": "x:y"}
    st.commit()
    assert (root / "f.txt").read_text(encoding="utf-8") == "staged"


def test_staging_discard_writes_nothing(store):
    fs, root = store
    st = FileStaging(fs)
    st.write("f.txt", "x", owner="action", owner_action_ref="x:y")
    st.discard()
    assert not (root / "f.txt").exists()


def test_staging_captures_prior_snapshot_on_commit(store):
    fs, root = store
    fs.write("f.txt", "old", owner="user")
    st = FileStaging(fs)
    st.write("f.txt", "new", owner="action", owner_action_ref="x:y")
    st.commit()
    # prior content AND ownership captured for rollback
    assert st.snapshots["f.txt"] == {"content": "old", "ownership": {"kind": "user", "action_ref": None}}


# --- end-to-end through the runner -----------------------------------------

def test_runner_action_writes_file_action_owned(user_db, tmp_path, tags):
    root = tmp_path / "instance"; root.mkdir()
    fs = FileStore(user_db, root)

    def write_action(pack):
        pack.filesystem.resolve("config/obliterator.json").write('{"remove": ["quark:rope"]}')

    result = run_action(write_action, tag_store=tags, packdump=FakeDump(),
                        action_ref="removal:nuke", file_store=fs)
    assert result.ok
    assert (root / "config" / "obliterator.json").read_text(encoding="utf-8") == '{"remove": ["quark:rope"]}'
    assert fs.ownership("config/obliterator.json") == {"kind": "action", "action_ref": "removal:nuke"}


def test_runner_fail_discards_file_write(user_db, tmp_path, tags):
    root = tmp_path / "instance"; root.mkdir()
    fs = FileStore(user_db, root)

    def bad(pack):
        pack.filesystem.resolve("config/x.json").write("{}")   # staged...
        pack.fail("nope")                                       # ...then bail

    result = run_action(bad, tag_store=tags, packdump=FakeDump(),
                        action_ref="demo:bad", file_store=fs)
    assert not result.ok
    assert not (root / "config" / "x.json").exists()            # never hit disk


def test_filesystem_unavailable_without_a_store(tags):
    def uses_fs(pack):
        pack.filesystem.resolve("x.txt").write("y")

    result = run_action(uses_fs, tag_store=tags, packdump=FakeDump(), action_ref="demo:x")
    assert not result.ok
    assert "filesystem" in result.reason.lower()


# --- read_json / write_json (JSON5-tolerant, whole-file) --------------------

JSON5_SAMPLE = """{
  // a line comment
  "configVersion": 2,
  "blacklisted_items": ["examplemod:example_item"],
  "use_hashmap_optimizations": false,
}"""


def test_read_json_parses_json5_with_comments_and_trailing_commas(store):
    fs, root = store
    (root / "config").mkdir()
    (root / "config" / "c.json5").write_text(JSON5_SAMPLE, encoding="utf-8")
    data = _FileHandle(FileStaging(fs), "config/c.json5", "x:y").read_json()
    assert data["configVersion"] == 2
    assert data["use_hashmap_optimizations"] is False
    assert data["blacklisted_items"] == ["examplemod:example_item"]


def test_read_json_missing_file_is_none(store):
    fs, _ = store
    assert _FileHandle(FileStaging(fs), "nope.json5", "x:y").read_json() is None


def test_write_json_edits_one_key_and_preserves_the_rest(store):
    fs, root = store
    (root / "config").mkdir()
    (root / "config" / "c.json5").write_text(JSON5_SAMPLE, encoding="utf-8")

    staging = FileStaging(fs)
    handle = _FileHandle(staging, "config/c.json5", "removal:nuke")
    data = handle.read_json()
    data["blacklisted_items"] = ["a:one", "a:two"]
    handle.write_json(data)
    staging.commit()

    reread = _FileHandle(FileStaging(fs), "config/c.json5", "x:y").read_json()
    assert reread["blacklisted_items"] == ["a:one", "a:two"]     # edited key
    assert reread["configVersion"] == 2                          # other keys preserved
    assert reread["use_hashmap_optimizations"] is False
    assert fs.ownership("config/c.json5") == {"kind": "action", "action_ref": "removal:nuke"}
