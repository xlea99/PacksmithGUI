"""Autocomplete for `pack.` in `.star` files (design 6.3's completion providers).

Two halves, tested two ways.

**The catalog** (`gui/editor/starlark_api.py`) is introspected from the real `Pack`, so the
failure worth guarding is not "it is wrong" but "it silently became thin" — a signature
that starts carrying `self`, a docstring that stops being found, a namespace that quietly
drops out. Those show up as an editor that is subtly less useful, which nobody files a bug
about.

**The matching logic** (`gui/editor/starlark_lang.js`) is regex-and-scanner work, which is
where actual bugs live. This repo's convention for the editor page has been to assert on
its text, and that would prove nothing here: "the page contains a completion provider" says
nothing about whether `pack.tags.` resolves to the tag members. So the JS is evaluated in a
`QJSEngine` — no browser, no Monaco — and the functions are called directly.

What is deliberately NOT tested is Monaco itself: that the provider is registered, that a
suggestion box appears. That needs a real web view, and it is the loud kind of broken — you
type a dot and nothing happens. Per the project's test-permanence rule, only failures that
*hide* earn a permanent test.
"""
import json
import os
import re
from pathlib import Path, PurePath

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.pack import Pack, _FileHandle
from packsmith.gui.editor.starlark_api import HOST_ONLY, catalog
from tests.test_pack_surface import CASES as REACHABLE_FROM_STARLARK

EDITOR = Path(__file__).resolve().parent.parent / "packsmith" / "gui" / "editor"
LANG_JS = EDITOR / "starlark_lang.js"
HOST_HTML = EDITOR / "monaco_host.html"


@pytest.fixture(scope="module")
def js():
    """`starlark_lang.js`, evaluated and callable.

    A `QJSEngine` needs a `QCoreApplication`; `QApplication` is one, and using it keeps this
    file compatible with every other Qt test in the suite (two different application
    classes cannot coexist in one process).
    """
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QJSEngine

    QApplication.instance() or QApplication([])
    engine = QJSEngine()
    result = engine.evaluate(LANG_JS.read_text(encoding="utf-8"))
    assert not result.isError(), f"{LANG_JS.name} did not evaluate: {result.toString()}"
    return engine


@pytest.fixture(scope="module")
def api():
    return catalog()


def call(js, function, *args):
    """Call a JS function with JSON-marshalled arguments and get plain Python back."""
    arguments = ", ".join(json.dumps(arg) for arg in args)
    result = js.evaluate(f"JSON.stringify({function}({arguments}))")
    assert not result.isError(), result.toString()
    text = result.toString()
    return None if text in ("undefined", "null", "") else json.loads(text)


def names(members):
    return [member["name"] for member in (members or [])]


# --- the catalog -------------------------------------------------------------------------

def test_the_catalog_describes_exactly_what_an_action_can_reach(api):
    """The strongest thing this file asserts: the editor advertises the same surface
    `test_pack_surface` proves is reachable from Starlark. Suggesting a member that does
    not exist would be worse than suggesting nothing — the author writes it, and finds out
    at run time."""
    advertised = set()
    for namespace, members in api["members"].items():
        for member in members:
            if member["kind"] == "namespace":
                continue
            advertised.add(f"{namespace}.{member['name']}" if namespace else member["name"])
    assert advertised == set(REACHABLE_FROM_STARLARK)


def test_no_signature_leaks_self(api):
    """Read off the class instead of an instance and every method grows a `self` an author
    must not type. It looks plausible in a tooltip, which is what makes it worth pinning."""
    everything = [m for members in api["members"].values() for m in members] + api["handle"]
    offenders = [m["name"] for m in everything if m["detail"].startswith("(self")]
    assert offenders == []


def test_every_callable_carries_help(api):
    """The docstrings live on `pack.py`, so this is really asserting that one source of
    truth still reaches the editor — an undocumented method shows as a bare signature."""
    undocumented = [
        f"{namespace}.{member['name']}"
        for namespace, members in api["members"].items()
        for member in members
        if member["kind"] == "function" and not member["doc"]
    ] + [m["name"] for m in api["handle"] if m["kind"] == "function" and not m["doc"]]
    assert undocumented == []


