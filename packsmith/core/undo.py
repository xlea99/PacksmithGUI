"""Cell-level undo across every L2 engine (design 9.3.3).

**One stack, not one per view.** The registry table had an `EditStack` and the blueprint
editor had nothing; the obvious fix — give the blueprint editor its own — would have meant
two implementations of the same reversal and two chances to get staleness wrong. §5.1's
original per-view design was superseded for exactly that reason.

That it *can* be one system is not a happy accident. L2 is a closed world of owned cells
(§3.2), and both engines already store the same thing about a cell:

    tag assignment   ->  (value, owner, action_ref)  or None when pristine
    blueprint binding ->  (value, owner, action_ref)  or None when unbound

So an `Edit` is engine-agnostic: a key, the state before, the state after. An `Engine` is
the small adapter that knows how to read and write one kind of key. Adding a third L2
engine later means writing an adapter, not another stack.

**Absence is a state, not a missing value.** `None` means the row does not exist, and that
is different from a row holding the type's default. §3.2.1 is explicit that a default is
never written to the database, so restoring "the old value" of a pristine cell would invent
a user-owned decision where the user had never made one. Only `prior is None` records it.

**Ownership travels with the value.** Undoing your edit over an action-owned cell gives it
back to that action, `action_ref` and all. Restoring the value alone launders an action's
data into a user decision and changes what the next run's conflict policy does.

**Staleness is what makes this safe.** See `_verify`.
"""
from dataclasses import dataclass, field


class UndoBlocked(Exception):
    """A move could not be applied, and the stack was left exactly as it was.

    Not a bug to swallow — a real refusal with a real reason, which the user can often fix
    before trying again. Two causes: the world moved under the stack (a tag undefined, an
    enum value dropped, a slot retyped), or something else has since written the cell.
    """

    def __init__(self, reason, batch=None):
        super().__init__(reason)
        self.reason = reason
        self.batch = batch
        self.command = batch          # the name the registry table has always used


@dataclass(frozen=True)
class Edit:
    """One cell, before and after. `after` exists solely for the staleness check."""
    engine: str
    key: tuple
    prior: tuple | None
    after: tuple | None


@dataclass
class Batch:
    """One gesture. §9.3.3: a batch is whatever one transaction wrote, so filling forty
    cells is one Ctrl+Z rather than forty."""
    label: str
    edits: list[Edit] = field(default_factory=list)

    def __bool__(self):
        return bool(self.edits)


def state_of(row) -> tuple | None:
    """An Assignment or Binding as the tuple both engines share, or None for absence."""
    if row is None:
        return None
    return (row.value, row.owner, row.action_ref)


class Engine:
    """How one kind of L2 cell is read and written. Subclasses supply three methods."""

    name = None

    def state(self, key) -> tuple | None:
        raise NotImplementedError

    def restore(self, items):
        """`items` is [(key, state), ...]. Bulk so an engine can group the writes."""
        raise NotImplementedError

    def describe(self, key) -> str:
        return ":".join(str(part) for part in key)


class TagEngine(Engine):
    name = "tag"

    def __init__(self, tag_store):
        self._store = tag_store

    def state(self, key):
        registry_type, entry_id, tag_name = key
        return state_of(self._store.assignment(registry_type, entry_id, tag_name))

    def restore(self, items):
        """Grouped, because a selection edit can be thousands of cells and `assign` takes a
        list of ids. Grouped by OWNER as well as value: a selection can span cells the user
        owned and cells an action owned, and collapsing those would hand the whole batch to
        whichever owner happened to sort first.
        """
        assigns, unassigns = {}, {}
        for (registry_type, entry_id, tag_name), state in items:
            if state is None:
                unassigns.setdefault((registry_type, tag_name), []).append(entry_id)
            else:
                value, owner, action_ref = state
                assigns.setdefault((registry_type, tag_name, value, owner, action_ref),
                                   []).append(entry_id)
        for (registry_type, tag_name, value, owner, ref), ids in assigns.items():
            self._store.assign(registry_type, ids, tag_name, value,
                               owner=owner, owner_action_ref=ref)
        for (registry_type, tag_name), ids in unassigns.items():
            self._store.unassign(registry_type, ids, tag_name)

    def describe(self, key):
        _registry_type, entry_id, tag_name = key
        return f"{entry_id} · {tag_name}"


