"""The ``pack`` object — the capability surface an action's code calls (design 7.x).

Action bodies are Starlark (design 7.6), which cannot reach anything this object does
not expose — the old honest-gentleman rule is now enforced by the language rather than
trusted. Every write routes through per-step staging (design 1.1) and
is stamped with the *calling action's own identity*, so ownership is correct by
construction — an action cannot forge a different owner.

One ``Pack`` is constructed per step invocation and injected into the action's entry
point as its single argument.
"""
import json

from packsmith.core.bindings import policy_key
from packsmith.core.capabilities import CapabilityError

try:
    import json5 as _json5

    def _loads_json5(text):
        return _json5.loads(text)
except ImportError:  # pragma: no cover - json5 is a declared dependency
    import re

    def _loads_json5(text):
        # Fallback if json5 isn't installed: strip /* */ and // comments, then parse
        # as JSON. Quick-and-dirty; json5 is the real parser when present.
        no_block = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        no_line = re.sub(r"(^|\s)//[^\n]*", r"\1", no_block)
        return json.loads(no_line)


def _dumps_json(obj):
    return json.dumps(obj, indent=2)


class ActionFailure(Exception):
    """Raised by ``pack.fail(reason)``. The runner catches it and discards the
    step's staged writes, so a failed step leaves no real state touched."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Registry:
    """``pack.registry`` — read-only Layer 1 (game data) access."""

    def __init__(self, packdump):
        self._dump = packdump
        self._members = {}      # registry_type -> set of ids, built on first `has`

    def entries(self, registry_type):
        return list(self._dump.registry.get(registry_type, {}).get("values", []))

    def has(self, registry_type, entry_id):
        """Membership, against a set built once per registry.

        This was `in` against a **list**, which is a linear scan: 18,638 comparisons per
        call on a real pack's item registry. Actions ask it in loops — the blueprint
        suggester tests one candidate id per gap — so the natural way to write an action
        was quietly quadratic.

        Built lazily and per registry, because an action typically touches one or two out
        of ~135 and paying for the rest would just move the cost.
        """
        members = self._members.get(registry_type)
        if members is None:
            members = set(self._dump.registry.get(registry_type, {}).get("values", []))
            self._members[registry_type] = members
        return entry_id in members

    def attribute(self, registry_type, entry_id, name):
        return self._dump.attribute(registry_type, entry_id, name)


def _refuse_foreign_delete(current, action_ref, *, cell, taken=()):
    """Deletes are not writes, and conflict policy does not cover them (design 3.2.1).

    "**Delete assignment** — fully remove an assignment row, returning the (entry, tag)
    pair to the pristine state. **User-only; actions cannot fully delete an assignment,
    only write over it** (which transfers ownership rather than clearing it)."

    So this is deliberately *stricter* than ``_may_write``: a declared `overwrite` policy
    licenses **taking** a cell, never **erasing** it. Pristine — "nobody has ever had an
    opinion about this" — is a state only the user can produce, and an action that could
    manufacture it could quietly destroy the record of a decision rather than supersede it.

    An action clearing something **it owns itself** is allowed, and is the one case that
    isn't covered by the rule above: it's the same "re-asserting is not a conflict"
    reasoning `_may_write` uses, applied to an action retracting its own output.
    """
    if current is None:
        return                                            # already pristine; nothing to do
    if current["kind"] == "action" and current["action_ref"] == action_ref:
        if cell in taken:
            # Taken from someone else EARLIER IN THIS STEP. Each call was legal on its own
            # — the policy allowed the write, and an action may retract its own work — but
            # together they launder "user-owned" into "pristine", which 3.2.1 reserves for
            # the user. The rule is about the step's net effect, so it needs the memory.
            raise ActionFailure(
                f"{cell} was taken from its previous owner during this step; clearing it "
                f"now would erase their record rather than supersede it. Write a value "
                f"instead, or leave it alone.")
        return                                            # genuinely ours to retract
    who = "the user" if current["kind"] == "user" else f"'{current['action_ref']}'"
    raise ActionFailure(
        f"{cell} is owned by {who}, and an action may not delete what it does not own — "
        f"only write over it. Write a new value instead, or leave it alone.")


class _Tags:
    """``pack.tags`` — Layer 2 tag access.

    Reads (``get``/``ownership``) are staging-aware — an action sees its own pending
    writes. Writes (``write``/``clear``) are staged and stamped with the calling
    action as owner. ``query`` reads the committed store; it does not yet reflect
    this step's own staged writes (fine for the common read-then-write pattern).

    Writing a cell **someone else already owns** is a conflict, resolved by the action's
    declared per-mapping conflict policy (design 3.3). Re-writing a cell this same action
    already owns is not a conflict — an action is the authoritative producer of its own
    state and is expected to re-assert it on every run.
    """

    def __init__(self, staging, tag_store, action_ref, conflict_policies=None, log=None):
        self._staging = staging
        self._store = tag_store
        self._action_ref = action_ref
        self._policies = dict(conflict_policies or {})
        self._log = log or (lambda level, message: None)
        self._taken = set()          # cells this step took from someone else

    def query(self, registry_type, tag_name, value):
        """Entry IDs whose ``tag_name`` equals ``value``, including this step's own writes.

        Staged-first like every other read on `pack`. It used to consult only the committed
        store, which meant an action could write a cell and then fail to find it a line
        later — the one place read-your-writes did not hold.
        """
        return self._staging.query(registry_type, tag_name, value)

    def get(self, registry_type, entry_id, tag_name):
        return self._staging.read(registry_type, entry_id, tag_name)

    def ownership(self, registry_type, entry_id, tag_name):
        return self._staging.read_ownership(registry_type, entry_id, tag_name)

    def write(self, registry_type, entry_id, tag_name, value):
        current = self._staging.read_ownership(registry_type, entry_id, tag_name)
        if not self._may_write(registry_type, entry_id, tag_name):
            return
        if current is not None and not (
                current["kind"] == "action"
                and current["action_ref"] == self._action_ref):
            self._taken.add(f"{tag_name} on {entry_id}")
        self._staging.write(registry_type, entry_id, tag_name, value,
                            owner="action", owner_action_ref=self._action_ref)

    def _may_write(self, registry_type, entry_id, tag_name) -> bool:
        """Resolve a write against the current owner. Returns False for `skip`; raises
        ActionFailure for `fail` (which discards the whole step)."""
        current = self._staging.read_ownership(registry_type, entry_id, tag_name)
        if current is None:
            return True                                   # pristine — claiming it is free
        if current["kind"] == "action" and current["action_ref"] == self._action_ref:
            return True                                   # already ours; re-asserting is not a conflict

        who = "the user" if current["kind"] == "user" else f"'{current['action_ref']}'"
        cell = f"{tag_name} on {entry_id}"
        policy = self._policies.get(policy_key("tag", registry_type, tag_name))

        if policy == "overwrite":
            self._log("info", f"took {cell} from {who} (conflict policy: overwrite)")
            return True
        if policy == "skip":
            self._log("info", f"left {cell} alone — owned by {who} (conflict policy: skip)")
            return False
        if policy == "ask":
            raise ActionFailure(
                f"{cell} is owned by {who} and this mapping's conflict policy is 'ask', "
                f"which Packsmith does not support yet — choose overwrite, skip, or fail.")
        if policy == "fail":
            raise ActionFailure(
                f"{cell} is owned by {who} and this mapping's conflict policy is 'fail'.")
        # No declared policy: the action is writing outside its declared contract, onto
        # data it doesn't own. Design 3.3 has no default for a reason — refuse, loudly.
        raise ActionFailure(
            f"{cell} is owned by {who}, and this action declares no conflict policy for "
            f"'{tag_name}'. Declare one on the mapping that binds it.")

    def clear(self, registry_type, entry_id, tag_name):
        _refuse_foreign_delete(
            self._staging.read_ownership(registry_type, entry_id, tag_name),
            self._action_ref, cell=f"{tag_name} on {entry_id}", taken=self._taken)
        self._staging.delete(registry_type, entry_id, tag_name)


class _Blueprints:
    """``pack.blueprints`` — Layer 2 blueprint access (design 3.2.2).

    **Schemas are absent from this object on purpose.** 3.2.2: "Actions cannot create,
    modify, rename, retype, or delete schemas." That isn't enforced with a guard — the
    methods simply aren't here, and Starlark can reach nothing the host doesn't hand it.
    The shape of a blueprint is the user's; only its *data* is shared.

    **Instances and bindings are hybrid.** Ownership is per binding, so writing a slot
    somebody else owns is resolved by the same declared conflict policy that governs tag
    writes (3.2.2: "identical to tag assignment conflicts") — one rule, one implementation,
    two primitives.

    **A blueprint with orphaned instances refuses to be touched at all.** 3.2.2 is
    unambiguous that the instance "does not silently degrade": until the user resolves what
    a destructive schema change meant, an action reading it would be reasoning about a
    shape that no longer describes the data.
    """

    def __init__(self, staging, blueprint_store, action_ref, conflict_policies=None,
                 log=None):
        self._staging = staging
        self._store = blueprint_store
        self._action_ref = action_ref
        self._policies = dict(conflict_policies or {})
        self._log = log or (lambda level, message: None)
        self._taken = set()          # bindings this step took from someone else

    # --- reads -------------------------------------------------------------

    def instances(self, blueprint):
        self._require_healthy(blueprint)
        return self._staging.instances(blueprint)

    def names(self):
        """Every blueprint the user has defined."""
        return self._store.names()

    def slots(self, blueprint):
        """Slot paths an instance of this blueprint can hold — the shape, read-only."""
        self._require_healthy(blueprint)
        return [s.path for s in self._store.value_slots(blueprint)]

    def slot(self, blueprint, slot_path):
        """What a slot *accepts*, as a dict the prelude turns into a struct.

        Without this an action can iterate slots but not reason about them, which rules
        out the whole interesting class — anything that searches a registry to fill a gap
        has to know which registry the gap wants.
        """
        self._require_healthy(blueprint)
        found = self._store.slot(blueprint, slot_path)
        return {
            "path": found.path, "name": found.name, "kind": found.kind,
            "type": found.type, "registry_type": found.registry_type,
            "blueprint": found.ref_blueprint, "values": list(found.enum_values),
            "group": found.path.rsplit(".", 1)[0] if "." in found.path else "",
        }

    def get(self, blueprint, instance, slot_path):
        self._require_healthy(blueprint)
        return self._staging.read(blueprint, instance, slot_path)

    def bindings(self, blueprint, instance):
        """Every bound slot at once, ``{path: value}``. One call instead of one per slot,
        and it reads the way an author thinks about an instance."""
        self._require_healthy(blueprint)
        return {path: self._staging.read(blueprint, instance, path)
                for path in self.slots(blueprint)
                if self._staging.read(blueprint, instance, path) is not None}

    def ownership(self, blueprint, instance, slot_path):
        self._require_healthy(blueprint)
        return self._staging.read_ownership(blueprint, instance, slot_path)

    def gaps(self, blueprint, instance):
        """Slot paths with nothing bound — the reason blueprints exist. Staging-aware, so
        an action sees the gaps it has already filled this step close behind it."""
        self._require_healthy(blueprint)
        return [path for path in self.slots(blueprint)
                if self._staging.read(blueprint, instance, path) is None]

    def has(self, blueprint, instance) -> bool:
        self._require_healthy(blueprint)
        return self._staging.instance_exists(blueprint, instance)

    # --- writes ------------------------------------------------------------

    def create(self, blueprint, instance):
        """Create an instance, attributed to the calling action."""
        self._require_healthy(blueprint)
        if self._staging.instance_exists(blueprint, instance):
            return
        self._staging.create_instance(blueprint, instance, created_by=self._action_ref)

    def bind(self, blueprint, instance, slot_path, value):
        self._require_healthy(blueprint)
        current = self._staging.read_ownership(blueprint, instance, slot_path)
        if not self._may_write(blueprint, instance, slot_path):
            return
        if current is not None and not (
                current["kind"] == "action"
                and current["action_ref"] == self._action_ref):
            self._taken.add(f"{blueprint}:{instance}.{slot_path}")
        if not self._staging.instance_exists(blueprint, instance):
            raise ActionFailure(
                f"'{blueprint}' has no instance '{instance}' — create it first.")
        self._staging.write(blueprint, instance, slot_path, value,
                            owner="action", owner_action_ref=self._action_ref)

    def unbind(self, blueprint, instance, slot_path):
        self._require_healthy(blueprint)
        _refuse_foreign_delete(
            self._staging.read_ownership(blueprint, instance, slot_path),
            self._action_ref, cell=f"{blueprint}:{instance}.{slot_path}",
            taken=self._taken)
        self._staging.delete(blueprint, instance, slot_path)

    def _may_write(self, blueprint, instance, slot_path) -> bool:
        """Same resolution as ``_Tags._may_write`` — see there for why each branch exists.
        Policy is keyed by blueprint name, which is what a mapping binds."""
        current = self._staging.read_ownership(blueprint, instance, slot_path)
        if current is None:
            return True
        if current["kind"] == "action" and current["action_ref"] == self._action_ref:
            return True

        who = "the user" if current["kind"] == "user" else f"'{current['action_ref']}'"
        cell = f"{blueprint}:{instance}.{slot_path}"
        policy = self._policies.get(policy_key("blueprint", None, blueprint))

        if policy == "overwrite":
            self._log("info", f"took {cell} from {who} (conflict policy: overwrite)")
            return True
        if policy == "skip":
            self._log("info", f"left {cell} alone — owned by {who} (conflict policy: skip)")
            return False
        if policy == "ask":
            raise ActionFailure(
                f"{cell} is owned by {who} and this mapping's conflict policy is 'ask', "
                f"which Packsmith does not support yet — choose overwrite, skip, or fail.")
        if policy == "fail":
            raise ActionFailure(
                f"{cell} is owned by {who} and this mapping's conflict policy is 'fail'.")
        raise ActionFailure(
            f"{cell} is owned by {who}, and this action declares no conflict policy for "
            f"'{blueprint}'. Declare one on the mapping that binds it.")

    def _require_healthy(self, blueprint):
        if self._store.has_orphans(blueprint):
            waiting = self._store.orphans(blueprint)
            names = ", ".join(sorted(o.instance for o in waiting[:5]))
            raise ActionFailure(
                f"'{blueprint}' has {len(waiting)} orphaned instance(s) ({names}) from a "
                f"schema change nobody has resolved yet. Resolve them in the Errors panel "
                f"before running actions against this blueprint.")


class _FileHandle:
    """A handle to one file (from ``pack.filesystem.resolve(path)``). Writes are
    staged and stamp the calling action as owner (whole-file, open-world engine)."""

    def __init__(self, staging, rel_path, action_ref):
        self._staging = staging
        self._path = rel_path
        self._action_ref = action_ref

    def write(self, content, *, file_must_exist=False):
        self._staging.write(self._path, content, owner="action",
                            owner_action_ref=self._action_ref, file_must_exist=file_must_exist)

    def read_all(self):
        return self._staging.read(self._path)

    def read_json(self):
        """Read the whole file as parsed JSON/JSON5 (comments tolerated). Returns
        None if the file doesn't exist."""
        content = self._staging.read(self._path)
        return None if content is None else _loads_json5(content)

    def write_json(self, obj, *, file_must_exist=False):
        """Write the whole file as pretty JSON, stamping the calling action as owner.
        Quick-and-dirty: comments are NOT preserved (whole-file ownership). Per-key,
        comment-preserving round-tripping is a deferred future capability."""
        self._staging.write(self._path, _dumps_json(obj), owner="action",
                            owner_action_ref=self._action_ref, file_must_exist=file_must_exist)

    def exists(self):
        return self._staging.exists(self._path)

    def ownership(self):
        return self._staging.ownership(self._path)


