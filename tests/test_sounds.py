"""From a sound event to its files.

The failures here are silent in the way that earns a permanent test: a merge that shadows
instead of merging plays the mod's original after the user's pack added a variant, and the
variant is simply never heard; a `replace` that is ignored plays sounds the pack meant to
remove; a reference followed the wrong way plays the wrong event under the right name. All
of them produce audio, so nothing errors.
"""
import json
import random
import zipfile

import pytest

from packsmith.core.launchers import asset_store_for
from packsmith.core.sounds import (
    SoundCatalog, SoundFile, choose, merge_sounds_json, sound_member, split_id,
)
from packsmith.core.textures import AssetObjects, TextureIndex


def make_jar(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data if isinstance(data, (bytes, str)) else json.dumps(data))
    return path


# --- id rules -----------------------------------------------------------------------------

def test_a_sound_name_becomes_the_ogg_path():
    assert sound_member("twigs:block/stone/step1") == "assets/twigs/sounds/block/stone/step1.ogg"


def test_an_unqualified_name_is_vanilla():
    assert sound_member("block/stone/step1") == "assets/minecraft/sounds/block/stone/step1.ogg"


def test_a_dotted_event_id_keeps_its_dots():
    """Event ids are dotted (`block.stone.step`) and sound names are slashed; the split
    must not turn one into the other, or every vanilla event misses its own key."""
    assert split_id("minecraft:block.stone.step") == ("minecraft", "block.stone.step")


@pytest.mark.parametrize("bad", ["", None, "twigs:", "twigs:../x"])
def test_nonsense_is_refused(bad):
    assert sound_member(bad) is None


# --- merging sounds.json ------------------------------------------------------------------

def test_a_later_document_adds_to_an_event_rather_than_replacing_it():
    """The game's rule and the one that matters: a resource pack that adds a fifth footstep
    variant does not delete the mod's four. A shadowing merge would play the pack's one
    file forever and the user would think the mod's were gone."""
    low = json.dumps({"step": {"sounds": ["m:a", "m:b"]}})
    high = json.dumps({"step": {"sounds": ["p:c"]}})
    assert merge_sounds_json([low, high])["step"] == ["m:a", "m:b", "p:c"]


def test_replace_wipes_what_was_beneath():
    low = json.dumps({"step": {"sounds": ["m:a", "m:b"]}})
    high = json.dumps({"step": {"sounds": ["p:c"], "replace": True}})
    assert merge_sounds_json([low, high])["step"] == ["p:c"]


def test_replace_only_touches_its_own_event():
    low = json.dumps({"step": {"sounds": ["m:a"]}, "break": {"sounds": ["m:z"]}})
    high = json.dumps({"step": {"sounds": ["p:c"], "replace": True}})
    merged = merge_sounds_json([low, high])
    assert merged == {"step": ["p:c"], "break": ["m:z"]}


def test_a_broken_document_costs_only_itself():
    good = json.dumps({"step": {"sounds": ["m:a"]}})
    assert merge_sounds_json(["{not json", good, "[]"])["step"] == ["m:a"]


# --- the catalog over an index ------------------------------------------------------------

def catalog_for(tmp_path, **jars):
    return SoundCatalog(TextureIndex([make_jar(tmp_path / f"{n}.jar", m) for n, m in jars.items()]))


def test_an_event_resolves_to_its_files_with_weights(tmp_path):
    cat = catalog_for(tmp_path, twigs={"assets/twigs/sounds.json": {
        "block.stone.step": {"sounds": ["twigs:block/step1",
                                        {"name": "twigs:block/step2", "weight": 3,
                                         "volume": 0.5, "pitch": 1.2}]}}})
    files = cat.files("twigs:block.stone.step")
    assert files == [SoundFile("assets/twigs/sounds/block/step1.ogg", 1, 1.0, 1.0),
                     SoundFile("assets/twigs/sounds/block/step2.ogg", 3, 0.5, 1.2)]


def test_a_pack_merges_over_the_mod_it_extends(tmp_path):
    """Index order is pack-first (most specific wins for textures); the catalog must walk
    it the other way so the pack's entries land on top and its `replace` is honoured."""
    pack = make_jar(tmp_path / "pack.zip", {"assets/twigs/sounds.json": json.dumps(
        {"step": {"sounds": ["pack:c"], "replace": True}})})
    jar = make_jar(tmp_path / "twigs.jar", {"assets/twigs/sounds.json": json.dumps(
        {"step": {"sounds": ["twigs:a", "twigs:b"]}})})
    cat = SoundCatalog(TextureIndex([pack, jar]))
    assert [f.member for f in cat.files("twigs:step")] == ["assets/pack/sounds/c.ogg"]


def test_an_event_reference_is_followed_and_its_tweaks_compound(tmp_path):
    cat = catalog_for(tmp_path, m={"assets/m/sounds.json": {
        "base": {"sounds": [{"name": "m:x", "volume": 0.5}]},
        "alias": {"sounds": [{"name": "m:base", "type": "event", "volume": 0.5, "pitch": 2.0}]}}})
    (only,) = cat.files("m:alias")
    assert only.member == "assets/m/sounds/x.ogg"
    assert (only.volume, only.pitch) == (0.25, 2.0)


def test_a_reference_across_namespaces_is_followed(tmp_path):
    cat = catalog_for(tmp_path,
                      a={"assets/a/sounds.json": {"e": {"sounds": [{"name": "b:f", "type": "event"}]}}},
                      b={"assets/b/sounds.json": {"f": {"sounds": ["b:the_file"]}}})
    assert [f.member for f in cat.files("a:e")] == ["assets/b/sounds/the_file.ogg"]


