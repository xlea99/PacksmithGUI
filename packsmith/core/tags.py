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
    def define(self, name: str, tag_type: str, enum_values: list[str] = None, default=None):
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
                    "INSERT INTO tag_definitions (name, type, enum_values) VALUES (?, ?, ?)",
                    (name, tag_type, enum_json)
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
                    "INSERT INTO tag_definitions (name, type, enum_values, default_value) VALUES (?, ?, ?, ?)",
                    (name, tag_type, enum_json, str(default))
                )
            # Rebuild cached definitions
            self._build_definitions()
        except sqlite3.IntegrityError:
            raise ValueError(f"Tag '{name}' already exists")
    def undefine(self, name: str):
        self._db.execute("DELETE FROM tag_definitions WHERE name = ?", (name,))
        # Rebuild cached definitions
        self._build_definitions()

    # === ASSIGNMENT ===
    # Tag assignment, supports one or many entry_ids.
    def assign(self,registry_type: str, entry_id: str | list[str], tag_name: str, value):
        self._validate_tag_value(tag_name, value)
        if isinstance(entry_id, str):
            entry_id = [entry_id]
        self._db.execute_many(
            """INSERT INTO tag_assignments (registry_type, entry_id, tag_name, value)
               VALUES (?, ?, ?, ?)
                   ON CONFLICT(registry_type, entry_id, tag_name) DO UPDATE SET value = excluded.value""",
            [(registry_type, eid, tag_name, str(value)) for eid in entry_id]
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

    # Gets a single tag value for an entry, or None if its not set.
    def get_tag(self, registry_type: str, entry_id: str, tag_name: str):
        row = self._db.fetch_one(
            "SELECT value FROM tag_assignments WHERE registry_type = ? AND entry_id = ? AND tag_name = ?",
            (registry_type, entry_id, tag_name)
        )
        if row:
            return self._cast_tag_value(tag_name, row["value"])
        # Check to see if there's a default value to return in cases where the tag isn't explicitly set.
        definition = self.definition(tag_name)
        if definition and definition.get("default_value") is not None:
            return self._cast_tag_value(tag_name, definition["default_value"])
        else:
            return None
    # Gets ALL tag assignments for a single entry.
    def get_all_tags(self, registry_type: str, entry_id: str) -> dict:
        rows = self._db.fetch_all(
            "SELECT tag_name, value FROM tag_assignments WHERE registry_type = ? AND entry_id = ?",
            (registry_type, entry_id)
        )
        return {row["tag_name"]: self._cast_tag_value(row["tag_name"], row["value"]) for row in rows}

    #region === Helpers ===

    # Builds the tag definition cache.
    def _build_definitions(self):
        rows = self._db.fetch_all("SELECT name, type, enum_values, default_value FROM tag_definitions ORDER BY name")
        definitions = {}
        for row in rows:
            d = {"name": row["name"], "type": row["type"], "default_value": row["default_value"]}
            # Unpack json of enum vals if needed
            if row["enum_values"]:
                d["values"] = json.loads(row["enum_values"])
            definitions[row['name']] = d
        self._cached_definitions = definitions
    # Returns the cached definitions dict.
    @property
    def all_definitions(self) -> dict[str,dict]:
        return self._cached_definitions
    # Gets a single tag definition, or None if it doesn't exist.
    def definition(self, name: str) -> dict | None:
        return self.all_definitions.get(name,None)

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
    def _validate_tag_value(self, tag_name: str, value):
        definition = self.definition(tag_name)
        if not definition:
            raise ValueError(f"Tag '{tag_name}' is not defined")
        tag_type = definition["type"]
        if tag_type == "bool" and not isinstance(value, bool):
            raise ValueError(f"Tag '{tag_name}' expects bool, got {type(value).__name__}")
        if tag_type == "number" and not isinstance(value, (int, float)):
            raise ValueError(f"Tag '{tag_name}' expects number, got {type(value).__name__}")
        if tag_type == "enum" and str(value) not in definition.get("values", []):
            raise ValueError(f"Tag '{tag_name}' value '{value}' not in allowed values: {definition['values']}")
    # Simply casts a stored string value back to its real python type, if its not supposed to be a string or enum
    def _cast_tag_value(self, tag_name: str, raw: str):
        definition = self.definition(tag_name)
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
