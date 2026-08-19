"""The registry table's edit commands, over the shared undo core (design 9.3.3).

This file used to *be* the undo system — a stack, a staleness story, and bulk-write
grouping, all tag-specific. §5.1 specified it that way and §9.3.3 superseded that: an undo
stack living inside one view implies a second one inside the next view, and with it a second
reversal and a second chance to omit the staleness check.

So the machinery moved to `packsmith.core.undo` and what remains here is the adapter: the
command objects the table already speaks in, translated into engine-agnostic `Edit`s. The
behaviour this file fought for is preserved, and now applies to blueprints too:

* `prior` is the cell's whole prior **state**, never just its value (§3.2.1's pristine vs.
  explicit-default distinction, and ownership).
* A refused move leaves both stacks untouched.
* A batch is one transaction, so a refusal cannot half-revert a selection.

What is genuinely new is that a move now refuses when something *else* has written the cell
since, rather than silently overwriting it.
"""
from dataclasses import dataclass, field

from packsmith.core.undo import (       # noqa: F401  (re-exported for existing importers)
    Batch, Edit, TagEngine, UndoBlocked, UndoStack, state_of,
)


@dataclass
class TagEditCommand:
    """A single tag edit that can be done and undone.

    ``prior`` is the cell's whole previous state — a ``tags.Assignment`` or None for
    pristine — not just its previous value. Two reasons, both learned the hard way:

    * A pristine cell on a defaulted tag reads back as the default through ``get_tag``, so
      an undo that re-assigns "the old value" *creates* a user-owned row where the user had
      never decided anything (design 3.2.1: "the default is NOT written to the database").
      Only existence distinguishes them, and only ``prior is None`` records it.
    * Ownership is part of assignment state. Undoing an edit to an **action-owned** cell has
      to give it back to that action, `action_ref` and all; restoring the value alone
      quietly launders it into a user decision and changes what conflict policy will do on
      the next run.
    """
    registry_type: str
    entry_id: str
    tag_name: str
    prior: object      # tags.Assignment, or None when the cell was pristine
    new_value: object  # None means "unset it"

    @property
    def key(self):
        return (self.registry_type, self.entry_id, self.tag_name)

    def edits(self):
        # A GUI edit is the user speaking, so it lands user-owned — taking the cell from an
        # action if one held it, which is the "loud transfer" 3.2.1 describes.
        after = None if self.new_value is None else (self.new_value, "user", None)
        return [Edit("tag", self.key, state_of(self.prior), after)]

    @property
    def label(self):
        return f"{self.entry_id} · {self.tag_name}"


@dataclass
class BatchEditCommand:
    """A group of edits applied and undone as one atomic action."""
    label: str
    edits_: list[TagEditCommand] = field(default_factory=list)

    def __init__(self, label, edits=()):
        self.label = label
        self.edits_ = list(edits)

    def edits(self):
        return [edit for command in self.edits_ for edit in command.edits()]


class EditStack:
    """Undo/redo for the registry table. A thin façade over `core.undo.UndoStack`.

    Kept as a distinct name because the table speaks in commands and the core speaks in
    batches, and because `execute` *applies* an edit where the core only records one — the
    table builds its commands before writing, the blueprint editor writes first and records
    after. Both are legitimate; the difference is one method, not one system.
    """

    def __init__(self, tag_store):
        self._tag_store = tag_store
        self._engine = TagEngine(tag_store)
        self._stack = UndoStack(tag_store._db, [self._engine])

    def execute(self, command):
        batch = Batch(command.label, command.edits())
        # Written through the same path a reversal takes, so "apply" and "undo" cannot
        # disagree about what a state means — and so a bulk edit gets the grouped writes.
        self._apply(batch, "after")
        self._stack.push(batch)

    def undo(self) -> bool:
        return self._stack.undo() is not None

    def redo(self) -> bool:
        return self._stack.redo() is not None

    def _apply(self, batch, target):
        try:
            with self._tag_store._db.transaction():
                self._engine.restore([(e.key, getattr(e, target)) for e in batch.edits])
        except (ValueError, KeyError) as e:
            raise UndoBlocked(str(e), batch) from e

    @property
    def can_undo(self) -> bool:
        return self._stack.can_undo

    @property
    def can_redo(self) -> bool:
        return self._stack.can_redo
