"""Putting a PNG where the game will find it (design 6.1, 7.3).

Packsmith does not generate images and is not going to. What it does is place an authored
one correctly — the right namespace, the right folder, a record of who put it there — which
is the tedious half, and the half that has to survive a dry run, a diff, an ownership check
and a rollback like every other write.

Every failure guarded here is **silent**. A PNG is not text, and each layer of the file
engine assumed it was:

* the store read and wrote through a UTF-8 codec, so a texture came back mangled or blew up
  on a decode error far from the line that caused it;
* the change record carries content on both sides so a dry run can be diffed — a PNG there
  is noise the run report would try to render;
* rollback data is JSON in `step_runs`, and **bytes are not JSON**. That one does not fail
  at write time. It fails when you try to undo, which is exactly when you cannot afford it.
"""
import json
import struct
import zlib

import pytest

from packsmith.core.db import UserDB
from packsmith.core.files import FileStore, content_hash, decode_snapshot, encode_snapshot
from packsmith.core.history import StepRunStore, rollback_step
from packsmith.core.packages import PackageIndex
from packsmith.core.runner import run_action
from packsmith.core.tags import TagStore

DEST = "resourcepacks/mine/assets/deep_end/textures/block/coprolith_mosaic.png"


def png(seed=1) -> bytes:
    """A real PNG. Deliberately not `b"not utf-8"` — a genuine one carries a zlib block full
    of bytes no text codec will round-trip, which is the thing under test."""
    raw = b"".join(b"\x00" + bytes([r * seed % 256, g * 80, 120, 255] * 2)
                   for r, g in ((1, 2), (3, 1)))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture
def world(tmp_path):
    """An instance, and a package holding one texture and one action that places it."""
    instance = tmp_path / "instance"
    instance.mkdir()
    pkg = tmp_path / "packages" / "art"
    (pkg / "textures").mkdir(parents=True)
    (pkg / "textures" / "coprolith_mosaic.png").write_bytes(png())
    (pkg / "manifest.json5").write_text(
        '{\n  "package": {"name":"art","version":"0.1.0","author":"x","description":"d"},\n'
        '  "actions": [{"id":"place","file":"place.star","function":"place",\n'
        '               "name":"Place","description":"copy a png in","mappings":{}}]\n}\n',
        encoding="utf-8")
    (pkg / "place.star").write_text(
        f'DEST = "{DEST}"\n\n'
        'def place(pack):\n'
        '    pack.filesystem.resolve(DEST).copy_from("textures/coprolith_mosaic.png")\n',
        encoding="utf-8")

    db = UserDB(tmp_path / "p.db")
    index = PackageIndex()
    index.scan(pkg.parent)
    assert index.errors == {}, index.errors
    return {
        "instance": instance, "pkg": pkg, "db": db,
        "tags": TagStore(db), "files": FileStore(db, instance),
        "history": StepRunStore(db), "run": index.load_callable("art:place"),
        "dest": instance / DEST,
    }


def place(world, *, commit=True):
    return run_action(world["run"], tag_store=world["tags"], packdump=None,
                      action_ref="art:place", file_store=world["files"],
                      history=world["history"] if commit else None,
                      package_dir=world["pkg"], commit=commit)


# --- the bytes arrive intact ------------------------------------------------------------

def test_a_png_lands_byte_identical(world):
    """Through a UTF-8 codec it would not. That is the whole reason this exists."""
    assert place(world).status == "success"
    assert world["dest"].read_bytes() == png()


def test_it_is_owned_like_any_other_write(world):
    place(world)
    assert world["files"].ownership(DEST) == {"kind": "action", "action_ref": "art:place"}


def test_a_dry_run_writes_nothing(world):
    result = place(world, commit=False)
    assert result.status == "success"
    assert not world["dest"].exists()


# --- what the run report is told ---------------------------------------------------------

