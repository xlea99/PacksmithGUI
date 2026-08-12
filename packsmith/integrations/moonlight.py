"""Moonlight Lib — a global *datapack* loader, almost by accident (design 8.1).

Moonlight is MehVahdJukaar's shared library, not a pack loader. Global datapacks are one
config key inside it, which is why nobody thinks of it as a loader — and why it turns up in
packs that never chose it deliberately. It is in the user's own 300-mod pack alongside Paxi.

Layout, verified from a real installation:

    config/moonlight-common.toml
        [general]
        global_datapacks_folder = "moonlight-global-datapacks"   # "" disables it

    <instance>/moonlight-global-datapacks/                       # relative to the INSTANCE

Three things follow, and all three are why this loader is worth having in the set:

1. **Datapacks only.** There is no resource-pack equivalent — not disabled, absent. This is
   §7.1's "provides `datapacks.read`/`write` but no resourcepacks capabilities at all",
   which the capability model predicted before anyone checked.
2. **The path is the user's to change**, so it is read from the config rather than
   hardcoded. A pack dev who renamed the folder is not misconfigured.
3. **It can be switched off** by setting the key to an empty string, so an installed
   Moonlight is not the same thing as an available capability.

No load ordering: Moonlight offers no mechanism for it, so the capability is genuinely
absent rather than unimplemented.
"""
import json
import tomllib
from pathlib import Path

from packsmith.core.capabilities import (
    DATAPACKS_READ, DATAPACKS_WRITE, CapabilityError, PackLoaderProvider)

CONFIG_FILE = "moonlight-common.toml"
FOLDER_KEY = "global_datapacks_folder"
DEFAULT_FOLDER = "moonlight-global-datapacks"
PACK_SUFFIXES = (".zip",)


class MoonlightProvider(PackLoaderProvider):
    """Moonlight Lib, by MehVahdJukaar."""

    name = "Moonlight"
    mod_id = "moonlight"
    mod_id_verified = True          # seen in a real packdump's mod list
    capabilities = (DATAPACKS_READ, DATAPACKS_WRITE)
    summary = ("Datapacks only — no resource packs, no load ordering. The folder is "
               "configurable in moonlight-common.toml and can be switched off there.")

    def folder_name(self, instance_root) -> str:
        """The configured folder, or "" when the user has disabled it."""
        path = Path(instance_root) / "config" / CONFIG_FILE
        try:
            with open(path, "rb") as handle:
                config = tomllib.load(handle)
        except (OSError, ValueError):
            return DEFAULT_FOLDER      # not generated yet; the default is what it will be
        value = config.get("general", {}).get(FOLDER_KEY, DEFAULT_FOLDER)
        return value.strip() if isinstance(value, str) else DEFAULT_FOLDER

    def available_capabilities(self, instance_root) -> tuple:
        if instance_root is None:
            return tuple(self.capabilities)
        return tuple(self.capabilities) if self.folder_name(instance_root) else ()

    def datapack_root(self, instance_root) -> Path:
        folder = self.folder_name(instance_root)
        if not folder:
            raise CapabilityError(
                "Moonlight's global datapack folder is disabled — set "
                f"{FOLDER_KEY} in config/{CONFIG_FILE} to turn it back on")
        return Path(instance_root) / folder

    def resourcepack_root(self, instance_root):
        raise CapabilityError("Moonlight does not load global resource packs")

    def packs(self, instance_root, kind="datapacks") -> list:
        if kind != "datapacks":
            return []
        try:
            root = self.datapack_root(instance_root)
            return sorted(child.name for child in root.iterdir()
                          if child.is_dir() or child.suffix.lower() in PACK_SUFFIXES)
        except (OSError, CapabilityError):
            return []

    def override_path(self, instance_root, pack_name: str, member: str,
                      kind="datapacks") -> Path:
        if kind != "datapacks":
            raise CapabilityError("Moonlight does not load global resource packs")
        if not pack_name or "/" in pack_name or "\\" in pack_name:
            raise ValueError(f"invalid pack name: {pack_name!r}")
        clean = str(member).replace("\\", "/").lstrip("/")
        root = self.datapack_root(instance_root).resolve()
        target = (self.datapack_root(instance_root) / pack_name / clean).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"override path escapes the pack: {member}")
        return target

    def create_pack(self, instance_root, pack_name: str, kind="datapacks",
                    description="Created by Packsmith", pack_format=15) -> Path:
        if kind != "datapacks":
            raise CapabilityError("Moonlight does not load global resource packs")
        folder = self.datapack_root(instance_root) / pack_name
        (folder / "data").mkdir(parents=True, exist_ok=True)
        (folder / "pack.mcmeta").write_text(json.dumps(
            {"pack": {"pack_format": pack_format, "description": description}}, indent=2),
            encoding="utf-8")
        return folder
