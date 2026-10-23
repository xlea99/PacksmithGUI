"""Open Loader — a global pack loader (design 8.1).

Two layouts, because the mod rewrote itself between Minecraft versions. Both verified by
installing the mod and letting it generate its own folders.

**19.x (Minecraft 1.20.1, Forge):**

    config/openloader/
        data/                       datapacks — folders and .zip both work
        resources/                  resource packs
        advanced_options.json       {"dataPacks": {"enabled": true, "additionalFolders": []},
                                     "resourcePacks": {...}, "displaySourceName": true}

**21.x (Minecraft 1.21.1, NeoForge):**

    config/openloader/
        packs/                      BOTH kinds, told apart by content (see below)
        options.json                {"load_data_packs": {"value": true, ...},
                                     "load_resource_packs": {"value": true, ...}, ...}

In 21.x a pack is a folder with a `pack.mcmeta`, or a .zip/.jar, and the mod reads its
*contents* to decide what it is: a `data/` directory makes it a datapack, an `assets/`
directory a resource pack, both makes it both. So one folder can be two packs at once, and
switching its manifest off switches off both halves — there is only one manifest.

**Which layout an instance has is read from disk, not from a version number.** A packdump
only names Open Loader once the game has run with it installed, and that first run is when
the mod writes its own options file — so by the time Packsmith is asked, the files say
which mod generated them. The 21.x markers win when both are present: an instance carried
forward from 1.20.1 keeps the old folders beside the new ones, and only the new ones load.

**The 19.x path had to be settled empirically, and the mod's own README is wrong about it.**
The README on the 1.20.1 branch says `.minecraft/openloader/data/` — instance root. The
generated folders say `config/openloader/data`. Trusting the repo would have produced a
directory the game never reads, failing silently, which is the exact failure mode §8.1's
override mechanism cannot tolerate.

**No load ordering** in either. Resource packs are ordered by sorting their names
alphabetically and datapacks are ordered per-world through the `/datapack` command —
neither of which is a thing Packsmith can set on the user's behalf, so
`datapacks.ordering` is genuinely absent rather than merely unimplemented.

Either kind can be **switched off independently** in the options file, which is why
capabilities are read from disk rather than declared once.
"""
import json
import zipfile
from pathlib import Path

from packsmith.core.capabilities import (
    DATAPACKS_READ, DATAPACKS_WRITE, PACK_MANIFEST, PackLoaderProvider,
    RESOURCEPACKS_READ, RESOURCEPACKS_WRITE)

OPTIONS_FILE = "options.json"                   # 21.x
LEGACY_OPTIONS_FILE = "advanced_options.json"   # 19.x
PACKS_DIR = "packs"                             # 21.x, both kinds
PACK_SUFFIXES = (".zip", ".jar")                # 19.x read only .zip; a .jar there is inert
# What makes a 21.x pack one kind or the other: the directory it carries.
_CONTENT_DIRS = {"datapacks": "data", "resourcepacks": "assets"}


class OpenLoaderProvider(PackLoaderProvider):
    """Open Loader, by Darkhax."""

    name = "Open Loader"
    mod_id = "openloader"
    mod_id_verified = True          # read from OpenLoader-Forge-1.20.1-19.0.5.jar and
                                    # OpenLoader-neoforge-1.21.1-21.1.5.jar
    capabilities = (DATAPACKS_READ, DATAPACKS_WRITE,
                    RESOURCEPACKS_READ, RESOURCEPACKS_WRITE)
    summary = ("Datapacks and resource packs, no load ordering — resource packs sort "
               "alphabetically and datapacks are ordered per world in-game.")

    def root(self, instance_root) -> Path:
        return Path(instance_root) / "config" / "openloader"

    def unified(self, instance_root) -> bool:
        """Whether this instance runs the 21.x layout (one `packs/` folder for both kinds).

        With no files at all — the mod has not run yet — the answer is the current layout,
        which is what a fresh install generates today.
        """
        root = self.root(instance_root)
        if (root / OPTIONS_FILE).is_file() or (root / PACKS_DIR).is_dir():
            return True
        legacy = ((root / LEGACY_OPTIONS_FILE).is_file() or (root / "data").is_dir()
                  or (root / "resources").is_dir())
        return not legacy

    def datapack_root(self, instance_root) -> Path:
        if self.unified(instance_root):
            return self.root(instance_root) / PACKS_DIR
        return self.root(instance_root) / "data"

    def resourcepack_root(self, instance_root) -> Path:
        if self.unified(instance_root):
            return self.root(instance_root) / PACKS_DIR
        return self.root(instance_root) / "resources"

    def _options(self, instance_root, filename) -> dict:
        try:
            return json.loads((self.root(instance_root) / filename).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # No file yet (the mod writes it on first launch) or unreadable. Both mean
            # "assume the defaults", which are on — pretending a capability is missing
            # because a config hasn't been generated would be its own wrong answer.
            return {}

    def available_capabilities(self, instance_root) -> tuple:
        if instance_root is None:
            return tuple(self.capabilities)
        if self.unified(instance_root):
            options = self._options(instance_root, OPTIONS_FILE)
            switches = (("load_data_packs", "value"), ("load_resource_packs", "value"))
        else:
            options = self._options(instance_root, LEGACY_OPTIONS_FILE)
            switches = (("dataPacks", "enabled"), ("resourcePacks", "enabled"))
        offered = []
        for (key, field), caps in zip(switches,
                                      ((DATAPACKS_READ, DATAPACKS_WRITE),
                                       (RESOURCEPACKS_READ, RESOURCEPACKS_WRITE))):
            section = options.get(key)
            if not isinstance(section, dict) or section.get(field, True) is not False:
                offered.extend(caps)
        return tuple(offered)

    def _root_for(self, instance_root, kind: str) -> Path:
        return (self.datapack_root(instance_root) if kind == "datapacks"
                else self.resourcepack_root(instance_root))

    def packs(self, instance_root, kind="datapacks") -> list:
        """Alphabetical, because that is genuinely the order the game loads them in for
        resource packs — this is not a display choice.

        In the 21.x layout the folder holds both kinds, so a pack is listed under the kind
        its contents make it, which is the same test the mod applies.
        """
        root = self._root_for(instance_root, kind)
        unified = self.unified(instance_root)
        try:
            children = list(root.iterdir())
        except OSError:
            return []
        found = []
        for child in children:
            if not (child.is_dir() or child.suffix.lower() in PACK_SUFFIXES):
                continue
            if unified and not _carries(child, _CONTENT_DIRS[kind]):
                continue
            found.append(child.name)
        return sorted(found)

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
        (folder / _CONTENT_DIRS[kind]).mkdir(parents=True, exist_ok=True)
        manifest = folder / PACK_MANIFEST
        # In the 21.x layout a resource pack and a datapack of the same name are one
        # folder. Adding the second half must not rewrite the first half's manifest.
        if not manifest.exists():
            manifest.write_text(json.dumps(
                {"pack": {"pack_format": pack_format, "description": description}},
                indent=2), encoding="utf-8")
        return folder


def _carries(pack: Path, directory: str) -> bool:
    """Does this pack — a folder or an archive — have a top-level `directory`?"""
    if pack.is_dir():
        return (pack / directory).is_dir()
    try:
        with zipfile.ZipFile(pack) as archive:
            return any(name.startswith(directory + "/") for name in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
