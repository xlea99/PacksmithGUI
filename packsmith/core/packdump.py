import json
import shutil
from pathlib import Path
from datetime import datetime, timezone
#from types import MappingProxyType
from packsmith.common.logging import log
from packsmith.util.misc import raise_log, write_json
from packsmith.core.profile import Profile

_VALID_SCHEMAS = {1}

# This class handles reading/accessing values from a packdump. Immutable by design (and by gentleman's agreement)
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

    #region === Comparison ===

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

    # This method generates a dict of inconsistencies between two packdumps.
    def compare(self,other):
        diffs = {}

        # Simple scalarish values we just return both
        if self._mc_version != other._mc_version:
            diffs["mc_version"] = (self._mc_version,other._mc_version)
        if self._loader != other._loader:
            diffs["loader"] = (self._loader,other._loader)
        if self._loader_version != other._loader_version:
            diffs["loader_version"] = (self._loader_version,other._loader_version)

        # Diff mods
        self_mod_ids = set(self.mods.keys())
        other_mod_ids = set(other.mods.keys())
        mods_only_in_self = self_mod_ids - other_mod_ids
        mods_only_in_other = other_mod_ids - self_mod_ids
        mods_common = self_mod_ids & other_mod_ids
        mods_changed = {}
        for common_mod in mods_common:
            this_diff = {}
            if self.mods[common_mod]["version"] != other.mods[common_mod]["version"]:
                this_diff["version"] = (self.mods[common_mod]["version"], other.mods[common_mod]["version"])
            if self.mods[common_mod]["name"] != other.mods[common_mod]["name"]:
                this_diff["name"] = (self.mods[common_mod]["name"], other.mods[common_mod]["name"])
            if this_diff:
                mods_changed[common_mod] = this_diff
        if mods_only_in_self:
            (diffs.setdefault("mods", {}))["only_in_self"] = list(mods_only_in_self)
        if mods_only_in_other:
            (diffs.setdefault("mods", {}))["only_in_other"] = list(mods_only_in_other)
        if mods_changed:
            (diffs.setdefault("mods", {}))["changed"] = mods_changed

        # Diff registries
        registry_diffs = {}
        self_registry_types = set(self.registry.keys())
        other_registry_types = set(other.registry.keys())
        registry_types_only_in_self = self_registry_types - other_registry_types
        registry_types_only_in_other = other_registry_types - self_registry_types
        if registry_types_only_in_self:
            registry_diffs["only_in_self"] = list(registry_types_only_in_self)
        if registry_types_only_in_other:
            registry_diffs["only_in_other"] = list(registry_types_only_in_other)
        registry_types_common = self_registry_types & other_registry_types
        reg_changes = {}
        for common_reg_type in registry_types_common:
            this_reg_type_change = {}
            self_this_reg_type_entries = set(self.registry[common_reg_type]["values"])
            other_this_reg_type_entries = set(other.registry[common_reg_type]["values"])
            this_reg_type_only_in_self = self_this_reg_type_entries - other_this_reg_type_entries
            this_reg_type_only_in_other = other_this_reg_type_entries - self_this_reg_type_entries
            if this_reg_type_only_in_self:
                this_reg_type_change["only_in_self"] = list(this_reg_type_only_in_self)
            if this_reg_type_only_in_other:
                this_reg_type_change["only_in_other"] = list(this_reg_type_only_in_other)
            if this_reg_type_change:
                reg_changes[common_reg_type] = this_reg_type_change
        if reg_changes:
            registry_diffs["changed"] = reg_changes
        if registry_diffs:
            diffs["registries"] = registry_diffs

        # Diff localizations. Per locale, per registry type, per entry
        loc_diffs = {}
        self_locales = set(self._localizations.keys())
        other_locales = set(other._localizations.keys())
        if self_locales - other_locales:
            loc_diffs["only_in_self"] = list(self_locales - other_locales)
        if other_locales - self_locales:
            loc_diffs["only_in_other"] = list(other_locales - self_locales)
        loc_changes = {}
        for locale in self_locales & other_locales:
            locale_diff = {}
            self_reg_types = set(self._localizations[locale].keys())
            other_reg_types = set(other._localizations[locale].keys())
            if self_reg_types - other_reg_types:
                locale_diff["only_in_self"] = list(self_reg_types - other_reg_types)
            if other_reg_types - self_reg_types:
                locale_diff["only_in_other"] = list(other_reg_types - self_reg_types)
            reg_type_changes = {}
            for reg_type in self_reg_types & other_reg_types:
                entry_diff = {}
                self_entries = self._localizations[locale][reg_type]
                other_entries = other._localizations[locale][reg_type]
                self_ids = set(self_entries.keys())
                other_ids = set(other_entries.keys())
                if self_ids - other_ids:
                    entry_diff["only_in_self"] = list(self_ids - other_ids)
                if other_ids - self_ids:
                    entry_diff["only_in_other"] = list(other_ids - self_ids)
                changed_names = {eid: (self_entries[eid], other_entries[eid])
                                 for eid in self_ids & other_ids
                                 if self_entries[eid] != other_entries[eid]}
                if changed_names:
                    entry_diff["changed"] = changed_names
                if entry_diff:
                    reg_type_changes[reg_type] = entry_diff
            if reg_type_changes:
                locale_diff["changed"] = reg_type_changes
            if locale_diff:
                loc_changes[locale] = locale_diff
        if loc_changes:
            loc_diffs["changed"] = loc_changes
        if loc_diffs:
            diffs["localizations"] = loc_diffs

        return diffs

    #endregion === Comparison ===

    #region === Serializing ===

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
            write_json(reg_dir / filename, reg_file)
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
        write_json(snapshot_path / "meta.json", meta)

        # Write attribute files
        for locale, values in self._localizations.items():
            loc_file = {
                "schema_version": self._schema,
                "type": "localization",
                "locale": locale,
                "values": values,
            }
            write_json(attr_dir / "localization.json", loc_file)

        log.info(f"Packdump saved to {snapshot_path}")

    #endregion === Serializing ===

    #region === Getters and Setters ===

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

    #endregion === Getters and Setters ===

    #region === Helpers ===

    # Reads one L1 attribute of an entry (design 3.1). Attributes are dump-provided,
    # per-entry data attached to (registry_type, entry_id) — today 'localization' is the
    # first and only one. Returns None when the attribute is absent: the raw-id fallback is
    # a renderer convenience, NOT the raw attribute, so callers that want a display string
    # do `attribute(...) or entry_id` themselves.
    def attribute(self, registry_type: str, entry_id: str, name: str):
        if name != "localization":
            return None
        if registry_type in self.localization:
            return self.localization[registry_type].get(entry_id)
        return None

    #endregion === Helpers

