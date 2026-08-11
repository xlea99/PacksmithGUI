"""Reading archives without unpacking them — design 6.5.

§6.5's premise is that "peeking into a mod's internals currently requires an external tool
that extracts files to a scratch directory just to view them." So the rule here is that
**nothing is ever written to disk**: a zip's central directory lists every path, size and
timestamp without touching the compressed data, and member content is read into memory on
demand.

A `.jar` is a zip. So is `.mrpack`, and so is a resource pack. One reader covers all of
them, which is why this module is named for the format rather than for Minecraft's use of
it.
"""
import zipfile
from dataclasses import dataclass
from pathlib import Path

# Archives that turn up *inside* mod jars. Listed so the viewer can mark them; opening one
# in place is a later step, and the marker is what makes that discoverable when it lands.
NESTED_SUFFIXES = (".jar", ".zip")


class ArchiveError(Exception):
    """The archive could not be read — corrupt, truncated, or not an archive at all."""


@dataclass(frozen=True)
class ArchiveEntry:
    """One member of an archive. Sizes come from the directory, not from decompressing."""
    path: str                  # always forward-slashed, never leading-slashed
    size: int                  # uncompressed bytes
    compressed: int
    is_dir: bool

    @property
    def name(self) -> str:
        return self.path.rstrip("/").rsplit("/", 1)[-1]

    @property
    def is_nested_archive(self) -> bool:
        return not self.is_dir and self.path.lower().endswith(NESTED_SUFFIXES)

    @property
    def ratio(self) -> float:
        """Compressed size as a fraction of original. 1.0 when stored uncompressed."""
        return (self.compressed / self.size) if self.size else 1.0


def read_entries(archive_path) -> list[ArchiveEntry]:
    """Every member of the archive, sorted by path.

    **The file handle is closed before this returns.** On Windows an open handle locks the
    file, and a jar viewer that quietly stopped you from updating that mod would be a
    strange thing to have built — reading the directory is fast enough (a 144 MB jar takes
    about a millisecond) that keeping it open buys nothing.

    Directory entries are synthesised for any path that has children but no explicit entry
    of its own: plenty of zip writers omit them, and a tree built only from what the
    archive happens to declare would be full of holes.
    """
    path = Path(archive_path)
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as e:
        raise ArchiveError(f"{path.name} is not a readable archive: {e}") from e
    except OSError as e:
        raise ArchiveError(f"{path.name} could not be opened: {e}") from e

    entries = {}
    for info in infos:
        clean = info.filename.replace("\\", "/").lstrip("/")
        if not clean:
            continue
        is_dir = info.is_dir()
        entries[clean.rstrip("/")] = ArchiveEntry(
            path=clean.rstrip("/") + ("/" if is_dir else ""),
            size=info.file_size, compressed=info.compress_size, is_dir=is_dir)

    for key in list(entries):
        parent = key.rsplit("/", 1)[0] if "/" in key else ""
        while parent and parent not in entries:
            entries[parent] = ArchiveEntry(path=parent + "/", size=0, compressed=0,
                                           is_dir=True)
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""

    return sorted(entries.values(), key=lambda e: e.path)


def read_member(archive_path, member: str) -> bytes:
    """One member's bytes, read into memory. Never written to disk (§6.5)."""
    try:
        with zipfile.ZipFile(Path(archive_path)) as archive:
            return archive.read(member)
    except KeyError as e:
        raise ArchiveError(f"no entry '{member}' in {Path(archive_path).name}") from e
    except (zipfile.BadZipFile, OSError) as e:
        raise ArchiveError(f"could not read '{member}': {e}") from e


def summarise(entries: list[ArchiveEntry]) -> dict:
    """Headline numbers for the viewer's status line."""
    files = [e for e in entries if not e.is_dir]
    return {
        "files": len(files),
        "folders": sum(1 for e in entries if e.is_dir),
        "size": sum(e.size for e in files),
        "compressed": sum(e.compressed for e in files),
        "nested": sum(1 for e in files if e.is_nested_archive),
    }
