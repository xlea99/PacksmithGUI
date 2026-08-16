"""Per-step write staging for the action harness (design 1.1, 3.3, 7.3).

An action's writes don't hit real state as they happen — they accumulate in a
per-step buffer. Reads see the buffer first (read-your-writes), so an action
observes its own pending writes. On clean completion the buffer commits; on
failure it's discarded and no real state was ever touched. This gives every
action step atomic all-or-nothing semantics without any special ceremony from
the action author.

This module is the LAYER 2 (tag) side of staging — the closed-world engine where
Packsmith is the sole writer. File staging (open-world, store-by-path snapshots)
is a separate concern handled elsewhere.
"""

# Sentinel marking a staged "delete this assignment" (return the cell to pristine).
# Distinct from a staged value that happens to be None.
_DELETE = object()


# --- one record shape, for every engine and both producers ---------------------------------
#
# A dry run and a real run must describe themselves identically, or a report cannot serve
# both — and the whole proposition of the report is that the preview IS the run. So each
# buffer emits the same thing, and it emits it in the same place either way: `changes()` is
# read by `preview_step` before discarding, and by `run_action` just before committing.
#
#     {engine, <key fields>, kind, before: {...} | None, after: {...} | None}
#
# `before` and `after` are **None for absent** — pristine cell, unbound slot, file that did
# not exist — which is the distinction §3.2.1 turns on and the one a bare value cannot
# carry. A cell displaying its default is absent; a cell explicitly set to that same default
# is present. They are different facts and the report has to show them differently.

ADDED, CHANGED, REMOVED, CLAIMED, UNCHANGED, CREATED = (
    "added", "changed", "removed", "claimed", "unchanged", "created")


def classify(before, after) -> str:
    """What a staged write actually did to a cell.

    **`claimed` is the one worth having.** An action writing `true` over a user-owned
    `true` changes nothing you can see and everything about what happens next: §3.2.1 makes
    ownership a property of the assignment's existence, so that cell is now the action's and
    it will keep rewriting it. A classifier comparing values alone would call it unchanged,
    which is the report lying at exactly the point ownership matters.

    **`unchanged` is kept rather than dropped.** A re-run of a removal job saying "197
    already removed, 3 newly removed" is a better answer than one silently listing 3, and
    keeping every staged write in the output is also what lets a caller check that a run and
    its preview staged the *same set* of cells, not merely reached the same end state.
    """
    if before is None and after is None:
        return UNCHANGED                       # clearing a cell that was already pristine
    if before is None:
        return ADDED
    if after is None:
        return REMOVED
    # Values compare directly because the store round-trips them faithfully — see
    # `tests/test_fidelity.py`. Without that guarantee a committed `True` read back as `1`
    # would classify as `changed` against a staged `True`, and every re-run would report
    # spurious edits.
    if before["value"] != after["value"]:
        return CHANGED
    if (before["owner"], before["action_ref"]) != (after["owner"], after["action_ref"]):
        return CLAIMED
    return UNCHANGED


def _side(value, owner, action_ref) -> dict:
    return {"value": value, "owner": owner, "action_ref": action_ref}


