"""The Folders panel, and the Files panel browsing more than the instance (design 6.6).

Two consents, and the panels are where a user could confuse them: adding a folder here
makes it *browsable*, binding it to a step makes it *writable by that action*. §6.2 asked
for exactly that separation and the panel says so on its face.

What is worth guarding is the quiet half. A root picker that changes the label without
changing the store shows one folder's tree while every claim, context menu and edit lands
in another — it looks like it worked, and the file it writes is not the file on screen.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.db import UserDB
from packsmith.core.roots import FileRoots


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def world(tmp_path, qapp):
    instance = tmp_path / "instance"
    repo = tmp_path / "tweaks"
    (instance / "config").mkdir(parents=True)
    (instance / "config" / "in_instance.json").write_text("{}", encoding="utf-8")
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "in_repo.json").write_text("{}", encoding="utf-8")
    db = UserDB(tmp_path / "p.db")
    roots = FileRoots(db, instance, protected=[tmp_path / "userdata"])
    return {"db": db, "roots": roots, "instance": instance, "repo": repo, "tmp": tmp_path}


def folders(world):
    from packsmith.gui.shell.panels.folders_panel import FoldersPanel
    return FoldersPanel(world["roots"])


def files(world):
    """The BASIC browser out of the Files panel — the tab that browses tracked roots.

    Built through the container so the wiring between the three subtabs is exercised, and
    the container is KEPT: it owns the browsers, so letting it fall out of scope deletes
    the very widget under test out from under Qt.
    """
    from packsmith.gui.shell.panels.files_panel import FilesPanel
    panel = FilesPanel(world["roots"].instance, roots=world["roots"])
    world["panel"] = panel
    return panel.basic


def rows(panel):
    tree = panel._tree
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


# --- the Folders panel ------------------------------------------------------------------

def test_the_instance_is_listed_and_first(world):
    """Hiding it would make "everywhere Packsmith looks" a lie of omission, and it is the
    root every other one is understood relative to."""
    assert rows(folders(world))[0] == "minecraft"


def test_a_tracked_folder_appears(world):
    world["roots"].add("tweaks", world["repo"])
    assert rows(folders(world)) == ["minecraft", "tweaks"]


def test_a_missing_folder_is_flagged_rather_than_shown_as_empty(world):
    """Design 6.6: a moved repo or an unplugged drive must not read as an empty folder that
    is fine to write into."""
    gone = world["tmp"] / "gone"
    gone.mkdir()
    world["roots"].add("gone", gone)
    gone.rmdir()

    panel = folders(world)
    row = next(panel._tree.topLevelItem(i) for i in range(panel._tree.topLevelItemCount())
               if panel._tree.topLevelItem(i).text(0) == "gone")
    assert row.text(1) == "missing"


def test_the_instance_offers_no_rename_or_untrack(world):
    """It cannot be either, so offering it would be a menu entry whose only outcome is an
    error dialog."""
    panel = folders(world)
    world["roots"].add("tweaks", world["repo"])
    panel.refresh()

    assert _menu_for(panel, "minecraft") == ["Show in Files"]
    assert _menu_for(panel, "tweaks") == ["Show in Files", "Rename…", "Stop tracking…"]


def test_a_refusal_is_surfaced_not_raised(world, monkeypatch):
    """Every guard in `FileRoots` is a correctness requirement, and the panel is where a
    user meets one. It must explain, not traceback."""
    from PySide6.QtWidgets import QMessageBox
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))

    panel = folders(world)
    panel._guarded(lambda: world["roots"].add("bad name", world["repo"]))

    assert warned and "usable name" in warned[0]


# --- the Files panel across roots ----------------------------------------------------------

def test_the_picker_is_hidden_while_there_is_no_choice(world):
    """One root is not a choice, and a picker with one option implies the feature is
    unavailable rather than simply unused."""
    browser = files(world)
    assert browser._root_row.isVisibleTo(browser) is False


def test_the_picker_appears_once_a_second_root_exists(world):
    world["roots"].add("tweaks", world["repo"])
    panel = files(world)
    assert [panel._root_pick.itemData(i) for i in range(panel._root_pick.count())] == \
        ["minecraft", "tweaks"]


def test_switching_root_switches_the_STORE_not_just_the_label(world):
    """The quiet failure. A picker that changes the label without changing the store shows
    one folder's tree while every claim and edit lands in another."""
    world["roots"].add("tweaks", world["repo"])
    panel = files(world)

    panel.show_root("tweaks")

    assert panel._files.name == "tweaks"
    assert panel._files.root == world["repo"]


def test_switching_root_shows_the_new_root_s_files(world):
    """The end the picker exists for. Asserted on the TREE rather than on a private flag —
    two internals I reached for first (`_index`, `_expanded`) turned out to be rebuilt by
    `refresh` a line later, so tests of them passed with the reset deleted."""
    world["roots"].add("tweaks", world["repo"])
    panel = files(world)
    assert "config" in rows(panel)

    panel.show_root("tweaks")

    assert rows(panel) == ["src"], "still showing the instance"


def test_smart_cannot_be_pointed_at_another_root(world):
    """Smart's categories come from a pack LOADER, which is a fact about the instance
    (§8.1). Under another root they describe nothing, so the Smart browser simply has no
    root registry — there is no state in which it is aimed somewhere it means nothing."""
    world["roots"].add("tweaks", world["repo"])
    files(world)                      # builds the container, which builds both browsers

    assert world["panel"].smart._roots is None
    world["panel"].show_root("tweaks")
    assert world["panel"].basic._files.name == "tweaks"
    assert world["panel"].smart._files.name == "minecraft", "Smart followed the root"


def test_an_unknown_root_is_ignored_rather_than_raising(world):
    """Reachable from a double-click in another panel, which can outlive the row."""
    panel = files(world)
    panel.show_root("never_existed")
    assert panel._files.name == "minecraft"


def _menu_for(panel, name):
    """The context-menu labels for one row, without opening a blocking popup."""
    from PySide6.QtWidgets import QMenu
    from PySide6.QtCore import QPoint
    import packsmith.gui.shell.panels.folders_panel as module

    captured = []

    class Fake(QMenu):
        def addAction(self, text, *_a, **_k):
            captured.append(text)

        def addSeparator(self):
            pass

        def exec(self, *_a):
            return None

    tree = panel._tree
    row = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
               if tree.topLevelItem(i).text(0) == name)
    original, module.QMenu = module.QMenu, Fake
    try:
        panel._on_context_menu(tree.visualItemRect(row).center())
    finally:
        module.QMenu = original
    return captured
