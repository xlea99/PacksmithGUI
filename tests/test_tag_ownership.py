"""Ownership lifecycle for tag assignments (design 3.2.1).

Ownership is a property of a row's EXISTENCE, not its value: no row = pristine
(nobody owns it); any row that exists has exactly one owner — the user, or a
specific action. "Whoever writes, owns" — the loud transfer confirmation and
action conflict policies live ABOVE this layer, not inside it.
"""
import pytest

REG = "minecraft:item"
ENTRY = "quark:rope"
TAG = "remove"


@pytest.fixture
def tags_remove(tags):
    """A TagStore with a `remove` bool tag (default False) already defined."""
    tags.define(REG, "remove", "bool", default=False)
    return tags


def test_pristine_cell_has_no_owner(tags_remove):
    assert tags_remove.get_ownership(REG, ENTRY, TAG) is None


def test_default_value_shows_without_creating_ownership(tags_remove):
    # A pristine cell displays the tag's default, but displaying a default must
    # never make the cell owned — this is the pristine-vs-owned-false crux.
    assert tags_remove.get_tag(REG, ENTRY, TAG) is False
    assert tags_remove.get_ownership(REG, ENTRY, TAG) is None


def test_user_assign_creates_user_ownership(tags_remove):
    tags_remove.assign(REG, ENTRY, TAG, True)
    assert tags_remove.get_tag(REG, ENTRY, TAG) is True
    assert tags_remove.get_ownership(REG, ENTRY, TAG) == {"kind": "user", "action_ref": None}


def test_explicit_false_is_not_pristine(tags_remove):
    # Setting a bool to False explicitly is an OWNED decision, not a reset to pristine.
    tags_remove.assign(REG, ENTRY, TAG, False)
    assert tags_remove.get_tag(REG, ENTRY, TAG) is False
    assert tags_remove.get_ownership(REG, ENTRY, TAG) is not None


def test_action_write_stamps_action_ownership(tags_remove):
    tags_remove.assign(REG, ENTRY, TAG, True, owner="action", owner_action_ref="removal_suite:nuke")
    assert tags_remove.get_ownership(REG, ENTRY, TAG) == {
        "kind": "action", "action_ref": "removal_suite:nuke",
    }


def test_user_takes_over_action_owned_cell(tags_remove):
    tags_remove.assign(REG, ENTRY, TAG, True, owner="action", owner_action_ref="removal_suite:nuke")
    tags_remove.assign(REG, ENTRY, TAG, False)  # user edits it -> transfer
    assert tags_remove.get_ownership(REG, ENTRY, TAG) == {"kind": "user", "action_ref": None}


def test_action_reclaims_after_user(tags_remove):
    tags_remove.assign(REG, ENTRY, TAG, True)  # user
    tags_remove.assign(REG, ENTRY, TAG, False, owner="action", owner_action_ref="x:y")
    assert tags_remove.get_ownership(REG, ENTRY, TAG) == {"kind": "action", "action_ref": "x:y"}


def test_unassign_returns_to_pristine(tags_remove):
    tags_remove.assign(REG, ENTRY, TAG, True)
    tags_remove.unassign(REG, ENTRY, TAG)
    assert tags_remove.get_ownership(REG, ENTRY, TAG) is None
    assert tags_remove.get_tag(REG, ENTRY, TAG) is False  # back to the default


def test_bulk_assign_stamps_every_entry(tags_remove):
    entries = ["a:1", "a:2", "a:3"]
    tags_remove.assign(REG, entries, TAG, True, owner="action", owner_action_ref="x:y")
    for e in entries:
        assert tags_remove.get_ownership(REG, e, TAG) == {"kind": "action", "action_ref": "x:y"}


@pytest.mark.parametrize("bad_kwargs", [
    {"owner": "action"},                            # action ownership requires a ref
    {"owner": "user", "owner_action_ref": "x:y"},   # user ownership must not carry a ref
    {"owner": "wizard"},                            # invalid owner kind
])
def test_ownership_validation_rejects_bad_input(tags_remove, bad_kwargs):
    with pytest.raises(ValueError):
        tags_remove.assign(REG, ENTRY, TAG, True, **bad_kwargs)
