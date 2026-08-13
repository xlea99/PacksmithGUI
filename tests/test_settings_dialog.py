"""Packsmith Settings — the skeleton (Open Questions §Settings).

Two settings that already existed in the profile and had no hand on them: auto-adopt, and
the Minecraft client jar. Both live in `profile.settings`, which is why the dialog is
profile-scoped rather than global — two profiles pointing at different instances genuinely
want different answers.

The load-bearing behaviour here is the **jar warning**. `launchers.locate` treats an
unusable override exactly like no override at all: it returns None and moves on. So a path
that doesn't verify, saved quietly, would behave identically to the broken state the user
opened this dialog to fix — while looking fixed. The dialog refuses instead.
"""
import json
import os
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QDialog

from packsmith.core import launchers
from packsmith.core.packdump import AUTO_ADOPT_SETTING
from packsmith.gui.shell import style


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def make_jar(path, version="1.20.1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("version.json", json.dumps(
            {"id": version, "pack_version": {"data": 15, "resource": 15}}))
    return path


class Profile:
    """Just what the dialog reads — a real Profile needs a directory on disk."""

    def __init__(self, mc_path, settings=None, mc_version="1.20.1"):
        self.name = "probe"
        self.mc_path = mc_path
        self.mc_version = mc_version
        self.settings = dict(settings or {})


@pytest.fixture
def undetectable(tmp_path):
    """An instance with no client jar anywhere near it — the case the warning is for."""
    instance = tmp_path / "instances" / "pack"
    instance.mkdir(parents=True)
    return Profile(instance)


@pytest.fixture
def detectable(tmp_path):
    """CurseForge's real layout, which detection knows."""
    instance = tmp_path / "curseforge" / "minecraft" / "Instances" / "My Pack"
    instance.mkdir(parents=True)
    make_jar(tmp_path / "curseforge" / "minecraft" / "Install" / "versions"
             / "1.20.1" / "1.20.1.jar")
    return Profile(instance)


def dialog_for(profile):
    from packsmith.gui.settings_dialog import SettingsDialog
    return SettingsDialog(profile)


# --- auto-adopt ------------------------------------------------------------------------

def test_auto_adopt_defaults_to_on(undetectable):
    """§3.1: stale is the worse failure mode, so the default is on — and a settings dialog
    that showed it off by default would quietly misreport what the app is doing."""
    assert dialog_for(undetectable)._auto_adopt.isChecked()


def test_an_existing_setting_is_reflected(undetectable):
    undetectable.settings[AUTO_ADOPT_SETTING] = False
    assert not dialog_for(undetectable)._auto_adopt.isChecked()


def test_turning_auto_adopt_off_is_saved(undetectable):
    dialog = dialog_for(undetectable)
    dialog._auto_adopt.setChecked(False)
    dialog.accept()
    assert dialog.result_settings[AUTO_ADOPT_SETTING] is False


def test_cancelling_changes_nothing(undetectable):
    dialog = dialog_for(undetectable)
    dialog._auto_adopt.setChecked(False)
    dialog.reject()
    assert dialog.result_settings is None


def test_other_settings_are_carried_through(undetectable):
    """The dialog edits two keys; it must not become a filter that drops the rest."""
    undetectable.settings["max_packdump_snapshot_count"] = 3
    dialog = dialog_for(undetectable)
    dialog.accept()
    assert dialog.result_settings["max_packdump_snapshot_count"] == 3


# --- the jar warning -------------------------------------------------------------------

def test_a_missing_jar_is_called_out_in_red(undetectable):
    """The whole reason this row exists: detection found nothing, so vanilla data and the
    real pack_format are silently unavailable. Saying nothing would leave no way to learn
    why."""
    dialog = dialog_for(undetectable)
    assert dialog.jar_warning_visible
    assert "Not found automatically" in dialog._jar_status.text()


def test_a_detected_jar_reports_which_launcher_found_it(detectable):
    dialog = dialog_for(detectable)
    assert not dialog.jar_warning_visible
    assert "CurseForge" in dialog._jar_status.text()
    assert style.SUCCESS in dialog._jar_status.styleSheet()


def test_setting_a_valid_path_clears_the_warning(undetectable, tmp_path):
    dialog = dialog_for(undetectable)
    assert dialog.jar_warning_visible

    dialog._jar_path.setText(str(make_jar(tmp_path / "elsewhere" / "mc.jar")))
    assert not dialog.jar_warning_visible
    assert "1.20.1" in dialog._jar_status.text()