class L2Staging:
    """Buffers tag-assignment writes for one action step over a TagStore.

    Nothing reaches the database until ``commit()``; ``discard()`` throws the
    buffer away and leaves the store untouched.
    """

    def __init__(self, tag_store):
        self._tags = tag_store
        # (registry_type, entry_id, tag_name) -> {"value", "owner", "owner_action_ref"} | _DELETE
        self._pending = {}
        # Which keys the CURRENT step has written, reset by `begin_step`. Recorded rather
        # than inferred: comparing a key's staged entry against the savepoint by identity
        # looks like it works and silently fails on `_DELETE`, which is one shared
        # sentinel — so a second step deleting a cell an earlier step already deleted was
        # reported by a real run and dropped by a dry one.
        self._touched = set()
        # Populated at commit: each cell's PRIOR state, for rollback.
        self.inverse = []

    # --- staging writes (no DB contact) ---

    def write(self, registry_type, entry_id, tag_name, value, *, owner, owner_action_ref=None):
        key = (registry_type, entry_id, tag_name)
        self._pending[key] = {
            "value": value, "owner": owner, "owner_action_ref": owner_action_ref,
        }
        self._touched.add(key)

    def delete(self, registry_type, entry_id, tag_name):
        key = (registry_type, entry_id, tag_name)
        self._pending[key] = _DELETE
        self._touched.add(key)

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

    def query(self, registry_type, tag_name, value) -> list:
        """Entry ids where ``tag_name == value``, **with this buffer folded in**.

        The one read that used to go straight to the store, which made it the only place
        an action could not see its own writes: write `remove = true`, then query for
        `remove == true`, and the entry you just wrote was missing. Every other read here
        is staged-first, and `blueprints.gaps` explicitly advertises the opposite
        behaviour — so this was an inconsistency rather than a policy.

        Matching is done the way the STORE does it — `str()` on both sides — because that
        is what the SQL compares once these writes commit. Reproducing the store's answer
        is the whole job: a dry run and a real run must agree, and they only can if the
        overlay computes what the store would have said.

        A staged **delete** is dropped from the result rather than re-tested against the
        tag's default. After a commit the row is gone, and the store selects FROM
        assignments — so a pristine cell cannot match, whatever its default displays as.
        """
        matched = set(self._tags.query(registry_type, **{tag_name: value}))
        wanted = str(value)
        for (staged_registry, entry_id, staged_tag), staged in self._pending.items():
            if staged_registry != registry_type or staged_tag != tag_name:
                continue
            if staged is not _DELETE and str(staged["value"]) == wanted:
                matched.add(entry_id)
            else:
                matched.discard(entry_id)
        return sorted(matched)

    # --- lifecycle ---

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    # --- step boundaries ---
    #
    # A buffer shared across a whole job needs "undo just this step" rather than `discard`,
    # which would take the earlier steps with it. Entries are always REPLACED, never mutated
    # in place, so a shallow copy is a complete snapshot of what earlier steps staged.

    def begin_step(self):
        """Start a step: forget what the last one touched, snapshot what it left behind.

        Called exactly once per step, by the runner. Resetting the touched set here rather
        than exposing a separate call is deliberate — two things that must happen together
        should not be two things a caller can forget to pair.
        """
        self._touched = set()
        return dict(self._pending)

    def rollback(self, savepoint):
        self._pending = dict(savepoint)
        self._touched = set()

    def changes(self, since=None) -> list:
        """What this buffer would DO, classified — design 3.3's dry run, read.

        ``since`` is the savepoint from :meth:`begin_step`. Given one, only the keys this
        step touched are reported, and a cell an **earlier** step staged is this step's
        ``before`` — exactly as an earlier step's committed writes would be in a real run.
        That is the whole reason a multi-step dry run can be 1:1 with a real one rather than
        approximately so.

        Sorted and free of internals so two runs of the same action can be compared
        directly; that comparison is the whole point (see `runner.preview_step`).
        """
        described = []
        keys = self._touched if since is not None else self._pending.keys()
        for key in list(keys):
            staged = self._pending.get(key)
            if staged is None:
                continue                       # rolled back out from under us
            registry_type, entry_id, tag_name = key
            earlier = since.get(key) if since is not None else None
            if earlier is not None:
                before = None if earlier is _DELETE else _side(
                    earlier["value"], earlier["owner"], earlier["owner_action_ref"])
            else:
                prior = self._tags.assignment(registry_type, entry_id, tag_name)
                before = None if prior is None else _side(
                    prior.value, prior.owner, prior.action_ref)
            after = None if staged is _DELETE else _side(
                staged["value"], staged["owner"], staged["owner_action_ref"])
            described.append({
                "engine": "tag", "registry_type": registry_type, "entry_id": entry_id,
                "tag": tag_name, "kind": classify(before, after),
                "before": before, "after": after,
            })
        return sorted(described, key=lambda d: (d["registry_type"], d["entry_id"], d["tag"]))

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
        # See `L2Staging._touched` — same reason, one set per collection.
        self._touched = set()
        self._touched_instances = set()
        self.inverse = []

    # --- staging writes (no DB contact) ---

    def create_instance(self, blueprint, instance, *, created_by=None):
        self._new_instances[(blueprint, instance)] = created_by
        self._touched_instances.add((blueprint, instance))

    def write(self, blueprint, instance, slot_path, value, *, owner, owner_action_ref=None):
        key = (blueprint, instance, slot_path)
        self._pending[key] = {
            "value": value, "owner": owner, "owner_action_ref": owner_action_ref,
        }
        self._touched.add(key)

    def delete(self, blueprint, instance, slot_path):
        key = (blueprint, instance, slot_path)
        self._pending[key] = _DELETE
        self._touched.add(key)

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

    def begin_step(self):
        self._touched = set()
        self._touched_instances = set()
        return (dict(self._pending), dict(self._new_instances))

    def rollback(self, savepoint):
        pending, instances = savepoint
        self._pending = dict(pending)
        self._new_instances = dict(instances)
        self._touched = set()
        self._touched_instances = set()

    def changes(self, since=None) -> list:
        """What this buffer would DO, classified. Instance creations are included: they are
        staged too, so a dry run that omitted them would under-report — and creating an
        instance is exactly the change §3.2.2 cares most about, since it is a new row of
        gaps to fill.

        ``since`` behaves as it does on :class:`L2Staging` — see there."""
        since_pending, since_instances = since if since is not None else (None, None)
        described = []
        instance_keys = (self._touched_instances if since is not None
                         else self._new_instances.keys())
        for key in list(instance_keys):
            if key not in self._new_instances:
                continue                       # rolled back out from under us
            blueprint, instance = key
            created_by = self._new_instances[key]
            # `commit` skips a create for an instance that already exists, so this has to
            # as well, or a re-run reports inventing something that was already there.
            exists = any(i.name == instance for i in self._safe_instances(blueprint))
            described.append({
                "engine": "blueprint", "blueprint": blueprint, "instance": instance,
                "slot": None, "kind": UNCHANGED if exists else CREATED,
                "before": None,
                "after": None if exists else _side(None, "action", created_by),
            })
        slot_keys = self._touched if since is not None else self._pending.keys()
        for key in list(slot_keys):
            staged = self._pending.get(key)
            if staged is None:
                continue
            blueprint, instance, slot_path = key
            earlier = since_pending.get(key) if since_pending is not None else None
            if earlier is not None:
                before = None if earlier is _DELETE else _side(
                    earlier["value"], earlier["owner"], earlier["owner_action_ref"])
            else:
                prior = None
                if any(i.name == instance for i in self._safe_instances(blueprint)):
                    prior = self._store.bindings(blueprint, instance).get(slot_path)
                before = None if prior is None else _side(
                    prior.value, prior.owner, prior.action_ref)
            after = None if staged is _DELETE else _side(
                staged["value"], staged["owner"], staged["owner_action_ref"])
            described.append({
                "engine": "blueprint", "blueprint": blueprint, "instance": instance,
                "slot": slot_path, "kind": classify(before, after),
                "before": before, "after": after,
            })
        return sorted(described,
                      key=lambda d: (d["blueprint"], d["instance"], d["slot"] or ""))

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
