"""On-disk packages: parse a manifest, index actions, resolve ref → callable,
and run a manifest-loaded action end-to-end through the runner (design 3.3.1)."""
import pytest

from packsmith.core.packages import load_package, PackageIndex
from packsmith.core.bindings import resolve_step, best_guess_bindings
from packsmith.core.runner import run_action

MANIFEST = """
{
  "package": {
    "name": "demo_suite",
    "version": "0.1.0",
    "author": "test",
    "description": "a demo package",
  },
  "actions": [
    {
      "id": "mark_queued",
      "file": "actions.star",
      "function": "mark_queued",
      "name": "Mark Queued",
      "description": "marks remove items as queued",
      "mappings": {
        "source": {
          "kind": "tag", "tag_type": "bool",
          "registry_type": "minecraft:item", "likely_name": "remove",
        },
        "target": {
          "kind": "tag", "tag_type": "bool", "registry_type": "minecraft:item",
        },
      },
      "configuration": {
        "registry_type": { "type": "string", "default": "minecraft:item" },
      },
    },
  ],
}
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
    (pkg / "manifest.json5").write_text(MANIFEST, encoding="utf-8")
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
    # A binding stores the tag's ID (design 3.2.1) — stable across renames.
    assert guesses["source"] == tags.definition("minecraft:item", "remove")["id"]
    # ...and a legacy name-binding still resolves alongside it, which is what lets existing
    # job rows keep working without a migration.
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
    (tmp_path / "packages" / "not_a_package").mkdir()          # no manifest.json5
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
    (pkg / "manifest.json5").write_text(
        '{"package": {"name": "vendored", "provenance": "downloaded"}}', encoding="utf-8")
    assert load_package(pkg).provenance == "downloaded"


def test_unknown_provenance_is_rejected(tmp_path):
    pkg = tmp_path / "weird"
    pkg.mkdir()
    (pkg / "manifest.json5").write_text(
        '{"package": {"name": "weird", "provenance": "borrowed"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="provenance"):
        load_package(pkg)


def test_index_exposes_packages_by_name(package_root):
    index = PackageIndex(package_root)
    assert index.package("demo_suite").name == "demo_suite"
    assert index.package("nope") is None
    assert "demo_suite" in index.packages


# --- authoring packages and actions (design 3.3.1) -------------------------

def test_create_package_writes_a_loadable_manifest(tmp_path):
    from packsmith.core.packages import create_package
    pkg = create_package(tmp_path, "my_pack", description="mine")
    assert pkg.name == "my_pack" and pkg.provenance == "authored"
    assert (tmp_path / "my_pack" / "manifest.json5").is_file()
    assert load_package(tmp_path / "my_pack").description == "mine"


def test_create_package_rejects_bad_names(tmp_path):
    from packsmith.core.packages import create_package
    for bad in ("Has Space", "../escape", "9lives", "UPPER", ""):
        with pytest.raises(ValueError):
            create_package(tmp_path, bad)


def test_create_package_rejects_duplicates(tmp_path):
    from packsmith.core.packages import create_package
    create_package(tmp_path, "twice")
    with pytest.raises(ValueError):
        create_package(tmp_path, "twice")


def test_add_action_appends_and_writes_a_stub(tmp_path):
    from packsmith.core.packages import create_package, add_action
    pkg = create_package(tmp_path, "mine")
    source = add_action(pkg, "do_thing", name="Do Thing", description="does the thing")
    assert source.name == "do_thing.star" and source.is_file()
    assert "def run(pack):" in source.read_text(encoding="utf-8")

    reloaded = load_package(pkg.root)
    action = reloaded.actions[0]
    assert action.ref == "mine:do_thing"
    assert action.file == "do_thing.star" and action.function == "run"
    assert action.name == "Do Thing"


def test_add_action_preserves_handwritten_manifest_content(tmp_path):
    """The manifest is spliced as text, never regenerated — a JSON round-trip would
    silently eat the user's comments."""
    from packsmith.core.packages import create_package, add_action
    pkg = create_package(tmp_path, "mine")
    manifest = pkg.root / "manifest.json5"
    manifest.write_text(manifest.read_text(encoding="utf-8")
                        + "\n// a comment the user wrote\n", encoding="utf-8")
    add_action(pkg, "thing")
    after = manifest.read_text(encoding="utf-8")
    assert "// a comment the user wrote" in after
    assert load_package(pkg.root).actions[0].action_id == "thing"


