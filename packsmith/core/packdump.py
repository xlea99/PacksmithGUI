import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from types import MappingProxyType
from packsmith.common.logging import log
from packsmith.util.misc import raise_log, write_json
from packsmith.core.profile import Profile

# Dump schema versions this build can READ. Old ones are never dropped: a snapshot on disk
# is an archive, not a cache — you cannot go back and re-dump a pack as it was six months
# ago, so a bump that orphaned existing snapshots would be destroying the one copy of
# something. New shapes are therefore additive (a block beside the old one), which lets an
# older reader ignore what it doesn't know and a newer one tell absent from empty.
#
#   1 — meta, registries, localization display names.
#   2 — localization gained `keys`: the translation key behind each display name.
#
# Mirrored in the mod as `DumpSchema.VERSION`; bump both together.
_VALID_SCHEMAS = {1, 2, 3}
_CURRENT_SCHEMA = max(_VALID_SCHEMAS)


class DumpTooNewError(ValueError):
    """The dump came from a newer Packsmith than this one.

    Distinct from an unreadable dump because the fix is different and the user can act on
    it: a dump from the future is not corrupt, it just describes a shape this build has
    never heard of, and guessing at it is how you silently drop the half you don't
    understand. Mirrors `SchemaTooNewError` for profile.db, for the same reason.
    """

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
        # Which block an item places, and what shape that block is — schema 3, and not
        # derivable from the registries: an item's block can carry a different id, and a
        # block's form is a fact about its Java class rather than about its name.
        # {registry_type: {entry_id: value}}, empty for older snapshots.
        self._places_block = {}
        self._forms = {}
        self._block_classes = {}

        # The translation key behind each display name: {registry_type: {entry_id: key}}.
        # NOT nested under locale, unlike the names above — `block.spawn.anthill` is the
        # same key whether you are reading English or German. Empty for schema-1 snapshots,
        # which predate the mod harvesting it.
        self._localization_keys = {}

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

        # Check each registry key, and that each holds the same ENTRIES — not merely the
        # same number of them. Counts alone let a config change that swaps one item for
        # another read as "no change", and equality here is what decides whether a new
        # dump gets imported at all; a false "identical" is exactly the silent staleness
        # the import design is built to prevent. Comparing id sets is cheap even at
        # 300-mod scale.
        if set(self._registries.keys()) != set(other._registries.keys()):
            return False
        for reg_type in self._registries:
            if (set(self._registries[reg_type]["values"])
                    != set(other._registries[reg_type]["values"])):
                return False

        # Localizations are L1 data too, and they are what the user actually READS: the
        # table shows display names, not ids. Leaving them out meant a mod that renamed
        # "Rose Quartz Block" to "Rose Quartz" was "identical" — no import, and the old
        # name persisted in the GUI for the life of the profile with no way to refresh
        # short of deleting the packdump. Staleness the user cannot see a reason for is
        # worse than an extra snapshot.
        if self._localizations != other._localizations:
            return False

        # Keys too, and for a sharper reason than names. A mod update that moves an item's
        # descriptionId changes no id, no count and often no display name — but it breaks
        # every rename pointed at the old key. Left out of equality, that dump reads as
        # "identical", import is skipped, and Packsmith keeps handing actions a key the game
        # no longer answers to.
        if self._localization_keys != other._localization_keys:
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

        # Diff translation keys. Reported separately from names because they mean different
        # things to the reader: a changed *name* is cosmetic, a changed *key* silently
        # invalidates anything written against the old one.
        key_diffs = {}
        self_reg_types = set(self._localization_keys)
        other_reg_types = set(other._localization_keys)
        if self_reg_types - other_reg_types:
            key_diffs["only_in_self"] = list(self_reg_types - other_reg_types)
        if other_reg_types - self_reg_types:
            key_diffs["only_in_other"] = list(other_reg_types - self_reg_types)
        key_changes = {}
        for reg_type in self_reg_types & other_reg_types:
            self_entries = self._localization_keys[reg_type]
            other_entries = other._localization_keys[reg_type]
            entry_diff = {}
            self_ids, other_ids = set(self_entries), set(other_entries)
            if self_ids - other_ids:
                entry_diff["only_in_self"] = list(self_ids - other_ids)
            if other_ids - self_ids:
                entry_diff["only_in_other"] = list(other_ids - self_ids)
            changed = {eid: (self_entries[eid], other_entries[eid])
                       for eid in self_ids & other_ids
                       if self_entries[eid] != other_entries[eid]}
            if changed:
                entry_diff["changed"] = changed
            if entry_diff:
                key_changes[reg_type] = entry_diff
        if key_changes:
            key_diffs["changed"] = key_changes
        if key_diffs:
            diffs["localization_keys"] = key_diffs

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
        if isinstance(schema, int) and schema > _CURRENT_SCHEMA:
            raise_log(DumpTooNewError,
                      f"This packdump is schema {schema}, but this build of Packsmith reads "
                      f"up to {_CURRENT_SCHEMA}. Update Packsmith to open it — importing it "
                      f"anyway would silently drop whatever the newer dump added.")
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

            # Now we load each attribute individually since attributes aren't discovered.
            # Locales ARE discovered, by glob: a snapshot holds one file per locale, and
            # which locales a pack dumped is not something the reader can know in advance.
            # `localization.json` (no locale in the name) is the old single-locale layout —
            # still read, because snapshots already on disk are archives, not caches.
            attr_dir_in = snapshot_path / "attributes"
            locale_files = sorted(attr_dir_in.glob("localization.*.json"))
            legacy = attr_dir_in / "localization.json"
            if legacy.is_file():
                locale_files.insert(0, legacy)
            if not locale_files:
                raise FileNotFoundError(f"no localization files in {attr_dir_in}")
            for path in locale_files:
                result._load_localizations(path)

            # Optional, and by presence rather than by version: a snapshot from before
            # schema 3 simply has no such file, and answers None for what it never held —
            # the same shrug it gives for an entry a registry never had.
            block_items = attr_dir_in / "block_items.json"
            if block_items.is_file():
                result._load_block_items(block_items)
            # Which locale is *active* must not depend on filename sort order, or adding a
            # German dump would silently re-language the whole GUI.
            if "en_us" in result._localizations:
                result._active_locale = "en_us"

        except (KeyError, TypeError, json.JSONDecodeError, FileNotFoundError) as e:
            raise_log(ValueError, f"Packsmith dump appears to be corrupted: {e}")

        log.info(f"Successfully loaded packdump from {snapshot_path}")
        return result
    # Various helper loaders for attributes
    def _load_block_items(self, path: Path):
        """`attributes/block_items.json` — what the game knows and a reader cannot guess.

        Each map is read independently. A half-written file that has `places` but no `forms`
        should still give up the half it has, for the same reason `keys` is feature-detected
        above: the version says what a reader may expect, not what a file actually contains.
        """
        with open(path, "r") as f:
            data = json.load(f)
        self._places_block = data.get("places") or {}
        self._forms = data.get("forms") or {}
        self._block_classes = data.get("classes") or {}

    def _load_localizations(self,locals_path: Path):
        with open(locals_path, "r") as f:
            locals_dict = json.load(f)
        locale = locals_dict['locale']
        self._localizations[locale] = locals_dict["values"]
        # Feature-detected rather than gated on the file's version number. The version says
        # what a reader may expect; what a *file* actually contains is a separate question,
        # and a dump whose meta and attribute files disagree about their version (easy
        # enough to produce by hand, or by a half-finished mod build) should still load
        # everything it genuinely has.
        keys = locals_dict.get("keys")
        if keys:
            # One key map per dump, not per locale. Later locales re-state the same keys, so
            # they merge rather than fight; if they ever disagreed the last would win, and
            # that is the correct shrug for a field that cannot legitimately vary.
            for reg_type, entries in keys.items():
                self._localization_keys.setdefault(reg_type, {}).update(entries)
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

        # Write attribute files. One file PER LOCALE: this used to write every locale to
        # the same localization.json, so the last one out of the dict won and every other
        # locale was silently dropped from the snapshot. Latent while the mod dumps only
        # en_us — but archived snapshots are the thing you cannot go back and re-dump, so
        # a silent loss there is permanent.
        for locale, values in self._localizations.items():
            payload = {
                "schema_version": self._schema,
                "type": "localization",
                "locale": locale,
                "values": values,
            }
            # Only written when this snapshot actually has them. Round-tripping a schema-1
            # dump must produce a schema-1 dump: writing an empty `keys` block would claim
            # the mod harvested nothing, when the truth is it was never asked.
            if self._localization_keys:
                payload["keys"] = self._localization_keys
            write_json(attr_dir / f"localization.{locale}.json", payload)

        # Same rule as `keys`: written only when this snapshot actually carries them, so
        # round-tripping an older dump produces an older dump rather than one claiming the
        # mod found no block items at all.
        if self._places_block or self._forms or self._block_classes:
            write_json(attr_dir / "block_items.json", {
                "schema_version": self._schema,
                "type": "block_items",
                "places": self._places_block,
                "forms": self._forms,
                "classes": self._block_classes,
            })

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

    # Read only views of the dicts cause we aint about dirty editing.
    #
    # These were commented out "temp… for ease of debug" and stayed that way, which made
    # L1's central promise — "read-only, Packsmith never writes it" (§3.1) — a convention
    # rather than a property. Starlark was insulated because the bridge hands actions
    # copies; every Python caller in the GUI was on the honour system, and an accidental
    # `dump.registry[...]["values"].append(...)` would have corrupted the in-memory L1 for
    # the rest of the session with nothing on disk to explain it.
    #
    # MappingProxyType is shallow: it stops rebinding keys of the outer dict, not mutation
    # of the inner ones. That's the cheap 90% — it catches the accidents — and paying for
    # deep immutability by copying an 18k-entry registry on every access is not worth it.
    @property
    def mods(self) -> MappingProxyType:
        return MappingProxyType(self._mods)
    @property
    def registry(self) -> MappingProxyType:
        return MappingProxyType(self._registries)

    # Convenience localizations getter
    @property
    def localization(self) -> MappingProxyType:
        return MappingProxyType(self._localizations[self._active_locale])

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
    # per-entry data attached to (registry_type, entry_id). Returns None when the attribute
    # is absent: the raw-id fallback is a renderer convenience, NOT the raw attribute, so
    # callers that want a display string do `attribute(...) or entry_id` themselves.
    #
    # Dispatched through a table rather than an `if name != "localization"` chain. That
    # single line was the only thing between actions and every attribute we might ever
    # harvest — the Starlark binding, the query catalog and the AST node were all already
    # generic — so adding the second attribute meant unpicking a special case rather than
    # extending anything.
    def attribute(self, registry_type: str, entry_id: str, name: str):
        entry = self._ATTRIBUTES.get(name)
        if entry is None:
            return None
        return entry[0](self, registry_type, entry_id)

    def _read_localization(self, registry_type: str, entry_id: str):
        if registry_type in self.localization:
            return self.localization[registry_type].get(entry_id)
        return None

    def _has_localization(self, registry_type: str) -> bool:
        return registry_type in self.localization

    def _read_localization_key(self, registry_type: str, entry_id: str):
        return self._localization_keys.get(registry_type, {}).get(entry_id)

    def _has_localization_key(self, registry_type: str) -> bool:
        return bool(self._localization_keys.get(registry_type))

    def _read_places_block(self, registry_type: str, entry_id: str):
        return self._places_block.get(registry_type, {}).get(entry_id)

    def _has_places_block(self, registry_type: str) -> bool:
        return bool(self._places_block.get(registry_type))

    def _read_form(self, registry_type: str, entry_id: str):
        return self._forms.get(registry_type, {}).get(entry_id)

    def _has_form(self, registry_type: str) -> bool:
        return bool(self._forms.get(registry_type))

    def _read_block_class(self, registry_type: str, entry_id: str):
        return self._block_classes.get(registry_type, {}).get(entry_id)

    def _has_block_class(self, registry_type: str) -> bool:
        return bool(self._block_classes.get(registry_type))

    def is_block_item(self, entry_id: str) -> bool:
        """Whether this `minecraft:item` places a block.

        A real answer from the game rather than an inference. The two guesses available from
        the registries alone — id present in `minecraft:block`, or a `block.` translation key
        — disagree with each other on 114 items of a 300-mod pack, and each is wrong in the
        direction the other is right. `False` for a snapshot older than schema 3, which is
        indistinguishable from "not a block item" and is why callers that care should ask
        `knows_block_items()` first.
        """
        return entry_id in self._places_block.get("minecraft:item", {})

    def knows_block_items(self) -> bool:
        """Whether this snapshot was dumped by a mod that recorded any of this at all."""
        return bool(self._places_block)

    # name -> (read it for one entry, does this registry carry it at all). The two live
    # together because they are the same fact asked at different scales, and a reader
    # without a prober is how a column ends up offered for a registry that has no values
    # for it. Order is the order columns appear in.
    _ATTRIBUTES = {
        "localization": (_read_localization, _has_localization),
        "localization_key": (_read_localization_key, _has_localization_key),
        "places_block": (_read_places_block, _has_places_block),
        "form": (_read_form, _has_form),
        "block_class": (_read_block_class, _has_block_class),
    }

    @classmethod
    def attribute_names(cls) -> list:
        """Every attribute name `attribute()` can answer for — what the view constructor
        offers as columns. A class method because it is a property of this build, not of
        one snapshot: an older dump simply answers None for what it doesn't carry, which is
        the same thing it does for an entry a registry never had."""
        return list(cls._ATTRIBUTES)

    def attributes_for(self, registry_type: str) -> list:
        """The attributes **this registry actually carries**, in `attribute_names()` order.

        Different question from `attribute_names()`, and the difference is most of the
        table: a pack dumps localization for a handful of registries out of ~135, so
        offering every known attribute everywhere would give `minecraft:block_predicate_type`
        two permanently empty columns. An empty column is not neutral — it reads as data
        that failed to load rather than data that was never going to exist.
        """
        return [name for name, (_, has) in self._ATTRIBUTES.items()
                if has(self, registry_type)]

    def has_localization_keys(self) -> bool:
        """Whether this snapshot carries translation keys at all.

        Schema-1 dumps predate the mod harvesting them, so every key reads None — which is
        indistinguishable, cell by cell, from an entry that genuinely has no key. Callers
        that need to explain themselves (a job refusing to run, a column rendering empty)
        need to know which of the two they are looking at."""
        return bool(self._localization_keys)

    #endregion === Helpers

