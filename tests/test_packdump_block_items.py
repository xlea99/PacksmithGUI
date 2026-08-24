"""Which items place a block, and what shape it is — packdump schema 3.

Two guesses at "is this a BlockItem" are available from the registries alone, and on a real
300-mod pack they disagree on 114 items while each is wrong in the direction the other is
right:

* **id also in `minecraft:block`** misses `bakery:bread`, a BlockItem whose block is
  `bakery:bread_block`;
* **a `block.` translation key** misses `brazilian_expansion:acai`, a BlockItem whose mod
  registered an `item.*` key anyway.

Inside the game it is one type check, so the mod records it. These tests are mostly about
the boundary that makes recording it safe: **a snapshot is an archive, not a cache**
(design 3.1). Dumps written before this existed must still open, and must not be quietly
upgraded into claiming they knew something they never recorded.
"""
import json
import re
from pathlib import Path

import pytest

from packsmith.core.packdump import Packdump, _VALID_SCHEMAS

ITEMS = ["minecraft:stone", "minecraft:stone_stairs", "minecraft:stick", "bakery:bread"]


def write_dump(path, *, schema=3, block_items=True):
    (path / "registries").mkdir(parents=True, exist_ok=True)
    (path / "attributes").mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({
        "type": "packsmith_full_dump", "schema_version": schema,
        "generated_at_utc": "2026-08-20T12:00:00+00:00",
        "minecraft_version": "1.20.1", "loader": "forge", "loader_version": "47.4.13",
        "mods": [{"mod_id": "bakery", "name": "Bakery", "version": "1.0"}],
        "registries": [{"type": "minecraft:item", "file": "minecraft_item.json",
                        "count": len(ITEMS)}],
    }), encoding="utf-8")
    (path / "registries" / "minecraft_item.json").write_text(
        json.dumps({"values": ITEMS}), encoding="utf-8")
    (path / "attributes" / "localization.json").write_text(json.dumps({
        "locale": "en_us",
        "values": {"minecraft:item": {i: i.split(":")[1].title() for i in ITEMS}},
    }), encoding="utf-8")

    if block_items:
        (path / "attributes" / "block_items.json").write_text(json.dumps({
            "schema_version": 3, "type": "block_items",
            "places": {"minecraft:item": {
                # the same id — the ordinary case
                "minecraft:stone": "minecraft:stone",
                "minecraft:stone_stairs": "minecraft:stone_stairs",
                # a DIFFERENT id — the case no outside reader can rebuild
                "bakery:bread": "bakery:bread_block",
            }},
            "forms": {
                "minecraft:item": {"minecraft:stone": "block",
                                   "minecraft:stone_stairs": "stairs",
                                   "bakery:bread": "block"},
                "minecraft:block": {"minecraft:stone": "block",
                                    "minecraft:stone_stairs": "stairs"},
            },
            "classes": {"minecraft:block": {"minecraft:stone": "Block",
                                            "minecraft:stone_stairs": "StairBlock"}},
        }), encoding="utf-8")
    return path


@pytest.fixture
def dump(tmp_path):
    return Packdump.load(write_dump(tmp_path / "dump"))


# --- the fact itself -----------------------------------------------------------------

def test_a_block_item_is_known_as_one(dump):
    assert dump.is_block_item("minecraft:stone") is True
    assert dump.is_block_item("minecraft:stone_stairs") is True


def test_a_plain_item_is_not(dump):
    """`minecraft:stick` is absent from `places`. Absence inside the file means "not a block
    item" — the file's own presence is what says this dump knows the difference."""
    assert dump.is_block_item("minecraft:stick") is False
    assert dump.attribute("minecraft:item", "minecraft:stick", "places_block") is None


def test_the_block_it_places_can_differ_from_its_own_id(dump):
    """The case that defeats intersecting the two registries, and the reason this records a
    block id rather than a boolean: a texture or model generator needs to know WHICH."""
    assert dump.attribute("minecraft:item", "bakery:bread", "places_block") == \
        "bakery:bread_block"
    assert dump.is_block_item("bakery:bread") is True


def test_the_form_comes_from_the_class_not_the_name(dump):
    assert dump.attribute("minecraft:item", "minecraft:stone_stairs", "form") == "stairs"
    assert dump.attribute("minecraft:item", "minecraft:stone", "form") == "block"
    assert dump.attribute("minecraft:block", "minecraft:stone_stairs", "block_class") == \
        "StairBlock"


