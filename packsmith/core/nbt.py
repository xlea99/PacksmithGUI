"""Reading and writing NBT — Minecraft's binary tag format (design 6.4).

Owned rather than depended on, because the format is small and frozen (thirteen tag types;
the last addition was TAG_Long_Array in 2017) and because **type exactness is the entire
job**. `Count: 64b` and `Count: 64` are different files: write an int where the game wants
a byte and you get anything from a silently ignored field to a world that won't load. A
library's opinions about coercion are exactly what you do not want between you and that.

**Types are carried by the Python type**, so values still behave like values:

    data["Data"]["Player"]["Health"] * 2      # it is a float, and it multiplies
    type(data["Data"]["Player"]["Health"])    # ...and it is specifically a Double

Two details a naive implementation gets wrong, both handled here:

- **Java's modified UTF-8.** NBT strings are written by `DataOutputStream.writeUTF`, which
  encodes NUL as ``C0 80`` and characters outside the BMP as CESU-8 surrogate pairs rather
  than four-byte UTF-8. Plain `bytes.decode("utf-8")` either fails or silently produces
  different bytes on the way back out.
- **The element type of an empty list.** It has to be preserved as written: some writers
  say TAG_End and some say the type the list would have held, and rewriting one as the
  other changes the file for no reason.

Region files (`.mca`) are deliberately not handled. They are a *container* of up to 1024
separately-compressed chunks, not a tag tree — a different format wearing a similar name.
"""
import gzip
import struct
import zlib
from io import BytesIO

# --- tag ids, in the order the format defines them ---------------------------
END, BYTE, SHORT, INT, LONG, FLOAT, DOUBLE = 0, 1, 2, 3, 4, 5, 6
BYTE_ARRAY, STRING, LIST, COMPOUND, INT_ARRAY, LONG_ARRAY = 7, 8, 9, 10, 11, 12

TAG_NAMES = {
    END: "TAG_End", BYTE: "TAG_Byte", SHORT: "TAG_Short", INT: "TAG_Int",
    LONG: "TAG_Long", FLOAT: "TAG_Float", DOUBLE: "TAG_Double",
    BYTE_ARRAY: "TAG_Byte_Array", STRING: "TAG_String", LIST: "TAG_List",
    COMPOUND: "TAG_Compound", INT_ARRAY: "TAG_Int_Array", LONG_ARRAY: "TAG_Long_Array",
}

# Inclusive ranges, so an edit can be refused before it silently wraps.
INT_RANGES = {
    BYTE: (-128, 127), SHORT: (-32768, 32767),
    INT: (-2**31, 2**31 - 1), LONG: (-2**63, 2**63 - 1),
}


class NbtError(Exception):
    """The bytes are not NBT, or an edit would change a value's type."""


# --- the value types ---------------------------------------------------------
# Subclasses of int/float/str/list/dict so a tag behaves like the thing it is while still
# knowing what it is. `tag_id` is what gets written back out.

class Byte(int):
    tag_id = BYTE


class Short(int):
    tag_id = SHORT


class Int(int):
    tag_id = INT


class Long(int):
    tag_id = LONG


class Float(float):
    tag_id = FLOAT


class Double(float):
    tag_id = DOUBLE


class String(str):
    tag_id = STRING


class ByteArray(list):
    tag_id = BYTE_ARRAY


class IntArray(list):
    tag_id = INT_ARRAY


class LongArray(list):
    tag_id = LONG_ARRAY


class List(list):
    """A homogeneous, positional list. Its element type is part of the data.

    Kept even when the list is empty, because the file said something and rewriting it as
    something else is a change nobody asked for.
    """
    tag_id = LIST

    def __init__(self, items=(), element_id=END):
        super().__init__(items)
        self.element_id = element_id


class Compound(dict):
    """A name → tag map. The closest thing NBT has to key-value."""
    tag_id = COMPOUND


