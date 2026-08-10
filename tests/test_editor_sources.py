"""Saving from an open editor, against §6.1 ownership.

§6.1's rule is "never silent overwrites", and editing an action-owned file is locked until
an explicit take. The lock used to live only in the editor's read-only flag, decided when
the document opened — so the rule was enforced by a snapshot rather than by the write.
"""
import pytest

from packsmith.core.files import FileStore, FileOwnershipError
from packsmith.gui.editor.sources import InstanceFileSource


@pytest.fixture
def source(tags, tmp_path):
    files = FileStore(tags._db, tmp_path)
    (tmp_path / "cfg.json").write_text('{"v": "original"}', encoding="utf-8")
    return InstanceFileSource(files), files


def test_saving_an_untouched_file_claims_it_for_the_user(source):
    src, files = source
    src.write("cfg.json", '{"v": "mine"}')
    assert files.read("cfg.json") == '{"v": "mine"}'
    assert files.ownership("cfg.json")["kind"] == "user"


def test_saving_a_file_you_already_own_is_fine(source):
    src, files = source
    files.claim("cfg.json", owner="user")
    src.write("cfg.json", '{"v": "mine"}')
    assert files.read("cfg.json") == '{"v": "mine"}'


def test_a_stale_buffer_cannot_overwrite_an_actions_output(source):
    """The reported path: open a file, a job's action writes it, Ctrl+S. The buffer is
    pre-run, so saving destroyed generated content AND took ownership, with no prompt."""
    src, files = source
    stale = src.read("cfg.json")                       # what the open tab is holding

    files.write("cfg.json", '{"v": "generated"}', owner="action",
                owner_action_ref="palette:fill")

    with pytest.raises(FileOwnershipError, match="palette:fill"):
        src.write("cfg.json", stale)
    assert files.read("cfg.json") == '{"v": "generated"}', "the action's output survived"
    assert files.ownership("cfg.json")["kind"] == "action", "ownership did not transfer"


def test_the_refusal_names_the_action_and_the_way_out(source):
    src, files = source
    files.write("cfg.json", "x", owner="action", owner_action_ref="palette:fill")
    with pytest.raises(FileOwnershipError) as caught:
        src.write("cfg.json", "y")
    message = str(caught.value)
    assert "palette:fill" in message
    assert "take ownership" in message.lower()


def test_taking_ownership_explicitly_then_saving_works(source):
    """The ceremony §6.1 requires — refusing is not a dead end."""
    src, files = source
    files.write("cfg.json", '{"v": "generated"}', owner="action",
                owner_action_ref="palette:fill")
    assert src.can_unlock("cfg.json")
    src.unlock("cfg.json")
    src.write("cfg.json", '{"v": "mine now"}')
    assert files.read("cfg.json") == '{"v": "mine now"}'
    assert files.ownership("cfg.json")["kind"] == "user"


def test_the_guard_is_on_the_write_not_just_the_ui(source):
    """`read_only_reason` knew all along; nothing consulted it at save time."""
    src, files = source
    files.write("cfg.json", "x", owner="action", owner_action_ref="palette:fill")
    assert src.read_only_reason("cfg.json")
    with pytest.raises(FileOwnershipError):
        src.write("cfg.json", "y")
