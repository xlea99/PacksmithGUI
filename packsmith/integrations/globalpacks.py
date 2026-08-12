"""Global Packs — a global pack loader with a different model (design 8.1).

The other three own a directory and load whatever is inside it. Global Packs owns a
**list**. From a real 1.20.1 Forge installation:

    config/global_packs.toml
        [resourcepacks]
            required = ["global_packs/required_resources/"]
            enable_builtin = []
        [datapacks]
            required = ["datapacks/", "resourcepacks/", "global_packs/required_data/"]
            optional = ["global_packs/optional_data/"]
            enable_builtin = []

    <instance>/global_packs/required_data/        generated
    <instance>/global_packs/optional_data/        generated
    <instance>/global_packs/required_resources/   generated

Two consequences worth stating plainly, because they are the reason this provider is not a
copy of the others:

**The folder is only loaded because the config says so.** Writing into
`global_packs/required_data/` works today because the generated config lists it. If a user
removes that entry, the folder still exists and is silently ignored — so the capability is
read from the toml, not assumed from the directory being present.

**Its ordering is over config entries, not over packs.** Priority follows the order of the
`required` list, and every pack we write lives *inside one entry* — so Packsmith cannot
reorder them, and this provider deliberately does **not** claim `datapacks.ordering`.
Claiming it would let an action declare a dependency on ordering and then be handed a
provider that cannot deliver it, which is precisely the silent-wrong-result the capability
split exists to prevent. A richer integration could register each pack as its own config
entry and earn the capability honestly; that is a bigger change than this is worth today.

**One thing still unverified:** whether a `required` entry names a pack *directly* or a
folder *containing* packs. Every other loader means the latter, the mod is described as
"a folder into which you can add all the datapacks you want", and the generated folders are
empty containers — so that is the reading implemented here. Worth two minutes in-game to
confirm before anyone relies on it.
"""
import json
import tomllib
from pathlib import Path

from packsmith.core.capabilities import (
    DATAPACKS_READ, DATAPACKS_WRITE, CapabilityError, PackLoaderProvider,
    RESOURCEPACKS_READ, RESOURCEPACKS_WRITE)

CONFIG_FILE = "global_packs.toml"
# What the mod generates, and what Packsmith writes into. `optional_data` is deliberately
# not used: an optional pack is one the *player* toggles per world, which is not what an
# override is for.
DEFAULT_ROOTS = {"datapacks": "global_packs/required_data",
                 "resourcepacks": "global_packs/required_resources"}
PACK_SUFFIXES = (".zip",)


class GlobalPacksProvider(PackLoaderProvider):
    """Global Packs, by JTK222."""

    name = "Global Packs"
    mod_id = "globalpacks"
    mod_id_verified = True          # read from globalpacks-forge-1.20.1-19.3.7.jar
    capabilities = (DATAPACKS_READ, DATAPACKS_WRITE,
                    RESOURCEPACKS_READ, RESOURCEPACKS_WRITE)
    summary = ("Datapacks and resource packs, driven by a list in global_packs.toml. "
               "Its ordering is over config entries rather than packs, so Packsmith "
               "cannot reorder them.")

    def _config(self, instance_root) -> dict:
        try:
            with open(Path(instance_root) / "config" / CONFIG_FILE, "rb") as handle:
                return tomllib.load(handle)
        except (OSError, ValueError):
            return {}

    def required_entries(self, instance_root, kind="datapacks") -> list:
        """The paths this loader is configured to load, relative to the instance."""
        section = self._config(instance_root).get(kind, {})
        entries = section.get("required", [])
        return [str(e).strip("/") for e in entries if isinstance(e, str)]

    def _is_listed(self, instance_root, kind: str) -> bool:
        """Is the folder Packsmith writes into actually in the config's list?

        Not present until the mod has run once — and then the default lists it. A missing
        config is treated as "will be listed", because refusing the capability on a fresh
        install would be wrong for the overwhelmingly common case.
        """
        if not (Path(instance_root) / "config" / CONFIG_FILE).is_file():
            return True
        return DEFAULT_ROOTS[kind] in self.required_entries(instance_root, kind)

    def available_capabilities(self, instance_root) -> tuple:
        if instance_root is None:
            return tuple(self.capabilities)
        offered = []
        if self._is_listed(instance_root, "datapacks"):
            offered.extend((DATAPACKS_READ, DATAPACKS_WRITE))
        if self._is_listed(instance_root, "resourcepacks"):
            offered.extend((RESOURCEPACKS_READ, RESOURCEPACKS_WRITE))
        return tuple(offered)

    def _root_for(self, instance_root, kind: str) -> Path:
        try:
            return Path(instance_root) / DEFAULT_ROOTS[kind]
        except KeyError:
            raise CapabilityError(f"unknown pack kind: {kind}")

    def datapack_root(self, instance_root) -> Path:
        return self._root_for(instance_root, "datapacks")

    def resourcepack_root(self, instance_root) -> Path:
        return self._root_for(instance_root, "resourcepacks")

    def packs(self, instance_root, kind="datapacks") -> list:
        root = self._root_for(instance_root, kind)
        try:
            return sorted(child.name for child in root.iterdir()
                          if child.is_dir() or child.suffix.lower() in PACK_SUFFIXES)
        except OSError:
            return []

    def override_path(self, instance_root, pack_name: str, member: str,
                      kind="datapacks") -> Path:
        if not pack_name or "/" in pack_name or "\\" in pack_name:
            raise ValueError(f"invalid pack name: {pack_name!r}")
        clean = str(member).replace("\\", "/").lstrip("/")
        root = self._root_for(instance_root, kind).resolve()
        target = (self._root_for(instance_root, kind) / pack_name / clean).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"override path escapes the pack: {member}")
        return target

    def create_pack(self, instance_root, pack_name: str, kind="datapacks",
                    description="Created by Packsmith", pack_format=15) -> Path:
        folder = self._root_for(instance_root, kind) / pack_name
        (folder / ("data" if kind == "datapacks" else "assets")).mkdir(
            parents=True, exist_ok=True)
        (folder / "pack.mcmeta").write_text(json.dumps(
            {"pack": {"pack_format": pack_format, "description": description}}, indent=2),
            encoding="utf-8")
        return folder
