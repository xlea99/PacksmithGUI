"""The Registry panel's namespace grouping — design 4.1.

The panel listed every registry type as one flat row. That is fine on paper and unusable
in practice: the real Deep End pack registers **135** of them, so the panel opened as a
wall of near-identical ids and you scrolled to find anything.

Grouping is by the text before the colon, which is the dumbest possible split and exactly
the right one — that prefix *is* the mod that owns the registry, so there is nothing to
map and nothing to keep up to date.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class Dump:
    """Just enough packdump for the panel: a registry mapping."""

    def __init__(self, registry):
        self.registry = registry


def make(registry, pins=()):
    from packsmith.gui.shell.panels.registry_panel import RegistryPanel
    return RegistryPanel(Dump(registry), pins=pins)


def entries(*names):
    return {"values": list(names)}


REGISTRY = {
    "minecraft:item": entries("a", "b", "c"),
    "minecraft:block": entries("d", "e"),
    "forge:biome_modifier": entries("f"),
    "alexscaves:gas": entries("g", "h"),
}


@pytest.fixture
def panel(qapp):
    """No pins, so the grouping tests measure grouping and nothing else."""
    p = make(REGISTRY)
    yield p
    p.deleteLater()


def rows(panel):
    tree = panel._tree
    return [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]


def labels(panel):
    return [item.text(0) for item in rows(panel)]


def test_the_top_level_is_namespaces_not_registries(panel):
    assert labels(panel) == ["alexscaves", "forge", "minecraft"]


def test_a_namespace_holds_its_own_registries(panel):
    minecraft = rows(panel)[2]
    assert [minecraft.child(i).text(0) for i in range(minecraft.childCount())] \
        == ["block", "item"]


def test_children_drop_the_namespace_from_their_label(panel):
    """It is already the row above them; repeating it in every child is what made the flat
    list hard to scan."""
    minecraft = rows(panel)[2]
    assert minecraft.child(0).text(0) == "block"


def test_a_child_still_opens_the_full_registry_type(panel):
    """The label is shortened, the identity is not. Emitting "block" instead of
    "minecraft:block" would open the wrong thing — or nothing."""
    fired = []
    panel.registry_activated.connect(fired.append)

    minecraft = rows(panel)[2]
    panel._on_clicked(minecraft.child(0))

    assert fired == ["minecraft:block"]


def test_a_namespace_totals_its_children(panel):
    assert rows(panel)[2].text(1) == "5"        # 3 items + 2 blocks
    assert rows(panel)[0].text(1) == "2"


def test_clicking_a_namespace_opens_it_rather_than_a_view(panel):
    """A namespace is not a registry, so there is nothing to open — but a row that does
    nothing at all when clicked reads as broken."""
    fired = []
    panel.registry_activated.connect(fired.append)
    namespace = rows(panel)[2]
    assert not namespace.isExpanded()

    panel._on_clicked(namespace)
    assert namespace.isExpanded()
    panel._on_clicked(namespace)
    assert not namespace.isExpanded()

    assert fired == [], "a namespace opened a registry table"


def test_namespaces_start_collapsed(panel):
    """The entire point is not opening on a wall of rows."""
    assert not any(item.isExpanded() for item in rows(panel))


def test_an_open_namespace_survives_a_refresh(panel):
    """`refresh` runs on packdump adopt, and collapsing what you had open is a strange
    thing for an import to do to you."""
    rows(panel)[2].setExpanded(True)
    panel.refresh()

    reopened = {item.text(0) for item in rows(panel) if item.isExpanded()}
    assert reopened == {"minecraft"}


def test_an_id_with_no_namespace_still_gets_a_home(qapp):
    """Vanilla ids are always namespaced, but the panel renders whatever the dump says and
    a bare key must not vanish from the list."""
    panel = make({"weird_no_colon": entries("x")})
    try:
        assert labels(panel) == [panel.NO_NAMESPACE]
        assert panel._tree.topLevelItem(0).child(0).text(0) == "weird_no_colon"
    finally:
        panel.deleteLater()


def test_an_empty_registry_is_an_empty_panel(qapp):
    panel = make({})
    try:
        assert rows(panel) == []
    finally:
        panel.deleteLater()


# --- filtering ---------------------------------------------------------------------------

def test_filtering_does_not_eat_which_namespaces_were_open(panel):
    """The one failure here that hides.

    A filtered tree is force-expanded, so the "which groups were open" snapshot `refresh`
    takes must not be read off it — otherwise typing one character and deleting it records
    *every* namespace as open, and the arrangement is gone with nothing on screen looking
    wrong. It reads as "the panel just opens everything now", days later, with no cause
    attached to it.

    The other filter behaviours (what matches, the empty-state row) announce themselves the
    moment you type; this one cannot.
    """
    rows(panel)[2].setExpanded(True)              # minecraft, by hand

    panel._search.setText("bio")
    assert all(item.isExpanded() for item in rows(panel)), "matches were left hidden"
    panel._search.setText("")

    reopened = {item.text(0) for item in rows(panel) if item.isExpanded()}
    assert reopened == {"minecraft"}, f"the filter rearranged the panel: {reopened}"


# --- pinning --------------------------------------------------------------------------------

@pytest.fixture
def pinned(qapp):
    p = make(REGISTRY, pins=["minecraft:item"])
    yield p
    p.deleteLater()


def section(panel):
    """The Pinned section, or None."""
    return next((r for r in rows(panel) if r.text(0) == panel.PINNED_LABEL), None)


def pinned_types(panel):
    top = section(panel)
    return [] if top is None else [top.child(i).data(0, Qt.UserRole)
                                   for i in range(top.childCount())]


def test_a_pin_sits_above_the_alphabet(pinned):
    assert rows(pinned)[0].text(0) == pinned.PINNED_LABEL
    assert pinned_types(pinned) == ["minecraft:item"]


def test_the_pinned_section_starts_open(pinned):
    """A shortcut list you have to expand to see is not a shortcut."""
    assert section(pinned).isExpanded()


def test_a_pinned_entry_is_labelled_with_its_full_id(pinned):
    """Up here the namespace isn't implied by anything, and "item" alone would be
    ambiguous the moment a second mod has one."""
    assert section(pinned).child(0).text(0) == "minecraft:item"


def test_a_pinned_entry_opens_like_any_other(pinned):
    fired = []
    pinned.registry_activated.connect(fired.append)
    pinned._on_clicked(section(pinned).child(0))
    assert fired == ["minecraft:item"]


def test_pinning_does_not_remove_it_from_its_namespace(pinned):
    """A Quick Access list, not a relocation — a `minecraft` group that mysteriously has
    no `item` in it would be the more confusing of the two."""
    minecraft = next(r for r in rows(pinned) if r.text(0) == "minecraft")
    assert [minecraft.child(i).text(0) for i in range(minecraft.childCount())] \
        == ["block", "item"]


def test_pinning_and_unpinning_round_trips(panel):
    assert section(panel) is None
    panel.pin("forge:biome_modifier")
    assert pinned_types(panel) == ["forge:biome_modifier"]
    panel.unpin("forge:biome_modifier")
    assert section(panel) is None


def test_a_pin_change_is_announced_for_saving(panel):
    """The panel doesn't know what a profile is; whoever does gets told."""
    seen = []
    panel.pins_changed.connect(seen.append)

    panel.pin("minecraft:block")
    panel.pin("forge:biome_modifier")
    panel.unpin("minecraft:block")

    assert seen == [["minecraft:block"],
                    ["minecraft:block", "forge:biome_modifier"],
                    ["forge:biome_modifier"]]