def test_plain_values_do_not_borrow_pythons_own_docstrings(api):
    """`pack.action_ref` is a `str` and `pack.step.mappings` is a `dict`. The only docstring
    reachable through them belongs to the built-in type, and putting `str(object='') -> str`
    in a tooltip about `pack` would be worse than saying nothing."""
    values = [m for members in api["members"].values() for m in members
              if m["kind"] == "value"]
    assert {m["name"] for m in values} >= {"action_ref", "mappings", "config"}
    assert all(m["doc"] == "" for m in values)
    assert next(m for m in values if m["name"] == "action_ref")["detail"] == "str"


def test_the_handle_surface_matches_the_real_file_handle(api):
    assert names(api["handle"]) == sorted(
        n for n in dir(_FileHandle) if not n.startswith("_"))
    write = next(m for m in api["handle"] if m["name"] == "write")
    assert write["detail"] == "(content, *, file_must_exist=False)"
    assert write["params"] == ["content", "file_must_exist=False"]


def test_host_only_members_are_kept_out(api):
    """They are the runtime's business (design 3.3), and offering `failure_reason` would
    invite an action to forge one."""
    assert HOST_ONLY & {m["name"] for m in api["members"][""]} == set()


def test_the_catalog_survives_the_trip_to_the_page(api):
    """It crosses as a JSON literal inside a `runJavaScript` call, so anything that cannot
    be serialised would fail inside a web view with no console anyone reads."""
    assert json.loads(json.dumps(api)) == api
    assert len(json.dumps(api)) < 200_000, "too big to inline on every editor start"


# --- what the cursor is pointing at ------------------------------------------------------

@pytest.mark.parametrize("path, expected", [
    ("deep_end/removal.star", True),
    ("A/B/SHOUTY.STAR", True),
    ("scripts/helper.py", False),      # opens as `python` too — the case URIs exist for
    ("config/quark.toml", False),
    ("", False),
])
def test_only_star_files_are_ours(js, path, expected):
    assert call(js, "pathIsStarlark", path) is expected


@pytest.mark.parametrize("prefix, expected", [
    ("    pack.", "top"),
    ("pack.", "top"),
    ("    pack.ta", "top"),                       # mid-word: the list must survive typing
    ("    pack.tags.", "tags"),
    ("    pack.tags.wr", "tags"),
    ("    items = pack.registry.", "registry"),
    ("    pack.datapacks.", "datapacks"),
    ('    pack.filesystem.resolve("a.txt").', "handle"),
    ('    pack.datapacks.resolve(pack="tweaks", namespace="mc", path="x.json").', "handle"),
    ("    pack.step.mappings.", None),            # a dict — nothing of ours to offer
    ("    unpack.", None),                        # `pack` must be a whole word
    ("    my_helper(", None),
    ("", None),
])
def test_the_right_member_list_is_offered(js, api, prefix, expected):
    got = call(js, "membersFor", api, prefix)
    if expected is None:
        assert got is None
        return
    if expected == "top":
        assert "tags" in names(got) and "datapacks" in names(got)
    elif expected == "handle":
        assert names(got) == names(api["handle"])
    else:
        assert names(got) == names(api["members"][expected])


@pytest.mark.parametrize("text, expected", [
    ("pack", "pack"),
    ("    pack.log", "log"),
    ("    pack.tags.write", "write"),
    ("    x = pack.blueprints.gaps", "gaps"),
    ('    pack.filesystem.resolve("a").write', "write"),
    ('    pack.datapacks.resolve(pack="t", namespace="mc", path="p").exists', "exists"),
    ("    pack.tags.nonesuch", None),
    ("    pack.a.b.c", None),
    ("    write", None),          # an author's own helper, not a file handle's method
    ("    result", None),
])
def test_what_the_cursor_is_on(js, api, text, expected):
    member = call(js, "memberAt", api, text)
    assert (member or {}).get("name") == expected


def test_hovering_pack_itself_explains_what_it_is(js, api):
    member = call(js, "memberAt", api, "def run(pack")
    assert member["name"] == "pack"
    assert "capability object" in member["doc"]


# --- which argument is being typed -------------------------------------------------------

