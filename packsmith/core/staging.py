"""Per-step write staging for the action harness (design 1.1, 3.3, 7.3).

An action's writes don't hit real state as they happen — they accumulate in a
per-step buffer. Reads see the buffer first (read-your-writes), so an action
observes its own pending writes. On clean completion the buffer commits; on
failure it's discarded and no real state was ever touched. This gives every
action step atomic all-or-nothing semantics without any special ceremony from
the action author.

This module is the LAYER 2 (tag) side of staging — the closed-world engine where
PackSmith is the sole writer. File staging (open-world, store-by-path snapshots)
is a separate concern handled elsewhere.
"""

# Sentinel marking a staged "delete this assignment" (return the cell to pristine).
# Distinct from a staged value that happens to be None.
_DELETE = object()


class L2Staging:
    """Buffers tag-assignment writes for one action step over a TagStore.

    Nothing reaches the database until ``commit()``; ``discard()`` throws the
    buffer away and leaves the store untouched.
    """

    def __init__(self, tag_store):
        self._tags = tag_store
        # (registry_type, entry_id, tag_name) -> {"value", "owner", "owner_action_ref"} | _DELETE
        self._pending = {}
        # Populated at commit: each cell's PRIOR state, for rollback.
        self.inverse = []

    # --- staging writes (no DB contact) ---

    def write(self, registry_type, entry_id, tag_name, value, *, owner, owner_action_ref=None):
        self._pending[(registry_type, entry_id, tag_name)] = {
            "value": value, "owner": owner, "owner_action_ref": owner_action_ref,
        }

    def delete(self, registry_type, entry_id, tag_name):
        self._pending[(registry_type, entry_id, tag_name)] = _DELETE

    # --- reads (read-your-writes: staged shadows committed) ---

    def read(self, registry_type, entry_id, tag_name):
        staged = self._pending.get((registry_type, entry_id, tag_name))
        if staged is None:
            return self._tags.get_tag(registry_type, entry_id, tag_name)
        if staged is _DELETE:
            # Will be pristine after commit — reads as the tag's default, like any pristine cell.
            return self._tags.default_for(registry_type, tag_name)
        return staged["value"]

    def read_ownership(self, registry_type, entry_id, tag_name):
        staged = self._pending.get((registry_type, entry_id, tag_name))
        if staged is None:
            return self._tags.get_ownership(registry_type, entry_id, tag_name)
        if staged is _DELETE:
            return None  # pristine
        return {"kind": staged["owner"], "action_ref": staged["owner_action_ref"]}

    # --- lifecycle ---

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def commit(self):
        """Flush every staged write to the store, capturing each cell's PRIOR
        (existence, value, owner) into ``self.inverse`` first — the record the
        runner persists so this step can later be rolled back."""
        self.inverse = []
        for key, staged in self._pending.items():
            registry_type, entry_id, tag_name = key
            prior_owner = self._tags.get_ownership(registry_type, entry_id, tag_name)
            self.inverse.append({
                "key": list(key),
                "existed": prior_owner is not None,
                # get_tag returns the stored value when a row exists (default is only for pristine)
                "value": self._tags.get_tag(registry_type, entry_id, tag_name) if prior_owner else None,
                "owner_kind": prior_owner["kind"] if prior_owner else None,
                "owner_ref": prior_owner["action_ref"] if prior_owner else None,
            })
            if staged is _DELETE:
                self._tags.unassign(registry_type, entry_id, tag_name)
            else:
                self._tags.assign(
                    registry_type, entry_id, tag_name, staged["value"],
                    owner=staged["owner"], owner_action_ref=staged["owner_action_ref"],
                )
        self._pending.clear()

    def discard(self):
        """Drop the buffer. The store was never touched."""
        self._pending.clear()
