"""The `pack` capability object: L2 tag access via staging, registry reads,
log, fail, and step bindings/config (design 7.x)."""
import pytest

from packsmith.core.bindings import policy_key

from packsmith.core.staging import L2Staging
from packsmith.core.pack import Pack, ActionFailure

REG, ENTRY, TAG = "minecraft:item", "quark:rope", "remove"


class FakeDump:
    """Minimal packdump stand-in for registry reads — just what _Registry touches."""

    def __init__(self, registries, names=None):
        self.registry = {rt: {"values": list(v)} for rt, v in registries.items()}
        self._names = names or {}

    def attribute(self, registry_type, entry_id, name):
        if name != "localization":
            return None
        return self._names.get((registry_type, entry_id))


@pytest.fixture
def pack(tags):
    tags.define(REG, "remove", "bool", default=False)
    staging = L2Staging(tags)
    dump = FakeDump(
        {"minecraft:item": ["quark:rope", "minecraft:diamond"]},
        names={("minecraft:item", "quark:rope"): "Rope"},
    )
    p = Pack(staging=staging, tag_store=tags, packdump=dump,
             action_ref="removal_suite:nuke",
             mappings={"target_items": "remove"}, config={"dry_run": True})
    return p, staging, tags


def test_write_stages_and_stamps_action_ownership(pack):
    p, staging, tags = pack
    p.tags.write(REG, ENTRY, TAG, True)
    assert tags.get_ownership(REG, ENTRY, TAG) is None          # not committed yet
    staging.commit()
    assert tags.get_tag(REG, ENTRY, TAG) is True
    assert tags.get_ownership(REG, ENTRY, TAG) == {
        "kind": "action", "action_ref": "removal_suite:nuke",
    }


def test_get_is_read_your_writes(pack):
    p, staging, tags = pack
    p.tags.write(REG, ENTRY, TAG, True)
    assert p.tags.get(REG, ENTRY, TAG) is True                  # sees own staged write
    assert p.tags.ownership(REG, ENTRY, TAG) == {"kind": "action", "action_ref": "removal_suite:nuke"}


def test_query_reads_committed_store(pack):
    p, staging, tags = pack
    tags.assign(REG, ENTRY, TAG, True, owner="user")
    assert set(p.tags.query(REG, "remove", True)) == {ENTRY}


def test_an_action_may_retract_what_it_owns(pack):
    """Clearing its own output is an action undoing itself, not erasing anybody."""
    p, staging, tags = pack
    p.tags.write(REG, ENTRY, TAG, True)
    staging.commit()
    p.tags.clear(REG, ENTRY, TAG)
    staging.commit()
    assert tags.get_ownership(REG, ENTRY, TAG) is None


def test_an_action_cannot_delete_what_the_user_owns(pack):
    """3.2.1: "actions cannot fully delete an assignment, only write over it." Returning a
    cell to pristine destroys the record of a decision rather than superseding it, and
    pristine is a state only the user can produce."""
    p, staging, tags = pack
    tags.assign(REG, ENTRY, TAG, True, owner="user")
    with pytest.raises(ActionFailure, match="may not delete what it does not own"):
        p.tags.clear(REG, ENTRY, TAG)
    staging.commit()
    assert tags.get_ownership(REG, ENTRY, TAG) == {"kind": "user", "action_ref": None}


def test_an_action_cannot_delete_another_actions_assignment(pack):
    p, staging, tags = pack
    tags.assign(REG, ENTRY, TAG, True, owner="action", owner_action_ref="other:thing")
    with pytest.raises(ActionFailure, match="'other:thing'"):
        p.tags.clear(REG, ENTRY, TAG)


def test_overwrite_policy_does_not_license_a_delete(pack):
    """A declared policy governs WRITES. `overwrite` means "I may take this cell", never
    "I may erase it" — there is no delete policy in 3.3 because deletes are user-only."""
    p, staging, tags = pack
    p.tags._policies = {policy_key("tag", REG, TAG): "overwrite"}
    tags.assign(REG, ENTRY, TAG, True, owner="user")
    p.tags.write(REG, ENTRY, TAG, False)                    # taking it: allowed
    staging.commit()
    assert tags.get_ownership(REG, ENTRY, TAG)["kind"] == "action"

    tags.assign(REG, "minecraft:diamond", TAG, True, owner="user")
    with pytest.raises(ActionFailure, match="may not delete"):
        p.tags.clear(REG, "minecraft:diamond", TAG)


def test_clearing_something_pristine_is_a_no_op(pack):
    p, staging, tags = pack
    p.tags.clear(REG, ENTRY, TAG)                           # nothing there; must not raise
    staging.commit()
    assert tags.get_ownership(REG, ENTRY, TAG) is None


def test_fail_raises_action_failure(pack):
    p, _, _ = pack
    with pytest.raises(ActionFailure) as exc:
        p.fail("nope")
    assert exc.value.reason == "nope"


def test_log_collects_lines(pack):
    p, _, _ = pack
    p.log("info", "hello")
    p.log("warn", "careful")
    assert p.log_lines == [("info", "hello"), ("warn", "careful")]


def test_registry_reads(pack):
    p, _, _ = pack
    assert p.registry.entries("minecraft:item") == ["quark:rope", "minecraft:diamond"]
    assert p.registry.has("minecraft:item", "quark:rope")
    assert not p.registry.has("minecraft:item", "nope:nope")
    assert p.registry.attribute("minecraft:item", "quark:rope", "localization") == "Rope"
    # None when the entry has no localization — the raw-id fallback is a renderer concern, not the attribute
    assert p.registry.attribute("minecraft:item", "minecraft:diamond", "localization") is None


def test_step_exposes_bindings_and_config(pack):
    p, _, _ = pack
    assert p.step.mappings["target_items"] == "remove"
    assert p.step.config["dry_run"] is True


def test_an_action_cannot_take_a_cell_and_then_clear_it(pack):
    """Two individually-legal calls composing into something §3.2.1 forbids: `overwrite`
    licenses TAKING a user cell, and an action may retract its own work — but together
    they turn user-owned into pristine, which only the user may produce."""
    p, staging, tags = pack
    p.tags._policies = {policy_key("tag", REG, TAG): "overwrite"}
    tags.assign(REG, ENTRY, TAG, True, owner="user")

    p.tags.write(REG, ENTRY, TAG, False)            # legal: policy allows the take
    with pytest.raises(ActionFailure, match="taken from its previous owner"):
        p.tags.clear(REG, ENTRY, TAG)               # would erase the user's record


def test_an_action_can_still_retract_what_it_created_this_step(pack):
    """The carve-out has to survive: writing to a PRISTINE cell and then clearing it
    leaves the world exactly as it was, and erases nobody."""
    p, staging, tags = pack
    p.tags.write(REG, ENTRY, TAG, True)
    p.tags.clear(REG, ENTRY, TAG)
    staging.commit()
    assert tags.get_ownership(REG, ENTRY, TAG) is None
