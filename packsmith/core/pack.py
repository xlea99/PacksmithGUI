"""The ``pack`` object — the capability surface an action's code calls (design 7.x).

For the MVP the action body is plain Python (the honest-gentleman rule), but this
object is the real, durable host-side surface: identical whether the body is Python
now or Starlark later. Every write routes through per-step staging (design 1.1) and
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
    """

    def __init__(self, staging, tag_store, action_ref):
        self._staging = staging
        self._store = tag_store
        self._action_ref = action_ref

    def query(self, registry_type, tag_name, value):
        """Entry IDs whose ``tag_name`` equals ``value`` in the committed store."""
        return self._store.query(registry_type, **{tag_name: value})

    def get(self, registry_type, entry_id, tag_name):
        return self._staging.read(registry_type, entry_id, tag_name)

    def ownership(self, registry_type, entry_id, tag_name):
        return self._staging.read_ownership(registry_type, entry_id, tag_name)

    def write(self, registry_type, entry_id, tag_name, value):
        self._staging.write(registry_type, entry_id, tag_name, value,
                            owner="action", owner_action_ref=self._action_ref)

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
                 file_staging=None, mappings=None, config=None):
        self.action_ref = action_ref
        self.registry = _Registry(packdump)
        self.tags = _Tags(staging, tag_store, action_ref)
        self.filesystem = _Filesystem(file_staging, action_ref)
        self.step = _Step(mappings, config)
        self._log = []

    def log(self, level, message):
        self._log.append((level, message))

    def fail(self, reason):
        raise ActionFailure(reason)

    @property
    def log_lines(self):
        return list(self._log)
