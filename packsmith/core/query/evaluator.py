"""The v1 executor — a pure-Python evaluator over in-memory L1 + L2 (design 3.2.4).

Two scopes: **registry** (rows are entries) and **blueprint** (rows are the instances of a
schema, fields are its slots). Order of operations mirrors SQL: WHERE -> project ->
DISTINCT -> ORDER BY -> LIMIT. The executor is swappable behind the AST; this one is
deliberately simple and correct at PackSmith's scale.

Null semantics (deliberate, conservative): when a field has no value for an entry (an
unset tag with no default, a missing attribute), every ``Cmp`` is False. Presence is
expressed explicitly with ``Has`` / ``Not(Has(...))`` — comparisons only ever match
entries that actually have a comparable value.
"""
import operator
import re

from packsmith.core.query.ast import (
    Query, Registry, Blueprint, Cmp, Has, And, Or, Not, VALID_OPS, QueryError,
    AGGREGATES, Collect, CountDistinct, Slot, _AllSlots, _Count, _Id,
)
from packsmith.core.query.catalog import (
    BlueprintFieldCatalog, RegistryFieldCatalog, _Resolver, column_name,
)
from packsmith.core.query.result import Result, Column, Row
from packsmith.core.query.tokens import tokenize, wanted_tokens


def evaluate(query, *, packdump=None, tag_store=None, blueprint_store=None) -> Result:
    """Run ``query`` against L1 + L2, returning a table.

    Two scopes: registry entries, and the instances of a blueprint. They differ only in
    where the rows come from and which catalog resolves the fields — everything after
    (filter, project, distinct, order, limit) is identical, because a row is just an
    identity string either way.
    """
    if not isinstance(query, Query):
        raise QueryError("evaluate() expects a Query")

    select = list(query.select or [])
    if not select:
        raise QueryError("a query must select at least one field")

    catalog, entries = _source(query.scope, packdump, tag_store, blueprint_store)
    select = _expand(select, catalog)

    if query.group_by:
        return _grouped(query, catalog, entries)

    # WHERE
    predicate = _compile_filter(query.filter, catalog)
    matched = [e for e in entries if predicate(e)]

    # SELECT / ORDER BY resolvers
    select_cols = [(column_name(f), catalog.resolver(f)) for f in select]
    order_resolvers = [catalog.resolver(f) for f in (query.order_by or [])]

    # Project each surviving entry into (entry_id, values, order_keys)
    records = []
    for e in matched:
        values = {name: res(e) for name, res in select_cols}
        keys = [res(e) for res in order_resolvers]
        records.append((e, values, keys))

    # DISTINCT — dedupe by projected values; collapses entry identity (computed rows)
    if query.distinct:
        seen = set()
        deduped = []
        for rec in records:
            sig = tuple(rec[1][name] for name, _ in select_cols)
            if sig not in seen:
                seen.add(sig)
                deduped.append(rec)
        records = deduped

    # ORDER BY — stable, None sinks to the bottom
    if order_resolvers:
        records.sort(key=lambda rec: [_sort_key(k) for k in rec[2]])

    # LIMIT
    if query.limit is not None:
        records = records[: query.limit]

    columns = [Column(name, res.type) for name, res in select_cols]
    # DISTINCT drops entry_id because a deduplicated row generally stands for several
    # entries, and editing it would be ambiguous. That is not true when `id` is projected:
    # ids are unique, so no two rows can have collapsed and each still names exactly one
    # entry. Blanket-stripping made those views needlessly read-only.
    identifies_one = any(isinstance(f, _Id) for f in query.select)
    anonymous = query.distinct and not identifies_one
    rows = [
        Row(values=values, entry_id=(None if anonymous else entry_id))
        for (entry_id, values, _keys) in records
    ]
    return Result(columns=columns, rows=rows)


