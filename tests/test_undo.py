"""Cell-level undo across both L2 engines (design 9.3.3).

`test_edit_stack.py` covers the registry table's side and predates this; it now runs on the
same core, which is the point. What this file adds is the two things that were new:

* **Blueprints have undo at all.** Typing into the grid wrote straight through, so Ctrl+Z
  did nothing and there was no way back from a mistyped cell short of restoring a snapshot.
* **A move refuses when something else has written the cell.** The old stack caught only
  what the *store* rejected. An action run that overwrote your cell was invisible to it, so
  undo would restore your old value over the action's and report success. Silent, and the
  person who lost the write had no way to know.

The failures worth guarding are all of the quiet kind: undo that reports success while
destroying something, or that launders ownership. A stack that visibly does nothing is
self-reporting; these are not.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.blueprints import BlueprintStore
from packsmith.core.undo import (
    Batch, BindingEngine, Edit, TagEngine, UndoBlocked, UndoStack)
from packsmith.core.tags import TagStore
from tests.test_blueprints import FakeDump

REG = "minecraft:item"
ENTRY = "minecraft:stick"


@pytest.fixture
def blueprints(user_db):
    store = BlueprintStore(user_db, packdump=FakeDump())
    store.define("StoneType")
    store.add_slot("StoneType", "base", "registry", registry_type="minecraft:block")
    store.add_slot("StoneType", "wall", "registry", registry_type="minecraft:block")
    store.create_instance("StoneType", "granite")
    store.create_instance("StoneType", "andesite")
    return store


@pytest.fixture
def stack(user_db, blueprints):
    return UndoStack(user_db, [BindingEngine(blueprints)])


def key(instance, path="base"):
    return ("StoneType", instance, path)


def edit_of(stack, instance, path, value, *, owner="user", ref=None):
    """Make a change the way a view does — capture, write, record."""
    before = stack.capture("binding", key(instance, path))
    store = stack._engines["binding"]._store
    if value is None:
        store.unbind("StoneType", instance, path)
    else:
        store.bind("StoneType", instance, path, value, owner=owner, action_ref=ref)
    after = stack.capture("binding", key(instance, path))
    batch = Batch(f"{instance}.{path}", [Edit("binding", key(instance, path),
                                              before, after)])
    stack.push(batch)
    return batch


def value(blueprints, instance, path="base"):
    return blueprints.value_of("StoneType", instance, path)


# --- the round trip -------------------------------------------------------------------

def test_a_binding_can_be_undone_and_redone(stack, blueprints):
    edit_of(stack, "granite", "base", "minecraft:granite")
    assert value(blueprints, "granite") == "minecraft:granite"

    stack.undo()
    assert value(blueprints, "granite") is None
    stack.redo()
    assert value(blueprints, "granite") == "minecraft:granite"


def test_undo_walks_back_more_than_one_edit(stack, blueprints):
    edit_of(stack, "granite", "base", "minecraft:granite")
    edit_of(stack, "granite", "base", "minecraft:andesite")

    stack.undo()
    assert value(blueprints, "granite") == "minecraft:granite"
    stack.undo()
    assert value(blueprints, "granite") is None
    assert stack.can_undo is False


def test_unbinding_is_undoable_too(stack, blueprints):
    """An empty cell is a state, not the absence of one — clearing a cell by mistake is
    exactly as costly as mistyping it."""
    blueprints.bind("StoneType", "granite", "base", "minecraft:granite")
    edit_of(stack, "granite", "base", None)
    assert value(blueprints, "granite") is None

    stack.undo()
    assert value(blueprints, "granite") == "minecraft:granite"


def test_a_new_edit_clears_the_redo_stack(stack, blueprints):
    edit_of(stack, "granite", "base", "minecraft:granite")
    stack.undo()
    assert stack.can_redo is True

    edit_of(stack, "granite", "base", "minecraft:andesite")
    assert stack.can_redo is False


def test_undoing_nothing_is_not_an_error(stack):
    assert stack.undo() is None
    assert stack.redo() is None


# --- ownership, which is the quiet one -------------------------------------------------

def test_undo_gives_an_action_owned_cell_back_to_its_action(stack, blueprints):
    """Restoring the value alone launders an action's data into a user decision, and the
    next run's conflict policy then sees a cell nobody owns."""
    blueprints.bind("StoneType", "granite", "base", "minecraft:granite",
                    owner="action", action_ref="deep_end:stones")
    edit_of(stack, "granite", "base", "minecraft:andesite")

    stack.undo()

    restored = blueprints.binding("StoneType", "granite", "base")
    assert restored.value == "minecraft:granite"
    assert (restored.owner, restored.action_ref) == ("action", "deep_end:stones")


