"""Recommendation templates — one query, resolved per cell (design 5.3).

§5.3 Part B: *"the **user authors the recommendation query**, parameterized over the
instance and anchored on a reliable slot… The author encodes 'base is my anchor' by
referencing it — PackSmith never discovers it."* This is that parameterisation.

A template is an ordinary filter with **parameter tokens** in it::

    @b:base_block @slot

For `granite` × `polished.stairs` that resolves to `granite polished stairs`, and the
candidate query runs over the slot's own registry.

Parameters come in two kinds, and keeping them apart is what makes the syntax compose:

* **``@`` + a field sigil** — *this row's value for that field*. ``@b:base_block`` reads as
  "the blueprint slot `base_block`, resolved here", reusing the same ``b:`` §3.2.3 already
  gives filter text (`HAS b:polished.wall`). ``@t:`` and ``@a:`` fall out for free the day
  a registry table grows a recommendation surface (§5.3's third Live Question).
* **``@slot`` / ``@group`` / ``@leaf`` / ``@instance``** — *where the cell is*, not the
  value of anything. They take no sigil because they aren't fields.

  ``@slot`` deliberately does **not** say "column". A column is presentation — render the
  same blueprint through §5.2's node view and it's a node in a tree, not a column — whereas
  the *slot* being filled is part of the schema, which is what a template actually needs to
  reason about. ``@group`` and ``@leaf`` are its halves, for when the whole path is wrong:
  `@slot` on a `*.base` column searches for the literal token `base`, and a slot name is a
  label the user chose, not a word any mod puts in an id.

**No new AST node.** A parameter is a token that happens to start with ``@``, so a
template is still plain serializable data: it saves, round-trips and prints like any other
query. Resolution rewrites those strings into concrete ones *before* evaluation.

**Value parameters drop the namespace, and that is not cosmetic.** `@b:base_block` on
granite is `minecraft:granite`, which tokenises to {minecraft, granite} — and requiring
`minecraft` finds **0** of the 6 cross-mod granite brick walls instead of all 6. Searching
across mods is the entire purpose, so the mod name must not survive into the tokens.

"""
import re

from packsmith.core.query.ast import (
    And, Cmp, Has, Id, Not, Or, Query, Registry, QueryError,
)

_PARAM = re.compile(r"^@([A-Za-z_][A-Za-z0-9_]*)(?::(.+))?$")


class UnresolvedParam(QueryError):
    """A template referenced something this cell has no value for — almost always an
    anchor slot that isn't bound yet. Not a crash: the bar goes red and the cell simply
    has no suggestions until the anchor is filled."""


def parameters(node) -> list:
    """Every parameter a template references, in encounter order, without repeats.

    De-duplication matters because a bare-word template lowers to *two* clauses — one over
    the id, one over the display name — so every parameter is genuinely present twice.
    """
    found = []
    for value in _token_lists(node):
        for token in value:
            if isinstance(token, str) and token.startswith("@") and token not in found:
                found.append(token)
    return found


def resolve(node, context: dict):
    """Rewrite parameter tokens into concrete ones. Raises :class:`UnresolvedParam`."""
    if node is None:
        return None
    if isinstance(node, (And, Or)):
        clauses = [resolve(c, context) for c in node.clauses]
        return type(node)(clauses)
    if isinstance(node, Not):
        return Not(resolve(node.clause, context))
    if isinstance(node, Has):
        return node
    if isinstance(node, Cmp):
        if node.op != "matches_tokens":
            return node
        resolved = []
        for token in node.value:
            if not (isinstance(token, str) and token.startswith("@")):
                resolved.append(token)
                continue
            # Anything starting with '@' is *meant* to be a parameter. Falling back to
            # treating a malformed one as a literal would search for the text "@b:" and
            # quietly return nothing.
            match = _PARAM.match(token)
            if match is None:
                raise UnresolvedParam(
                    f"'{token}' is not a valid parameter — try @slot, @instance, or "
                    f"@b:slot_path")
            resolved.extend(_lookup(match.group(1), match.group(2), context))
        if not resolved:
            raise UnresolvedParam("nothing to search for once the template is resolved")
        return Cmp(node.field, node.op, sorted(set(resolved)), node.ci)
    return node


def _lookup(name, argument, context) -> list:
    name = name.lower()
    if name == "slot":
        path = context.get("col")
        if not path:
            raise UnresolvedParam("@slot has no slot here")
        return path.replace(".", " ").split()
    if name in ("group", "leaf"):
        # The pieces of @slot, separately, because a slot's *name* is often not a word that
        # appears in any id: the base form of a cut is `chiseled_granite`, never
        # `chiseled_granite_base`. A per-column override writes `@b:base_block @group` and
        # the literal `base` stops poisoning the search.
        path = context.get("col") or ""
        if name == "leaf":
            return [path.rsplit(".", 1)[-1]] if path else []
        group = path.rsplit(".", 1)[0] if "." in path else ""
        if not group:
            raise UnresolvedParam("@group is empty — this column isn't inside a group")
        return group.replace(".", " ").split()
    if name == "instance":
        instance = context.get("instance")
        if not instance:
            raise UnresolvedParam("@instance has no row here")
        return [instance]
    if name == "b":
        if not argument:
            raise UnresolvedParam("@b: needs a slot path, e.g. @b:base_block")
        value = (context.get("slots") or {}).get(argument)
        if value in (None, ""):
            raise UnresolvedParam(f"'{argument}' isn't bound on this row yet")
        return [strip_namespace(str(value))]
    if name in ("t", "a"):
        # The same syntax will mean "this entry's tag / attribute value" when a registry
        # table grows a recommendation surface (§5.3). Saying so beats a generic error.
        raise UnresolvedParam(
            f"@{name}: isn't available on a blueprint — a row here is an instance, not a "
            f"registry entry")
    raise UnresolvedParam(f"unknown parameter @{name}")


def strip_namespace(value: str) -> str:
    """`minecraft:granite` -> `granite`. See the module docstring: keeping the namespace
    turns a 6-candidate search into a 0-candidate one."""
    return value.split(":", 1)[1] if ":" in value else value


def context_for(store, blueprint: str, instance: str, slot) -> dict:
    """The values a template can reference for one cell."""
    bindings = {}
    for path, binding in store.bindings(blueprint, instance).items():
        bindings[path] = binding.value
    return {"col": slot.path, "instance": instance, "slots": bindings}


def candidate_query(template, slot, *, limit: int = None) -> Query:
    """A resolved template becomes a search over the slot's OWN registry.

    The scope is never written by the author — a slot already declares what it accepts,
    and §5.3 is firm that a recommendation "must never restrict": the type is the wall, the
    recommendation only ranks what's inside it.
    """
    if slot.type != "registry":
        raise UnresolvedParam(
            f"'{slot.path}' holds a {slot.describe()}, which has no registry to search")
    return Query(scope=Registry(slot.registry_type), select=[Id], filter=template,
                 order_by=[Id], limit=limit)


def _token_lists(node):
    if node is None:
        return
    if isinstance(node, (And, Or)):
        for clause in node.clauses:
            yield from _token_lists(clause)
    elif isinstance(node, Not):
        yield from _token_lists(node.clause)
    elif isinstance(node, Cmp) and node.op == "matches_tokens":
        yield list(node.value)
