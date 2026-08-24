"""Creating, renaming and deleting files from the browser — design 6.2.

§6.2 specifies a standard filesystem context menu in Honest Mode. The interesting part
isn't the menu, it's what happens to *ownership* when a path moves or disappears: a rename
must carry the record (the artifact is the same one), and a delete must drop it (or the
next file to take that name inherits a stranger's owner).
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.files import FileStore


@pytest.fixture
def files(user_db, tmp_path):
    root = tmp_path / "instance"
    root.mkdir()
    return FileStore(user_db, root)


# --- rename ----------------------------------------------------------------------------

def test_renaming_a_file_carries_its_owner(files):
    files.write("cfg.json", "{}", owner="action", owner_action_ref="palette:fill")
    files.rename("cfg.json", "settings.json")

    assert files.read("settings.json") == "{}"
    assert not files.exists("cfg.json")
    assert files.ownership("settings.json") == {"kind": "action", "action_ref": "palette:fill"}
    assert files.ownership("cfg.json") is None


def test_renaming_never_launders_an_action_file_into_an_untouched_one(files):
    """The failure mode worth naming: drop the record on rename and the hard-block stops
    applying, so the user can overwrite an action's output by renaming it first."""
    files.write("gen.json", "{}", owner="action", owner_action_ref="palette:fill")
    files.rename("gen.json", "gen2.json")
    assert files.ownership("gen2.json")["kind"] == "action"


def test_renaming_a_directory_moves_every_record_beneath_it(files):
    files.write("cfg/a.json", "1", owner="user")
    files.write("cfg/deep/b.json", "2", owner="action", owner_action_ref="palette:fill")
    files.rename("cfg", "config")

    assert files.ownership("config/a.json")["kind"] == "user"
    assert files.ownership("config/deep/b.json")["action_ref"] == "palette:fill"
    assert files.ownership("cfg/a.json") is None
    assert files.read("config/deep/b.json") == "2"


def test_a_sibling_with_the_same_prefix_is_left_alone(files):
    """"cfg" must not drag "cfgold" along with it."""
    files.write("cfg/a.json", "1", owner="user")
    files.write("cfgold/a.json", "2", owner="user")
    files.rename("cfg", "config")

    assert files.ownership("cfgold/a.json")["kind"] == "user", "an unrelated tree moved"
    assert files.exists("cfgold/a.json")


def test_underscores_in_a_folder_name_are_not_sql_wildcards(files):
    """`_` matches any character in LIKE. Mod ids are full of underscores, so this is the
    normal case rather than a pathological one."""
    files.write("some_mod/a.json", "1", owner="user")
    files.write("someXmod/a.json", "2", owner="action", owner_action_ref="palette:fill")
    files.rename("some_mod", "renamed")

    assert files.ownership("renamed/a.json")["kind"] == "user"
    assert files.ownership("someXmod/a.json")["kind"] == "action", "a wildcard match stole it"


def test_renaming_onto_an_existing_path_refuses(files):
    files.write("a.json", "1", owner="user")
    files.write("b.json", "2", owner="user")
    with pytest.raises(FileExistsError):
        files.rename("a.json", "b.json")
    assert files.read("b.json") == "2", "the destination was clobbered"


def test_renaming_cannot_escape_the_instance_root(files):
    files.write("a.json", "1", owner="user")
    with pytest.raises(ValueError, match="escapes"):
        files.rename("a.json", "../../evil.json")


# --- delete ----------------------------------------------------------------------------

def test_deleting_a_tree_forgets_everything_under_it(files):
    files.write("cfg/a.json", "1", owner="user")
    files.write("cfg/deep/b.json", "2", owner="action", owner_action_ref="palette:fill")
    files.delete_tree("cfg")

    assert not (files.root / "cfg").exists()
    assert files.all_ownership() == {}


def test_a_deleted_path_does_not_bequeath_its_owner_to_the_next_file(files):
    """Leave the record behind and a brand-new file lands pre-owned by a stranger — and,
    if that stranger was an action, hard-blocked for a reason nobody can explain."""
    files.write("cfg/a.json", "1", owner="action", owner_action_ref="palette:fill")
    files.delete_tree("cfg")
    files.write("cfg/a.json", "fresh", owner="user")
    assert files.ownership("cfg/a.json")["kind"] == "user"


def test_deleting_a_tree_leaves_its_siblings_alone(files):
    files.write("cfg/a.json", "1", owner="user")
    files.write("cfgold/a.json", "2", owner="user")
    files.delete_tree("cfg")
    assert files.ownership("cfgold/a.json") is not None
    assert files.exists("cfgold/a.json")


# --- what a delete would cost -----------------------------------------------------------