def test_add_action_rejects_duplicate_ids(tmp_path):
    from packsmith.core.packages import create_package, add_action
    pkg = create_package(tmp_path, "mine")
    add_action(pkg, "thing")
    with pytest.raises(ValueError):
        add_action(load_package(pkg.root), "thing")


def test_cannot_add_an_action_to_a_downloaded_package(tmp_path):
    from packsmith.core.packages import add_action
    root = tmp_path / "vendored"; root.mkdir()
    (root / "manifest.json5").write_text(
        '{"package": {"name": "vendored", "provenance": "downloaded"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="downloaded"):
        add_action(load_package(root), "thing")


def test_index_reload_picks_up_a_new_package(tmp_path):
    from packsmith.core.packages import create_package, add_action
    packages = tmp_path / "packages"; packages.mkdir()
    index = PackageIndex(packages)
    assert index.actions == {}
    add_action(create_package(packages, "fresh"), "go")
    index.reload()
    assert "fresh:go" in index.actions


# --- the two layers: declarations vs files ----------------------------------------

def test_many_actions_can_share_one_file(tmp_path):
    """The entry point is (file, function), and neither is tied to the action id."""
    from packsmith.core.packages import create_package, add_action
    pkg = create_package(tmp_path, "mine")
    add_action(pkg, "alpha", file="toolbox.star", function="alpha")
    add_action(load_package(pkg.root), "beta", file="toolbox.star", function="beta")

    reloaded = load_package(pkg.root)
    assert {a.action_id: (a.file, a.function) for a in reloaded.actions} == {
        "alpha": ("toolbox.star", "alpha"),
        "beta": ("toolbox.star", "beta"),
    }
    source = (pkg.root / "toolbox.star").read_text(encoding="utf-8")
    assert "def alpha(pack):" in source and "def beta(pack):" in source
    assert len(list(pkg.root.glob("*.star"))) == 1


def test_declaring_into_an_existing_file_leaves_its_contents_alone(tmp_path):
    from packsmith.core.packages import create_package, add_action, create_file
    pkg = create_package(tmp_path, "mine")
    create_file(pkg, "toolbox.star")
    source = pkg.root / "toolbox.star"
    source.write_text("# hand written\n\ndef helper():\n    return 1\n", encoding="utf-8")

    add_action(pkg, "go", file="toolbox.star", function="go")
    text = source.read_text(encoding="utf-8")
    assert "# hand written" in text and "def helper():" in text
    assert "def go(pack):" in text


def test_declaring_an_existing_function_does_not_append_a_second_stub(tmp_path):
    from packsmith.core.packages import create_package, add_action, create_file
    pkg = create_package(tmp_path, "mine")
    create_file(pkg, "toolbox.star")
    source = pkg.root / "toolbox.star"
    source.write_text("def go(pack):\n    pack.log('info', 'mine')\n", encoding="utf-8")
    add_action(pkg, "go", file="toolbox.star", function="go")
    assert source.read_text(encoding="utf-8").count("def go(pack):") == 1


def test_two_actions_cannot_share_one_entry_point(tmp_path):
    from packsmith.core.packages import create_package, add_action
    pkg = create_package(tmp_path, "mine")
    add_action(pkg, "alpha", file="toolbox.star", function="run")
    with pytest.raises(ValueError, match="already points at"):
        add_action(load_package(pkg.root), "beta", file="toolbox.star", function="run")


def test_create_file_declares_nothing(tmp_path):
    """The operation that used to be impossible — a library file with no manifest entry."""
    from packsmith.core.packages import create_package, create_file, source_files
    pkg = create_package(tmp_path, "mine")
    created = create_file(pkg, "helpers.star")
    assert created.is_file()
    reloaded = load_package(pkg.root)
    assert reloaded.actions == []
    assert source_files(reloaded) == ["manifest.json5", "helpers.star"]


def test_create_file_rejects_bad_names(tmp_path):
    """What is still refused: nothing, and anything that tries to leave the package.

    The extension rule used to be `.star` or bust, which made a `.json` fixture or a README
    impossible to add through the app even though a package is just a folder. The traversal
    cases stay refused, and they are refused by the *folder* rule — no segment naming a
    parent can pass it.
    """
    from packsmith.core.packages import create_package, create_file
    pkg = create_package(tmp_path, "mine")
    for bad in ("", "   ", "../escape.star", "..\\escape.star", "lib/../../out.star",
                "manifest.json5"):
        with pytest.raises(ValueError):
            create_file(pkg, bad)


def test_a_package_may_hold_files_that_are_not_starlark(tmp_path):
    """A package is a folder on disk, and an author needs things beside their code."""
    from packsmith.core.packages import create_package, create_file, source_files
    pkg = create_package(tmp_path, "mine")
    for name in ("ids.json", "README.md", "data/table.csv", "Notes.txt"):
        create_file(pkg, name)
    assert set(source_files(load_package(pkg.root))) == {
        "manifest.json5", "ids.json", "README.md", "data/table.csv", "Notes.txt"}


def test_only_starlark_files_get_the_starlark_stub(tmp_path):
    """The boilerplate explains `load()` and the `pack` argument — right for Starlark, and
    gibberish inside a JSON fixture (invalid JSON, at that)."""
    from packsmith.core.packages import create_package, create_file
    pkg = create_package(tmp_path, "mine")
    assert create_file(pkg, "helpers.star").read_text(encoding="utf-8").strip()
    assert create_file(pkg, "ids.json").read_text(encoding="utf-8") == ""


def test_a_file_with_no_extension_becomes_starlark(tmp_path):
    from packsmith.core.packages import create_package, create_file
    pkg = create_package(tmp_path, "mine")
    assert create_file(pkg, "helpers").name == "helpers.star"


def test_an_actions_entry_point_must_still_be_starlark(tmp_path):
    """The runner reads it as Starlark and `load()` addresses it, so the loose rule for
    plain files must not reach the one file that has to be code."""
    from packsmith.core.packages import create_package, add_action
    pkg = create_package(tmp_path, "mine")
    with pytest.raises(ValueError, match="invalid for an action"):
        add_action(pkg, "thing", file="thing.json")


def test_a_declared_file_cannot_be_renamed_out_of_starlark(tmp_path):
    """Otherwise the manifest points at an entry point the runner cannot read — loadable
    now, broken the moment a job runs it, far from the rename that caused it."""
    from packsmith.core.packages import create_package, add_action, rename_file
    pkg = create_package(tmp_path, "mine")
    add_action(pkg, "thing", file="thing.star")
    with pytest.raises(ValueError, match="invalid for an action"):
        rename_file(load_package(pkg.root), "thing.star", "thing.json")


def test_an_undeclared_file_may_be_renamed_to_anything(tmp_path):
    from packsmith.core.packages import create_package, create_file, rename_file
    pkg = create_package(tmp_path, "mine")
    create_file(pkg, "notes.star")
    assert rename_file(load_package(pkg.root), "notes.star", "notes.md").name == "notes.md"


def test_remove_action_undeclares_but_keeps_the_file(tmp_path):
    from packsmith.core.packages import create_package, add_action, remove_action
    pkg = create_package(tmp_path, "mine")
    add_action(pkg, "alpha", file="toolbox.star", function="alpha")
    add_action(load_package(pkg.root), "beta", file="toolbox.star", function="beta")

    remove_action(load_package(pkg.root), "alpha")
    reloaded = load_package(pkg.root)
    assert [a.action_id for a in reloaded.actions] == ["beta"]
    source = (pkg.root / "toolbox.star").read_text(encoding="utf-8")
    assert "def alpha(pack):" in source and "def beta(pack):" in source


def test_remove_action_takes_its_subtables_and_spares_the_rest(tmp_path):
    """Removing a block as text has to take the action's [actions.*] sub-tables with it,
    and touch nothing else in the file."""
    from packsmith.core.packages import remove_action
    root = tmp_path / "mine"; root.mkdir()
    (root / "manifest.json5").write_text('''// top comment
{
  "package": { "name": "mine" },
  "actions": [
    {
      "id": "doomed",
      "file": "a.star",
      "function": "run",
      "mappings": {
        "target": { "tag_type": "bool", "registry_type": "minecraft:item" },
      },
    },
    {
      "id": "kept",
      "file": "b.star",
      "function": "run",
      "mappings": { "other": { "tag_type": "string" } },
    },
  ],
}
''', encoding="utf-8")
    remove_action(load_package(root), "doomed")

    text = (root / "manifest.json5").read_text(encoding="utf-8")
    assert "// top comment" in text
    assert "doomed" not in text and "minecraft:item" not in text
    kept = load_package(root)
    assert [a.action_id for a in kept.actions] == ["kept"]
    assert list(kept.actions[0].mappings) == ["other"]


def test_remove_action_rejects_an_unknown_id(tmp_path):
    from packsmith.core.packages import create_package, remove_action
    pkg = create_package(tmp_path, "mine")
    with pytest.raises(ValueError, match="no action"):
        remove_action(pkg, "ghost")


def test_delete_file_is_refused_while_an_action_declares_it(tmp_path):
    from packsmith.core.packages import create_package, add_action, delete_file
    pkg = create_package(tmp_path, "mine")
    add_action(pkg, "go", file="toolbox.star")
    with pytest.raises(ValueError, match="still implements"):
        delete_file(load_package(pkg.root), "toolbox.star")
    assert (pkg.root / "toolbox.star").is_file()


def test_delete_file_works_once_undeclared(tmp_path):
    from packsmith.core.packages import (
        create_package, add_action, remove_action, delete_file)
    pkg = create_package(tmp_path, "mine")
    add_action(pkg, "go", file="toolbox.star")
    remove_action(load_package(pkg.root), "go")
    delete_file(load_package(pkg.root), "toolbox.star")
    assert not (pkg.root / "toolbox.star").exists()


def test_delete_file_refuses_the_manifest(tmp_path):
    from packsmith.core.packages import create_package, delete_file
    pkg = create_package(tmp_path, "mine")
    with pytest.raises(ValueError):
        delete_file(pkg, "manifest.json5")


def test_rename_file_retargets_declarations_and_spares_the_manifest(tmp_path):
    from packsmith.core.packages import create_package, add_action, rename_file
    pkg = create_package(tmp_path, "mine")
    manifest = pkg.root / "manifest.json5"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n// keep me\n",
                        encoding="utf-8")
    add_action(pkg, "alpha", file="toolbox.star", function="alpha")
    add_action(load_package(pkg.root), "beta", file="toolbox.star", function="beta")

    rename_file(load_package(pkg.root), "toolbox.star", "kit.star")
    assert (pkg.root / "kit.star").is_file()
    assert not (pkg.root / "toolbox.star").exists()
    assert "// keep me" in manifest.read_text(encoding="utf-8")
    assert {a.file for a in load_package(pkg.root).actions} == {"kit.star"}


