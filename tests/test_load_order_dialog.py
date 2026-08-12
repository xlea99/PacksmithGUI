"""Reordering a loader's packs — design 8.1.

Load order decides **which override wins**, so it is the one piece of this whole area where
getting the sequence wrong produces a pack that looks right and behaves wrong. Hence the
tests below care about order as a *sequence* rather than a set: a comparison that passes on
`{"a", "b"}` would pass on the exact bug this exists to prevent.

The dialog lives in Settings today as an admitted stopgap — load order is pack content, not
an application preference — so it is deliberately reachable from the provider rather than
coupled to the settings dialog, and will move to the Files panel's Datapacks category
(§6.2) when that exists.
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.integrations.paxi import PaxiProvider


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def world(tmp_path):
    """Paxi with three datapacks in a NON-alphabetical load order, so that sorting by name
    and honouring the order file give visibly different answers."""
    paxi = PaxiProvider()
    for name in ("alpha", "beta", "gamma"):
        paxi.create_pack(tmp_path, name)
    paxi.set_load_order(tmp_path, ["gamma", "alpha", "beta"])
    return paxi, tmp_path


def dialog_for(paxi, root, kinds=(("datapacks", "Datapacks"),)):
    from packsmith.gui.load_order_dialog import LoadOrderDialog
    return LoadOrderDialog(paxi, root, kinds=list(kinds))


def rows(dialog):
    return [dialog._list.item(i).text() for i in range(dialog._list.count())]


# --- what it shows ---------------------------------------------------------------------

def test_packs_are_listed_in_load_order_not_alphabetically(world):
    paxi, root = world
    assert rows(dialog_for(paxi, root)) == ["gamma", "alpha", "beta"]


def test_an_empty_loader_says_so_instead_of_showing_nothing(tmp_path):
    paxi = PaxiProvider()
    dialog = dialog_for(paxi, tmp_path)
    assert rows(dialog) == []
    assert "nothing to order" in dialog._empty.text()


# --- moving ------------------------------------------------------------------------------

def test_moving_a_pack_down_swaps_it_with_its_neighbour(world):
    paxi, root = world
    dialog = dialog_for(paxi, root)
    dialog._list.setCurrentRow(0)
    dialog._move(1)

    assert rows(dialog) == ["alpha", "gamma", "beta"]
    assert dialog._list.currentRow() == 1, "the selection didn't follow the moved pack"


def test_moving_up_is_the_exact_inverse(world):
    paxi, root = world
    dialog = dialog_for(paxi, root)
    dialog._list.setCurrentRow(2)
    dialog._move(-1)
    assert rows(dialog) == ["gamma", "beta", "alpha"]


def test_the_arrows_are_dead_at_the_ends(world):
    paxi, root = world
    dialog = dialog_for(paxi, root)

    dialog._list.setCurrentRow(0)
    dialog._sync_arrows()
    assert not dialog._up.isEnabled() and dialog._down.isEnabled()

    dialog._list.setCurrentRow(2)
    dialog._sync_arrows()
    assert dialog._up.isEnabled() and not dialog._down.isEnabled()


def test_moving_past_the_end_is_a_no_op_rather_than_an_error(world):
    paxi, root = world
    dialog = dialog_for(paxi, root)
    dialog._list.setCurrentRow(0)
    dialog._move(-1)              # must not raise or wrap around
    assert rows(dialog) == ["gamma", "alpha", "beta"]


# --- saving --------------------------------------------------------------------------------

def test_accepting_writes_the_order_through_the_provider(world):
    paxi, root = world
    dialog = dialog_for(paxi, root)
    dialog._list.setCurrentRow(0)
    dialog._move(1)
    dialog.accept()

    assert dialog.saved
    assert paxi.load_order(root, "datapacks") == ["alpha", "gamma", "beta"]
    # ...and the file on disk is what Paxi itself writes, not our own shape.
    written = json.loads(
        (root / "config" / "paxi" / "datapack_load_order.json").read_text(encoding="utf-8"))
    assert written == {"loadOrder": ["alpha", "gamma", "beta"]}


def test_cancelling_changes_nothing_on_disk(world):
    paxi, root = world
    dialog = dialog_for(paxi, root)
    dialog._list.setCurrentRow(0)
    dialog._move(1)
    dialog.reject()

    assert not dialog.saved
    assert paxi.load_order(root, "datapacks") == ["gamma", "alpha", "beta"]


def test_both_kinds_are_saved_even_though_only_one_is_on_screen(world):
    """The combo is a view over two lists. Saving only the visible one would silently
    discard a reorder the user made and then switched away from."""
    paxi, root = world
    for name in ("skins", "sounds"):
        paxi.create_pack(root, name, kind="resourcepacks")
    paxi.set_load_order(root, ["sounds", "skins"], "resourcepacks")

    dialog = dialog_for(paxi, root, kinds=(("datapacks", "Datapacks"),
                                           ("resourcepacks", "Resource Packs")))
    dialog._kind.setCurrentIndex(1)          # reorder resource packs...
    dialog._list.setCurrentRow(0)
    dialog._move(1)
    dialog._kind.setCurrentIndex(0)          # ...then switch away before saving
    dialog.accept()

    assert paxi.load_order(root, "resourcepacks") == ["skins", "sounds"]
    assert paxi.load_order(root, "datapacks") == ["gamma", "alpha", "beta"]


def test_a_pack_added_outside_packsmith_still_appears(world):
    """`packs()` returns the order file's entries first and everything else after, so a
    folder dropped in by hand is listed rather than invisible."""
    paxi, root = world
    paxi.create_pack(root, "delta")
    assert rows(dialog_for(paxi, root)) == ["gamma", "alpha", "beta", "delta"]
