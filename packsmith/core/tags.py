"""Tags — Layer 2's first primitive (design 3.2.1).

**Identity is the id; the name is a label.** Every internal reference (assignments here,
job-step bindings later) points at ``tag_definitions.id``, so renaming a tag is a one-row
update that touches no data. The *public API of this module is still name-based* — callers
say ``("minecraft:item", "remove")`` — because names are what the user, the query AST, and
action manifests speak. Resolution happens here, against the in-memory definition cache, so
it costs a dict lookup rather than a join.
"""
import json
import sqlite3
from dataclasses import dataclass

from packsmith.core.db import UserDB


@dataclass(frozen=True)
class Assignment:
    """A row that actually exists in ``tag_assignments`` — value *and* who owns it.

    The counterpart to ``get_tag``, which deliberately sugars a pristine cell into the tag's
    default. That sugar is right for display and wrong for anything that has to *restore* a
    cell later: "explicitly False" and "pristine, defaulting to False" read identically
    through it, so a caller that captures a pristine cell and writes the value back has
    silently invented a user-owned assignment (design 3.2.1: "the default is NOT written to
    the database"). Anything undoing, diffing, or asking "was a decision made here?" wants
    this instead.
    """
    value: object
    owner: str                   # "user" | "action"
    action_ref: str | None


@dataclass(frozen=True)
class Orphan:
    """An assignment pointing at something that no longer exists (design 3.2.1).

    Two causes, one state. ``reason`` distinguishes them because the available
    resolutions differ: a missing entry can only be cleared, while a stale value can also
    be reassigned or un-removed.
    """
    registry_type: str
    tag_name: str
    entry_id: str
    value: str
    reason: str          # "missing_entry" | "stale_value"

    @property
    def detail(self) -> str:
        if self.reason == "missing_entry":
            return f"{self.entry_id} is not in the current packdump"
        return f"{self.tag_name} = '{self.value}' is no longer a value of that tag"

# Stands in for "this tag isn't defined". No row can have it, so a positive filter matches
# nothing and a negative filter matches everything — which is the correct reading of a
# condition on a tag that doesn't exist.
_NO_SUCH_TAG = -1