# --- folders: organisation only ---------------------------------------------------

def test_create_folder_shows_up_while_still_empty(tmp_path):
    """Listed from disk, not inferred from file paths — organising usually starts by
    making the empty box."""
    from packsmith.core.packages import create_package, create_folder, folders
    pkg = create_package(tmp_path, "mine")
    create_folder(pkg, "helpers")
    assert folders(load_package(pkg.root)) == ["helpers"]


def test_folders_are_listed_parents_before_children(tmp_path):
    from packsmith.core.packages import create_package, create_folder, folders
    pkg = create_package(tmp_path, "mine")
    create_folder(pkg, "a/b/c")
    assert folders(load_package(pkg.root)) == ["a", "a/b", "a/b/c"]


def test_folder_names_reject_traversal(tmp_path):
    from packsmith.core.packages import create_package, create_folder, create_file
    pkg = create_package(tmp_path, "mine")
    for bad in ("../escape", "a/../../b", "Helpers", ""):
        with pytest.raises(ValueError):
            create_folder(pkg, bad)
    with pytest.raises(ValueError):
        create_file(pkg, "../escape.star")


def test_an_action_can_live_in_a_folder(tmp_path):
    from packsmith.core.packages import create_package, add_action
    pkg = create_package(tmp_path, "mine")
    source = add_action(pkg, "go", file="helpers/math.star", function="go")
    assert source == pkg.root / "helpers" / "math.star" and source.is_file()
    assert load_package(pkg.root).actions[0].file == "helpers/math.star"


