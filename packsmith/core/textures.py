"""Where a block's PNG lives, and how to make one that doesn't exist yet.

The packdump answers *which texture* a block uses (§3.1, schema 4 and 5). This answers the
next two questions, both of which are about files rather than about the game: **where does
that texture live on disk**, and **what do you write when the answer is nowhere**.

Those are separate from the packdump on purpose. A texture id is a fact about the running
game and cannot be reconstructed from outside it; a texture *path* is arithmetic, and the
place it resolves to depends on which packs are installed — a question the dump has no
opinion about and should not grow one.

**The id-to-path rule is a real rule**, unlike the block-id-to-texture-id one it is easy to
confuse it with. `<namespace>:<path>` always means `assets/<namespace>/textures/<path>.png`;
that is how Minecraft resolves a sprite name and there is no mod-specific escape from it.
The convention that breaks for 77% of this pack is `block id -> texture id`, which is
exactly why the dump carries the answer instead of deriving it. Once you *have* the texture
id, the file follows.
"""
import struct
import tempfile
import zipfile
import zlib
from pathlib import Path

# A texture that has never been drawn starts here. 16x16 because that is what a modpack is
# made of — of 381 textures sampled from eight real mods, 229 were exactly this size — and
# fully transparent because an opaque default is a decision about the art, made by the tool,
# that the artist would have to undo before starting.
DEFAULT_SIZE = 16

# Archives a texture can be read out of. A resource pack and a mod jar are both zips and are
# searched identically; a folder-shaped resource pack is the third case and needs no reader.
_ARCHIVE_SUFFIXES = (".jar", ".zip")


def texture_member(texture_id: str) -> str | None:
    """``"twigs:block/rocky_dirt"`` -> ``"assets/twigs/textures/block/rocky_dirt.png"``.

    The same shape as a jar member and as a path inside a resource pack, because it is
    literally the same string — which is what makes an override work at all (§6.5): a pack
    shadows a mod by putting a file at the identical path.

    An unqualified id takes the `minecraft` namespace, matching `ResourceLocation`. Returns
    None for anything that is not a texture id at all, rather than building a nonsense path
    out of it.
    """
    if not texture_id or not isinstance(texture_id, str):
        return None
    namespace, colon, path = texture_id.partition(":")
    # The separator, not the emptiness of `path` — `"twigs:"` is a namespace with nothing
    # after it, which is malformed, while `"block/stone"` is a bare path meaning vanilla.
    # Testing `not path` alone conflates them and turns the first into `minecraft:twigs`.
    if not colon:
        namespace, path = "minecraft", namespace
    if not namespace or not path or ".." in path:
        return None
    return f"assets/{namespace}/textures/{path}.png"