_BY_ID = {BYTE: Byte, SHORT: Short, INT: Int, LONG: Long, FLOAT: Float, DOUBLE: Double,
          STRING: String, BYTE_ARRAY: ByteArray, INT_ARRAY: IntArray,
          LONG_ARRAY: LongArray, LIST: List, COMPOUND: Compound}


# --- Java's modified UTF-8 ---------------------------------------------------

def decode_modified_utf8(raw: bytes) -> str:
    """Decode what `DataOutputStream.writeUTF` produced.

    Fast path first: real NBT is overwhelmingly ASCII, and paying for a byte-by-byte decode
    on every string in an 18 MB file to handle a case that almost never occurs would be a
    poor trade. The slow path only runs when the fast one cannot.
    """
    try:
        if b"\xc0\x80" not in raw:
            return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    out, index, size = [], 0, len(raw)
    while index < size:
        byte = raw[index]
        if byte == 0xC0 and index + 1 < size and raw[index + 1] == 0x80:
            out.append("\x00")           # NUL, encoded so it never appears as a zero byte
            index += 2
        elif byte < 0x80:
            out.append(chr(byte))
            index += 1
        elif byte & 0xE0 == 0xC0:
            out.append(chr(((byte & 0x1F) << 6) | (raw[index + 1] & 0x3F)))
            index += 2
        elif byte & 0xF0 == 0xE0:
            code = (((byte & 0x0F) << 12) | ((raw[index + 1] & 0x3F) << 6)
                    | (raw[index + 2] & 0x3F))
            out.append(chr(code))
            index += 3
        else:
            raise NbtError(f"invalid modified UTF-8 byte 0x{byte:02x}")
    text = "".join(out)
    # Supplementary characters arrive as a surrogate PAIR (CESU-8); Python holds them as
    # two lone surrogates, which `surrogatepass` folds back into one real character.
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        text = text.encode("utf-16", "surrogatepass").decode("utf-16")
    return text


def encode_modified_utf8(text: str) -> bytes:
    """The inverse. Round-trips whatever `decode_modified_utf8` produced."""
    if text.isascii() and "\x00" not in text:
        return text.encode("ascii")
    out = bytearray()
    for char in text:
        code = ord(char)
        if code == 0:
            out += b"\xc0\x80"
        elif code < 0x80:
            out.append(code)
        elif code < 0x800:
            out += bytes((0xC0 | (code >> 6), 0x80 | (code & 0x3F)))
        elif code < 0x10000:
            out += bytes((0xE0 | (code >> 12), 0x80 | ((code >> 6) & 0x3F),
                          0x80 | (code & 0x3F)))
        else:
            # Outside the BMP: written as the two halves of a surrogate pair, each in
            # three bytes. This is CESU-8, not UTF-8, and it is what Java writes.
            code -= 0x10000
            for half in (0xD800 | (code >> 10), 0xDC00 | (code & 0x3FF)):
                out += bytes((0xE0 | (half >> 12), 0x80 | ((half >> 6) & 0x3F),
                              0x80 | (half & 0x3F)))
    return bytes(out)


# --- reading -----------------------------------------------------------------

