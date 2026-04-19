from dataclasses import dataclass, field


@dataclass
class TagEditCommand:
    """A single tag edit that can be done and undone."""
    registry_type: str
    entry_id: str
    tag_name: str
    old_value: object  # None means "was unset"
    new_value: object  # None means "unset it"

    def apply(self, tag_store):
        if self.new_value is None:
            tag_store.unassign(self.registry_type, self.entry_id, self.tag_name)
        else:
            tag_store.assign(self.registry_type, self.entry_id, self.tag_name, self.new_value)

    def undo(self, tag_store):
        if self.old_value is None:
            tag_store.unassign(self.registry_type, self.entry_id, self.tag_name)
        else:
            tag_store.assign(self.registry_type, self.entry_id, self.tag_name, self.old_value)


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
        """Group edits by (registry_type, tag_name, value) and issue bulk assign/unassign calls."""
        assigns = {}   # (registry_type, tag_name, value) -> [entry_id, ...]
        unassigns = {}  # (registry_type, tag_name) -> [entry_id, ...]

        for edit in self.edits:
            value = edit.new_value if forward else edit.old_value
            if value is None:
                key = (edit.registry_type, edit.tag_name)
                unassigns.setdefault(key, []).append(edit.entry_id)
            else:
                key = (edit.registry_type, edit.tag_name, value)
                assigns.setdefault(key, []).append(edit.entry_id)

        for (reg, tag, val), ids in assigns.items():
            tag_store.assign(reg, ids, tag, val)
        for (reg, tag), ids in unassigns.items():
            tag_store.unassign(reg, ids, tag)


class EditStack:
    """Undo/redo stack for tag edits."""

    def __init__(self, tag_store):
        self._tag_store = tag_store
        self._undo_stack: list[TagEditCommand | BatchEditCommand] = []
        self._redo_stack: list[TagEditCommand | BatchEditCommand] = []

    def execute(self, command: TagEditCommand | BatchEditCommand):
        command.apply(self._tag_store)
        self._undo_stack.append(command)
        self._redo_stack.clear()

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        command = self._undo_stack.pop()
        command.undo(self._tag_store)
        self._redo_stack.append(command)
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        command = self._redo_stack.pop()
        command.apply(self._tag_store)
        self._undo_stack.append(command)
        return True

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0
