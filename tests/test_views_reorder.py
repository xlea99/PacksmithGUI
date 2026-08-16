"""Drag-to-reorder in the Views panel — design 4.1.

Views are hand-authored, so their order is a **workflow**: the one you open first, then
the one you work, then reference. That is a sequence rather than a hierarchy, which is why
this panel gets reordering where the Registry panel got pinning — 135 machine-generated
names have a long tail and no workflow, and a hand-made list of a dozen has the opposite.

The order is furniture, so it lives in per-profile UI state rather than as a column on the
views table. It therefore has to survive the thing it describes changing underneath it.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QAbstractItemView

from packsmith.gui.shell.panels.views_panel import ViewsPanel


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class View:
    def __init__(self, id, name):
        self.id, self.name = id, name


VIEWS = [View(1, "All Items"), View(2, "Removal Queue"), View(3, "Tiered")]


@pytest.fixture
def panel(qapp):
    p = ViewsPanel([])
    yield p
    p.deleteLater()


def shown(panel):
    """Top-level entries by name — a group by its own name, a view by the view's."""
    out = []
    for i in range(panel._tree.topLevelItemCount()):
        out.append(panel._tree.topLevelItem(i).text(0))
    return out


# --- the stored order ------------------------------------------------------------------

def test_views_appear_in_the_stored_order(panel):
    panel.set_views(VIEWS, layout=[{"view": 3}, {"view": 1}, {"view": 2}])
    assert shown(panel) == ["Tiered", "All Items", "Removal Queue"]


def test_a_view_the_order_does_not_mention_goes_last(panel):
    """Where a view you just made belongs — not silently first, and not dropped."""
    panel.set_views(VIEWS + [View(9, "Brand New")],
                layout=[{"view": 3}, {"view": 1}, {"view": 2}])
    assert shown(panel)[-1] == "Brand New"


def test_an_order_naming_a_deleted_view_is_ignored(panel):
    """The order outlives the views it describes; a stale id is ordinary, not an error."""
    panel.set_views(VIEWS, layout=[{"view": 99}, {"view": 3}, {"view": 1},
                              {"view": 2}])
    assert shown(panel) == ["Tiered", "All Items", "Removal Queue"]


def test_no_stored_order_keeps_the_store_order(panel):
    panel.set_views(VIEWS)
    assert shown(panel) == ["All Items", "Removal Queue", "Tiered"]


# --- dragging --------------------------------------------------------------------------

def test_a_move_republishes_the_whole_order(panel):
    """Read back off the widget rather than derived from drop indices — re-deriving it
    from source/destination rows is the classic off-by-one that only shows when you drag
    downward."""
    panel.set_views(VIEWS)
    seen = []
    panel.layout_changed.connect(seen.append)

    item = panel._tree.takeTopLevelItem(0)   # what an internal move does
    panel._tree.insertTopLevelItem(2, item)
    panel._on_dropped()

    assert seen[-1] == [{"view": 2}, {"view": 3}, {"view": 1}]
    assert shown(panel) == ["Removal Queue", "Tiered", "All Items"]


def test_the_new_order_survives_a_refresh(panel):
    """A rename or an edit re-runs `refresh`; the order must not snap back."""
    panel.set_views(VIEWS)
    item = panel._tree.takeTopLevelItem(0)
    panel._tree.insertTopLevelItem(2, item)
    panel._on_dropped()

    panel.refresh()
    assert shown(panel) == ["Removal Queue", "Tiered", "All Items"]


# --- dragging while filtered -----------------------------------------------------------

def test_dragging_is_off_while_the_search_box_has_text(panel):
    """You would be rearranging a subset, and the drop position says nothing about where
    the item sits in the whole list."""
    panel.set_views(VIEWS)
    assert panel._tree.dragDropMode() == QAbstractItemView.InternalMove

    panel._search.setText("rem")
    assert panel._tree.dragDropMode() == QAbstractItemView.NoDragDrop
    assert shown(panel) == ["Removal Queue"]


def test_dragging_comes_back_when_the_filter_is_cleared(panel):
    panel.set_views(VIEWS)
    panel._search.setText("rem")
    panel._search.setText("")
    assert panel._tree.dragDropMode() == QAbstractItemView.InternalMove
    assert len(shown(panel)) == 3


# --- groups ------------------------------------------------------------------------------
#
# A removal workflow is one working view plus three saturated filters of it: four entries
# that are ONE thing, which a flat list renders as four unrelated ones.

GROUPED = [{"group": "Removal", "views": [2, 3]}, {"view": 1}]


def children(panel, top_index):
    item = panel._tree.topLevelItem(top_index)
    return [item.child(i).text(0) for i in range(item.childCount())]


def test_a_group_holds_its_views(panel):
    panel.set_views(VIEWS, layout=GROUPED)
    assert shown(panel) == ["Removal", "All Items"]
    assert children(panel, 0) == ["Removal Queue", "Tiered"]