class _Filesystem:
    """``pack.filesystem`` — whole-file access within the instance root. Raises if
    the step wasn't given a filesystem provider (no instance / file store)."""

    def __init__(self, staging, action_ref):
        self._staging = staging
        self._action_ref = action_ref

    def resolve(self, path):
        if self._staging is None:
            raise RuntimeError("filesystem capability is not available for this step")
        return _FileHandle(self._staging, path, self._action_ref)


class _PackNamespace:
    """``pack.datapacks`` / ``pack.resourcepacks`` — provider-routed writes (design 7.3).

    The action never learns which loader is installed. It names a pack the *user* bound to
    the step and a namespace path, and the active provider (§8.1) computes where that lands
    — which is what makes an action portable across Paxi, OpenLoader and Moonlight.

    Unlike ``filesystem``, this resolver checks that the **pack** exists. §7.3 says
    resolvers don't check whether the target *file* exists, and that still holds; a missing
    pack is a different thing. A directory with no `pack.mcmeta` is not loaded by Minecraft
    at all, so writing into a pack that isn't there succeeds, changes nothing in-game, and
    looks exactly like it worked.
    """

    # Minecraft's own structure, not the loader's: `data/` is datapack territory and
    # `assets/` is resource pack territory (§6.5).
    _ROOTS = {"datapacks": "data", "resourcepacks": "assets"}

    def __init__(self, staging, action_ref, targets, kind):
        self._staging = staging
        self._action_ref = action_ref
        self._targets = targets
        self._kind = kind

    @property
    def _noun(self):
        return "datapack" if self._kind == "datapacks" else "resource pack"

    def resolve(self, pack, namespace, path):
        if self._staging is None:
            raise CapabilityError(
                f"{self._kind} capability is not available for this step (no file store)")
        provider = self._targets.provider_for(self._kind) if self._targets else None
        if provider is None:
            raise CapabilityError(
                f"nothing in this profile provides '{self._kind}.write' — install a global "
                f"pack loader such as Paxi (design 8.1)")
        if not pack:
            raise CapabilityError(
                f"no {self._noun} was given — bind one to a 'pack' mapping on this step "
                f"rather than naming it in the action (design 3.3)")
        available = self._targets.available(self._kind) or []
        if pack not in available:
            raise CapabilityError(
                f"there is no {self._noun} called '{pack}' — writing into it would produce "
                f"a folder the game silently ignores")
        member = f"{self._ROOTS[self._kind]}/{namespace}/{str(path).lstrip('/')}"
        target = provider.override_path(self._targets.root, pack, member, kind=self._kind)
        rel = target.relative_to(self._targets.root).as_posix()
        return _FileHandle(self._staging, rel, self._action_ref)


