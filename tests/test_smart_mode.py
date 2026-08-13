"""Smart Mode in the file browser — design 6.2 / 8.1.

§6.2's Smart Mode "organizes files by purpose", and its categories are contributed by
integrations. For global packs that integration is the loader (§8.1), which is what makes
the mode possible at all: Minecraft has no vanilla global datapack mechanism, so without a
loader mod there is genuinely nothing to categorise — and §6.2 says categories without an
integration simply don't appear.

The categories are the loader's own directories. Honest Mode can tell you
`config/paxi/datapacks/tweaks_create` is a folder inside a mod's config; only Smart Mode
can tell you it is a datapack.
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.files import FileStore
from packsmith.integrations.paxi import PaxiProvider


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    yield app


@pytest.fixture
def instance(user_db, tmp_path):
    root = tmp_path / "instance"
    paxi = root / "config" / "paxi"
    (paxi / "datapacks" / "tweaks_create" / "data").mkdir(parents=True)
    (paxi / "datapacks" / "rei_removals").mkdir(parents=True)
    (paxi / "resourcepacks").mkdir(parents=True)
    (paxi / "datapack_load_order.json").write_text(
        json.dumps({"loadOrder": ["rei_removals"]}), encoding="utf-8")
    return FileStore(user_db, root)


@pytest.fixture
def panel(instance, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from packsmith.gui.shell.panels.files_panel import FilesPanel

    answers = {"text": "", "ok": True}
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: (answers["text"], answers["ok"])))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    view = FilesPanel(instance, loader=PaxiProvider())
    view._mc_version = "1.20.1"
    return view, instance, answers


def categories(view):
    root = view._tree.invisibleRootItem()
    return [root.child(i) for i in range(root.childCount())]


def labels(item):
    return [item.child(i).text(0) for i in range(item.childCount())]


# --- the mode only exists when a loader does -------------------------------------------

def test_without_a_loader_smart_mode_is_disabled(instance):
    """§6.2: categories without an integration simply don't appear — and with no loader
    mod installed there is no such thing as a global datapack to show."""
    from packsmith.gui.shell.panels.files_panel import FilesPanel

    view = FilesPanel(instance, loader=None)
    assert not view._mode.model().item(1).isEnabled()
    assert "pack loader" in view._mode.toolTip()


def test_with_a_loader_smart_mode_is_offered(panel):
    view, _, _ = panel
    assert view._mode.model().item(1).isEnabled()
    assert "Paxi" in view._mode.toolTip()


def test_honest_mode_is_still_the_default(panel):
    """The raw tree is the one that never lies, so it stays the thing you land on."""
    view, _, _ = panel
    assert view._mode.currentIndex() == 0
    assert [i.text(0) for i in categories(view)] == ["config"]


# --- the categories ---------------------------------------------------------------------

def test_smart_mode_shows_the_two_supercategories(panel):
    view, _, _ = panel
    view._mode.setCurrentIndex(1)
    assert [c.text(0) for c in categories(view)] == ["Datapacks", "Resource Packs"]


def test_packs_appear_under_their_category(panel):
    view, _, _ = panel
    view._mode.setCurrentIndex(1)
    assert set(labels(categories(view)[0])) == {"tweaks_create", "rei_removals"}


def test_packs_are_listed_in_load_order(panel):
    """With Paxi the order is a thing the user controls, so showing them alphabetically
    would show something the game disagrees with."""
    view, _, _ = panel
    view._mode.setCurrentIndex(1)
    assert labels(categories(view)[0]) == ["rei_removals", "tweaks_create"]


def test_an_empty_category_says_how_to_fill_it(panel):
    view, _, _ = panel
    view._mode.setCurrentIndex(1)
    assert "right-click to create" in labels(categories(view)[1])[0]


def test_switching_back_to_honest_mode_restores_the_raw_tree(panel):
    view, _, _ = panel
    view._mode.setCurrentIndex(1)
    view._mode.setCurrentIndex(0)
    assert [i.text(0) for i in categories(view)] == ["config"]


# --- the category menu ------------------------------------------------------------------

def test_a_category_offers_only_what_makes_sense_there(panel):
    """It is the loader's own directory, not a folder you manage: renaming or deleting it
    would break the loader."""
    view, _, _ = panel
    view._mode.setCurrentIndex(1)
    actions = [a.text() for a in view._menu_for(categories(view)[0]).actions() if a.text()]
    assert "New Datapack…" in actions
    assert not any(a in actions for a in ("Rename…", "Delete…", "New Folder…"))


def test_the_resource_pack_category_offers_the_right_noun(panel):
    view, _, _ = panel
    view._mode.setCurrentIndex(1)
    actions = [a.text() for a in view._menu_for(categories(view)[1]).actions() if a.text()]
    assert "New Resource Pack…" in actions


def test_a_pack_inside_a_category_keeps_the_ordinary_menu(panel):
    """A pack IS a folder you manage — only the category is special."""
    view, _, _ = panel
    view._mode.setCurrentIndex(1)
    pack = categories(view)[0].child(0)
    actions = [a.text() for a in view._menu_for(pack).actions() if a.text()]
    assert "New File…" in actions and "Delete…" in actions


# --- creating a pack --------------------------------------------------------------------

def test_creating_a_datapack_makes_a_loadable_pack(panel):
    """A pack without `pack.mcmeta` is silently ignored by the game — folder there, files
    there, nothing happens — so the loader always writes one."""
    view, files, answers = panel
    answers["text"] = "my_tweaks"
    view._new_pack("datapacks")

    folder = files.root / "config" / "paxi" / "datapacks" / "my_tweaks"
    assert (folder / "data").is_dir()
    meta = json.loads((folder / "pack.mcmeta").read_text(encoding="utf-8"))
    assert meta["pack"]["pack_format"] == 15, "1.20.1's datapack format"


def test_a_resource_pack_gets_assets_and_its_own_format(panel):
    view, files, answers = panel
    answers["text"] = "my_textures"
    view._new_pack("resourcepacks")

    folder = files.root / "config" / "paxi" / "resourcepacks" / "my_textures"
    assert (folder / "assets").is_dir() and not (folder / "data").exists()


def test_the_pack_format_follows_the_minecraft_version(panel):
    """Minecraft refuses a pack whose format doesn't match, and "incompatible" is a
    confusing thing to read on a pack you just made."""
    view, files, answers = panel
    view._mc_version = "1.19.4"
    answers["text"] = "older"
    view._new_pack("datapacks")

    meta = json.loads((files.root / "config" / "paxi" / "datapacks" / "older"
                       / "pack.mcmeta").read_text(encoding="utf-8"))
    assert meta["pack"]["pack_format"] == 12


def test_a_new_pack_appears_immediately(panel):
    view, _, answers = panel
    view._mode.setCurrentIndex(1)
    answers["text"] = "fresh"
    view._new_pack("datapacks")
    assert "fresh" in labels(categories(view)[0])


def test_cancelling_creates_nothing(panel):
    view, files, answers = panel
    answers["text"], answers["ok"] = "unwanted", False
    view._new_pack("datapacks")
    assert not (files.root / "config" / "paxi" / "datapacks" / "unwanted").exists()


def test_an_empty_name_creates_nothing(panel):
    view, files, answers = panel
    answers["text"] = "   "
    view._new_pack("datapacks")
    assert len(list((files.root / "config" / "paxi" / "datapacks").iterdir())) == 2


def test_a_duplicate_name_is_refused_rather_than_merged(panel):
    """Writing into an existing pack would quietly adopt someone else's files."""
    view, files, answers = panel
    answers["text"] = "tweaks_create"
    view._new_pack("datapacks")
    assert not (files.root / "config" / "paxi" / "datapacks" / "tweaks_create"
                / "pack.mcmeta").exists(), "an existing pack was written into"