def test_the_arrangement_round_trips(panel):
    panel.set_views(VIEWS, layout=GROUPED)
    assert panel.current_layout() == GROUPED


def test_a_new_view_lands_at_the_top_level_not_in_a_group(panel):
    """Filing is a decision. A view quietly appearing inside a group is a view you will
    not find where you left it."""
    panel.set_views(VIEWS + [View(9, "Brand New")], layout=GROUPED)
    assert shown(panel)[-1] == "Brand New"


def test_an_empty_group_survives(panel):
    """The user named it and may still be filing into it — only the user removes it."""
    panel.set_views(VIEWS, layout=[{"group": "Later", "views": []}, {"view": 1}])
    assert shown(panel)[0] == "Later"
    assert panel.current_layout()[0] == {"group": "Later", "views": []}


def test_deleting_a_group_returns_its_views_rather_than_eating_them(panel):
    """The worst possible surprise in a panel whose whole point is that closing is not
    deleting."""
    panel.set_views(VIEWS, layout=GROUPED)
    panel._delete_group("Removal")
    assert set(shown(panel)) == {"All Items", "Removal Queue", "Tiered"}
    assert {v.id for v in VIEWS} == {e["view"] for e in panel.current_layout()}


def test_moving_a_view_into_a_group_takes_it_out_of_where_it_was(panel):
    panel.set_views(VIEWS, layout=GROUPED)
    panel._move_to_group(VIEWS[0], "Removal")            # All Items -> Removal
    assert panel.current_layout() == [{"group": "Removal", "views": [2, 3, 1]}]


def test_moving_a_view_out_of_a_group_puts_it_at_the_top_level(panel):
    panel.set_views(VIEWS, layout=GROUPED)
    panel._move_to_group(VIEWS[1], None)                 # Removal Queue -> top level
    assert panel.current_layout() == [
        {"group": "Removal", "views": [3]}, {"view": 1}, {"view": 2}]


def test_a_view_cannot_end_up_in_two_groups(panel):
    panel.set_views(VIEWS, layout=[{"group": "A", "views": [1]},
                                   {"group": "B", "views": [2, 3]}])
    panel._move_to_group(VIEWS[0], "B")
    everywhere = [vid for e in panel.current_layout() for vid in e.get("views", [])]
    assert everywhere.count(1) == 1


def test_a_group_dropped_into_a_group_is_pulled_back_out(panel):
    """Enforced after the drop rather than by refusing it mid-drag, because Qt's drop
    validation runs on every mouse move and getting it wrong makes the list feel sticky."""
    panel.set_views(VIEWS, layout=[{"group": "A", "views": [1]},
                                   {"group": "B", "views": [2]}])
    outer = panel._tree.topLevelItem(0)
    inner = panel._tree.takeTopLevelItem(1)
    outer.addChild(inner)

    panel._tree._flatten_groups()

    # "Tiered" is unmentioned by that layout, so it sits at the top level too — what
    # matters is that neither group is inside the other any more.
    assert "B" in shown(panel)
    assert children(panel, 0) == ["All Items"], "B is still nested inside A"


def test_filtering_hides_a_group_with_no_matches(panel):
    panel.set_views(VIEWS, layout=GROUPED)
    panel._search.setText("all")
    assert shown(panel) == ["All Items"]


def test_filtering_keeps_a_group_that_still_has_matches(panel):
    panel.set_views(VIEWS, layout=GROUPED)
    panel._search.setText("tier")
    assert shown(panel) == ["Removal"]
    assert children(panel, 0) == ["Tiered"]


# --- the refinement, kept per view -------------------------------------------------------
#
# §3.2.3 calls the bar transient, and it still is in the sense that matters: ANDed on top,
# clearable, never part of what the View IS — "Keep" remains the deliberate act that folds
# it in. What it stops being is transient across closing a tab, which was only ever an
# accident of where it happened to live.

def test_setting_text_applies_it_without_waiting_for_the_debounce(qapp):
    """Restoring a refinement is not typing. Falling through the debounce would apply the
    same filter twice — once now and once when the timer fires."""
    from packsmith.gui.query_bar import QueryBar

    bar = QueryBar()
    applied = []
    bar.filter_changed.connect(applied.append)
    try:
        bar.set_text('mod == "quark"')
        assert bar.text() == 'mod == "quark"'
        assert len(applied) == 1
        assert not bar._timer.isActive(), "the debounce was armed by a restore"
    finally:
        bar.deleteLater()


def test_an_empty_restore_is_a_cleared_bar(qapp):
    from packsmith.gui.query_bar import QueryBar

    bar = QueryBar()
    try:
        bar.set_text("")
        assert bar.text() == ""
    finally:
        bar.deleteLater()
