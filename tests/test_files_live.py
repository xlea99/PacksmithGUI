"""The Files panel following the disk while you look at it — design 6.2.

The game creates files under folders you have open (a new log, a config written on first
launch, a crash report). Before, the tree only changed when its panel was reopened or a
save refreshed it, so a file the game had just made was simply not there. And a rebuild
threw the tree back to the top, which is why one cannot be done every couple of seconds
without keeping your place.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.files import FileStore
from packsmith.gui.shell.panels.files_panel import FileBrowser, FilesPanel


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "instance"
    (root / "config").mkdir(parents=True)
    (root / "logs").mkdir()
    for n in range(40):
        (root / "config" / f"mod_{n:02}.toml").write_text("a = 1", encoding="utf-8")
    return root


def names(browser, folder):
    for i in range(browser._tree.topLevelItemCount()):
        item = browser._tree.topLevelItem(i)
        if item.text(0) == folder:
            return [item.child(j).text(0) for j in range(item.childCount())]
    return None


def test_a_file_created_under_an_open_folder_appears_on_its_own(qapp, user_db, root):
    panel = FilesPanel(FileStore(user_db, root))
    panel.show()
    panel.basic.select("logs")
    panel.basic._tree.currentItem().setExpanded(True)
    panel.check_disk()                                  # first look: remember the folders

    (root / "logs" / "latest.log").write_text("[12:00] hi", encoding="utf-8")
    os.utime(root / "logs", ns=(1, 1))                  # a coarse clock must not hide it
    panel.check_disk()

    assert names(panel.basic, "logs") == ["latest.log"]
    panel.deleteLater()


def test_writing_inside_a_file_does_not_rebuild_the_tree(qapp, user_db, root):
    """Logs grow every second while the game runs. A rebuild per write would be visible
    churn for nothing — the listing has not changed."""
    browser = FileBrowser(FileStore(user_db, root))
    before = browser.disk_signature()
    (root / "config" / "mod_00.toml").write_text("a = 2, now longer", encoding="utf-8")
    assert browser.disk_signature() == before


def test_a_rebuild_keeps_your_place(qapp, user_db, root):
    browser = FileBrowser(FileStore(user_db, root))
    browser.resize(300, 200)
    browser.show()
    assert browser.select("config/mod_30.toml")
    bar = browser._tree.verticalScrollBar()
    bar.setValue(bar.maximum())
    scrolled = bar.value()
    assert scrolled > 0, "the fixture must actually scroll, or this proves nothing"

    browser.refresh()

    assert browser._tree.currentItem().text(0) == "mod_30.toml"
    assert browser._tree.verticalScrollBar().value() == scrolled
    browser.deleteLater()