def test_pinning_the_same_thing_twice_is_not_two_pins(panel):
    seen = []
    panel.pins_changed.connect(seen.append)
    panel.pin("minecraft:block")
    panel.pin("minecraft:block")
    assert panel.pins() == ["minecraft:block"]
    assert len(seen) == 1, "a no-op still announced a change"


def test_pins_keep_the_order_they_were_added(panel):
    for reg_type in ("minecraft:block", "alexscaves:gas", "forge:biome_modifier"):
        panel.pin(reg_type)
    assert pinned_types(panel) == [
        "minecraft:block", "alexscaves:gas", "forge:biome_modifier"]


def test_a_fresh_profile_pins_the_two_you_always_open(qapp):
    """`None` means the profile has never said — distinct from a profile that says
    "nothing", which is what unpinning everything leaves behind."""
    from packsmith.gui.shell.panels.registry_panel import RegistryPanel

    p = RegistryPanel(Dump(REGISTRY))
    try:
        assert p.pins() == list(RegistryPanel.DEFAULT_PINS)
        assert "minecraft:item" in pinned_types(p)
    finally:
        p.deleteLater()


def test_unpinning_everything_is_remembered_as_empty(qapp):
    """The bug this guards: treating an empty saved list as "no answer" and handing the
    defaults straight back, so the pins the user removed reappear next launch."""
    p = make(REGISTRY, pins=[])
    try:
        assert p.pins() == []
        assert section(p) is None
    finally:
        p.deleteLater()