# This helper method imports a packdump from the minecraft instance (or a given path) as the local, current
# packdump snapshot, rotating out previous dumps as specified by `max_packdump_snapshot_count` in the
# PROFILE's settings (profile.json) — not main.toml, which holds app-wide config. Retention is per-profile
# because it's a property of the pack being worked on, not of the app: a 300-mod pack's snapshot is ~4 MB.
# This is all skipped if the incoming dump is identical to the current dump
@dataclass
class ImportResult:
    """What an attempted import did, and what it means for the user's data.

    The import itself is never the risky part — Layer 1 is read-only, so adopting a dump
    modifies nothing the user owns. What changes is how their Layer 2 data *reads* against
    it. So this carries enough for the caller to be loud in proportion to the consequences
    rather than to the mere fact that an import happened.
    """
    status: str                       # unchanged | imported | refused | missing | unreadable
    packdump: object = None           # the dump now in effect (previous one, if refused)
    issues: dict = field(default_factory=dict)      # from Profile.validate_packdump
    diff: dict = field(default_factory=dict)        # from Packdump.compare, old -> new
    adopted: list = field(default_factory=list)     # contract fields this dump defined
    forced: bool = False
    reason: str = ""                  # why, when status is refused or missing

    @property
    def errors(self) -> dict:
        return {name: issue for name, issue in self.issues.items()
                if issue.get("level") == "error"}

    def registry_delta(self) -> tuple:
        """(added, removed) entry counts across every registry, for a one-line summary."""
        changed = self.diff.get("registries", {}).get("changed", {})
        # compare() is called as current.compare(incoming), so "only_in_self" is what the
        # OLD dump had and the new one doesn't: removed.
        removed = sum(len(c.get("only_in_self", [])) for c in changed.values())
        added = sum(len(c.get("only_in_other", [])) for c in changed.values())
        return added, removed

    def mod_delta(self) -> tuple:
        mods = self.diff.get("mods", {})
        return len(mods.get("only_in_other", [])), len(mods.get("only_in_self", []))


