"""The sidebar panel roster (design 4.1).

One entry per navigation panel, in sidebar order. These are **navigation** panels — how
you find and open things; clicking an item typically opens a tab in the workspace. The
descriptions are lifted from §4.1 so the stubs double as in-app documentation of what
each panel is going to be.

``PANEL_SPECS`` is the single source the sidebar builds from: adding a panel is adding a
row here (plus, eventually, a real widget in place of the stub).

**The order is two groups, separated by a rule**, because the panels answer two different
questions. The first three are *where you work* — the views you read, the automation you
run, the files you edit. The second three are *the vocabulary those are expressed in* — the
tags and blueprints you define, and the registry they describe. Sitting a divider between
them means the strip can be aimed at by half before it is read at all.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PanelSpec:
    key: str
    letter: str      # fallback badge, used only if the icon font fails to load (see icons.py)
    title: str
    description: str
    group: int = 1   # a change of group draws a separator in the strip


PANEL_SPECS = [
    # --- where you work ---------------------------------------------------------------
    PanelSpec(
        "views", "V", "Views",
        "Searchable list of saved view configurations. Double-click opens the View in "
        "its renderer — usually a registry table. Ships with sensible defaults.",
    ),
    PanelSpec(
        "automation", "A", "Automation",
        "Jobs and Actions in one place. Jobs are what you run — pinned ones reach the "
        "header's run control; Actions are the installed packages they are composed from. "
        "Two tabs because they are different shapes, not because they are unrelated.",
    ),
    PanelSpec(
        "files", "F", "Files",
        "The semantic file browser — smart/honest modes, loader-aware path resolution "
        "through integrations.",
    ),

    # --- the vocabulary it is expressed in ----------------------------------------------
    PanelSpec(
        "tags", "T", "Tags",
        "All declared tags. Click to edit a definition. Quick-action to spawn a minimal "
        "view scoped to one tag.",
        group=2,
    ),
    PanelSpec(
        "blueprints", "B", "Blueprints",
        "Browse blueprint schemas and instances as a tree. Click a schema to see all "
        "instances; click an instance to open an editor tab.",
        group=2,
    ),
    PanelSpec(
        "registry", "R", "Registry",
        "Categorical registry-type list. Clicking a type opens a zero-tag table scoped "
        "to it — the fastest way to just look at everything.",
        group=2,
    ),
]
