"""The `pack` surface, described for the editor's completion providers (design 6.3).

**Derived, never hand-written.** A parallel table of "what `pack` offers" would be a second
source of truth that drifts the moment a capability is added — and this project has already
paid for exactly that failure once: `pack.datapacks` existed in Python and not in Starlark
for nine commits without anything noticing. So the catalog is introspected from the real
:class:`~packsmith.core.pack.Pack`: names and signatures from `inspect`, help text from the
docstrings that are already on the methods.

Two consequences worth stating, because they are the whole reason this is built this way:

* **Adding a capability updates the editor for free.** A new method on `_Tags` shows up in
  autocomplete with its signature and its docstring, with nothing here to remember.
* **It cannot advertise something an action can't call.** `tests/test_pack_surface.py`
  asserts every member of `Pack` is reachable from Starlark, so "in the catalog" and "in
  the prelude" cannot come apart without a test failing.

What this deliberately does NOT include is anything dynamic — registry ids, the user's tag
names, a step's mapping slots. Those need a profile and a round trip; the `pack` surface is
the same in every profile, so it ships as one static blob pushed at editor start.
"""
import inspect

# Private on purpose over there — a handle is reached by calling a resolver, never named by
# an author — but the editor has to describe its members, and reading them off the real
# class is the point. `test_pack_surface.py` imports it for the same reason.
from packsmith.core.pack import Pack, _FileHandle

# Mirrors `test_pack_surface.HOST_ONLY`: members of the Python object that are the runtime's
# business rather than an author's. Kept as its own copy rather than imported from the test,
# because production code reaching into the suite would be the wrong direction — the two
# lists agreeing is asserted there.
HOST_ONLY = {"failure_reason", "log_lines"}

# Resolvers whose return value is a `_FileHandle`. Named explicitly because the catalog
# describes a *surface*, and no amount of introspection can see through a call to find out
# what comes back — Python signatures here carry no return annotation.
HANDLE_RESOLVERS = ("filesystem", "datapacks", "resourcepacks")

# Assembled from stubs purely to be walked. Nothing on it is ever called, which is why every
# collaborator can be None; `blueprint_store` has to be non-None only because `Pack.__init__`
# leaves `pack.blueprints` as None without one, and the whole namespace would vanish.
_SHAPE = Pack(staging=None, tag_store=None, packdump=None, action_ref="catalog:only",
              blueprint_store=object())

# Likewise never operated on — a handle with no staging behind it, existing so its methods
# can be read as BOUND methods (see `_entry`).
_HANDLE = _FileHandle(None, "", "")


def _doc(value) -> str:
    return inspect.getdoc(value) or ""


def _entry(owner, name: str) -> dict:
    """One completion item: what it is called, how it is called, and what it does.

    Read off the *instance* rather than the class, so bound methods report the signature an
    author actually writes — `write(content, *, file_must_exist=False)` and not a leading
    `self`. Properties are the exception: on an instance `path` is already the string it
    returns, so its documentation has to come off the class before that happens.
    """
    declared = getattr(type(owner), name, None)
    if isinstance(declared, property):
        return {"name": name, "kind": "value", "detail": "",
                "doc": _doc(declared.fget), "params": []}

    value = getattr(owner, name)
    if not callable(value):
        # Plain data (`pack.action_ref`, `pack.step.mappings`). Deliberately no doc: the
        # only docstring reachable here belongs to `str` or `dict` and is Python's, not
        # ours — showing it would put library boilerplate in a tooltip about `pack`.
        return {"name": name, "kind": "value", "detail": type(value).__name__,
                "doc": "", "params": []}
    try:
        signature = inspect.signature(value)
    except (TypeError, ValueError):     # a builtin or C-level callable — describe it plainly
        return {"name": name, "kind": "function", "detail": "(…)", "doc": _doc(value),
                "params": []}
    return {
        "name": name,
        "kind": "function",
        "detail": str(signature),
        "doc": _doc(value),
        "params": [str(param) for param in signature.parameters.values()],
    }


def _public(obj):
    return sorted(name for name in dir(obj) if not name.startswith("_"))


def _is_namespace(value) -> bool:
    """A sub-object of `pack` (`pack.tags`), as opposed to a leaf like `pack.action_ref`.

    Identified by being defined in `pack.py` itself and not callable — the same rule
    `test_pack_surface` walks by, so the two derivations describe the same tree.
    """
    return type(value).__module__ == Pack.__module__ and not callable(value)


def catalog() -> dict:
    """The whole surface as plain JSON-able data, ready to hand to the editor page."""
    members = {"": []}
    for name in _public(_SHAPE):
        if name in HOST_ONLY:
            continue
        value = getattr(_SHAPE, name)
        if _is_namespace(value):
            members[""].append({"name": name, "kind": "namespace", "detail": "",
                                "doc": _doc(type(value)), "params": []})
            members[name] = [_entry(value, member) for member in _public(value)]
        else:
            members[""].append(_entry(_SHAPE, name))
    return {
        "members": members,
        "handle": [_entry(_HANDLE, name) for name in _public(_FileHandle)],
        "handleResolvers": list(HANDLE_RESOLVERS),
        "rootDoc": _doc(Pack),
    }
