"""Editor dispatch (design 6.0): which editor a file belongs to.

The rule that matters: a modpack instance is mostly NOT text. `mods/` is nothing but jars
and the browser lists every one of them, so opening a binary file is an ordinary thing a
user does, not an edge case — and it must never reach an editor that assumes UTF-8.
"""
import pytest

from packsmith.core import filetypes as ft


@pytest.mark.parametrize("name", [
    "config/foo.json", "a.json5", "b.toml", "server.properties", "scripts/x.js",
    "run.star", "pack.mcmeta", "notes.txt", "data/f.mcfunction",
])
def test_known_text_extensions_go_to_the_text_editor(name):
    assert ft.classify(name, b'{"hello": "world"}') == ft.TEXT


@pytest.mark.parametrize("name", ["mods/jei.jar", "x.zip", "pack.mrpack"])
def test_archives_go_to_the_jar_viewer(name):
    assert ft.classify(name, b"PK\x03\x04\x14\x00") == ft.ARCHIVE


@pytest.mark.parametrize("name", ["level.dat", "r.0.0.mca", "build.nbt", "x.dat_old"])
def test_nbt_extensions_go_to_the_nbt_editor(name):
    assert ft.classify(name, b"\x1f\x8b\x08\x00") == ft.NBT


def test_content_overrules_a_lying_extension():
    """A .json full of NUL bytes is not text however it's named."""
    assert ft.classify("config/evil.json", b"\x00\x01\x02binary") == ft.BINARY
    assert ft.classify("notes.txt", b"PK\x03\x04") == ft.ARCHIVE


def test_an_unlisted_text_file_still_opens():
    """The extension list is a fast path, not a gate — mods invent their own suffixes."""
    assert ft.classify("config/weird.somemodcfg", b"a = 1\nb = 2\n") == ft.TEXT


def test_a_png_is_binary():
    assert ft.classify("pack.png", b"\x89PNG\r\n\x1a\n\x00\x00") == ft.BINARY


def test_a_non_utf8_config_is_not_fed_to_the_text_editor():
    """The audit's quiet case: latin-1 configs exist and used to raise UnicodeDecodeError
    inside a Qt slot — no tab, no error, nothing."""
    assert ft.classify("config/legacy.cfg", "café".encode("latin-1")) == ft.BINARY


def test_a_multibyte_character_across_the_probe_boundary_is_not_binary():
    """A full probe window can cut a UTF-8 sequence in half; that proves nothing about the
    file, and calling it binary would hide a perfectly good config."""
    split = ("x" * (ft._PROBE_BYTES - 1) + "é").encode("utf-8")[:ft._PROBE_BYTES]
    assert len(split) == ft._PROBE_BYTES
    assert ft.classify("config/ok.json", split) == ft.TEXT


def test_a_short_non_utf8_file_gets_no_boundary_excuse():
    """That tolerance applies only to a TRUNCATED window. A whole small file that isn't
    UTF-8 is simply not UTF-8 — this is the latin-1 config the audit flagged."""
    assert ft.classify("config/legacy.cfg", "café".encode("latin-1")) == ft.BINARY


def test_classification_without_a_probe_still_keeps_jars_out_of_the_editor():
    assert ft.classify("mods/jei.jar") == ft.ARCHIVE
    assert ft.classify("level.dat") == ft.NBT
    assert ft.classify("config/x.json") == ft.TEXT


def test_unreadable_files_do_not_raise(tmp_path):
    assert ft.probe_file(tmp_path / "nope.bin") == b""


@pytest.mark.parametrize("kind", [ft.NBT, ft.ARCHIVE, ft.BINARY])
def test_every_unsupported_kind_explains_itself(kind):
    text = ft.describe(kind)
    assert text and not text.endswith("None")


def test_a_file_that_sniffs_clean_and_goes_bad_later_still_classifies_text():
    """Documents the limit of sniffing, and why the open path needs a guard behind it: the
    probe is the first 8KB, so bad bytes after that are invisible here."""
    probe = b"{" + b"a" * (ft._PROBE_BYTES - 1)
    assert ft.classify("config/late.json", probe) == ft.TEXT
