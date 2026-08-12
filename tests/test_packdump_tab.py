"""The Packdump review surfaces — design 3.1 / 4.1.

Two surfaces, one answer. §4.1 puts packdump review in the bottom panel, and the summary
belongs there — but a real update to a 300-mod pack moves thousands of entries, and a strip
a few rows tall is a place to learn *that* something changed, not to read *what*. So the
strip summarises and launches; the diff is a first-class tab.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.packdiff import DiffSummary, ModChange, RegistryChange, summarise_diff
from packsmith.gui.packdump_diff import PackdumpDiffTab
from packsmith.gui.shell.bottom_views import PackdumpView


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    yield app


class FakeOrphan:
    def __init__(self, entry_id, tag_name="remove", value=True):
        self.entry_id, self.tag_name, self.value = entry_id, tag_name, value
        self.reason = "missing_entry"


def summary_with(**kwargs):
    summary = DiffSummary()
    for key, value in kwargs.items():
        setattr(summary, key, value)
    return summary


def groups(tab):
    return [tab._tree.topLevelItem(i) for i in range(tab._tree.topLevelItemCount())]


def group_named(tab, fragment):
    return next((g for g in groups(tab) if fragment in g.text(0)), None)


# --- the diff tab -------------------------------------------------------------------------

def test_added_and_removed_entries_are_labelled_as_such(qapp):
    tab = PackdumpDiffTab(summary_with(registries=[
        RegistryChange("minecraft:item", added=("mod:new",), removed=("mod:gone",))]))
    group = group_named(tab, "minecraft:item")
    rows = {group.child(i).text(0): group.child(i).text(1) for i in range(group.childCount())}
    assert rows == {"mod:new": "added", "mod:gone": "removed"}


def test_the_header_leads_with_the_headline(qapp):
    tab = PackdumpDiffTab(summary_with(registries=[
        RegistryChange("minecraft:item", added=tuple(f"m:{i}" for i in range(214)))]))
    assert "+214 entries" in tab._headline()


def test_an_identical_dump_says_so_plainly(qapp):
    tab = PackdumpDiffTab(DiffSummary())
    assert "identical" in tab._headline()
    assert groups(tab) == []


def test_tags_at_risk_are_surfaced_first_and_in_red(qapp):
    """§4.1 asks the tab to "surface orphaned tags at risk". They come before the entry
    lists because they are the only part that costs the user something."""
    tab = PackdumpDiffTab(summary_with(registries=[
        RegistryChange("minecraft:item", removed=("mod:gone",))]),
        at_risk=[FakeOrphan("mod:gone")])
    assert "1 tag assignment" in tab._headline()
    assert "at risk" in groups(tab)[0].text(0)


def test_an_identity_change_outranks_everything(qapp):
    """A loader or MC version change is not one row among thousands — it decides whether
    this is even the same pack (§3.1)."""
    tab = PackdumpDiffTab(summary_with(
        identity={"loader_version": ("47.4.10", "47.9.9")},
        registries=[RegistryChange("minecraft:item", added=("a:b",))]))
    assert "loader version changed" in groups(tab)[0].text(0)
    assert groups(tab)[0].text(1) == "47.4.10 → 47.9.9"


def test_mods_show_their_version_move(qapp):
    tab = PackdumpDiffTab(summary_with(mods=[
        ModChange("create", "updated", "0.5.1", "6.0.8")]))
    group = group_named(tab, "Mods")
    assert group.child(0).text(1) == "0.5.1 → 6.0.8"


def test_a_whole_registry_appearing_gets_its_own_line(qapp):
    tab = PackdumpDiffTab(summary_with(registries_added=("minecraft:enchantment",)))
    assert group_named(tab, "Registries added") is not None


def test_filtering_narrows_the_entries(qapp):
    tab = PackdumpDiffTab(summary_with(registries=[
        RegistryChange("minecraft:item",
                       added=("mod:polished_granite", "mod:oak_planks"))]))
    tab._filter.setText("granite")
    group = group_named(tab, "minecraft:item")
    assert [group.child(i).text(0) for i in range(group.childCount())] == \
        ["mod:polished_granite"]


def test_filtering_drops_registries_with_no_match(qapp):
    tab = PackdumpDiffTab(summary_with(registries=[
        RegistryChange("minecraft:item", added=("mod:granite",)),
        RegistryChange("minecraft:block", added=("mod:oak",))]))
    tab._filter.setText("granite")
    assert group_named(tab, "minecraft:block") is None


def test_a_large_update_is_not_slow(qapp):
    """2,500 added entries is an ordinary mod addition on a real pack."""
    import time
    summary = summary_with(registries=[
        RegistryChange("minecraft:item", added=tuple(f"mod:item_{i}" for i in range(2500)))])
    start = time.time()
    tab = PackdumpDiffTab(summary)
    assert time.time() - start < 2.0
    assert group_named(tab, "minecraft:item").childCount() == 2500


# --- the bottom strip -----------------------------------------------------------------------

def test_the_strip_summarises_without_listing(qapp):
    view = PackdumpView()
    view.show_state(summary=summary_with(registries=[
        RegistryChange("minecraft:item", added=("a:b", "a:c"))]))
    assert "+2 entries" in view._headline.text()
    assert view._open_button.isEnabled()


def test_the_strip_says_when_nothing_changed(qapp):
    view = PackdumpView()
    view.show_state(summary=DiffSummary())
    assert "matches the active snapshot" in view._headline.text()
    assert not view._open_button.isEnabled(), "nothing to open"


def test_bless_is_hidden_unless_something_is_waiting(qapp):
    """With auto-adopt on — the default — there is never anything to bless, and a
    permanently disabled button is a standing invitation to wonder what it does."""
    view = PackdumpView()
    view.show_state(summary=DiffSummary(), pending=False)
    assert view._bless_button.isHidden()

    view.show_state(summary=summary_with(mods=[ModChange("x", "added")]), pending=True)
    assert not view._bless_button.isHidden()
    assert "Nothing has changed until you bless it" in view._headline.text()


def test_the_strip_warns_about_orphaned_assignments(qapp):
    view = PackdumpView()
    view.show_state(summary=summary_with(registries=[
        RegistryChange("minecraft:item", removed=("a:b",))]),
        at_risk=[FakeOrphan("a:b"), FakeOrphan("a:c")])
    assert "2 tag assignments orphaned" in view._headline.text()


def test_snapshots_are_listed_with_the_active_one_marked(qapp):
    view = PackdumpView()
    view.show_state(summary=DiffSummary(), active="2026-08-09_19-14-06", snapshots=[
        {"name": "2026-08-09_19-14-06", "timestamp": "2026-08-09T19:14:06", "mod_count": 304},
        {"name": "2026-01-01_00-00-00", "timestamp": "2026-01-01T00:00:00", "mod_count": 280},
    ])
    assert view._tree.topLevelItemCount() == 2
    assert view._tree.topLevelItem(0).text(2) == "304"
    assert view._tree.topLevelItem(0).toolTip(0) == "the active snapshot"


def test_reverting_offers_the_snapshot_by_name(qapp):
    view = PackdumpView()
    view.show_state(summary=DiffSummary(), snapshots=[
        {"name": "2026-01-01_00-00-00", "timestamp": "2026-01-01T00:00:00", "mod_count": 1}])
    asked = []
    view.revert_requested.connect(asked.append)

    menu = view._menu_for(view._tree.topLevelItem(0))
    revert = next(a for a in menu.actions() if a.text().startswith("Revert"))
    assert revert.text() == "Revert to 2026-01-01_00-00-00"
    revert.trigger()
    assert asked == ["2026-01-01_00-00-00"]


def test_a_snapshot_can_be_inspected_before_it_is_reverted_to(qapp):
    """Looking comes before deciding — reverting to a snapshot you haven't inspected is
    exactly the guess this tab exists to replace, so the menu offers it first."""
    view = PackdumpView()
    view.show_state(summary=DiffSummary(), snapshots=[
        {"name": "2026-01-01_00-00-00", "timestamp": "2026-01-01T00:00:00", "mod_count": 1}])
    asked = []
    view.snapshot_diff_requested.connect(asked.append)

    menu = view._menu_for(view._tree.topLevelItem(0))
    assert menu.actions()[0].text() == "See what changed in this snapshot"
    menu.actions()[0].trigger()
    assert asked == ["2026-01-01_00-00-00"]


def test_a_snapshot_can_also_be_compared_with_the_active_dump(qapp):
    """Two different questions: what that update did, and what has changed since. The
    second is usually why you opened the history at all."""
    view = PackdumpView()
    view.show_state(summary=DiffSummary(), snapshots=[
        {"name": "2026-01-01_00-00-00", "timestamp": "2026-01-01T00:00:00", "mod_count": 1}])
    asked = []
    view.snapshot_vs_active_requested.connect(asked.append)

    menu = view._menu_for(view._tree.topLevelItem(0))
    action = next(a for a in menu.actions() if a.text() == "Compare with the active dump")
    action.trigger()
    assert asked == ["2026-01-01_00-00-00"]


def test_empty_space_offers_nothing_to_revert(qapp):
    view = PackdumpView()
    view.show_state(summary=DiffSummary())
    assert view._menu_for(None) is None
