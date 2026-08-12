"""Saved Views: persisting a query as a named, reopenable artifact (design 3.2.3).

The load-bearing property is that a stored View's query comes back as the *same query* —
that's what makes "a View is a saved query" true across restarts, forks, and exports.
"""
import pytest

from packsmith.core.views import ViewStore, DEFAULT_RENDERER
from packsmith.core.query import (
    Query, Registry, Id, Mod, Tag, Attribute, Cmp, Has, And, Or, Not, evaluate,
)

REG = "minecraft:item"


@pytest.fixture
def views(user_db):
    return ViewStore(user_db)


def _simple_query():
    return Query(scope=Registry(REG), filter=Cmp(Tag("remove"), "eq", True), select=[Id])


def _hairy_query():
    """Exercises every node kind, so the round-trip test means something."""
    return Query(
        scope=Registry(REG),
        filter=And([
            Has(Tag("tier")),
            Or([
                Cmp(Id, "matches", r".*_ore$"),
                Cmp(Attribute("localization"), "contains", "ORE", ci=True),
                Not(Cmp(Mod, "in", ["minecraft"])),
            ]),
        ]),
        select=[Id, Mod, Tag("tier"), Attribute("localization")],
        order_by=[Attribute("localization")],
        limit=25,
        distinct=True,
    )


# --- create / read ----------------------------------------------------------

def test_create_and_read_back(views):
    created = views.create("Removal Queue", _simple_query())
    assert created.id
    fetched = views.get(created.id)
    assert fetched.name == "Removal Queue"
    assert fetched.renderer == DEFAULT_RENDERER


def test_query_survives_the_round_trip(views):
    """The whole point: what comes out of the database IS the query that went in."""
    original = _hairy_query()
    view = views.create("Everything", original)
    assert views.get(view.id).query == original


def test_stored_query_still_runs(views, tags):
    """A rehydrated query is a real query — it evaluates like any other."""
    class FakeDump:
        registry = {REG: {"values": ["quark:rope", "minecraft:diamond"]}}
        def attribute(self, rt, eid, name): return None

    tags.define(REG, "remove", "bool", default=False)
    tags.assign(REG, "quark:rope", "remove", True, owner="user")
    view = views.create("Removal Queue", _simple_query())

    result = evaluate(views.get(view.id).query, packdump=FakeDump(), tag_store=tags)
    assert [r.values["id"] for r in result.rows] == ["quark:rope"]


def test_all_returns_display_order(views):
    a = views.create("Alpha", _simple_query())
    b = views.create("Beta", _simple_query())
    c = views.create("Gamma", _simple_query())
    assert [v.name for v in views.all()] == ["Alpha", "Beta", "Gamma"]
    views.reorder([c.id, a.id, b.id])
    assert [v.name for v in views.all()] == ["Gamma", "Alpha", "Beta"]


def test_get_by_name(views):
    views.create("Tiered", _simple_query())
    assert views.get_by_name("Tiered") is not None
    assert views.get_by_name("nope") is None


# --- update -----------------------------------------------------------------

def test_update_query_persists_a_constructor_edit(views):
    view = views.create("All Items", Query(scope=Registry(REG), select=[Id]))
    edited = Query(scope=Registry(REG), filter=Cmp(Mod, "eq", "quark"), select=[Id])
    views.update_query(view.id, edited)
    assert views.get(view.id).query == edited


def test_rename_keeps_identity_and_query(views):
    view = views.create("Old Name", _hairy_query())
    views.rename(view.id, "New Name")
    again = views.get(view.id)
    assert again.id == view.id            # identity survives the rename
    assert again.name == "New Name"
    assert again.query == _hairy_query()


def test_renderer_config_round_trips(views):
    view = views.create("Widths", _simple_query(), renderer_config={"col_widths": [300, 120]})
    assert views.get(view.id).renderer_config == {"col_widths": [300, 120]}
    views.set_renderer_config(view.id, {"col_widths": [200]})
    assert views.get(view.id).renderer_config == {"col_widths": [200]}


def test_view_with_no_renderer_config_reads_as_empty(views):
    view = views.create("Bare", _simple_query())
    assert views.get(view.id).renderer_config == {}


# --- delete + validation ----------------------------------------------------