def blank_png(size: int = DEFAULT_SIZE) -> bytes:
    """A fully transparent RGBA PNG, encoded by hand.

    Hand-rolled rather than delegated because the alternatives are both worse than twenty
    lines: Pillow is a dependency earned by one call, and `QImage` would put a GUI import
    inside `packsmith.core`, which is a layering the rest of this package does not have.
    PNG's container is four length-tag-data-CRC chunks and `zlib` is in the standard
    library, so there is nothing here to get subtly wrong that a round-trip test misses.
    """
    if size < 1:
        raise ValueError(f"texture size must be positive, got {size}")

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    # bit depth 8, colour type 6 (RGBA), deflate, no filter, no interlace.
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    # Each row is prefixed with its filter byte — 0, "none". Every pixel is four zero bytes,
    # which for RGBA is transparent rather than black; the distinction is the whole point.
    raw = b"".join(b"\x00" + b"\x00" * (size * 4) for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def staged_copy(name: str, data: bytes, root=None) -> Path:
    """Write `data` to a temp file called `name`, ready to be put on the clipboard.

    **A file, because the clipboard carries a reference rather than the bytes.** Handing the
    OS a file to paste means there has to *be* a file, and most textures worth copying have
    none — a jar member is not a file, and neither is a texture a mod composited at load
    time. So one is made.

    **The name is the point, not an implementation detail.** Whatever this is called is what
    a paste produces on the other side, so `cut_abyssmarine.png` is the whole difference
    between this being useful and being a temp file with a number in it.

    **It is not deleted, and must not be.** The clipboard holds a path, so removing the file
    when this returns would leave the user pasting nothing. It is overwritten in place on a
    repeat copy of the same texture, which bounds the mess without a lifetime to manage.

    **The OS temp directory, never the user's tree.** §3.3.4 bans scratch files in a tracked
    root, and the reason applies exactly here — everything under one is compiled or packaged.
    A staging file for the clipboard is what a temp directory is for, and calling it one is
    honest rather than a loophole.
    """
    clean = str(name).replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not clean or clean in (".", ".."):
        raise ValueError(f"not a usable file name: {name!r}")
    staging = Path(root) if root else Path(tempfile.gettempdir()) / "packsmith-textures"
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / clean
    target.write_bytes(data)
    return target


class GeneratedPacks:
    """Textures a mod builds at load time, as dumped to `debug/generated_resource_pack`.

    **The problem this solves.** Some mods do not ship their textures at all — they composite
    them while the game loads. StoneZone is the case that forced this: it registers thousands
    of blocks and ships 25 PNGs, deriving the rest from the palettes of whatever mods are
    installed. The packdump records what those blocks render with, correctly, and the file it
    names exists nowhere on disk. So "show me this texture" and "start from the existing art"
    both failed for the mod whose blocks are most tedious to hand-edit.

    **Why this folder can be trusted.** It is not a guess or a cache — it is Moonlight
    writing out the exact bytes it just handed the resource manager, on the launch that
    produced them. Every mod built on Moonlight lands here, which on a real pack is 33 of
    them: EveryComp, Quark, Twigs, Supplementaries, Create and the rest.

    **It is debug output, so it may not be there.** An absent folder is an ordinary state and
    costs exactly what it did before this existed: the texture is not found and a blank one
    gets created instead. Nothing depends on it being present.

    The layout is `<namespace>/textures/...` — one folder per generated pack, and the
    `assets/` level a real resource pack has is simply not there, which is why this is a
    source of its own rather than another directory in the list.
    """

    def __init__(self, path):
        self.path = Path(path)

    def namespaces(self) -> set:
        try:
            return {child.name for child in self.path.iterdir() if child.is_dir()}
        except OSError:
            return set()

    def find(self, member: str):
        """``(bytes, the pack that answered)``, or ``(None, None)``.

        The pack subdirectory rather than this folder is reported, so a status line says
        "from stonezone" — which is the useful half — instead of naming a debug directory
        every generated texture in the pack has in common.
        """
        if not member.startswith("assets/"):
            return None, None
        namespace, _, rest = member[len("assets/"):].partition("/")
        if not rest:
            return None, None
        candidate = self.path / namespace / rest
        try:
            if candidate.is_file():
                return candidate.read_bytes(), self.path / namespace
        except OSError:
            pass
        return None, None


class TextureIndex:
    """Which of the pack's files holds a given texture, searched in resolution order.

    **The caller supplies the order and this does not guess it.** Resource packs shadow mods
    and later packs shadow earlier ones, but *which* packs are live and in what sequence is
    the pack loader's answer (§8.1) — a module that reinvented it here would drift from the
    one the game actually uses, and be wrong in the direction that matters: it would hand
    back the texture a mod shipped after the user had already replaced it.

    So `sources` is an ordered list of directories and archives, most-specific first, and
    the first hit wins.

    **Indexed by namespace, not by member.** A pack of 244 jars answers a single lookup in
    about half a second if every archive is opened, which is tolerable once and not tolerable
    per right-click. Every source declares which namespaces it contains in its top two path
    segments, so one pass over the directories turns every later lookup into a map hit
    against the handful of sources that could possibly answer.
    """

    def __init__(self, sources):
        # A source is either somewhere on disk or something that resolves its own members.
        # Coercing everything to `Path` was fine while only the first kind existed and is
        # what a folder-of-generated-packs cannot survive.
        self._sources = [s if hasattr(s, "find") else Path(s) for s in sources]
        self._by_namespace = None        # built on first use, not in __init__
        self._misses = set()

    def _namespaces_of(self, source) -> set:
        """Which `assets/<ns>` this source carries, without reading any file's content."""
        if hasattr(source, "namespaces"):
            return source.namespaces()
        found = set()
        try:
            if source.is_dir():
                assets = source / "assets"
                if assets.is_dir():
                    found = {p.name for p in assets.iterdir() if p.is_dir()}
            elif source.suffix.lower() in _ARCHIVE_SUFFIXES:
                with zipfile.ZipFile(source) as archive:
                    for name in archive.namelist():
                        parts = name.replace("\\", "/").split("/")
                        if len(parts) > 2 and parts[0] == "assets" and parts[1]:
                            found.add(parts[1])
        except (OSError, zipfile.BadZipFile):
            # A corrupt or vanished jar costs its own textures and nobody else's. Refusing
            # the whole index over one bad file would take the feature away from a pack
            # whose only fault is a half-downloaded mod.
            return set()
        return found

    def _build(self):
        self._by_namespace = {}
        for source in self._sources:
            for namespace in self._namespaces_of(source):
                self._by_namespace.setdefault(namespace, []).append(source)

    def sources_for(self, namespace: str) -> list:
        """Every source that could answer for this namespace, in resolution order."""
        if self._by_namespace is None:
            self._build()
        return list(self._by_namespace.get(namespace, ()))

    def find(self, member: str):
        """``(bytes, source)`` for the winning copy of `member`, or ``(None, None)``.

        The source comes back beside the bytes because "copied from Twigs" and "copied from
        the resource pack you already made" are different enough that a status line saying
        neither is a status line worth deleting.
        """
        if not member:
            return None, None
        parts = member.split("/")
        namespace = parts[1] if len(parts) > 2 and parts[0] == "assets" else None
        if namespace is None:
            return None, None
        for source in self.sources_for(namespace):
            # A source that resolves its own members reports where it found one, because
            # "which pack answered" is not always the entry in this list — a folder of
            # generated packs is one source holding many.
            if hasattr(source, "find"):
                data, where = source.find(member)
                if data is not None:
                    return data, where
                continue
            data = self._read(source, member)
            if data is not None:
                return data, source
        return None, None

    @staticmethod
    def _read(source: Path, member: str):
        try:
            if source.is_dir():
                candidate = source / member
                return candidate.read_bytes() if candidate.is_file() else None
            with zipfile.ZipFile(source) as archive:
                return archive.read(member)
        except (KeyError, OSError, zipfile.BadZipFile):
            return None
