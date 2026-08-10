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


class BlueprintStaging:
    """The same contract as :class:`L2Staging`, for blueprint slot bindings (design 3.2.2).

    Ownership here is **per binding**, not per instance: "an instance created by an action
    can have its bindings individually overwritten by the user, transferring ownership
    slot-by-slot." So the staged key is ``(blueprint, instance, slot_path)`` — the same
    granularity as a tag cell, which is why the conflict machinery transfers unchanged.

    Creating an *instance* is staged too. An action that creates an instance and then binds
    into it has to be able to read it back mid-step, and neither may survive a failure.
    """

    def __init__(self, blueprint_store):
        self._store = blueprint_store
        # (blueprint, instance, slot_path) -> {"value", "owner", "owner_action_ref"} | _DELETE
        self._pending = {}
        # (blueprint, instance) -> created_by
        self._new_instances = {}
        self.inverse = []

    # --- staging writes (no DB contact) ---

    def create_instance(self, blueprint, instance, *, created_by=None):
        self._new_instances[(blueprint, instance)] = created_by

    def write(self, blueprint, instance, slot_path, value, *, owner, owner_action_ref=None):
        self._pending[(blueprint, instance, slot_path)] = {
            "value": value, "owner": owner, "owner_action_ref": owner_action_ref,
        }

    def delete(self, blueprint, instance, slot_path):
        self._pending[(blueprint, instance, slot_path)] = _DELETE

    # --- reads (read-your-writes) ---

    def instance_exists(self, blueprint, instance) -> bool:
        if (blueprint, instance) in self._new_instances:
            return True
        return any(i.name == instance for i in self._safe_instances(blueprint))

    def instances(self, blueprint) -> list:
        committed = [i.name for i in self._safe_instances(blueprint)]
        staged = [name for (bp, name) in self._new_instances if bp == blueprint]
        return sorted(set(committed) | set(staged))

    def read(self, blueprint, instance, slot_path):
        staged = self._pending.get((blueprint, instance, slot_path))
        if staged is _DELETE:
            return None
        if staged is not None:
            return staged["value"]
        if (blueprint, instance) in self._new_instances:
            return None                       # not committed yet, so nothing is bound
        return self._store.value_of(blueprint, instance, slot_path)

    def read_ownership(self, blueprint, instance, slot_path):
        staged = self._pending.get((blueprint, instance, slot_path))
        if staged is _DELETE:
            return None
        if staged is not None:
            return {"kind": staged["owner"], "action_ref": staged["owner_action_ref"]}
        if (blueprint, instance) in self._new_instances:
            return None
        binding = self._store.bindings(blueprint, instance).get(slot_path)
        if binding is None:
            return None
        return {"kind": binding.owner, "action_ref": binding.action_ref}

    # --- lifecycle ---

    @property
    def has_pending(self) -> bool:
        return bool(self._pending or self._new_instances)

    def commit(self):
        self.inverse = []
        for (blueprint, instance), created_by in self._new_instances.items():
            if not any(i.name == instance for i in self._safe_instances(blueprint)):
                self._store.create_instance(blueprint, instance, created_by=created_by)
                self.inverse.append({"kind": "instance", "key": [blueprint, instance],
                                     "existed": False})

        for key, staged in self._pending.items():
            blueprint, instance, slot_path = key
            prior = self._store.bindings(blueprint, instance).get(slot_path)
            self.inverse.append({
                "kind": "binding",
                "key": list(key),
                "existed": prior is not None,
                "value": prior.value if prior else None,
                "owner_kind": prior.owner if prior else None,
                "owner_ref": prior.action_ref if prior else None,
            })
            if staged is _DELETE:
                self._store.unbind(blueprint, instance, slot_path)
            else:
                self._store.bind(blueprint, instance, slot_path, staged["value"],
                                 owner=staged["owner"],
                                 action_ref=staged["owner_action_ref"])
        self._pending.clear()
        self._new_instances.clear()

    def discard(self):
        self._pending.clear()
        self._new_instances.clear()

    def _safe_instances(self, blueprint):
        try:
            return self._store.instances(blueprint)
        except Exception:
            return []
