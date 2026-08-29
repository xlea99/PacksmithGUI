"""`pack.package` — the action's own folder (design 3.3.1, 3.3.4).

§3.3.4 banned scratch files, and the ban was over-broad: it forbade a *location* when its
real content was a *pattern*. Both costs it named are absent inside a package — nothing
under `userdata/` can reach a tracked root (§6.6), and containment plus no-cross-package
access means two actions cannot collide on a path. So state may live here.

What must not change is the reason the ownership system is worth having: its protections are
**structural, not promised**. That is the whole of this file. A `write` that could reach the
file the action asked the USER to author would turn "Packsmith never overwrites your rules"
into a courtesy, which is precisely the trade the design refused when it rejected co-authored
files. And a `write` that could reach the package's own sources would let an action change
what it declared after the user read it.

Both failures are silent. You find out when your hand-written config is gone.
"""
import pytest

from packsmith.core.pack import Pack


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "pkg"
    (root / "rules").mkdir(parents=True)
    (root / "manifest.json5").write_text("{}", encoding="utf-8")
    (root / "act.star").write_text("def run(pack):\n    pass\n", encoding="utf-8")
    (root / "rules" / "custom.json").write_text('[{"mob": ["a"]}]', encoding="utf-8")
    pack = Pack(staging=None, tag_store=None, packdump=None, action_ref="p:a",
                package_dir=root, user_authored=["rules/custom.json"])
    return pack.package, root


# --- reading what the user wrote ---------------------------------------------------------

def test_a_user_file_is_read(package):
    api, _ = package
    assert api.read_json("rules/custom.json") == [{"mob": ["a"]}]


def test_an_absent_file_is_None_rather_than_an_error(package):
    """So an action can write `or []` instead of guarding every call. The common case is a
    file the user has not written yet, which is not a failure — it is Tuesday."""
    api, _ = package
    assert api.read("rules/nothing.json") is None
    assert api.read_json("rules/nothing.json") is None
    assert api.exists("rules/nothing.json") is False


def test_malformed_json_says_which_file(package):
    api, root = package
    (root / "rules" / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="broken.json"):
        api.read_json("rules/broken.json")


# --- the boundary ------------------------------------------------------------------------

def test_writing_a_user_authored_file_is_refused(package):
    """**The guarantee this design rests on.** The user's rules file is merged into
    `spawn.json` by the action; if the action could also write it, "Packsmith never
    overwrites what you wrote" would be a promise rather than a rule — and a promise is
    exactly what was rejected when co-authored files were considered and turned down."""
    api, root = package
    before = (root / "rules" / "custom.json").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="user_authored"):
        api.write("rules/custom.json", "[]")

    assert (root / "rules" / "custom.json").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("path", ["manifest.json5", "act.star", "rules/../act.star"])
def test_writing_the_packages_own_code_or_manifest_is_refused(package, path):
    """An action that could rewrite its own manifest could grant itself mappings or change
    its id; one that could rewrite its sources could make the code that ran differ from the
    code the user reviewed. §3.3.1's provenance rules are enforced against files on disk."""
    api, _ = package
    with pytest.raises(ValueError):
        api.write(path, "anything")


@pytest.mark.parametrize("path", ["../outside.json", "../../etc/passwd",
                                  "rules/../../escape.json"])
def test_escaping_the_package_is_refused(package, path):
    api, _ = package
    with pytest.raises(ValueError, match="escapes"):
        api.write(path, "x")
    with pytest.raises(ValueError, match="escapes"):
        api.read(path)


def test_no_package_folder_is_a_clear_error(tmp_path):
    """A step run without one should say so, not fail somewhere further down."""
    api = Pack(staging=None, tag_store=None, packdump=None, action_ref="p:a").package
    with pytest.raises(RuntimeError, match="package folder"):
        api.read("anything.json")


# --- writing what the action legitimately owns -------------------------------------------

def test_the_action_can_write_its_own_state(package):
    """History, not a cache. "What I wrote last time" exists nowhere else and passes
    §3.3.4's escape-hatch test outright — unlike a derivable fact, which stays banned."""
    api, root = package
    api.write_json("state/previous.json", {"mobs": ["a", "b"]})

    assert (root / "state" / "previous.json").is_file()
    assert api.read_json("state/previous.json") == {"mobs": ["a", "b"]}


def test_writing_creates_intermediate_folders(package):
    api, root = package
    api.write("deep/down/here.txt", "hi")
    assert (root / "deep" / "down" / "here.txt").read_text(encoding="utf-8") == "hi"


def test_an_undeclared_file_is_writable(package):
    """Only what the manifest declared as the user's is protected. Everything else in the
    package is the action's own, which is the point of allowing writes at all."""
    api, _ = package
    api.write("rules/generated.json", "[]")
    assert api.read("rules/generated.json") == "[]"