def _grouped(query, catalog, entries) -> Result:
    """WHERE -> GROUP BY -> aggregate -> HAVING -> ORDER BY -> LIMIT.

    Grouped rows are **computed**: no single entry backs them, so they carry no
    ``entry_id`` and a View renders them read-only. That falls out of the shape of the
    query rather than being a flag anyone sets (design 3.2.4).
    """
    group_fields = list(query.group_by)
    group_names = [column_name(f) for f in group_fields]
    group_resolvers = [catalog.resolver(f) for f in group_fields]

    # The ordinary SQL rule, for the ordinary reason: anything else has no single value
    # for a group, so there is no honest thing to put in the cell.
    for field in query.select:
        if isinstance(field, AGGREGATES):
            continue
        if column_name(field) not in group_names:
            raise QueryError(
                f"'{column_name(field)}' is neither grouped nor aggregated — add it to "
                f"group_by, or wrap it in Count/CountDistinct/Collect")

    predicate = _compile_filter(query.filter, catalog)
    buckets = {}
    for entry in entries:
        if not predicate(entry):
            continue
        key = tuple(res(entry) for res in group_resolvers)
        buckets.setdefault(key, []).append(entry)

    columns, rows = [], []
    for field in query.select:
        columns.append(Column(column_name(field), _column_type(field, catalog)))

    # HAVING may name an aggregate that isn't selected — `HAVING count > 1` without a
    # count column is the normal way to write a duplicates report. Compute those too, then
    # project them back out.
    needed = list(query.select) + [f for f in _fields_in(query.having)
                                   if isinstance(f, AGGREGATES)]

    for key, members in buckets.items():
        values = dict(zip(group_names, key))
        for field in needed:
            if isinstance(field, AGGREGATES):
                values[column_name(field)] = _aggregate(field, members, catalog)
        rows.append(values)

    if query.having is not None:
        keep = _compile_filter(query.having, _ValuesCatalog())
        rows = [v for v in rows if keep(v)]

    selected = [c.name for c in columns]
    rows = [{name: v[name] for name in selected} for v in rows]

    if query.order_by:
        names = [column_name(f) for f in query.order_by]
        for name in names:
            if name not in [c.name for c in columns]:
                raise QueryError(f"cannot order a grouped query by '{name}' — it isn't "
                                 f"selected")
        rows.sort(key=lambda v: [_sort_key(v.get(n)) for n in names])

    if query.limit is not None:
        rows = rows[: query.limit]
    return Result(columns=columns, rows=[Row(values=v, entry_id=None) for v in rows])


def _fields_in(node) -> list:
    """Every field a filter tree references, so HAVING's aggregates can be computed."""
    if node is None:
        return []
    if isinstance(node, (And, Or)):
        return [f for c in node.clauses for f in _fields_in(c)]
    if isinstance(node, Not):
        return _fields_in(node.clause)
    if isinstance(node, (Cmp, Has)):
        return [node.field]
    return []


def _aggregate(field, members, catalog):
    if isinstance(field, _Count):
        return len(members)
    resolver = catalog.resolver(field.field)
    values = [resolver(m) for m in members]
    if isinstance(field, CountDistinct):
        return len({v for v in values if v is not None})
    gathered = sorted({v for v in values if v is not None})
    return gathered[: field.limit] if field.limit else gathered


def _column_type(field, catalog) -> str:
    if isinstance(field, (_Count, CountDistinct)):
        return "number"
    if isinstance(field, Collect):
        return "list"
    return catalog.resolver(field).type


class _ValuesCatalog:
    """Resolves a HAVING clause against a group's computed row rather than an entry — the
    same resolver protocol, one phase later."""

    @staticmethod
    def resolver(field):
        name = column_name(field)
        return _Resolver("number" if isinstance(field, (_Count, CountDistinct))
                         else "string", lambda values: values.get(name))


def _expand(select, catalog):
    """Replace ``AllSlots`` with the scope's actual slots, at evaluation time.

    Expanding here rather than when the query is written is the point: the saved query
    keeps saying "every slot", so a schema that grows is reflected the next time the view
    is opened instead of quietly under-reporting gaps.
    """
    if not any(isinstance(f, _AllSlots) for f in select):
        return select
    if not isinstance(catalog, BlueprintFieldCatalog):
        raise QueryError("AllSlots needs a Blueprint scope")
    expanded = []
    for field in select:
        if isinstance(field, _AllSlots):
            expanded.extend(Slot(path) for path in catalog.slot_paths())
        else:
            expanded.append(field)
    return expanded


