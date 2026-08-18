"""Making a profile, and unmaking one (design 3.1).

The version *contract* is covered in `test_profile_contract`, and the state *file* in
`test_sticky_profile`. What neither covered is the lifecycle: what a brand-new profile
contains, and what survives deleting one. Both gaps produced real bugs.

A profile is two stores, not one — its data in `profile.db` under `userdata/profiles/<name>/`,
and its remembered furniture in `state.json`, keyed by **name**. Nothing structural links
their lifecycles, so every test here is ultimately about keeping them in step.
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.common import setup as app_setup
from packsmith.common.setup import load_ui_state, save_ui_state
from packsmith.core.db import UserDB
from packsmith.core.profile import Profile, delete_profile, list_profiles
from packsmith.core.tags import TagStore
from packsmith.core.views import ViewStore


@pytest.fixture
def userdata(tmp_path, monkeypatch):
    """Redirect BOTH stores at a temp directory.

    Both, because that is the whole subject: a test that patched only `profiles` would
    write the real `state.json` and, worse, would not be able to see the leak these tests
    exist to catch.
    """
    profiles = tmp_path / "profiles"
    config = tmp_path / "config"
    profiles.mkdir()
    config.mkdir()
    monkeypatch.setattr(app_setup.GLOBAL_PATHS, "profiles", profiles)
    monkeypatch.setattr(app_setup.GLOBAL_PATHS, "config", config)
    return tmp_path


@pytest.fixture
def instance(tmp_path):
    """A stand-in Minecraft directory. `Profile.create` refuses a path that isn't there."""
    path = tmp_path / "instance"
    (path / "config").mkdir(parents=True)
    (path / "config" / "quark-common.toml").write_text("a = 1", encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def with_packdump(instance):
    """The minimum a profile needs before the shell will build anything.

    Without one `MainWindow` stops at the gated shell (§3.1) and never reaches the code
    that used to seed — so a seed test without this would pass for the wrong reason.
    """
    dump = instance / "packsmith"
    (dump / "registries").mkdir(parents=True, exist_ok=True)
    (dump / "attributes").mkdir(parents=True, exist_ok=True)
    (dump / "meta.json").write_text(json.dumps({
        "type": "packsmith_full_dump", "schema_version": 1,
        "generated_at_utc": "2026-08-12T00:00:00+00:00",
        "minecraft_version": "1.20.1", "loader": "forge", "loader_version": "47.4.13",
        "mods": [{"mod_id": "a", "name": "A", "version": "1"}],
        "registries": [{"type": "minecraft:item", "file": "i.json", "count": 1}],
    }), encoding="utf-8")
    (dump / "registries" / "i.json").write_text('{"values": ["minecraft:stone"]}',
                                                encoding="utf-8")
    (dump / "attributes" / "localization.json").write_text(
        '{"locale": "en_us", "values": {}}', encoding="utf-8")
    return instance


def make(name, instance, **kw):
    return Profile.create(name, loader="forge", mc_path=str(instance),
                          mc_version="1.20.1", loader_version="47.4.13", **kw)


# --- what a NEW profile contains -----------------------------------------------------------

def test_a_new_profile_is_still_empty_after_the_app_opens_it(userdata, instance, qapp):
    """A fresh profile is EMPTY — and the check has to go through the WINDOW.

    `_seed_tags` and `_seed_views` ran on every profile open, writing four junk tags and
    four demo Views into whatever you loaded, including a real profile carrying two
    thousand imported decisions. They were scaffolding from before the Tags panel existed
    and their own docstrings said so.

    Constructing the profile and reading its database proves nothing: `Profile.create`
    never seeded. The seeding was in `MainWindow._enter_profile`, so only opening it there
    can catch a seed coming back.
    """
    with_packdump(instance)
    make("fresh", instance)

    from packsmith.gui.main_window import MainWindow
    window = MainWindow(profile_name="fresh")
    try:
        assert window._blocked is None, "no packdump — the shell never got as far as seeding"
        assert window._tags.definitions_for("minecraft:item") == {}
        assert window._views.count == 0
    finally:
        window.close()


def test_a_new_profile_keeps_the_contract_it_was_given(userdata, instance):
    """Round-trip through `profile.json`, because that file IS the contract — a wrong
    loader or version silently reinterprets every tag against a registry that isn't the
    user's (§3.1)."""
    make("contract", instance)
    reloaded = Profile.load("contract")

    assert reloaded.loader == "forge"
    assert reloaded.mc_version == "1.20.1"
    assert reloaded.loader_version == "47.4.13"
    assert str(reloaded.mc_path) == str(instance)


def test_settings_survive_being_written_and_reloaded(userdata, instance):
    """`pack_loader` decides where every datapack resolver writes. Losing it on a save is
    not a cosmetic loss."""
    profile = make("settings", instance)
    profile.settings["pack_loader"] = "Paxi"
    profile.settings["override_target_datapacks"] = "WOW"
    profile.save()

    reloaded = Profile.load("settings")
    assert reloaded.settings["pack_loader"] == "Paxi"
    assert reloaded.settings["override_target_datapacks"] == "WOW"


def test_a_second_profile_of_the_same_name_is_refused(userdata, instance):
    """Not overwritten. Creating over a live profile would destroy every tag in it, and
    the user asked to create rather than replace."""
    make("taken", instance)

    with pytest.raises(ValueError):
        make("taken", instance)

    assert list_profiles() == ["taken"]


# --- what deleting one leaves behind -------------------------------------------------------

def test_deleting_a_profile_forgets_its_remembered_layout(userdata, instance):
    """The bug this file was written for.

    UI state is keyed by profile NAME, and a name is reusable. The layout it stores refers
    to rows by *id* — a View group holding views 1 and 2, column widths per tag, a
    remembered job. Delete the profile, make a new one with the same name, and the panel
    restores a group of Views that do not exist, with no clue where it came from. Deleting
    the database never touched any of it.
    """
    make("recycled", instance)
    save_ui_state("recycled", view_layout=[{"group": "Removal", "views": [1, 2]}],
                  last_job=5)
    assert load_ui_state("recycled") != {}

    delete_profile("recycled")

    assert load_ui_state("recycled") == {}, "a deleted profile's furniture outlived it"
    make("recycled", instance)
    assert load_ui_state("recycled") == {}, "the new profile inherited the dead one's layout"


def test_deleting_one_profile_leaves_the_others_alone(userdata, instance):
    """`forget_ui_state` edits a shared file, so the obvious way to get it wrong is to
    write back a dict that dropped somebody else's key."""
    make("going", instance)
    make("staying", instance)
    save_ui_state("going", bottom_height=300)
    save_ui_state("staying", bottom_height=222, last_job=9)

    delete_profile("going")

    assert load_ui_state("staying") == {"bottom_height": 222, "last_job": 9}
    assert list_profiles() == ["staying"]


def test_deleting_a_profile_takes_its_whole_directory(userdata, instance):
    """Tags, views, jobs, history, authored packages, packdump snapshots — all of it."""
    profile = make("doomed", instance)
    (profile.root / "packages" / "mine").mkdir(parents=True, exist_ok=True)
    (profile.root / "packages" / "mine" / "manifest.json5").write_text(
        '[package]\nname = "mine"\n', encoding="utf-8")
    UserDB(profile.root / "profile.db").close()
    root = profile.root
    assert root.exists()

    delete_profile("doomed")

    assert not root.exists()
    assert list_profiles() == []


def test_deleting_a_profile_does_not_touch_the_minecraft_instance(userdata, instance):
    """The one that would be unforgivable. A profile *points at* a game folder; it does not
    contain one, and deleting your work must never delete your game."""
    make("pointer", instance)

    delete_profile("pointer")

    assert instance.exists()
    assert (instance / "config" / "quark-common.toml").read_text(encoding="utf-8") == "a = 1"


def test_deleting_something_that_is_not_there_says_so(userdata):
    """Rather than succeeding quietly, which would make a typo look like a deletion."""
    with pytest.raises(ValueError):
        delete_profile("never_existed")


def test_deleting_a_profile_that_was_never_opened_is_harmless(userdata, instance):
    """It has no furniture to forget, and looking for some must not raise."""
    make("unopened", instance)

    delete_profile("unopened")

    assert load_ui_state("unopened") == {}
    assert list_profiles() == []
