"""On-disk action packages and the manifest → callable seam (design 3.3.1, 7.x).

A package is a folder with a ``manifest.toml`` declaring one or more actions, plus the
``.star`` files their entry points live in. The manifest is the durable, language-agnostic
part: it describes the action without caring what its body is written in.

``PackageIndex.load_callable`` is the ONE language-specific seam: it reads the action's
``.star`` source and returns a closure that evaluates it. Everything above it (scanning,
indexing, ref resolution, the runner) never learns what language actions are written in —
which is exactly what let the Python-callable stand-in be swapped out for real Starlark
without touching a line of the runner.
"""
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from packsmith.core.shapes import parse_shape
from packsmith.core.starlark_runtime import PackageLoader, run_starlark


@dataclass
class MappingSlot:
    """A declared mapping slot the user binds a Layer 2 artifact into (design 7.1).
    ``kind`` is ``tag``, ``blueprint`` (a schema) or ``blueprint_instance``."""
    name: str
    kind: str = "tag"
    tag_type: str = None          # required kind of tag (bool/string/enum/number/reference)
    registry_type: str = None     # e.g. minecraft:item
    access: str = "read"          # read | write | read_write (stored; write-on-read enforcement deferred)
    cardinality: str = "one"      # one | many (MVP: one)
    required: bool = True
    likely_name: str = None       # hint for best-guess fill
    description: str = ""
    # Structural typing for blueprint mappings (design 3.3): the SHAPE this action needs,
    # never a schema name. Flat requirements by dotted path — see core/shapes.py.
    required_shape: tuple = ()
    # The enum values this action actually branches on (design 3.3) — the tag analog of
    # required_shape, and "the shape of the thing I am bound to" for a vocabulary.
    requires_values: tuple = ()
    # Mandatory on write/read_write mappings (design 3.3): what happens when this action
    # writes a cell someone else already owns. There is deliberately NO default — "the
    # author must explicitly choose… there is no universally-correct answer."
    conflict_policy: str = None   # overwrite | skip | fail | ask


@dataclass
class ConfigParam:
    """A declared scalar configuration parameter (design 3.3.1)."""
    name: str
    type: str = "string"
    default: object = None
    required: bool = False
    description: str = ""


@dataclass
class ActionManifest:
    """One declared action (the ``[[actions]]`` entry)."""
    package_name: str
    action_id: str
    file: str
    function: str
    name: str = ""
    description: str = ""
    mappings: dict = field(default_factory=dict)   # dict[str, MappingSlot]
    config: dict = field(default_factory=dict)      # dict[str, ConfigParam]
    # conflict_policies deferred to Phase 4

    @property
    def ref(self) -> str:
        return f"{self.package_name}:{self.action_id}"


@dataclass
class Package:
    """A parsed package (the ``[package]`` block + its actions)."""
    name: str
    root: Path
    version: str = ""
    author: str = ""
    description: str = ""
    actions: list = field(default_factory=list)   # list[ActionManifest]
    # Design 3.3.1: purely a provenance label — authored and downloaded packages are
    # structurally identical. It governs *editability*, not behaviour: you may edit what
    # you wrote, not what you installed. Absent means authored, because a package you
    # created by hand has no reason to declare anything.
    provenance: str = "authored"


def load_package(package_dir) -> Package:
    """Parse a package's ``manifest.toml`` into a Package."""
    package_dir = Path(package_dir)
    manifest_path = package_dir / "manifest.toml"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"No manifest.toml in package: {package_dir}")

    with open(manifest_path, "rb") as f:
        data = tomllib.load(f)

    meta = data.get("package", {})
    name = meta.get("name")
    if not name:
        raise ValueError(f"Package manifest missing [package].name: {manifest_path}")

    actions = []
    for entry in data.get("actions", []):
        for required in ("id", "file", "function"):
            if required not in entry:
                raise ValueError(f"Action in package '{name}' missing '{required}': {entry}")
        actions.append(ActionManifest(
            package_name=name,
            action_id=entry["id"],
            file=entry["file"],
            function=entry["function"],
            name=entry.get("name", entry["id"]),
            description=entry.get("description", ""),
            mappings=_parse_mappings(entry.get("mappings", {})),
            config=_parse_config(entry.get("configuration", {})),
        ))

    provenance = meta.get("provenance", "authored")
    if provenance not in ("authored", "downloaded"):
        raise ValueError(f"Package '{name}': provenance must be authored or downloaded")

    return Package(
        name=name, root=package_dir,
        version=meta.get("version", ""), author=meta.get("author", ""),
        description=meta.get("description", ""), actions=actions,
        provenance=provenance,
    )


