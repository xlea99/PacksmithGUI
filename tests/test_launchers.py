"""Finding the real Minecraft client jar — design 3.1.

The jar holds what the packdump can't: vanilla's own loot tables, recipes and tags
(5,888 files under `data/` in 1.20.1), plus the authoritative `pack_format`. Reading it is
easy; *finding* it is the problem, because launchers don't agree and there is nothing to
ask.

So the shape under test is three strategies ending in one that always works — an explicit
user setting, then detection, then an honest nothing. The list of launchers will always be
incomplete, so being wrong about one has to cost a row of data rather than a redesign.
"""
import json
import zipfile

import pytest

from packsmith.core import launchers
from packsmith.core.capabilities import pack_format_for


def make_jar(path, version="1.20.1", data=15, resource=15, *, valid=True):
    """A client jar, as far as anything here is concerned: it is `version.json` that
    matters, not the name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        if valid:
            archive.writestr("version.json", json.dumps({
                "id": version, "pack_version": {"data": data, "resource": resource}}))
        archive.writestr("data/minecraft/loot_tables/blocks/oak_leaves.json", "{}")
    return path


@pytest.fixture
def curseforge(tmp_path):
    """CurseForge's real layout: instances and `Install/` are siblings under one root."""
    instance = tmp_path / "curseforge" / "minecraft" / "Instances" / "My Pack"
    instance.mkdir(parents=True)
    make_jar(tmp_path / "curseforge" / "minecraft" / "Install" / "versions"
             / "1.20.1" / "1.20.1.jar")
    return instance


# --- detection ---------------------------------------------------------------------------

def test_the_curseforge_layout_is_found(curseforge):
    found = launchers.locate(curseforge, "1.20.1")
    assert found is not None
    assert found.launcher == "CurseForge"
    assert found.path.name == "1.20.1.jar"


@pytest.mark.parametrize("launcher, pattern", [
    ("Vanilla launcher", "versions/{v}/{v}.jar"),
    ("Modrinth App", "meta/versions/{v}/{v}.jar"),
    ("Prism / MultiMC",
     "libraries/com/mojang/minecraft/{v}/minecraft-{v}-client.jar"),
])
def test_every_known_layout_is_found(tmp_path, launcher, pattern):
    """Adding a launcher is adding a row to LAYOUTS — that is the whole design, so each
    row has to actually work."""
    root = tmp_path / "launcher"
    instance = root / "instances" / "pack" / ".minecraft"
    instance.mkdir(parents=True)
    make_jar(root / pattern.format(v="1.20.1"))

    found = launchers.locate(instance, "1.20.1")
    assert found is not None and found.launcher == launcher


def test_nothing_is_found_when_nothing_is_there(tmp_path):
    """An honest None, so the caller degrades instead of inventing a path."""
    instance = tmp_path / "instances" / "pack"
    instance.mkdir(parents=True)
    assert launchers.locate(instance, "1.20.1") is None


def test_the_search_does_not_climb_out_of_the_neighbourhood(tmp_path):
    """Walking up forever would eventually match somebody else's Minecraft."""
    deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "instance"
    deep.mkdir(parents=True)
    make_jar(tmp_path / "versions" / "1.20.1" / "1.20.1.jar")
    assert launchers.locate(deep, "1.20.1") is None


# --- verification, because a filename is a claim ---------------------------------------------

def test_a_jar_for_another_version_is_not_accepted(tmp_path):
    """Silently using vanilla data for a game the user isn't running is worse than finding
    nothing: a missing answer is obvious, a wrong one isn't."""
    instance = tmp_path / "Instances" / "pack"
    instance.mkdir(parents=True)
    make_jar(tmp_path / "versions" / "1.20.1" / "1.20.1.jar", version="1.19.2")
    assert launchers.locate(instance, "1.20.1") is None


def test_something_that_is_not_a_client_jar_is_rejected(tmp_path):
    instance = tmp_path / "Instances" / "pack"
    instance.mkdir(parents=True)
    make_jar(tmp_path / "versions" / "1.20.1" / "1.20.1.jar", valid=False)
    assert launchers.locate(instance, "1.20.1") is None


