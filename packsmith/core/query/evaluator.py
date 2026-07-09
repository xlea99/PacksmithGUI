"""The v1 executor — a pure-Python evaluator over in-memory L1 + L2 (design 3.2.4).

Registry scope only (blueprint scope waits for blueprints). Order of operations mirrors
SQL: WHERE -> project -> DISTINCT -> ORDER BY -> LIMIT. The executor is swappable behind
the AST; this one is deliberately simple and correct at PackSmith's scale.

Null semantics (deliberate, conservative): when a field has no value for an entry (an
unset tag with no default, a missing attribute), every ``Cmp`` is False. Presence is
expressed explicitly with ``Has`` / ``Not(Has(...))`` — comparisons only ever match
entries that actually have a comparable value.
"""
import operator
import re

from packsmith.core.query.ast import (
    Query, Registry, Blueprint, Cmp, Has, And, Or, Not, VALID_OPS, QueryError,
)
from packsmith.core.query.catalog import RegistryFieldCatalog, column_name
from packsmith.core.query.result import Result, Column, Row


def evaluate(query, *, packdump, tag_store) -> Result:
    """Run ``query`` against a packdump (L1) and tag store (L2), returning a table."""
    if not isinstance(query, Query):
        raise QueryError("evaluate() expects a Query")
    scope = query.scope
    if isinstance(scope, Blueprint):
        raise QueryError("Blueprint scope is not supported in v1 (registry scope only)")
    if not isinstance(scope, Registry):
        raise QueryError(f"unsupported scope: {scope!r}")

    select = list(query.select or [])
    if not select:
        raise QueryError("a query must select at least one field")

    reg = scope.type
    catalog = RegistryFieldCatalog(packdump, tag_store, reg)
    entries = list(packdump.registry.get(reg, {}).get("values", []))

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
    rows = [
        Row(values=values, entry_id=(None if query.distinct else entry_id))
        for (entry_id, values, _keys) in records
    ]
    return Result(columns=columns, rows=rows)


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
        res = catalog.resolver(node.field)
        return lambda e: res(e) is not None
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