def test_a_reference_cycle_terminates(tmp_path):
    cat = catalog_for(tmp_path, m={"assets/m/sounds.json": {
        "a": {"sounds": [{"name": "m:b", "type": "event"}, "m:real"]},
        "b": {"sounds": [{"name": "m:a", "type": "event"}]}}})
    assert [f.member for f in cat.files("m:a")] == ["assets/m/sounds/real.ogg"]


def test_an_event_nobody_gave_sounds_is_empty(tmp_path):
    cat = catalog_for(tmp_path, m={"assets/m/sounds.json": {"other": {"sounds": ["m:x"]}}})
    assert cat.files("m:registered_in_code_only") == []


def test_a_namespace_with_no_sounds_json_is_empty_not_an_error(tmp_path):
    cat = catalog_for(tmp_path, m={"assets/m/textures/block/x.png": b"png"})
    assert cat.files("m:anything") == []


def test_the_merged_document_is_parsed_once_per_namespace(tmp_path):
    cat = catalog_for(tmp_path, m={"assets/m/sounds.json": {"e": {"sounds": ["m:x"]}}})
    calls = []
    real = cat.index.find_all
    cat.index.find_all = lambda member: (calls.append(member), real(member))[1]
    cat.files("m:e"); cat.files("m:e"); cat.files("m:other")
    assert calls == ["assets/m/sounds.json"]


def test_reading_a_file_goes_through_the_index(tmp_path):
    cat = catalog_for(tmp_path, m={"assets/m/sounds.json": {"e": {"sounds": ["m:x"]}},
                                   "assets/m/sounds/x.ogg": b"OggS"})
    data, source = cat.read(cat.files("m:e")[0])
    assert data == b"OggS" and source.name == "m.jar"


# --- choosing like the game ---------------------------------------------------------------

def test_choice_respects_weight():
    files = [SoundFile("a", weight=1), SoundFile("b", weight=9)]
    rng = random.Random(0)
    picks = [choose(files, rng).member for _ in range(2000)]
    assert 0.85 < picks.count("b") / len(picks) < 0.95


def test_choice_of_nothing_is_none():
    assert choose([], random.Random(0)) is None


# --- the launcher's asset store -----------------------------------------------------------

def store(tmp_path, objects):
    """A launcher layout: versions/<v>/<v>.jar + .json, assets/indexes/<id>.json, objects."""
    root = tmp_path / "Install"
    (root / "versions/1.20.1").mkdir(parents=True)
    jar = make_jar(root / "versions/1.20.1/1.20.1.jar", {"x": b""})
    (root / "versions/1.20.1/1.20.1.json").write_text(json.dumps({"assets": "5"}))
    index = {"objects": {}}
    for path, data in objects.items():
        digest = f"{abs(hash(path)) % 10**16:016x}" + "0" * 24
        index["objects"][path] = {"hash": digest, "size": len(data)}
        (root / "assets/objects" / digest[:2]).mkdir(parents=True, exist_ok=True)
        (root / "assets/objects" / digest[:2] / digest).write_bytes(
            data if isinstance(data, bytes) else data.encode())
    (root / "assets/indexes").mkdir(parents=True, exist_ok=True)
    (root / "assets/indexes/5.json").write_text(json.dumps(index))
    return jar


def test_the_store_is_found_from_the_jar_via_the_version_json(tmp_path):
    """The index id is NOT the game version — 1.20.1 uses `5` — so guessing `1.20.1.json`
    finds nothing and every vanilla sound is silently absent."""
    jar = store(tmp_path, {"minecraft/sounds/x.ogg": b"OggS"})
    objects, index = asset_store_for(jar)
    assert objects == tmp_path / "Install/assets/objects"
    assert index.name == "5.json"


def test_no_store_is_none(tmp_path):
    jar = make_jar(tmp_path / "lonely.jar", {"x": b""})
    assert asset_store_for(jar) is None


def test_the_store_answers_member_paths_like_any_source(tmp_path):
    jar = store(tmp_path, {"minecraft/sounds/block/step1.ogg": b"OggS",
                           "minecraft/sounds.json": json.dumps({"step": {"sounds": ["block/step1"]}})})
    source = AssetObjects(*asset_store_for(jar))
    assert source.namespaces() == {"minecraft"}
    data, where = source.find("assets/minecraft/sounds/block/step1.ogg")
    assert data == b"OggS"
    assert source.find("assets/minecraft/sounds/nope.ogg") == (None, None)


def test_vanilla_sounds_resolve_end_to_end_through_the_store(tmp_path):
    jar = store(tmp_path, {"minecraft/sounds/block/step1.ogg": b"OggS",
                           "minecraft/sounds.json": json.dumps({"block.stone.step": {"sounds": ["block/step1"]}})})
    index = TextureIndex([jar, AssetObjects(*asset_store_for(jar))])
    cat = SoundCatalog(index)
    (only,) = cat.files("minecraft:block.stone.step")
    assert cat.read(only)[0] == b"OggS"


def test_find_all_returns_every_copy_most_specific_first(tmp_path):
    member = "assets/m/sounds.json"
    pack = make_jar(tmp_path / "pack.zip", {member: b"pack"})
    jar = make_jar(tmp_path / "m.jar", {member: b"mod"})
    assert [d for d, _ in TextureIndex([pack, jar]).find_all(member)] == [b"pack", b"mod"]
