"""Hardcoded demo Views for the dev build — each is a (title, query) pair the shell
renders as a tab. This is the proto of the real Views system: a View *is* a saved query,
rendered. Here the queries are authored in code; later they'll be user-built and persisted.

Everything is registry-scoped to minecraft:item (the only registry the dev build browses)
and leans on the tags `_seed_tags` guarantees: remove, tier, weight, tooltip, banned.
"""
from packsmith.core.query import (
    Query, Registry, Id, Mod, Tag, Attribute, Cmp, Has,
)

ITEM = "minecraft:item"


def demo_views() -> list[tuple[str, Query]]:
    """The tabs the dev shell shows, in order."""
    all_item_tags = [
        Tag("remove"), Tag("tier"), Tag("weight"), Tag("tooltip"), Tag("banned"),
    ]
    return [
        # The main browser — every item, every tag column. Editable. This is today's
        # table, now proven to be "just a query, rendered."
        ("All Items", Query(
            scope=Registry(ITEM),
            select=[Id, Attribute("localization"), *all_item_tags],
            order_by=[Id],
        )),

        # A filtered View: only what's queued for removal.
        ("Removal Queue", Query(
            scope=Registry(ITEM),
            filter=Cmp(Tag("remove"), "eq", True),
            select=[Id, Attribute("localization"), Tag("remove")],
            order_by=[Id],
        )),

        # Another filter: everything that's been assigned a tier.
        ("Tiered", Query(
            scope=Registry(ITEM),
            filter=Has(Tag("tier")),
            select=[Id, Attribute("localization"), Tag("tier")],
            order_by=[Attribute("localization")],
        )),

        # A computed View: the distinct set of mods. Read-only rows (no entry behind
        # them) — proves non-editable rendering.
        ("Mods", Query(
            scope=Registry(ITEM),
            select=[Mod],
            distinct=True,
            order_by=[Mod],
        )),
    ]
