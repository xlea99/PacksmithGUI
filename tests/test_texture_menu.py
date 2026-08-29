"""Getting from a cell to the right PNG.

The failures worth a permanent test are the ones that produce a *file*. Offering the wrong
verb is cosmetic and self-reporting — you see it in the menu. Resolving an entry to the
wrong block is not: it writes real bytes to a real path in a real pack, the write succeeds,
the game loads the pack, and nothing about the block you were actually looking at changes.
You find out by wondering why your texture did not take.
"""
import pytest

from packsmith.gui.texture_menu import BLOCKS, ITEMS, TextureMenu, resolve


class FakeDump:
    """Just enough packdump to answer the three questions `resolve` asks."""

    def __init__(self, textures=None, places=None):
        self._textures = textures or {}
        self._places = places or {}

    def attribute(self, registry_type, entry_id, name):
        if name == "places_block":
            return self._places.get(entry_id)
        if name == "texture":
            return self._textures.get(entry_id)
        return None

    def texture_source(self, registry_type, entry_id):
        found = self._textures.get(entry_id)
        return None if found in (None, "minecraft:missingno") else found


DRAWN = FakeDump(textures={"twigs:rocky_dirt": "twigs:block/rocky_dirt"})


# --- which block answers for an entry ----------------------------------------------------

def test_a_block_answers_for_itself():
    found = resolve(DRAWN, BLOCKS, "twigs:rocky_dirt")
    assert found.block_id == "twigs:rocky_dirt"
    assert found.member == "assets/twigs/textures/block/rocky_dirt.png"


def test_an_item_reaches_its_texture_through_the_block_it_places():
    """`places_block`, and the case that matters is the one where the ids differ.

    `bakery:baguette` places `bakery:baguette_block` — measured, along with 113 others on a
    300-mod pack. Assuming the item id doubles as the block id writes a PNG at a path no
    model names, in a pack the game dutifully loads, changing nothing.
    """
    dump = FakeDump(textures={"bakery:baguette_block": "bakery:block/baguette"},
                    places={"bakery:baguette": "bakery:baguette_block"})
    found = resolve(dump, ITEMS, "bakery:baguette")

    assert found.block_id == "bakery:baguette_block"
    assert found.member == "assets/bakery/textures/block/baguette.png"
    assert found.entry_id == "bakery:baguette"      # what the user clicked, kept


def test_an_item_that_places_nothing_has_no_texture_here():
    """A sword's sprite is not a block texture, and the dump records block textures only.
    Offering the action anyway would write a file with no bearing on the item."""
    assert resolve(FakeDump(), ITEMS, "minecraft:diamond_sword") is None


def test_a_registry_that_is_not_items_or_blocks_is_left_alone():
    assert resolve(DRAWN, "minecraft:entity_type", "minecraft:creeper") is None


def test_a_block_that_names_no_texture_at_all_resolves_to_nothing():
    """Create's technical blocks render as nothing AND declare nothing. There is no file to
    open and nowhere to create one, so the menu must not offer to."""
    dump = FakeDump(textures={"create:fake_track": "minecraft:missingno"})
    assert resolve(dump, BLOCKS, "create:fake_track") is None


def test_undrawn_art_resolves_to_where_it_belongs():
    """The whole point. `texture` says missingno, `requested_texture` says where the PNG
    goes, and `texture_source` is the rule that picks between them."""
    dump = FakeDump(textures={"deep_end_assets:coprolith_mosaic": "minecraft:missingno"})
    dump.texture_source = lambda r, e: "deep_end_assets:block/coprolith_mosaic"
    found = resolve(dump, BLOCKS, "deep_end_assets:coprolith_mosaic")

    assert found.drawn is False
    assert found.member == "assets/deep_end_assets/textures/block/coprolith_mosaic.png"


# --- the menu ----------------------------------------------------------------------------

@pytest.fixture
def menu():
    QMenu = pytest.importorskip("PySide6.QtWidgets").QMenu
    pytest.importorskip("PySide6.QtWidgets").QApplication.instance() or \
        pytest.importorskip("PySide6.QtWidgets").QApplication([])
    return QMenu()


def labels(menu):
    return [a.text() for a in menu.actions() if a.text()]


def test_a_drawn_block_can_be_looked_at_and_replaced(menu):
    added = TextureMenu(lambda: DRAWN, None, None).extend(menu, BLOCKS, "twigs:rocky_dirt")

    assert added is True
    assert labels(menu) == ["View Texture", "Override Texture…"]


def test_undrawn_art_is_created_not_viewed(menu):
    """No "View" on a block with no art: there is nothing to show, and an action that
    opens an empty tab is worse than an action that is not there (§5.1)."""
    dump = FakeDump(textures={"x:y": "minecraft:missingno"})
    dump.texture_source = lambda r, e: "x:block/y"
    TextureMenu(lambda: dump, None, None).extend(menu, BLOCKS, "x:y")

    assert labels(menu) == ["Create Texture…"]


def test_nothing_is_offered_for_an_entry_with_no_texture(menu):
    added = TextureMenu(lambda: FakeDump(), None, None).extend(
        menu, ITEMS, "minecraft:diamond_sword")

    assert added is False
    assert labels(menu) == []


def test_the_action_fires_with_the_resolved_target(menu):
    """The handler gets the TARGET, not the entry id — it needs the member path, and
    recomputing it downstream is a second place for the resolution rule to live."""
    fired = []
    TextureMenu(lambda: DRAWN, fired.append, None).extend(menu, BLOCKS, "twigs:rocky_dirt")
    next(a for a in menu.actions() if a.text() == "View Texture").trigger()

    assert fired[0].member == "assets/twigs/textures/block/rocky_dirt.png"


def test_a_differing_block_is_disclosed_rather_than_hidden(menu):
    """Acting on `bakery:baguette_block` under a menu that says `bakery:baguette` is a
    small lie. The tooltip names the block; withholding the action instead would leave the
    user with no way to reach a texture that is genuinely theirs to override."""
    dump = FakeDump(textures={"bakery:baguette_block": "bakery:block/baguette"},
                    places={"bakery:baguette": "bakery:baguette_block"})
    TextureMenu(lambda: dump, None, None).extend(menu, ITEMS, "bakery:baguette")

    assert "bakery:baguette_block" in menu.actions()[0].toolTip()


def test_no_packdump_offers_nothing(menu):
    """A window between profiles has no dump. Asking it for an attribute would raise."""
    assert TextureMenu(lambda: None, None, None).extend(menu, BLOCKS, "twigs:x") is False
