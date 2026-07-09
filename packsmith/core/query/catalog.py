"""The field catalog — the bridge from abstract field refs to physical L1/L2 data.

For a given scope, the catalog answers, per field reference, *how to compute it for
each row* (a resolver) and *what type the resulting column is*. It is the concrete
seam the design (§3.2.4) names: ``Id`` -> the entry id, ``Mod`` -> a namespace split,
``Attribute(x)`` -> a packdump lookup, ``Tag(x)`` -> a tag-store lookup. Because tag
definitions are registry-scoped (design 3.2.1), the catalog is built for one registry
and reads its own ``definitions_for(registry_type)``.
"""
from packsmith.core.query.ast import _Id, _Mod, Tag, Attribute, Slot, QueryError


def mod_of(entry_id: str) -> str:
    """The mod namespace: everything before the first colon (``alexscaves:galena`` ->
    ``alexscaves``). Ids are always namespaced; a colon-less id derives to itself."""
    return entry_id.split(":", 1)[0] if ":" in entry_id else entry_id


def column_name(field) -> str:
    """The canonical column name a field projects to. (Aliasing via ``.as_()`` is a
    later, additive feature; v1 uses canonical names.)"""
    if isinstance(field, _Id):
        return "id"
    if isinstance(field, _Mod):
        return "mod"
    if isinstance(field, (Tag, Attribute, Slot)):
        return field.name
    raise QueryError(f"not a field: {field!r}")


class _Resolver:
    """A field bound to a scope: ``.type`` is the column type, calling it with an
    entry id yields that entry's value for the field."""
    __slots__ = ("type", "_fn")

    def __init__(self, type: str, fn):
        self.type = type
        self._fn = fn

    def __call__(self, entry_id):
        return self._fn(entry_id)


class RegistryFieldCatalog:
    """Resolves field references for a single ``Registry`` scope.

    Note: tag values are read per (entry, tag) via the tag store. Bulk-loading L2 for
    a scope up front is a straightforward future optimization behind this same seam;
    at PackSmith's ~2000-entry scale the per-cell reads are fine for v1.
    """

    def __init__(self, packdump, tag_store, registry_type: str):
        self._dump = packdump
        self._tags = tag_store
        self._reg = registry_type
        self._defs = tag_store.definitions_for(registry_type)

    def resolver(self, field) -> _Resolver:
        reg = self._reg
        if isinstance(field, _Id):
            return _Resolver("id", lambda e: e)
        if isinstance(field, _Mod):
            return _Resolver("string", mod_of)
        if isinstance(field, Attribute):
            name = field.name
            return _Resolver("string", lambda e: self._dump.attribute(reg, e, name))
        if isinstance(field, Tag):
            name = field.name
            defn = self._defs.get(name)
            col_type = defn["type"] if defn else "string"
            return _Resolver(col_type, lambda e: self._tags.get_tag(reg, e, name))
        if isinstance(field, Slot):
            raise QueryError("blueprint slots require a Blueprint scope (not supported in v1)")
        raise QueryError(f"not a field: {field!r}")
