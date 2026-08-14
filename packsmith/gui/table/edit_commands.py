from dataclasses import dataclass, field


@dataclass
class TagEditCommand:
    """A single tag edit that can be done and undone.

    ``prior`` is the cell's whole previous **state** — a ``tags.Assignment`` or None for
    pristine — not just its previous value. Two reasons, both learned the hard way:

    * A pristine cell on a defaulted tag reads back as the default through ``get_tag``, so
      an undo that re-assigns "the old value" *creates* a user-owned row where the user had
      never decided anything (design 3.2.1: "the default is NOT written to the database").
      Only existence distinguishes them, and only ``prior is None`` records it.
    * Ownership is part of assignment state. Undoing an edit to an **action-owned** cell has
      to give it back to that action, `action_ref` and all; restoring the value alone
      quietly launders it into a user decision and changes what conflict policy will do on
      the next run.

    This mirrors what ``L2Staging.inverse`` already captures for action rollback — the
    runner had it right; the edit stack didn't.
    """
    registry_type: str
    entry_id: str
    tag_name: str
    prior: object      # tags.Assignment, or None when the cell was pristine
    new_value: object  # None means "unset it"

    def apply(self, tag_store):
        if self.new_value is None:
            tag_store.unassign(self.registry_type, self.entry_id, self.tag_name)
        else:
            # A GUI edit is the user speaking, so it lands user-owned — taking the cell from
            # an action if one held it, which is the "loud transfer" 3.2.1 describes.
            tag_store.assign(self.registry_type, self.entry_id, self.tag_name,
                             self.new_value, owner="user")

    def undo(self, tag_store):
        if self.prior is None:
            tag_store.unassign(self.registry_type, self.entry_id, self.tag_name)
        else:
            tag_store.assign(self.registry_type, self.entry_id, self.tag_name,
                             self.prior.value, owner=self.prior.owner,
                             owner_action_ref=self.prior.action_ref)


@dataclass
class BatchEditCommand:
    """A group of edits applied and undone as one atomic action."""
    label: str
    edits: list[TagEditCommand] = field(default_factory=list)

    def apply(self, tag_store):
        self._apply_bulk(tag_store, forward=True)

    def undo(self, tag_store):
        self._apply_bulk(tag_store, forward=False)

    def _apply_bulk(self, tag_store, forward: bool):
        """Bulk assign/unassign, grouped by everything that has to match.

        Undo groups by **owner too**, not just value: a selection can span cells the user
        owned and cells an action owned, and collapsing those into one `assign` would hand
        the whole batch to whichever owner happened to sort first.
        """
        assigns = {}   # (registry_type, tag_name, value, owner, action_ref) -> [entry_id]
        unassigns = {}  # (registry_type, tag_name) -> [entry_id, ...]

        for edit in self.edits:
            if forward:
                value, owner, action_ref = edit.new_value, "user", None
            elif edit.prior is None:
                unassigns.setdefault((edit.registry_type, edit.tag_name), []).append(
                    edit.entry_id)
                continue
            else:
                value = edit.prior.value
                owner, action_ref = edit.prior.owner, edit.prior.action_ref

            if value is None:
                unassigns.setdefault((edit.registry_type, edit.tag_name), []).append(
                    edit.entry_id)
            else:
                key = (edit.registry_type, edit.tag_name, value, owner, action_ref)
                assigns.setdefault(key, []).append(edit.entry_id)

        for (reg, tag, val, owner, action_ref), ids in assigns.items():
            tag_store.assign(reg, ids, tag, val, owner=owner,
                             owner_action_ref=action_ref)
        for (reg, tag), ids in unassigns.items():
            tag_store.unassign(reg, ids, tag)


class UndoBlocked(Exception):
    """An undo (or redo) could not be applied, and the stack was left untouched.

    The world moves under a stack: a tag gets undefined, an enum value is dropped from a
    definition, and suddenly the state an old edit wants to restore is one the store will
    refuse. That is not a bug to swallow — it is a real refusal with a real reason, and the
    user can often fix the cause and try again. Carries `reason` for saying so out loud.
    """

    def __init__(self, reason, command):
        super().__init__(reason)
        self.reason = reason
        self.command = command


class EditStack:
    """Undo/redo stack for tag edits.

    Two invariants, both of which cost real data before they were invariants:

    * **A command is never dropped by a failed move.** The stack is mutated only after the
      store accepts the change. Popping first meant a refusal deleted the command from the
      undo stack without ever landing it in redo — the edit became both un-undoable and
      un-redoable, and the exception went on to escape into Qt's shortcut handler.
    * **A move is all-or-nothing.** A batch is several writes; a refusal partway through
      used to leave half the selection reverted and half not, with no record of which. One
      transaction makes "it didn't work" mean the store is exactly as it was.
    """

    def __init__(self, tag_store):
        self._tag_store = tag_store
        self._undo_stack: list[TagEditCommand | BatchEditCommand] = []
        self._redo_stack: list[TagEditCommand | BatchEditCommand] = []

    def execute(self, command: TagEditCommand | BatchEditCommand):
        self._attempt(command.apply, command)
        self._undo_stack.append(command)
        self._redo_stack.clear()

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        command = self._undo_stack[-1]
        self._attempt(command.undo, command)
        self._undo_stack.pop()
        self._redo_stack.append(command)
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        command = self._redo_stack[-1]
        self._attempt(command.apply, command)
        self._redo_stack.pop()
        self._undo_stack.append(command)
        return True

    def _attempt(self, move, command):
        """Run one move inside a transaction, or leave the store and the stack alone."""
        try:
            with self._tag_store._db.transaction():
                move(self._tag_store)
        except UndoBlocked:
            raise
        except (ValueError, KeyError) as e:
            # What the tag store raises when the world no longer permits the write: the
            # tag was undefined, or the value is no longer one the definition allows.
            raise UndoBlocked(str(e), command) from e

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0
