"""The GUI undo stack against tag ownership (design 3.2.1, 5.1).

The rule these all turn on: **pristine is not a value.** "Nobody has decided anything here"
and "someone decided the default" are different states, and only one of them has a row in
the database. A stack that captures the *displayed* value cannot tell them apart, so undo
invents assignments the user never made.
"""
import pytest

from packsmith.gui.table.edit_commands import BatchEditCommand, EditStack, TagEditCommand

REG = "minecraft:item"
ENTRY = "quark:rope"


@pytest.fixture
def store(tags):
    tags.define(REG, "remove", "bool", default=False)
    tags.define(REG, "tier", "string")
    return tags


def edit(store, entry, tag, new):
    """Capture prior state the way the table does, then build the command."""
    return TagEditCommand(registry_type=REG, entry_id=entry, tag_name=tag,
                          prior=store.assignment(REG, entry, tag), new_value=new)


# --- the store boundary -----------------------------------------------------

def test_assignment_distinguishes_pristine_from_an_explicit_default(store):
    assert store.get_tag(REG, ENTRY, "remove") is False        # displayed value
    assert store.assignment(REG, ENTRY, "remove") is None      # ...but no row exists

    store.assign(REG, ENTRY, "remove", False)
    prior = store.assignment(REG, ENTRY, "remove")
    assert prior.value is False and prior.owner == "user"


def test_assignment_carries_ownership(store):
    store.assign(REG, ENTRY, "remove", True, owner="action",
                 owner_action_ref="removal:nuke")
    prior = store.assignment(REG, ENTRY, "remove")
    assert (prior.owner, prior.action_ref) == ("action", "removal:nuke")


# --- undo must not fabricate ------------------------------------------------

def test_undoing_an_edit_to_a_pristine_cell_leaves_it_pristine(store):
    """The headline bug: check `remove` on an untouched item, Ctrl+Z, and the cell came
    back as an explicit user-owned False with an ownership bar."""
    stack = EditStack(store)
    stack.execute(edit(store, ENTRY, "remove", True))
    assert store.get_ownership(REG, ENTRY, "remove") == {"kind": "user", "action_ref": None}

    stack.undo()
    assert store.assignment(REG, ENTRY, "remove") is None, "undo invented an assignment"
    assert store.get_ownership(REG, ENTRY, "remove") is None


def test_undo_gives_an_action_owned_cell_back_to_its_action(store):
    """Restoring the value alone launders an action's decision into the user's, and
    changes what conflict policy does on the next run."""
    store.assign(REG, ENTRY, "remove", True, owner="action",
                 owner_action_ref="removal:nuke")
    stack = EditStack(store)
    stack.execute(edit(store, ENTRY, "remove", False))
    assert store.get_ownership(REG, ENTRY, "remove")["kind"] == "user"   # loud takeover

    stack.undo()
    assert store.get_ownership(REG, ENTRY, "remove") == {
        "kind": "action", "action_ref": "removal:nuke"}


def test_undoing_a_delete_restores_the_owner_too(store):
    store.assign(REG, ENTRY, "tier", "late", owner="action",
                 owner_action_ref="classify:go")
    stack = EditStack(store)
    stack.execute(edit(store, ENTRY, "tier", None))
    assert store.assignment(REG, ENTRY, "tier") is None

    stack.undo()
    prior = store.assignment(REG, ENTRY, "tier")
    assert (prior.value, prior.owner, prior.action_ref) == ("late", "action", "classify:go")


def test_redo_still_lands_user_owned(store):
    store.assign(REG, ENTRY, "remove", True, owner="action",
                 owner_action_ref="removal:nuke")
    stack = EditStack(store)
    stack.execute(edit(store, ENTRY, "remove", False))
    stack.undo()
    stack.redo()
    assert store.get_ownership(REG, ENTRY, "remove")["kind"] == "user"


# --- batches ----------------------------------------------------------------

def test_a_batch_undo_returns_each_cell_to_its_own_owner(store):
    """A selection can span pristine, user-owned and action-owned cells. Grouping the undo
    by value alone would hand them all to one owner."""
    store.assign(REG, "a", "tier", "early", owner="user")
    store.assign(REG, "b", "tier", "early", owner="action",
                 owner_action_ref="classify:go")
    # "c" stays pristine
    stack = EditStack(store)
    stack.execute(BatchEditCommand("set tier", [
        edit(store, e, "tier", "late") for e in ("a", "b", "c")]))
    assert [store.get_tag(REG, e, "tier") for e in ("a", "b", "c")] == ["late"] * 3

    stack.undo()
    assert store.assignment(REG, "a", "tier").owner == "user"
    assert store.assignment(REG, "b", "tier").action_ref == "classify:go"
    assert store.assignment(REG, "c", "tier") is None, "pristine cell was materialised"


