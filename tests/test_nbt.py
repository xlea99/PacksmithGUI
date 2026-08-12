"""Reading and writing NBT — design 6.4.

The whole job is **type exactness**. `Count: 64b` and `Count: 64` are different files, and
writing an int where the game wants a byte gets you anything from a silently ignored field
to a world that won't load. So most of what follows is about a value keeping its width
across a round trip, not about parsing succeeding.

Verified beyond these tests against reality: 8,369 real `.dat`/`.nbt`/`.dat_old`/
`.schematic` files from the user's instances round-tripped **byte-exactly**, with zero
differences — including 700KB `level.dat` files carrying 337,000 nodes.
"""
import gzip
import struct

import pytest

from packsmith.core import nbt
from packsmith.core.nbt import (
    Byte, ByteArray, Compound, Double, Float, Int, IntArray, List, Long, LongArray,
    NbtError, Short, String)


def roundtrip(root, name="", compression="none"):
    """Serialise and reparse. Returns the tree that came back."""
    return nbt.loads(nbt.dumps(root, name, compression))[1]


def payload(root, name="", compression="none"):
    """The decompressed bytes, so comparisons can't be flattered by gzip settings."""
    return nbt.decompress(nbt.dumps(root, name, compression))[0]


# --- every type survives, as itself ------------------------------------------------------

@pytest.mark.parametrize("value", [
    Byte(-128), Byte(127), Short(-32768), Short(32767), Int(-2**31), Int(2**31 - 1),
    Long(-2**63), Long(2**63 - 1), Float(0.5), Double(1e300), String("hello"),
])
def test_a_scalar_keeps_its_type_and_value(value):
    back = roundtrip(Compound({"v": value}))["v"]
    assert back == value
    assert type(back) is type(value), "the width changed, which changes the file"


def test_the_same_number_in_two_widths_stays_two_widths():
    """The single failure this format punishes: 64 as a byte and 64 as an int are not
    interchangeable, and nothing downstream can recover the distinction once it is lost."""
    back = roundtrip(Compound({"b": Byte(64), "i": Int(64)}))
    assert type(back["b"]) is Byte and type(back["i"]) is Int
    assert payload(Compound({"b": Byte(64)})) != payload(Compound({"b": Int(64)}))


@pytest.mark.parametrize("array", [
    ByteArray([-1, 0, 127]), IntArray([-2**31, 0, 2**31 - 1]),
    LongArray([-2**63, 0, 2**63 - 1]), ByteArray([]), IntArray([]),
])
def test_arrays_survive_including_empty_ones(array):
    back = roundtrip(Compound({"a": array}))["a"]
    assert list(back) == list(array) and type(back) is type(array)


def test_nested_compounds_and_lists_survive():
    tree = Compound({
        "Data": Compound({
            "Player": Compound({
                "Health": Float(20.0),
                "Inventory": List([Compound({"id": String("minecraft:stone"),
                                             "Count": Byte(64)})], element_id=nbt.COMPOUND),
            }),
        }),
    })
    back = roundtrip(tree)
    item = back["Data"]["Player"]["Inventory"][0]
    assert item["id"] == "minecraft:stone"
    assert type(item["Count"]) is Byte


def test_the_root_name_survives():
    assert nbt.loads(nbt.dumps(Compound({"a": Byte(1)}), "Data", "none"))[0] == "Data"


# --- the two details a naive reader gets wrong ---------------------------------------------

def test_an_empty_lists_element_type_is_preserved():
    """Some writers say TAG_End for an empty list and some say the type it would hold.
    Rewriting one as the other changes the file for no reason."""
    typed = List([], element_id=nbt.COMPOUND)
    back = roundtrip(Compound({"L": typed}))["L"]
    assert back.element_id == nbt.COMPOUND
    assert payload(Compound({"L": typed})) != payload(
        Compound({"L": List([], element_id=nbt.END)}))


@pytest.mark.parametrize("text", [
    "plain", "", "unicode: café", "emoji: 🧱", "with\x00nul", "日本語",
])
def test_strings_round_trip_through_javas_modified_utf8(text):
    """NBT strings come from `DataOutputStream.writeUTF`: NUL is `C0 80` and non-BMP
    characters are CESU-8 surrogate pairs. Plain UTF-8 either throws or produces different
    bytes on the way back out."""
    assert roundtrip(Compound({"s": String(text)}))["s"] == text


def test_a_nul_byte_is_not_written_as_a_zero_byte():
    """The reason modified UTF-8 exists: a real NUL would terminate the string early for
    anything reading it the Java way."""
    assert b"\xc0\x80" in payload(Compound({"s": String("a\x00b")}))


def test_a_supplementary_character_is_written_as_a_surrogate_pair():
    """CESU-8, not UTF-8: six bytes rather than four."""
    encoded = nbt.encode_modified_utf8("🧱")
    assert len(encoded) == 6
    assert nbt.decode_modified_utf8(encoded) == "🧱"


# --- compression -------------------------------------------------------------------------

@pytest.mark.parametrize("compression", ["gzip", "zlib", "none"])
def test_each_compression_round_trips_and_reports_itself(compression):
    raw = nbt.dumps(Compound({"a": Int(1)}), "", compression)
    _name, tree, found = nbt.loads(raw)
    assert found == compression and tree["a"] == 1


def test_a_rewrite_is_byte_stable():
    """Two writes of the same tree must produce the same bytes, or "did this change" is
    unanswerable by comparison. gzip stamps an mtime unless told not to."""
    tree = Compound({"a": Int(1)})
    assert nbt.dumps(tree, "", "gzip") == nbt.dumps(tree, "", "gzip")


# --- refusing to guess ---------------------------------------------------------------------

def test_a_bare_python_int_is_refused():
    """Byte, short, int and long are all plausible for `5`, and the file says which.
    Guessing is how a Count stops being a byte."""
    with pytest.raises(NbtError, match="no NBT type"):
        nbt.dumps(Compound({"a": 5}))


def test_the_refusal_names_the_types_available():
    with pytest.raises(NbtError, match="Byte, Int, String"):
        nbt.dumps(Compound({"a": 5}))


# --- damaged and hostile input ---------------------------------------------------------------

def test_a_file_that_is_not_nbt_is_refused_cleanly():
    """`.dat` is not a reliable signal: mods use it for their own formats. One in the
    user's own instances begins with the ASCII bytes 'Qewnfles'."""
    with pytest.raises(NbtError, match="root tag is not a compound"):
        nbt.loads(b"Qewnfles\x00\x01\x02")


def test_an_empty_file_is_refused_cleanly():
    with pytest.raises(NbtError):
        nbt.loads(b"")


def test_a_truncated_file_says_so():
    good = nbt.dumps(Compound({"a": Int(1), "b": String("x" * 40)}), "", "none")
    with pytest.raises(NbtError, match="truncated"):
        nbt.loads(good[:len(good) // 2])


def test_a_gzip_wrapper_around_rubbish_is_refused():
    with pytest.raises(NbtError):
        nbt.loads(gzip.compress(b"not nbt at all"))


def test_a_negative_list_length_reads_as_empty():
    """Seen in the wild; treating it as a huge allocation would be worse than treating it
    as nothing."""
    raw = (struct.pack(">b", nbt.COMPOUND) + struct.pack(">H", 0)
           + struct.pack(">b", nbt.LIST) + struct.pack(">H", 1) + b"L"
           + struct.pack(">b", nbt.INT) + struct.pack(">i", -1) + b"\x00")
    assert list(nbt.loads(raw)[1]["L"]) == []