class _Capabilities:
    """``pack.capabilities`` — introspection (design 7.4), for actions that declare a
    capability optional and branch on whether it is there."""

    def __init__(self, table):
        self._table = table

    def has(self, name) -> bool:
        return bool(self._table is not None and self._table.satisfies(name))

    def version(self, name):
        """The active provider's version, or None. Providers carry no version yet (§7.1's
        versioning model is declared, not implemented), so this answers None until they do
        — which is the same answer as "no provider", deliberately: an action must not read
        a missing version as a satisfied one."""
        provider = self._table.provider_for(name) if self._table is not None else None
        return getattr(provider, "version", None)


class _Step:
    """``pack.step`` — the bindings and configuration the user set on this job step."""

    def __init__(self, mappings, config):
        self.mappings = dict(mappings or {})
        self.config = dict(config or {})


class Pack:
    """The capability object injected into an action for a single step invocation."""

    def __init__(self, *, staging, tag_store, packdump, action_ref,
                 file_staging=None, mappings=None, config=None, conflict_policies=None,
                 blueprint_staging=None, blueprint_store=None, pack_targets=None):
        self.action_ref = action_ref
        self._log = []
        # Set by fail(); None means "no deliberate failure was requested".
        self.failure_reason = None
        self.registry = _Registry(packdump)
        self.tags = _Tags(staging, tag_store, action_ref,
                          conflict_policies=conflict_policies, log=self.log)
        self.blueprints = _Blueprints(blueprint_staging, blueprint_store, action_ref,
                                      conflict_policies=conflict_policies, log=self.log) \
            if blueprint_store is not None else None
        self.filesystem = _Filesystem(file_staging, action_ref)
        # Provider-routed (§7.3): the same two namespaces exist whether or not a loader is
        # installed, and refuse with a message naming what is missing rather than being
        # absent — `pack.datapacks` raising AttributeError would tell the author nothing.
        self.datapacks = _PackNamespace(file_staging, action_ref, pack_targets, "datapacks")
        self.resourcepacks = _PackNamespace(file_staging, action_ref, pack_targets,
                                            "resourcepacks")
        self.capabilities = _Capabilities(
            pack_targets.table if pack_targets is not None else None)
        self.step = _Step(mappings, config)

    def log(self, level, message):
        self._log.append((level, message))

    def fail(self, reason):
        """Halt the step deliberately (design 3.3 — Starlark has no exceptions, so this is
        the only way an author signals failure).

        The reason is recorded on the Pack *before* raising, because the exception itself
        does not survive the Starlark boundary: starlark-pyo3 wraps any host exception in
        a ``StarlarkError``, which makes a deliberate ``fail()`` indistinguishable from a
        genuine bug by type alone. This flag is how the runtime tells them apart.
        """
        self.failure_reason = reason
        raise ActionFailure(reason)

    @property
    def log_lines(self):
        return list(self._log)