class TagStore:

    def __init__(self, db: UserDB):
        self._db = db
        # Build cached definitions so casting doesn't hit the db over and over
        self._cached_definitions = None
        self._build_definitions()

    # === DEFINITION ===
    # Methods to define and undefine tags. Undefining also deletes all assignments (via CASCADE)
    def define(self, registry_type: str, name: str, tag_type: str, enum_values: list[str] = None,
               default=None) -> int:
        if tag_type not in ("bool", "string", "enum", "number"):
            raise ValueError(f"Invalid tag type: '{tag_type}'")
        if tag_type == "enum" and not enum_values:
            raise ValueError("Enum tags require a list of values")
        if tag_type != "enum" and enum_values:
            raise ValueError(f"enum_values provided but tag type is '{tag_type}', not 'enum'")
        if default is not None:
            # Validate the default against the type we're about to insert (the definition
            # doesn't exist yet, so _validate_tag_value can't do it).
            if tag_type == "bool" and not isinstance(default, bool):
                raise ValueError(f"Default for '{name}' expects bool, got {type(default).__name__}")
            if tag_type == "number" and not isinstance(default, (int, float)):
                raise ValueError(f"Default for '{name}' expects number, got {type(default).__name__}")
            if tag_type == "enum" and str(default) not in enum_values:
                raise ValueError(f"Default for '{name}' value '{default}' not in allowed values: {enum_values}")
        enum_json = json.dumps(enum_values) if enum_values else None
        try:
            cur = self._db.execute(
                """INSERT INTO tag_definitions (registry_type, name, type, enum_values, default_value)
                   VALUES (?, ?, ?, ?, ?)""",
                (registry_type, name, tag_type, enum_json,
                 None if default is None else str(default)),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"Tag '{name}' already exists on registry '{registry_type}'")
        self._build_definitions()
        return cur.lastrowid

    def undefine(self, registry_type: str, name: str):
        self._db.execute("DELETE FROM tag_definitions WHERE registry_type = ? AND name = ?",
                         (registry_type, name))
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
        tag_id = self._tag_id(registry_type, tag_name)
        if isinstance(entry_id, str):
            entry_id = [entry_id]
        self._db.execute_many(
            """INSERT INTO tag_assignments (tag_id, entry_id, value, owner_kind, owner_action_ref)
               VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tag_id, entry_id) DO UPDATE SET
                       value = excluded.value,
                       owner_kind = excluded.owner_kind,
                       owner_action_ref = excluded.owner_action_ref""",
            [(tag_id, eid, str(value), owner, owner_action_ref) for eid in entry_id]
        )

    def unassign(self, registry_type: str, entry_id: str | list[str], tag_name: str):
        tag_id = self._tag_id(registry_type, tag_name, required=False)
        if tag_id == _NO_SUCH_TAG:
            return
        if isinstance(entry_id, str):
            entry_id = [entry_id]
        self._db.execute_many(
            "DELETE FROM tag_assignments WHERE tag_id = ? AND entry_id = ?",
            [(tag_id, eid) for eid in entry_id]
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

        registry_tag_ids = [d["id"] for d in self.definitions_for(registry_type).values()]
        if not registry_tag_ids:
            return []

        conditions = []
        params = list(registry_tag_ids)

        for f in all_filters:
            op = f.get("op", "eq")
            if op not in self._VALID_OPS:
                raise ValueError(f"Invalid filter op: '{op}'")
            tag_id = self._tag_id(registry_type, f["tag"], required=False)

            if op == "eq":
                conditions.append(
                    "entry_id IN (SELECT entry_id FROM tag_assignments WHERE tag_id = ? AND value = ?)")
                params.extend([tag_id, str(f["value"])])

            elif op == "neq":
                conditions.append(
                    "entry_id NOT IN (SELECT entry_id FROM tag_assignments WHERE tag_id = ? AND value = ?)")
                params.extend([tag_id, str(f["value"])])

            elif op in ("in", "not_in"):
                placeholders = ",".join("?" for _ in f["values"])
                negate = "NOT " if op == "not_in" else ""
                conditions.append(
                    f"entry_id {negate}IN (SELECT entry_id FROM tag_assignments "
                    f"WHERE tag_id = ? AND value IN ({placeholders}))")
                params.extend([tag_id] + [str(v) for v in f["values"]])

            elif op in ("gt", "lt", "gte", "lte"):
                sql_op = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}[op]
                conditions.append(
                    f"entry_id IN (SELECT entry_id FROM tag_assignments "
                    f"WHERE tag_id = ? AND CAST(value AS REAL) {sql_op} ?)")
                params.extend([tag_id, float(f["value"])])

            elif op in ("exists", "not_exists"):
                negate = "NOT " if op == "not_exists" else ""
                conditions.append(
                    f"entry_id {negate}IN (SELECT entry_id FROM tag_assignments WHERE tag_id = ?)")
                params.append(tag_id)

        id_placeholders = ",".join("?" for _ in registry_tag_ids)
        sql = (f"SELECT DISTINCT entry_id FROM tag_assignments "
               f"WHERE tag_id IN ({id_placeholders}) AND {' AND '.join(conditions)}")
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
        tag_id = self._tag_id(registry_type, tag_name, required=False)
        row = self._db.fetch_one(
            "SELECT value FROM tag_assignments WHERE tag_id = ? AND entry_id = ?",
            (tag_id, entry_id)
        )
        if row:
            return self._cast_tag_value(registry_type, tag_name, row["value"])
        # Pristine cell — fall back to the tag's default (or None).
        return self.default_for(registry_type, tag_name)

    def assignment(self, registry_type: str, entry_id: str,
                   tag_name: str) -> "Assignment | None":
        """The stored assignment, or **None when the cell is pristine**.

        One query for value + ownership, because every caller that cares about existence
        also cares about who owned it — restoring a value without its owner turns an
        action's cell into the user's.
        """
        tag_id = self._tag_id(registry_type, tag_name, required=False)
        row = self._db.fetch_one(
            "SELECT value, owner_kind, owner_action_ref FROM tag_assignments "
            "WHERE tag_id = ? AND entry_id = ?",
            (tag_id, entry_id)
        )
        if not row:
            return None
        return Assignment(
            value=self._cast_tag_value(registry_type, tag_name, row["value"]),
            owner=row["owner_kind"], action_ref=row["owner_action_ref"])

    # Gets ALL tag assignments for a single entry.
    def get_all_tags(self, registry_type: str, entry_id: str) -> dict:
        rows = self._db.fetch_all(
            """SELECT d.name AS name, a.value AS value
               FROM tag_assignments a JOIN tag_definitions d ON a.tag_id = d.id
               WHERE d.registry_type = ? AND a.entry_id = ?""",
            (registry_type, entry_id)
        )
        return {row["name"]: self._cast_tag_value(registry_type, row["name"], row["value"])
                for row in rows}

    # Returns the owner of an assignment, or None if the cell is PRISTINE (no row exists).
    # This is the existence/ownership channel the registry table needs: a cell can display
    # a default value while still being pristine (no row, no owner) — the only way to tell
    # pristine-with-default apart from an owned value is to ask here, not to look at the value.
    # Shape: None (pristine) | {"kind": "user"} | {"kind": "action", "action_ref": "pkg:id"}.
    def get_ownership(self, registry_type: str, entry_id: str, tag_name: str) -> dict | None:
        tag_id = self._tag_id(registry_type, tag_name, required=False)
        row = self._db.fetch_one(
            "SELECT owner_kind, owner_action_ref FROM tag_assignments "
            "WHERE tag_id = ? AND entry_id = ?",
            (tag_id, entry_id)
        )
        if not row:
            return None
        return {"kind": row["owner_kind"], "action_ref": row["owner_action_ref"]}

    #region === Helpers ===

    # Builds the tag definition cache, nested by registry type:
    # {registry_type: {name: definition}}. Definitions are registry-scoped (design 3.2.1).
    def _build_definitions(self):
        rows = self._db.fetch_all(
            "SELECT id, registry_type, name, type, enum_values, default_value FROM tag_definitions "
            "ORDER BY registry_type, name")
        definitions = {}
        for row in rows:
            d = {"id": row["id"], "name": row["name"], "type": row["type"],
                 "default_value": row["default_value"]}
            # Unpack json of enum vals if needed
            if row["enum_values"]:
                d["values"] = json.loads(row["enum_values"])
            definitions.setdefault(row["registry_type"], {})[row["name"]] = d
        self._cached_definitions = definitions

    # Returns the cached definitions for one registry: {name: definition} (empty if none).
    def definitions_for(self, registry_type: str) -> dict[str, dict]:
        return dict(self._cached_definitions.get(registry_type, {}))

    # Every registry type that has at least one tag defined on it, sorted.
    def defined_registries(self) -> list[str]:
        return sorted(self._cached_definitions.keys())

    # Gets a single tag definition on a registry, or None if it doesn't exist.
    def definition(self, registry_type: str, name: str) -> dict | None:
        return self._cached_definitions.get(registry_type, {}).get(name)

    def definition_by_id(self, tag_id: int) -> dict | None:
        """The definition a stored **id** points at, or None if it's been undefined.

        Design 3.2.1: "a tag definition's identity is a surrogate id, not its name", and
        job step bindings reference the id. This is how a binding gets back to a tag it
        will keep pointing at across renames. A cache scan, not a query.
        """
        for by_name in self._cached_definitions.values():
            for definition in by_name.values():
                if definition["id"] == tag_id:
                    return definition
        return None

    # Resolves (registry, name) -> the definition's id, the thing rows actually reference.
    # A cache hit, not a query. With required=False an unknown tag yields the _NO_SUCH_TAG
    # sentinel so callers can build SQL that simply matches nothing.
    def _tag_id(self, registry_type: str, name: str, *, required: bool = True) -> int:
        definition = self.definition(registry_type, name)
        if definition is None:
            if required:
                raise ValueError(f"Tag '{name}' is not defined on registry '{registry_type}'")
            return _NO_SUCH_TAG
        return definition["id"]

    # Assignments that reference something no longer present (design 3.2.1). Two causes,
    # one state: the ENTRY is gone from the packdump, or the VALUE is no longer part of its
    # definition (an enum value was removed). Derived, never stored, so it can't go stale.
    def find_orphans(self, packdump) -> list[Orphan]:
        rows = self._db.fetch_all(
            """SELECT d.registry_type AS registry_type, d.name AS tag_name,
                      a.entry_id AS entry_id, a.value AS value
               FROM tag_assignments a JOIN tag_definitions d ON a.tag_id = d.id
               ORDER BY d.registry_type, d.name, a.entry_id"""
        )
        orphans = []
        for row in rows:
            reg_type, entry_id = row["registry_type"], row["entry_id"]
            registry = packdump.registry.get(reg_type)
            if registry is None or entry_id not in registry["values"]:
                orphans.append(Orphan(reg_type, row["tag_name"], entry_id, row["value"],
                                      "missing_entry"))
                continue
            definition = self.definition(reg_type, row["tag_name"])
            if (definition is not None and definition["type"] == "enum"
                    and row["value"] not in definition.get("values", [])):
                orphans.append(Orphan(reg_type, row["tag_name"], entry_id, row["value"],
                                      "stale_value"))
        return orphans

    # Summary view of the above: {registry_type: [entry_id, ...]}.
    def orphaned_tags(self, packdump) -> dict[str, list[str]]:
        grouped = {}
        for orphan in self.find_orphans(packdump):
            grouped.setdefault(orphan.registry_type, set()).add(orphan.entry_id)
        return {reg: sorted(ids) for reg, ids in grouped.items()}

    # === ENUM VALUE EVOLUTION (design 3.2.1) ===
    # Adding and reordering are free. REMOVING is destructive — the caller is responsible
    # for confirming it, and `preview_enum_change` is what makes that confirmation honest.

    def entries_with_value(self, registry_type: str, tag_name: str, value) -> list[str]:
        """Entries currently holding this exact value — one half of a removal's blast radius."""
        tag_id = self._tag_id(registry_type, tag_name, required=False)
        rows = self._db.fetch_all(
            "SELECT entry_id FROM tag_assignments WHERE tag_id = ? AND value = ? ORDER BY entry_id",
            (tag_id, str(value)))
        return [row["entry_id"] for row in rows]

    def preview_enum_change(self, registry_type: str, tag_name: str, new_values: list[str]) -> dict:
        """What changing an enum's value list would cost, *before* committing it.

        Returns ``{"added", "removed", "orphaned" {value: [entry_ids]}, "orphan_count",
        "default_cleared"}``. The UI turns this into the confirmation dialog; nothing here
        mutates anything.
        """
        definition = self.definition(registry_type, tag_name)
        if not definition or definition["type"] != "enum":
            raise ValueError(f"Tag '{tag_name}' on '{registry_type}' is not an enum")
        current = definition.get("values", [])
        removed = [v for v in current if v not in new_values]
        orphaned = {v: self.entries_with_value(registry_type, tag_name, v) for v in removed}
        orphaned = {v: ids for v, ids in orphaned.items() if ids}
        return {
            "added": [v for v in new_values if v not in current],
            "removed": removed,
            "orphaned": orphaned,
            "orphan_count": sum(len(ids) for ids in orphaned.values()),
            # A default pointing at a removed value can't survive the change.
            "default_cleared": definition.get("default_value") in removed,
        }

    def set_enum_values(self, registry_type: str, tag_name: str, values: list[str]):
        """Replace an enum tag's value list (add / remove / reorder in one shot).

        Order is preserved because it is semantic — tables sort by it. Assignments still
        holding a removed value are *not* deleted; they become orphans (see
        ``find_orphans``) so the user can resolve them deliberately. A default that pointed
        at a removed value is cleared, since it can no longer be valid.
        """
        definition = self.definition(registry_type, tag_name)
        if not definition or definition["type"] != "enum":
            raise ValueError(f"Tag '{tag_name}' on '{registry_type}' is not an enum")
        values = [v for v in values if v]
        if not values:
            raise ValueError("An enum tag needs at least one value")

        default = definition.get("default_value")
        clear_default = default is not None and default not in values
        self._db.execute(
            "UPDATE tag_definitions SET enum_values = ?"
            + (", default_value = NULL" if clear_default else "")
            + " WHERE registry_type = ? AND name = ?",
            (json.dumps(values), registry_type, tag_name))
        self._build_definitions()

    def clear_value(self, registry_type: str, tag_name: str, value) -> int:
        """Delete every assignment holding this value. Returns how many were removed.
        The 'Clear' orphan resolution."""
        entries = self.entries_with_value(registry_type, tag_name, value)
        if entries:
            self.unassign(registry_type, entries, tag_name)
        return len(entries)

    def reassign_value(self, registry_type: str, tag_name: str, old_value, new_value) -> int:
        """Bulk-move every assignment from one value to another, preserving ownership.
        Returns how many moved. The 'Reassign to…' orphan resolution."""
        self._validate_tag_value(registry_type, tag_name, new_value)
        tag_id = self._tag_id(registry_type, tag_name)
        entries = self.entries_with_value(registry_type, tag_name, old_value)
        if entries:
            self._db.execute(
                "UPDATE tag_assignments SET value = ? WHERE tag_id = ? AND value = ?",
                (str(new_value), tag_id, str(old_value)))
        return len(entries)

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