def test_redo_lands_the_edit_back_under_the_user(stack, blueprints):
    blueprints.bind("StoneType", "granite", "base", "minecraft:granite",
                    owner="action", action_ref="deep_end:stones")
    edit_of(stack, "granite", "base", "minecraft:andesite")
    stack.undo()
    stack.redo()

    again = blueprints.binding("StoneType", "granite", "base")
    assert (again.value, again.owner, again.action_ref) == \
        ("minecraft:andesite", "user", None)


# --- staleness: the reason this is safe -------------------------------------------------

def test_undo_refuses_when_something_else_wrote_the_cell(stack, blueprints):
    """The silent one. An action run between your edit and your Ctrl+Z would have had its
    write replaced by your old value, with undo reporting success."""
    edit_of(stack, "granite", "base", "minecraft:granite")
    blueprints.bind("StoneType", "granite", "base", "minecraft:andesite",
                    owner="action", action_ref="deep_end:stones")

    with pytest.raises(UndoBlocked, match="something else wrote it"):
        stack.undo()

    survived = blueprints.binding("StoneType", "granite", "base")
    assert (survived.value, survived.owner) == ("minecraft:andesite", "action")


def test_a_refused_undo_keeps_the_batch(stack, blueprints):
    """A refusal is a condition, not a verdict — undo the cause and it is still there."""
    edit_of(stack, "granite", "base", "minecraft:granite")
    blueprints.bind("StoneType", "granite", "base", "minecraft:andesite")

    with pytest.raises(UndoBlocked):
        stack.undo()
    assert stack.can_undo is True
    assert stack.can_redo is False, "a failed undo must not count as done"

    blueprints.bind("StoneType", "granite", "base", "minecraft:granite")   # put it back
    assert stack.undo() is not None
    assert value(blueprints, "granite") is None


def test_an_emptied_cell_is_not_a_conflict(stack, blueprints):
    """The rule is "never overwrite a value it did not write", not "the cell must be
    untouched". Absence holds no decision to destroy, and treating it as a conflict would
    break undefine-then-redefine — see test_edit_stack's retry case."""
    edit_of(stack, "granite", "base", "minecraft:granite")
    blueprints.unbind("StoneType", "granite", "base")

    assert stack.undo() is not None
    assert value(blueprints, "granite") is None


def test_a_refusal_names_the_cell(stack, blueprints):
    """"Can't undo" alone is useless — which cell is what makes it actionable."""
    edit_of(stack, "granite", "wall", "minecraft:granite")
    blueprints.bind("StoneType", "granite", "wall", "minecraft:andesite")

    with pytest.raises(UndoBlocked) as caught:
        stack.undo()
    assert "granite.wall" in caught.value.reason


# --- batches --------------------------------------------------------------------------

def test_a_batch_is_one_undo(stack, blueprints):
    """§9.3.3: one gesture, one entry. Filling forty cells must not need forty Ctrl+Zs."""
    edits = []
    for instance in ("granite", "andesite"):
        before = stack.capture("binding", key(instance))
        blueprints.bind("StoneType", instance, "base", f"minecraft:{instance}")
        edits.append(Edit("binding", key(instance), before,
                          stack.capture("binding", key(instance))))
    stack.push(Batch("fill base", edits))

    stack.undo()

    assert value(blueprints, "granite") is None
    assert value(blueprints, "andesite") is None
    assert stack.can_undo is False