def _source(scope, packdump, tag_store, blueprint_store):
    """(catalog, rows) for a scope. The only place the two scopes differ."""
    if isinstance(scope, Registry):
        if packdump is None:
            raise QueryError("a registry-scoped query needs a packdump")
        return (RegistryFieldCatalog(packdump, tag_store, scope.type),
                list(packdump.registry.get(scope.type, {}).get("values", [])))
    if isinstance(scope, Blueprint):
        if blueprint_store is None:
            raise QueryError("a blueprint-scoped query needs a blueprint store")
        return (BlueprintFieldCatalog(blueprint_store, scope.name),
                [i.name for i in blueprint_store.instances(scope.name)])
    raise QueryError(f"unsupported scope: {scope!r}")


def _sort_key(v):
    # None always sorts last; the type name keeps mixed types from comparing across kinds.
    if v is None:
        return (1, "", "")
    return (0, type(v).__name__, v)


# --- filter compilation -----------------------------------------------------

def _compile_filter(node, catalog):
    if node is None:
        return lambda e: True
    if isinstance(node, And):
        subs = [_compile_filter(c, catalog) for c in node.clauses]
        return lambda e: all(s(e) for s in subs)
    if isinstance(node, Or):
        subs = [_compile_filter(c, catalog) for c in node.clauses]
        return lambda e: any(s(e) for s in subs)
    if isinstance(node, Not):
        sub = _compile_filter(node.clause, catalog)
        return lambda e: not sub(e)
    if isinstance(node, Has):
        # Existence, not value — see `presence`. Using the resolver here made HAS true for
        # every entry whenever the tag had a default.
        return catalog.presence(node.field)
    if isinstance(node, Cmp):
        return _compile_cmp(node, catalog.resolver(node.field))
    raise QueryError(f"not a filter: {node!r}")


def _compile_cmp(node, res):
    op, val, ci = node.op, node.value, node.ci
    if op not in VALID_OPS:
        raise QueryError(f"unknown op: {op!r}")

    if op in ("eq", "neq"):
        want = (op == "eq")
        def f(e):
            v = res(e)
            return False if v is None else (_equal(v, val, ci) == want)
        return f

    if op in ("gt", "lt", "gte", "lte"):
        fn = {"gt": operator.gt, "lt": operator.lt, "gte": operator.ge, "lte": operator.le}[op]
        def f(e):
            v = res(e)
            if v is None:
                return False
            try:
                return fn(v, val)
            except TypeError:
                return False
        return f

    if op in ("in", "not_in"):
        if not isinstance(val, (list, tuple)):
            raise QueryError(f"'{op}' needs a list value, got {val!r}")
        want = (op == "in")
        def f(e):
            v = res(e)
            if v is None:
                return False
            member = any(_equal(v, x, ci) for x in val)
            return member == want
        return f

    if op == "contains":
        def f(e):
            v = res(e)
            if v is None:
                return False
            s = v if isinstance(v, str) else str(v)
            needle = str(val)
            return (needle.lower() in s.lower()) if ci else (needle in s)
        return f

    if op == "matches_tokens":
        wanted = wanted_tokens([val] if isinstance(val, str) else list(val or []))
        if not wanted:
            # Almost always a template whose anchor resolved to nothing. Matching every
            # row would silently hand back the whole registry as "recommendations".
            raise QueryError("'matches_tokens' needs at least one token")
        exact = {t for t, prefix in wanted if not prefix}
        prefixes = [t for t, prefix in wanted if prefix]

        def f(e):
            v = res(e)
            if v is None:
                return False
            have = tokenize(v)
            if not exact <= have:
                return False
            return all(any(t.startswith(p) for t in have) for p in prefixes)
        return f

    if op == "matches":
        pattern = re.compile(val, re.IGNORECASE if ci else 0)
        def f(e):
            v = res(e)
            if v is None:
                return False
            s = v if isinstance(v, str) else str(v)
            return pattern.search(s) is not None
        return f

    raise QueryError(f"unhandled op: {op!r}")  # unreachable given VALID_OPS


def _equal(v, other, ci):
    if ci and isinstance(v, str) and isinstance(other, str):
        return v.lower() == other.lower()
    return v == other
