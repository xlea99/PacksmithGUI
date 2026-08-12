"""Open Loader — a global pack loader (design 8.1).

Layout, verified by installing the mod on a 1.20.1 Forge instance and letting it generate
its own folders:

    config/openloader/
        data/                       datapacks — folders and .zip both work
        resources/                  resource packs
        advanced_options.json       {"dataPacks": {"enabled": true, "additionalFolders": []},
                                     "resourcePacks": {...}, "displaySourceName": true}

**The path had to be settled empirically, and the mod's own README is wrong about it.**
The README on the 1.20.1 branch says `.minecraft/openloader/data/` — instance root. The
generated folders say `config/openloader/data`. Trusting the repo would have produced a
directory the game never reads, failing silently, which is the exact failure mode §8.1's
override mechanism cannot tolerate.

**No load ordering.** Resource packs are ordered by sorting their names alphabetically and
datapacks are ordered per-world through the `/datapack` command — neither of which is a
thing Packsmith can set on the user's behalf, so `datapacks.ordering` is genuinely absent
rather than merely unimplemented.

Either kind can be **switched off independently** in `advanced_options.json`, which is why
capabilities are read from disk rather than declared once.
"""
import json
from pathlib import Path

from packsmith.core.capabilities import (
    DATAPACKS_READ, DATAPACKS_WRITE, PackLoaderProvider,
    RESOURCEPACKS_READ, RESOURCEPACKS_WRITE)

OPTIONS_FILE = "advanced_options.json"
PACK_SUFFIXES = (".zip",)


class OpenLoaderProvider(PackLoaderProvider):
    """Open Loader, by Darkhax."""

    name = "Open Loader"
    mod_id = "openloader"
    mod_id_verified = True          # read from OpenLoader-Forge-1.20.1-19.0.5.jar
    capabilities = (DATAPACKS_READ, DATAPACKS_WRITE,
                    RESOURCEPACKS_READ, RESOURCEPACKS_WRITE)
    summary = ("Datapacks and resource packs, no load ordering — resource packs sort "
               "alphabetically and datapacks are ordered per world in-game.")

    def root(self, instance_root) -> Path:
        return Path(instance_root) / "config" / "openloader"

    def datapack_root(self, instance_root) -> Path:
        return self.root(instance_root) / "data"

    def resourcepack_root(self, instance_root) -> Path:
        return self.root(instance_root) / "resources"

    def _options(self, instance_root) -> dict:
        try:
            return json.loads(
                (self.root(instance_root) / OPTIONS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # No file yet (the mod writes it on first launch) or unreadable. Both mean
            # "assume the defaults", which are on — pretending a capability is missing
            # because a config hasn't been generated would be its own wrong answer.
            return {}

    def available_capabilities(self, instance_root) -> tuple:
        if instance_root is None:
            return tuple(self.capabilities)
        options = self._options(instance_root)
        offered = []
        for key, caps in (("dataPacks", (DATAPACKS_READ, DATAPACKS_WRITE)),
                          ("resourcePacks", (RESOURCEPACKS_READ, RESOURCEPACKS_WRITE))):
            if options.get(key, {}).get("enabled", True):
                offered.extend(caps)
        return tuple(offered)

    def _root_for(self, instance_root, kind: str) -> Path:
        return (self.datapack_root(instance_root) if kind == "datapacks"
                else self.resourcepack_root(instance_root))

    def packs(self, instance_root, kind="datapacks") -> list:
        """Alphabetical, because that is genuinely the order the game loads them in for
        resource packs — this is not a display choice."""
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
