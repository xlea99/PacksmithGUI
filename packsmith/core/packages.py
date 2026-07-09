"""On-disk action packages and the manifest → callable seam (design 3.3.1, 7.x).

A package is a folder with a ``manifest.toml`` declaring one or more actions, plus the
``.py`` files their entry points live in. The manifest is the durable, language-agnostic
part: read identically whether the action body is Python now or Starlark later.

``PackageIndex.load_callable`` is the ONE Python-specific seam — today it ``importlib``s
a ``.py`` and grabs the function; later it compiles a ``.star``. Everything above it
(scanning, indexing, ref resolution, the runner) is unchanged by that swap.
"""
import importlib.util
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MappingSlot:
    """A declared mapping slot the user binds a Layer 2 artifact into (design 7.1).
    MVP supports ``kind='tag'``; blueprint kinds are deferred."""
    name: str
    kind: str = "tag"
    tag_type: str = None          # required kind of tag (bool/string/enum/number/reference)
    registry_type: str = None     # e.g. minecraft:item (informational for now — see note below)
    access: str = "read"          # read | write | read_write (stored; enforcement deferred to Phase 4)
    cardinality: str = "one"      # one | many (MVP: one)
    required: bool = True
    likely_name: str = None       # hint for best-guess fill
    description: str = ""


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

    return Package(
        name=name, root=package_dir,
        version=meta.get("version", ""), author=meta.get("author", ""),
        description=meta.get("description", ""), actions=actions,
    )


class PackageIndex:
    """Scans a packages directory, indexes every declared action by its
    ``package:action_id`` ref, and resolves refs to callables."""

    def __init__(self, packages_dir=None):
        self._packages = {}   # package_name -> Package
        self._actions = {}    # action_ref  -> ActionManifest
        if packages_dir is not None:
            self.scan(packages_dir)

    def scan(self, packages_dir):
        packages_dir = Path(packages_dir)
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

    def get(self, action_ref: str) -> ActionManifest:
        try:
            return self._actions[action_ref]
        except KeyError:
            raise KeyError(f"No action '{action_ref}' in the package index")

    def load_callable(self, action_ref: str):
        """Resolve an action ref to its Python callable — the one Python-specific
        seam (``importlib`` now, Starlark compile later)."""
        manifest = self.get(action_ref)
        pkg = self._packages[manifest.package_name]
        module = _import_module_from_path(
            pkg.root / manifest.file,
            f"packsmith_action_{manifest.package_name}_{manifest.action_id}",
        )
        fn = getattr(module, manifest.function, None)
        if not callable(fn):
            raise AttributeError(
                f"Action '{action_ref}' names function '{manifest.function}' in "
                f"'{manifest.file}', which is missing or not callable")
        return fn


def _parse_mappings(raw: dict) -> dict:
    return {
        name: MappingSlot(
            name=name,
            kind=spec.get("kind", "tag"),
            tag_type=spec.get("tag_type"),
            registry_type=spec.get("registry_type"),
            access=spec.get("access", "read"),
            cardinality=spec.get("cardinality", "one"),
            required=spec.get("required", True),
            likely_name=spec.get("likely_name"),
            description=spec.get("description", ""),
        )
        for name, spec in raw.items()
    }


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


def _import_module_from_path(file_path, module_name):
    file_path = Path(file_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Action file not found: {file_path}")
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