def test_a_path_to_nothing_says_so(undetectable, tmp_path):
    dialog = dialog_for(undetectable)
    dialog._jar_path.setText(str(tmp_path / "nope.jar"))
    assert dialog.jar_warning_visible
    assert "no file at that path" in dialog._jar_status.text()


def test_a_jar_for_the_wrong_version_is_named_as_such(undetectable, tmp_path):
    """A filename is a claim; `version.json` is the fact. Using 1.19 data for a 1.20 pack
    is worse than having none — a missing answer is obvious, a wrong one isn't."""
    dialog = dialog_for(undetectable)
    dialog._jar_path.setText(str(make_jar(tmp_path / "old.jar", version="1.19.2")))
    assert dialog.jar_warning_visible
    assert "1.19.2" in dialog._jar_status.text()


# --- committing the jar path ------------------------------------------------------------

def test_a_verified_path_is_stored(undetectable, tmp_path):
    jar = make_jar(tmp_path / "custom" / "mc.jar")
    dialog = dialog_for(undetectable)
    dialog._jar_path.setText(str(jar))
    dialog.accept()
    assert dialog.result_settings[launchers.JAR_SETTING] == str(jar)


def test_the_stored_path_is_one_locate_actually_accepts(undetectable, tmp_path):
    """Closes the loop rather than trusting the string: what the dialog saved has to be
    what the locator then finds, or the setting is decorative."""
    jar = make_jar(tmp_path / "custom" / "mc.jar")
    dialog = dialog_for(undetectable)
    dialog._jar_path.setText(str(jar))
    dialog.accept()

    undetectable.settings = dialog.result_settings
    found = launchers.locate_for(undetectable)
    assert found is not None and found.path == jar and found.user_set


def test_an_unverifiable_path_is_refused_rather_than_saved(undetectable, tmp_path):
    """The failure this guards: `locate` treats a bad override as "nothing found", so a
    saved-but-broken path behaves exactly like the empty state the user was trying to fix,
    while looking fixed."""
    dialog = dialog_for(undetectable)
    dialog._jar_path.setText(str(make_jar(tmp_path / "wrong.jar", version="1.19.2")))
    dialog.accept()

    assert dialog.result_settings is None, "a jar for the wrong version was accepted"
    assert dialog.jar_warning_visible, "refused, but the dialog didn't say why"
    assert dialog.result() != QDialog.Accepted, "the dialog closed on an invalid path"


def test_clearing_the_path_returns_to_detection(detectable, tmp_path):
    """An empty field is 'detect it', not 'store an empty string' — which would be an
    override that can never verify."""
    detectable.settings[launchers.JAR_SETTING] = str(make_jar(tmp_path / "x.jar"))
    dialog = dialog_for(detectable)
    dialog._jar_path.setText("")
    dialog.accept()

    assert launchers.JAR_SETTING not in dialog.result_settings
    detectable.settings = dialog.result_settings
    assert launchers.locate_for(detectable).launcher == "CurseForge"


def test_an_existing_override_is_shown_on_open(undetectable, tmp_path):
    jar = make_jar(tmp_path / "custom" / "mc.jar")
    undetectable.settings[launchers.JAR_SETTING] = str(jar)
    assert dialog_for(undetectable)._jar_path.text() == str(jar)


# --- the global pack loader (§8.1) ---------------------------------------------------------

class Dump:
    def __init__(self, *mods):
        self.mods = {m: {"mod_id": m} for m in mods}
        self.registry = {}


def dialog_with_loaders(profile, *mods):
    from packsmith.gui.settings_dialog import SettingsDialog
    return SettingsDialog(profile, packdump=Dump(*mods))


def test_only_installed_loaders_are_offered(undetectable):
    """No path chooser and no full list: each loader dictates its own directory, so the
    only real choice is which of the mods you actually have."""
    dialog = dialog_with_loaders(undetectable, "paxi", "moonlight")
    offered = [dialog._loader.itemText(i) for i in range(dialog._loader.count())]
    assert offered == ["Paxi", "Moonlight"]


