"""`pack` in Python and `pack` in Starlark are supposed to be the same object.

They are two artifacts that nothing links. `core/pack.py` builds the real capability
object; `core/starlark_runtime.py`'s `_prelude` assembles a Starlark struct that mirrors
it out of injected callables. A namespace added to one and not the other is invisible —
which is exactly what happened to `pack.datapacks`, `pack.resourcepacks` and
`pack.capabilities`. All three were built, tested, and wired into the GUI, and stayed
unreachable from the only language that writes actions for nine commits.

**Why the existing tests didn't catch it**, which is the part worth remembering: the 25
tests in `test_pack_mapping.py` hand `run_action` a plain Python callable — §1.1's MVP
language seam, still real code but no longer the path any action takes. So they exercised
manifest parsing, binding validation, best-guess fill, provider routing, staging and the
write itself, and every one of them passed. Every layer was covered except the one an
author actually stands on, and a `def body(pack):` that reads like Starlark is what made
the gap invisible.

So this file derives the expected surface **from the Python object by introspection** and
asserts each member is reachable from real Starlark source. Adding a capability to `Pack`
now fails here until the prelude carries it, the same way a new packdump holder fails the
walk in `test_packdump_adopt` until it implements `set_packdump`.
"""
import pytest

from packsmith.core.capabilities import PackTargets, resolve
from packsmith.core.files import FileStore, FileStaging
from packsmith.core.pack import Pack, _FileHandle
from packsmith.core.staging import L2Staging
from packsmith.core.starlark_runtime import run_starlark
from packsmith.integrations.paxi import PaxiProvider

REG = "minecraft:item"

# Members of the Python `Pack` that are deliberately NOT part of the author-facing surface.
# Each needs a reason, because "it isn't in the prelude" is the bug this file exists to
# catch — an entry here has to be a decision, not an omission.
HOST_ONLY = {
    # Set by `fail()` *before* it raises, because starlark-pyo3 wraps host exceptions and
    # the type alone can't tell a deliberate refusal from a genuine bug. The runtime reads
    # it; an action must not be able to read or forge it.
    "failure_reason",
    # The runner drains this after the step to build the run log. An action writes to it
    # through `pack.log(...)`, which IS exposed.
    "log_lines",
}


class _Dump:
    """A pack with Paxi installed, as far as detection is concerned."""

    def __init__(self):
        self.mods = {"paxi": {"mod_id": "paxi"}}
        self.registry = {REG: {"values": ["quark:rope"]}}

    def attribute(self, *_a):
        return None


# --- deriving the expected surface -----------------------------------------------------
#
# Built from a `Pack` assembled out of stubs at import time. Nothing on it is ever CALLED —
# it exists only to be walked with `dir()`, which is why every collaborator can be None or
# a bare object. Doing it at module scope is what lets the case list drive
# `@parametrize`; the tests themselves run against the real fixture below.

_SHAPE = Pack(staging=None, tag_store=None, packdump=None, action_ref="shape:only",
              blueprint_store=object())


def _public(obj):
    return sorted(name for name in dir(obj) if not name.startswith("_"))


def _cases():
    """Every `pack.…` path an action should be able to reach, one per assertion.

    A member whose value is one of `pack.py`'s own namespace objects is descended into;
    anything else (a string, a bound method, a dict) is a leaf.
    """
    out = []
    for name in _public(_SHAPE):
        if name in HOST_ONLY:
            continue
        value = getattr(_SHAPE, name)
        if type(value).__module__ == Pack.__module__ and not callable(value):
            out.extend(f"{name}.{member}" for member in _public(value))
        else:
            out.append(name)
    return out


CASES = _cases()

# The one shape §7.3 promises is uniform across every resolver that produces it.
HANDLE_MEMBERS = _public(_FileHandle)

RESOLVERS = {
    "filesystem": 'pack.filesystem.resolve("notes.txt")',
    "datapacks": 'pack.datapacks.resolve(pack="tweaks", namespace="minecraft", '
                 'path="loot_tables/blocks/oak_leaves.json")',
    "resourcepacks": 'pack.resourcepacks.resolve(pack="textures", namespace="minecraft", '
                     'path="textures/block/stone.png")',
}


@pytest.fixture
def pack(tags, user_db, tmp_path):
    """A fully-equipped `Pack` — every namespace present and genuinely usable.

    Deliberately not minimal. A thinner one would leave `pack.blueprints` as None and
    `pack_targets` absent, and the introspection above would then quietly derive a much
    smaller surface — a file that passes by checking less. `test_the_derived_surface_is_
    not_thin` below is the guard that makes that failure loud rather than silent.
    """
    from packsmith.core.blueprints import BlueprintStore

    tags.define(REG, "remove", "bool", default=False)
    root = tmp_path / "instance"
    paxi = PaxiProvider()
    paxi.create_pack(root, "tweaks")
    paxi.create_pack(root, "textures", kind="resourcepacks")
    dump = _Dump()
    return Pack(
        staging=L2Staging(tags),
        file_staging=FileStaging(FileStore(user_db, root)),
        tag_store=tags, packdump=dump, action_ref="demo:act",
        blueprint_store=BlueprintStore(user_db, dump),
        pack_targets=PackTargets(resolve(dump, loaders=(paxi,)), root),
        mappings={"out": "tweaks"}, config={},
    )


def reaches(pack, expression):
    """Run Starlark that resolves `expression` and nothing else.

    Binding to a local is what forces the attribute lookup; a missing member is a Starlark
    error naming it. `True` comes back so the assertion also proves the snippet ran, rather
    than passing because nothing was evaluated.
    """
    return run_starlark(f"def run(pack):\n    _it = {expression}\n    return True\n", pack)


# --- the parity assertions -------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=CASES)
def test_every_member_of_pack_is_reachable_from_starlark(pack, case):
    assert reaches(pack, f"pack.{case}") is True


@pytest.mark.parametrize("resolver", sorted(RESOLVERS), ids=sorted(RESOLVERS))
@pytest.mark.parametrize("member", HANDLE_MEMBERS, ids=HANDLE_MEMBERS)
def test_every_resolver_produces_the_same_handle(pack, resolver, member):
    """§7.3: a handle "exposes this uniform operation surface, regardless of which resolver
    produced it". The provider-routed resolvers get there by handing the prelude a routed
    path and letting `_resolve` build the handle, so there is one shape rather than three
    that have to be kept in step by hand — this is what pins that down."""
    assert reaches(pack, f"{RESOLVERS[resolver]}.{member}") is True


def test_the_derived_surface_is_not_thin(pack):
    """A guard on the guard.

    Every assertion above is generated by introspection, so a `Pack` that quietly lost a
    namespace — or a `_cases()` that stopped descending into them — would shrink the case
    list toward nothing and this file would pass by checking almost nothing. That is the
    exact failure mode the vacuous tests earlier in this project had, so the case list
    gets its own floor.
    """
    namespaces = {case.split(".")[0] for case in CASES if "." in case}
    assert {"registry", "tags", "blueprints", "filesystem", "datapacks",
            "resourcepacks", "capabilities", "step"} <= namespaces
    assert len(CASES) >= 30, CASES
    assert "write" in HANDLE_MEMBERS and "path" in HANDLE_MEMBERS


def test_host_only_members_really_are_on_the_python_object():
    """Otherwise the exclusion list rots into a lie: a renamed attribute would leave a
    stale entry here silently excusing a member that no longer exists, and the real one
    would go unchecked."""
    assert HOST_ONLY <= set(_public(_SHAPE))