class _Reader:
    def __init__(self, data: bytes):
        self._data = data
        self._at = 0

    def take(self, count: int) -> bytes:
        if self._at + count > len(self._data):
            raise NbtError("truncated NBT: the file ends mid-tag")
        chunk = self._data[self._at:self._at + count]
        self._at += count
        return chunk

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def string(self) -> String:
        return String(decode_modified_utf8(self.take(self.unpack(">H"))))

    def payload(self, tag_id: int):
        if tag_id == BYTE:
            return Byte(self.unpack(">b"))
        if tag_id == SHORT:
            return Short(self.unpack(">h"))
        if tag_id == INT:
            return Int(self.unpack(">i"))
        if tag_id == LONG:
            return Long(self.unpack(">q"))
        if tag_id == FLOAT:
            return Float(self.unpack(">f"))
        if tag_id == DOUBLE:
            return Double(self.unpack(">d"))
        if tag_id == STRING:
            return self.string()
        if tag_id == BYTE_ARRAY:
            count = self.unpack(">i")
            return ByteArray(struct.unpack(f">{count}b", self.take(count)))
        if tag_id == INT_ARRAY:
            count = self.unpack(">i")
            return IntArray(struct.unpack(f">{count}i", self.take(count * 4)))
        if tag_id == LONG_ARRAY:
            count = self.unpack(">i")
            return LongArray(struct.unpack(f">{count}q", self.take(count * 8)))
        if tag_id == LIST:
            element_id = self.unpack(">b")
            count = self.unpack(">i")
            # A negative length is legal-ish in the wild and means empty.
            items = [self.payload(element_id) for _ in range(max(0, count))]
            return List(items, element_id=element_id)
        if tag_id == COMPOUND:
            compound = Compound()
            while True:
                child_id = self.unpack(">b")
                if child_id == END:
                    return compound
                name = self.string()
                compound[name] = self.payload(child_id)
        raise NbtError(f"unknown tag id {tag_id}")


def decompress(raw: bytes) -> tuple[bytes, str]:
    """Undo whatever compression the file used, and say which it was.

    Returned so a rewrite can use the same one: silently converting a gzipped `.dat` to
    raw NBT produces a file the game may still read but that no longer matches what every
    other tool expects to find.
    """
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw), "gzip"
    if raw[:1] == b"\x78":                 # zlib's usual first byte
        try:
            return zlib.decompress(raw), "zlib"
        except zlib.error:
            pass
    return raw, "none"


def loads(raw: bytes):
    """Parse NBT bytes. Returns ``(root_name, root_tag, compression)``."""
    data, compression = decompress(raw)
    reader = _Reader(data)
    if reader.unpack(">b") != COMPOUND:
        raise NbtError("not NBT: the root tag is not a compound")
    return reader.string(), reader.payload(COMPOUND), compression


def load(path):
    """Read an NBT file. Returns ``(root_name, root_tag, compression)``."""
    with open(path, "rb") as handle:
        return loads(handle.read())


# --- writing -----------------------------------------------------------------

def _write_payload(out: BytesIO, tag) -> None:
    tag_id = tag_id_of(tag)
    if tag_id == BYTE:
        out.write(struct.pack(">b", int(tag)))
    elif tag_id == SHORT:
        out.write(struct.pack(">h", int(tag)))
    elif tag_id == INT:
        out.write(struct.pack(">i", int(tag)))
    elif tag_id == LONG:
        out.write(struct.pack(">q", int(tag)))
    elif tag_id == FLOAT:
        out.write(struct.pack(">f", float(tag)))
    elif tag_id == DOUBLE:
        out.write(struct.pack(">d", float(tag)))
    elif tag_id == STRING:
        encoded = encode_modified_utf8(str(tag))
        out.write(struct.pack(">H", len(encoded)))
        out.write(encoded)
    elif tag_id == BYTE_ARRAY:
        out.write(struct.pack(f">i{len(tag)}b", len(tag), *tag))
    elif tag_id == INT_ARRAY:
        out.write(struct.pack(f">i{len(tag)}i", len(tag), *tag))
    elif tag_id == LONG_ARRAY:
        out.write(struct.pack(f">i{len(tag)}q", len(tag), *tag))
    elif tag_id == LIST:
        element_id = getattr(tag, "element_id", END)
        if tag and element_id == END:
            element_id = tag_id_of(tag[0])
        out.write(struct.pack(">bi", element_id, len(tag)))
        for item in tag:
            _write_payload(out, item)
    elif tag_id == COMPOUND:
        for name, child in tag.items():
            out.write(struct.pack(">b", tag_id_of(child)))
            encoded = encode_modified_utf8(str(name))
            out.write(struct.pack(">H", len(encoded)))
            out.write(encoded)
            _write_payload(out, child)
        out.write(b"\x00")
    else:
        raise NbtError(f"cannot write tag id {tag_id}")


