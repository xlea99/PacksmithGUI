"""Jumping the browser to one file — the tab menu's "Show in Files Tab".

The tree is lazy: a folder's children do not exist until it is expanded. So selecting a
file five levels down is not a search, it is a controlled descent that has to *create* each
level on the way. The failure this guards is quiet in the worst way — the tab switches, the
panel is right there, and the cursor is simply somewhere else, which reads as "the feature
doesn't do anything" rather than as a bug.
"""
import pytest

from packsmith.core.files import FileStore
from packsmith.gui.shell.panels.files_panel import FileBrowser

DEEP = "config/deep/nested/way_down.json"


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qapp, user_db, tmp_path):
    root = tmp_path / "instance"
    (root / "config" / "deep" / "nested").mkdir(parents=True)
    (root / "mods").mkdir()
    (root / DEEP).write_text("{}", encoding="utf-8")
    (root / "config" / "quark-common.toml").write_text("a = 1", encoding="utf-8")
    (root / "mods" / "Quark.jar").write_text("x", encoding="utf-8")
    (root / "options.txt").write_text("x", encoding="utf-8")
    widget = FileBrowser(FileStore(user_db, root))
    yield widget
    widget.deleteLater()


def current(panel):
    item = panel._tree.currentItem()
    return item.text(0) if item is not None else None


def test_a_file_at_the_top_level_is_selected(panel):
    assert panel.select("options.txt") is True
    assert current(panel) == "options.txt"


def test_a_deeply_nested_file_is_reached_through_folders_that_did_not_exist_yet(panel):
    """The whole point. Nothing under `config/` has been built when this is called, so
    every level of the chain has to be expanded and populated on the way down."""
    assert panel.select(DEEP) is True
    assert current(panel) == "way_down.json"


def test_the_folders_on_the_way_are_left_open(panel):
    """Landing on a selected row inside collapsed parents is not possible, but landing on
    one whose parents snapped shut behind it would be — and then the file is highlighted
    somewhere you cannot see it."""
    panel.select(DEEP)
    item = panel._tree.currentItem()
    parent = item.parent()
    while parent is not None:
        assert parent.isExpanded()
        parent = parent.parent()


def test_a_folder_can_be_selected_too(panel):
    assert panel.select("config/deep") is True
    assert current(panel) == "deep"


def test_a_file_that_is_not_there_is_a_polite_miss(panel):
    """False rather than an exception, and the cursor left alone. A tab can outlive the
    file it was opened from, and a deleted file must not take the window with it."""
    assert panel.select("config/gone.json") is False
    assert current(panel) is None


def test_an_empty_path_is_a_miss(panel):
    assert panel.select("") is False
    assert panel.select(None) is False


def test_backslashes_are_understood(panel):
    """Paths reach this from Windows-shaped places — a report row, a jar member split, a
    store key. The tree stores them one way."""
    assert panel.select("config\\deep\\nested\\way_down.json") is True
    assert current(panel) == "way_down.json"


def test_selecting_twice_is_stable(panel):
    """The second call runs against a tree the first one already expanded, which is a
    different code path through `_restore_expanded` than the cold one."""
    assert panel.select(DEEP) is True
    assert panel.select(DEEP) is True
    assert current(panel) == "way_down.json"
