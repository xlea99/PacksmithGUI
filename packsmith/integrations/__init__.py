"""Mod-aware integrations — design 8.

Each integration lives in its own module and is reached only through this registry: base
code never imports a specific loader. That boundary is the one a plugin system would need
later (§3.4), so adding real discovery becomes additive rather than a rewrite — while
nothing here loads code dynamically, because a *public* API frozen before several
providers exist would be shaped by whichever one was written first.

Adding a loader is adding a module and one line here.
"""
from packsmith.integrations.paxi import PaxiProvider

PACK_LOADERS = (PaxiProvider(),)


def loaders_for(packdump):
    """The loaders actually installed in this pack (§8.1: scan the packdump's mod list)."""
    return [loader for loader in PACK_LOADERS if loader.detect(packdump)]