class BindingEngine(Engine):
    name = "binding"

    def __init__(self, blueprint_store):
        self._store = blueprint_store

    def state(self, key):
        blueprint, instance, slot_path = key
        return state_of(self._store.binding(blueprint, instance, slot_path))

    def restore(self, items):
        for (blueprint, instance, slot_path), state in items:
            if state is None:
                self._store.unbind(blueprint, instance, slot_path)
            else:
                value, owner, action_ref = state
                self._store.bind(blueprint, instance, slot_path, value,
                                 owner=owner, action_ref=action_ref)

    def describe(self, key):
        _blueprint, instance, slot_path = key
        return f"{instance}.{slot_path}"


class UndoStack:
    """Undo/redo over recorded batches.

    Two invariants, both of which cost real data before they were invariants:

    * **A batch is never dropped by a failed move.** The stacks are mutated only after the
      write is accepted. Popping first meant a refusal deleted the batch from undo without
      landing it in redo — un-undoable *and* un-redoable.
    * **A move is all-or-nothing.** One transaction, so "it didn't work" means the store is
      exactly as it was rather than half-reverted with no record of which half.
    """

    def __init__(self, db, engines):
        self._db = db
        self._engines = {engine.name: engine for engine in engines}
        self._undo: list[Batch] = []
        self._redo: list[Batch] = []

    # --- recording ---------------------------------------------------------

    def push(self, batch: Batch):
        """Remember an edit the caller has already made. A no-op for an empty batch, so a
        view can record unconditionally without checking whether anything changed."""
        if not batch:
            return
        self._undo.append(batch)
        self._redo.clear()

    def capture(self, engine_name, key):
        """The state of a cell right now — what a view reads before editing it."""
        return self._engines[engine_name].state(key)

    def clear(self):
        self._undo.clear()
        self._redo.clear()

    # --- moving ------------------------------------------------------------

    def undo(self) -> Batch | None:
        if not self._undo:
            return None
        batch = self._undo[-1]
        self._move(batch, "after", "prior")
        self._undo.pop()
        self._redo.append(batch)
        return batch

    def redo(self) -> Batch | None:
        if not self._redo:
            return None
        batch = self._redo[-1]
        self._move(batch, "prior", "after")
        self._redo.pop()
        self._undo.append(batch)
        return batch

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def peek(self) -> Batch | None:
        return self._undo[-1] if self._undo else None

    # --- the part that makes it safe ---------------------------------------

    def _verify(self, batch, expected: str):
        """Refuse if a cell now holds a value this batch did not put there.

        An entry claims a cell was X before this edit made it Y. If an action run, or
        another tab, has since made it Z, reversing would silently clobber Z — and whoever
        lost that write would have no way to know, because undo reported success. This is
        §6.1's file hard-block applied to L2: refuse and name the cell, rather than
        overwrite and say nothing.

        **An empty cell is not a conflict.** The rule is "never overwrite a value it did
        not write", not "the cell must be untouched" — absence holds no decision to
        destroy. This matters for a real workflow: undefining a tag deletes its
        assignments, and redefining it leaves the cells pristine; §5.1's stack has always
        let you fix that cause and retry the undo, and treating pristine as a conflict
        would take that away to protect nothing.

        (Design 9.3.3 stated the strict form. This is the refinement that survived contact
        with the cases — the destructive scenario is caught identically either way.)
        """
        for edit in batch.edits:
            engine = self._engines[edit.engine]
            current = engine.state(edit.key)
            if current is not None and current != getattr(edit, expected):
                raise UndoBlocked(
                    f"{engine.describe(edit.key)} has changed since then — something else "
                    f"wrote it. Reversing would overwrite that.", batch)

    def _move(self, batch, expected: str, target: str):
        self._verify(batch, expected)
        by_engine = {}
        for edit in batch.edits:
            by_engine.setdefault(edit.engine, []).append(
                (edit.key, getattr(edit, target)))
        try:
            with self._db.transaction():
                for engine_name, items in by_engine.items():
                    self._engines[engine_name].restore(items)
        except UndoBlocked:
            raise
        except (ValueError, KeyError) as e:
            # What the stores raise when the world no longer permits the write: a tag was
            # undefined, an enum value dropped, a slot retyped under a binding.
            raise UndoBlocked(str(e), batch) from e
