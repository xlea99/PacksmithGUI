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
from pathlib import Path

import starlark
from starlark import FileLoader, Globals, LibraryExtension, Module, StarlarkError

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


class PackageLoader:
    """Resolves intra-package ``load("//path.star", …)`` against one package root (3.3.1).

    Three properties are load-bearing, and each is a decision rather than a detail:

    * **Containment.** ``//`` means the package root and nothing above it. A load that
      escapes is refused — the same shape as ``pack.filesystem`` being scoped to the
      instance root. This is what makes "a package can only talk to itself" true, and it
      is why the manifest does not need to list every file it owns: the boundary is the
      directory, checked here, not a list that can drift from disk.
    * **No ambient capability** (3.3.1). Loaded modules are evaluated with the globals and
      *nothing else* — no ``pack``. A helper has exactly the authority its caller hands it
      as an argument, which keeps capability tracking readable at the call site.
    * **Freeze-and-cache per run.** A module is evaluated once per action invocation and
      reused, so a diamond doesn't re-evaluate. The cache is deliberately per-invocation:
      sources are re-read on every run so an edit takes effect without a restart, and a
      longer-lived cache would quietly defeat that.

    Cross-package ``@other//`` is not handled here. It needs the dependency list to
    resolve against, which needs an installer that doesn't exist yet.
    """

    PREFIX = "//"

    def __init__(self, package_root, package_name: str = ""):
        self._root = Path(package_root).resolve()
        self._name = package_name or self._root.name
        self._cache = {}
        self._loading = []      # the active load chain, for cycle detection

    def file_loader(self) -> FileLoader:
        return FileLoader(self.load)

    def resolve(self, module_id: str) -> Path:
        """Turn a Starlark module id into a real path inside the package."""
        if module_id.startswith("@"):
            raise ValueError(
                f"cross-package load '{module_id}' isn't supported yet — an action can "
                f"currently only load from its own package with "
                f"'{self.PREFIX}path/to/file.star'")
        if not module_id.startswith(self.PREFIX):
            raise ValueError(
                f"load path '{module_id}' must start with '{self.PREFIX}', which means "
                f"the root of package '{self._name}' (e.g. "
                f"'{self.PREFIX}helpers/thing.star')")

        relative = module_id[len(self.PREFIX):]
        if not relative.endswith(".star"):
            raise ValueError(f"load path '{module_id}' must name a .star file")
        # Rejected by name rather than by the containment check below, because "you can't
        # go up out of the package" is the actual rule and says so.
        if ".." in relative.replace("\\", "/").split("/"):
            raise ValueError(
                f"load path '{module_id}' leaves package '{self._name}'; a package can "
                f"only load its own files")

        target = (self._root / relative).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError(
                f"load path '{module_id}' leaves package '{self._name}'; a package can "
                f"only load its own files")
        if not target.is_file():
            raise ValueError(f"no such file in package '{self._name}': {relative}")
        return target

    def load(self, module_id: str):
        """The FileLoader callback: evaluate a module and hand back its frozen form."""
        if module_id in self._cache:
            return self._cache[module_id]
        if module_id in self._loading:
            chain = " -> ".join(self._loading + [module_id])
            raise ValueError(f"load cycle: {chain}")

        target = self.resolve(module_id)
        self._loading.append(module_id)
        try:
            module = Module()
            # No _inject here, deliberately: helpers get no ambient `pack`.
            starlark.eval(module, starlark.parse(module_id,
                                                 target.read_text(encoding="utf-8")),
                          _GLOBALS, self.file_loader())
            frozen = module.freeze()
        finally:
            self._loading.pop()
        self._cache[module_id] = frozen
        return frozen


class _NoBlueprints:
    """Stands in when a step was constructed without a blueprint store.

    ``add_callable`` needs something to bind either way, so the absence has to be a
    callable that explains itself rather than a missing name — a Starlark NameError on
    ``_bp_bind`` would tell the author nothing about why.
    """

    def __getattr__(self, name):
        def unavailable(*_args, **_kwargs):
            raise ActionFailure("the blueprint capability is not available for this step")
        return unavailable


def _blueprints(pack):
    return pack.blueprints if pack.blueprints is not None else _NoBlueprints()


