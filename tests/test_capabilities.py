"""Capabilities, providers, and the resolution table — design 7.1 / 8.1.

The distinction being tested: a **capability** is what an action asks for, a **provider**
is what answers. The granularity is per capability rather than per provider, and that is
load-bearing — Paxi supports load ordering and OpenLoader does not, so an action needing
order can be refused where it is unavailable instead of quietly doing the wrong thing.
Treating loaders as interchangeable would make that unsayable.
"""
import json

import pytest

from packsmith.core.capabilities import (
    BUILT_IN, BUILT_IN_PROVIDER, CapabilityError, DATAPACKS_ORDERING, DATAPACKS_READ,
    DATAPACKS_WRITE, PackLoaderProvider, RESOURCEPACKS_WRITE, resolve)
from packsmith.integrations import PACK_LOADERS, loaders_for
from packsmith.integrations.paxi import PaxiProvider


class Dump:
    """Just enough packdump: detection reads the mod list (§8.1)."""

    def __init__(self, *mod_ids):
        self.mods = {mod_id: {"mod_id": mod_id} for mod_id in mod_ids}


class FakeLoader(PackLoaderProvider):
    """A second loader, so disambiguation can be tested without inventing a real one."""

    name = "OpenLoader"
    mod_id = "openloader"
    # Deliberately WITHOUT ordering — the real OpenLoader doesn't have it, and that
    # asymmetry is the reason capabilities are named individually.
    capabilities = (DATAPACKS_READ, DATAPACKS_WRITE, RESOURCEPACKS_WRITE)

    def datapack_root(self, instance_root):
        from pathlib import Path
        return Path(instance_root) / "openloader" / "data"

    def resourcepack_root(self, instance_root):
        from pathlib import Path
        return Path(instance_root) / "openloader" / "resources"


# --- the table ---------------------------------------------------------------------------

def test_the_runtime_answers_for_its_own_capabilities():
    table = resolve(Dump())
    assert all(table.entries[name].provider == BUILT_IN_PROVIDER for name in BUILT_IN)


def test_a_capability_nobody_provides_is_simply_absent():
    """§7.1: "Capabilities with no active provider are simply absent from the table" —
    which is what makes "can this action run here" answerable without special cases."""
    table = resolve(Dump())
    assert not table.satisfies(DATAPACKS_WRITE)
    assert table.missing([DATAPACKS_WRITE, "log"]) == [DATAPACKS_WRITE]


def test_an_installed_loader_provides_its_capabilities():
    table = resolve(Dump("paxi"), loaders=[PaxiProvider()])
    assert table.entries[DATAPACKS_WRITE].provider == "Paxi"
    assert table.satisfies(DATAPACKS_ORDERING)


def test_a_loader_that_is_not_installed_provides_nothing():
    assert not resolve(Dump("jei"), loaders=[PaxiProvider()]).satisfies(DATAPACKS_WRITE)


def test_requiring_a_missing_capability_says_what_is_missing():
    with pytest.raises(CapabilityError, match="datapacks.write"):
        resolve(Dump()).require(DATAPACKS_WRITE)


# --- two loaders at once (§8.1's multi-loader case) ----------------------------------------

def test_the_second_loader_becomes_an_alternative_not_a_conflict():
    """§7.1: Packsmith picks one as the default and offers the choice. Neither is an
    error — having both installed is unusual but legal."""
    table = resolve(Dump("paxi", "openloader"),
                    loaders=[PaxiProvider(), FakeLoader()])
    entry = table.entries[DATAPACKS_WRITE]
    assert entry.contested
    assert entry.provider in ("Paxi", "OpenLoader")
    assert set(entry.alternatives) | {entry.provider} == {"Paxi", "OpenLoader"}


def test_the_users_preference_wins():
    """Sticky per profile (§8.1), so the answer doesn't change between launches."""
    table = resolve(Dump("paxi", "openloader"), loaders=[PaxiProvider(), FakeLoader()],
                    preferred="OpenLoader")
    assert table.entries[DATAPACKS_WRITE].provider == "OpenLoader"
    assert table.entries[DATAPACKS_WRITE].alternatives == ("Paxi",)


def test_a_capability_only_one_loader_has_is_never_contested():
    """The asymmetry that justifies per-capability granularity: ordering comes from Paxi
    alone, so there is nothing to disambiguate even with both installed."""
    table = resolve(Dump("paxi", "openloader"), loaders=[PaxiProvider(), FakeLoader()],
                    preferred="OpenLoader")
    assert table.entries[DATAPACKS_ORDERING].provider == "Paxi"
    assert not table.entries[DATAPACKS_ORDERING].contested


def test_an_action_needing_ordering_is_refused_where_it_is_absent():
    """The whole reason ordering is its own capability rather than part of write: an
    action that depends on load order fails loudly on OpenLoader instead of silently
    producing something wrong."""
    table = resolve(Dump("openloader"), loaders=[PaxiProvider(), FakeLoader()])
    assert table.satisfies(DATAPACKS_WRITE)
    assert table.missing([DATAPACKS_WRITE, DATAPACKS_ORDERING]) == [DATAPACKS_ORDERING]