@pytest.mark.parametrize("prefix, chain, index", [
    ("    pack.tags.write(", "pack.tags.write", 0),
    ('    pack.tags.write("minecraft:item", ', "pack.tags.write", 1),
    ('    pack.tags.write("minecraft:item", "quark:rope", "remove", ', "pack.tags.write", 3),
    ('    pack.log("info", ', "pack.log", 1),
    # A comma inside a string is punctuation, not an argument separator. Scanning backwards
    # cannot tell those apart without re-deriving the line, which is why the scan is forward.
    ('    pack.log("info", "a, b, c", ', "pack.log", 2),
    # Same for a parenthesis inside a string — it must not open a call.
    ('    pack.log("info", "f(x", ', "pack.log", 2),
    # A completed nested call belongs to the inner frame, which is closed again by the time
    # the cursor gets here, so the outer count keeps going.
    ('    pack.log("info", str(1), ', "pack.log", 2),
    # An OPEN nested call is the frame the cursor is actually in.
    ('    pack.log("info", pack.registry.entries(', "pack.registry.entries", 0),
])
def test_which_call_and_argument_the_cursor_is_in(js, prefix, chain, index):
    call_site = call(js, "openCall", prefix)
    assert call_site is not None
    assert call_site["before"].endswith(chain)
    assert call_site["argIndex"] == index


@pytest.mark.parametrize("prefix", [
    "    pack.tags",
    "    pack.tags.write()",            # closed again — the cursor is not inside it
    '    pack.log("a(b")',
    "",
])
def test_no_signature_help_outside_a_call(js, prefix):
    assert call(js, "openCall", prefix) is None


def test_the_signature_help_it_would_show(js, api):
    """The two halves joined: find the call, then look the member up. This is exactly what
    the provider in the host page does, minus Monaco's presentation."""
    prefix = '    pack.tags.write("minecraft:item", "quark:rope", '
    site = call(js, "openCall", prefix)
    member = call(js, "memberAt", api, site["before"])
    assert member["name"] == "write"
    assert member["params"] == ["registry_type", "entry_id", "tag_name", "value"]
    assert member["params"][site["argIndex"]] == "tag_name"


# --- the wiring between Python and the page ----------------------------------------------

def test_the_page_defines_what_python_calls():
    """`EditorHost._send_starlark_api` runs `setStarlarkApi(...)` in the page. If the two
    names ever drift the failure is a JavaScript error inside a web view with no console
    anybody reads — the editor simply comes up with no completions and no complaint."""
    from packsmith.gui.editor import host

    page = HOST_HTML.read_text(encoding="utf-8")
    source = Path(host.__file__).read_text(encoding="utf-8")
    assert "setStarlarkApi(" in source
    assert "function setStarlarkApi(" in page


def test_the_page_loads_the_language_file_and_uses_its_functions():
    page = HOST_HTML.read_text(encoding="utf-8")
    assert 'src="./starlark_lang.js"' in page
    for function in ("pathIsStarlark", "membersFor", "memberAt", "openCall"):
        assert function in page, f"{function} moved out of the page's reach"


def test_every_script_the_page_loads_would_actually_ship():
    """Caught for real: `package-data` listed `*.html`, `monaco/**/*` and `vendor/**/*`, so
    a script sitting beside the page — which `starlark_lang.js` does, being ours rather than
    vendored — was excluded from an install and a freeze. The page still loads, Monaco still
    works, and completions are just gone with nothing logged."""
    import tomllib

    root = Path(__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as f:
        patterns = tomllib.load(f)["tool"]["setuptools"]["package-data"]["packsmith.gui.editor"]

    page = HOST_HTML.read_text(encoding="utf-8")
    local_scripts = re.findall(r'<script src="\./([^"]+)"', page)
    assert local_scripts, "no local scripts found — did the page's markup change?"
    for script in local_scripts:
        asset = EDITOR / script
        assert asset.is_file(), f"{script} is referenced by the page but not on disk"
        covered = any(PurePath(script).match(pattern) for pattern in patterns)
        assert covered, f"{script} ships with no package-data pattern covering it"


def test_models_are_created_with_a_uri():
    """Without one Monaco assigns `inmemory://model/N` and a provider cannot tell a `.star`
    action from any other Python-highlighted buffer, so every completion would fire on
    every file."""
    page = HOST_HTML.read_text(encoding="utf-8")
    assert "modelUri(key)" in page
    assert "monaco.editor.createModel(text, language || 'plaintext',\n" in page
