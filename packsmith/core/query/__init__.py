"""The query engine (design 3.2.4) — headless: query in, table out.

One engine powers View construction, live filtering, and (later) blueprint slot
suggestions. It reads L1 (packdump) and L2 (tags); it never reads L3 — actions call
*it* via ``pack.query``. The AST is serializable data; the executor is swappable.
"""
from packsmith.core.query.ast import (
    Query, Registry, Blueprint,
    Id, Mod, Tag, Attribute, Slot, AllSlots,
    Cmp, Has, And, Or, Not,
    QueryError, VALID_OPS,
)
from packsmith.core.query.result import Result, Column, Row
from packsmith.core.query.catalog import RegistryFieldCatalog, column_name, mod_of
from packsmith.core.query.evaluator import evaluate
from packsmith.core.query.serde import to_dict, from_dict

__all__ = [
    "Query", "Registry", "Blueprint",
    "Id", "Mod", "Tag", "Attribute", "Slot", "AllSlots",
    "Cmp", "Has", "And", "Or", "Not",
    "QueryError", "VALID_OPS",
    "Result", "Column", "Row",
    "RegistryFieldCatalog", "column_name", "mod_of",
    "evaluate", "to_dict", "from_dict",
]