def retype(tag, text: str):
    """Parse ``text`` back into the SAME tag type as ``tag`` (design 6.4).

    Editing a value must never change its type. `Count: 64` and `Count: 64b` are different
    files, and a byte silently promoted to an int is a corrupted save that still loads —
    the worst kind. So the existing tag decides how its replacement is read, and anything
    that doesn't fit is refused rather than coerced.

    Raises ``ValueError`` with a message written for the person who typed it.
    """
    tag_id = tag_id_of(tag)
    if tag_id == STRING:
        return String(text)
    if tag_id in INT_RANGES:
        try:
            value = int(text.strip(), 0)
        except ValueError:
            raise ValueError(f"{TAG_NAMES[tag_id].removeprefix('TAG_')} needs a whole "
                             f"number, not {text.strip()!r}")
        low, high = INT_RANGES[tag_id]
        if not low <= value <= high:
            raise ValueError(
                f"{value:,} does not fit in a "
                f"{TAG_NAMES[tag_id].removeprefix('TAG_')} ({low:,} to {high:,})")
        return _BY_ID[tag_id](value)
    if tag_id in (FLOAT, DOUBLE):
        try:
            return _BY_ID[tag_id](float(text.strip()))
        except ValueError:
            raise ValueError(f"{TAG_NAMES[tag_id].removeprefix('TAG_')} needs a number, "
                             f"not {text.strip()!r}")
    if tag_id in (BYTE_ARRAY, INT_ARRAY, LONG_ARRAY):
        element = {BYTE_ARRAY: BYTE, INT_ARRAY: INT, LONG_ARRAY: LONG}[tag_id]
        low, high = INT_RANGES[element]
        body = text.strip().strip("[]")
        values = []
        for piece in (p for p in body.split(",") if p.strip()):
            try:
                value = int(piece.strip(), 0)
            except ValueError:
                raise ValueError(f"{piece.strip()!r} is not a whole number")
            if not low <= value <= high:
                raise ValueError(f"{value:,} does not fit in this array ({low:,} to {high:,})")
            values.append(value)
        return _BY_ID[tag_id](values)
    raise ValueError(f"{TAG_NAMES.get(tag_id, '?')} is not editable as a single value")


def tag_id_of(tag) -> int:
    """The NBT type of a value, refusing to guess.

    A bare Python ``int`` has no NBT type — byte, short, int and long are all plausible and
    the file says which. Guessing is how a `Count: 64b` becomes a `Count: 64` and the game
    stops reading the field.
    """
    tag_id = getattr(type(tag), "tag_id", None)
    if tag_id is None:
        raise NbtError(
            f"{type(tag).__name__} has no NBT type — wrap it (Byte, Int, String, …) so the "
            f"width is stated rather than guessed")
    return tag_id


def dumps(root_tag, root_name: str = "", compression: str = "gzip") -> bytes:
    """Serialise a tree back to bytes, in the compression it came in."""
    out = BytesIO()
    out.write(struct.pack(">b", COMPOUND))
    encoded = encode_modified_utf8(root_name)
    out.write(struct.pack(">H", len(encoded)))
    out.write(encoded)
    _write_payload(out, root_tag)
    data = out.getvalue()
    if compression == "gzip":
        # mtime=0: otherwise every write produces different bytes for identical data, which
        # makes "did this change" unanswerable by comparing files.
        return gzip.compress(data, mtime=0)
    if compression == "zlib":
        return zlib.compress(data)
    return data


def dump(path, root_tag, root_name: str = "", compression: str = "gzip") -> None:
    with open(path, "wb") as handle:
        handle.write(dumps(root_tag, root_name, compression))