def check_packdump(profile: Profile, source_path: Path = None) -> ImportResult:
    """Look at what's on disk without adopting it. Same decision as ``import_packdump``
    makes, minus the side effects — used to poll on window focus without importing."""
    source_path = source_path or (profile.mc_path / "packsmith")
    current = current_packdump(profile)
    if not (source_path / "meta.json").exists():
        return ImportResult("missing", current,
                            reason=f"no packdump found at {source_path}")
    try:
        incoming = Packdump.load(source_path)
    except Exception as e:
        # "unreadable", not "missing" — a corrupt or half-written dump is a different
        # problem from an instance the Forge mod has never run in, and conflating them
        # would send the user looking in the wrong place.
        return ImportResult("unreadable", current, reason=str(e))

    if current is not None and current == incoming:
        return ImportResult("unchanged", current)
    issues = profile.validate_packdump(incoming)
    diff = current.compare(incoming) if current is not None else {}
    status = "refused" if any(i.get("level") == "error" for i in issues.values()) \
        else "imported"
    return ImportResult(status, incoming if status == "imported" else current,
                        issues=issues, diff=diff)


def current_packdump(profile: Profile):
    """The snapshot currently in effect, or None if the profile has never imported one."""
    latest_dir = profile.packdumps_dir / "latest"
    if not (latest_dir / "meta.json").exists():
        return None
    return Packdump.load(latest_dir)


