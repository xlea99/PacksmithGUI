"""The JAR Viewer tab — design 6.5, step one.

Browsing only: the tree, the sizes, the filter. Content viewing and save-as-override are
later steps, and nothing here should quietly grow into them.
"""
import os
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.gui.jar_viewer import JarViewerTab, human_size


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def jar(tmp_path):
    path = tmp_path / "testmod.jar"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        archive.writestr("META-INF/jars/embedded.jar", b"PK\x03\x04")
        archive.writestr("pack.mcmeta", '{"pack": {}}')
        archive.writestr("assets/testmod/lang/en_us.json", '{"a": "b"}' * 200)
        archive.writestr("assets/testmod/blockstates/stone.json", "{}")
        archive.writestr("com/example/Mod.class", b"\xca\xfe\xba\xbe")
    return path


def rows(item):
    return [item.child(i) for i in range(item.childCount())]


def top(tab):
    return rows(tab._tree.invisibleRootItem())


def find(tab, name):
    """Depth-first search for a row by its displayed name."""
    stack = list(top(tab))
    while stack:
        item = stack.pop()
        if item.text(0) == name:
            return item
        stack.extend(rows(item))
    return None


# --- the tree ----------------------------------------------------------------------------

def test_the_archive_opens_as_a_tree(jar):
    tab = JarViewerTab(jar)
    assert sorted(i.text(0) for i in top(tab)) == ["META-INF", "assets", "com", "pack.mcmeta"]


def test_folders_nest_rather_than_showing_flat_paths(jar):
    tab = JarViewerTab(jar)
    assets = find(tab, "assets")
    assert [i.text(0) for i in rows(assets)] == ["testmod"]
    assert sorted(i.text(0) for i in rows(rows(assets)[0])) == ["blockstates", "lang"]


def test_a_file_shows_both_sizes(jar):
    tab = JarViewerTab(jar)
    lang = find(tab, "en_us.json")
    assert lang.text(1) and lang.text(2), "no size shown"
    assert lang.text(1) != lang.text(2), "deflated file reported as its own packed size"


def test_a_folder_shows_no_size(jar):
    """A folder's size in a zip is a fiction — there is no such record."""
    tab = JarViewerTab(jar)
    assert find(tab, "assets").text(1) == ""


def test_the_summary_says_what_is_in_there(jar):
    tab = JarViewerTab(jar)
    assert "6 files" in tab._summary.text()
    assert "packed" in tab._summary.text()


def test_a_nested_jar_is_marked(jar):
    """Marked, not opened — recursing is a later step, and the marker is what makes that
    discoverable when it lands."""
    tab = JarViewerTab(jar)
    embedded = find(tab, "embedded.jar")
    assert "nested archive" in embedded.toolTip(0)


# --- the filter ---------------------------------------------------------------------------

def test_filtering_narrows_to_matching_paths(jar):
    tab = JarViewerTab(jar)
    tab._filter.setText("blockstates")
    assert find(tab, "stone.json") is not None
    assert find(tab, "Mod.class") is None


def test_filtering_keeps_the_path_to_a_match_visible(jar):
    """A match you cannot see the path to is not a result: the ancestors have to survive
    even though their own names don't match."""
    tab = JarViewerTab(jar)
    tab._filter.setText("en_us")
    assert [i.text(0) for i in top(tab)] == ["assets"]
    assert find(tab, "testmod") is not None and find(tab, "lang") is not None


def test_clearing_the_filter_restores_everything(jar):
    tab = JarViewerTab(jar)
    tab._filter.setText("nothing matches this")
    assert top(tab) == []
    tab._filter.setText("")
    assert len(top(tab)) == 4


# --- damaged input -------------------------------------------------------------------------

def test_a_corrupt_jar_opens_a_tab_that_says_so(tmp_path):
    """A half-downloaded jar is a normal thing to double-click. The tab explains itself
    rather than refusing to exist — which would look like the click did nothing."""
    broken = tmp_path / "broken.jar"
    broken.write_bytes(b"PK\x03\x04 not really a zip")

    tab = JarViewerTab(broken)
    # `isVisible()` is False for any child of a widget that was never shown, so it
    # answers a question about the test harness rather than about the banner.
    assert not tab._banner.isHidden()
    assert "not a readable archive" in tab._banner.text()
    assert top(tab) == []


def test_a_missing_jar_does_not_raise(tmp_path):
    tab = JarViewerTab(tmp_path / "gone.jar")
    assert not tab._banner.isHidden()


# --- sizes read like sizes -------------------------------------------------------------------

@pytest.mark.parametrize("count, expected", [
    (0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (1536 * 1024, "1.5 MB"),
    (151 * 1024 * 1024, "151 MB"),
])
def test_human_size(count, expected):
    """Jars run from a few KB to 144 MB, so a raw byte count is wrong at both ends."""
    assert human_size(count) == expected
