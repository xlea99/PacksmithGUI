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
    """An L1 packdump-provided attribute — ``localization``, ``localization_key`` (§3.1)."""
    name: str


@dataclass(frozen=True)
class Slot:
    """A blueprint slot binding (blueprint-scoped queries)."""
    name: str


@dataclass(frozen=True)
class _AllSlots:
    """Every bindable slot of the scope's blueprint, in schema order.

    A blueprint's columns are a **fact about the blueprint**, not a preference of the view:
    empty slots are the primitive's whole output (design 3.2.2 — "empty slots = missing
    content that needs to be generated or sourced"), so a saved view that froze its column
    list would start hiding *gaps* the moment the schema grew. Selecting this instead means
    the view follows the schema.

    Listing slots explicitly is still allowed and still means what it says — a deliberately
    curated subset. The difference between "everything" and "these" is then visible in the
    query rather than implied.
    """


AllSlots = _AllSlots()


@dataclass(frozen=True)
class _AllAttributes:
    """Every attribute the scope's registry carries, in the dump's order.

    ``AllSlots``' argument, one layer up: what a packdump knows about an entry is a **fact
    about the dump**, not a preference of the view. The Registry panel's browse table froze
    its columns at id-and-name, so when the dump learned to harvest translation keys the
    default way of looking at a registry kept showing exactly what it showed before —
    hiding, by omission, the thing that had just been added.

    Expands to what a registry **actually has**, not to every attribute in existence: a pack
    dumps localization for a handful of registries out of ~135, and a permanently empty
    column reads as data that failed to load rather than data that never existed.

    Naming attributes explicitly is still allowed and still means a curated subset.
    """


AllAttributes = _AllAttributes()


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
    "matches_tokens",
})


@dataclass(frozen=True)
class Cmp:
    """Compare a field against a value. ``matches`` is the Python ``re`` dialect
    (unanchored ``search``); ``contains`` and string equality honour ``ci``.

    ``matches_tokens`` takes a **list of tokens** and holds when the field's value carries
    all of them, separators and namespace and word order ignored (design 5.3 — the
    `quark:ac_galena_wall` case that defeats substring matching). It is an ordinary op
    rather than a new node type, so a query using it stays plain serializable data."""
    field: object
    op: str
    value: object
    ci: bool = False


@dataclass(frozen=True)
class Has:
    """The field is present at all — the tag is assigned, the attribute exists.
    Distinct from comparing its value."""
    field: object


# --- Registry entries, asked about a blueprint -------------------------------
#
# Both cross from L1 back into L2 the *opposite* way to everything above: the row is a
# registry entry and the question is about a blueprint. That direction had no expression at
# all — `Slot` under a registry scope refuses, and rightly, because a slot belongs to one
# instance and an entry belongs to none.
#
# What makes these well-formed where `Slot` is not: neither asks which instance or slot an
# entry belongs to. They ask a yes/no question about the blueprint taken WHOLE — is this id
# claimed anywhere in it, does its name belong to the vocabulary of its instances. A
# blueprint as a *set* is something a registry entry can be compared against.
#
# Together they express the question the primitive implies but could not previously ask:
# "everything that looks like it belongs to one of my stones, that I did not choose."
#
#     Mentions("StoneType") AND NOT BoundIn("StoneType")

@dataclass(frozen=True)
class BoundIn:
    """This entry's id is a value bound somewhere in ``blueprint``.

    ``slot`` narrows it to one slot path — "what else did I pass over for `pillar.base`"
    rather than "what did I pass over entirely".
    """
    blueprint: str
    slot: str = None


