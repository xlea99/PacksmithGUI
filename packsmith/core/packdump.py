import json
from pathlib import Path
from datetime import datetime, timezone
#from types import MappingProxyType
from packsmith.common.setup import GLOBAL_PATHS
from packsmith.common.logging import log
from packsmith.util.misc import raise_log

_VALID_SCHEMAS = {1}

# lil json writing helper
def _write_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# This file handles reading/accessing values from a packdump. Immutable by design (and by gentleman's agreement)...
# if you're trying to edit a packdump you're doing something deeply wrong.
class Packdump:

    def __init__(self):
        # Meta info
        self._schema = None
        self._timestamp = None
        self._mc_version = None
        self._loader = None
        self._loader_version = None
        self._mods = {}

        # Registries dict
        self._registries = {}

        # Various attributes
        self._localizations = {}

        # Toggled locale
        self._active_locale = None

    # For true equality, we compare many aspects of the dump to each other.
    def __eq__(self, other):
        if type(other) is not type(self):
            return NotImplemented

        if self._mc_version != other._mc_version:
            return False
        if self._loader != other._loader:
            return False
        if self._loader_version != other._loader_version:
            return False

        # Check each mod specifically
        if len(self.mods) != len(other.mods):
            return False
        for mod in self.mods.values():
            other_mod = other.mods.get(mod['mod_id'],None)
            if not other_mod:
                return False
            if mod['name'] != other_mod['name']:
                return False
            if mod['version'] != other_mod['version']:
                return False

        # Check for each registry key, and that each key has equivalent number of items
        if set(self._registries.keys()) != set(other._registries.keys()):
            return False
        for reg_type in self._registries:
            if self._registries[reg_type]["count"] != other._registries[reg_type]["count"]:
                return False

        return True
    # For comparing GT/LT for packdumps, we simply use its timestamp and assume they're already different
    def __gt__(self, other):
        return self._timestamp > other._timestamp
    def __ge__(self, other):
        return self._timestamp >= other._timestamp
    def __lt__(self, other):
        return self._timestamp < other._timestamp
    def __le__(self, other):
        return self._timestamp <= other._timestamp

    # Given a full packsmith snapshot, this loads the full registry dump.
    @classmethod
    def load(cls, snapshot_path: Path):
        # Structural checks that hopefully guide user toward a fix
        if not snapshot_path.exists():
            raise_log(FileNotFoundError, f"Packsmith snapshot not found: '{snapshot_path}'")
        if not (snapshot_path / "meta.json").exists():
            raise_log(ValueError, f"Snapshot is missing meta.json: '{snapshot_path}'")
        if not (snapshot_path / "registries").is_dir():
            raise_log(ValueError, f"Snapshot is missing registries/: '{snapshot_path}'")
        if not (snapshot_path / "attributes").is_dir():
            raise_log(ValueError, f"Snapshot is missing attributes/: '{snapshot_path}'")

        with open(snapshot_path / "meta.json", "r") as f:
            meta = json.load(f)

        # Worht explicitly checking types and schema
        if meta.get("type") != "packsmith_full_dump":
            raise_log(ValueError, f"meta.json type is '{meta.get('type')}', expected 'packsmith_full_dump'")
        schema = meta.get("schema_version")
        if schema not in _VALID_SCHEMAS:
            raise_log(ValueError, f"Unsupported schema version: {schema}")

        # If the mod produced garbage (or the user decided to garbagify mod output) we yell loudly
        result = cls()
        try:
            result._schema = schema

            # Best-effort timestamp, but not worth crashing over
            try:
                result._timestamp = datetime.fromisoformat(meta["generated_at_utc"])
            except (KeyError, ValueError):
                log.warning("Could not parse timestamp from meta.json, defaulting to now")
                result._timestamp = datetime.now(timezone.utc)

            result._mc_version = meta["minecraft_version"]
            result._loader = meta["loader"]
            result._loader_version = meta["loader_version"]
            result._mods = {mod["mod_id"]: mod for mod in meta["mods"]}

            # Load registries via manifest
            for entry in meta["registries"]:
                reg_path = snapshot_path / "registries" / entry["file"]
                with open(reg_path, "r") as f:
                    reg_data = json.load(f)
                result._registries[entry["type"]] = {**entry,"values": reg_data["values"]}

            # Now we load each attribute individually since attributes aren't discovered
            result._load_localizations(snapshot_path / "attributes" / "localization.json")

        except (KeyError, TypeError, json.JSONDecodeError, FileNotFoundError) as e:
            raise_log(ValueError, f"Packsmith dump appears to be corrupted: {e}")

        log.info(f"Successfully loaded packdump from {snapshot_path}")
        return result
    # Various helper loaders for attributes
    def _load_localizations(self,locals_path: Path):
        with open(locals_path, "r") as f:
            locals_dict = json.load(f)
        locale = locals_dict['locale']
        self._localizations[locale] = locals_dict["values"]
        if self._active_locale is None:
            self._active_locale = locale

    # Saves a snapshot of this Packdump as is to the given path.
    def save(self, snapshot_path: Path):
        reg_dir = snapshot_path / "registries"
        attr_dir = snapshot_path / "attributes"
        reg_dir.mkdir(parents=True, exist_ok=True)
        attr_dir.mkdir(parents=True, exist_ok=True)

        # Build registry manifest and write individual registry files
        registries_manifest = []
        for reg_type, reg_data in self._registries.items():
            filename = reg_type.replace(":", "_").replace("/", "_") + ".json"
            reg_file = {
                "schema_version": self._schema,
                "type": reg_type,
                "values": reg_data["values"],
            }
            _write_json(reg_dir / filename, reg_file)
            registries_manifest.append({
                "type": reg_type,
                "file": filename,
                "count": len(reg_data["values"]),
            })

        # Write meta.json
        meta = {
            "type": "packsmith_full_dump",
            "schema_version": self._schema,
            "created_by": "packsmith_gui",
            "generated_at_utc": self._timestamp.isoformat(),
            "minecraft_version": self._mc_version,
            "loader": self._loader,
            "loader_version": self._loader_version,
            "mod_count": len(self._mods),
            "mods": sorted(self._mods.values(), key=lambda m: m["mod_id"]),
            "registries": registries_manifest,
        }
        _write_json(snapshot_path / "meta.json", meta)

        # Write attribute files
        for locale, values in self._localizations.items():
            loc_file = {
                "schema_version": self._schema,
                "type": "localization",
                "locale": locale,
                "values": values,
            }
            _write_json(attr_dir / "localization.json", loc_file)

        log.info(f"Packdump saved to {snapshot_path}")

    # Immutable getters
    @property
    def schema(self) -> int:
        return self._schema
    @property
    def timestamp(self) -> datetime:
        return self._timestamp
    @property
    def mc_version(self) -> str:
        return self._mc_version
    @property
    def loader(self) -> str:
        return self._loader
    @property
    def loader_version(self) -> str:
        return self._loader_version

    # Read only views of the dicts cause we aint about dirty editing
    #@property
    #def mods(self) -> MappingProxyType:
    #    return MappingProxyType(self._mods)
    #@property
    #def registry(self) -> MappingProxyType:
    #    return MappingProxyType(self._registries)
    # (temp disabling it for ease of debug)
    @property
    def mods(self) -> dict:
        return self._mods
    @property
    def registry(self) -> dict:
        return self._registries

    # Convenience localizations getter
    @property
    def localization(self) -> dict:
        return self._localizations[self._active_locale]

    # Handles getting and setting the active locale that localizations is serving from.
    @property
    def locale(self):
        return self._active_locale
    @locale.setter
    def locale(self,locale_str: str):
        if locale_str not in self._localizations:
            raise_log(KeyError,f"Tried to set the active locale to '{locale_str}', but this locale is not present in the localizations dictionary!")
        self._active_locale = locale_str



r = Packdump.load(GLOBAL_PATHS.mc_root / "packsmith")
r2 = Packdump.load(GLOBAL_PATHS.mc_root / "packsmith")



