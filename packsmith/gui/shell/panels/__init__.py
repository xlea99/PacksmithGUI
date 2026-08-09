"""The sidebar panel roster (design 4.1).

One entry per navigation panel, in sidebar order. These are **navigation** panels — how
you find and open things; clicking an item typically opens a tab in the workspace. The
descriptions are lifted from §4.1 so the stubs double as in-app documentation of what
each panel is going to be.

``PANEL_SPECS`` is the single source the sidebar builds from: adding a panel is adding a
row here (plus, eventually, a real widget in place of the stub).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PanelSpec:
    key: str
    letter: str      # the icon-strip badge (no icon assets yet — letters per §4.1's V/B/T/F/R/J/A)
    title: str
    description: str


PANEL_SPECS = [
    PanelSpec(
        "views", "V", "Views",
        "Searchable list of saved view configurations. Double-click opens the View in "
        "its renderer — usually a registry table. Ships with sensible defaults.",
    ),
    PanelSpec(
        "blueprints", "B", "Blueprints",
        "Browse blueprint schemas and instances as a tree. Click a schema to see all "
        "instances; click an instance to open an editor tab.",
    ),
    PanelSpec(
        "tags", "T", "Tags",
        "All declared tags. Click to edit a definition. Quick-action to spawn a minimal "
        "view scoped to one tag.",
    ),
    PanelSpec(
        "files", "F", "Files",
        "The semantic file browser — smart/honest modes, loader-aware path resolution "
        "through integrations.",
    ),
    PanelSpec(
        "registry", "R", "Registry",
        "Categorical registry-type list. Clicking a type opens a zero-tag table scoped "
        "to it — the fastest way to just look at everything.",
    ),
    PanelSpec(
        "jobs", "J", "Jobs",
        "The primary automation surface. Pinned jobs with one-click play buttons, all "
        "jobs listed below, searchable.",
    ),
    PanelSpec(
        "actions", "A", "Actions",
        "Reference panel for installed actions, organized by package. Inspect manifests "
        "and launch standalone runs.",
    ),
]
