"""The editor's assets are local (design 4.2).

Monaco used to load from a CDN, which meant no internet = no editor at all, and a frozen
build betting on cdnjs serving one pinned version forever. These tests are cheap insurance
on two things that fail *quietly*:

* a remote URL creeping back into the host page — which works fine on the dev machine and
  breaks only for a user on a plane;
* naming a language Monaco doesn't ship. That isn't an error, it just renders as
  plaintext, which is exactly how `.toml` went unnoticed until it was looked at directly.
"""
import re
from pathlib import Path

import pytest

from packsmith.gui.editor.host import _EXT_TO_LANGUAGE, language_for

EDITOR = Path(__file__).resolve().parent.parent / "packsmith" / "gui" / "editor"
HOST_HTML = EDITOR / "monaco_host.html"
VS = EDITOR / "vendor" / "monaco" / "vs"

# Provided by the rich language services in min/vs/language rather than by a Monarch
# grammar in basic-languages, so they need checking a different way.
_RICH_LANGUAGES = {"json", "css", "html", "typescript", "javascript"}


def test_monaco_is_vendored():
    assert VS.is_dir(), "run tools/vendor_monaco.py"
    assert (VS / "loader.js").is_file()
    assert (VS / "editor" / "editor.main.js").is_file()


def test_the_host_page_fetches_nothing_remote():
    html = HOST_HTML.read_text(encoding="utf-8")
    remote = re.findall(r"""["'(](https?://[^"')\s]+)""", html)
    assert remote == [], f"the editor page must be fully local, found: {remote}"


def test_the_host_page_resolves_monaco_relative_to_itself():
    """Not a hardcoded path: deriving it from baseURI is what makes the frozen build need
    no path handling, since vendor/ simply sits beside this file wherever it lands."""
    html = HOST_HTML.read_text(encoding="utf-8")
    assert "document.baseURI" in html
    assert "./vendor/monaco/vs" in html


def test_workers_get_an_absolute_url():
    """Monaco's workers resolve against the ORIGIN root, not the document — on file:// that
    is the drive root. Without this shim the editor comes up looking fine with its
    background services dead."""
    html = HOST_HTML.read_text(encoding="utf-8")
    assert "MonacoEnvironment" in html and "getWorkerUrl" in html
    assert "workerMain.js" in html


@pytest.mark.parametrize("language", sorted(set(_EXT_TO_LANGUAGE.values())))
def test_every_mapped_language_actually_exists(language):
    if language == "plaintext" or language in _RICH_LANGUAGES:
        return
    assert (VS / "basic-languages" / language).is_dir(), (
        f"nothing maps to '{language}' in Monaco — it will silently render as plaintext")


@pytest.mark.parametrize("path,expected", [
    ("action.star", "python"),          # Starlark is a Python dialect
    ("manifest.toml", "ini"),           # Monaco ships no TOML grammar; ini is the shape
    ("server_scripts/recipes.js", "javascript"),   # KubeJS
    ("types/kubejs.d.ts", "typescript"),
    ("config/thing.json5", "json"),
    ("scripts/thing.zs", "javascript"),  # ZenScript / CraftTweaker
    ("pack.mcmeta", "json"),
    ("mystery.wat", "plaintext"),
])
def test_the_file_types_this_app_opens(path, expected):
    assert language_for(path) == expected
