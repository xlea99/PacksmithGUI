"""The four global pack loaders — design 8.1.

Minecraft has no vanilla global datapack mechanism, so every override workflow runs through
one of these mods. All four layouts here were **verified by installing the mods on a real
1.20.1 Forge instance** and reading the folders and configs they generated. That mattered:
two of the four are documented wrongly upstream — Paxi's CurseForge page describes a newer
`config/_paxi_/`, and Open Loader's own README on the 1.20.1 branch points at the instance
root rather than `config/`. Building from either would have written into a directory the
game never reads, and failed *silently*.

The load-bearing idea under test is that loaders are **not interchangeable**. Paxi orders
packs and Open Loader cannot; Moonlight does datapacks and has no concept of resource
packs; Global Packs is driven by a list in a toml. Modelling them as one thing would make
those differences unsayable, which is exactly how an action ends up quietly producing the
wrong result.
"""
import json
import os

import pytest

from packsmith.core.capabilities import (
    DATAPACKS_ORDERING, DATAPACKS_READ, DATAPACKS_WRITE, CapabilityError,
    RESOURCEPACKS_ORDERING, RESOURCEPACKS_WRITE, resolve)
from packsmith.integrations import PACK_LOADERS
from packsmith.integrations.globalpacks import GlobalPacksProvider
from packsmith.integrations.moonlight import MoonlightProvider
from packsmith.integrations.openloader import OpenLoaderProvider
from packsmith.integrations.paxi import PaxiProvider


class Dump:
    def __init__(self, *mods):
        self.mods = {m: {"mod_id": m} for m in mods}
        self.registry = {}


ALL_FOUR = Dump("paxi", "openloader", "moonlight", "globalpacks")


# --- detection -------------------------------------------------------------------------

@pytest.mark.parametrize("provider, mod_id", [
    (PaxiProvider(), "paxi"),
    (OpenLoaderProvider(), "openloader"),
    (MoonlightProvider(), "moonlight"),
    (GlobalPacksProvider(), "globalpacks"),
])
def test_each_loader_detects_by_its_real_mod_id(provider, mod_id):
    """Every id read from the mod's own `mods.toml`. A wrong one fails silently — the
    loader simply never appears — so these are pinned rather than trusted."""
    assert provider.mod_id == mod_id
    assert provider.detect(Dump(mod_id))
    assert not provider.detect(Dump("something_else"))


def test_every_shipped_loader_has_a_verified_mod_id():
    """The guard against reintroducing a guess. An unverified id is not a bug on its own,
    but it *is* something that must be known rather than assumed, because its failure mode
    is invisible."""
    unverified = [l.name for l in PACK_LOADERS if not l.mod_id_verified]
    assert unverified == [], f"mod ids never read from a real jar: {unverified}"


def test_detection_survives_a_pack_with_no_loader_at_all():
    for provider in PACK_LOADERS:
        assert not provider.detect(Dump("jei", "quark"))
        assert not provider.detect(None)


# --- paths, as they are actually on disk -------------------------------------------------

@pytest.mark.parametrize("provider, expected", [
    (PaxiProvider(), "config/paxi/datapacks"),
    (OpenLoaderProvider(), "config/openloader/data"),
    (GlobalPacksProvider(), "global_packs/required_data"),
])
def test_datapack_roots_match_the_installed_mods(tmp_path, provider, expected):
    assert provider.datapack_root(tmp_path).as_posix().endswith(expected)


@pytest.mark.parametrize("provider, expected", [
    (PaxiProvider(), "config/paxi/resourcepacks"),
    (OpenLoaderProvider(), "config/openloader/resources"),
    (GlobalPacksProvider(), "global_packs/required_resources"),
])
def test_resourcepack_roots_match_the_installed_mods(tmp_path, provider, expected):
    assert provider.resourcepack_root(tmp_path).as_posix().endswith(expected)


def test_a_missing_directory_does_not_mean_the_loader_is_gone(tmp_path):
    """Paxi creates `datapacks/` lazily — a fresh install that has only reached the main
    menu has the resourcepack folder and not the datapack one. Treating absence as
    unavailability would disable overrides on a perfectly good install."""
    paxi = PaxiProvider()
    assert not paxi.datapack_root(tmp_path).exists()
    assert DATAPACKS_WRITE in paxi.available_capabilities(tmp_path)
    assert paxi.packs(tmp_path, "datapacks") == []


# --- what each one can and cannot do -----------------------------------------------------

