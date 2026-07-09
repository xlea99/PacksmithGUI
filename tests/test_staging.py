"""Per-step L2 staging: read-your-writes, atomic commit, discard (design 1.1)."""
import pytest

from packsmith.core.staging import L2Staging

REG, ENTRY, TAG = "minecraft:item", "quark:rope", "remove"


@pytest.fixture
def staging(tags):
    """An L2Staging buffer over a TagStore with a `remove` bool tag (default False)."""
    tags.define(REG, "remove", "bool", default=False)
    return L2Staging(tags)


def test_write_does_not_touch_store_until_commit(staging, tags):
    staging.write(REG, ENTRY, TAG, True, owner="action", owner_action_ref="x:y")
    assert tags.get_ownership(REG, ENTRY, TAG) is None       # store still pristine
    assert tags.get_tag(REG, ENTRY, TAG) is False


def test_read_your_writes(staging, tags):
    staging.write(REG, ENTRY, TAG, True, owner="user")
    assert staging.read(REG, ENTRY, TAG) is True             # sees its own staged write
    assert staging.read_ownership(REG, ENTRY, TAG) == {"kind": "user", "action_ref": None}
    assert tags.get_tag(REG, ENTRY, TAG) is False            # ...but the store hasn't changed


def test_commit_applies_value_and_ownership(staging, tags):
    staging.write(REG, ENTRY, TAG, True, owner="action", owner_action_ref="x:y")
    staging.commit()
    assert tags.get_tag(REG, ENTRY, TAG) is True
    assert tags.get_ownership(REG, ENTRY, TAG) == {"kind": "action", "action_ref": "x:y"}
    assert not staging.has_pending


def test_discard_leaves_store_untouched(staging, tags):
    staging.write(REG, ENTRY, TAG, True, owner="user")
    staging.discard()
    assert tags.get_ownership(REG, ENTRY, TAG) is None
    assert not staging.has_pending


def test_unstaged_read_falls_through_to_store(staging, tags):
    tags.assign(REG, ENTRY, TAG, True, owner="user")         # committed directly
    assert staging.read(REG, ENTRY, TAG) is True
    assert staging.read_ownership(REG, ENTRY, TAG) == {"kind": "user", "action_ref": None}


def test_staged_delete_reads_as_pristine_default(staging, tags):
    tags.assign(REG, ENTRY, TAG, True, owner="user")         # store has committed True
    staging.delete(REG, ENTRY, TAG)
    assert staging.read(REG, ENTRY, TAG) is False            # reads as default (pristine-to-be)
    assert staging.read_ownership(REG, ENTRY, TAG) is None
    assert tags.get_tag(REG, ENTRY, TAG) is True             # store unchanged until commit
    staging.commit()
    assert tags.get_ownership(REG, ENTRY, TAG) is None       # now genuinely pristine


def test_last_write_wins_before_commit(staging, tags):
    staging.write(REG, ENTRY, TAG, True, owner="user")
    staging.write(REG, ENTRY, TAG, False, owner="action", owner_action_ref="x:y")
    staging.commit()
    assert tags.get_tag(REG, ENTRY, TAG) is False
    assert tags.get_ownership(REG, ENTRY, TAG) == {"kind": "action", "action_ref": "x:y"}
