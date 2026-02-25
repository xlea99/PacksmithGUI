import json
from pathlib import Path
from datetime import datetime, timezone
from packsmith.common.setup import GLOBAL_PATHS
from packsmith.common.logging import log
from packsmith.util.misc import raise_log

_VALID_SCHEMAS = {1}

# This file handles reading/accessing values from a registry dump.
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
                result._registries[entry["type"]] = {
                    **entry,
                    "values": reg_data["values"],
                }

            # Now we load each attribute individually since attributes aren't discovered
            result._load_localizations(snapshot_path / "attributes" / "localization.json")

        except (KeyError, TypeError, json.JSONDecodeError, FileNotFoundError) as e:
            raise_log(ValueError, f"Packsmith dump appears to be corrupted: {e}")

        return result
    # Various helper loaders for attributes
    def _load_localizations(self,locals_path: Path):
        with open(locals_path,"r") as f:
            locals_dict = json.load(f)
        self._localizations[locals_dict['locale']] = locals_dict["values"]

    # Saves this Packdump





r = Packdump.load(GLOBAL_PATHS.mc_root / "packsmith")



