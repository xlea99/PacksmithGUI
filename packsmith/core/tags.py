import json
import sqlite3
from packsmith.core.db import UserDB


class TagStore:

    def __init__(self, db: UserDB):
        self._db = db
        # Build cached definitions so casting doesn't hit the db over and over
        self._cached_definitions = None
        self._build_definitions()

    # === DEFINITION ===
    # Methods to define and undefine tags. Undefining also deletes all assignments (via CASCADE)
    def define(self, registry_type: str, name: str, tag_type: str, enum_values: list[str] = None, default=None):
        if tag_type not in ("bool", "string", "enum", "number"):
            raise ValueError(f"Invalid tag type: '{tag_type}'")
        if tag_type == "enum" and not enum_values:
            raise ValueError("Enum tags require a list of values")
        if tag_type != "enum" and enum_values:
            raise ValueError(f"enum_values provided but tag type is '{tag_type}', not 'enum'")
        enum_json = json.dumps(enum_values) if enum_values else None
        try:
            if default is None:
                self._db.execute(
                    "INSERT INTO tag_definitions (registry_type, name, type, enum_values) VALUES (?, ?, ?, ?)",
                    (registry_type, name, tag_type, enum_json)
                )
            else:
                # Validate default against the type we're about to insert (can't use _validate_tag_value yet)
                if tag_type == "bool" and not isinstance(default, bool):
                    raise ValueError(f"Default for '{name}' expects bool, got {type(default).__name__}")
                if tag_type == "number" and not isinstance(default, (int, float)):
                    raise ValueError(f"Default for '{name}' expects number, got {type(default).__name__}")
                if tag_type == "enum" and str(default) not in enum_values:
                    raise ValueError(f"Default for '{name}' value '{default}' not in allowed values: {enum_values}")
                self._db.execute(
                    "INSERT INTO tag_definitions (registry_type, name, type, enum_values, default_value) VALUES (?, ?, ?, ?, ?)",
                    (registry_type, name, tag_type, enum_json, str(default))
                )
            # Rebuild cached definitions
            self._build_definitions()
        except sqlite3.IntegrityError:
            raise ValueError(f"Tag '{name}' already exists on registry '{registry_type}'")
    def undefine(self, registry_type: str, name: str):
        self._db.execute("DELETE FROM tag_definitions WHERE registry_type = ? AND name = ?", (registry_type, name))
        # Rebuild cached definitions
        self._build_definitions()

    # === ASSIGNMENT ===
    # Tag assignment, supports one or many entry_ids.
    # Assigns a value and stamps ownership. Whoever writes, owns: a user edit stamps
    # owner='user', an action write stamps owner='action' with its package:action_id.
    # This is deliberately dumb-but-correct at the DB layer — the loud transfer
    # confirmation (user overriding an action-owned cell) and action conflict policies
    # (design 3.2.1 / 3.3) live ABOVE this call, not inside it. On conflict we overwrite
    # both value AND owner, so taking over a cell is just an assign by the new owner.
    def assign(self, registry_type: str, entry_id: str | list[str], tag_name: str, value,
               *, owner: str = "user", owner_action_ref: str = None):
        self._validate_tag_value(registry_type, tag_name, value)
        if owner not in ("user", "action"):
            raise ValueError(f"Invalid owner: '{owner}' (expected 'user' or 'action')")
        if owner == "action" and not owner_action_ref:
            raise ValueError("Action-owned assignments require an owner_action_ref (package:action_id)")
        if owner == "user" and owner_action_ref:
            raise ValueError("User-owned assignments must not carry an owner_action_ref")
        if isinstance(entry_id, str):
            entry_id = [entry_id]
        self._db.execute_many(
            """INSERT INTO tag_assignments
                   (registry_type, entry_id, tag_name, value, owner_kind, owner_action_ref)
               VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(registry_type, entry_id, tag_name) DO UPDATE SET
                       value = excluded.value,
                       owner_kind = excluded.owner_kind,
                       owner_action_ref = excluded.owner_action_ref""",
            [(registry_type, eid, tag_name, str(value), owner, owner_action_ref) for eid in entry_id]
        )
    def unassign(self, registry_type: str, entry_id: str | list[str], tag_name: str):
        if isinstance(entry_id,str):
            entry_id = [entry_id]
        self._db.execute_many(
            "DELETE FROM tag_assignments WHERE registry_type = ? AND entry_id = ? AND tag_name = ?",
            [(registry_type, eid, tag_name) for eid in entry_id]
        )

    # === QUERYING ===
    # Unified query method. Supports simple kwargs (tier="mid", banned=True) and/or
    # a rich filters list for advanced ops. All conditions are ANDed together.
    #
    # Simple:   tags.query("minecraft:item", tier="mid", banned=True)
    # Advanced: tags.query("minecraft:item", filters=[{"tag": "tier", "op": "in", "values": ["mid", "low"]}])
    # Both:     tags.query("minecraft:item", filters=[{"tag": "weight", "op": "gt", "value": 5}], banned=True)
    _VALID_OPS = {"eq", "neq", "in", "not_in", "gt", "lt", "gte", "lte", "exists", "not_exists"}
    def query(self, registry_type: str, filters: list[dict] = None, **kwargs) -> list[str]:
        all_filters = list(filters) if filters else []

        # Convert kwargs into eq filters
        for tag_name, value in kwargs.items():
            all_filters.append({"tag": tag_name, "op": "eq", "value": value})

        if not all_filters:
            return []

        conditions = []
        params = [registry_type]

        for f in all_filters:
            op = f.get("op", "eq")
            tag = f["tag"]
            if op not in self._VALID_OPS:
                raise ValueError(f"Invalid filter op: '{op}'")

            if op == "eq":
                conditions.append(
                    "entry_id IN (SELECT entry_id FROM tag_assignments "
                    "WHERE registry_type = ? AND tag_name = ? AND value = ?)"
                )
                params.extend([registry_type, tag, str(f["value"])])

            elif op == "neq":
                conditions.append(
                    "entry_id NOT IN (SELECT entry_id FROM tag_assignments "
                    "WHERE registry_type = ? AND tag_name = ? AND value = ?)"
                )
                params.extend([registry_type, tag, str(f["value"])])

            elif op == "in":
                placeholders = ",".join("?" for _ in f["values"])
                conditions.append(
                    f"entry_id IN (SELECT entry_id FROM tag_assignments "
                    f"WHERE registry_type = ? AND tag_name = ? AND value IN ({placeholders}))"
                )
                params.extend([registry_type, tag] + [str(v) for v in f["values"]])

            elif op == "not_in":
                placeholders = ",".join("?" for _ in f["values"])
                conditions.append(
                    f"entry_id NOT IN (SELECT entry_id FROM tag_assignments "
                    f"WHERE registry_type = ? AND tag_name = ? AND value IN ({placeholders}))"
                )
                params.extend([registry_type, tag] + [str(v) for v in f["values"]])

            elif op in ("gt", "lt", "gte", "lte"):
                sql_op = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}[op]
                conditions.append(
                    f"entry_id IN (SELECT entry_id FROM tag_assignments "
                    f"WHERE registry_type = ? AND tag_name = ? AND CAST(value AS REAL) {sql_op} ?)"
                )
                params.extend([registry_type, tag, float(f["value"])])

            elif op == "exists":
                conditions.append(
                    "entry_id IN (SELECT entry_id FROM tag_assignments "
                    "WHERE registry_type = ? AND tag_name = ?)"
                )
                params.extend([registry_type, tag])

            elif op == "not_exists":
                conditions.append(
                    "entry_id NOT IN (SELECT entry_id FROM tag_assignments "
                    "WHERE registry_type = ? AND tag_name = ?)"
                )
                params.extend([registry_type, tag])

        sql = f"SELECT DISTINCT entry_id FROM tag_assignments WHERE registry_type = ? AND {' AND '.join(conditions)}"
        rows = self._db.fetch_all(sql, tuple(params))
        return [row["entry_id"] for row in rows]

    # The value a PRISTINE cell reads: the tag's default (cast to its type), or None
    # if the tag has no default (or isn't defined). Note this is purely about VALUE —
    # a pristine cell has no owner regardless of what default it displays.
    def default_for(self, registry_type: str, tag_name: str):
        definition = self.definition(registry_type, tag_name)
        if definition and definition.get("default_value") is not None:
            return self._cast_tag_value(registry_type, tag_name, definition["default_value"])
        return None

    # Gets a single tag value for an entry, or the tag's default if unset (None if neither).
    def get_tag(self, registry_type: str, entry_id: str, tag_name: str):
        row = self._db.fetch_one(
            "SELECT value FROM tag_assignments WHERE registry_type = ? AND entry_id = ? AND tag_name = ?",
            (registry_type, entry_id, tag_name)
        )
        if row:
            return self._cast_tag_value(registry_type, tag_name, row["value"])
        # Pristine cell — fall back to the tag's default (or None).
        return self.default_for(registry_type, tag_name)
    # Gets ALL tag assignments for a single entry.
    def get_all_tags(self, registry_type: str, entry_id: str) -> dict:
        rows = self._db.fetch_all(
            "SELECT tag_name, value FROM tag_assignments WHERE registry_type = ? AND entry_id = ?",
            (registry_type, entry_id)
        )
        return {row["tag_name"]: self._cast_tag_value(registry_type, row["tag_name"], row["value"]) for row in rows}

    # Returns the owner of an assignment, or None if the cell is PRISTINE (no row exists).
    # This is the existence/ownership channel the registry table needs: a cell can display
    # a default value while still being pristine (no row, no owner) — the only way to tell
    # pristine-with-default apart from an owned value is to ask here, not to look at the value.
    # Shape: None (pristine) | {"kind": "user"} | {"kind": "action", "action_ref": "pkg:id"}.
    def get_ownership(self, registry_type: str, entry_id: str, tag_name: str) -> dict | None:
        row = self._db.fetch_one(
            "SELECT owner_kind, owner_action_ref FROM tag_assignments "
            "WHERE registry_type = ? AND entry_id = ? AND tag_name = ?",
            (registry_type, entry_id, tag_name)
        )
        if not row:
            return None
        return {"kind": row["owner_kind"], "action_ref": row["owner_action_ref"]}

    #region === Helpers ===

    # Builds the tag definition cache, nested by registry type:
    # {registry_type: {name: definition}}. Definitions are registry-scoped (design 3.2.1).
    def _build_definitions(self):
        rows = self._db.fetch_all(
            "SELECT registry_type, name, type, enum_values, default_value FROM tag_definitions ORDER BY registry_type, name")
        definitions = {}
        for row in rows:
            d = {"name": row["name"], "type": row["type"], "default_value": row["default_value"]}
            # Unpack json of enum vals if needed
            if row["enum_values"]:
                d["values"] = json.loads(row["enum_values"])
            definitions.setdefault(row["registry_type"], {})[row["name"]] = d
        self._cached_definitions = definitions
    # Returns the cached definitions for one registry: {name: definition} (empty if none).
    def definitions_for(self, registry_type: str) -> dict[str, dict]:
        return dict(self._cached_definitions.get(registry_type, {}))
    # Gets a single tag definition on a registry, or None if it doesn't exist.
    def definition(self, registry_type: str, name: str) -> dict | None:
        return self._cached_definitions.get(registry_type, {}).get(name)

    # Returns a dict of tag assignments that reference entries that aren't present in the given packdump (orphaned).
    def orphaned_tags(self, packdump) -> dict[str, list[str]]:
        rows = self._db.fetch_all(
            "SELECT DISTINCT registry_type, entry_id FROM tag_assignments"
        )
        orphans = {}
        for row in rows:
            reg_type = row["registry_type"]
            entry_id = row["entry_id"]
            reg = packdump.registry.get(reg_type)
            if reg is None or entry_id not in reg["values"]:
                orphans.setdefault(reg_type, []).append(entry_id)
        return orphans

    # Validates a value against the tag's definition, ensuring it exists and conforms to expected type.
    def _validate_tag_value(self, registry_type: str, tag_name: str, value):
        definition = self.definition(registry_type, tag_name)
        if not definition:
            raise ValueError(f"Tag '{tag_name}' is not defined on registry '{registry_type}'")
        tag_type = definition["type"]
        if tag_type == "bool" and not isinstance(value, bool):
            raise ValueError(f"Tag '{tag_name}' expects bool, got {type(value).__name__}")
        if tag_type == "number" and not isinstance(value, (int, float)):
            raise ValueError(f"Tag '{tag_name}' expects number, got {type(value).__name__}")
        if tag_type == "enum" and str(value) not in definition.get("values", []):
            raise ValueError(f"Tag '{tag_name}' value '{value}' not in allowed values: {definition['values']}")
    # Simply casts a stored string value back to its real python type, if its not supposed to be a string or enum
    def _cast_tag_value(self, registry_type: str, tag_name: str, raw: str):
        definition = self.definition(registry_type, tag_name)
        if not definition:
            return raw
        tag_type = definition["type"]
        if tag_type == "bool":
            return raw.lower() == "true"
        if tag_type == "number":
            try:
                return int(raw)
            except ValueError:
                return float(raw)
        return raw

    #endregion === Helpers ===
