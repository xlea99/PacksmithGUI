"""Snapshot round-trip and L1 immutability — design 3.1.

A snapshot is an *archive*, not a cache: `history/` is the only copy of what the game
looked like at that moment, and no amount of re-dumping brings back a version of the pack
that isn't installed any more. So anything a save silently drops is gone for good, which
is what makes the multi-locale case worth a test even while the mod dumps only en_us.
"""
import json

import pytest

from packsmith.core.packdump import Packdump

VALUES = {"minecraft:item": {"minecraft:stone": "Stone"}}


def write_dump(path, *, locales=None, items=("minecraft:stone",), mods=("alpha",),
               legacy_single_locale=False):
    """A dump on disk. `legacy_single_locale` writes the pre-fix layout — which is also
    exactly what the Forge mod writes today (one `localization.json`, hardcoded en_us)."""
    (path / "registries").mkdir(parents=True, exist_ok=True)
    (path / "attributes").mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({
        "type": "packsmith_full_dump", "schema_version": 1,
        "generated_at_utc": "2026-08-09T12:00:00+00:00",
        "minecraft_version": "1.20.1", "loader": "forge", "loader_version": "47.4.10",
        "mods": [{"mod_id": m, "name": m.title(), "version": "1.0"} for m in mods],
        "registries": [{"type": "minecraft:item", "file": "minecraft_item.json",
                        "count": len(items)}],
    }), encoding="utf-8")
    (path / "registries" / "minecraft_item.json").write_text(
        json.dumps({"values": list(items)}), encoding="utf-8")

    locales = locales or {"en_us": VALUES}
    for locale, values in locales.items():
        name = "localization.json" if legacy_single_locale else f"localization.{locale}.json"
        (path / "attributes" / name).write_text(
            json.dumps({"locale": locale, "values": values}), encoding="utf-8")
    return path


# --- multi-locale round trip -------------------------------------------------------------

def test_every_locale_survives_a_save(tmp_path):
    """The bug: save() looped locales and wrote each to the same localization.json, so the
    last one won and the rest vanished from the archive."""
    source = write_dump(tmp_path / "src", locales={
        "en_us": {"minecraft:item": {"minecraft:stone": "Stone"}},
        "de_de": {"minecraft:item": {"minecraft:stone": "Stein"}},
        "fr_fr": {"minecraft:item": {"minecraft:stone": "Pierre"}},
    })
    dump = Packdump.load(source)
    assert set(dump._localizations) == {"en_us", "de_de", "fr_fr"}, "load lost a locale"

    dump.save(tmp_path / "out")
    reloaded = Packdump.load(tmp_path / "out")

    assert set(reloaded._localizations) == {"en_us", "de_de", "fr_fr"}
    assert reloaded._localizations["de_de"]["minecraft:item"]["minecraft:stone"] == "Stein"
    assert reloaded._localizations["fr_fr"]["minecraft:item"]["minecraft:stone"] == "Pierre"


def test_the_single_locale_layout_still_loads(tmp_path):
    """Snapshots already in `history/` were written the old way, and the Forge mod still
    writes it. An archive format that can't read its own past is not an archive."""
    dump = Packdump.load(write_dump(tmp_path / "src", legacy_single_locale=True))
    assert dump.locale == "en_us"
    assert dump.localization["minecraft:item"]["minecraft:stone"] == "Stone"


def test_english_stays_active_regardless_of_filename_order(tmp_path):
    """Locales are discovered by glob, so without this the active locale would be whichever
    sorted first — adding a German dump would re-language the whole GUI."""
    dump = Packdump.load(write_dump(tmp_path / "src", locales={
        "de_de": {"minecraft:item": {}}, "en_us": {"minecraft:item": {}}}))
    assert dump.locale == "en_us"


def test_a_dump_with_no_localizations_is_corrupt_not_silently_empty(tmp_path):
    source = write_dump(tmp_path / "src")
    for f in (source / "attributes").iterdir():
        f.unlink()
    with pytest.raises(ValueError, match="corrupted"):
        Packdump.load(source)


# --- what counts as "changed" ------------------------------------------------------------

def test_a_renamed_display_name_is_not_an_identical_dump(tmp_path):
    """Localizations were left out of __eq__, so a mod renaming an item produced no import
    and the old name persisted in the GUI for the life of the profile."""
    a = Packdump.load(write_dump(tmp_path / "a", locales={
        "en_us": {"minecraft:item": {"minecraft:stone": "Rose Quartz Block"}}}))
    b = Packdump.load(write_dump(tmp_path / "b", locales={
        "en_us": {"minecraft:item": {"minecraft:stone": "Rose Quartz"}}}))
    assert a != b


def test_a_new_locale_is_not_an_identical_dump(tmp_path):
    a = Packdump.load(write_dump(tmp_path / "a"))
    b = Packdump.load(write_dump(tmp_path / "b", locales={
        "en_us": VALUES, "de_de": {"minecraft:item": {"minecraft:stone": "Stein"}}}))
    assert a != b


def test_an_actually_identical_dump_still_compares_equal(tmp_path):
    """The other half: equality has to stay cheap to satisfy, or every launch burns a
    snapshot slot."""
    assert Packdump.load(write_dump(tmp_path / "a")) == Packdump.load(write_dump(tmp_path / "b"))


def test_a_mod_version_bump_counts_as_a_change_even_with_identical_registries(tmp_path):
    """Deliberate, not drift: mod versions are displayed as provenance, and a snapshot is
    how the user gets to diff against "the pack before that update"."""
    a = Packdump.load(write_dump(tmp_path / "a"))
    b_path = write_dump(tmp_path / "b")
    meta = json.loads((b_path / "meta.json").read_text(encoding="utf-8"))
    meta["mods"][0]["version"] = "2.0"
    (b_path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert a != Packdump.load(b_path)


# --- L1 immutability (design 3.1) ---------------------------------------------------------

def test_the_registry_cannot_be_rebound_through_the_getter(tmp_path):
    """§3.1's "read-only, Packsmith never writes it" was convention only — the guards were
    commented out "temp… for ease of debug"."""
    dump = Packdump.load(write_dump(tmp_path / "src"))
    with pytest.raises(TypeError):
        dump.registry["minecraft:item"] = {"values": []}
    with pytest.raises(TypeError):
        del dump.registry["minecraft:item"]


def test_the_mod_list_cannot_be_rebound_through_the_getter(tmp_path):
    dump = Packdump.load(write_dump(tmp_path / "src"))
    with pytest.raises(TypeError):
        dump.mods["fake"] = {"mod_id": "fake"}


def test_localizations_cannot_be_rebound_through_the_getter(tmp_path):
    dump = Packdump.load(write_dump(tmp_path / "src"))
    with pytest.raises(TypeError):
        dump.localization["minecraft:item"] = {}


def test_reading_still_works_normally(tmp_path):
    """The guard must not cost the read patterns every caller uses."""
    dump = Packdump.load(write_dump(tmp_path / "src"))
    assert dump.registry.get("minecraft:item")["values"] == ["minecraft:stone"]
    assert sorted(dump.registry) == ["minecraft:item"]
    assert sorted(dump.registry.keys()) == ["minecraft:item"]
    assert len(dump.mods) == 1
    assert dump.attribute("minecraft:item", "minecraft:stone", "localization") == "Stone"
