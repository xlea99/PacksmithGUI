"""Running an action written in Starlark (design 7.6).

Starlark is an *embedded* language: it has no ambient environment whatsoever. There is no
`open`, no `import`, no `eval` — not because we removed them, but because nothing exists
in a Starlark module unless the host puts it there. That property is the whole reason
§3.3 chose it: a downloaded community action physically cannot touch the disk, the
network, or the clock except through the capability object we hand it.

**How the `pack` object gets across.** A Python object cannot be used from Starlark —
starlark-pyo3 marshals it as an `OpaquePythonObject`, which "cannot be interacted with
from the Starlark side" (no attribute access, no method calls). So `pack` is not passed;
it is *assembled inside Starlark* out of three ingredients:

* **behaviour** — bound methods of the real :class:`~packsmith.core.pack.Pack`, injected
  with ``Module.add_callable``;
* **data** (``pack.step.mappings`` / ``config``) — emitted as literals in the prelude,
  because ``add_callable`` only accepts callables;
* **handles** (``pack.filesystem.resolve(path).write(...)``) — constructed by a prelude
  function that pre-binds the path with ``partial``, which is what preserves §7.3's
  resolver → handle → operation shape.

The host-side ``Pack`` is unchanged and remains the real implementation; this module is
only the bridge.
"""
import json

import starlark
from starlark import Globals, LibraryExtension, Module, StarlarkError

from packsmith.core.pack import ActionFailure

# The language surface actions get. `struct` and `partial` are load-bearing (they build
# `pack` itself); `json` is a genuine convenience; `print` keeps a stray debugging print
# from being a hard error. Everything omitted here simply does not exist for an action.
_EXTENSIONS = [
    LibraryExtension.StructType,
    LibraryExtension.Partial,
    LibraryExtension.Json,
    LibraryExtension.Print,
    LibraryExtension.Typing,
]

_GLOBALS = Globals.extended_by(_EXTENSIONS)


def _literal(value) -> str:
    """Render a host value as Starlark source.

    Not JSON — the dialects diverge exactly where it hurts: JSON writes ``true``/``false``/
    ``null`` where Starlark wants ``True``/``False``/``None``. Everything else about our
    value contract (strings, numbers, lists, dicts) does overlap, so string escaping still
    borrows ``json.dumps``.
    """
    if value is None:
        return "None"
    if isinstance(value, bool):          # before int — bool is an int subclass
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_literal(k)}: {_literal(v)}" for k, v in value.items()) + "}"
    raise TypeError(
        f"{type(value).__name__} cannot cross into Starlark; step data must be strings, "
        f"numbers, booleans, None, lists, or dicts")


class StarlarkActionError(Exception):
    """An action's Starlark failed for a reason other than a deliberate ``pack.fail()`` —
    a syntax error, a bad reference, a host error. Carries Starlark's own traceback, which
    includes file and line, so it can be shown to the user verbatim."""


def _prelude(pack) -> str:
    """The Starlark source that assembles `pack` from the injected callables.

    Static data is baked in as literals; behaviour is referenced by the underscore-prefixed
    names that :func:`_inject` adds to the module.
    """
    return f"""
def _resolve(path):
    return struct(
        path = path,
        read_all = partial(_fs_read_all, path),
        read_json = partial(_fs_read_json, path),
        write = partial(_fs_write, path),
        write_json = partial(_fs_write_json, path),
        exists = partial(_fs_exists, path),
        ownership = partial(_fs_ownership, path),
    )

pack = struct(
    action_ref = {_literal(pack.action_ref)},
    log = _log,
    fail = _fail,
    registry = struct(
        entries = _registry_entries,
        has = _registry_has,
        attribute = _registry_attribute,
    ),
    tags = struct(
        query = _tags_query,
        get = _tags_get,
        ownership = _tags_ownership,
        write = _tags_write,
        clear = _tags_clear,
    ),
    filesystem = struct(resolve = _resolve),
    step = struct(
        mappings = {_literal(pack.step.mappings)},
        config = {_literal(pack.step.config)},
    ),
)
"""


def _inject(module: Module, pack):
    """Bind the host's behaviour into the module under the names the prelude expects."""
    for name, fn in {
        "_log": pack.log,
        "_fail": pack.fail,
        "_registry_entries": pack.registry.entries,
        "_registry_has": pack.registry.has,
        "_registry_attribute": pack.registry.attribute,
        "_tags_query": pack.tags.query,
        "_tags_get": pack.tags.get,
        "_tags_ownership": pack.tags.ownership,
        "_tags_write": pack.tags.write,
        "_tags_clear": pack.tags.clear,
        # Filesystem operations take the path first so `partial` can pre-bind it.
        "_fs_read_all": lambda path: pack.filesystem.resolve(path).read_all(),
        "_fs_read_json": lambda path: pack.filesystem.resolve(path).read_json(),
        "_fs_write": lambda path, content: pack.filesystem.resolve(path).write(content),
        "_fs_write_json": lambda path, obj: pack.filesystem.resolve(path).write_json(obj),
        "_fs_exists": lambda path: pack.filesystem.resolve(path).exists(),
        "_fs_ownership": lambda path: pack.filesystem.resolve(path).ownership(),
    }.items():
        module.add_callable(name, fn)


def run_starlark(source: str, pack, *, function: str = "run", filename: str = "action.star"):
    """Evaluate an action's Starlark source and call its entry point with ``pack``.

    Raises :class:`~packsmith.core.pack.ActionFailure` when the action called
    ``pack.fail()``, and :class:`StarlarkActionError` for anything else. The caller
    (``run_action``) already treats both as "discard the step" — the distinction exists so
    a deliberate refusal reads as the author's reason instead of a stack trace.
    """
    module = Module()
    _inject(module, pack)
    try:
        starlark.eval(module, starlark.parse("_prelude.star", _prelude(pack)), _GLOBALS)
        starlark.eval(module, starlark.parse(filename, source), _GLOBALS)
        return starlark.eval(module, starlark.parse(f"{filename}#call",
                                                    f"{function}(pack)"), _GLOBALS)
    except StarlarkError as e:
        # Host exceptions are wrapped by starlark-pyo3, so the exception TYPE can't tell us
        # whether this was a deliberate fail(). The flag Pack.fail() set can.
        if pack.failure_reason is not None:
            raise ActionFailure(pack.failure_reason) from None
        raise StarlarkActionError(str(e)) from None
