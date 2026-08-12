"""Blueprints — Layer 2's second primitive (design 3.2.2).

Tags are flat: they describe one entry in isolation. Blueprints model the thing tags
can't — **relationships between registry entries**. The canonical case is the stone
palette: `minecraft:granite`, `minecraft:polished_granite`,
`minecraft:polished_granite_stairs` and `quark:granite_bricks` are entries from different
mods that are conceptually one "stone type", and nothing in the game's registry says so.

A blueprint is an instantiable schema — "a struct, or a class without methods". Define
``StoneType`` with inline groups and slots, instantiate ``StoneType:granite``, bind entries
into slots, and the empty slots *are* the answer to "what's missing from my pack".

Blueprints are purely descriptive. They live on Layer 2, not Layer 3 — they organise
information and do nothing on their own. Actions consuming them is where the power is.

**Schema evolution** follows one principle, stated in 3.2.2: *non-destructive changes
propagate silently; destructive changes orphan at the instance level and refuse to
participate in actions until the user explicitly resolves their intent.* Add and rename are
non-destructive. Remove and retype orphan — and they orphan the **whole instance**, not
just the affected binding, because schema destruction is significant enough that the user
should be made to confirm intent before anything downstream proceeds.
"""
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field

SCALAR_TYPES = ("string", "number", "bool", "enum")
SLOT_TYPES = SCALAR_TYPES + ("registry", "blueprint")

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}

# "leave this as it is" — distinct from None, which means the top level.
_KEEP = object()


class BlueprintError(Exception):
    """A blueprint schema or binding was rejected."""


@dataclass(frozen=True)
class Slot:
    """One row of a blueprint's shape — either a ``group`` or a bindable ``value``."""
    id: int
    blueprint: str
    name: str
    path: str                     # dotted, e.g. "polished.stairs"
    kind: str                     # group | value
    type: str = None              # None for groups
    registry_type: str = None
    ref_blueprint: str = None
    enum_values: tuple = ()
    parent_id: int = None
    depth: int = 0

    @property
    def is_group(self) -> bool:
        return self.kind == "group"

    def describe(self) -> str:
        if self.is_group:
            return "group"
        if self.type == "registry":
            return self.registry_type
        if self.type == "blueprint":
            return self.ref_blueprint
        if self.type == "enum":
            return f"enum({', '.join(self.enum_values)})"
        return self.type


@dataclass(frozen=True)
class Instance:
    id: int
    blueprint: str
    name: str
    created_by: str = None

    @property
    def ref(self) -> str:
        return f"{self.blueprint}:{self.name}"


@dataclass(frozen=True)
class Binding:
    slot_path: str
    value: str
    owner: str = "user"
    action_ref: str = None


@dataclass(frozen=True)
class BindingOrphan:
    """A binding pointing at something that no longer exists (design 3.2.2).

    Deliberately NOT the same thing as an orphaned *instance*. An instance is orphaned by a
    destructive schema edit — the user's own act, stored, and it locks the instance out of
    actions until they resolve it. This is the packdump changing underneath a binding, which
    is nobody's decision, so it mirrors tag orphans instead: derived on demand, surfaced,
    and it locks nothing.
    """
    blueprint: str
    instance: str
    slot_path: str
    value: str
    expected: str            # the registry type, or the referenced blueprint
    reason: str              # "missing_entry" | "missing_instance"

    @property
    def detail(self) -> str:
        if self.reason == "missing_instance":
            return f"'{self.value}' is not an instance of {self.expected} any more"
        return f"'{self.value}' is no longer in {self.expected}"


@dataclass(frozen=True)
class Impact:
    """What a schema change is about to do, for the confirmation dialog 3.2.2 requires.

    Every destructive mutation is previewable before it commits, because the whole design
    rests on the user confirming intent — and you can't confirm what you weren't shown.
    """
    kind: str                       # add_slot | rename_slot | remove_slot | retype_slot
    blueprint: str
    slot_path: str
    instances: int = 0              # instances of this blueprint in total
    bound: tuple = ()               # instance names holding a binding to the slot
    destructive: bool = False
    can_coerce: bool = False        # retype only: every binding survives the new type
    problematic: tuple = ()         # retype only: (instance, value) that would not
    blocked: str = None             # why this mutation cannot proceed at all

    @property
    def orphans(self) -> tuple:
        return self.bound if self.destructive else ()


@dataclass(frozen=True)
class Orphan:
    """An instance locked out of actions until the user says what they meant."""
    blueprint: str
    instance: str
    reason: str                     # remove_slot | retype_slot
    slot_path: str
    problematic: tuple = ()         # slot paths awaiting a re-bind (retype only)

    @property
    def resolutions(self) -> tuple:
        """Which of 3.2.2's four resolutions apply. "Change Problematic Values" is
        retype-only — nothing can conjure back a slot that was removed."""
        common = ("discard", "preserve", "revert")
        return common + ("rebind",) if self.reason == "retype_slot" else common


