"""Searching the Files panel (design 6.2).

The panel's tree is **lazy** — a folder holds a placeholder until you open it — which is
what makes its filter unlike every other panel's. Two of the consequences fail silently,
and those are what is tested here. Ranking, the result cap and the count line all announce
themselves the moment you type.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.files import FileStore
from packsmith.gui.shell.panels.files_panel import FilesPanel


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qapp, user_db, tmp_path):
    root = tmp_path / "instance"
    (root / "config" / "deep").mkdir(parents=True)
    (root / "mods").mkdir()
    (root / "config" / "quark-common.toml").write_text("a = 1", encoding="utf-8")
    (root / "config" / "deep" / "quark_extra.json").write_text("{}", encoding="utf-8")
    (root / "mods" / "Quark.jar").write_text("x", encoding="utf-8")
    (root / "options.txt").write_text("x", encoding="utf-8")
    widget = FilesPanel(FileStore(user_db, root))
    yield widget
    widget.deleteLater()


def rows(panel):
    return [panel._tree.topLevelItem(i).text(0)
            for i in range(panel._tree.topLevelItemCount())]


# --- it must search the DISK, not the loaded nodes -----------------------------------------

def test_a_match_is_found_without_having_opened_its_folder(panel):
    """The whole reason this filter walks the filesystem.

    Filtering the loaded tree would find a file only if you had already clicked your way to
    it — so a search would answer "no files match" for a file sitting right there, and the
    answer would depend on where you happened to have browsed. `config/deep/` is unopened
    and its placeholder is the only thing in it.
    """
    panel._search.setText("quark_extra")
    assert rows(panel) == ["config/deep/quark_extra.json"]


def test_the_index_is_not_built_until_somebody_searches(panel):
    """Measured at ~1.5s cold on a real 300-mod instance. Paying that when the panel merely
    reloads ownership — which happens on every claim, release and file operation — would
    stall the UI for something nobody asked for."""
    assert panel._index is None
    panel._search.setText("options")
    assert panel._index is not None


# --- the index must not outlive the tree it describes --------------------------------------

def test_a_file_created_after_the_first_search_is_findable(panel):
    """The silent one. The index is cached, so without invalidating it on refresh a file you
    just made through the panel is absent from search — and the search says "no files
    match", which reads as an answer about the pack rather than about a stale cache."""
    panel._search.setText("brand_new")
    assert rows(panel) == []

    (panel._files.root / "config" / "brand_new.json").write_text("{}", encoding="utf-8")
    panel.refresh()

    assert rows(panel) == ["config/brand_new.json"], "the search index went stale"


def test_searching_does_not_cost_you_the_folders_you_had_open(panel):
    """The tree survives a search.

    `refresh` preserves expanded folders by reading them off the tree — but a search
    REPLACES the tree with a flat list, so by the time the box is cleared there is nothing
    on screen to read them from. Re-harvesting at that moment yields an empty set and
    collapses everything the user had opened, which is a real cost for having glanced at a
    search. The set is remembered across the search instead.
    """
    opened = next(panel._tree.topLevelItem(i)
                  for i in range(panel._tree.topLevelItemCount())
                  if panel._tree.topLevelItem(i).text(0) == "config")
    opened.setExpanded(True)

    panel._search.setText("quark")
    assert "config" not in rows(panel), "the tree was still showing"
    panel._search.setText("")

    restored = {panel._tree.topLevelItem(i).text(0)
                for i in range(panel._tree.topLevelItemCount())
                if panel._tree.topLevelItem(i).isExpanded()}
    assert "config" in restored, "searching collapsed the tree behind it"
