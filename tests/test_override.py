"""Save-as-override — design 6.5 / 8.1.

The mechanism is deliberately dumb, and §8.1 says so: *"PackSmith doesn't do anything
clever here — it just writes the file to the right place and lets Minecraft's pack layering
do the rest."* An override works by sitting at **the same namespace path the mod uses**,
inside a pack that loads afterwards. So the one thing that must never drift is the path.

Two consequences drive everything here: the destination path is not a choice, and the
action is only offered where an override target exists at all.
"""
import json
import os
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.archives import read_member
from packsmith.core.capabilities import override_kind
from packsmith.core.files import FileStore
from packsmith.gui.jar_viewer import _ROLE_PATH
from packsmith.integrations.paxi import PaxiProvider


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


LOOT = "data/minecraft/loot_tables/blocks/oak_leaves.json"
TEXTURE = "assets/testmod/textures/block/stone.png"


@pytest.fixture
def world(user_db, tmp_path):
    """An instance with Paxi, one existing pack, and a mod jar to override from."""
    root = tmp_path / "instance"
    (root / "config" / "paxi" / "datapacks" / "tweaks").mkdir(parents=True)
    (root / "config" / "paxi" / "resourcepacks").mkdir(parents=True)
    (root / "mods").mkdir(parents=True)
    with zipfile.ZipFile(root / "mods" / "testmod.jar", "w") as archive:
        archive.writestr(LOOT, json.dumps({"type": "minecraft:block", "pools": []}))
        archive.writestr(TEXTURE, b"\x89PNG\r\n\x1a\nfake")
        archive.writestr("com/example/Mod.class", b"\xca\xfe\xba\xbe")
        archive.writestr("META-INF/mods.toml", 'modId="testmod"')
    return FileStore(user_db, root), PaxiProvider()


# --- what can be overridden at all ---------------------------------------------------------

@pytest.mark.parametrize("member, expected", [
    (LOOT, "datapacks"),
    ("data/minecraft/tags/blocks/leaves.json", "datapacks"),
    (TEXTURE, "resourcepacks"),
    ("assets/testmod/lang/en_us.json", "resourcepacks"),
    ("com/example/Mod.class", None),
    ("META-INF/mods.toml", None),
    ("mods.toml", None),
    ("assets/testmod/Weird.class", None),
])
def test_only_data_and_assets_have_an_override_target(member, expected):
    """Minecraft's own structure decides this, not the loader: `data/` is datapack
    territory and `assets/` is resource pack territory. Compiled code and a mod's metadata
    have no override target and never will (§6.3)."""
    assert override_kind(member) == expected


def test_the_kind_decides_which_pack_it_goes_into(world):
    """`data/` → datapack, `assets/` → resource pack — the file says where it belongs."""
    files, paxi = world
    assert paxi.override_path(files.root, "p", LOOT, kind=override_kind(LOOT)).is_relative_to(
        paxi.datapack_root(files.root))
    assert paxi.override_path(files.root, "p", TEXTURE,
                              kind=override_kind(TEXTURE)).is_relative_to(
        paxi.resourcepack_root(files.root))


# --- the path is the mechanism ----------------------------------------------------------------

def test_the_path_inside_the_pack_is_the_path_inside_the_jar(world):
    """The whole of how an override works. Change the path and it becomes an unrelated
    file the game never reads."""
    files, paxi = world
    target = paxi.override_path(files.root, "tweaks", LOOT, kind="datapacks")
    inside = target.relative_to(paxi.datapack_root(files.root) / "tweaks").as_posix()
    assert inside == LOOT


def test_the_copy_is_byte_identical(world):
    """Not re-serialised, not reformatted: whatever the mod shipped is what lands, so a
    diff against the original shows only what the user then changes."""
    files, paxi = world
    data = read_member(files.root / "mods" / "testmod.jar", LOOT)
    target = paxi.override_path(files.root, "tweaks", LOOT, kind="datapacks")
    rel = target.relative_to(files.root).as_posix()

    files.write_bytes(rel, data, owner="user")
    assert target.read_bytes() == data