class PackageIndex:
    """Scans a packages directory, indexes every declared action by its
    ``package:action_id`` ref, and resolves refs to callables."""

    def __init__(self, packages_dir=None):
        self._packages = {}   # package_name -> Package
        self._actions = {}    # action_ref  -> ActionManifest
        self._dir = None
        if packages_dir is not None:
            self.scan(packages_dir)

    @property
    def directory(self):
        """The packages directory this index was scanned from."""
        return self._dir

    def reload(self):
        """Re-read everything from disk. Needed after a package or action is created or a
        manifest is edited — action *sources* are re-read on every run, but manifests are
        only parsed here."""
        if self._dir is None:
            return
        self._packages.clear()
        self._actions.clear()
        self.scan(self._dir)

    def scan(self, packages_dir):
        packages_dir = Path(packages_dir)
        self._dir = packages_dir
        if not packages_dir.is_dir():
            return
        for child in sorted(packages_dir.iterdir()):
            if child.is_dir() and (child / "manifest.toml").is_file():
                self.add_package(child)

    def add_package(self, package_dir):
        pkg = load_package(package_dir)
        self._packages[pkg.name] = pkg
        for action in pkg.actions:
            self._actions[action.ref] = action

    @property
    def actions(self) -> dict:
        return dict(self._actions)

    @property
    def packages(self) -> dict:
        return dict(self._packages)

    def package(self, name: str):
        return self._packages.get(name)

    def get(self, action_ref: str) -> ActionManifest:
        try:
            return self._actions[action_ref]
        except KeyError:
            raise KeyError(f"No action '{action_ref}' in the package index")

    def load_callable(self, action_ref: str):
        """Resolve an action ref to something the runner can call with ``pack``.

        This is the language seam. It returns a plain Python closure, so everything above
        it — ``run_action``, the job runner, the GUI — is unchanged by the fact that the
        action's body is Starlark rather than Python.
        """
        manifest = self.get(action_ref)
        pkg = self._packages[manifest.package_name]
        path = pkg.root / manifest.file
        if not path.is_file():
            raise FileNotFoundError(f"Action file not found: {path}")
        source = path.read_text(encoding="utf-8")

        def invoke(pack):
            # A fresh loader per invocation: its module cache exists to stop a diamond
            # re-evaluating within one run, not to survive between runs. Sources are
            # re-read every run so an edit takes effect without a restart, and helper
            # files have to obey the same rule as entry points.
            loader = PackageLoader(pkg.root, pkg.name)
            return run_starlark(source, pack, function=manifest.function,
                                filename=manifest.file, loader=loader)
        invoke.__name__ = f"{manifest.package_name}_{manifest.action_id}"
        return invoke


CONFLICT_POLICIES = ("overwrite", "skip", "fail", "ask")
WRITE_ACCESS = ("write", "read_write")
# `blueprint` binds one of the user's SCHEMAS — the action works over all of its instances.
# `blueprint_instance` binds the instances themselves, which is what an action that should
# run over *these six stone types* and not the whole schema needs. Both are typed by
# `required_shape` (3.3), never by the name the user gave their schema.
# `registry_entry` binds one specific Layer 1 entry, typed by `registry_type` — for the
# action that needs "the block you want everything cut from", not a whole tag or schema.
MAPPING_KINDS = ("tag", "blueprint", "blueprint_instance", "registry_entry")
BLUEPRINT_KINDS = ("blueprint", "blueprint_instance")
# 3.3: `one` is a single-select picker, `many` is "zero or more" and a multi-select list.
CARDINALITIES = ("one", "many")

MANIFEST_NAME = "manifest.toml"
SOURCE_SUFFIX = ".star"

