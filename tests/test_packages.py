"""On-disk packages: parse a manifest, index actions, resolve ref → callable,
and run a manifest-loaded action end-to-end through the runner (design 3.3.1)."""
import pytest

from packsmith.core.packages import load_package, PackageIndex
from packsmith.core.bindings import resolve_step, best_guess_bindings
from packsmith.core.runner import run_action

MANIFEST = """
[package]
name = "demo_suite"
version = "0.1.0"
author = "test"
description = "a demo package"

[[actions]]
id = "mark_queued"
file = "actions.star"
function = "mark_queued"
name = "Mark Queued"
description = "marks remove items as queued"

[actions.mappings.source]
kind = "tag"
tag_type = "bool"
registry_type = "minecraft:item"
likely_name = "remove"

[actions.mappings.target]
kind = "tag"
tag_type = "bool"
registry_type = "minecraft:item"

[actions.configuration.registry_type]
type = "string"
default = "minecraft:item"
"""

ACTION_STAR = '''
def mark_queued(pack):
    reg = pack.step.config.get("registry_type", "minecraft:item")
    for eid in pack.tags.query(reg, pack.step.mappings["source"], True):
        pack.tags.write(reg, eid, pack.step.mappings["target"], True)
    pack.log("info", "marked")
'''


class FakeDump:
    registry = {}

    def attribute(self, registry_type, entry_id, name):
        return None


@pytest.fixture
def package_root(tmp_path):
    """A packages/ dir containing one demo_suite package."""
    pkg = tmp_path / "packages" / "demo_suite"
    pkg.mkdir(parents=True)
    (pkg / "manifest.toml").write_text(MANIFEST, encoding="utf-8")
    (pkg / "actions.star").write_text(ACTION_STAR, encoding="utf-8")
    return tmp_path / "packages"


def test_load_package_parses_manifest(package_root):
    pkg = load_package(package_root / "demo_suite")
    assert pkg.name == "demo_suite" and pkg.version == "0.1.0"
    assert len(pkg.actions) == 1
    a = pkg.actions[0]
    assert a.ref == "demo_suite:mark_queued"
    assert a.file == "actions.star" and a.function == "mark_queued"
    assert a.name == "Mark Queued"
    # mappings + config are parsed into typed slots
    assert a.mappings["source"].tag_type == "bool"
    assert a.mappings["source"].kind == "tag"
    assert a.config["registry_type"].default == "minecraft:item"


def test_index_scans_and_resolves_ref(package_root):
    index = PackageIndex(package_root)
    assert "demo_suite:mark_queued" in index.actions
    assert index.get("demo_suite:mark_queued").function == "mark_queued"


def test_load_callable_imports_the_function(package_root):
    index = PackageIndex(package_root)
    fn = index.load_callable("demo_suite:mark_queued")
    assert callable(fn)


def test_manifest_loaded_action_runs_through_the_runner(package_root, tags):
    tags.define("minecraft:item", "remove", "bool", default=False)
    tags.define("minecraft:item", "queued", "bool", default=False)
    tags.assign("minecraft:item", "quark:rope", "remove", True, owner="user")

    fn = PackageIndex(package_root).load_callable("demo_suite:mark_queued")
    result = run_action(fn, tag_store=tags, packdump=FakeDump(),
                        action_ref="demo_suite:mark_queued",
                        mappings={"source": "remove", "target": "queued"})
    assert result.ok
    assert tags.get_ownership("minecraft:item", "quark:rope", "queued") == {
        "kind": "action", "action_ref": "demo_suite:mark_queued",
    }


def test_full_chain_manifest_to_bindings_to_run(package_root, tags):
    """Capstone: manifest → best-guess/resolve bindings → load_callable → run."""
    tags.define("minecraft:item", "remove", "bool", default=False)
    tags.define("minecraft:item", "queued", "bool", default=False)
    tags.assign("minecraft:item", "quark:rope", "remove", True, owner="user")

    index = PackageIndex(package_root)
    manifest = index.get("demo_suite:mark_queued")

    # best-guess fills `source` from the `remove` tag (its likely_name); user picks target
    guesses = best_guess_bindings(manifest, tags)
    assert guesses["source"] == "remove"
    bindings = {**guesses, "target": "queued"}

    mappings, config = resolve_step(manifest, bindings=bindings, config={}, tag_store=tags)
    assert config["registry_type"] == "minecraft:item"        # config default applied

    fn = index.load_callable("demo_suite:mark_queued")
    result = run_action(fn, tag_store=tags, packdump=FakeDump(),
                        action_ref="demo_suite:mark_queued", mappings=mappings, config=config)
    assert result.ok
    assert tags.get_ownership("minecraft:item", "quark:rope", "queued") == {
        "kind": "action", "action_ref": "demo_suite:mark_queued",
    }


def test_missing_manifest_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_package(tmp_path / "does_not_exist")


def test_unknown_action_ref_raises(package_root):
    index = PackageIndex(package_root)
    with pytest.raises(KeyError):
        index.get("nope:nope")


def test_scan_ignores_non_package_dirs(tmp_path):
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "not_a_package").mkdir()          # no manifest.toml
    (tmp_path / "packages" / "loose.txt").write_text("x", encoding="utf-8")
    index = PackageIndex(tmp_path / "packages")
    assert index.actions == {}


# --- provenance (design 3.3.1) ---------------------------------------------
# "Purely a provenance label — structurally, authored and downloaded packages are
# identical." It governs EDITABILITY, not behaviour: you may edit what you wrote, not
# what you installed (design 6.3).

def test_packages_are_authored_unless_they_say_otherwise(package_root):
    pkg = load_package(package_root / "demo_suite")
    assert pkg.provenance == "authored"


def test_downloaded_provenance_is_parsed(tmp_path):
    pkg = tmp_path / "vendored"
    pkg.mkdir()
    (pkg / "manifest.toml").write_text(
        '[package]\nname = "vendored"\nprovenance = "downloaded"\n', encoding="utf-8")
    assert load_package(pkg).provenance == "downloaded"


def test_unknown_provenance_is_rejected(tmp_path):
    pkg = tmp_path / "weird"
    pkg.mkdir()
    (pkg / "manifest.toml").write_text(
        '[package]\nname = "weird"\nprovenance = "borrowed"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="provenance"):
        load_package(pkg)


def test_index_exposes_packages_by_name(package_root):
    index = PackageIndex(package_root)
    assert index.package("demo_suite").name == "demo_suite"
    assert index.package("nope") is None
    assert "demo_suite" in index.packages