def test_a_folder_action_resolves_to_a_callable(tmp_path):
    """The manifest holds a relative path, so nesting has to survive ref resolution."""
    from packsmith.core.packages import create_package, add_action
    packages = tmp_path / "packages"; packages.mkdir()
    add_action(create_package(packages, "mine"), "go", file="helpers/math.star",
               function="go")
    index = PackageIndex(packages)
    assert callable(index.load_callable("mine:go"))


def test_rename_file_into_a_folder_is_a_move(tmp_path):
    from packsmith.core.packages import create_package, add_action, rename_file
    pkg = create_package(tmp_path, "mine")
    add_action(pkg, "go", file="toolbox.star")
    rename_file(load_package(pkg.root), "toolbox.star", "helpers/toolbox.star")
    assert (pkg.root / "helpers" / "toolbox.star").is_file()
    assert not (pkg.root / "toolbox.star").exists()
    assert load_package(pkg.root).actions[0].file == "helpers/toolbox.star"


def test_rename_folder_retargets_everything_underneath(tmp_path):
    from packsmith.core.packages import create_package, add_action, rename_folder
    pkg = create_package(tmp_path, "mine")
    add_action(pkg, "alpha", file="lib/a.star", function="alpha")
    add_action(load_package(pkg.root), "beta", file="lib/deep/b.star", function="beta")
    add_action(load_package(pkg.root), "loose", file="top.star")

    rename_folder(load_package(pkg.root), "lib", "helpers")
    assert {a.action_id: a.file for a in load_package(pkg.root).actions} == {
        "alpha": "helpers/a.star",
        "beta": "helpers/deep/b.star",
        "loose": "top.star",
    }
    assert (pkg.root / "helpers" / "deep" / "b.star").is_file()