def test_owned_under_reports_the_blast_radius(files):
    files.write("cfg/a.json", "1", owner="user")
    files.write("cfg/deep/b.json", "2", owner="action", owner_action_ref="palette:fill")
    files.write("other.json", "3", owner="user")

    under = files.owned_under("cfg")
    assert set(under) == {FileStore.key("cfg/a.json"), FileStore.key("cfg/deep/b.json")}
    assert sum(1 for o in under.values() if o["kind"] == "action") == 1


def test_owned_under_a_single_file_is_that_file(files):
    files.write("a.json", "1", owner="user")
    assert list(files.owned_under("a.json")) == [FileStore.key("a.json")]


def test_owned_under_an_untracked_path_is_empty(files):
    (files.root / "raw.txt").write_text("hi", encoding="utf-8")
    assert files.owned_under("raw.txt") == {}


# --- the menu itself --------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    yield app


@pytest.fixture
def panel(files, monkeypatch):
    """A real panel over a real store, with the modals answered for it."""
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from packsmith.gui.shell.panels.files_panel import FileBrowser

    (files.root / "cfg").mkdir()
    files.write("cfg/a.json", "1", owner="user")
    files.write("top.json", "2", owner="action", owner_action_ref="palette:fill")

    answers = {"text": "", "confirm": QMessageBox.Yes}
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: (answers["text"], True)))
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: answers["confirm"]))
    return FileBrowser(files), files, answers


def _labels(menu):
    return [a.text() for a in menu.actions() if a.text()]


def test_a_directory_gets_a_menu_at_all(panel):
    """It used to get nothing: right-clicking a folder returned early, so there was no way
    to make a file in one."""
    view, files, _ = panel
    item = _find(view, "cfg")
    menu = view._menu_for(item)
    assert "New File…" in _labels(menu)
    assert "New Folder…" in _labels(menu)
    assert "Delete…" in _labels(menu)


def test_a_directory_menu_offers_no_ownership_actions(panel):
    """Ownership is per-file (§6.1). A folder has none to claim."""
    view, files, _ = panel
    labels = _labels(view._menu_for(_find(view, "cfg")))
    assert not [t for t in labels if "ownership" in t.lower()]


def test_a_file_keeps_its_ownership_actions_and_gains_the_rest(panel):
    view, files, _ = panel
    labels = _labels(view._menu_for(_find(view, "top.json")))
    assert "Take ownership from the action…" in labels
    assert "Rename…" in labels and "Delete…" in labels


def test_empty_space_targets_the_root(panel):
    """So an empty instance isn't a dead end."""
    view, files, answers = panel
    labels = _labels(view._menu_for(None))
    assert "New File…" in labels
    assert "Rename…" not in labels, "nothing is selected to rename"


def test_new_file_creates_it_owned_by_the_user_and_opens_it(panel):
    view, files, answers = panel
    answers["text"] = "fresh.json"
    opened = []
    view.file_activated.connect(opened.append)

    view._new_file("cfg")

    assert files.read("cfg/fresh.json") == ""
    assert files.ownership("cfg/fresh.json")["kind"] == "user"
    assert opened == ["cfg/fresh.json"], "created a file and left the user hunting for it"


def test_new_file_refuses_to_clobber(panel):
    view, files, answers = panel
    answers["text"] = "a.json"
    view._new_file("cfg")
    assert files.read("cfg/a.json") == "1", "an existing file was blanked"


def test_renaming_through_the_panel_moves_the_record(panel):
    view, files, answers = panel
    answers["text"] = "b.json"
    view._rename("cfg/a.json", False)
    assert files.ownership("cfg/b.json")["kind"] == "user"


def test_declining_the_managed_rename_warning_stops_it(panel):
    from PySide6.QtWidgets import QMessageBox
    view, files, answers = panel
    answers["text"] = "renamed.json"
    answers["confirm"] = QMessageBox.No
    view._rename("top.json", False)
    assert files.exists("top.json"), "renamed an action's file over a 'No'"


def test_declining_the_delete_confirmation_stops_it(panel):
    from PySide6.QtWidgets import QMessageBox
    view, files, answers = panel
    answers["confirm"] = QMessageBox.No
    view._delete("cfg", True)
    assert files.exists("cfg/a.json"), "deleted a tree over a 'No'"


def test_deleting_a_folder_takes_its_records_with_it(panel):
    view, files, _ = panel
    view._delete("cfg", True)
    assert not files.exists("cfg/a.json")
    assert files.ownership("cfg/a.json") is None


def _find(view, name):
    root = view._tree.invisibleRootItem()
    for i in range(root.childCount()):
        if root.child(i).text(0) == name:
            return root.child(i)
    raise AssertionError(f"no tree item named {name}")
