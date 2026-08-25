"""The texture each block actually renders with — packdump schema 4.

`<namespace>:block/<path>` is a convention, not a rule, and the pack this exists for breaks
it constantly: StoneZone registers blocks at `c/alexscaves/cut_guanostone`, Alex's Caves
nests paths, and any mod may point a model wherever it likes. A generator cutting stairs
from a foreign base has to name that base's texture *exactly* — and guessing wrong produces
the purple checkerboard, a failure that hides until someone is standing in front of it.

These tests exist because every failure mode here is silent. A missing attribute reads as
None, an absent file reads as an empty map, and a dedicated-server dump legitimately has no
textures at all — three different situations that all look like "this block has no texture"
to a caller that does not distinguish them.
"""
import json

import pytest

from packsmith.core.packdump import Packdump, _VALID_SCHEMAS

BLOCKS = ["minecraft:stone", "twigs:polished_calcite",
          "stonezone:c/alexscaves/cut_guanostone", "deep_end_assets:polished_plainstone"]

# What each block's baked model resolved to. Two of these are the whole point:
# `stonezone` puts its texture somewhere no rule predicts, and the generated block has no
# art yet and honestly says so.
TEXTURES = {
    "minecraft:stone": "minecraft:block/stone",
    "twigs:polished_calcite": "twigs:block/polished_calcite",
    "stonezone:c/alexscaves/cut_guanostone": "stonezone:block/c/alexscaves/cut_guanostone",
    "deep_end_assets:polished_plainstone": "minecraft:missingno",
}


def write_dump(path, *, schema=4, textures=True):
    (path / "registries").mkdir(parents=True, exist_ok=True)
    (path / "attributes").mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({
        "type": "packsmith_full_dump", "schema_version": schema,
        "generated_at_utc": "2026-08-24T12:00:00+00:00",
        "minecraft_version": "1.20.1", "loader": "forge", "loader_version": "47.4.13",
        "mods": [{"mod_id": "twigs", "name": "Twigs", "version": "1.0"}],
        "registries": [{"type": "minecraft:block", "file": "minecraft_block.json",
                        "count": len(BLOCKS)}],
    }), encoding="utf-8")
    (path / "registries" / "minecraft_block.json").write_text(
        json.dumps({"values": BLOCKS}), encoding="utf-8")
    (path / "attributes" / "localization.json").write_text(json.dumps({
        "locale": "en_us",
        "values": {"minecraft:block": {b: b.split(":")[1] for b in BLOCKS}},
    }), encoding="utf-8")

    if textures:
        (path / "attributes" / "textures.json").write_text(json.dumps({
            "schema_version": 4, "type": "textures",
            "textures": {"minecraft:block": TEXTURES},
        }), encoding="utf-8")
    return path


@pytest.fixture
def dump(tmp_path):
    return Packdump.load(write_dump(tmp_path / "dump"))


# --- the fact itself -------------------------------------------------------------------

def test_a_texture_path_is_read_back(dump):
    assert dump.attribute("minecraft:block", "minecraft:stone", "texture") == \
        "minecraft:block/stone"


def test_the_path_is_not_derivable_from_the_block_id(dump):
    """The entire reason this is dumped rather than computed. Nothing about
    `stonezone:c/alexscaves/cut_guanostone` tells a reader where its texture lives, and the
    convention every generator would reach for gets it wrong."""
    got = dump.attribute("minecraft:block", "stonezone:c/alexscaves/cut_guanostone",
                         "texture")
    assert got == "stonezone:block/c/alexscaves/cut_guanostone"

    naive = "stonezone:block/cut_guanostone"          # what `<ns>:block/<name>` would guess
    assert got != naive


def test_a_block_with_no_art_yet_says_missingno_rather_than_nothing(dump):
    """Recorded on purpose. A generated base whose PNG has not been drawn is exactly this
    state, and it is the report the art backlog comes from — dropping the entry would make
    "the game found no texture" indistinguishable from "this dump never looked"."""
    assert dump.attribute("minecraft:block", "deep_end_assets:polished_plainstone",
                          "texture") == "minecraft:missingno"


def test_an_unknown_block_answers_none(dump):
    assert dump.attribute("minecraft:block", "nope:nothing", "texture") is None