def test_a_binary_override_survives_the_trip(world):
    """A resource-pack override is a PNG. Routing it through the text path would either
    mangle it or raise while capturing the prior content."""
    files, paxi = world
    data = read_member(files.root / "mods" / "testmod.jar", TEXTURE)
    target = paxi.override_path(files.root, "textures", TEXTURE, kind="resourcepacks")
    rel = target.relative_to(files.root).as_posix()

    files.write_bytes(rel, data, owner="user")
    assert target.read_bytes() == data
    assert data.startswith(b"\x89PNG")


def test_an_override_is_owned_by_the_user(world):
    """The user made it, so §6.1's engine records it as theirs — and an action that later
    wants to write it is hard-blocked rather than silently taking it."""
    files, paxi = world
    target = paxi.override_path(files.root, "tweaks", LOOT, kind="datapacks")
    rel = target.relative_to(files.root).as_posix()

    files.write_bytes(rel, b"{}", owner="user")
    assert files.ownership(rel)["kind"] == "user"


def test_writing_bytes_returns_the_prior_content_for_rollback(world):
    files, paxi = world
    rel = "config/paxi/datapacks/tweaks/data/x.json"
    assert files.write_bytes(rel, b"first", owner="user") is None
    assert files.write_bytes(rel, b"second", owner="user") == b"first"


def test_an_override_cannot_escape_its_pack(world):
    files, paxi = world
    with pytest.raises(ValueError, match="escapes"):
        paxi.override_path(files.root, "tweaks", "../../../../evil.json")


# --- the dialog ---------------------------------------------------------------------------------

def test_the_dialog_offers_the_packs_the_loader_reports(world, qapp):
    from packsmith.gui.override_dialog import OverrideTargetDialog

    files, paxi = world
    dialog = OverrideTargetDialog(LOOT, "datapacks",
                                  paxi.packs(files.root, "datapacks"))
    assert [dialog._combo.itemText(i) for i in range(dialog._combo.count())] == ["tweaks"]


def test_the_sticky_default_is_preselected(world, qapp):
    """§8.1: sticky per profile, so the next override goes where the last one did."""
    from packsmith.gui.override_dialog import OverrideTargetDialog

    files, paxi = world
    paxi.create_pack(files.root, "second")
    dialog = OverrideTargetDialog(LOOT, "datapacks", paxi.packs(files.root, "datapacks"),
                                  default="second")
    assert dialog._combo.currentText() == "second"


def test_with_no_packs_the_only_option_is_to_make_one(world, qapp):
    from packsmith.gui.override_dialog import OverrideTargetDialog

    dialog = OverrideTargetDialog(TEXTURE, "resourcepacks", [])
    assert dialog._create.isChecked()
    assert not dialog._existing.isEnabled(), "offering a choice between nothing"


def test_a_new_pack_name_with_a_separator_is_refused(world, qapp):
    from packsmith.gui.override_dialog import OverrideTargetDialog

    dialog = OverrideTargetDialog(LOOT, "datapacks", ["tweaks"])
    dialog._create.setChecked(True)
    dialog._name.setText("../escape")
    dialog.accept()
    assert dialog.result_pack is None, "accepted a name that climbs out of the pack"


# --- the menu only offers it where it works -------------------------------------------------------

def test_the_jar_viewer_offers_override_for_data_and_assets(world, qapp):
    """Offering it everywhere and failing afterwards would teach the user to distrust the
    menu."""
    from packsmith.gui.jar_viewer import JarViewerTab

    files, _ = world
    # The jar viewer takes a PATH — it reads the archive directory, unlike the image
    # and NBT viewers which take bytes so jar members work through them.
    tab = JarViewerTab(files.root / "mods" / "testmod.jar")

    def menu_labels(path):
        item = next(i for i in _walk(tab) if i.data(0, _ROLE_PATH) == path)
        return [a.text() for a in tab._menu_for(item).actions() if a.text()]

    assert any("override" in label for label in menu_labels(LOOT))
    assert any("override" in label for label in menu_labels(TEXTURE))