# Package and action names become part of a public identifier (`package:action_id`) and a
# folder name, so keep them boring: lowercase, no spaces, no path separators.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FILE_RE = re.compile(r"^[a-z][a-z0-9_]*\.star$")

_STUB_ACTION = '''"""{summary}

Starlark: the only way out of this file is `pack`. See the capability API for what's
available — pack.registry, pack.tags, pack.filesystem, pack.step, pack.log, pack.fail.
"""

def {function}(pack):
    pack.log("info", "hello from {ref}")
'''

# Appended when an action is declared against a file that already exists. Declaring an
# entry point the file doesn't define would leave the package loadable but broken at run
# time, and the failure would surface far from the mistake.
_STUB_FUNCTION = '''

def {function}(pack):
    pack.log("info", "hello from {ref}")
'''

_STUB_SOURCE = '''"""{summary}

A plain Starlark source file. Nothing in here runs on its own — either an action in
{manifest} points at one of its functions, or another file in this package loads from it:

    load("//{path}", "helper")

Names starting with an underscore stay private to this file; that's the language, not a
convention. Loaded helpers get no `pack` of their own, so anything that needs to touch
the pack takes it as an argument from the action that called it.
"""

def helper(pack):
    pack.log("info", "hello from {path}")
'''


def _check_name(kind: str, name: str):
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"{kind} name '{name}' is invalid — use lowercase letters, digits and "
            f"underscores, starting with a letter")


def _segments(path) -> list[str]:
    return [p for p in str(path or "").replace("\\", "/").split("/") if p]


def _check_folder_path(path) -> str:
    """Validate a package-relative folder path, returning it normalised to posix.

    Every segment must be a plain name, which is also what keeps ``..`` out — there is no
    separate escape check because no traversal segment can pass the name rule.
    """
    parts = _segments(path)
    if not parts:
        raise ValueError("A folder needs a name")
    for part in parts:
        if not _NAME_RE.match(part):
            raise ValueError(
                f"Folder name '{part}' is invalid — use lowercase letters, digits and "
                f"underscores, starting with a letter")
    return "/".join(parts)


def _check_file_path(path) -> str:
    """Validate a package-relative file path, returning it normalised to posix."""
    parts = _segments(path)
    if not parts:
        raise ValueError("A file needs a name")
    if len(parts) > 1:
        _check_folder_path("/".join(parts[:-1]))
    if not _FILE_RE.match(parts[-1]):
        raise ValueError(
            f"File name '{parts[-1]}' is invalid — use lowercase letters, digits and "
            f"underscores, starting with a letter, ending in {SOURCE_SUFFIX}")
    return "/".join(parts)


def _require_authored(package: Package):
    """Provenance, not ownership (design 3.3.1): you may edit what you wrote, not what you
    installed. A downloaded package refuses every structural edit at the source."""
    if package.provenance != "authored":
        raise ValueError(f"'{package.name}' is a downloaded package and can't be edited")


def create_package(packages_dir, name: str, *, description: str = "",
                   author: str = "", version: str = "0.1.0") -> Package:
    """Create an authored package on disk (design 3.3.1).

    Nothing about an authored package is special — "no privileged local package, no magic
    default bucket, no special namespace." It is exactly what a downloaded package is, which
    is what lets one be published later without restructuring.
    """
    _check_name("Package", name)
    root = Path(packages_dir) / name
    if root.exists():
        raise ValueError(f"A package named '{name}' already exists")
    root.mkdir(parents=True)
    lines = ["[package]", f'name = "{name}"', f'version = "{version}"']
    if author:
        lines.append(f'author = "{author}"')
    if description:
        lines.append(f'description = "{description}"')
    lines.append("")
    (root / "manifest.toml").write_text("\n".join(lines), encoding="utf-8")
    return load_package(root)