def _prelude(pack) -> str:
    """The Starlark source that assembles `pack` from the injected callables.

    Static data is baked in as literals; behaviour is referenced by the underscore-prefixed
    names that :func:`_inject` adds to the module.
    """
    return f"""
def _bp_slot_info(blueprint, path):
    # add_callable can only hand back plain values, so the host returns a dict and this
    # promotes it to a struct — `slot.registry_type` rather than `slot["registry_type"]`,
    # matching how `pack` itself reads.
    d = _bp_slot(blueprint, path)
    if d == None:
        return None
    return struct(
        path = d["path"],
        name = d["name"],
        kind = d["kind"],
        type = d["type"],
        registry_type = d["registry_type"],
        blueprint = d["blueprint"],
        values = d["values"],
        group = d["group"],
    )

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
    blueprints = struct(
        names = _bp_names,
        instances = _bp_instances,
        slots = _bp_slots,
        slot = _bp_slot_info,
        get = _bp_get,
        bindings = _bp_bindings,
        ownership = _bp_ownership,
        gaps = _bp_gaps,
        has = _bp_has,
        create = _bp_create,
        bind = _bp_bind,
        unbind = _bp_unbind,
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
        # Blueprint SCHEMA methods are deliberately absent (design 3.2.2 — "actions cannot
        # create, modify, rename, retype, or delete schemas"). Starlark can reach nothing
        # that isn't injected here, so the rule needs no guard.
        "_bp_names": _blueprints(pack).names,
        "_bp_instances": _blueprints(pack).instances,
        "_bp_slots": _blueprints(pack).slots,
        "_bp_slot": _blueprints(pack).slot,
        "_bp_get": _blueprints(pack).get,
        "_bp_bindings": _blueprints(pack).bindings,
        "_bp_ownership": _blueprints(pack).ownership,
        "_bp_gaps": _blueprints(pack).gaps,
        "_bp_has": _blueprints(pack).has,
        "_bp_create": _blueprints(pack).create,
        "_bp_bind": _blueprints(pack).bind,
        "_bp_unbind": _blueprints(pack).unbind,
        # Filesystem operations take the path first so `partial` can pre-bind it.
        "_fs_read_all": lambda path: pack.filesystem.resolve(path).read_all(),
        "_fs_read_json": lambda path: pack.filesystem.resolve(path).read_json(),
        # The existence flags are part of the declared surface (§7.3) — dropping them here
        # made `handle.write(x, file_must_exist=True)` a TypeError from Starlark, so the
        # only guard an action could ask for was unreachable from the only language that
        # writes actions.
        "_fs_write": lambda path, content, file_must_exist=False:
            pack.filesystem.resolve(path).write(
                content, file_must_exist=file_must_exist),
        "_fs_write_json": lambda path, obj, file_must_exist=False:
            pack.filesystem.resolve(path).write_json(
                obj, file_must_exist=file_must_exist),
        "_fs_exists": lambda path: pack.filesystem.resolve(path).exists(),
        "_fs_ownership": lambda path: pack.filesystem.resolve(path).ownership(),
    }.items():
        module.add_callable(name, fn)


def run_starlark(source: str, pack, *, function: str = "run", filename: str = "action.star",
                 loader: PackageLoader = None):
    """Evaluate an action's Starlark source and call its entry point with ``pack``.

    ``loader`` enables intra-package ``load()``; without one, a ``load`` statement is a
    plain Starlark error ("No imports are available"), which is the correct behaviour for
    a bare source string with no package behind it.

    Raises :class:`~packsmith.core.pack.ActionFailure` when the action called
    ``pack.fail()``, and :class:`StarlarkActionError` for anything else. The caller
    (``run_action``) already treats both as "discard the step" — the distinction exists so
    a deliberate refusal reads as the author's reason instead of a stack trace.
    """
    module = Module()
    _inject(module, pack)
    files = loader.file_loader() if loader is not None else None
    try:
        starlark.eval(module, starlark.parse("_prelude.star", _prelude(pack)), _GLOBALS)
        # Only the action's own source can carry loads; the prelude and the call are ours.
        starlark.eval(module, starlark.parse(filename, source), _GLOBALS, files)
        return starlark.eval(module, starlark.parse(f"{filename}#call",
                                                    f"{function}(pack)"), _GLOBALS)
    except StarlarkError as e:
        # Host exceptions are wrapped by starlark-pyo3, so the exception TYPE can't tell us
        # whether this was a deliberate fail(). The flag Pack.fail() set can.
        if pack.failure_reason is not None:
            raise ActionFailure(pack.failure_reason) from None
        raise StarlarkActionError(str(e)) from None
