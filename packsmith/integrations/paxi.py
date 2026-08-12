"""Paxi — a global pack loader (design 8.1).

Verified against a real installation rather than the docs alone; the layout below is what
is actually on disk in the user's packs:

    config/paxi/
        datapacks/                       folders and .zip both work
        datapack_load_order.json         {"loadOrder": ["pack_name", ...]}
        resourcepacks/
        resourcepack_load_order.json

**Paxi's distinguishing capability is load ordering**, which OpenLoader does not have.
That is why `datapacks.ordering` is a capability of its own rather than something folded
into `datapacks.write`: an action that genuinely depends on load order can declare it and
be refused on a pack where it is unavailable, instead of silently producing the wrong
result. Modelling the loaders as interchangeable would make that distinction unsayable.

A pack may be a folder or a zip. Packsmith writes folders — a zip would have to be
rebuilt on every edit, and the whole point of an override is that you can go and look at
it — but both are listed, because the user's packs contain both.
"""
import json
from pathlib import Path

from packsmith.core.capabilities import (
    DATAPACKS_ORDERING, DATAPACKS_READ, DATAPACKS_WRITE, PackLoaderProvider,
    RESOURCEPACKS_ORDERING, RESOURCEPACKS_READ, RESOURCEPACKS_WRITE, CapabilityError)

# Paxi's own file, and it is a real one — 21 bytes of `{"loadOrder": []}` sitting in every
# instance that has the mod. Named per kind, hence the pattern rather than a constant.
LOAD_ORDER_FILE = "{kind}_load_order.json"
PACK_SUFFIXES = (".zip",)


class PaxiProvider(PackLoaderProvider):
    """Paxi, by YungNickYoung."""

    name = "Paxi"
    mod_id = "paxi"
    capabilities = (DATAPACKS_READ, DATAPACKS_WRITE, DATAPACKS_ORDERING,
                    RESOURCEPACKS_READ, RESOURCEPACKS_WRITE, RESOURCEPACKS_ORDERING)

    def root(self, instance_root) -> Path:
        return Path(instance_root) / "config" / "paxi"

    def datapack_root(self, instance_root) -> Path:
        return self.root(instance_root) / "datapacks"

    def resourcepack_root(self, instance_root) -> Path:
        return self.root(instance_root) / "resourcepacks"

    def _root_for(self, instance_root, kind: str) -> Path:
        if kind == "datapacks":
            return self.datapack_root(instance_root)
        if kind == "resourcepacks":
            return self.resourcepack_root(instance_root)
        raise ValueError(f"unknown pack kind: {kind}")

    # --- reading ------------------------------------------------------------

    def packs(self, instance_root, kind="datapacks") -> list:
        """Installed packs, folders and zips alike, in load order where one is set.

        Ordered because Paxi's whole distinguishing feature is that order matters: listing
        them alphabetically would show something the game does not agree with.
        """
        root = self._root_for(instance_root, kind)
        if not root.is_dir():
            return []
        found = sorted(child.name for child in root.iterdir()
                       if child.is_dir() or child.suffix.lower() in PACK_SUFFIXES)
        ordered = [name for name in self.load_order(instance_root, kind) if name in found]
        # Anything not named in the order file loads after, in Paxi's own default order.
        return ordered + [name for name in found if name not in ordered]

    def _order_path(self, instance_root, kind: str) -> Path:
        singular = kind.rstrip("s")
        return self.root(instance_root) / LOAD_ORDER_FILE.format(kind=singular)

    def load_order(self, instance_root, kind="datapacks") -> list:
        path = self._order_path(instance_root, kind)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A missing or malformed order file means "no explicit order", which is what
            # Paxi itself does with it. Refusing to list packs over a broken sidecar would
            # be a worse answer than listing them unordered.
            return []
        order = payload.get("loadOrder")
        return [str(name) for name in order] if isinstance(order, list) else []

    def set_load_order(self, instance_root, order, kind="datapacks") -> None:
        path = self._order_path(instance_root, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Paxi's own formatting: two-space indent, one key. Matching it keeps the file
        # diffable against what the mod writes rather than churning it on every touch.
        path.write_text(json.dumps({"loadOrder": list(order)}, indent=2) + "\n",
                        encoding="utf-8")

    # --- writing ------------------------------------------------------------

    def override_path(self, instance_root, pack_name: str, member: str,
                      kind="datapacks") -> Path:
        """Where a file must land to override a mod's built-in copy (§8.1).

        "Packsmith doesn't do anything clever here — it just writes the file to the right
        place and lets Minecraft's pack layering do the rest." The member path is the one
        the mod uses inside its own jar (`data/<namespace>/…`), reproduced verbatim.
        """
        if not pack_name or "/" in pack_name or "\\" in pack_name:
            raise ValueError(f"invalid pack name: {pack_name!r}")
        clean = str(member).replace("\\", "/").lstrip("/")
        target = (self._root_for(instance_root, kind) / pack_name / clean).resolve()
        root = self._root_for(instance_root, kind).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"override path escapes the pack: {member}")
        return target

    def create_pack(self, instance_root, pack_name: str, kind="datapacks",
                    description="Created by Packsmith", pack_format=15) -> Path:
        """Make an empty pack with the `pack.mcmeta` Minecraft requires (§8.1).

        A pack without one is silently ignored by the game, which is the most confusing
        possible failure: the files are there, the folder is there, and nothing happens.
        """
        if kind == "datapacks":
            folder = self.datapack_root(instance_root) / pack_name
            (folder / "data").mkdir(parents=True, exist_ok=True)
        else:
            folder = self.resourcepack_root(instance_root) / pack_name
            (folder / "assets").mkdir(parents=True, exist_ok=True)
        (folder / "pack.mcmeta").write_text(json.dumps(
            {"pack": {"pack_format": pack_format, "description": description}}, indent=2),
            encoding="utf-8")
        return folder
