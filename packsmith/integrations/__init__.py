"""Mod-aware integrations — design 8.

Each integration lives in its own module and is reached only through this registry: base
code never imports a specific loader. That boundary is the one a plugin system would need
later (§3.4), so adding real discovery becomes additive rather than a rewrite — while
nothing here loads code dynamically, because a *public* API frozen before several
providers exist would be shaped by whichever one was written first.

Adding a loader is adding a module and one line here.

**The four here are verified against a real 1.20.1 Forge instance** — installed together,
launched once, and read from the folders and configs they generated for themselves. Two of
the four are documented wrongly upstream (Paxi's page describes a newer `config/_paxi_/`
layout, and Open Loader's own README points at the instance root rather than `config/`), so
the docs were not a usable source. Mod ids come from each jar's `mods.toml`.

**This set is explicitly for Minecraft 1.20.1.** Loaders move their own paths between game
versions — Paxi already has — so a later version needs this re-verified, not assumed. Doing
that properly means per-version layouts; today it means: check before trusting.
"""
from packsmith.integrations.globalpacks import GlobalPacksProvider
from packsmith.integrations.moonlight import MoonlightProvider
from packsmith.integrations.openloader import OpenLoaderProvider
from packsmith.integrations.paxi import PaxiProvider

PACK_LOADERS = (PaxiProvider(), OpenLoaderProvider(), MoonlightProvider(),
                GlobalPacksProvider())


def loaders_for(packdump):
    """The loaders actually installed in this pack (§8.1: scan the packdump's mod list)."""
    return [loader for loader in PACK_LOADERS if loader.detect(packdump)]