def test_rename_folder_refuses_to_move_inside_itself(tmp_path):
    from packsmith.core.packages import create_package, create_folder, rename_folder
    pkg = create_package(tmp_path, "mine")
    create_folder(pkg, "lib")
    with pytest.raises(ValueError, match="inside itself"):
        rename_folder(pkg, "lib", "lib/deeper")


def test_delete_folder_is_refused_while_it_holds_a_declared_file(tmp_path):
    from packsmith.core.packages import create_package, add_action, delete_folder
    pkg = create_package(tmp_path, "mine")
    add_action(pkg, "go", file="lib/a.star")
    with pytest.raises(ValueError, match="still holds"):
        delete_folder(load_package(pkg.root), "lib")
    assert (pkg.root / "lib" / "a.star").is_file()


def test_delete_folder_takes_undeclared_contents(tmp_path):
    from packsmith.core.packages import (
        create_package, create_file, delete_folder, source_files)
    pkg = create_package(tmp_path, "mine")
    create_file(pkg, "lib/a.star")
    create_file(pkg, "lib/deep/b.star")
    delete_folder(load_package(pkg.root), "lib")
    assert source_files(load_package(pkg.root)) == ["manifest.json5"]


# --- cutting one function out of a file (the action reference page) -----------------------
#
# The action page shows the source of the ONE function a manifest points at, so the cut has
# to be right. Its failures all hide: a wrong boundary renders as a perfectly plausible
# function that is quietly truncated or quietly running on into the next one, and the reader
# has no way to tell. That is what earns these a permanent home rather than a look.

_TWO_FUNCTIONS = '''"""Module docstring."""

load("//lib/helpers.star", "each")

def first(pack):
    """Doc."""
    count = 0
    for entry in each(pack):
        count += 1

    pack.log("info", "done %d" % count)


def second(pack):
    pack.log("info", "not part of first")
'''


@pytest.fixture
def two_functions(tmp_path):
    from packsmith.core.packages import create_package
    pkg = create_package(tmp_path, "cutting")
    (pkg.root / "mod.star").write_text(_TWO_FUNCTIONS, encoding="utf-8")
    return pkg


def test_a_blank_line_inside_a_function_does_not_end_it(two_functions):
    """The one that would go unnoticed.

    `first` has a blank line before its last statement, which is ordinary style. Cutting at
    the first blank line yields a function that looks complete, parses fine to the eye, and
    is missing its last line — and nothing on the page says so.
    """
    from packsmith.core.packages import function_source

    cut = function_source(two_functions, "mod.star", "first")
    assert cut.startswith("def first(pack):")
    assert 'pack.log("info", "done %d" % count)' in cut, "truncated at the blank line"


def test_the_next_function_is_not_swept_in(two_functions):
    from packsmith.core.packages import function_source

    cut = function_source(two_functions, "mod.star", "first")
    assert "def second" not in cut
    assert not cut.endswith("\n"), "trailing blank lines belong to the gap, not the function"


def test_a_function_the_file_does_not_define_reports_itself(two_functions):
    """A manifest may name a function that isn't there. The package still loads and the job
    still fails at run time, far from the mistake — so the page has to be able to say so
    rather than showing an empty box."""
    from packsmith.core.packages import function_source

    assert function_source(two_functions, "mod.star", "third") is None
    assert function_source(two_functions, "nowhere.star", "first") is None


def test_file_operations_refuse_downloaded_packages(tmp_path):
    from packsmith.core.packages import create_file, delete_file, remove_action
    root = tmp_path / "vendored"; root.mkdir()
    (root / "manifest.json5").write_text(
        '{"package": {"name": "vendored", "provenance": "downloaded"}}', encoding="utf-8")
    pkg = load_package(root)
    for call in (lambda: create_file(pkg, "x.star"),
                 lambda: delete_file(pkg, "x.star"),
                 lambda: remove_action(pkg, "x")):
        with pytest.raises(ValueError, match="downloaded"):
            call()