def add_action(package: Package, action_id: str, *, file: str = None, name: str = "",
               description: str = "", function: str = "run") -> Path:
    """Declare a new action in an existing package, creating its source if needed.

    ``file`` and ``function`` together are the entry point, and they are genuinely
    independent of the action id: one file may hold many actions, and a file may hold none
    at all. Defaulting ``file`` to ``<action_id>.star`` is a convenience for the common
    case, not a rule the rest of the system knows about.

    The manifest is **appended to as text**, never regenerated through a TOML writer. The
    file belongs to the user — they may have comments and formatting in it — and rewriting
    it wholesale would quietly destroy that. Same round-tripping concern that governs mod
    configs, applied to our own file format.
    """
    _check_name("Action", action_id)
    _check_name("Function", function)
    _require_authored(package)
    if any(a.action_id == action_id for a in package.actions):
        raise ValueError(f"'{package.name}' already declares an action '{action_id}'")

    file_name = _check_file_path(file or f"{action_id}{SOURCE_SUFFIX}")
    clash = next((a for a in package.actions
                  if a.file == file_name and a.function == function), None)
    if clash is not None:
        raise ValueError(f"'{clash.action_id}' already points at {file_name}:{function}()")

    entry = ["", "[[actions]]", f'id = "{action_id}"', f'file = "{file_name}"',
             f'function = "{function}"']
    if name:
        entry.append(f'name = "{name}"')
    if description:
        entry.append(f'description = "{description}"')
    entry.append("")

    manifest = package.root / MANIFEST_NAME
    existing = manifest.read_text(encoding="utf-8").rstrip("\n")
    manifest.write_text(existing + "\n" + "\n".join(entry), encoding="utf-8")

    ref = f"{package.name}:{action_id}"
    source = package.root / file_name
    if not source.is_file():
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(_STUB_ACTION.format(
            summary=description or name or f"The {action_id} action.",
            function=function, ref=ref), encoding="utf-8")
    elif not _defines(source.read_text(encoding="utf-8"), function):
        # Append only — the file is the user's, and everything already in it survives.
        with open(source, "a", encoding="utf-8") as f:
            f.write(_STUB_FUNCTION.format(function=function, ref=ref))
    return source


def remove_action(package: Package, action_id: str):
    """Undeclare an action. **The source file is left exactly where it is.**

    Removing a declaration and deleting source are different acts with different
    consequences, and the panel keeps them on different rows for that reason: undeclaring
    turns an entry point back into an ordinary function, which is how a file becomes a
    library. Deleting the file destroys the code.
    """
    _require_authored(package)
    manifest = package.root / MANIFEST_NAME
    updated = _strip_action_block(manifest.read_text(encoding="utf-8"), action_id)
    if updated is None:
        raise ValueError(f"'{package.name}' declares no action '{action_id}'")
    manifest.write_text(updated, encoding="utf-8")


def source_files(package: Package) -> list[str]:
    """Every file the package holds, manifest first, as package-relative posix paths.

    This is the *file* layer, and it is deliberately not derived from the manifest — a file
    with no declaration is exactly as real as one with three.
    """
    root = Path(package.root)
    if not root.is_dir():
        return []
    names = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    return sorted(names, key=lambda n: (n != MANIFEST_NAME, n))


def folders(package: Package) -> list[str]:
    """Every folder in the package, parents before children, as relative posix paths.

    Listed from disk rather than inferred from file paths, so an **empty** folder is still
    a folder. Organising a package usually starts by making the empty box.
    """
    root = Path(package.root)
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir())


def create_file(package: Package, file_name: str, *, description: str = "") -> Path:
    """Add a source file to a package without declaring anything.

    This is the operation that was previously impossible: the only way to get a file was to
    declare an action and then hand-edit the declaration back out.
    """
    _require_authored(package)
    file_name = _check_file_path(file_name)
    target = package.root / file_name
    if target.exists():
        raise ValueError(f"{file_name} already exists in '{package.name}'")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_STUB_SOURCE.format(
        summary=description or f"Helpers for {package.name}.",
        manifest=MANIFEST_NAME, path=file_name), encoding="utf-8")
    return target


def create_folder(package: Package, path: str) -> Path:
    """Add an empty folder. Purely organisational — nothing about a package's meaning
    depends on the shape of its tree, and an action's ``file`` is just a relative path."""
    _require_authored(package)
    path = _check_folder_path(path)
    target = package.root / path
    if target.exists():
        raise ValueError(f"{path} already exists in '{package.name}'")
    target.mkdir(parents=True)
    return target