def import_packdump(profile: Profile, source_path: Path = None, *, force: bool = False) -> ImportResult:
    """Adopt the instance's packdump as this profile's latest snapshot.

    Refuses when the dump fails the profile's contract (see ``Profile.validate_packdump``):
    a different loader or Minecraft version means this is not a snapshot of the same pack,
    and adopting it would reinterpret every tag the user owns against a registry that isn't
    theirs. ``force`` overrides, and exists only because a check we got wrong shouldn't be
    a dead end.
    """
    source_path = source_path or (profile.mc_path / "packsmith")
    checked = check_packdump(profile, source_path)
    if checked.status in ("unchanged", "missing", "unreadable"):
        return checked
    if checked.status == "refused" and not force:
        return checked

    incoming = Packdump.load(source_path)
    latest_dir = profile.packdumps_dir / "latest"
    history_dir = profile.packdumps_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    # If we already have a latest, check if it's the same dump
    if (latest_dir / "meta.json").exists():
        current = Packdump.load(latest_dir)

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

    # A profile created without a declared version has no contract; the first dump it
    # accepts defines one, and everything after is checked against that.
    adopted = profile.adopt_contract_from(incoming)
    return ImportResult("imported", incoming, issues=checked.issues, diff=checked.diff,
                        adopted=adopted, forced=force and bool(checked.errors))

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