def test_verify_reports_the_version_it_really_is(tmp_path):
    jar = make_jar(tmp_path / "x.jar", version="1.19.4")
    assert launchers.verify(jar) == "1.19.4"
    assert launchers.verify(jar, "1.19.4") == "1.19.4"
    assert launchers.verify(jar, "1.20.1") is None


def test_a_missing_file_verifies_as_nothing(tmp_path):
    assert launchers.verify(tmp_path / "gone.jar") is None


# --- the user's own setting wins ---------------------------------------------------------------

def test_an_explicit_path_is_used(tmp_path, curseforge):
    """A launcher nobody has heard of must never be a dead end."""
    elsewhere = make_jar(tmp_path / "somewhere" / "custom.jar")
    found = launchers.locate(curseforge, "1.20.1", override=elsewhere)
    assert found.path == elsewhere and found.user_set


def test_an_explicit_path_is_not_quietly_replaced_by_detection(tmp_path, curseforge):
    """If someone has said where the jar is, preferring a detected one would make the
    setting a suggestion."""
    assert launchers.locate(curseforge, "1.20.1", override=tmp_path / "nope.jar") is None


def test_an_explicit_path_is_still_verified(tmp_path, curseforge):
    wrong = make_jar(tmp_path / "wrong.jar", version="1.19.2")
    assert launchers.locate(curseforge, "1.20.1", override=wrong) is None


# --- what the jar is for ------------------------------------------------------------------------

def test_the_pack_format_comes_from_the_jar(tmp_path):
    """Mojang's own numbers. They don't follow a pattern — 10, 12, 15, 18, 26, 41 across
    1.19→1.20.5 — so a memorised table is wrong the first release nobody updates it."""
    jar = make_jar(tmp_path / "1.20.1.jar", data=15, resource=15)
    assert launchers.pack_version(jar) == (15, 15)
    assert pack_format_for("1.20.1", "datapacks", client_jar=jar) == 15


def test_the_jar_overrules_the_table(tmp_path):
    """A future version the table has never heard of still gets the right answer."""
    jar = make_jar(tmp_path / "j.jar", version="1.99", data=77, resource=66)
    assert pack_format_for("1.99", "datapacks", client_jar=jar) == 77
    assert pack_format_for("1.99", "resourcepacks", client_jar=jar) == 66
    assert pack_format_for("1.99", "datapacks") == 15, "the table's fallback, unaided"


def test_an_older_single_number_format_is_understood(tmp_path):
    """Old jars stated one `pack_version` rather than a pair."""
    path = tmp_path / "old.jar"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("version.json", json.dumps({"id": "1.16.5", "pack_version": 6}))
    assert launchers.pack_version(path) == (6, 6)


def test_a_jar_without_pack_version_says_nothing(tmp_path):
    path = tmp_path / "x.jar"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("version.json", json.dumps({"id": "1.20.1"}))
    assert launchers.pack_version(path) is None
    # ...and the caller falls back to the table rather than failing.
    assert pack_format_for("1.20.1", "datapacks", client_jar=path) == 15


def test_vanilla_data_is_reachable_once_the_jar_is_found(curseforge):
    """The point of all this: the loot table for a vanilla block, without extracting."""
    from packsmith.core.archives import read_entries

    found = launchers.locate(curseforge, "1.20.1")
    paths = [entry.path for entry in read_entries(found.path)]
    assert "data/minecraft/loot_tables/blocks/oak_leaves.json" in paths


# --- profiles -----------------------------------------------------------------------------------

def test_a_profile_without_an_instance_locates_nothing():
    profile = type("P", (), {"mc_path": None, "mc_version": "1.20.1", "settings": {}})()
    assert launchers.locate_for(profile) is None


def test_a_profile_setting_overrides_detection(tmp_path, curseforge):
    elsewhere = make_jar(tmp_path / "custom" / "mc.jar")
    profile = type("P", (), {"mc_path": curseforge, "mc_version": "1.20.1",
                             "settings": {launchers.JAR_SETTING: str(elsewhere)}})()
    assert launchers.locate_for(profile).path == elsewhere
