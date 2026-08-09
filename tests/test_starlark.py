"""Actions written in Starlark (design 7.6) — the language boundary.

Two things are being checked here. First that the **capability surface survives the
crossing**: `pack` is assembled inside Starlark out of injected callables, baked-in data,
and partial-bound handles, and it has to behave exactly like the Python object it mirrors.
Second that the **sandbox is real** — the reason §3.3 chose Starlark is that a downloaded
action *cannot* reach the disk, the network, or the clock except through `pack`, and that
claim deserves tests rather than trust.
"""
import pytest

from packsmith.core.files import FileStore, FileStaging
from packsmith.core.pack import Pack, ActionFailure
from packsmith.core.staging import L2Staging
from packsmith.core.starlark_runtime import run_starlark, StarlarkActionError

REG = "minecraft:item"


class FakeDump:
    def __init__(self):
        self.registry = {REG: {"values": ["quark:rope", "minecraft:diamond"]}}

    def attribute(self, registry_type, entry_id, name):
        return "Rope" if entry_id == "quark:rope" and name == "localization" else None


@pytest.fixture
def pack(tags, user_db, tmp_path):
    tags.define(REG, "remove", "bool", default=False)
    tags.define(REG, "queued", "bool", default=False)
    tags.assign(REG, "quark:rope", "remove", True, owner="user")
    root = tmp_path / "instance"
    root.mkdir()
    return Pack(
        staging=L2Staging(tags),
        file_staging=FileStaging(FileStore(user_db, root)),
        tag_store=tags, packdump=FakeDump(), action_ref="demo:act",
        mappings={"source": "remove", "target": "queued"},
        config={"registry_type": REG, "loud": True, "limit": 5},
        conflict_policies={"queued": "overwrite"},
    )


def run(src, pack, **kw):
    return run_starlark(src, pack, **kw)


# --- the capability surface -------------------------------------------------

def test_registry_reads(pack):
    src = """
def run(pack):
    return [
        len(pack.registry.entries("minecraft:item")),
        pack.registry.has("minecraft:item", "quark:rope"),
        pack.registry.attribute("minecraft:item", "quark:rope", "localization"),
    ]
"""
    assert run(src, pack) == [2, True, "Rope"]


def test_tag_read_and_write_round_trip(pack, tags):
    src = """
def run(pack):
    ids = pack.tags.query("minecraft:item", pack.step.mappings["source"], True)
    for i in ids:
        pack.tags.write("minecraft:item", i, pack.step.mappings["target"], True)
    return ids
"""
    assert run(src, pack) == ["quark:rope"]
    assert pack.tags.get(REG, "quark:rope", "queued") is True     # staged, read-your-writes


def test_step_data_crosses_as_real_values(pack):
    """mappings/config are data, not callables — they're baked into the prelude as
    literals, and must arrive as ordinary subscriptable Starlark values."""
    src = """
def run(pack):
    return [
        pack.step.mappings["source"],
        pack.step.config["registry_type"],
        pack.step.config["loud"],
        pack.step.config["limit"] + 1,
        pack.step.config.get("missing", "fallback"),
    ]
"""
    assert run(src, pack) == ["remove", REG, True, 6, "fallback"]


def test_action_ref_is_exposed(pack):
    assert run("def run(pack):\n    return pack.action_ref", pack) == "demo:act"


def test_log_lines_reach_the_host(pack):
    run('def run(pack):\n    pack.log("info", "hello from starlark")', pack)
    assert ("info", "hello from starlark") in pack.log_lines


def test_filesystem_handles_preserve_resolve_then_operate(pack):
    """§7.3's resolver -> handle -> operation shape: `resolve` returns a struct whose
    operations already know their path (bound with `partial`)."""
    src = """
def run(pack):
    h = pack.filesystem.resolve("config/c.json")
    before = h.exists()
    h.write_json({"a": 1, "b": [2, 3]})
    return [before, h.path]
"""
    assert run(src, pack) == [False, "config/c.json"]
    assert pack.filesystem.resolve("config/c.json").read_json() == {"a": 1, "b": [2, 3]}


def test_a_custom_entry_point_name_is_honoured(pack):
    src = 'def do_thing(pack):\n    return "ran"'
    assert run(src, pack, function="do_thing") == "ran"


# --- failure vs. crash ------------------------------------------------------

def test_fail_surfaces_as_an_action_failure_with_the_authors_reason(pack):
    """starlark-pyo3 wraps host exceptions, so ActionFailure can't survive by type — the
    flag Pack.fail() sets is what distinguishes a deliberate refusal from a bug."""
    with pytest.raises(ActionFailure) as excinfo:
        run('def run(pack):\n    pack.fail("not today")', pack)
    assert excinfo.value.reason == "not today"


def test_a_bad_reference_is_a_starlark_error_with_a_source_location(pack):
    with pytest.raises(StarlarkActionError) as excinfo:
        run("def run(pack):\n    return nonexistent_thing()", pack)
    message = str(excinfo.value)
    assert "nonexistent_thing" in message
    assert "action.star" in message          # carries file:line for the user


def test_a_syntax_error_is_reported_not_raised_raw(pack):
    with pytest.raises(StarlarkActionError):
        run("def run(pack)\n    broken", pack)


def test_a_host_error_is_not_mistaken_for_a_deliberate_failure(tags):
    """An exception raised *inside the host* must report as a crash, not as an authored
    refusal — the two are only distinguishable by the flag, since both arrive wrapped."""
    bare = Pack(staging=L2Staging(tags), tag_store=tags, packdump=FakeDump(),
                action_ref="demo:act")           # no file_staging for this step
    with pytest.raises(StarlarkActionError) as excinfo:
        run('def run(pack):\n    pack.filesystem.resolve("x.txt").write("y")', bare)
    assert "filesystem" in str(excinfo.value).lower()
    assert bare.failure_reason is None           # nothing pretended this was a fail()


# --- the sandbox is the point (design 3.3 / 7.4) ----------------------------

@pytest.mark.parametrize("forbidden", [
    'open("x")',
    '__import__("os")',
    'eval("1+1")',
    'getattr(pack, "fail")',
    'exec("x = 1")',
    'globals()',
])
def test_the_outside_world_is_unreachable(pack, forbidden):
    with pytest.raises(StarlarkActionError):
        run(f"def run(pack):\n    return {forbidden}", pack)


@pytest.mark.parametrize("construct", [
    "try:\n        pass\n    except:\n        pass",     # §3.3: no exceptions, hence fail()
    "while True:\n        break",                        # bounded iteration only
    "import os",
])
def test_language_constructs_the_design_relies_on_being_absent(pack, construct):
    with pytest.raises(StarlarkActionError):
        run(f"def run(pack):\n    {construct}", pack)


def test_actions_cannot_stash_pack_for_ambient_use(pack):
    """§7.6: no global `pack` binding. A helper must be handed it explicitly, which is
    what keeps every capability call locatable for §7.2's static analysis."""
    src = """
_stashed = None

def run(pack):
    _stashed = pack
    return _helper()

def _helper():
    return _stashed.action_ref
"""
    with pytest.raises(StarlarkActionError):
        run(src, pack)


def test_helpers_work_when_pack_is_passed_explicitly(pack):
    src = """
def run(pack):
    return _helper(pack)

def _helper(pack):
    pack.log("info", "from a helper")
    return pack.action_ref
"""
    assert run(src, pack) == "demo:act"
    assert ("info", "from a helper") in pack.log_lines
