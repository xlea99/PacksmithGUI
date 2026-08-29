"""Finding a block's PNG, and making one that was never drawn.

Every failure here is quiet. A blank texture that is opaque black instead of transparent
looks like a blank texture. A search that prefers a mod jar over the resource pack shadowing
it returns real bytes from the wrong file. A corrupt jar that takes the index down with it
turns the feature off for the whole pack rather than for one mod. None of those announce
themselves, and all of them are found by someone halfway through drawing.
"""
import zipfile

import pytest

from packsmith.core.textures import (
    DEFAULT_SIZE, TextureIndex, blank_png, texture_member,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# --- id -> path, which unlike block-id -> texture-id is a real rule -----------------------

def test_a_texture_id_becomes_the_path_a_pack_must_use():
    assert texture_member("twigs:block/rocky_dirt") == \
        "assets/twigs/textures/block/rocky_dirt.png"


def test_a_nested_path_survives_intact():
    """StoneZone's real one. The depth is not a special case to flatten — the member path
    is what makes an override an override, so any reshaping produces a file nothing reads."""
    deep = "stonezone:block/c/alexscaves/palettes/stone_types/brick/abyssmarine_cut_brick"
    assert texture_member(deep) == (
        "assets/stonezone/textures/block/c/alexscaves/palettes/stone_types/brick/"
        "abyssmarine_cut_brick.png")


def test_an_unqualified_id_means_vanilla():
    """`ResourceLocation`'s own rule. Getting this wrong would send every vanilla override
    to `assets//textures/...`, which is a path no loader resolves and no error reports."""
    assert texture_member("block/stone") == "assets/minecraft/textures/block/stone.png"


@pytest.mark.parametrize("bad", ["", None, "twigs:", ":block/x", "twigs:../../../evil"])
def test_nonsense_is_refused_rather_than_turned_into_a_path(bad):
    """`..` above all. This path is handed to a writer, and a texture id is data from a
    dump — refusing here is cheaper than trusting the containment check downstream to be
    the only thing between a mod's metadata and someone's home directory."""
    assert texture_member(bad) is None


# --- the blank ---------------------------------------------------------------------------

def test_a_blank_texture_is_a_real_png():
    data = blank_png()
    assert data.startswith(PNG_MAGIC)
    # The IEND chunk in full: zero length, tag, and the CRC of the tag alone.
    assert data[-12:] == b"\x00\x00\x00\x00IEND\xaeB`\x82"


def test_a_blank_texture_is_transparent_and_not_black(tmp_path):
    """The failure this exists for is invisible by construction: on the checkerboard the
    image viewer draws, and in-game against a dark block, opaque black and fully transparent
    are the same picture. An artist would find out by painting over it."""
    QImage = pytest.importorskip("PySide6.QtGui").QImage
    image = QImage.fromData(blank_png(), "PNG")

    assert not image.isNull()
    assert image.hasAlphaChannel()
    assert (image.width(), image.height()) == (DEFAULT_SIZE, DEFAULT_SIZE)
    for x, y in ((0, 0), (DEFAULT_SIZE - 1, DEFAULT_SIZE - 1), (7, 9)):
        assert image.pixelColor(x, y).alpha() == 0


def test_a_size_can_be_asked_for():
    QImage = pytest.importorskip("PySide6.QtGui").QImage
    image = QImage.fromData(blank_png(32), "PNG")
    assert (image.width(), image.height()) == (32, 32)


def test_a_meaningless_size_is_refused():
    with pytest.raises(ValueError):
        blank_png(0)


# --- the search --------------------------------------------------------------------------

MEMBER = "assets/twigs/textures/block/rocky_dirt.png"


def make_jar(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_a_texture_is_found_in_a_mod_jar(tmp_path):
    jar = make_jar(tmp_path / "twigs.jar", {MEMBER: b"shipped"})
    data, source = TextureIndex([jar]).find(MEMBER)

    assert data == b"shipped"
    assert source == jar


def test_a_folder_shaped_resource_pack_is_searched_too(tmp_path):
    pack = tmp_path / "my_pack"
    (pack / "assets/twigs/textures/block").mkdir(parents=True)
    (pack / MEMBER).write_bytes(b"mine")

    data, source = TextureIndex([pack]).find(MEMBER)
    assert (data, source) == (b"mine", pack)


def test_the_first_source_wins(tmp_path):
    """The order is the whole contract. A resource pack shadows the mod it retextures, so a
    search that reached the jar first would hand back the art the user already replaced —
    and the bytes would look perfectly valid on the way out."""
    pack = make_jar(tmp_path / "pack.zip", {MEMBER: b"retextured"})
    jar = make_jar(tmp_path / "twigs.jar", {MEMBER: b"shipped"})

    assert TextureIndex([pack, jar]).find(MEMBER)[0] == b"retextured"
    assert TextureIndex([jar, pack]).find(MEMBER)[0] == b"shipped"


def test_a_source_that_does_not_carry_the_namespace_is_skipped(tmp_path):
    unrelated = make_jar(tmp_path / "other.jar",
                         {"assets/create/textures/block/andesite.png": b"nope"})
    jar = make_jar(tmp_path / "twigs.jar", {MEMBER: b"shipped"})

    index = TextureIndex([unrelated, jar])
    assert index.find(MEMBER)[0] == b"shipped"
    assert unrelated not in index.sources_for("twigs")


def test_a_texture_nobody_ships_is_a_miss_not_an_error(tmp_path):
    """StoneZone composites its palettes at runtime and ships 25 PNGs for thousands of
    blocks, so a texture id that resolves in-game can have no file anywhere. That is the
    signal to create a blank one, not a failure to report."""
    jar = make_jar(tmp_path / "twigs.jar", {MEMBER: b"shipped"})
    assert TextureIndex([jar]).find(
        "assets/stonezone/textures/block/generated.png") == (None, None)


def test_a_corrupt_jar_costs_only_its_own_textures(tmp_path):
    """A half-downloaded mod is a normal state for a modpack. Letting one take the index
    down would turn the feature off for all 244 jars, and the symptom — "nothing is ever
    found" — points nowhere near the cause."""
    broken = tmp_path / "broken.jar"
    broken.write_bytes(b"this is not a zip")
    jar = make_jar(tmp_path / "twigs.jar", {MEMBER: b"shipped"})

    assert TextureIndex([broken, jar]).find(MEMBER)[0] == b"shipped"


def test_a_missing_source_costs_only_its_own_textures(tmp_path):
    jar = make_jar(tmp_path / "twigs.jar", {MEMBER: b"shipped"})
    assert TextureIndex([tmp_path / "gone.jar", jar]).find(MEMBER)[0] == b"shipped"


def test_a_member_outside_assets_is_refused(tmp_path):
    jar = make_jar(tmp_path / "twigs.jar", {"data/twigs/loot/x.json": b"{}"})
    assert TextureIndex([jar]).find("data/twigs/loot/x.json") == (None, None)


def test_the_index_is_built_once_and_reused(tmp_path):
    """Half a second per lookup across a real pack's 244 jars is the difference between a
    right-click that responds and one that appears to have hung."""
    jar = make_jar(tmp_path / "twigs.jar", {MEMBER: b"shipped"})
    index = TextureIndex([jar])
    index.find(MEMBER)

    built = index._by_namespace
    index.find(MEMBER)
    assert index._by_namespace is built


# --- textures a mod builds at load time --------------------------------------------------
#
# StoneZone ships 25 PNGs and registers thousands of blocks, compositing the rest while the
# game loads. Moonlight writes what it generated to `debug/generated_resource_pack`, laid
# out `<namespace>/textures/...` — one folder per pack, without the `assets/` level a real
# resource pack has. Getting that one difference wrong makes every lookup miss silently and
# hands the user a blank canvas for art that already exists.

from packsmith.core.textures import GeneratedPacks     # noqa: E402


def make_dump(root, namespace="stonezone", rest="block/c/cut_abyssmarine.png",
              data=b"generated"):
    target = root / namespace / rest
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return root


GENERATED = "assets/stonezone/textures/block/c/cut_abyssmarine.png"


def test_a_generated_texture_is_found_without_the_assets_level(tmp_path):
    dump = make_dump(tmp_path / "generated_resource_pack",
                     rest="textures/block/c/cut_abyssmarine.png")
    data, where = GeneratedPacks(dump).find(GENERATED)

    assert data == b"generated"
    assert where == dump / "stonezone"      # the pack that answered, not the debug folder


def test_the_index_reaches_generated_textures(tmp_path):
    dump = make_dump(tmp_path / "generated_resource_pack",
                     rest="textures/block/c/cut_abyssmarine.png")
    index = TextureIndex([GeneratedPacks(dump)])

    assert index.find(GENERATED)[0] == b"generated"
    assert "stonezone" in index._by_namespace


def test_a_pack_a_person_made_still_wins(tmp_path):
    """The order is the point. These are the textures a mod generated for itself, so an
    override the user has already written must beat the original it was written to replace
    — otherwise editing one appears to do nothing."""
    mine = make_jar(tmp_path / "mine.zip", {GENERATED: b"retextured"})
    dump = make_dump(tmp_path / "generated_resource_pack",
                     rest="textures/block/c/cut_abyssmarine.png")

    assert TextureIndex([mine, GeneratedPacks(dump)]).find(GENERATED)[0] == b"retextured"


def test_no_dump_folder_is_an_ordinary_miss(tmp_path):
    """It is debug output and may simply not be there. Costing exactly what it cost before
    this existed — a miss, and then a blank canvas — is the whole contract."""
    absent = GeneratedPacks(tmp_path / "never_launched")

    assert absent.namespaces() == set()
    assert absent.find(GENERATED) == (None, None)
    assert TextureIndex([absent]).find(GENERATED) == (None, None)


def test_a_member_outside_assets_is_refused_here_too(tmp_path):
    dump = make_dump(tmp_path / "generated_resource_pack")
    assert GeneratedPacks(dump).find("stonezone/textures/block/x.png") == (None, None)
    assert GeneratedPacks(dump).find("assets/stonezone") == (None, None)


# --- staging a texture for the clipboard -------------------------------------------------
#
# The clipboard carries a PATH, not bytes, so handing the OS something to paste means there
# has to be a file — and most textures worth copying have none, being jar members or built
# at load time. Two things here are quiet if wrong: the NAME, which is what a paste produces
# on the other side, and the BYTES, which must be the original file rather than anything
# re-encoded on the way through.

from packsmith.core.textures import staged_copy      # noqa: E402


def test_a_texture_is_staged_under_its_own_name(tmp_path):
    staged = staged_copy("cut_abyssmarine.png", b"original", root=tmp_path)

    assert staged.name == "cut_abyssmarine.png"
    assert staged.read_bytes() == b"original"


def test_the_bytes_are_the_originals(tmp_path):
    """Byte-for-byte, not an image that happens to look the same. Re-encoding a PNG through
    a decoder is free to change colour type, bit depth and palette — all invisible until
    someone diffs the file they pasted against the one they meant to copy."""
    source = blank_png()
    assert staged_copy("blank.png", source, root=tmp_path).read_bytes() == source


def test_a_member_path_is_reduced_to_its_file_name(tmp_path):
    """Callers hand over whatever they have, which is usually a member path. Staging
    `assets/x/textures/block/y.png` verbatim would create directories nobody asked for and
    paste a file named after the whole path on platforms that allow it."""
    staged = staged_copy("assets/twigs/textures/block/rocky_dirt.png", b"x", root=tmp_path)

    assert staged.name == "rocky_dirt.png"
    assert staged.parent == tmp_path


@pytest.mark.parametrize("bad", ["", "   ", "..", ".", "some/dir/"])
def test_a_name_that_is_not_a_file_is_refused(bad):
    """`..` in particular. The name reaches here from a texture id, which is data out of a
    dump, and this function's whole job is to turn it into a path and write to it."""
    with pytest.raises(ValueError):
        staged_copy(bad, b"x")


def test_copying_the_same_texture_twice_reuses_one_file(tmp_path):
    """Overwritten in place rather than accumulating. A staged file cannot be deleted when
    the copy returns — the clipboard still points at it — so bounding the mess has to come
    from the name being stable rather than from a lifetime somebody manages."""
    staging = tmp_path / "staging"
    first = staged_copy("stone.png", b"one", root=staging)
    second = staged_copy("stone.png", b"two", root=staging)

    assert first == second
    assert second.read_bytes() == b"two"
    assert list(staging.iterdir()) == [second]


def test_the_staging_directory_is_made_if_absent(tmp_path):
    staged = staged_copy("stone.png", b"x", root=tmp_path / "never" / "existed")
    assert staged.is_file()