@dataclass(frozen=True)
class Mentions:
    """This entry's id carries the name of one of ``blueprint``'s instances, as whole words.

    **Whole words, never substring.** `stone` is a real instance name in a real pack, and
    substring matching on it drags in `cobblestone`, `sandstone`, `limestone`, `tombstone`
    and `sandstone_stove` — measured at 674 false positives out of 2,135, a third of the
    answer being garbage. Token matching (§5.3's `matches_tokens`) is the same machinery the
    candidate finder already uses, for the same reason.

    **The instance name is the search term, and no ceremony says so.** Measured against a
    real 42-instance blueprint, 98% of bound ids carry their own instance name as whole
    words; a dedicated `search_term` slot would be 42 cells encoding what 41 of them already
    say. It would also be a drift surface — add an instance, forget the cell, and it
    silently leaves the view — and unfilled cells would read as gaps in a blueprint whose
    gaps are its actual output (§3.2.2).

    ``alias_slot`` is the escape hatch for the other 2%: a string slot whose value adds
    search terms for that instance. In the pack this was designed against exactly one
    instance needed it — `primestone`, whose blocks are all named `alexscaves:limestone`.
    Additive, never replacing: the question is whether ANY instance mentions the entry, so a
    wider net costs nothing while a wrong exclusion costs a block going unnoticed. Absent
    means "the name is enough", which is true for almost every instance.
    """
    blueprint: str
    alias_slot: str = None


@dataclass(frozen=True)
class And:
    clauses: list


@dataclass(frozen=True)
class Or:
    clauses: list


@dataclass(frozen=True)
class Not:
    clause: object


# --- Aggregates -------------------------------------------------------------
#
# The Power Ladder's "aggregation / grouping" tier. These are the only nodes that may
# appear in ``select`` alongside ``group_by`` fields, and the only ones ``having`` can
# compare — the ordinary SQL rule, for the ordinary SQL reason.

@dataclass(frozen=True)
class _Count:
    """How many rows fell into this group."""


Count = _Count()


@dataclass(frozen=True)
class CountDistinct:
    """How many *different* values of a field the group holds — "3 mods claim this name"
    is a different and more interesting fact than "3 rows"."""
    field: object


@dataclass(frozen=True)
class Collect:
    """The group's values for a field, gathered into a list. Turns "this name collides"
    into "these are the ids that collide", which is the answer you actually wanted."""
    field: object
    limit: int = None


AGGREGATES = (_Count, CountDistinct, Collect)


# --- Query ------------------------------------------------------------------

@dataclass(frozen=True)
class Query:
    """A relational query: filter/project/group/sort/limit/distinct over a scope.

    Required: ``scope`` and ``select``. ``filter`` may be omitted (all rows). The
    conceptual signature in §3.2.4 lists ``(scope, filter, select, ...)``; in code the
    required params come first and everything is passed by keyword in practice.

    ``group_by`` collapses rows into groups; ``select`` may then hold only grouping fields
    and aggregates, and ``having`` filters the resulting groups (as opposed to ``filter``,
    which runs before grouping). A grouped row is *computed* — no single entry backs it, so
    it isn't editable.
    """
    scope: object
    select: list
    filter: object = None
    order_by: list = None
    limit: int = None
    distinct: bool = False
    group_by: list = None
    having: object = None


def blueprints_read(node) -> set:
    """Every blueprint name ``node`` depends on, at any depth.

    A registry-scoped query can now read L2 *blueprint* state (`BoundIn`, `Mentions`), which
    means it can go stale from an edit made somewhere else entirely — bind a cell in the
    grid and a "what did I not choose" view is wrong until it re-runs. Whoever refreshes
    views needs to know which ones care, and asking the query is the only way that cannot
    drift from what the query actually does.

    Walks generically rather than matching known shapes: a node added later is covered the
    day it exists, which is the same argument `serde` makes for deriving its node table.
    """
    import dataclasses

    found = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (list, tuple)):
            stack.extend(current)
            continue
        if isinstance(current, Blueprint):
            found.add(current.name)
        elif isinstance(current, (BoundIn, Mentions)):
            found.add(current.blueprint)
        if dataclasses.is_dataclass(current) and not isinstance(current, type):
            stack.extend(getattr(current, f.name) for f in dataclasses.fields(current))
    return found