# --- old snapshots are archives, not caches (design 3.1) --------------------------------

def test_a_dump_from_before_this_existed_still_opens(tmp_path):
    old = Packdump.load(write_dump(tmp_path / "old", schema=3, textures=False))

    assert old.registry["minecraft:block"]["values"] == BLOCKS
    assert old.attribute("minecraft:block", "minecraft:stone", "localization") == "stone"


def test_no_textures_is_distinguishable_from_no_texture(tmp_path):
    """The failure this guards is a generator emitting thousands of blocks pointing at
    nothing, all at once and silently. Textures are CLIENT data, so an absent file means
    either an old snapshot or one dumped on a dedicated server — never "these blocks are
    untextured", which is what a bare `None` would suggest."""
    blind = Packdump.load(write_dump(tmp_path / "old", schema=3, textures=False))

    assert blind.knows_textures() is False
    assert blind.attribute("minecraft:block", "minecraft:stone", "texture") is None

    seeing = Packdump.load(write_dump(tmp_path / "new"))
    assert seeing.knows_textures() is True


def test_the_column_is_offered_only_when_the_snapshot_holds_it(tmp_path):
    old = Packdump.load(write_dump(tmp_path / "old", schema=3, textures=False))
    new = Packdump.load(write_dump(tmp_path / "new"))

    assert "texture" not in old.attributes_for("minecraft:block")
    assert "texture" in new.attributes_for("minecraft:block")


# --- round trip ------------------------------------------------------------------------

def test_schema_4_is_accepted(tmp_path):
    assert 4 in _VALID_SCHEMAS
    assert Packdump.load(write_dump(tmp_path / "d")).schema == 4


def test_saving_and_reloading_keeps_the_textures(tmp_path):
    dump = Packdump.load(write_dump(tmp_path / "d"))
    dump.save(tmp_path / "out")

    again = Packdump.load(tmp_path / "out")
    assert again.knows_textures() is True
    for block, texture in TEXTURES.items():
        assert again.attribute("minecraft:block", block, "texture") == texture


def test_round_tripping_an_older_dump_does_not_invent_the_file(tmp_path):
    """A schema-3 snapshot re-saved must stay a snapshot that never knew about textures —
    writing an empty map would have it claim the mod looked and found none."""
    old = Packdump.load(write_dump(tmp_path / "old", schema=3, textures=False))
    old.save(tmp_path / "out")

    assert not (tmp_path / "out" / "attributes" / "textures.json").exists()
    assert Packdump.load(tmp_path / "out").knows_textures() is False


# --- the import gate (design 3.1) -------------------------------------------------------

def test_gaining_textures_makes_a_dump_UNEQUAL_to_one_without_them(tmp_path):
    """The failure this guards is total and permanent, not partial.

    Import only happens when the incoming dump differs from the stored one. Textures change
    nothing a registry, count or display name would notice — so a snapshot that gained
    13,240 of them read as a duplicate of the one holding none, was discarded, and re-dumping
    produced the same verdict every time. The data could never arrive.
    """
    without = Packdump.load(write_dump(tmp_path / "old", schema=3, textures=False))
    with_ = Packdump.load(write_dump(tmp_path / "new"))

    assert without != with_
    assert with_ != without


def test_a_retexture_alone_is_a_different_dump(tmp_path):
    """A resource pack repointing one block changes no id and no name. If that is not a
    difference, the profile keeps generating models against a texture nobody renders."""
    a = Packdump.load(write_dump(tmp_path / "a"))

    moved = dict(TEXTURES)
    moved["twigs:polished_calcite"] = "mypack:block/fancy_calcite"
    path = write_dump(tmp_path / "b")
    (path / "attributes" / "textures.json").write_text(json.dumps({
        "schema_version": 4, "type": "textures",
        "textures": {"minecraft:block": moved},
    }), encoding="utf-8")
    b = Packdump.load(path)

    assert a != b


def test_two_identical_dumps_are_still_equal(tmp_path):
    """The other half. Equality that always says "different" would re-import on every open
    and fill the snapshot history with copies of one pack."""
    assert Packdump.load(write_dump(tmp_path / "a")) == \
        Packdump.load(write_dump(tmp_path / "b"))
