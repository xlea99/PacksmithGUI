import json
from pathlib import Path
from datetime import datetime, timezone
from packsmith.common.setup import GLOBAL_PATHS
from packsmith.common.logging import log
from packsmith.util.misc import raise_log

_VALID_SCHEMAS = {1}
_VALID_LOADERS = {"forge","fabric","quilt","neoforge"}

# This file handles reading/accessing values from a registry dump.
class McRegistry:

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

    # Given a full packsmith snapshot, this loads the full registry dump.
    @classmethod
    def load(cls, snapshot_path: Path):
        if not snapshot_path.exists():
            raise_log(FileNotFoundError,f"Supplied packsmith snapshot could not be found: '{snapshot_path}'")
        if not (snapshot_path / "meta.json").exists():
            raise_log(ValueError,f"Supplied packsmith snapshot is missing meta.json: '{snapshot_path}'")
        if not (snapshot_path / "registries").exists():
            raise_log(ValueError,f"Supplied packsmith snapshot is missing registries subdir: '{snapshot_path}'")
        if not (snapshot_path / "attributes").exists():
            raise_log(ValueError,f"Supplied packsmith snapshot is missing attributes subdir: '{snapshot_path}'")

        # Open and parse meta.json
        with open(snapshot_path / "meta.json","r") as f:
            meta = json.load(f)
        meta_type = meta.get("type","")
        if not meta_type == "packsmith_full_dump":
            raise_log(ValueError,f"Supplied packsmith snapshot contains a meta.json, but the type does not seem to be a packsmith dump, as it's listed as '{meta_type}'. Full path: '{snapshot_path}'")

        result = cls()

        # Handle schema
        schema = meta.get("schema_version",None)
        if schema not in _VALID_SCHEMAS:
            raise_log(ValueError,f"Invalid schema listed in meta.json: '{schema}'")
        result._schema = schema

        # Convert timestamp to a datetime object, WARN on failure not crash although idk if this is best practice or no
        timestamp = meta.get("timestamp","")
        try:
            timestamp_dt = datetime.fromisoformat(timestamp)
            result._timestamp = timestamp_dt
        except ValueError:
            #TODO warn for now, or should this be a hard crash???
            log.warning(f"Invalid ISO timestamp in meta.json, treating the dump's timespan as now. Invalid timestamp string: '{timestamp}'")
            result._timestamp = datetime.now(timezone.utc)

        # Get the loader
        loader = meta.get("loader",None)
        if loader not in _VALID_LOADERS:
            raise_log(ValueError,f"Invalid loader listed in meta.json: '{loader}'")
        result._loader = loader

        # Treat MC version and loader version as mandatory, but don't lint specifically for version string.
        try:
            result._mc_version = meta["minecraft_version"]
            result._loader_version = meta["loader_version"]
        except KeyError:
            raise_log(ValueError,f"Either minecraft_version or loader_version are missing from meta.json")

        # Read all mods listed as loaded in meta.json
        if "mods" not in meta:
            raise_log(ValueError,f"Mods list seems to be missing in meta.json!")
        for mod_entry in meta["mods"]:
            if "mod_id" not in mod_entry or "name" not in mod_entry or "version" not in mod_entry:
                raise_log(ValueError,f"Mod entry seems to be deformed: '{mod_entry}'")
            else:
                result._mods[mod_entry['mod_id']] = mod_entry

        # Read registry manifest as listed in meta.json
        if "registries" not in meta:
            raise_log(ValueError,f"Registries manifest seems to be missing in meta.json!")
        for registry_entry in meta["registries"]:
            if "type" not in registry_entry or "file" not in registry_entry or "count" not in registry_entry:
                raise_log(ValueError,f"Registry entry seems to be deformed: '{registry_entry}'")
            else:
                this_registry_path = snapshot_path / "registries" / registry_entry["file"]
                if not this_registry_path.exists():
                    raise_log(FileNotFoundError,f"Specified registry file '{this_registry_path}' does not seem to exist!!!")
                with open(this_registry_path,"r") as f:
                    this_registry_data = json.load(f)
                # Make sure some weird bs hasn't happened between registry types in manifest vs actual file
                if this_registry_data.get("type") != registry_entry["type"]:
                    raise_log(ValueError,f"Registry entry of type '{registry_entry['type']}' in the manifest does not match its pointed file '{registry_entry['file']}', which has registry type '{this_registry_data['type']}'. Full registry_entry: {registry_entry}")
                # Make sure schema matches
                if this_registry_data.get("schema_version") != schema:
                    raise_log(ValueError,f"Meta.json's schema '{schema}' does not match registry file '{registry_entry['file']}'s schema of '{this_registry_data['schema_version']}'. Full registry_entry: {registry_entry}")
                # Make sure it actually has a list of values
                if "values" not in this_registry_data:
                    raise_log(ValueError,f"Values list seems to be missing from registry file path '{this_registry_path}'")

                # Finally, load the registry into this object.
                result._registries[registry_entry['type']] = registry_entry
                result._registries[registry_entry['type']]["values"] = this_registry_data["values"]

        return result



r = McRegistry.load(GLOBAL_PATHS.mc_root / "packsmith")
