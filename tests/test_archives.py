"""Reading archives — design 6.5.

The rule the whole section rests on: peeking into a jar must not extract anything. These
tests assert the reader never writes, never keeps the file locked, and produces a tree that
is complete even when the archive itself is missing directory entries.
"""
import zipfile

import pytest

from packsmith.core.archives import (
    ArchiveEntry, ArchiveError, read_entries, read_member, summarise)


def make_jar(path, members, *, dirs=()):
    # Deflated, like every real jar — `writestr` defaults to STORED, which would make the
    # compression figures the viewer shows meaningless in tests but not in the wild.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in dirs:
            archive.writestr(name if name.endswith("/") else name + "/", "")
        for name, content in members.items():
            archive.writestr(name, content)
    return path


@pytest.fixture
def jar(tmp_path):
    return make_jar(tmp_path / "testmod.jar", {
        "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n",
        "pack.mcmeta": '{"pack": {}}',
        "assets/testmod/textures/block/stone.png": b"\x89PNG fake",
        "com/example/testmod/Mod.class": b"\xca\xfe\xba\xbe",
    })


# --- listing --------------------------------------------------------------------------

def test_every_member_is_listed(jar):
    files = [e.path for e in read_entries(jar) if not e.is_dir]
    assert files == [
        "META-INF/MANIFEST.MF",
        "assets/testmod/textures/block/stone.png",
        "com/example/testmod/Mod.class",
        "pack.mcmeta",
    ]


def test_missing_directory_entries_are_synthesised(jar):
    """Plenty of zip writers store only files. A tree built from what the archive happens
    to declare would have holes where the intermediate folders should be."""
    folders = [e.path for e in read_entries(jar) if e.is_dir]
    assert folders == [
        "META-INF/", "assets/", "assets/testmod/", "assets/testmod/textures/",
        "assets/testmod/textures/block/", "com/", "com/example/", "com/example/testmod/",
    ]


def test_declared_directories_are_not_duplicated(tmp_path):
    jar = make_jar(tmp_path / "d.jar", {"a/b.txt": "x"}, dirs=["a"])
    assert [e.path for e in read_entries(jar) if e.is_dir] == ["a/"]


def test_sizes_come_from_the_directory_not_from_unpacking(tmp_path):
    jar = make_jar(tmp_path / "s.jar", {"big.txt": "x" * 5000})
    entry = next(e for e in read_entries(jar) if e.path == "big.txt")
    assert entry.size == 5000
    assert entry.compressed < entry.size, "stored uncompressed; ratio would be meaningless"
    assert 0 < entry.ratio < 1


def test_deep_paths_are_fine(tmp_path):
    """Real shaded jars nest 15 deep — `cloth-config` ships snakeyaml inside gdata inside
    itself. Nothing here should care."""
    deep = "/".join(f"level{i}" for i in range(15)) + "/Deep.class"
    jar = make_jar(tmp_path / "deep.jar", {deep: "x"})
    entries = read_entries(jar)
    assert deep in [e.path for e in entries]
    assert max(e.path.rstrip("/").count("/") for e in entries) == 15


def test_a_nested_jar_is_marked_but_not_recursed(tmp_path):
    jar = make_jar(tmp_path / "outer.jar", {"META-INF/jars/inner.jar": b"PK\x03\x04",
                                            "plain.txt": "x"})
    entries = {e.path: e for e in read_entries(jar)}
    assert entries["META-INF/jars/inner.jar"].is_nested_archive
    assert not entries["plain.txt"].is_nested_archive


def test_backslash_and_leading_slash_names_are_normalised(tmp_path):
    path = tmp_path / "odd.jar"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("/rooted.txt", "x")
        archive.writestr("windows\\style.txt", "y")
    paths = [e.path for e in read_entries(path) if not e.is_dir]
    assert paths == ["rooted.txt", "windows/style.txt"]


def test_an_empty_archive_reads_as_empty(tmp_path):
    assert read_entries(make_jar(tmp_path / "e.jar", {})) == []


# --- the two rules §6.5 rests on --------------------------------------------------------

def test_nothing_is_extracted_to_disk(tmp_path, jar):
    """The entire premise: viewing a jar must not litter a scratch directory."""
    before = {p for p in tmp_path.rglob("*")}
    read_entries(jar)
    read_member(jar, "pack.mcmeta")
    assert {p for p in tmp_path.rglob("*")} == before


def test_the_archive_is_not_left_locked(jar):
    """An open handle locks the file on Windows — a viewer that stopped you updating the
    mod you were looking at would be a strange thing to have built."""
    read_entries(jar)
    jar.unlink()               # would raise PermissionError on Windows if still open
    assert not jar.exists()


# --- reading one member ------------------------------------------------------------------

def test_a_member_reads_back_exactly(jar):
    assert read_member(jar, "pack.mcmeta") == b'{"pack": {}}'


def test_reading_a_missing_member_is_a_clean_error(jar):
    with pytest.raises(ArchiveError, match="no entry"):
        read_member(jar, "nope.txt")


# --- damaged input -----------------------------------------------------------------------

def test_a_corrupt_archive_raises_cleanly(tmp_path):
    """A jar half-downloaded by a launcher is a normal thing to double-click."""
    broken = tmp_path / "broken.jar"
    broken.write_bytes(b"PK\x03\x04 this is not really a zip")
    with pytest.raises(ArchiveError, match="not a readable archive"):
        read_entries(broken)


def test_a_missing_file_raises_cleanly(tmp_path):
    with pytest.raises(ArchiveError, match="could not be opened"):
        read_entries(tmp_path / "gone.jar")


# --- summary ------------------------------------------------------------------------------

def test_the_summary_counts_what_the_status_line_shows(jar):
    summary = summarise(read_entries(jar))
    assert summary["files"] == 4
    assert summary["folders"] == 8
    assert summary["nested"] == 0
    assert summary["size"] > 0