# This helper method imports a packdump from the minecraft instance (or a given path) as the local, current
# packdump snapshot, rotating out previous dumps as specified by the user's `packdump_snapshot_count` in main.toml.
# This is all skipped if the incoming dump is identical to the current dump
def import_packdump(profile: Profile, source_path: Path = None):
    source_path = source_path or (profile.mc_path / "packsmith")
    incoming = Packdump.load(source_path)

    latest_dir = profile.packdumps_dir / "latest"
    history_dir = profile.packdumps_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    # If we already have a latest, check if it's the same dump
    if (latest_dir / "meta.json").exists():
        current = Packdump.load(latest_dir)
        if current == incoming:
            log.info("Incoming packdump is identical to latest, skipping import")
            return current

        # This means its a new dump! Archive the current latest before replacing
        archive_name = current.timestamp.strftime("%Y-%m-%d_%H-%M-%S")
        archive_path = history_dir / archive_name
        if archive_path.exists():
            shutil.rmtree(archive_path)
        shutil.move(str(latest_dir), str(archive_path))
        log.info(f"Archived previous packdump to {archive_path}")

        # Prune oldest snapshots
        max_snapshots = profile.settings.get("max_packdump_snapshot_count", 10)
        snapshots = sorted(history_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in snapshots[max_snapshots:]:
            shutil.rmtree(old)
            log.info(f"Pruned old packdump snapshot: {old.name}")

    # Save the incoming dump as latest
    latest_dir.mkdir(parents=True, exist_ok=True)
    incoming.save(latest_dir)
    log.info(f"Imported new packdump as latest")
    return incoming

# Simply returns a list of snapshot summaries from history, sorted newest first. Each summary contains
# a timestamp, mc_version, loader, loader_version, mod_count, and path.
def list_snapshots(profile: Profile) -> list[dict]:
    history_dir = profile.packdumps_dir / "history"
    if not history_dir.is_dir():
        return []

    summaries = []
    for folder in history_dir.iterdir():
        meta_path = folder / "meta.json"
        if not meta_path.exists():
            continue
        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)
            summaries.append({
                "path": folder,
                "timestamp": meta.get("generated_at_utc", ""),
                "mc_version": meta.get("minecraft_version", ""),
                "loader": meta.get("loader", ""),
                "loader_version": meta.get("loader_version", ""),
                "mod_count": meta.get("mod_count", 0),
            })
        except (json.JSONDecodeError, OSError):
            log.warning(f"Skipping corrupt snapshot: {folder.name}")
    summaries.sort(key=lambda s: s["timestamp"], reverse=True)
    return summaries