# --- old snapshots are archives, not caches (design 3.1) --------------------------------

def test_a_dump_from_before_this_existed_still_opens(tmp_path):
    """The rule that outranks the feature: `history/` is the only copy of what the pack
    looked like, and no re-dump brings back a version that is no longer installed."""
    old = Packdump.load(write_dump(tmp_path / "old", schema=2, block_items=False))

    assert old.registry["minecraft:item"]["values"] == ITEMS
    assert old.attribute("minecraft:item", "minecraft:stone", "localization") == "Stone"


def test_an_old_dump_says_it_does_not_know_rather_than_guessing(tmp_path):
    """`is_block_item` returns False for a snapshot that never recorded it, which is
    indistinguishable from a real False — so anything acting on the answer has to be able to
    ask whether the question was ever put."""
    old = Packdump.load(write_dump(tmp_path / "old", schema=2, block_items=False))

    assert old.knows_block_items() is False
    assert old.is_block_item("minecraft:stone") is False, "no guessing from the id"

    new = Packdump.load(write_dump(tmp_path / "new"))
    assert new.knows_block_items() is True


def test_the_columns_offered_follow_what_the_snapshot_holds(tmp_path):
    """An empty column reads as data that failed to load rather than data that never
    existed, so an old dump must not offer these at all."""
    old = Packdump.load(write_dump(tmp_path / "old", schema=2, block_items=False))
    new = Packdump.load(write_dump(tmp_path / "new"))

    assert "places_block" not in old.attributes_for("minecraft:item")
    assert "places_block" in new.attributes_for("minecraft:item")
    assert "block_class" not in new.attributes_for("minecraft:item"), \
        "only blocks carry a block class"


# --- round trip ------------------------------------------------------------------------

def test_saving_and_reloading_keeps_it(tmp_path):
    dump = Packdump.load(write_dump(tmp_path / "src"))
    dump.save(tmp_path / "out")
    again = Packdump.load(tmp_path / "out")

    assert again.attribute("minecraft:item", "bakery:bread", "places_block") == \
        "bakery:bread_block"
    assert again.attribute("minecraft:item", "minecraft:stone_stairs", "form") == "stairs"


def test_round_tripping_an_old_dump_does_not_invent_the_file(tmp_path):
    """Writing an empty `places` block would claim the mod looked and found nothing, when
    the truth is it was never asked — the rule `keys` already follows."""
    old = Packdump.load(write_dump(tmp_path / "old", schema=2, block_items=False))
    old.save(tmp_path / "out")

    assert not (tmp_path / "out" / "attributes" / "block_items.json").exists()


# --- a half-written file gives up the half it has ----------------------------------------

def test_places_without_forms_still_loads(tmp_path):
    """Feature-detected per map, like `keys`: the version says what a reader may expect, not
    what a file actually contains."""
    path = write_dump(tmp_path / "partial")
    (path / "attributes" / "block_items.json").write_text(json.dumps({
        "schema_version": 3, "type": "block_items",
        "places": {"minecraft:item": {"minecraft:stone": "minecraft:stone"}},
    }), encoding="utf-8")

    dump = Packdump.load(path)

    assert dump.is_block_item("minecraft:stone") is True
    assert dump.attribute("minecraft:item", "minecraft:stone", "form") is None


# --- the two repos have to agree on the number --------------------------------------------

def test_the_gui_accepts_the_version_the_mod_stamps():
    """The cross-repo contract, and the half that is easy to forget. The mod bumps
    `DumpSchema.VERSION`; if the GUI's `_VALID_SCHEMAS` is not extended in the same change,
    every fresh dump is refused outright as "written by a newer Packsmith".

    Read out of the Java source rather than restated here, because a copy of the number is
    a third place for it to drift.
    """
    java = (Path.home() / "IdeaProjects" / "Packsmith" / "src" / "main" / "java" /
            "net" / "xlea99" / "packsmith" / "dump" / "DumpSchema.java")
    if not java.is_file():
        pytest.skip("the Forge mod repo is not checked out beside this one")

    stamped = int(re.search(r"VERSION\s*=\s*(\d+)", java.read_text(encoding="utf-8")).group(1))
    assert stamped in _VALID_SCHEMAS, (
        f"the mod stamps schema {stamped} and this build accepts {sorted(_VALID_SCHEMAS)} — "
        f"every fresh dump would be refused")