class BlueprintStore:
    """CRUD over blueprint schemas, instances and bindings.

    ``packdump`` is optional but strongly wanted: 3.2.2 says registry slots are "validated
    on bind", and without a dump there is nothing to validate against. Passing None keeps
    the store usable in tests and headless contexts that don't care.
    """

    def __init__(self, db, packdump=None):
        self._db = db
        self._dump = packdump

    def set_packdump(self, packdump):
        """Point at a newly adopted dump (design 3.1).

        The store reads ``_dump`` lazily, at validation time — so there is no derived state
        to rebuild here, only the reference to swap. Every holder of a packdump implements
        this method; see ``MainWindow._rebind_packdump`` for why that is a rule rather than
        a convenience.
        """
        self._dump = packdump

    # --- schemas -----------------------------------------------------------

    def define(self, name: str, description: str = "") -> "Blueprint":
        _check_name("Blueprint", name)
        if self._blueprint_row(name) is not None:
            raise BlueprintError(f"A blueprint named '{name}' already exists")
        self._db.execute("INSERT INTO blueprints (name, description) VALUES (?, ?)",
                         (name, description))
        return self.get(name)

    def all(self) -> list:
        return [self.get(row["name"]) for row in
                self._db.fetch_all("SELECT name FROM blueprints ORDER BY name")]

    def names(self) -> list:
        return [row["name"] for row in
                self._db.fetch_all("SELECT name FROM blueprints ORDER BY name")]

    def id_of(self, name: str) -> int:
        """The surrogate id a job-step binding stores (design 3.2.1's identity table)."""
        return self._require_blueprint(name)["id"]

    def name_of(self, blueprint_id: int) -> str | None:
        """The current name of a blueprint id, or None if it's been deleted."""
        row = self._db.fetch_one("SELECT name FROM blueprints WHERE id = ?",
                                 (blueprint_id,))
        return row["name"] if row else None

    def instance_by_id(self, instance_id: int) -> "Instance | None":
        """The instance an id points at, with its blueprint's *current* name."""
        row = self._db.fetch_one(
            """SELECT i.id AS id, i.name AS name, i.created_by AS created_by,
                      b.name AS blueprint
               FROM blueprint_instances i JOIN blueprints b ON b.id = i.blueprint_id
               WHERE i.id = ?""", (instance_id,))
        if row is None:
            return None
        return Instance(id=row["id"], blueprint=row["blueprint"], name=row["name"],
                        created_by=row["created_by"])

    def get(self, name: str) -> "Blueprint":
        row = self._require_blueprint(name)
        return Blueprint(id=row["id"], name=row["name"], description=row["description"],
                         slots=self.slots(name))

    def rename(self, name: str, new_name: str):
        """Metadata only — the id is the identity, so nothing else moves."""
        row = self._require_blueprint(name)
        _check_name("Blueprint", new_name)
        if name != new_name and self._blueprint_row(new_name) is not None:
            raise BlueprintError(f"A blueprint named '{new_name}' already exists")
        self._db.execute("UPDATE blueprints SET name = ? WHERE id = ?",
                         (new_name, row["id"]))

    def describe(self, name: str, description: str):
        self._db.execute("UPDATE blueprints SET description = ? WHERE id = ?",
                         (description, self._require_blueprint(name)["id"]))

    def delete(self, name: str):
        row = self._require_blueprint(name)
        users = self._db.fetch_all(
            "SELECT DISTINCT b.name AS blueprint FROM blueprint_slots s "
            "JOIN blueprints b ON b.id = s.blueprint_id "
            "WHERE s.ref_blueprint_id = ? AND s.blueprint_id != ?", (row["id"], row["id"]))
        if users:
            # RESTRICT would raise anyway; saying which schemas point here is the useful
            # half, and it stops a delete from silently gutting another blueprint.
            names = ", ".join(sorted(u["blueprint"] for u in users))
            raise BlueprintError(
                f"'{name}' is referenced by slots on: {names}. Remove those slots first.")
        # Its OWN slots may point at it — a self-referencing schema like CladeNode is
        # legal and deliberate (3.2.2). Those go with the blueprint, but the RESTRICT
        # fires before the cascade can reach them, so clear them out first.
        self._db.execute("DELETE FROM blueprint_slots WHERE blueprint_id = ?", (row["id"],))
        self._db.execute("DELETE FROM blueprints WHERE id = ?", (row["id"],))

    # --- slots -------------------------------------------------------------

    def add_group(self, blueprint: str, name: str, *, parent: str = None) -> Slot:
        """A structural node. The user thinks of this as one blueprint with grouped
        fields, not as a separate blueprint (3.2.2)."""
        return self._add_slot(blueprint, name, "group", parent=parent)

    def add_slot(self, blueprint: str, name: str, type: str, *, parent: str = None,
                 registry_type: str = None, ref_blueprint: str = None,
                 enum_values=None) -> Slot:
        """A bindable slot. All slots are strictly typed — no or-types (3.2.2)."""
        if type not in SLOT_TYPES:
            raise BlueprintError(
                f"Unknown slot type '{type}' (expected one of {', '.join(SLOT_TYPES)})")
        if type == "registry" and not registry_type:
            raise BlueprintError(f"Slot '{name}' is a registry slot but names no registry")
        if type == "blueprint" and not ref_blueprint:
            raise BlueprintError(f"Slot '{name}' is a blueprint slot but names no blueprint")
        if type == "enum" and not enum_values:
            raise BlueprintError(f"Enum slot '{name}' declares no values")
        # Drop qualifiers that don't belong to the chosen type. A caller that fills in
        # every field and lets the type decide is behaving reasonably; storing a stray
        # ref_blueprint on a registry slot is not, and it creates a real foreign key that
        # later refuses to let the blueprint be deleted.
        return self._add_slot(blueprint, name, "value", parent=parent, type=type,
                              **_qualifiers(type, registry_type, ref_blueprint,
                                            enum_values))

    def _add_slot(self, blueprint, name, kind, *, parent=None, type=None,
                  registry_type=None, ref_blueprint=None, enum_values=None) -> Slot:
        _check_name("Slot", name)
        blueprint_row = self._require_blueprint(blueprint)
        parent_id = None
        if parent:
            parent_slot = self.slot(blueprint, parent)
            if not parent_slot.is_group:
                raise BlueprintError(
                    f"'{parent}' is a {parent_slot.describe()} slot, not a group — only "
                    f"groups can contain slots")
            parent_id = parent_slot.id

        ref_id = None
        if ref_blueprint:
            # Self-reference is legal and deliberate: 3.2.2 calls a CladeNode whose
            # `descendants` slot is a CladeNode "a legitimate and powerful pattern".
            ref_id = self._require_blueprint(ref_blueprint)["id"]

        siblings = self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM blueprint_slots WHERE blueprint_id = ? "
            "AND parent_id IS ?", (blueprint_row["id"], parent_id))
        if self._sibling(blueprint_row["id"], parent_id, name) is not None:
            where = f"'{parent}'" if parent else f"'{blueprint}'"
            raise BlueprintError(f"{where} already has a slot named '{name}'")

        self._db.execute(
            "INSERT INTO blueprint_slots (blueprint_id, parent_id, name, kind, type, "
            "registry_type, ref_blueprint_id, enum_values, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (blueprint_row["id"], parent_id, name, kind, type, registry_type, ref_id,
             json.dumps(list(enum_values)) if enum_values else None, siblings["n"]))
        return self.slot(blueprint, f"{parent}.{name}" if parent else name)

    def slots(self, blueprint: str) -> list:
        """Every slot, parents before children, in declaration order within each level."""
        blueprint_row = self._require_blueprint(blueprint)
        rows = self._db.fetch_all(
            "SELECT s.*, r.name AS ref_name FROM blueprint_slots s "
            "LEFT JOIN blueprints r ON r.id = s.ref_blueprint_id "
            "WHERE s.blueprint_id = ? ORDER BY s.position, s.id", (blueprint_row["id"],))
        by_parent = {}
        for row in rows:
            by_parent.setdefault(row["parent_id"], []).append(row)

        ordered = []

        def walk(parent_id, prefix, depth):
            for row in by_parent.get(parent_id, []):
                path = f"{prefix}.{row['name']}" if prefix else row["name"]
                ordered.append(self._slot(row, blueprint_row["name"], path, depth))
                walk(row["id"], path, depth + 1)

        walk(None, "", 0)
        return ordered

    def slot(self, blueprint: str, path: str) -> Slot:
        for candidate in self.slots(blueprint):
            if candidate.path == path:
                return candidate
        raise BlueprintError(f"'{blueprint}' has no slot '{path}'")

    def value_slots(self, blueprint: str) -> list:
        return [s for s in self.slots(blueprint) if not s.is_group]

    def move_slot(self, blueprint: str, path: str, *, parent=_KEEP, position: int = None):
        """Reorder a slot among its siblings and/or move it under a different group.

        Non-destructive, in the same sense 3.2.2 gives ``rename``: bindings are keyed by
        slot **id**, so they follow the slot wherever it goes and nothing is orphaned. The
        slot's *path* changes, which matters only to code that names it as a string.

        ``parent`` left alone keeps the current one; ``None`` means the top level.
        """
        slot = self.slot(blueprint, path)
        blueprint_row = self._require_blueprint(blueprint)

        if parent is _KEEP:
            parent_id, parent_path = slot.parent_id, (
                slot.path.rsplit(".", 1)[0] if "." in slot.path else None)
        elif parent is None:
            parent_id, parent_path = None, None
        else:
            target = self.slot(blueprint, parent)
            if not target.is_group:
                raise BlueprintError(
                    f"'{parent}' is a {target.describe()} slot, not a group — only groups "
                    f"can contain slots")
            parent_id, parent_path = target.id, target.path

        # A group cannot become its own descendant; the tree would stop being a tree.
        forbidden = {s.id for s in self._descendants(blueprint, slot)} | {slot.id}
        if parent_id in forbidden:
            raise BlueprintError(f"Can't move '{path}' inside itself")

        clash = self._sibling(blueprint_row["id"], parent_id, slot.name)
        if clash is not None and clash["id"] != slot.id:
            where = f"'{parent_path}'" if parent_path else f"'{blueprint}'"
            raise BlueprintError(f"{where} already has a slot named '{slot.name}'")

        siblings = [row["id"] for row in self._db.fetch_all(
            "SELECT id FROM blueprint_slots WHERE blueprint_id = ? AND parent_id IS ? "
            "ORDER BY position, id", (blueprint_row["id"], parent_id))
            if row["id"] != slot.id]
        index = len(siblings) if position is None else max(0, min(position, len(siblings)))
        siblings.insert(index, slot.id)

        self._db.execute("UPDATE blueprint_slots SET parent_id = ? WHERE id = ?",
                         (parent_id, slot.id))
        # Renumber the whole sibling run rather than nudging one row: positions are only
        # meaningful relative to each other, and gaps accumulate into ties.
        for order, slot_id in enumerate(siblings):
            self._db.execute("UPDATE blueprint_slots SET position = ? WHERE id = ?",
                             (order, slot_id))

    def duplicate_slot(self, blueprint: str, path: str, new_name: str,
                       *, parent: str = None) -> Slot:
        """Clone a slot — or a whole group and everything nested under it — under a new
        name.

        A stone palette is a *cartesian product*: every cut wants the same set of forms.
        Duplicating a group is how the second cut costs two clicks instead of five dialogs,
        and it is additive, so nothing is orphaned and existing instances simply gain the
        new slots unset (the same rule as adding one by hand).
        """
        source = self.slot(blueprint, path)
        if parent is None:
            parent = source.path.rsplit(".", 1)[0] if "." in source.path else None
        new_root = f"{parent}.{new_name}" if parent else new_name
        if f"{new_root}." .startswith(f"{source.path}."):
            raise BlueprintError(f"Can't duplicate '{path}' into itself")

        created = None
        # _descendants comes back parents-before-children, so each target parent exists by
        # the time its children are made.
        for slot in [source] + self._descendants(blueprint, source):
            if slot.id == source.id:
                target_parent, name = parent, new_name
            else:
                below = slot.path[len(source.path) + 1:]
                head = below.rsplit(".", 1)[0] if "." in below else ""
                target_parent = f"{new_root}.{head}" if head else new_root
                name = slot.name
            if slot.is_group:
                made = self.add_group(blueprint, name, parent=target_parent)
            else:
                made = self.add_slot(
                    blueprint, name, slot.type, parent=target_parent,
                    registry_type=slot.registry_type, ref_blueprint=slot.ref_blueprint,
                    enum_values=list(slot.enum_values) or None)
            created = created or made
        return created

    def rename_slot(self, blueprint: str, path: str, new_name: str):
        """Bindings migrate automatically — this is metadata only, not remove-plus-add
        (3.2.2), which the surrogate slot id is what makes true."""
        _check_name("Slot", new_name)
        slot = self.slot(blueprint, path)
        blueprint_row = self._require_blueprint(blueprint)
        clash = self._sibling(blueprint_row["id"], slot.parent_id, new_name)
        if clash is not None and clash["id"] != slot.id:
            raise BlueprintError(f"'{blueprint}' already has a slot named '{new_name}' here")
        self._db.execute("UPDATE blueprint_slots SET name = ? WHERE id = ?",
                         (new_name, slot.id))

    def preview_remove_slot(self, blueprint: str, path: str) -> Impact:
        bound = self._bound_instances(blueprint, self._subtree(blueprint, path))
        return Impact(kind="remove_slot", blueprint=blueprint, slot_path=path,
                      instances=len(self.instances(blueprint)), bound=tuple(bound),
                      destructive=bool(bound), blocked=self._mutation_block(blueprint))

    def remove_slot(self, blueprint: str, path: str) -> Impact:
        """Remove a slot (and anything nested under it).

        3.2.2: "If zero instances have a binding for the slot, it is removed silently — no
        confirmation, no orphaning. If any instance has a binding, every instance of the
        blueprint that had a binding becomes orphaned."
        """
        impact = self.preview_remove_slot(blueprint, path)
        self._require_no_block(impact)
        subtree = self._subtree(blueprint, path)

        if not impact.destructive:
            self._db.execute("DELETE FROM blueprint_slots WHERE id = ?", (subtree[0].id,))
            return impact

        mutation_id = self._record_mutation(blueprint, "remove_slot", path, subtree)
        # Displaced bindings go to limbo BEFORE the slot rows are deleted, or the FK
        # cascade would take them with it and "Preserve" would have nothing to preserve.
        self._to_limbo(subtree, mutation_id)
        self._db.execute("DELETE FROM blueprint_slots WHERE id = ?", (subtree[0].id,))
        self._orphan(blueprint, impact.bound, mutation_id)
        return impact

    def preview_retype_slot(self, blueprint: str, path: str, type: str, *,
                            registry_type: str = None, ref_blueprint: str = None,
                            enum_values=None) -> Impact:
        """Can every existing binding survive the new type?

        3.2.2 offers auto-coerce "only when Packsmith can *prove* the coercion is
        lossless". The proof needs no table of which type converts to which: run every
        existing binding through the *new* slot's own validator. If they all pass, the
        coercion is lossless by construction, and it stays correct as types are added.
        """
        slot = self._require_value_slot(blueprint, path)
        proposed = self._proposed(slot, type, registry_type, ref_blueprint, enum_values)
        bound, problematic = [], []
        for instance, binding in self._bindings_to(slot):
            bound.append(instance)
            try:
                self._validate(proposed, self._decode(slot, binding["value"]))
            except BlueprintError:
                problematic.append((instance, binding["value"]))
        return Impact(kind="retype_slot", blueprint=blueprint, slot_path=path,
                      instances=len(self.instances(blueprint)), bound=tuple(bound),
                      destructive=bool(bound), can_coerce=bool(bound) and not problematic,
                      problematic=tuple(problematic),
                      blocked=self._mutation_block(blueprint))

    def retype_slot(self, blueprint: str, path: str, type: str, *,
                    registry_type: str = None, ref_blueprint: str = None,
                    enum_values=None, auto_coerce: bool = False) -> Impact:
        """Change a slot's type. Orphans by default; ``auto_coerce`` is only honoured when
        the change is provably lossless (3.2.2 — "default and strictness are preserved")."""
        impact = self.preview_retype_slot(blueprint, path, type,
                                          registry_type=registry_type,
                                          ref_blueprint=ref_blueprint,
                                          enum_values=enum_values)
        self._require_no_block(impact)
        if auto_coerce and not impact.can_coerce:
            raise BlueprintError(
                f"'{path}' can't be retyped without loss: "
                + ", ".join(f"{i} = {v!r}" for i, v in impact.problematic[:5]))

        slot = self.slot(blueprint, path)
        proposed = self._proposed(slot, type, registry_type, ref_blueprint, enum_values)
        mutation_id = None
        if impact.destructive and not auto_coerce:
            mutation_id = self._record_mutation(blueprint, "retype_slot", path, [slot])

        # Rewrite what survives, displace what doesn't. Values are re-encoded because a
        # stored form is type-specific — number 3 is "3.0", the same 3 as a string is "3".
        #
        # ...unless the user picked **Retype & Orphan**, which 3.2.2 defines as "orphan all
        # instances anyway (explicit user choice to re-bind manually)". Converting the
        # survivors there contradicts the choice AND the label: the instance is orphaned
        # for the user to re-bind, but there is nothing left to re-bind and the originals
        # are gone. Displacing everything into limbo keeps the values recoverable and makes
        # "re-bind manually" mean something.
        orphaning = mutation_id is not None
        for instance, binding in self._bindings_to(slot):
            try:
                if orphaning:
                    raise BlueprintError("retype & orphan: displace rather than convert")
                restored = self._validate(proposed, self._decode(slot, binding["value"]))
            except BlueprintError:
                self._db.execute(
                    "INSERT INTO blueprint_limbo (instance_id, slot_path, value, owner, "
                    "action_ref, mutation_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (binding["instance_id"], path, binding["value"], binding["owner"],
                     binding["action_ref"], mutation_id))
                self._db.execute(
                    "DELETE FROM instance_bindings WHERE instance_id = ? AND slot_id = ?",
                    (binding["instance_id"], slot.id))
                continue
            self._db.execute(
                "UPDATE instance_bindings SET value = ? WHERE instance_id = ? AND slot_id = ?",
                (restored, binding["instance_id"], slot.id))

        self._apply_type(slot.id, proposed)
        if mutation_id is not None:
            self._orphan(blueprint, impact.bound, mutation_id)
        return impact

    def preview_rename_slot(self, blueprint: str, path: str) -> Impact:
        """Non-destructive, but 3.2.2 still wants the impact shown — "this will update 42
        existing instances. The old name will no longer exist.\""""
        return Impact(kind="rename_slot", blueprint=blueprint, slot_path=path,
                      instances=len(self.instances(blueprint)),
                      bound=tuple(self._bound_instances(
                          blueprint, self._subtree(blueprint, path))))

    def preview_add_slot(self, blueprint: str, name: str) -> Impact:
        """Silent propagation; the notification is the only visible side effect."""
        return Impact(kind="add_slot", blueprint=blueprint, slot_path=name,
                      instances=len(self.instances(blueprint)))

    # --- orphans and their resolutions -------------------------------------

    def orphans(self, blueprint: str = None) -> list:
        """Every instance currently locked out of actions."""
        sql = ("SELECT i.name AS instance, b.name AS blueprint, m.kind, m.slot_path, "
               "i.id AS instance_id, i.orphaned_by FROM blueprint_instances i "
               "JOIN blueprints b ON b.id = i.blueprint_id "
               "JOIN blueprint_mutations m ON m.id = i.orphaned_by "
               "WHERE i.orphaned_by IS NOT NULL")
        params = ()
        if blueprint is not None:
            sql += " AND b.name = ?"
            params = (blueprint,)
        found = []
        for row in self._db.fetch_all(sql + " ORDER BY b.name, i.name", params):
            waiting = self._db.fetch_all(
                "SELECT slot_path FROM blueprint_limbo WHERE instance_id = ? "
                "AND mutation_id = ? ORDER BY slot_path",
                (row["instance_id"], row["orphaned_by"]))
            found.append(Orphan(blueprint=row["blueprint"], instance=row["instance"],
                                reason=row["kind"], slot_path=row["slot_path"],
                                problematic=tuple(w["slot_path"] for w in waiting)))
        return found

    def has_orphans(self, blueprint: str) -> bool:
        """3.2.2: "any action that touches a blueprint with orphaned instances refuses to
        run until the orphans are resolved.\""""
        return bool(self._db.fetch_one(
            "SELECT 1 FROM blueprint_instances i JOIN blueprints b ON b.id = i.blueprint_id "
            "WHERE b.name = ? AND i.orphaned_by IS NOT NULL LIMIT 1", (blueprint,)))

    def discard(self, blueprint: str, instance: str):
        """Delete the orphaned bindings permanently and un-orphan. Data tied to removed or
        retyped slots is lost — that is the point of the option."""
        row = self.instance(blueprint, instance)
        mutation_id = self._mutation_id_for(row.id)
        self._db.execute("DELETE FROM blueprint_limbo WHERE instance_id = ?", (row.id,))
        self._unorphan(row.id)
        self._settle(mutation_id)

    def preserve(self, blueprint: str, instance: str):
        """Un-orphan but keep the displaced bindings in limbo — retained in the database,
        never exposed to actions, in case the slot comes back."""
        row = self.instance(blueprint, instance)
        mutation_id = self._mutation_id_for(row.id)
        self._unorphan(row.id)
        self._settle(mutation_id)

    def revert(self, blueprint: str):
        """Undo the schema mutation that caused the orphaning. Every instance of the
        blueprint snaps back to the pre-mutation state — blueprint-wide, not per-instance,
        because a half-reverted schema isn't a state that means anything."""
        mutation = self._db.fetch_one(
            "SELECT m.* FROM blueprint_mutations m JOIN blueprints b ON b.id = m.blueprint_id "
            "WHERE b.name = ? ORDER BY m.id DESC LIMIT 1", (blueprint,))
        if mutation is None:
            raise BlueprintError(f"'{blueprint}' has no schema change to revert")
        snapshot = json.loads(mutation["snapshot"])

        if mutation["kind"] == "remove_slot":
            self._restore_slots(blueprint, snapshot["slots"])
        else:
            self._apply_type(self._restore_target(blueprint, snapshot), snapshot["slots"][0])

        by_path = {s.path: s.id for s in self.slots(blueprint)}
        for row in self._db.fetch_all(
                "SELECT * FROM blueprint_limbo WHERE mutation_id = ?", (mutation["id"],)):
            slot_id = by_path.get(row["slot_path"])
            if slot_id is not None:
                self._db.execute(
                    "INSERT INTO instance_bindings (instance_id, slot_id, value, owner, "
                    "action_ref) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(instance_id, slot_id) DO UPDATE SET value = excluded.value",
                    (row["instance_id"], slot_id, row["value"], row["owner"],
                     row["action_ref"]))
        for row in self._db.fetch_all(
                "SELECT id FROM blueprint_instances WHERE orphaned_by = ?",
                (mutation["id"],)):
            self._unorphan(row["id"])
        self._db.execute("DELETE FROM blueprint_limbo WHERE mutation_id = ?",
                         (mutation["id"],))
        self._db.execute("DELETE FROM blueprint_mutations WHERE id = ?", (mutation["id"],))

    def rebind(self, blueprint: str, instance: str, path: str, value, *,
               owner: str = "user", action_ref: str = None):
        """Re-bind a problematic value under the new type. "Once all problematic bindings
        are re-bound, the instance un-orphans automatically" — retype orphans only."""
        row = self.instance(blueprint, instance)
        mutation_id = self._mutation_id_for(row.id)
        self.bind(blueprint, instance, path, value, owner=owner, action_ref=action_ref)
        self._db.execute(
            "DELETE FROM blueprint_limbo WHERE instance_id = ? AND slot_path = ?",
            (row.id, path))
        remaining = self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM blueprint_limbo WHERE instance_id = ?", (row.id,))
        if not remaining["n"]:
            self._unorphan(row.id)
            self._settle(mutation_id)

    def limbo(self, blueprint: str, instance: str) -> list:
        """Preserved-but-hidden bindings. Deliberately never returned by
        :meth:`bindings` — "not exposed to actions" is structural here, not a flag."""
        row = self.instance(blueprint, instance)
        return [Binding(slot_path=r["slot_path"], value=r["value"], owner=r["owner"],
                        action_ref=r["action_ref"])
                for r in self._db.fetch_all(
                    "SELECT * FROM blueprint_limbo WHERE instance_id = ? ORDER BY slot_path",
                    (row.id,))]

    # --- evolution internals -----------------------------------------------

    def _subtree(self, blueprint: str, path: str) -> list:
        slot = self.slot(blueprint, path)
        return [slot] + self._descendants(blueprint, slot)

    def _descendants(self, blueprint: str, slot: Slot) -> list:
        prefix = slot.path + "."
        return [s for s in self.slots(blueprint) if s.path.startswith(prefix)]

    def _bound_instances(self, blueprint: str, subtree: list) -> list:
        ids = [s.id for s in subtree]
        if not ids:
            return []
        rows = self._db.fetch_all(
            f"SELECT DISTINCT i.name FROM instance_bindings ib "
            f"JOIN blueprint_instances i ON i.id = ib.instance_id "
            f"WHERE ib.slot_id IN ({','.join('?' * len(ids))}) ORDER BY i.name", tuple(ids))
        return [row["name"] for row in rows]

    def _bindings_to(self, slot: Slot) -> list:
        return [(row["name"], row) for row in self._db.fetch_all(
            "SELECT ib.*, i.name FROM instance_bindings ib "
            "JOIN blueprint_instances i ON i.id = ib.instance_id "
            "WHERE ib.slot_id = ? ORDER BY i.name", (slot.id,))]

    def _proposed(self, slot: Slot, type, registry_type, ref_blueprint, enum_values) -> Slot:
        if type not in SLOT_TYPES:
            raise BlueprintError(f"Unknown slot type '{type}'")
        if type == "registry" and not registry_type:
            raise BlueprintError(f"Retyping '{slot.path}' to registry names no registry")
        if type == "blueprint" and not ref_blueprint:
            raise BlueprintError(f"Retyping '{slot.path}' to blueprint names no blueprint")
        if type == "enum" and not enum_values:
            raise BlueprintError(f"Retyping '{slot.path}' to enum declares no values")
        clean = _qualifiers(type, registry_type, ref_blueprint, enum_values)
        return Slot(id=slot.id, blueprint=slot.blueprint, name=slot.name, path=slot.path,
                    kind="value", type=type,
                    registry_type=clean["registry_type"],
                    ref_blueprint=clean["ref_blueprint"],
                    enum_values=tuple(clean["enum_values"] or ()),
                    parent_id=slot.parent_id, depth=slot.depth)

    def _apply_type(self, slot_id: int, proposed):
        spec = proposed if isinstance(proposed, dict) else {
            "type": proposed.type, "registry_type": proposed.registry_type,
            "ref_blueprint": proposed.ref_blueprint,
            "enum_values": list(proposed.enum_values)}
        ref_id = None
        if spec.get("ref_blueprint"):
            ref_id = self._require_blueprint(spec["ref_blueprint"])["id"]
        self._db.execute(
            "UPDATE blueprint_slots SET type = ?, registry_type = ?, ref_blueprint_id = ?, "
            "enum_values = ? WHERE id = ?",
            (spec["type"], spec.get("registry_type"), ref_id,
             json.dumps(spec["enum_values"]) if spec.get("enum_values") else None, slot_id))

    def _record_mutation(self, blueprint: str, kind: str, path: str, subtree: list) -> int:
        blueprint_row = self._require_blueprint(blueprint)
        snapshot = {"slots": [{
            "path": s.path, "name": s.name, "kind": s.kind, "type": s.type,
            "registry_type": s.registry_type, "ref_blueprint": s.ref_blueprint,
            "enum_values": list(s.enum_values),
            "parent_path": s.path.rsplit(".", 1)[0] if "." in s.path else None,
            "position": self._position_of(s.id),
        } for s in subtree]}
        cursor = self._db.execute(
            "INSERT INTO blueprint_mutations (blueprint_id, kind, slot_path, snapshot, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (blueprint_row["id"], kind, path, json.dumps(snapshot),
             datetime.now(timezone.utc).isoformat()))
        return cursor.lastrowid

    def _to_limbo(self, subtree: list, mutation_id: int):
        for slot in subtree:
            for _instance, binding in self._bindings_to(slot):
                self._db.execute(
                    "INSERT INTO blueprint_limbo (instance_id, slot_path, value, owner, "
                    "action_ref, mutation_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (binding["instance_id"], slot.path, binding["value"], binding["owner"],
                     binding["action_ref"], mutation_id))

    def _restore_slots(self, blueprint: str, slots: list) -> int:
        blueprint_row = self._require_blueprint(blueprint)
        for spec in slots:
            parent_id = None
            if spec["parent_path"]:
                parent_id = self.slot(blueprint, spec["parent_path"]).id
            ref_id = None
            if spec["ref_blueprint"]:
                ref_id = self._require_blueprint(spec["ref_blueprint"])["id"]
            self._db.execute(
                "INSERT INTO blueprint_slots (blueprint_id, parent_id, name, kind, type, "
                "registry_type, ref_blueprint_id, enum_values, position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (blueprint_row["id"], parent_id, spec["name"], spec["kind"], spec["type"],
                 spec["registry_type"], ref_id,
                 json.dumps(spec["enum_values"]) if spec["enum_values"] else None,
                 spec.get("position", 0)))
        return 0

    def _position_of(self, slot_id: int) -> int:
        row = self._db.fetch_one("SELECT position FROM blueprint_slots WHERE id = ?",
                                 (slot_id,))
        return row["position"] if row else 0

    def _restore_target(self, blueprint: str, snapshot: dict) -> int:
        return self.slot(blueprint, snapshot["slots"][0]["path"]).id

    def _orphan(self, blueprint: str, instances, mutation_id: int):
        for name in instances:
            self._db.execute("UPDATE blueprint_instances SET orphaned_by = ? WHERE id = ?",
                             (mutation_id, self.instance(blueprint, name).id))

    def _unorphan(self, instance_id: int):
        self._db.execute("UPDATE blueprint_instances SET orphaned_by = NULL WHERE id = ?",
                         (instance_id,))

    def _mutation_id_for(self, instance_id: int):
        row = self._db.fetch_one(
            "SELECT orphaned_by FROM blueprint_instances WHERE id = ?", (instance_id,))
        return row["orphaned_by"] if row else None

    def _settle(self, mutation_id):
        """A mutation nobody is still orphaned by has been fully resolved, so it stops
        being revertible. Keeping it would let "one step back" reach past a decision the
        user already made."""
        if mutation_id is None:
            return
        still = self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM blueprint_instances WHERE orphaned_by = ?",
            (mutation_id,))
        if not still["n"]:
            self._db.execute("DELETE FROM blueprint_mutations WHERE id = ?", (mutation_id,))

    def _mutation_block(self, blueprint: str):
        if self.has_orphans(blueprint):
            return (f"'{blueprint}' has unresolved orphaned instances. Resolve them "
                    f"before changing the schema again — reverting only goes one step "
                    f"back.")
        return None

    @staticmethod
    def _require_no_block(impact: Impact):
        if impact.blocked:
            raise BlueprintError(impact.blocked)

    def _require_value_slot(self, blueprint: str, path: str) -> Slot:
        slot = self.slot(blueprint, path)
        if slot.is_group:
            raise BlueprintError(f"'{path}' is a group — groups have no type to change")
        return slot

    # --- instances ---------------------------------------------------------

    def create_instance(self, blueprint: str, name: str, *, created_by: str = None) -> Instance:
        _check_name("Instance", name)
        blueprint_row = self._require_blueprint(blueprint)
        if self._instance_row(blueprint_row["id"], name) is not None:
            raise BlueprintError(f"'{blueprint}' already has an instance '{name}'")
        self._db.execute(
            "INSERT INTO blueprint_instances (blueprint_id, name, created_by) "
            "VALUES (?, ?, ?)", (blueprint_row["id"], name, created_by))
        return self.instance(blueprint, name)

    def instances(self, blueprint: str) -> list:
        blueprint_row = self._require_blueprint(blueprint)
        return [Instance(id=row["id"], blueprint=blueprint, name=row["name"],
                         created_by=row["created_by"])
                for row in self._db.fetch_all(
                    "SELECT * FROM blueprint_instances WHERE blueprint_id = ? "
                    "ORDER BY name", (blueprint_row["id"],))]

    def instance(self, blueprint: str, name: str) -> Instance:
        blueprint_row = self._require_blueprint(blueprint)
        row = self._instance_row(blueprint_row["id"], name)
        if row is None:
            raise BlueprintError(f"'{blueprint}' has no instance '{name}'")
        return Instance(id=row["id"], blueprint=blueprint, name=row["name"],
                        created_by=row["created_by"])

    def rename_instance(self, blueprint: str, name: str, new_name: str):
        _check_name("Instance", new_name)
        instance = self.instance(blueprint, name)
        blueprint_row = self._require_blueprint(blueprint)
        clash = self._instance_row(blueprint_row["id"], new_name)
        if clash is not None and clash["id"] != instance.id:
            raise BlueprintError(f"'{blueprint}' already has an instance '{new_name}'")
        self._db.execute("UPDATE blueprint_instances SET name = ? WHERE id = ?",
                         (new_name, instance.id))

    def delete_instance(self, blueprint: str, name: str):
        instance = self.instance(blueprint, name)
        referrers = self._db.fetch_all(
            "SELECT b.name AS blueprint, i.name AS instance FROM instance_bindings ib "
            "JOIN blueprint_slots s ON s.id = ib.slot_id "
            "JOIN blueprint_instances i ON i.id = ib.instance_id "
            "JOIN blueprints b ON b.id = i.blueprint_id "
            "WHERE s.type = 'blueprint' AND ib.value = ?", (str(instance.id),))
        if referrers:
            where = ", ".join(sorted(f"{r['blueprint']}:{r['instance']}" for r in referrers))
            raise BlueprintError(
                f"'{instance.ref}' is bound into: {where}. Unbind it there first.")
        self._db.execute("DELETE FROM blueprint_instances WHERE id = ?", (instance.id,))

    # --- bindings ----------------------------------------------------------

    def bind(self, blueprint: str, instance: str, path: str, value, *,
             owner: str = "user", action_ref: str = None):
        """Bind a value into a slot, validating it against the slot's type."""
        slot = self.slot(blueprint, path)
        if slot.is_group:
            raise BlueprintError(f"'{path}' is a group — groups hold slots, not values")
        stored = self._validate(slot, value)
        row = self.instance(blueprint, instance)
        if slot.type == "blueprint":
            self._refuse_cycle(row.id, int(stored))
        self._db.execute(
            "INSERT INTO instance_bindings (instance_id, slot_id, value, owner, action_ref) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(instance_id, slot_id) DO UPDATE SET "
            "value = excluded.value, owner = excluded.owner, action_ref = excluded.action_ref",
            (row.id, slot.id, stored, owner, action_ref))

    # Bindings that point at something no longer present (design 3.2.2 Live Questions —
    # resolved by the doc's own steer: "this should probably mirror the tag orphan-detection
    # operation so the two primitives stay symmetric"). So, exactly like `find_orphans` on
    # tags: **derived, never stored, so it can't go stale**, and surfaced rather than
    # locking. That last part is the symmetry that matters — a vanished registry entry is
    # the *packdump's* doing, not a schema decision the user made, and tag orphans don't
    # lock either. Instance orphaning stays reserved for destructive schema edits, which
    # are the user's own act.
    def find_binding_orphans(self, packdump) -> list:
        """Every binding pointing at a registry entry or instance that no longer exists."""
        # Blueprint slots store the target's ID, so "does it still exist" is an id lookup.
        labels = self._instance_labels()
        orphans = []
        for name in self.names():
            slots = {s.path: s for s in self.value_slots(name)}
            for instance in self.instances(name):
                for path, binding in self.bindings(name, instance.name).items():
                    slot = slots.get(path)
                    if slot is None:
                        continue
                    if slot.type == "registry":
                        registry = (packdump.registry.get(slot.registry_type)
                                    if packdump is not None else None)
                        if registry is None or binding.value not in registry["values"]:
                            orphans.append(BindingOrphan(
                                name, instance.name, path, binding.value,
                                slot.registry_type, "missing_entry"))
                    elif slot.type == "blueprint":
                        try:
                            target = int(binding.value)
                        except (TypeError, ValueError):
                            target = None
                        if target not in labels:
                            orphans.append(BindingOrphan(
                                name, instance.name, path, binding.value,
                                slot.ref_blueprint, "missing_instance"))
        return orphans

    # === INSTANCE CYCLES (design 3.2.2 Live Questions — resolved: reject at bind time) ===
    #
    # SCHEMA-level cycles stay legal: a `CladeNode` with a `descendants` slot of type
    # `CladeNode` is the example 3.2.2 gives, and a type referring to itself is fine.
    # INSTANCE-level cycles are refused, because a reference is a pointer and a loop of
    # pointers has no bottom. Nothing dereferences them *yet* — Deref is up the Power
    # Ladder and the Node View is unbuilt — which is exactly why now is the time: refusing
    # at the edge means every future traversal gets to assume termination instead of
    # carrying a visited-set forever, and the alternative is discovering the loop in a
    # renderer that hangs.

    def _instance_labels(self) -> dict:
        """``{instance_id: "Blueprint:name"}`` — ids are identity, labels are for humans."""
        return {row["id"]: f"{row['blueprint']}:{row['name']}"
                for row in self._db.fetch_all(
                    """SELECT i.id AS id, i.name AS name, b.name AS blueprint
                       FROM blueprint_instances i
                       JOIN blueprints b ON b.id = i.blueprint_id""")}

    def _reference_edges(self) -> dict:
        """``{instance_id: [instance_id, ...]}`` over blueprint-typed bindings only.

        Keyed by **id**, not by name: a blueprint slot stores the target's id precisely so
        renaming the instance it points at doesn't break the pointer, so the reachability
        graph has to use the same identity or it silently matches nothing.
        """
        edges = {}
        for row in self._db.fetch_all(
                """SELECT ib.instance_id AS source, ib.value AS target
                   FROM instance_bindings ib
                   JOIN blueprint_slots s ON s.id = ib.slot_id
                   WHERE s.type = 'blueprint'"""):
            try:
                edges.setdefault(row["source"], []).append(int(row["target"]))
            except (TypeError, ValueError):
                continue        # not an id; find_binding_orphans reports it
        return edges

    def reference_cycle(self, source_id: int, target_id: int) -> list:
        """The instance ids proving that pointing ``source`` at ``target`` closes a loop,
        or [] if it doesn't. Returned rather than raised so callers can preview."""
        if target_id == source_id:
            return [source_id, source_id]
        edges = self._reference_edges()
        # Depth-first from the target: if the source is reachable, adding this edge makes
        # it a cycle. `seen` also protects the search from cycles that already exist.
        stack = [(target_id, [source_id, target_id])]
        seen = {target_id}
        while stack:
            node, path = stack.pop()
            for nxt in edges.get(node, ()):
                if nxt == source_id:
                    return path + [nxt]
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, path + [nxt]))
        return []

    def cycles(self) -> list:
        """Every instance cycle currently in the database, as readable ref paths.

        Bind-time refusal keeps new ones out; this catches any that predate the rule or
        arrive some other way, so the Errors panel can say so instead of a traversal
        hanging.
        """
        edges = self._reference_edges()
        labels = self._instance_labels()
        found, seen = [], set()
        for start in edges:
            stack = [(start, [start])]
            walked = {start}
            while stack:
                node, path = stack.pop()
                for nxt in edges.get(node, ()):
                    if nxt == start:
                        key = frozenset(path)
                        if key not in seen:
                            seen.add(key)
                            found.append([labels.get(i, str(i)) for i in path + [nxt]])
                    elif nxt not in walked:
                        walked.add(nxt)
                        stack.append((nxt, path + [nxt]))
        return found

    def _refuse_cycle(self, source_id: int, target_id: int):
        path = self.reference_cycle(source_id, target_id)
        if path:
            labels = self._instance_labels()
            raise BlueprintError(
                "that would create a cycle: "
                + " → ".join(labels.get(i, str(i)) for i in path))

    def unbind(self, blueprint: str, instance: str, path: str):
        slot = self.slot(blueprint, path)
        row = self.instance(blueprint, instance)
        self._db.execute(
            "DELETE FROM instance_bindings WHERE instance_id = ? AND slot_id = ?",
            (row.id, slot.id))

    def claim(self, blueprint: str, instance: str, path: str) -> "Binding":
        """Take an action-managed binding into user ownership, value unchanged.

        Distinct from re-``bind``-ing the same value on purpose: this says nothing about
        *what* is bound, only about **who owns it**, so it can't fail validation and can't
        quietly rewrite a value while claiming it. Ownership is per binding (design 3.2.2),
        which is what makes "this one cell is mine, the rest of the column is generated" a
        representable state.
        """
        binding = self.bindings(blueprint, instance).get(path)
        if binding is None:
            raise BlueprintError(f"'{instance}.{path}' has nothing bound to claim")
        if binding.owner == "user":
            return binding
        slot = self.slot(blueprint, path)
        row = self.instance(blueprint, instance)
        self._db.execute(
            "UPDATE instance_bindings SET owner = 'user', action_ref = NULL "
            "WHERE instance_id = ? AND slot_id = ?", (row.id, slot.id))
        return Binding(slot_path=path, value=binding.value, owner="user", action_ref=None)

    def bindings(self, blueprint: str, instance: str) -> dict:
        """``{slot_path: Binding}`` for everything currently bound."""
        row = self.instance(blueprint, instance)
        by_id = {s.id: s for s in self.slots(blueprint)}
        result = {}
        for binding in self._db.fetch_all(
                "SELECT * FROM instance_bindings WHERE instance_id = ?", (row.id,)):
            slot = by_id.get(binding["slot_id"])
            if slot is None:
                continue
            result[slot.path] = Binding(slot_path=slot.path, value=binding["value"],
                                        owner=binding["owner"],
                                        action_ref=binding["action_ref"])
        return result

    def value_of(self, blueprint: str, instance: str, path: str):
        """The bound value, decoded to a Python value, or None when unset."""
        binding = self.bindings(blueprint, instance).get(path)
        if binding is None:
            return None
        return self._decode(self.slot(blueprint, path), binding.value)

    def gaps(self, blueprint: str, instance: str) -> list:
        """Slot paths with no binding — "empty slots = missing content" (3.2.2). The
        whole point of the primitive, so it gets a name rather than being a comprehension
        every caller rewrites."""
        bound = set(self.bindings(blueprint, instance))
        return [s.path for s in self.value_slots(blueprint) if s.path not in bound]

    # --- validation --------------------------------------------------------

    def _validate(self, slot: Slot, value) -> str:
        """Check a value against its slot's type and return its stored form."""
        if value is None:
            raise BlueprintError(f"'{slot.path}' cannot be bound to nothing — unbind it "
                                 f"instead")
        if slot.type == "registry":
            entry = str(value)
            if self._dump is not None and not self._has_entry(slot.registry_type, entry):
                raise BlueprintError(
                    f"'{entry}' is not in {slot.registry_type} — '{slot.path}' only "
                    f"accepts entries from that registry")
            return entry
        if slot.type == "blueprint":
            return str(self._resolve_instance(slot, value))
        if slot.type == "enum":
            text = str(value)
            if text not in slot.enum_values:
                raise BlueprintError(
                    f"'{text}' is not a value of '{slot.path}' "
                    f"({', '.join(slot.enum_values)})")
            return text
        if slot.type == "bool":
            return "true" if _as_bool(value, slot) else "false"
        if slot.type == "number":
            try:
                return repr(float(value))
            except (TypeError, ValueError):
                raise BlueprintError(f"'{value}' is not a number for '{slot.path}'")
        return str(value)

    def _decode(self, slot: Slot, stored: str):
        if slot.type == "bool":
            return stored == "true"
        if slot.type == "number":
            number = float(stored)
            return int(number) if number.is_integer() else number
        if slot.type == "blueprint":
            row = self._db.fetch_one(
                "SELECT i.name AS instance, b.name AS blueprint "
                "FROM blueprint_instances i JOIN blueprints b ON b.id = i.blueprint_id "
                "WHERE i.id = ?", (int(stored),))
            return f"{row['blueprint']}:{row['instance']}" if row else None
        return stored

    def _resolve_instance(self, slot: Slot, value) -> int:
        """Blueprint slots store the instance's ID, so renaming the instance it points at
        doesn't break the pointer — 3.2.2's "this is a pointer, not a copy"."""
        if isinstance(value, Instance):
            name = value.name
            if value.blueprint != slot.ref_blueprint:
                raise BlueprintError(
                    f"'{slot.path}' takes a {slot.ref_blueprint}, not a {value.blueprint}")
        else:
            name = str(value)
            if ":" in name:
                blueprint_name, name = name.split(":", 1)
                if blueprint_name != slot.ref_blueprint:
                    raise BlueprintError(
                        f"'{slot.path}' takes a {slot.ref_blueprint}, not a {blueprint_name}")
        try:
            return self.instance(slot.ref_blueprint, name).id
        except BlueprintError:
            raise BlueprintError(
                f"'{slot.ref_blueprint}' has no instance '{name}' to bind into "
                f"'{slot.path}'")

    def _has_entry(self, registry_type: str, entry_id: str) -> bool:
        registry = self._dump.registry.get(registry_type)
        return bool(registry) and entry_id in registry.get("values", [])

    # --- rows --------------------------------------------------------------

    def _blueprint_row(self, name: str):
        return self._db.fetch_one("SELECT * FROM blueprints WHERE name = ?", (name,))

    def _require_blueprint(self, name: str):
        row = self._blueprint_row(name)
        if row is None:
            raise BlueprintError(f"No blueprint named '{name}'")
        return row

    def _instance_row(self, blueprint_id: int, name: str):
        return self._db.fetch_one(
            "SELECT * FROM blueprint_instances WHERE blueprint_id = ? AND name = ?",
            (blueprint_id, name))

    def _sibling(self, blueprint_id: int, parent_id, name: str):
        return self._db.fetch_one(
            "SELECT * FROM blueprint_slots WHERE blueprint_id = ? AND parent_id IS ? "
            "AND name = ?", (blueprint_id, parent_id, name))

    @staticmethod
    def _slot(row, blueprint: str, path: str, depth: int) -> Slot:
        return Slot(
            id=row["id"], blueprint=blueprint, name=row["name"], path=path,
            kind=row["kind"], type=row["type"], registry_type=row["registry_type"],
            ref_blueprint=row["ref_name"],
            enum_values=tuple(json.loads(row["enum_values"]) if row["enum_values"] else ()),
            parent_id=row["parent_id"], depth=depth)


@dataclass(frozen=True)
class Blueprint:
    id: int
    name: str
    description: str = ""
    slots: list = field(default_factory=list)

    def value_slots(self) -> list:
        return [s for s in self.slots if not s.is_group]


def _as_bool(value, slot: Slot) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise BlueprintError(f"'{value}' is not a true/false value for '{slot.path}'")


def _qualifiers(type: str, registry_type, ref_blueprint, enum_values) -> dict:
    """Keep only the type qualifier the slot's type actually uses."""
    return {
        "registry_type": registry_type if type == "registry" else None,
        "ref_blueprint": ref_blueprint if type == "blueprint" else None,
        "enum_values": enum_values if type == "enum" else None,
    }


def _check_name(kind: str, name: str):
    """Names are labels, not identity (ids are), so the rules only need to keep paths
    parseable and displayable: no dots (they separate path segments) and no blanks."""
    text = (name or "").strip()
    if not text:
        raise BlueprintError(f"{kind} name cannot be empty")
    if "." in text:
        raise BlueprintError(
            f"{kind} name '{text}' cannot contain '.' — dots separate slot path segments")
    if text != name:
        raise BlueprintError(f"{kind} name '{name}' has leading or trailing whitespace")