def test_a_pin_for_a_registry_the_pack_lost_is_kept_but_not_shown(qapp):
    """Mods come and go across packdump imports. Dropping the pin would mean putting the
    mod back doesn't bring the shortcut back with it."""
    p = make(REGISTRY, pins=["minecraft:item", "gonemod:widget"])
    try:
        assert pinned_types(p) == ["minecraft:item"]
        assert "gonemod:widget" in p.pins()
    finally:
        p.deleteLater()


def test_a_namespace_offers_no_menu(panel):
    """Right-clicking a heading offers nothing — there is no registry behind it to pin."""
    namespace = next(r for r in rows(panel) if r.text(0) == "minecraft")
    assert namespace.data(0, Qt.UserRole) is None, "a namespace carries a registry type"
    assert panel.menu_for(namespace.data(0, Qt.UserRole)) is None


def test_the_menu_offers_pin_then_unpin_for_the_same_row(panel):
    """One entry that flips, rather than two that are wrong half the time."""
    menu = panel.menu_for("minecraft:block")
    assert [a.text() for a in menu.actions()] == ["Pin to top"]

    menu.actions()[0].trigger()
    assert panel.pins() == ["minecraft:block"]

    menu = panel.menu_for("minecraft:block")
    assert [a.text() for a in menu.actions()] == ["Unpin"]
    menu.actions()[0].trigger()
    assert panel.pins() == []


def test_the_menu_on_a_pinned_row_unpins_the_right_thing(pinned):
    """The pinned row and its twin under `minecraft` are the same registry, so unpinning
    from either has to mean the same thing."""
    menu = pinned.menu_for(section(pinned).child(0).data(0, Qt.UserRole))
    menu.actions()[0].trigger()
    assert pinned.pins() == []


# --- the rule under the pins -----------------------------------------------------------------

def ruled(panel):
    """Rows carrying the separator flag, by label."""
    from packsmith.gui.shell.panels.registry_panel import SEPARATOR_ROLE
    return [r.text(0) for r in rows(panel) if r.data(0, SEPARATOR_ROLE)]


def test_the_rule_follows_the_pins(panel):
    """A divider above the first row would be a line under nothing."""
    assert ruled(panel) == []
    panel.pin("minecraft:block")
    assert ruled(panel) == ["alexscaves"]       # the first namespace
    panel.unpin("minecraft:block")
    assert ruled(panel) == []


def test_the_rule_does_not_move_when_the_pins_collapse(pinned):
    """The only non-obvious part: the flag hangs off the row *below* the rule, because the
    pinned section changes height when it opens and "under the last pinned thing" is a
    moving target."""
    section(pinned).setExpanded(False)
    pinned.refresh()
    assert ruled(pinned) == ["alexscaves"]


def test_the_count_column_keeps_its_room(panel):
    """Sizing the name column to its contents — what the flat list did — now measures the
    indent too, and pushed the numbers off the panel's edge."""
    from PySide6.QtWidgets import QHeaderView

    header = panel._tree.header()
    assert header.sectionResizeMode(0) == QHeaderView.Stretch
    assert header.sectionResizeMode(1) == QHeaderView.ResizeToContents
