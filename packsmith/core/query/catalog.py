"""The field catalog — the bridge from abstract field refs to physical L1/L2 data.

For a given scope, the catalog answers, per field reference, *how to compute it for
each row* (a resolver) and *what type the resulting column is*. It is the concrete
seam the design (§3.2.4) names: ``Id`` -> the entry id, ``Mod`` -> a namespace split,
``Attribute(x)`` -> a packdump lookup, ``Tag(x)`` -> a tag-store lookup. Because tag
definitions are registry-scoped (design 3.2.1), the catalog is built for one registry
and reads its own ``definitions_for(registry_type)``.
"""
from packsmith.core.query.ast import (
    _Id, _Mod, _Count, Collect, CountDistinct, Tag, Attribute, Slot, QueryError,
)


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
    if isinstance(field, _Count):
        return "count"
    if isinstance(field, CountDistinct):
        return f"distinct_{column_name(field.field)}"
    if isinstance(field, Collect):
        return f"{column_name(field.field)}_values"
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


class BlueprintFieldCatalog:
    """Resolves field references for a ``Blueprint`` scope (design 3.2.4).

    "Rows are the instances of a blueprint (fields become slots)" — so a row identity is an
    *instance name* rather than an entry id, and ``Slot("polished.base")`` reads that
    instance's binding. The evaluator needs no special case for any of this: both catalogs
    hand back resolvers keyed by a row identity string, which is the whole reason the scope
    is a swap rather than a second engine.

    Deliberately absent: ``Tag`` and ``Attribute``. Those are facts about a *registry
    entry*, and an instance is not one — reaching them means following a slot binding to
    the entry it names, which is the Power Ladder's ``Deref`` and genuinely later. Saying so
    is better than resolving them to None and looking broken.
    """

    def __init__(self, blueprint_store, blueprint_name: str):
        self._store = blueprint_store
        self._blueprint = blueprint_name
        self._slots = {s.path: s for s in blueprint_store.value_slots(blueprint_name)}
        # Every binding for this blueprint, read once. A catalog is built per query and a
        # query reads every cell, so resolving one binding at a time meant re-reading the
        # schema for each — see `BlueprintStore.binding_index`.
        self._bindings = blueprint_store.all_bindings(blueprint_name)

    def _bound(self, instance, path):
        binding = self._bindings.get(instance, {}).get(path)
        return binding.value if binding is not None else None

    def slot_paths(self) -> list:
        return list(self._slots)

    def resolver(self, field) -> _Resolver:
        blueprint = self._blueprint
        if isinstance(field, _Id):
            return _Resolver("id", lambda instance: instance)
        if isinstance(field, Slot):
            slot = self._slots.get(field.name)
            if slot is None:
                raise QueryError(f"'{blueprint}' has no slot '{field.name}'")
            return _Resolver(_COLUMN_TYPES.get(slot.type, "string"),
                             lambda instance: self._store.decode(
                                 slot, self._bound(instance, slot.path)))
        if isinstance(field, _Mod):
            raise QueryError(
                "Mod is a fact about a registry entry, not a blueprint instance")
        if isinstance(field, (Tag, Attribute)):
            kind = "Tag" if isinstance(field, Tag) else "Attribute"
            raise QueryError(
                f"{kind} needs a registry scope — reading one through a blueprint slot "
                f"requires Deref, which is not supported in v1")
        raise QueryError(f"not a field: {field!r}")

    def presence(self, field):
        """Blueprint bindings have no defaults — a slot is bound or it is a gap — so here
        existence really is "the resolver found something". Present for interface parity,
        so the evaluator never has to ask which catalog it is holding."""
        resolve = self.resolver(field)
        return lambda e: resolve(e) is not None


# A slot's storage type mapped to the column type the renderers understand. Registry and
# blueprint bindings are ids: strings, as far as sorting and comparison care.
_COLUMN_TYPES = {
    "string": "string", "number": "number", "bool": "bool", "enum": "enum",
    "registry": "string", "blueprint": "string",
}


class RegistryFieldCatalog:
    """Resolves field references for a single ``Registry`` scope.

    **L2 is read a column at a time, not a cell at a time.** This used to do one query per
    (entry, tag); measured against a real 300-mod pack that was 18,638 queries per tag
    column — a three-tag view of `minecraft:item` fired 55,914 selects and took 1.2s to
    open, where the same view now takes 27ms. The cost scaled with rows *times* columns,
    so it worsened precisely as views became more useful.

    Loaded **lazily, per field**: a resolver is built only for fields a query actually
    names, so filtering by one tag doesn't drag in the columns beside it. The maps live as
    long as the catalog, which is one evaluation — long enough to be worth loading, too
    short to go stale.
    """

    def __init__(self, packdump, tag_store, registry_type: str):
        self._dump = packdump
        self._tags = tag_store
        self._reg = registry_type
        # No tag store means no Layer 2 at all, which is a coherent world to query in —
        # the candidate finder (design 5.3) is a pure L1 id search and shouldn't have to
        # invent a tag store to run. Tags then simply have no value anywhere.
        self._defs = tag_store.definitions_for(registry_type) if tag_store else {}
        # Memoised per catalog, so a tag named in both `select` and `filter` is loaded
        # once. Never shared between catalogs: one is built per evaluation, which is
        # exactly how long a snapshot of L2 can be trusted.
        self._columns = {}

    def _column(self, tag_name: str) -> dict:
        if tag_name not in self._columns:
            self._columns[tag_name] = self._tags.column(self._reg, tag_name)
        return self._columns[tag_name]

    def attribute_names(self) -> list:
        """What ``AllAttributes`` expands to for this registry — the attributes the dump
        actually carries here, not every attribute it knows how to answer.

        Asked of the dump only when a query actually uses the wildcard, rather than in
        ``__init__``. A catalog is built for *every* query, most of which name their columns
        outright, so eager work here is work nobody asked for — and it would make
        ``attributes_for`` a hard requirement of anything standing in for a packdump, which
        is a lot of things that have no business knowing about attributes.
        """
        if self._dump is None:
            return []
        return list(self._dump.attributes_for(self._reg))

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
            if self._tags is None:
                return _Resolver(col_type, lambda e: None)
            # A pristine cell shows the tag's default (§3.2.1), so the default is the
            # miss-value rather than None — the column map holds assigned cells only.
            values = self._column(name)
            default = self._tags.default_for(reg, name)
            return _Resolver(col_type, lambda e: values.get(e, default))
        if isinstance(field, Slot):
            raise QueryError("blueprint slots require a Blueprint scope (not supported in v1)")
        raise QueryError(f"not a field: {field!r}")

    def presence(self, field):
        """"Is this assigned?" — **existence**, which is a different question from value.

        `resolver` deliberately sugars a pristine cell into the tag's default, because that
        is what a table cell should show (design 3.2.1). Asking existence through it makes
        `HAS t:remove` true for every entry the moment `remove` has a default — so the
        gap-finding idiom the whole engine is built around quietly returns everything, and
        `NOT HAS` returns nothing. 3.2.4 defines Has as "the tag is assigned", and only the
        row's existence answers that.
        """
        if isinstance(field, Tag) and self._tags is not None:
            # The same loaded column: a key means a row exists, which *is* "assigned".
            # Deliberately not "the value differs from the default" — a cell assigned the
            # tag's own default is assigned, and that is the whole distinction Has exists
            # to make.
            assigned = self._column(field.name)
            return lambda e: e in assigned
        # Everything else (id, mod, attributes) has no default to inflate, so presence is
        # just "the resolver found something".
        resolve = self.resolver(field)
        return lambda e: resolve(e) is not None
