"""Queries the shell builds on the user's behalf (design 4.1).

These are the "I didn't configure anything, just show me" queries — the ones the sidebar
panels hand you. They're ordinary queries with no special status; the difference between
one of these and a saved View is only that nobody named and kept it.
"""
from packsmith.core.query import (
    Query, Registry, Blueprint, Id, Attribute, Tag, AllSlots,
)


def browse_query(registry_type: str) -> Query:
    """A **zero-tag** table for a registry type (§4.1, Registry panel): just the entries
    and their names. The fastest way to look at everything without configuring a View —
    and visibly distinct from a curated View, which is the point. Add columns or a filter
    through the ⚙ constructor and it becomes one."""
    return Query(
        scope=Registry(registry_type),
        select=[Id, Attribute("localization")],
        order_by=[Id],
    )


def tag_query(registry_type: str, tag_name: str) -> Query:
    """A minimal view scoped to one tag (§4.1, Tags panel quick-action): the entries, their
    names, and that single editable column."""
    return Query(
        scope=Registry(registry_type),
        select=[Id, Attribute("localization"), Tag(tag_name)],
        order_by=[Id],
    )


def blueprint_query(blueprint: str) -> Query:
    """Every instance of a blueprint, with every one of its slots (§4.1, Blueprints panel).

    ``AllSlots`` rather than a frozen list of columns: a blueprint's slots are a fact about
    the blueprint, not a preference of the view. Freeze them and a schema that grows starts
    hiding *gaps* — and gaps are the whole output of the primitive (§3.2.2). A deliberately
    curated view still names its slots explicitly; this is just the default.
    """
    return Query(
        scope=Blueprint(blueprint),
        select=[Id, AllSlots],
        order_by=[Id],
    )