def test_the_change_describes_the_bytes_rather_than_carrying_them(world):
    """`changes()` carries content on both sides so a dry run can be diffed. A PNG cannot be
    diffed and does not survive the JSON the record travels through, so it is described."""
    change = place(world, commit=False).changes[0]

    assert change["kind"] == "added"
    assert change["after"]["binary"] is True
    assert change["after"]["size"] == len(png())
    assert "binary" in change["after"]["value"], "the raw bytes are in the record"
    json.dumps(change)          # must not raise


def test_the_hash_is_of_the_real_bytes(world):
    """So a diff you can trust is still distinguishable from one where the file has been
    edited since — the reason `after` carries a hash at all."""
    assert place(world, commit=False).changes[0]["after"]["hash"] == content_hash(png())


# --- rollback, where the JSON problem actually bites ---------------------------------------

def test_the_rollback_record_survives_being_stored(world):
    """Bytes are not JSON. This does not fail at write time — it fails when you undo."""
    result = place(world)
    stored = json.loads(world["history"].get(result.run_id)["rollback_data"])
    snap = stored["files"][f"minecraft::{DEST}"]
    assert snap["path"] == DEST and snap["root"] == "minecraft"


def test_rolling_back_a_created_file_removes_it(world):
    result = place(world)
    assert world["dest"].exists()

    rollback_step(result.run_id, tag_store=world["tags"], history=world["history"],
                  file_store=world["files"])

    assert not world["dest"].exists()


def test_rolling_back_an_overwrite_restores_the_original_bytes(world):
    """The case that a delete-on-rollback would silently destroy: a texture that was already
    there, replaced, and then undone."""
    original = png(seed=99)
    world["dest"].parent.mkdir(parents=True, exist_ok=True)
    world["dest"].write_bytes(original)

    result = place(world)
    assert world["dest"].read_bytes() == png()

    rollback_step(result.run_id, tag_store=world["tags"], history=world["history"],
                  file_store=world["files"])

    assert world["dest"].read_bytes() == original


def test_a_snapshot_round_trips_through_json(tmp_path):
    """The encode/decode pair, alone. They live beside each other because two files apart is
    how an encoder and a decoder stop agreeing."""
    raw = png()
    assert decode_snapshot(json.loads(json.dumps(encode_snapshot(raw)))) == raw
    assert encode_snapshot("plain text") == "plain text", "text is untouched"
    assert decode_snapshot("plain text") == "plain text"
    assert encode_snapshot(None) is None


# --- reading binary back -------------------------------------------------------------------

def test_the_store_reads_a_png_without_choking(world):
    """`read` is what `changes()` and the hard-block both call. It used to decode as UTF-8
    unconditionally, so merely *looking* at a texture raised."""
    place(world)
    assert world["files"].read(DEST) == png()


def test_text_files_still_come_back_as_text(world):
    """The sniff must not turn every file into bytes — everything else in the engine, and
    every existing action, expects a string."""
    world["files"].write("config/thing.json", '{"a": 1}\n', owner="user")
    assert world["files"].read("config/thing.json") == '{"a": 1}\n'


# --- the source is bounded -------------------------------------------------------------------

def test_a_source_outside_the_package_is_refused(world, tmp_path):
    """A package is content the user authored and version-controls with the action, which is
    why it needs no new trust boundary. Reading an arbitrary path on disk is a different
    question and belongs with tracked folders, not smuggled in through here."""
    secret = tmp_path / "secret.png"
    secret.write_bytes(png())
    handle = _handle(world)

    with pytest.raises(ValueError, match="escapes the package"):
        handle.copy_from("../../secret.png")


def test_a_missing_source_says_so(world):
    with pytest.raises(FileNotFoundError, match="not in the 'art' package"):
        _handle(world).copy_from("textures/nope.png")


def _handle(world):
    from packsmith.core.files import FileStaging
    from packsmith.core.pack import Pack
    pack = Pack(staging=None, tag_store=world["tags"], packdump=None,
                action_ref="art:place",
                file_staging=FileStaging(world["files"]), package_dir=world["pkg"])
    return pack.filesystem.resolve(DEST)