def test_only_paxi_orders_packs(tmp_path):
    """The distinction the capability split exists for. An action needing ordering declares
    it and is refused elsewhere, instead of silently getting the wrong load order."""
    assert DATAPACKS_ORDERING in PaxiProvider().available_capabilities(tmp_path)
    for other in (OpenLoaderProvider(), MoonlightProvider(), GlobalPacksProvider()):
        assert DATAPACKS_ORDERING not in other.available_capabilities(tmp_path), other.name
        assert RESOURCEPACKS_ORDERING not in other.available_capabilities(tmp_path)


def test_moonlight_has_no_resource_packs_at_all(tmp_path):
    """Not disabled — absent. §7.1 predicted exactly this shape before anyone checked."""
    moonlight = MoonlightProvider()
    assert RESOURCEPACKS_WRITE not in moonlight.available_capabilities(tmp_path)
    with pytest.raises(CapabilityError, match="does not load global resource packs"):
        moonlight.resourcepack_root(tmp_path)


def test_open_loader_can_have_one_kind_switched_off(tmp_path):
    """`advanced_options.json` toggles data and resource packs independently, so "installed"
    is not the same fact as "available" — and the granularity is per capability, which is
    what §7.1 argues for."""
    options = tmp_path / "config" / "openloader"
    options.mkdir(parents=True)
    (options / "advanced_options.json").write_text(json.dumps({
        "dataPacks": {"enabled": True}, "resourcePacks": {"enabled": False}}),
        encoding="utf-8")

    offered = OpenLoaderProvider().available_capabilities(tmp_path)
    assert DATAPACKS_WRITE in offered
    assert RESOURCEPACKS_WRITE not in offered


def test_open_loader_with_no_config_yet_assumes_its_defaults(tmp_path):
    """The file is written on first launch. Reporting the capability missing because the
    game hasn't run yet would be its own wrong answer."""
    assert DATAPACKS_WRITE in OpenLoaderProvider().available_capabilities(tmp_path)


# --- moonlight's configurable folder ------------------------------------------------------

def write_moonlight(root, folder):
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "moonlight-common.toml").write_text(
        f'[general]\nglobal_datapacks_folder = "{folder}"\n', encoding="utf-8")


def test_moonlights_folder_is_read_from_its_config(tmp_path):
    """A pack dev who renamed it is not misconfigured, and a hardcoded path would write
    somewhere nothing reads."""
    write_moonlight(tmp_path, "my-own-packs")
    assert MoonlightProvider().datapack_root(tmp_path).name == "my-own-packs"


def test_moonlight_defaults_when_it_has_never_run(tmp_path):
    assert MoonlightProvider().datapack_root(tmp_path).name == "moonlight-global-datapacks"


def test_an_empty_folder_setting_disables_moonlight(tmp_path):
    """Its documented off switch. Offering the capability anyway would write files the
    game has been told to ignore."""
    write_moonlight(tmp_path, "")
    moonlight = MoonlightProvider()
    assert moonlight.available_capabilities(tmp_path) == ()
    with pytest.raises(CapabilityError, match="disabled"):
        moonlight.datapack_root(tmp_path)


# --- global packs is driven by a list -----------------------------------------------------

def write_globalpacks(root, datapack_entries, resource_entries=("global_packs/required_resources/",)):
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "global_packs.toml").write_text(
        "[resourcepacks]\n"
        f"required = {json.dumps(list(resource_entries))}\n"
        "[datapacks]\n"
        f"required = {json.dumps(list(datapack_entries))}\n", encoding="utf-8")


def test_global_packs_reads_the_config_it_generates(tmp_path):
    write_globalpacks(tmp_path, ["datapacks/", "global_packs/required_data/"])
    assert DATAPACKS_WRITE in GlobalPacksProvider().available_capabilities(tmp_path)


def test_global_packs_loses_the_capability_if_its_folder_is_delisted(tmp_path):
    """The folder still exists and is silently ignored — which is precisely why the
    capability comes from the config rather than from the directory being there."""
    write_globalpacks(tmp_path, ["datapacks/"])
    (tmp_path / "global_packs" / "required_data").mkdir(parents=True)

    offered = GlobalPacksProvider().available_capabilities(tmp_path)
    assert DATAPACKS_WRITE not in offered
    assert RESOURCEPACKS_WRITE in offered, "delisting datapacks took resource packs with it"


def test_global_packs_before_first_launch_assumes_the_default_list(tmp_path):
    assert DATAPACKS_WRITE in GlobalPacksProvider().available_capabilities(tmp_path)