def revert_to_snapshot(profile: Profile, snapshot_name: str) -> ImportResult:
    """Make an archived snapshot the active one again (design 3.1's review workflow).

    Adopting a dump is loud but not destructive — Layer 1 is read-only and the import never
    touches Layer 2 — so "undo that update" has to be a real operation rather than an
    apology. This is that operation.

    **The current latest is archived first**, so reverting is itself revertible. Losing the
    dump you just came from would make this a one-way door and turn a review into a
    commitment, which is the opposite of the point.
    """
    history_dir = profile.packdumps_dir / "history"
    source = history_dir / snapshot_name
    if not (source / "meta.json").is_file():
        return ImportResult("missing", current_packdump(profile),
                            reason=f"No archived snapshot named '{snapshot_name}'")
    try:
        restoring = Packdump.load(source)
    except ValueError as e:
        return ImportResult("unreadable", current_packdump(profile), reason=str(e))

    latest_dir = profile.packdumps_dir / "latest"
    previous = None
    if (latest_dir / "meta.json").is_file():
        previous = Packdump.load(latest_dir)
        archive_path = history_dir / previous.timestamp.strftime("%Y-%m-%d_%H-%M-%S")
        if archive_path.exists():
            shutil.rmtree(archive_path)
        shutil.move(str(latest_dir), str(archive_path))
        log.info(f"Archived the current packdump to {archive_path} before reverting")

    latest_dir.mkdir(parents=True, exist_ok=True)
    restoring.save(latest_dir)
    shutil.rmtree(source, ignore_errors=True)      # it now lives in latest/, not history/
    log.info(f"Reverted packdump to snapshot {snapshot_name}")

    return ImportResult("imported", restoring,
                        diff=previous.compare(restoring) if previous else {})