def delete_file(package: Package, file_name: str):
    """Delete a source file. Refused while any action still points at it — the declaration
    has to come off first, so that the destructive step is the one you actually asked for."""
    _require_authored(package)
    file_name = _normalise(file_name)
    if file_name == MANIFEST_NAME:
        raise ValueError("The manifest defines the package; delete the package instead.")
    users = _declared_under(package, file_name, exact=True)
    if users:
        raise ValueError(
            f"{file_name} still implements {', '.join(users)} — remove "
            f"{'those declarations' if len(users) > 1 else 'that declaration'} first")
    target = package.root / file_name
    if not target.is_file():
        raise ValueError(f"No such file in '{package.name}': {file_name}")
    target.unlink()


def delete_folder(package: Package, path: str):
    """Delete a folder and everything under it. Refused while anything inside is declared —
    same rule as a single file, applied to the subtree."""
    _require_authored(package)
    path = _check_folder_path(path)
    users = _declared_under(package, path)
    if users:
        raise ValueError(
            f"{path}/ still holds the source for {', '.join(users)} — remove "
            f"{'those declarations' if len(users) > 1 else 'that declaration'} first")
    target = package.root / path
    if not target.is_dir():
        raise ValueError(f"No such folder in '{package.name}': {path}")
    shutil.rmtree(target)


def rename_file(package: Package, file_name: str, new_name: str) -> Path:
    """Rename a source file and retarget every declaration that pointed at it.

    ``new_name`` may include folders, which makes this a **move** as well — that is how a
    file gets filed away after the fact. Only the ``file = "…"`` lines change; the manifest
    is otherwise byte-identical.
    """
    _require_authored(package)
    file_name = _normalise(file_name)
    if file_name == MANIFEST_NAME:
        raise ValueError("The manifest can't be renamed.")
    new_name = _check_file_path(new_name)
    source = package.root / file_name
    target = package.root / new_name
    if not source.is_file():
        raise ValueError(f"No such file in '{package.name}': {file_name}")
    if target.exists():
        raise ValueError(f"{new_name} already exists in '{package.name}'")
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    _retarget(package, rf'{re.escape(file_name)}', lambda _: new_name)
    return target


def rename_folder(package: Package, path: str, new_path: str) -> Path:
    """Rename or move a folder, retargeting every declaration underneath it."""
    _require_authored(package)
    path = _check_folder_path(path)
    new_path = _check_folder_path(new_path)
    source = package.root / path
    target = package.root / new_path
    if not source.is_dir():
        raise ValueError(f"No such folder in '{package.name}': {path}")
    if target.exists():
        raise ValueError(f"{new_path} already exists in '{package.name}'")
    if f"{new_path}/".startswith(f"{path}/"):
        raise ValueError(f"Can't move {path}/ inside itself")
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    _retarget(package, rf'{re.escape(path)}/(?P<rest>[^"]*)',
              lambda m: f"{new_path}/{m.group('rest')}")
    return target


def _normalise(path) -> str:
    return "/".join(_segments(path))


def _declared_under(package: Package, path: str, *, exact: bool = False) -> list[str]:
    """Action ids whose source is ``path`` (or lives inside it, for a folder)."""
    prefix = f"{path}/"
    return sorted(a.action_id for a in package.actions
                  if a.file == path or (not exact and a.file.startswith(prefix)))


def _retarget(package: Package, pattern: str, replace):
    """Rewrite ``file = "…"`` lines in place. Everything else in the manifest — comments,
    spacing, key order — is byte-identical afterwards."""
    manifest = package.root / MANIFEST_NAME
    text = manifest.read_text(encoding="utf-8")
    line = re.compile(r'^(?P<lead>\s*file\s*=\s*)"' + pattern + r'"[ \t]*$', re.M)
    manifest.write_text(
        line.sub(lambda m: f'{m.group("lead")}"{replace(m)}"', text), encoding="utf-8")


def _defines(source: str, function: str) -> bool:
    return re.search(rf"^def\s+{re.escape(function)}\s*\(", source, re.M) is not None


def _manifest_blocks(text: str) -> list[list[str]]:
    """Split a manifest into chunks, each starting at a table header. Trailing blank lines
    stay with the block above them, which is what makes block removal leave tidy text."""
    blocks, current = [], []
    for line in text.splitlines():
        if line.lstrip().startswith("[") and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