def test_delete_removes_only_that_view(views):
    keep = views.create("Keep", _simple_query())
    drop = views.create("Drop", _simple_query())
    views.delete(drop.id)
    assert views.get(drop.id) is None
    assert views.get(keep.id) is not None
    assert views.count == 1


def test_duplicate_name_raises(views):
    views.create("Only One", _simple_query())
    with pytest.raises(ValueError):
        views.create("Only One", _simple_query())


def test_rename_onto_existing_name_raises(views):
    views.create("First", _simple_query())
    second = views.create("Second", _simple_query())
    with pytest.raises(ValueError):
        views.rename(second.id, "First")


def test_blank_name_raises(views):
    with pytest.raises(ValueError):
        views.create("   ", _simple_query())


def test_one_unreadable_view_does_not_take_the_others_with_it(views):
    """A saved query is data that outlives the code that wrote it — a row from a newer
    Packsmith or a hand-edited profile is a real possibility. Losing every healthy view to
    one broken row is a far worse failure than the broken row."""
    import json
    views.create("first", Query(scope=Registry("minecraft:item"), select=[Id]))
    doomed = views.create("doomed", Query(scope=Registry("minecraft:item"), select=[Id]))
    views.create("last", Query(scope=Registry("minecraft:block"), select=[Id]))

    views._db.execute("UPDATE views SET query_json = ? WHERE id = ?",
                      (json.dumps({"node": "FromTheFuture"}), doomed.id))
    assert [v.name for v in views.all()] == ["first", "last"]
    assert [(name, reason.split(":")[0]) for _id, name, reason in views.unreadable()] == [
        ("doomed", "QueryError")]


def test_an_aggregate_view_survives_a_save_and_reload(views):
    """Q-1: this bricked the whole panel on next launch."""
    from packsmith.core.query.ast import Attribute, Cmp, Count
    query = Query(scope=Registry("minecraft:item"),
                  select=[Attribute("localization"), Count],
                  group_by=[Attribute("localization")],
                  having=Cmp(Count, "gt", 1))
    views.create("duplicate names", query)
    reloaded = [v for v in views.all() if v.name == "duplicate names"][0]
    assert reloaded.query == query
    assert views.unreadable() == []


# --- the rollback menu gate (design 3.3) ------------------------------------

@pytest.mark.parametrize("status, data, offered", [
    ("success",     {"files": {"a.json": {}}, "l2": [], "blueprints": []}, True),
    ("failed",      {"files": {"a.json": {}}, "l2": [], "blueprints": []}, True),
    ("failed",      {"files": {}, "l2": [], "blueprints": []},             False),
    ("success",     {"files": {}, "l2": [{"key": ["r", "e", "t"]}], "blueprints": []}, True),
    ("rolled_back", {"files": {"a.json": {}}, "l2": [], "blueprints": []}, False),
])
def test_rollback_is_offered_when_there_is_something_to_undo(status, data, offered):
    """Not when the step "succeeded". A commit can fail having already written files —
    the runner records how to reverse them and its reason tells the user to — and gating
    on status made that instruction impossible to follow."""
    import json
    from packsmith.gui.shell.bottom_views import _has_rollback
    step = {"status": status, "rollback_data": json.dumps(data)}
    assert _has_rollback(step) is offered


def test_a_missing_or_unreadable_rollback_record_offers_nothing():
    from packsmith.gui.shell.bottom_views import _has_rollback
    assert _has_rollback({"status": "failed", "rollback_data": None}) is False
    assert _has_rollback({"status": "success", "rollback_data": "{not json"}) is False


def test_unreadable_views_are_reported_not_just_skipped(views):
    """Skipping a broken row keeps the panel alive (Q-1); reporting it is what stops the
    view merely *vanishing*."""
    import json
    views.create("fine", Query(scope=Registry("minecraft:item"), select=[Id]))
    doomed = views.create("doomed", Query(scope=Registry("minecraft:item"), select=[Id]))
    views._db.execute("UPDATE views SET query_json = ? WHERE id = ?",
                      (json.dumps({"node": "Nope"}), doomed.id))
    assert [v.name for v in views.all()] == ["fine"]
    reported = views.unreadable()
    assert [name for _id, name, _why in reported] == ["doomed"]
    assert reported[0][2], "the reason must be carried, not just the fact"