def test_a_name_with_a_separator_is_refused(panel):
    view, files, answers = panel
    answers["text"] = "sneaky/../../escape"
    view._new_pack("datapacks")
    assert not (files.root / "escape").exists()


# --- a loader that does not do both kinds -------------------------------------------------
#
# Reported from real use: opening Smart Mode on deep_end threw
# `CapabilityError: Moonlight does not load global resource packs` and left the panel
# showing datapacks only. The panel asked for both roots unconditionally.

def test_smart_mode_survives_a_datapacks_only_loader(user_db, tmp_path):
    """Moonlight has no concept of resource packs — not disabled, absent — so asking it for
    that root raises. Smart Mode must show the categories the loader HAS rather than
    assuming every loader does both."""
    from packsmith.core.files import FileStore
    from packsmith.gui.shell.panels.files_panel import FilesPanel
    from packsmith.integrations.moonlight import MoonlightProvider

    root = tmp_path / "instance"
    (root / "moonlight-global-datapacks" / "tweaks").mkdir(parents=True)
    panel = FilesPanel(FileStore(user_db, root), loader=MoonlightProvider())
    try:
        panel._smart = True
        panel.refresh()          # used to raise CapabilityError and take the panel down

        labels = [panel._tree.topLevelItem(i).text(0)
                  for i in range(panel._tree.topLevelItemCount())]
        assert any("Datapacks" in label for label in labels)
        assert not any("Resource Pack" in label for label in labels), \
            "offered a category the loader cannot provide"
    finally:
        panel.deleteLater()


def test_smart_mode_shows_both_kinds_for_a_loader_that_has_both(user_db, tmp_path):
    """The carve-out must be about the loader, not a blanket removal of resource packs."""
    from packsmith.core.files import FileStore
    from packsmith.gui.shell.panels.files_panel import FilesPanel
    from packsmith.integrations.paxi import PaxiProvider

    root = tmp_path / "instance"
    paxi = PaxiProvider()
    paxi.create_pack(root, "tweaks")
    paxi.create_pack(root, "skins", kind="resourcepacks")
    panel = FilesPanel(FileStore(user_db, root), loader=paxi)
    try:
        panel._smart = True
        panel.refresh()
        labels = [panel._tree.topLevelItem(i).text(0)
                  for i in range(panel._tree.topLevelItemCount())]
        assert any("Datapacks" in label for label in labels)
        assert any("Resource Pack" in label for label in labels)
    finally:
        panel.deleteLater()
