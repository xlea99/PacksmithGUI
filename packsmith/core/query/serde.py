"""(De)serialization for the query AST — the mechanism behind "a query is data."

Encodes every node to a JSON-safe dict tagged with ``node`` and decodes it back to an
equal AST. All behaviour lives in the executor, so nothing here needs to reconstruct
logic — just values. A round-trip (``from_dict(json.loads(json.dumps(to_dict(q))))``)
must equal the original query; that invariant is what lets Views be saved, forked, and
shared.
"""
import dataclasses

from packsmith.core.query.ast import (
    Query, Registry, Blueprint, Tag, Attribute, Slot,
    Cmp, Has, And, Or, Not, Id, Mod, AllSlots, _Id, _Mod, _AllSlots, QueryError,
)

_NODE_TYPES = {
    cls.__name__: cls
    for cls in (Query, Registry, Blueprint, Tag, Attribute, Slot, Cmp, Has, And, Or, Not)
}


def to_dict(node):
    """Encode an AST node (or None / primitive / list) to a JSON-safe value."""
    return _enc(node)


def from_dict(data):
    """Decode a value produced by :func:`to_dict` back into an AST node."""
    return _dec(data)


def _enc(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, _Id):
        return {"node": "Id"}
    if isinstance(v, _Mod):
        return {"node": "Mod"}
    if isinstance(v, _AllSlots):
        return {"node": "AllSlots"}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        d = {"node": type(v).__name__}
        for f in dataclasses.fields(v):
            d[f.name] = _enc(getattr(v, f.name))
        return d
    raise QueryError(f"cannot serialize {v!r}")


def _dec(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if isinstance(v, dict):
        tag = v.get("node")
        if tag == "Id":
            return Id
        if tag == "Mod":
            return Mod
        if tag == "AllSlots":
            return AllSlots
        cls = _NODE_TYPES.get(tag)
        if cls is None:
            raise QueryError(f"unknown node type: {tag!r}")
        kwargs = {k: _dec(val) for k, val in v.items() if k != "node"}
        return cls(**kwargs)
    raise QueryError(f"cannot deserialize {v!r}")