def test_a_batch_undo_restores_differing_values(store):
    store.assign(REG, "a", "tier", "early")
    store.assign(REG, "b", "tier", "mid")
    stack = EditStack(store)
    stack.execute(BatchEditCommand("set tier", [
        edit(store, e, "tier", "late") for e in ("a", "b")]))
    stack.undo()
    assert [store.get_tag(REG, e, "tier") for e in ("a", "b")] == ["early", "mid"]


# --- when the world moves under the stack -----------------------------------
#
# A stack outlives the shape of the data it describes. Undefine a tag, drop a value from
# an enum, and an edit from ten minutes ago wants to restore a state the store will now
# refuse. Every one of these was found by attacking the stack rather than by using it.

from packsmith.gui.table.edit_commands import UndoBlocked


def test_a_refused_undo_does_not_destroy_the_command(store):
    """The nastiest of them. `undo()` popped the command, *then* applied it — so a refusal
    left the edit in neither stack. Un-undoable and un-redoable, gone, with the exception
    continuing on into Qt's shortcut handler.
    """
    stack = EditStack(store)
    store.assign(REG, ENTRY, "tier", "original")
    stack.execute(edit(store, ENTRY, "tier", "edited"))
    store.undefine(REG, "tier")

    with pytest.raises(UndoBlocked):
        stack.undo()

    assert stack.can_undo, "the edit was swallowed by its own failure"
    assert not stack.can_redo, "a failed undo must not count as done"


def test_a_refused_undo_can_be_retried_once_the_cause_is_fixed(store):
    """Which is the point of keeping it: the refusal is a condition, not a verdict."""
    stack = EditStack(store)
    store.assign(REG, ENTRY, "tier", "original")
    stack.execute(edit(store, ENTRY, "tier", "edited"))
    store.undefine(REG, "tier")
    with pytest.raises(UndoBlocked):
        stack.undo()

    store.define(REG, "tier", "string")           # the user puts the tag back
    assert stack.undo()
    assert store.get_tag(REG, ENTRY, "tier") == "original"


def test_the_refusal_says_what_the_store_said(store):
    """"Can't undo" alone is useless — the reason is what tells you it is fixable."""
    stack = EditStack(store)
    store.assign(REG, ENTRY, "tier", "original")
    stack.execute(edit(store, ENTRY, "tier", "edited"))
    store.undefine(REG, "tier")

    with pytest.raises(UndoBlocked) as caught:
        stack.undo()
    assert "tier" in caught.value.reason


def test_an_enum_value_dropped_from_its_definition_blocks_rather_than_crashes(tags):
    """Editing a definition is allowed; the old value stops being writable. Undo must
    refuse in a way the UI can report, not raise a bare ValueError at a shortcut."""
    tags.define(REG, "phase", "enum", enum_values=["early", "late"])
    tags.assign(REG, ENTRY, "phase", "early")
    stack = EditStack(tags)
    stack.execute(TagEditCommand(REG, ENTRY, "phase",
                                 tags.assignment(REG, ENTRY, "phase"), "late"))

    tags.undefine(REG, "phase")
    tags.define(REG, "phase", "enum", enum_values=["late"])
    with pytest.raises(UndoBlocked):
        stack.undo()
    assert stack.can_undo


def test_a_batch_that_cannot_finish_reverts_nothing(tags):
    """A batch is several writes. A refusal partway used to leave half the selection
    reverted and half not — and nothing anywhere recording which half."""
    tags.define(REG, "phase", "enum", enum_values=["early", "late"])
    tags.assign(REG, "mod:a", "phase", "early")
    tags.assign(REG, "mod:b", "phase", "late")
    stack = EditStack(tags)
    stack.execute(BatchEditCommand("bulk", [
        TagEditCommand(REG, "mod:a", "phase", tags.assignment(REG, "mod:a", "phase"), "late"),
        TagEditCommand(REG, "mod:b", "phase", tags.assignment(REG, "mod:b", "phase"), "late"),
    ]))
    assert [tags.get_tag(REG, e, "phase") for e in ("mod:a", "mod:b")] == ["late", "late"]

    # 'early' is no longer writable, so restoring mod:a must fail — and mod:b must not be
    # quietly restored on its own.
    tags.undefine(REG, "phase")
    tags.define(REG, "phase", "enum", enum_values=["late"])
    tags.assign(REG, "mod:a", "phase", "late")
    tags.assign(REG, "mod:b", "phase", "late")

    with pytest.raises(UndoBlocked):
        stack.undo()
    assert [tags.get_tag(REG, e, "phase") for e in ("mod:a", "mod:b")] == ["late", "late"], \
        "half the batch was reverted"


def test_a_refused_redo_keeps_the_command_too(store):
    """Same invariant, mirrored. Redo popped before applying as well."""
    stack = EditStack(store)
    store.assign(REG, ENTRY, "tier", "original")
    stack.execute(edit(store, ENTRY, "tier", "edited"))
    stack.undo()
    assert stack.can_redo

    store.undefine(REG, "tier")
    with pytest.raises(UndoBlocked):
        stack.redo()
    assert stack.can_redo, "the redo was swallowed by its own failure"