# --- writing an override through each ------------------------------------------------------

LOOT = "data/minecraft/loot_tables/blocks/oak_leaves.json"


@pytest.mark.parametrize("provider", [
    PaxiProvider(), OpenLoaderProvider(), MoonlightProvider(), GlobalPacksProvider()])
def test_every_loader_puts_an_override_at_the_same_namespace_path(tmp_path, provider):
    """The whole mechanism: a file at the path the mod uses, in a pack that loads later.
    Whatever the loader's root is, what sits under the pack must be untouched."""
    provider.create_pack(tmp_path, "tweaks")
    target = provider.override_path(tmp_path, "tweaks", LOOT)
    root = provider.datapack_root(tmp_path) / "tweaks"
    assert target.relative_to(root).as_posix() == LOOT


@pytest.mark.parametrize("provider", [
    PaxiProvider(), OpenLoaderProvider(), MoonlightProvider(), GlobalPacksProvider()])
def test_a_created_pack_has_the_mcmeta_minecraft_requires(tmp_path, provider):
    """Without it the folder is silently ignored — files present, nothing happening, which
    is the most confusing failure available."""
    folder = provider.create_pack(tmp_path, "tweaks")
    assert json.loads((folder / "pack.mcmeta").read_text())["pack"]["pack_format"] == 15
    assert provider.packs(tmp_path, "datapacks") == ["tweaks"]


@pytest.mark.parametrize("provider", [
    PaxiProvider(), OpenLoaderProvider(), MoonlightProvider(), GlobalPacksProvider()])
def test_no_loader_lets_an_override_escape_its_pack(tmp_path, provider):
    provider.create_pack(tmp_path, "tweaks")
    with pytest.raises(ValueError, match="escapes"):
        provider.override_path(tmp_path, "tweaks", "../../../../evil.json")


# --- disambiguation, which is now a real situation -------------------------------------------

def test_without_a_preference_the_winner_is_alphabetical_and_that_is_the_problem(tmp_path):
    """Documents the hazard rather than endorsing it. On a pack holding Paxi and Moonlight —
    the user's own — "Global Packs" and "Moonlight" both sort ahead of "Paxi", so overrides
    would silently land somewhere the user never chose."""
    table = resolve(ALL_FOUR, loaders=PACK_LOADERS, instance_root=tmp_path)
    assert table.entries[DATAPACKS_WRITE].provider == "Global Packs"


def test_the_stored_preference_decides_who_wins(tmp_path):
    table = resolve(ALL_FOUR, loaders=PACK_LOADERS, preferred="Paxi", instance_root=tmp_path)
    assert table.entries[DATAPACKS_WRITE].provider == "Paxi"
    assert set(table.entries[DATAPACKS_WRITE].alternatives) == {
        "Global Packs", "Moonlight", "Open Loader"}


def test_a_preference_only_wins_what_it_actually_offers(tmp_path):
    """Preferring Moonlight must not hand it resource packs it cannot do — the capability
    is per name, not per provider."""
    table = resolve(ALL_FOUR, loaders=PACK_LOADERS, preferred="Moonlight",
                    instance_root=tmp_path)
    assert table.entries[DATAPACKS_WRITE].provider == "Moonlight"
    assert table.entries[RESOURCEPACKS_WRITE].provider != "Moonlight"


def test_ordering_resolves_to_paxi_even_when_it_is_not_preferred(tmp_path):
    """It is the only provider of it, so preference cannot take it away."""
    table = resolve(ALL_FOUR, loaders=PACK_LOADERS, preferred="Open Loader",
                    instance_root=tmp_path)
    assert table.entries[DATAPACKS_ORDERING].provider == "Paxi"


def test_a_disabled_loader_drops_out_of_the_table(tmp_path):
    """Installed but switched off is not a provider. Moonlight is the only datapack loader
    here, so turning it off must leave the capability genuinely absent."""
    write_moonlight(tmp_path, "")
    table = resolve(Dump("moonlight"), loaders=PACK_LOADERS, instance_root=tmp_path)
    assert not table.satisfies(DATAPACKS_WRITE)
    assert table.missing([DATAPACKS_READ]) == [DATAPACKS_READ]


def test_without_an_instance_the_answer_falls_back_to_the_static_maximum():
    """Callers holding a packdump but no instance on disk still get a usable table."""
    table = resolve(ALL_FOUR, loaders=PACK_LOADERS)
    assert table.satisfies(DATAPACKS_WRITE) and table.satisfies(DATAPACKS_ORDERING)
