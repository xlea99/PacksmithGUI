"""The query AST — the serializable spine of the query engine (design 3.2.4).

A query is **data, not code**: every node here is a plain frozen dataclass that
round-trips through JSON with zero behaviour attached (serde.py handles that). All
resolver logic — how ``Mod`` is computed, how an ``Attribute`` is looked up — lives in
the executor (catalog.py / evaluator.py), keyed by node type, NEVER inside the node.
This is a hard constraint, not an aspiration: queries are written to the user DB (a
saved View *is* a query), forked, and shared, so they must be inert, portable values.

There is no shorthand here. The ``t:`` / ``a:`` / ``b:`` prefixes a user types live only
in the surface language (§3.2.3); the builder lowers them into these full-word nodes
before the engine ever sees them.
"""
from dataclasses import dataclass


class QueryError(Exception):
    """A query is malformed, or uses a feature this executor doesn't support."""


# --- Intrinsic fields: part of the entry itself, no lookup ------------------
# An entry *is* its id; ``Mod`` is a pure derivation of the id. Both are value-less
# singletons so they read bare in a query (``select=[Id, Mod]``) while remaining
# ordinary, comparable, serializable node instances.

@dataclass(frozen=True)
class _Id:
    """The entry itself — an entry *is* its id (design 3.1)."""


@dataclass(frozen=True)
class _Mod:
    """The mod namespace, derived from the id (``alexscaves:galena`` -> ``alexscaves``)."""


Id = _Id()
Mod = _Mod()


# --- Attached fields: looked up from a source -------------------------------

@dataclass(frozen=True)
class Tag:
    """An L2 user-data tag value."""
    name: str


@dataclass(frozen=True)
class Attribute:
    """An L1 packdump-provided attribute (today only ``localization``, design 3.1)."""
    name: str


@dataclass(frozen=True)
class Slot:
    """A blueprint slot binding (blueprint-scoped queries; not supported in v1)."""
    name: str


# --- Scopes: the base row-source --------------------------------------------

@dataclass(frozen=True)
class Registry:
    """Rows are the entries of a registry."""
    type: str


@dataclass(frozen=True)
class Blueprint:
    """Rows are the instances of a blueprint (fields become slots)."""
    name: str


# --- Filters ----------------------------------------------------------------

VALID_OPS = frozenset({
    "eq", "neq", "gt", "lt", "gte", "lte", "in", "not_in", "contains", "matches",
})


@dataclass(frozen=True)
class Cmp:
    """Compare a field against a value. ``matches`` is the Python ``re`` dialect
    (unanchored ``search``); ``contains`` and string equality honour ``ci``."""
    field: object
    op: str
    value: object
    ci: bool = False


@dataclass(frozen=True)
class Has:
    """The field is present at all — the tag is assigned, the attribute exists.
    Distinct from comparing its value."""
    field: object


@dataclass(frozen=True)
class And:
    clauses: list


@dataclass(frozen=True)
class Or:
    clauses: list


@dataclass(frozen=True)
class Not:
    clause: object


# --- Query ------------------------------------------------------------------

@dataclass(frozen=True)
class Query:
    """A relational query: filter/project/sort/limit/distinct over a scope.

    Required: ``scope`` and ``select``. ``filter`` may be omitted (all rows). The
    conceptual signature in §3.2.4 lists ``(scope, filter, select, ...)``; in code the
    required params come first and everything is passed by keyword in practice.
    """
    scope: object
    select: list
    filter: object = None
    order_by: list = None
    limit: int = None
    distinct: bool = False