_ID_LINE = re.compile(r'^\s*id\s*=\s*"([^"]*)"')


def _strip_action_block(text: str, action_id: str):
    """Remove one ``[[actions]]`` block and its ``[actions.*]`` sub-tables, as text.

    Returns None when nothing matched. Same reasoning as :func:`add_action`: everything
    outside the removed block — comments, spacing, key order — comes through untouched.
    """
    kept, removed, dropping = [], False, False
    for block in _manifest_blocks(text):
        header = block[0].lstrip()
        if header.startswith("[[actions]]"):
            dropping = any((m := _ID_LINE.match(line)) and m.group(1) == action_id
                           for line in block)
        elif dropping and not header.startswith("[actions."):
            dropping = False
        if dropping:
            removed = True
            continue
        kept.append(block)
    if not removed:
        return None
    lines = [line for block in kept for line in block]
    return "\n".join(lines).rstrip("\n") + "\n"


def _parse_mappings(raw: dict) -> dict:
    mappings = {}
    for name, spec in raw.items():
        kind = spec.get("kind", "tag")
        if kind not in MAPPING_KINDS:
            raise ValueError(
                f"mapping '{name}': unknown kind '{kind}' "
                f"(expected one of {', '.join(MAPPING_KINDS)})")
        access = spec.get("access", "read")
        policy = spec.get("conflict_policy")
        # Design 3.3: conflict policy is a MANDATORY per-write-mapping declaration with no
        # default. Refusing the package at load time is the only way that stays true — a
        # default applied quietly here would be exactly the silent choice the rule forbids.
        if access in WRITE_ACCESS and policy is None:
            raise ValueError(
                f"mapping '{name}' has access='{access}' but declares no conflict_policy; "
                f"one of {', '.join(CONFLICT_POLICIES)} is required")
        if policy is not None and policy not in CONFLICT_POLICIES:
            raise ValueError(
                f"mapping '{name}': invalid conflict_policy '{policy}' "
                f"(expected one of {', '.join(CONFLICT_POLICIES)})")
        if kind == "registry_entry" and not spec.get("registry_type"):
            # 3.3 types this kind by registry_type, and without one the picker would have
            # to offer every entry of every registry — which is not a contract.
            raise ValueError(
                f"mapping '{name}' is kind 'registry_entry' but declares no registry_type")
        cardinality = spec.get("cardinality", "one")
        if cardinality not in CARDINALITIES:
            raise ValueError(
                f"mapping '{name}': invalid cardinality '{cardinality}' "
                f"(expected one of {', '.join(CARDINALITIES)})")
        raw_values = spec.get("requires_values")
        if raw_values is not None:
            if kind != "tag" or spec.get("tag_type") != "enum":
                raise ValueError(
                    f"mapping '{name}': requires_values only applies to enum tag mappings")
            if not isinstance(raw_values, (list, tuple)) or not all(
                    isinstance(v, str) for v in raw_values):
                raise ValueError(
                    f"mapping '{name}': requires_values must be a list of strings")
        raw_shape = spec.get("required_shape")
        if raw_shape is not None and kind not in BLUEPRINT_KINDS:
            # The tag analog of required_shape is `requires_values`; a shape on a tag
            # mapping is an author mistake worth naming rather than ignoring.
            raise ValueError(
                f"mapping '{name}': required_shape only applies to blueprint mappings, "
                f"not kind '{kind}'")
        shape = parse_shape(raw_shape, where=f"mapping '{name}'")
        mappings[name] = MappingSlot(
            name=name,
            kind=kind,
            tag_type=spec.get("tag_type"),
            registry_type=spec.get("registry_type"),
            access=access,
            cardinality=cardinality,
            required=spec.get("required", True),
            likely_name=spec.get("likely_name"),
            description=spec.get("description", ""),
            conflict_policy=policy,
            required_shape=shape,
            requires_values=tuple(raw_values or ()),
        )
    return mappings


def _parse_config(raw: dict) -> dict:
    return {
        name: ConfigParam(
            name=name,
            type=spec.get("type", "string"),
            default=spec.get("default"),
            required=spec.get("required", False),
            description=spec.get("description", ""),
        )
        for name, spec in raw.items()
    }


