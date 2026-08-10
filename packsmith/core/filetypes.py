"""Which editor a file belongs to (design 6.0, Editor Dispatch).

§6.0: *"PackSmith picks the editor by file extension **and content sniffing**"* — known text
extensions to the Text Editor, NBT to the NBT Editor, archives to the JAR Viewer, and
*"unknown/binary → a 'cannot display' placeholder for formats the system doesn't
recognize."*

Two of those editors don't exist yet, which is sanctioned. What isn't sanctioned is their
file types reaching an editor that assumes UTF-8 — a modpack instance is *mostly* binary
(`mods/` is nothing but jars), and the file browser lists all of it, so "double-click a jar"
is an ordinary thing to do rather than an edge case.

Extension first, content second, because the extension is the author's declared intent and
the bytes are the check on it. A `.json` full of NUL bytes is not text however it's named.
"""
from pathlib import Path

TEXT = "text"
NBT = "nbt"
ARCHIVE = "archive"
BINARY = "binary"

# Deliberately broad: modpack configs use all of these, and several mods invent their own
# flavour of "it's really just JSON". Anything missing still gets sniffed, so an unlisted
# text file opens fine — the list is a fast path, not a gate.
TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".json5", ".jsonc", ".toml", ".cfg", ".conf", ".properties",
    ".yaml", ".yml", ".ini", ".snbt", ".js", ".ts", ".mjs", ".star", ".py", ".lua",
    ".xml", ".html", ".css", ".csv", ".tsv", ".log", ".mcmeta", ".mcfunction", ".lang",
    ".gitignore", ".editorconfig",
}
NBT_SUFFIXES = {".dat", ".dat_old", ".nbt", ".mca", ".mcr", ".schematic", ".litematic"}
ARCHIVE_SUFFIXES = {".jar", ".zip", ".mrpack", ".gz", ".tgz", ".7z", ".rar"}

_PROBE_BYTES = 8192


def classify(path, probe: bytes = None) -> str:
    """The editor family this file belongs to.

    ``probe`` is the first few KB, when the caller has them. Without it this falls back to
    the extension alone, which is enough to keep a jar out of a text editor.
    """
    suffix = Path(path).suffix.lower()
    if suffix in ARCHIVE_SUFFIXES:
        return ARCHIVE
    if suffix in NBT_SUFFIXES:
        return NBT
    if probe is not None:
        sniffed = sniff(probe)
        # The bytes overrule a text extension but never promote an unknown one past what
        # it looks like — a `.dat` that happens to be ASCII is still NBT's business.
        if sniffed is not None:
            return sniffed
    if suffix in TEXT_SUFFIXES:
        return TEXT
    return TEXT if probe is not None and _is_utf8_text(probe) else BINARY


def sniff(probe: bytes):
    """What the *content* says, or None when it says nothing definite."""
    if probe.startswith(b"PK\x03\x04") or probe.startswith(b"PK\x05\x06"):
        return ARCHIVE
    if probe.startswith(b"\x1f\x8b"):          # gzip — how Minecraft writes NBT
        return NBT
    if b"\x00" in probe:
        return BINARY                          # no text format embeds NUL
    if not _is_utf8_text(probe):
        return BINARY
    return None


def _is_utf8_text(probe: bytes) -> bool:
    try:
        probe.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        # A multi-byte character can straddle the end of the probe window, so a failure in
        # its final bytes proves nothing — but ONLY if the window was truncated. When the
        # probe is the whole file there is no boundary to blame, and forgiving it would
        # feed a latin-1 config to a UTF-8 editor, which is the bug this exists to stop.
        truncated = len(probe) >= _PROBE_BYTES
        return truncated and e.start >= len(probe) - 3


def probe_file(path, size: int = _PROBE_BYTES) -> bytes:
    """Read the sniffing window, or b"" if it can't be read at all."""
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def describe(kind: str) -> str:
    """What to tell the user about a file this build can't open."""
    return {
        NBT: "This is an NBT file. The NBT editor isn't built yet (design 6.4).",
        ARCHIVE: "This is an archive. The JAR viewer isn't built yet (design 6.5).",
        BINARY: "This file isn't text, so there's nothing to show yet — a hex view is "
                "part of the JAR viewer (design 6.5).",
    }.get(kind, "PackSmith can't display this file yet.")
