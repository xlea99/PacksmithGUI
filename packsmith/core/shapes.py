"""Structural typing for blueprint mappings — `required_shape` (design 3.3).

An action must not name the user's artifacts. For tags that's easy: a mapping says "a bool
tag on `minecraft:item`" and any tag of that type fits. Blueprints are harder, because a
blueprint isn't one typed thing — it's a *shape*, and an action that reads
`polished.stairs` needs that slot to exist and to hold a block.

So the contract is structural, not nominal (3.3): "Any user-defined blueprint whose shape
matches this contract is a valid binding, regardless of what the user named it.
`MyRockKind`, `StoneSchema42`, `Granite-Style` — all fine as long as the structure
matches."

**Matching is at-least, never exact.** The user's schema must *contain* what the action
declared; extra slots and extra groups are fine and invisible to it. This is the same rule
`requires_values` follows for enums, and for the same reason 3.3 gives there: adding a slot
must never invalidate an action that doesn't care about it. An `exact` variant would only
make sense for an action that exhaustively walks every slot, and nothing does.

The shape is declared as nested JSON5 objects and parsed here into a **flat, ordered tuple of
requirements keyed by dotted path** — the same addressing blueprint slots already use. Flat
is what makes a mismatch reportable as a sentence about one slot ("'polished.stairs' takes
minecraft:item, but the action needs minecraft:block") rather than a diff of two trees.
"""
from dataclasses import dataclass, field

# What a required_shape leaf may ask for, and the blueprint slot type each maps onto.
# `registry_entry` is 3.3's spelling; `registry` is what the store calls the same thing.
_KIND_TO_SLOT_TYPE = {
    "registry_entry": "registry",
    "blueprint": "blueprint",
    "string": "string",
    "number": "number",
    "bool": "bool",
    "enum": "enum",
}
SHAPE_KINDS = ("group",) + tuple(_KIND_TO_SLOT_TYPE)


@dataclass(frozen=True)
class ShapeRequirement:
    """One thing the action needs to find, at one dotted path."""
    path: str
    kind: str                       # "group" or a key of _KIND_TO_SLOT_TYPE
    registry_type: str = None       # registry_entry only
    values: tuple = field(default_factory=tuple)     # enum only, at-least

    def describe(self) -> str:
        if self.kind == "group":
            return "a group"
        if self.kind == "registry_entry":
            return f"a {self.registry_type} entry" if self.registry_type else "a registry entry"
        if self.kind == "enum" and self.values:
            return f"an enum including {', '.join(self.values)}"
        return f"a {self.kind} value"


def parse_shape(raw, *, where: str) -> tuple:
    """Parse a `required_shape` table into flat requirements, innermost last.

    Raises ValueError on a malformed declaration, so a broken manifest is refused at load
    time rather than at run time — the same stance conflict policy takes.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: required_shape must be a table")
    return tuple(_walk(raw, prefix="", where=where))


def _walk(table: dict, *, prefix: str, where: str):
    for name, spec in table.items():
        path = f"{prefix}.{name}" if prefix else name
        if not isinstance(spec, dict):
            raise ValueError(
                f"{where}: required_shape entry '{path}' must be a table, got "
                f"{type(spec).__name__}")
        kind = spec.get("kind")
        if kind is None:
            raise ValueError(f"{where}: required_shape entry '{path}' declares no kind")
        if kind not in SHAPE_KINDS:
            raise ValueError(
                f"{where}: required_shape entry '{path}' has unknown kind '{kind}' "
                f"(expected one of {', '.join(SHAPE_KINDS)})")
        if kind == "group":
            nested = spec.get("slots")
            if not isinstance(nested, dict) or not nested:
                raise ValueError(
                    f"{where}: required_shape group '{path}' declares no slots — a group "
                    f"the action needs nothing out of is not a requirement")
            yield ShapeRequirement(path=path, kind="group")
            yield from _walk(nested, prefix=path, where=where)
            continue
        if kind == "registry_entry" and not spec.get("registry_type"):
            raise ValueError(
                f"{where}: required_shape entry '{path}' is a registry_entry but declares "
                f"no registry_type")
        yield ShapeRequirement(
            path=path, kind=kind,
            registry_type=spec.get("registry_type"),
            values=tuple(spec.get("values", ())))


def describe_shape(shape) -> str:
    """One line naming what a mapping needs, for a tooltip or a hint label."""
    leaves = [r.path for r in shape if r.kind != "group"]
    return ", ".join(leaves) if leaves else "any shape"


def mismatches(shape, slots) -> list:
    """Every way ``slots`` fails to satisfy ``shape``, as readable sentences.

    ``slots`` is what ``BlueprintStore.slots(name)`` returns. An empty list means the
    blueprint is a valid binding — extra slots it happens to have are none of the action's
    business.
    """
    by_path = {s.path: s for s in slots}
    problems = []
    for need in shape:
        found = by_path.get(need.path)
        if found is None:
            problems.append(f"no slot '{need.path}' (needs {need.describe()})")
            continue
        if need.kind == "group":
            if not found.is_group:
                problems.append(f"'{need.path}' is a slot, but the action needs a group")
            continue
        if found.is_group:
            problems.append(
                f"'{need.path}' is a group, but the action needs {need.describe()}")
            continue
        wanted_type = _KIND_TO_SLOT_TYPE[need.kind]
        if found.type != wanted_type:
            problems.append(
                f"'{need.path}' holds a {found.type} value, but the action needs "
                f"{need.describe()}")
            continue
        if (need.kind == "registry_entry" and need.registry_type
                and found.registry_type != need.registry_type):
            problems.append(
                f"'{need.path}' takes {found.registry_type}, but the action needs "
                f"{need.registry_type}")
            continue
        if need.kind == "enum" and need.values:
            # At-least, exactly as `requires_values` defines it for tags (3.3).
            absent = [v for v in need.values if v not in (found.enum_values or ())]
            if absent:
                problems.append(
                    f"'{need.path}' is missing enum value(s): {', '.join(absent)}")
    return problems


def satisfies(shape, slots) -> bool:
    return not mismatches(shape, slots)