def test_a_batch_refuses_as_a_whole(stack, blueprints):
    """All-or-nothing. Half a selection reverted, with no record of which half, is worse
    than nothing happening."""
    edits = []
    for instance in ("granite", "andesite"):
        before = stack.capture("binding", key(instance))
        blueprints.bind("StoneType", instance, "base", f"minecraft:{instance}")
        edits.append(Edit("binding", key(instance), before,
                          stack.capture("binding", key(instance))))
    stack.push(Batch("fill base", edits))
    blueprints.bind("StoneType", "andesite", "base", "minecraft:polished_andesite")

    with pytest.raises(UndoBlocked):
        stack.undo()

    assert value(blueprints, "granite") == "minecraft:granite", \
        "the untouched half was reverted anyway"


def test_an_empty_batch_is_not_recorded(stack):
    """So a view can push unconditionally without first asking whether anything changed."""
    stack.push(Batch("nothing", []))
    assert stack.can_undo is False


# --- both engines, one stack ------------------------------------------------------------

def test_one_stack_reverses_tags_and_bindings_together(user_db, blueprints):
    """The claim §9.3.3 rests on: the two engines differ by a key tuple and a store method,
    so a single batch can legitimately span both."""
    tags = TagStore(user_db)
    tags.define(REG, "remove", "bool")
    stack = UndoStack(user_db, [TagEngine(tags), BindingEngine(blueprints)])

    tag_key = (REG, ENTRY, "remove")
    binding_key = key("granite")
    before = (stack.capture("tag", tag_key), stack.capture("binding", binding_key))
    tags.assign(REG, ENTRY, "remove", True)
    blueprints.bind("StoneType", "granite", "base", "minecraft:granite")
    stack.push(Batch("both", [
        Edit("tag", tag_key, before[0], stack.capture("tag", tag_key)),
        Edit("binding", binding_key, before[1], stack.capture("binding", binding_key)),
    ]))

    stack.undo()

    assert tags.assignment(REG, ENTRY, "remove") is None
    assert value(blueprints, "granite") is None


# --- the tab wires it up ----------------------------------------------------------------

@pytest.fixture
def tab(blueprints, qapp):
    from packsmith.core.query.ast import AllSlots, Blueprint, Id, Query
    from packsmith.gui.blueprint_editor import BlueprintEditorTab
    return BlueprintEditorTab(
        Query(scope=Blueprint("StoneType"), select=[Id, AllSlots]),
        blueprints, packdump=FakeDump())


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_typing_into_the_grid_is_undoable(tab, blueprints):
    """What was actually broken: the grid wrote straight through, so Ctrl+Z did nothing."""
    tab.commit_cell(0, 0, "minecraft:granite")
    assert value(blueprints, "andesite") == "minecraft:granite"   # row 0 sorts first

    tab.undo()
    assert value(blueprints, "andesite") is None
    tab.redo()
    assert value(blueprints, "andesite") == "minecraft:granite"


def test_a_refused_bind_leaves_no_phantom_entry(tab, monkeypatch):
    """The stack must record what the store ACCEPTED. An entry for a write that never
    landed would, on undo, restore a prior state over a cell that never left it."""
    import packsmith.gui.blueprint_editor as editor
    warned = []
    monkeypatch.setattr(editor.QMessageBox, "warning",
                        lambda *args, **kw: warned.append(args[2]))   # or it blocks

    tab.commit_cell(0, 0, "not:a_real_block")

    assert warned, "the bind was expected to be refused"
    assert tab._undo.can_undo is False


def test_undo_moves_the_cursor_to_what_changed(tab, blueprints):
    """An undo you cannot see is indistinguishable from one that did nothing."""
    tab.commit_cell(1, 0, "minecraft:granite")
    tab._grid.setCurrentCell(0, 1)

    tab.undo()

    assert (tab._grid.currentRow(), tab._grid.currentColumn()) == (1, 0)


def test_the_shortcut_finds_a_blueprint_tab(tab):
    """`_active_history` duck-types the focused tab. A blueprint tab has no entry in
    `_tab_models`, which is why Ctrl+Z reached nothing before."""
    from packsmith.gui.main_window import MainWindow

    assert hasattr(tab, "undo") and hasattr(tab, "redo")
    source = __import__("inspect").getsource(MainWindow._active_history)
    assert "_tab_models" not in source or "hasattr" in source