def test_the_jar_viewer_does_not_offer_it_for_class_files(world, qapp):
    from packsmith.gui.jar_viewer import JarViewerTab

    files, _ = world
    # The jar viewer takes a PATH — it reads the archive directory, unlike the image
    # and NBT viewers which take bytes so jar members work through them.
    tab = JarViewerTab(files.root / "mods" / "testmod.jar")
    item = next(i for i in _walk(tab) if i.data(0, _ROLE_PATH) == "com/example/Mod.class")
    assert not any("override" in a.text() for a in tab._menu_for(item).actions() if a.text())


def _walk(tab):
    """Every row in the viewer, expanding as it goes."""
    stack = [tab._tree.topLevelItem(i) for i in range(tab._tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        item.setExpanded(True)
        yield item
        stack.extend(item.child(i) for i in range(item.childCount()))


# --- coming back from the game must not cost you your workspace ---------------------------

def test_reopening_restores_what_a_packdump_rebuild_closed(qapp):
    """Adopting a dump happens on window focus — which fires exactly when you come back
    from playtesting. A mod updating itself in the launcher is enough to trigger it, so the
    user may not have done anything at all; losing every open tab for that is a punishment
    for the loop PackSmith exists to support.
    """
    from packsmith.gui.main_window import MainWindow

    opened = []
    window = type("W", (), {
        "_open_document": lambda s, source, path: opened.append(("doc", source, path)),
        "_open_view": lambda s, v: opened.append(("view", v.id)),
        "_open_blueprint": lambda s, n: opened.append(("blueprint", n)),
        "_open_job_editor": lambda s, j: opened.append(("job", j.id)),
        "_editor_host": type("H", (), {"split_key": staticmethod(
            lambda key: tuple(key.split(":", 1)))})(),
        "_views": type("V", (), {"all": lambda s: [type("Vw", (), {"id": 7})()]})(),
        "_blueprints": type("B", (), {"names": lambda s: ["StoneType"]})(),
        "_jobs": type("J", (), {"get": lambda s, i: type("Job", (), {"id": i})()})(),
    })()

    MainWindow._reopen_tabs(window, [
        ("doc", "instance:config/x.json"), ("view", 7),
        ("blueprint", "StoneType"), ("job", 3)])

    assert opened == [("doc", "instance", "config/x.json"), ("view", 7),
                      ("blueprint", "StoneType"), ("job", 3)]


def test_something_the_new_dump_invalidated_simply_does_not_come_back(qapp):
    """Best-effort on purpose: an empty tab claiming to show a view that no longer exists
    is worse than the tab being gone."""
    from packsmith.gui.main_window import MainWindow

    opened = []
    window = type("W", (), {
        "_open_view": lambda s, v: opened.append(v.id),
        "_open_blueprint": lambda s, n: opened.append(n),
        "_views": type("V", (), {"all": lambda s: []})(),
        "_blueprints": type("B", (), {"names": lambda s: []})(),
    })()

    MainWindow._reopen_tabs(window, [("view", 99), ("blueprint", "Gone")])
    assert opened == []


def test_a_tab_that_raises_does_not_stop_the_others(qapp):
    """One unrestorable tab must not cost the user the rest of them."""
    from packsmith.gui.main_window import MainWindow

    opened = []

    def open_view(_self, view):
        if view.id == 1:
            raise RuntimeError("this one is beyond saving")
        opened.append(view.id)

    window = type("W", (), {
        "_open_view": open_view,
        "_views": type("V", (), {"all": lambda s: [
            type("Vw", (), {"id": 1})(), type("Vw", (), {"id": 5})()]})(),
    })()

    MainWindow._reopen_tabs(window, [("view", 1), ("view", 5)])
    assert opened == [5], "a failure on one tab swallowed the ones after it"
