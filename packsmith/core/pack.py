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

    def entries(self, registry_type):
        return list(self._dump.registry.get(registry_type, {}).get("values", []))

    def has(self, registry_type, entry_id):
        return entry_id in self._dump.registry.get(registry_type, {}).get("values", [])

    def attribute(self, registry_type, entry_id, name):
        return self._dump.attribute(registry_type, entry_id, name)


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

    def query(self, registry_type, tag_name, value):
        """Entry IDs whose ``tag_name`` equals ``value`` in the committed store."""
        return self._store.query(registry_type, **{tag_name: value})

    def get(self, registry_type, entry_id, tag_name):
        return self._staging.read(registry_type, entry_id, tag_name)

    def ownership(self, registry_type, entry_id, tag_name):
        return self._staging.read_ownership(registry_type, entry_id, tag_name)

    def write(self, registry_type, entry_id, tag_name, value):
        if not self._may_write(registry_type, entry_id, tag_name):
            return
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
        policy = self._policies.get(tag_name)

        if policy == "overwrite":
            self._log("info", f"took {cell} from {who} (conflict policy: overwrite)")
            return True
        if policy == "skip":
            self._log("info", f"left {cell} alone — owned by {who} (conflict policy: skip)")
            return False
        if policy == "ask":
            raise ActionFailure(
                f"{cell} is owned by {who} and this mapping's conflict policy is 'ask', "
                f"which PackSmith does not support yet — choose overwrite, skip, or fail.")
        if policy == "fail":
            raise ActionFailure(
                f"{cell} is owned by {who} and this mapping's conflict policy is 'fail'.")
        # No declared policy: the action is writing outside its declared contract, onto
        # data it doesn't own. Design 3.3 has no default for a reason — refuse, loudly.
        raise ActionFailure(
            f"{cell} is owned by {who}, and this action declares no conflict policy for "
            f"'{tag_name}'. Declare one on the mapping that binds it.")

    def clear(self, registry_type, entry_id, tag_name):
        self._staging.delete(registry_type, entry_id, tag_name)


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


class _Step:
    """``pack.step`` — the bindings and configuration the user set on this job step."""

    def __init__(self, mappings, config):
        self.mappings = dict(mappings or {})
        self.config = dict(config or {})


class Pack:
    """The capability object injected into an action for a single step invocation."""

    def __init__(self, *, staging, tag_store, packdump, action_ref,
                 file_staging=None, mappings=None, config=None, conflict_policies=None):
        self.action_ref = action_ref
        self._log = []
        # Set by fail(); None means "no deliberate failure was requested".
        self.failure_reason = None
        self.registry = _Registry(packdump)
        self.tags = _Tags(staging, tag_store, action_ref,
                          conflict_policies=conflict_policies, log=self.log)
        self.filesystem = _Filesystem(file_staging, action_ref)
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