def test_no_loader_at_all_is_a_red_error(undetectable):
    """Overrides have nowhere to go, and §8.1 says the user is pointed at installing one."""
    dialog = dialog_with_loaders(undetectable, "jei")
    assert style.ERROR in dialog._loader_status.styleSheet()
    assert "No global pack loader installed" in dialog._loader_status.text()
    assert not dialog._loader.isEnabled()
    assert not dialog._order_button.isEnabled()


def test_the_capability_summary_follows_the_selection(undetectable):
    """Moonlight genuinely cannot do resource packs, and saying so here is the whole point
    of the summary — otherwise the user picks it and silently loses a workflow."""
    dialog = dialog_with_loaders(undetectable, "paxi", "moonlight")

    dialog._loader.setCurrentIndex(dialog._loader.findData("Moonlight"))
    text = dialog._loader_status.text()
    assert "✓ Datapacks" in text and "✗ Resource packs" in text and "✗ Load ordering" in text

    dialog._loader.setCurrentIndex(dialog._loader.findData("Paxi"))
    text = dialog._loader_status.text()
    assert "✓ Resource packs" in text and "✓ Load ordering" in text


def test_the_ordering_button_is_live_only_where_ordering_exists(undetectable):
    dialog = dialog_with_loaders(undetectable, "paxi", "openloader")

    dialog._loader.setCurrentIndex(dialog._loader.findData("Paxi"))
    assert dialog._order_button.isEnabled()

    dialog._loader.setCurrentIndex(dialog._loader.findData("Open Loader"))
    assert not dialog._order_button.isEnabled(), \
        "offered to reorder packs for a loader that has no such notion"


def test_choosing_a_loader_is_saved(undetectable):
    from packsmith.core.capabilities import PACK_LOADER_SETTING
    dialog = dialog_with_loaders(undetectable, "paxi", "moonlight")
    dialog._loader.setCurrentIndex(dialog._loader.findData("Moonlight"))
    dialog.accept()
    assert dialog.result_settings[PACK_LOADER_SETTING] == "Moonlight"


def test_a_stored_choice_comes_back_selected(undetectable):
    from packsmith.core.capabilities import PACK_LOADER_SETTING
    undetectable.settings[PACK_LOADER_SETTING] = "Moonlight"
    dialog = dialog_with_loaders(undetectable, "paxi", "moonlight")
    assert dialog._loader.currentData() == "Moonlight"


def test_a_choice_whose_mod_was_uninstalled_falls_back_without_being_forgotten(undetectable):
    """Reinstalling the mod should restore the user's choice, not find it quietly
    overwritten by whatever happened to be first."""
    from packsmith.core.capabilities import PACK_LOADER_SETTING
    undetectable.settings[PACK_LOADER_SETTING] = "Global Packs"
    dialog = dialog_with_loaders(undetectable, "paxi")

    assert dialog._loader.currentData() == "Paxi"
    dialog.reject()
    assert undetectable.settings[PACK_LOADER_SETTING] == "Global Packs"


# --- what is deliberately absent ---------------------------------------------------------

def test_there_is_no_load_order_setting(detectable):
    """Datapack load order is *pack content* — it decides which override wins — no more an
    application setting than plugin order is a Mod Organizer setting. It belongs to the
    pack as first-class user data, and filing it here would file the user's work under the
    app's preferences."""
    dialog = dialog_for(detectable)
    text = " ".join(child.text() for child in dialog.findChildren(type(dialog._jar_status)))
    assert "load order" not in text.lower()
    assert not hasattr(dialog, "_load_order")


def test_the_dialog_shows_the_loader_the_app_is_actually_using(undetectable):
    """Reported from real use: on a Paxi + Moonlight pack with nothing chosen, the dialog
    displayed "Paxi" while every override went to Moonlight — two different orderings, the
    combo listing PACK_LOADERS order and the resolver sorting alphabetically.

    A settings screen that disagrees with the running app is worse than no settings screen,
    so the displayed default is whatever `resolve` actually picks.
    """
    from packsmith.core.capabilities import DATAPACKS_WRITE, resolve
    from packsmith.integrations import PACK_LOADERS

    dump = Dump("paxi", "moonlight")
    dialog = dialog_with_loaders(undetectable, "paxi", "moonlight")
    actual = resolve(dump, loaders=PACK_LOADERS,
                     instance_root=undetectable.mc_path).provider_for(DATAPACKS_WRITE)

    assert dialog._loader.currentData() == actual.name