# --- Paxi, against the layout that is really on disk ----------------------------------------

@pytest.fixture
def instance(tmp_path):
    paxi = tmp_path / "config" / "paxi"
    (paxi / "datapacks" / "tweaks_create" / "data").mkdir(parents=True)
    (paxi / "datapacks" / "rei_removals").mkdir(parents=True)
    (paxi / "resourcepacks").mkdir(parents=True)
    (paxi / "resourcepacks" / "FreshAnimations.zip").write_bytes(b"PK\x03\x04")
    (paxi / "datapack_load_order.json").write_text(
        json.dumps({"loadOrder": ["rei_removals"]}, indent=2), encoding="utf-8")
    return tmp_path


def test_paxi_is_detected_from_the_mod_list(instance):
    assert PaxiProvider().detect(Dump("paxi", "jei")) is True
    assert PaxiProvider().detect(Dump("jei")) is False
    assert PaxiProvider().detect(None) is False


def test_the_roots_are_where_paxi_actually_puts_things(instance):
    paxi = PaxiProvider()
    assert paxi.datapack_root(instance) == instance / "config" / "paxi" / "datapacks"
    assert paxi.resourcepack_root(instance) == instance / "config" / "paxi" / "resourcepacks"


def test_packs_are_listed_in_load_order(instance):
    """Paxi's distinguishing feature is that order matters, so listing alphabetically
    would show something the game disagrees with."""
    assert PaxiProvider().packs(instance, "datapacks") == ["rei_removals", "tweaks_create"]


def test_packs_not_named_in_the_order_file_come_after(instance):
    PaxiProvider().set_load_order(instance, ["tweaks_create"], "datapacks")
    assert PaxiProvider().packs(instance, "datapacks") == ["tweaks_create", "rei_removals"]


def test_zips_count_as_packs(instance):
    """The user's own resourcepacks folder holds both folders and zips."""
    assert PaxiProvider().packs(instance, "resourcepacks") == ["FreshAnimations.zip"]


def test_the_load_order_file_matches_paxis_own_format(instance):
    paxi = PaxiProvider()
    paxi.set_load_order(instance, ["a", "b"], "datapacks")
    written = (instance / "config" / "paxi" / "datapack_load_order.json").read_text(
        encoding="utf-8")
    assert json.loads(written) == {"loadOrder": ["a", "b"]}
    assert written.startswith('{\n  "loadOrder"'), "should stay diffable against Paxi's own"


def test_a_missing_or_broken_order_file_means_no_order(instance, tmp_path):
    """Paxi treats it that way itself, and refusing to list packs over a broken sidecar
    would be a worse answer than listing them unordered."""
    (instance / "config" / "paxi" / "datapack_load_order.json").write_text("{ oops",
                                                                          encoding="utf-8")
    assert PaxiProvider().load_order(instance, "datapacks") == []
    assert len(PaxiProvider().packs(instance, "datapacks")) == 2


def test_an_empty_instance_lists_nothing(tmp_path):
    assert PaxiProvider().packs(tmp_path, "datapacks") == []


# --- where an override lands ------------------------------------------------------------------

def test_an_override_reproduces_the_jar_path_inside_the_pack(instance):
    """§8.1: "just writes the file to the right place and lets Minecraft's pack layering
    do the rest" — the member path is the one the mod uses inside its own jar."""
    target = PaxiProvider().override_path(
        instance, "tweaks_create", "data/minecraft/recipes/x.json")
    assert target == (instance / "config" / "paxi" / "datapacks" / "tweaks_create"
                      / "data" / "minecraft" / "recipes" / "x.json")


def test_an_override_cannot_escape_its_pack(instance):
    with pytest.raises(ValueError, match="escapes"):
        PaxiProvider().override_path(instance, "tweaks_create", "../../../evil.json")


def test_a_pack_name_with_a_separator_is_refused(instance):
    with pytest.raises(ValueError, match="invalid pack name"):
        PaxiProvider().override_path(instance, "a/b", "data/x.json")


def test_creating_a_pack_writes_the_mcmeta_minecraft_requires(instance):
    """A pack without one is silently ignored by the game — the files are there, the
    folder is there, and nothing happens."""
    folder = PaxiProvider().create_pack(instance, "new_pack")
    meta = json.loads((folder / "pack.mcmeta").read_text(encoding="utf-8"))
    assert meta["pack"]["pack_format"] and meta["pack"]["description"]
    assert (folder / "data").is_dir()


def test_creating_a_resourcepack_makes_assets_not_data(instance):
    folder = PaxiProvider().create_pack(instance, "new_rp", kind="resourcepacks")
    assert (folder / "assets").is_dir() and not (folder / "data").exists()


# --- the registry ------------------------------------------------------------------------------

def test_the_registry_only_returns_installed_loaders():
    assert [l.name for l in loaders_for(Dump("paxi"))] == ["Paxi"]
    assert loaders_for(Dump("jei")) == []


def test_base_code_reaches_loaders_only_through_the_registry():
    """The boundary a plugin system would need later (§3.4). Nothing outside the
    integrations package should name a specific loader."""
    assert all(isinstance(loader, PackLoaderProvider) for loader in PACK_LOADERS)
