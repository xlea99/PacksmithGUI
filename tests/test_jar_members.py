"""Reading one file out of a jar — design 6.5, step two.

Two rules carry this whole step:

1. **Nothing is extracted.** §6.5 exists because peeking into a mod "requires an external
   tool that extracts files to a scratch directory just to view them"; a viewer that wrote
   its own scratch copy would have moved the problem rather than solved it.
2. **Everything is read-only, for one of two different reasons.** §6.3 separates
   "JAR-extracted non-overridable files — class files, META-INF/, mods.toml" from content
   that *could* be overridden. The first is permanent; the second is waiting on step three.
   Saying which is the difference between "you can't" and "you can't yet".
"""
import zipfile

import pytest

from packsmith.core.archives import ArchiveError
from packsmith.core.filetypes import BINARY, TEXT, classify
from packsmith.gui.editor.sources import (
    JarMemberSource, is_overridable, member_path, split_member)

CLASS_BYTES = b"\xca\xfe\xba\xbe\x00\x00\x00\x34"


@pytest.fixture
def instance(tmp_path):
    (tmp_path / "mods").mkdir()
    with zipfile.ZipFile(tmp_path / "mods" / "testmod.jar", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data/testmod/recipes/thing.json", '{"type": "minecraft:crafting"}')
        z.writestr("assets/testmod/lang/en_us.json", '{"item.testmod.thing": "Thing"}')
        z.writestr("com/example/Mod.class", CLASS_BYTES)
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        z.writestr("mods.toml", 'modId="testmod"\n')
    return tmp_path


@pytest.fixture
def source(instance):
    return JarMemberSource(instance)


def path_to(member):
    return member_path("mods/testmod.jar", member)


# --- addressing ---------------------------------------------------------------------------

def test_a_member_path_round_trips():
    path = member_path("mods/foo.jar", "assets/x.json")
    assert path == "mods/foo.jar!assets/x.json"
    assert split_member(path) == ("mods/foo.jar", "assets/x.json")


def test_a_path_without_a_separator_is_rejected():
    with pytest.raises(ValueError, match="not a jar member"):
        split_member("mods/foo.jar")


# --- reading ------------------------------------------------------------------------------

def test_a_text_member_reads_as_text(source):
    assert source.read(path_to("data/testmod/recipes/thing.json")) == \
        '{"type": "minecraft:crafting"}'


def test_nothing_is_written_to_disk(source, instance):
    """The rule the whole section exists for."""
    before = {p for p in instance.rglob("*")}
    source.read(path_to("assets/testmod/lang/en_us.json"))
    source.raw(path_to("com/example/Mod.class"))
    assert {p for p in instance.rglob("*")} == before


def test_the_jar_is_not_left_locked(source, instance):
    source.read(path_to("mods.toml"))
    (instance / "mods" / "testmod.jar").unlink()      # PermissionError if still open
    assert not (instance / "mods" / "testmod.jar").exists()


def test_a_binary_member_does_not_come_back_as_mojibake(source):
    """Better nothing than a wall of replacement characters presented as the file."""
    assert source.read(path_to("com/example/Mod.class")) is None


def test_raw_bytes_are_available_for_classifying(source):
    assert source.raw(path_to("com/example/Mod.class")) == CLASS_BYTES


def test_a_missing_member_reads_as_nothing_rather_than_raising(source):
    assert source.read(path_to("nope/gone.json")) is None


def test_a_member_path_cannot_escape_the_instance(source):
    with pytest.raises(ValueError, match="escapes"):
        source.raw(member_path("../../../etc/passwd.jar", "x"))


# --- classifying from the member's own bytes ------------------------------------------------

def test_members_are_classified_by_their_own_name_and_bytes(source):
    """The jar's extension says nothing about what is inside it: `.json` in there is text
    and `.class` is not, and only the member can answer."""
    for member, expected in (("data/testmod/recipes/thing.json", TEXT),
                             ("com/example/Mod.class", BINARY)):
        probe = source.raw(path_to(member))
        assert classify(member, probe) == expected


# --- read-only, and which kind of read-only -------------------------------------------------

def test_everything_in_a_jar_is_read_only(source):
    for member in ("data/testmod/recipes/thing.json", "com/example/Mod.class", "mods.toml"):
        assert source.read_only_reason(path_to(member)) is not None


def test_no_lock_here_can_be_lifted(source):
    """A provenance lock, not a claim — there is nothing to release."""
    assert source.can_unlock(path_to("data/testmod/recipes/thing.json")) is False


def test_writing_is_refused_and_says_why(source):
    with pytest.raises(ArchiveError, match="cannot be edited in place"):
        source.write(path_to("data/testmod/recipes/thing.json"), "{}")


def test_overridable_content_points_at_the_way_to_edit_it(source):
    """`data/` and `assets/` are what a datapack or resource pack replaces, so the answer
    here is not "no" but "not this copy" — and the message has to say which copy to edit
    rather than leaving the user to find it."""
    reason = source.read_only_reason(path_to("data/testmod/recipes/thing.json"))
    assert "Save as override" in reason
    assert "testmod.jar" in reason, "the reason should name the jar it came from"


@pytest.mark.parametrize("member", ["com/example/Mod.class", "META-INF/MANIFEST.MF",
                                    "mods.toml"])
def test_non_overridable_content_reads_as_permanent(source, member):
    """§6.3: these have "no meaningful override target" — not now, not after step three."""
    reason = source.read_only_reason(path_to(member))
    assert "no override target" in reason
    assert "Save as override" not in reason, "offered a fix that cannot work here"


@pytest.mark.parametrize("member, expected", [
    ("data/testmod/recipes/x.json", True),
    ("assets/testmod/lang/en_us.json", True),
    ("assets/testmod/Weird.class", False),      # a class is a class wherever it sits
    ("com/example/Mod.class", False),
    ("META-INF/MANIFEST.MF", False),
    ("mods.toml", False),
])
def test_what_counts_as_overridable(member, expected):
    assert is_overridable(member) is expected
