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
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from packsmith.core.starlark_runtime import run_starlark


@dataclass
class MappingSlot:
    """A declared mapping slot the user binds a Layer 2 artifact into (design 7.1).
    MVP supports ``kind='tag'``; blueprint kinds are deferred."""
    name: str
    kind: str = "tag"
    tag_type: str = None          # required kind of tag (bool/string/enum/number/reference)
    registry_type: str = None     # e.g. minecraft:item
    access: str = "read"          # read | write | read_write (stored; write-on-read enforcement deferred)
    cardinality: str = "one"      # one | many (MVP: one)
    required: bool = True
    likely_name: str = None       # hint for best-guess fill
    description: str = ""
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
            return run_starlark(source, pack, function=manifest.function,
                                filename=manifest.file)
        invoke.__name__ = f"{manifest.package_name}_{manifest.action_id}"
        return invoke


CONFLICT_POLICIES = ("overwrite", "skip", "fail", "ask")
WRITE_ACCESS = ("write", "read_write")


def _parse_mappings(raw: dict) -> dict:
    mappings = {}
    for name, spec in raw.items():
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
        mappings[name] = MappingSlot(
            name=name,
            kind=spec.get("kind", "tag"),
            tag_type=spec.get("tag_type"),
            registry_type=spec.get("registry_type"),
            access=access,
            cardinality=spec.get("cardinality", "one"),
            required=spec.get("required", True),
            likely_name=spec.get("likely_name"),
            description=spec.get("description", ""),
            conflict_policy=policy,
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


