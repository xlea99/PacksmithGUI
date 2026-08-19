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
    (paxi / "resourcepacks").mkdir(parents=True)
    # Real packs, manifest and all: a folder without `pack.mcmeta` is not a pack the game
    # loads, so a fixture of bare directories was modelling something that does not work —
    # and now reads, correctly, as disabled.
    for name in ("tweaks_create", "rei_removals"):
        (paxi / "datapacks" / name / "data").mkdir(parents=True)
        (paxi / "datapacks" / name / "pack.mcmeta").write_text(
            json.dumps({"pack": {"pack_format": 15, "description": name}}),
            encoding="utf-8")
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
    manifest = (files.root / "config" / "paxi" / "datapacks" / "tweaks_create"
                / "pack.mcmeta")
    before = manifest.read_text(encoding="utf-8")

    answers["text"] = "tweaks_create"
    view._new_pack("datapacks")

    # Checked by CONTENT rather than existence: every real pack has a manifest, so "one is
    # there" says nothing about whether this call wrote it. `create_pack` would stamp its
    # own description over the pack's.
    assert manifest.read_text(encoding="utf-8") == before,         "an existing pack was written into"


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


# --- the browser has to notice a claim when it happens ---------------------------------
#
# Reported from real use: editing an untracked file claimed it (5ms, measured) and coloured
# the editor tab, but the Files panel kept showing it as untracked until you switched tabs
# and back. Saving refreshed the panel; the *first edit* claim only set a status message.

def test_a_first_edit_claim_reaches_the_files_panel(user_db, tmp_path, qapp):
    """§6.2: "editing is the gesture that tracks it" — so the browser must say so at the
    keystroke, not whenever something else happens to rebuild the tree."""
    from packsmith.core.files import FileStore
    from packsmith.gui.editor.sources import InstanceFileSource
    from packsmith.gui.shell.panels.files_panel import FilesPanel

    root = tmp_path / "instance"
    (root / "config").mkdir(parents=True)
    (root / "config" / "emi.json").write_text("{}", encoding="utf-8")
    files = FileStore(user_db, root)
    panel = FilesPanel(files, loader=None)
    assert panel._owners == {}, "nothing is owned yet"

    claimed = []
    source = InstanceFileSource(files, on_claim=claimed.append)
    source.on_first_edit("config/emi.json")
    panel.refresh()                      # what `_on_file_claimed` now does for us

    assert claimed == ["config/emi.json"]
    assert any("emi.json" in path for path in panel._owners)


def test_the_window_refreshes_the_panel_on_a_claim(user_db, tmp_path, qapp, monkeypatch):
    """The wiring, which is the half that was missing — the panel could always refresh,
    nothing asked it to."""
    from packsmith.gui.main_window import MainWindow

    window = MainWindow.__new__(MainWindow)
    refreshed = []
    window._set_status = lambda text: None
    window._files_panel = type("P", (), {"refresh": lambda self: refreshed.append(True)})()

    window._on_file_claimed("config/emi.json")

    assert refreshed == [True]


# --- switching a pack off from the browser (design 8.1) -------------------------------

def _pack_row(view, index=0):
    view._mode.setCurrentIndex(1)
    return categories(view)[0].child(index)


def test_a_pack_row_offers_disable(panel):
    view, _files, _answers = panel
    menu = view._menu_for(_pack_row(view))
    assert "Disable" in [a.text() for a in menu.actions() if a.text()]


def test_a_pack_keeps_its_ordinary_actions_too(panel):
    """A pack IS a folder you manage — switching it off is one more entry, not a menu of
    its own."""
    view, _files, _answers = panel
    labels = [a.text() for a in view._menu_for(_pack_row(view)).actions() if a.text()]
    assert "New File…" in labels and "Delete…" in labels


def test_disabling_renames_the_manifest_and_says_so(panel):
    view, files, _answers = panel
    said = []
    view.ownership_changed.connect(said.append)
    folder = files.root / "config" / "paxi" / "datapacks" / "rei_removals"

    view._set_pack_enabled("datapacks", "rei_removals", False, "off")

    assert not (folder / "pack.mcmeta").exists()
    assert (folder / "pack.mcmeta.DISABLED").is_file()
    assert said == ["off"], "the status bar is where this panel says such things"


def test_a_disabled_pack_still_appears_but_says_it_is_off(panel):
    """Hiding it would make it unreachable — switching one back on means finding it."""
    view, files, _answers = panel
    view._set_pack_enabled("datapacks", "rei_removals", False, "off")
    view._mode.setCurrentIndex(1)

    shown = labels(categories(view)[0])
    assert "rei_removals  (disabled)" in shown
    assert "tweaks_create" in shown


def test_a_disabled_pack_offers_enable(panel):
    view, _files, _answers = panel
    view._set_pack_enabled("datapacks", "rei_removals", False, "off")
    view._mode.setCurrentIndex(1)
    row = next(c for c in [categories(view)[0].child(i)
                           for i in range(categories(view)[0].childCount())]
               if "rei_removals" in c.text(0))

    assert "Enable" in [a.text() for a in view._menu_for(row).actions() if a.text()]