# Whether a changed dump is adopted the moment it is noticed, or held for review.
#
# **Auto-adopt is the default**, and the reasoning is worth keeping next to the switch:
# stale is the worse failure mode. An unwanted import announces itself the moment you look
# at anything, while staleness produces confidently wrong output that looks fine. §3.1's
# argument for holding — "prevents transient mod installs from polluting the tag/blueprint
# data" — turned out not to apply, because importing never touches Layer 2 at all.
#
# It is a setting rather than a constant because the preference is genuinely arguable, and
# a user who wants to inspect every dump before it lands should be able to say so. One
# function, so a settings menu has exactly one thing to flip.
AUTO_ADOPT_SETTING = "auto_adopt_packdump"


def auto_adopt_enabled(profile: Profile) -> bool:
    return bool(profile.settings.get(AUTO_ADOPT_SETTING, True))


# The active dump lives in `latest/`, not in `history/`, so it has no folder name to refer
# to. This is the name the timeline uses for it.
ACTIVE_SNAPSHOT = "latest"


def snapshot_timeline(profile: Profile) -> list[dict]:
    """Every snapshot this profile holds, **newest first**, including the active one.

    Ordered by the timestamp the *game* generated the dump rather than by folder mtime: a
    revert rewrites folders, so file times record when Packsmith shuffled things around
    while the dump's own timestamp records when the pack actually looked like that. Only
    the second one makes "the previous snapshot" mean anything.
    """
    entries = [{**entry, "name": entry["path"].name, "active": False}
               for entry in list_snapshots(profile)]
    latest_dir = profile.packdumps_dir / "latest"
    if (latest_dir / "meta.json").is_file():
        try:
            with open(latest_dir / "meta.json", "r") as handle:
                meta = json.load(handle)
            entries.append({
                "path": latest_dir, "name": ACTIVE_SNAPSHOT, "active": True,
                "timestamp": meta.get("generated_at_utc", ""),
                "mc_version": meta.get("minecraft_version", ""),
                "loader": meta.get("loader", ""),
                "loader_version": meta.get("loader_version", ""),
                "mod_count": meta.get("mod_count", 0),
            })
        except (json.JSONDecodeError, OSError):
            log.warning("Skipping the active snapshot: its meta.json is unreadable")
    entries.sort(key=lambda entry: entry["timestamp"], reverse=True)
    return entries


def previous_snapshot(profile: Profile, name: str):
    """The snapshot immediately older than ``name``, or None if it is the oldest held.

    "Older" is by the dump's own timestamp, so this answers "what did the pack look like
    before this one" rather than "which folder was written first".
    """
    timeline = snapshot_timeline(profile)
    for index, entry in enumerate(timeline):
        if entry["name"] == name:
            return timeline[index + 1] if index + 1 < len(timeline) else None
    return None


def compare_snapshots(profile: Profile, older_name: str, newer_name: str) -> dict:
    """The raw diff between two stored snapshots, in ``old.compare(new)`` order.

    Direction is fixed here so callers cannot get it backwards — which is the one mistake
    that produces a confident, plausible, entirely wrong report (see `core/packdiff`).
    """
    by_name = {entry["name"]: entry["path"] for entry in snapshot_timeline(profile)}
    if older_name not in by_name or newer_name not in by_name:
        missing = older_name if older_name not in by_name else newer_name
        raise ValueError(f"No snapshot named '{missing}'")
    return Packdump.load(by_name[older_name]).compare(Packdump.load(by_name[newer_name]))
