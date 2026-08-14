"""Translation keys as an L1 attribute, and what a dump schema bump has to promise — 3.1.

A display name says what a thing is called. The **key** is what a resource pack has to
write in order to call it something else, and it cannot be reconstructed from the id:
`spawn:ant_pupa` resolves to `item.spawn.ant_pupa` while `spawn:anthill` resolves to
`block.spawn.anthill`, because a BlockItem defers to its block. Same registry, different
prefix, no rule to derive it from — so the mod harvests it from the running game, where
every override has already been applied.

The schema half matters as much as the feature. A snapshot on disk is an **archive**: you
cannot re-dump a pack as it was six months ago, so a bump that orphaned older snapshots
would be destroying the only copy of something.
"""
import json

import pytest

from packsmith.core.packdump import Packdump, DumpTooNewError, _CURRENT_SCHEMA

KEYS = {"minecraft:item": {"spawn:ant_pupa": "item.spawn.ant_pupa",
                           "spawn:anthill": "block.spawn.anthill"}}
NAMES = {"minecraft:item": {"spawn:ant_pupa": "Ant Pupa", "spawn:anthill": "Anthill"}}


def write_dump(path, *, keys=KEYS, names=NAMES, schema=_CURRENT_SCHEMA,
               items=("spawn:ant_pupa", "spawn:anthill")):
    (path / "registries").mkdir(parents=True, exist_ok=True)
    (path / "attributes").mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({
        "type": "packsmith_full_dump", "schema_version": schema,
        "generated_at_utc": "2026-08-13T12:00:00+00:00",
        "minecraft_version": "1.20.1", "loader": "forge", "loader_version": "47.4.13",
        "mods": [{"mod_id": "spawn", "name": "Spawn", "version": "1.0"}],
        "registries": [{"type": "minecraft:item", "file": "minecraft_item.json",
                        "count": len(items)}],
    }), encoding="utf-8")
    (path / "registries" / "minecraft_item.json").write_text(
        json.dumps({"values": list(items)}), encoding="utf-8")

    payload = {"locale": "en_us", "values": names}
    if keys is not None:
        payload["keys"] = keys
    (path / "attributes" / "localization.en_us.json").write_text(
        json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def dump(tmp_path):
    return Packdump.load(write_dump(tmp_path / "dump"))


# --- the attribute itself ------------------------------------------------------------------

def test_the_key_is_readable_as_an_attribute(dump):
    assert dump.attribute("minecraft:item", "spawn:ant_pupa", "localization_key") \
        == "item.spawn.ant_pupa"


def test_a_blockitem_reports_its_blocks_key(dump):
    """The case that motivated harvesting rather than deriving. Nothing about the id says
    this one is a `block.` — only the game knows."""
    assert dump.attribute("minecraft:item", "spawn:anthill", "localization_key") \
        == "block.spawn.anthill"


def test_the_name_and_the_key_are_separate_attributes(dump):
    assert dump.attribute("minecraft:item", "spawn:anthill", "localization") == "Anthill"
    assert dump.attribute("minecraft:item", "spawn:anthill", "localization_key") \
        == "block.spawn.anthill"


def test_an_unknown_attribute_is_none_not_an_error(dump):
    """`presence()` in the query catalog reads absence as None. Raising here would turn a
    filter on a field this dump lacks into a crash."""
    assert dump.attribute("minecraft:item", "spawn:anthill", "nonsense") is None


def test_an_entry_with_no_key_is_none(dump):
    assert dump.attribute("minecraft:item", "spawn:nothing", "localization_key") is None
    assert dump.attribute("minecraft:tomato", "spawn:anthill", "localization_key") is None


def test_the_attribute_list_is_discoverable():
    """What the view constructor offers as columns. Hardcoding `localization` at both ends
    is why adding the second one meant unpicking a special case."""
    assert Packdump.attribute_names() == ["localization", "localization_key"]


# --- old snapshots ---------------------------------------------------------------------------

def test_a_schema_1_snapshot_still_loads(tmp_path):
    """The promise. Every dump on disk today predates keys, and refusing them would make
    the bump destroy archives that cannot be regenerated."""
    dump = Packdump.load(write_dump(tmp_path / "old", keys=None, schema=1))
    assert dump.attribute("minecraft:item", "spawn:anthill", "localization") == "Anthill"


def test_an_old_snapshot_reports_no_keys_rather_than_pretending(tmp_path):
    """"None" cell by cell is indistinguishable from an entry that genuinely has no key.
    A job refusing to run needs to say which of the two it hit."""
    old = Packdump.load(write_dump(tmp_path / "old", keys=None, schema=1))
    assert old.attribute("minecraft:item", "spawn:anthill", "localization_key") is None
    assert not old.has_localization_keys()

    new = Packdump.load(write_dump(tmp_path / "new"))
    assert new.has_localization_keys()


def test_a_schema_1_snapshot_round_trips_as_schema_1(tmp_path):
    """Re-saving must not claim the mod harvested nothing — it was never asked. An empty
    `keys` block would be a newer reader's evidence that the answer was "none"."""
    Packdump.load(write_dump(tmp_path / "old", keys=None, schema=1)).save(tmp_path / "out")
    written = json.loads(
        (tmp_path / "out" / "attributes" / "localization.en_us.json").read_text("utf-8"))
    assert "keys" not in written


def test_keys_survive_a_round_trip(tmp_path):
    Packdump.load(write_dump(tmp_path / "src")).save(tmp_path / "out")
    reloaded = Packdump.load(tmp_path / "out")
    assert reloaded.attribute("minecraft:item", "spawn:anthill", "localization_key") \
        == "block.spawn.anthill"


def test_a_dump_from_the_future_is_refused_by_name(tmp_path):
    """Not merely "unsupported": a newer dump is not corrupt, it describes a shape this
    build has never seen, and reading it anyway silently drops whatever it added."""
    with pytest.raises(DumpTooNewError):
        Packdump.load(write_dump(tmp_path / "future", schema=_CURRENT_SCHEMA + 1))


def test_the_reader_trusts_the_file_over_its_version_number(tmp_path):
    """Feature-detected, not version-gated. A dump whose meta and attribute files disagree
    about their version is easy enough to produce by hand or by a half-finished mod build,
    and it should still load everything it genuinely has."""
    dump = Packdump.load(write_dump(tmp_path / "mixed", schema=1))
    assert dump.attribute("minecraft:item", "spawn:anthill", "localization_key") \
        == "block.spawn.anthill"


# --- the traps ------------------------------------------------------------------------------

def test_a_changed_key_makes_two_dumps_unequal(tmp_path):
    """The sharp one. A mod update that moves a descriptionId changes no id, no count and
    no display name — but it breaks every rename pointed at the old key. Left out of
    equality the dump reads as "identical", the import is skipped, and Packsmith keeps
    handing actions a key the game no longer answers to.
    """
    a = Packdump.load(write_dump(tmp_path / "a"))
    moved = {"minecraft:item": {**KEYS["minecraft:item"],
                                "spawn:anthill": "block.spawn.ant_hill"}}
    b = Packdump.load(write_dump(tmp_path / "b", keys=moved))

    assert a != b, "a moved translation key read as no change at all"


def test_a_changed_key_is_reported_separately_from_a_changed_name(tmp_path):
    """They mean different things to a reader: a changed name is cosmetic, a changed key
    silently invalidates anything written against the old one."""
    a = Packdump.load(write_dump(tmp_path / "a"))
    moved = {"minecraft:item": {**KEYS["minecraft:item"],
                                "spawn:anthill": "block.spawn.ant_hill"}}
    b = Packdump.load(write_dump(tmp_path / "b", keys=moved))

    diff = a.compare(b)
    assert "localization_keys" in diff
    changed = diff["localization_keys"]["changed"]["minecraft:item"]["changed"]
    assert changed["spawn:anthill"] == ("block.spawn.anthill", "block.spawn.ant_hill")
    assert "localizations" not in diff, "a key move was reported as a name change"


def test_identical_dumps_are_still_equal(tmp_path):
    """The other half — if keys made every comparison unequal, every launch would import."""
    assert Packdump.load(write_dump(tmp_path / "a")) \
        == Packdump.load(write_dump(tmp_path / "b"))


# --- the whole point: reaching it from an action ----------------------------------------------

def test_an_action_can_read_the_key(tmp_path, tags):
    """End to end through the real chain — Starlark binding, `Pack.attribute`, the dump —
    because harvesting a key nothing can reach is worth nothing.

    Against a real `Packdump` rather than the stub in `test_pack.py`, which reimplements
    the very `if name != "localization"` this replaced and would happily keep passing with
    the feature reverted.
    """
    from packsmith.core.pack import Pack
    from packsmith.core.staging import L2Staging
    from packsmith.core.starlark_runtime import run_starlark

    dump = Packdump.load(write_dump(tmp_path / "dump"))
    pack = Pack(staging=L2Staging(tags), tag_store=tags, packdump=dump,
                action_ref="rename:localize", mappings={}, config={})

    src = """
def run(pack):
    out = []
    for entry in pack.registry.entries("minecraft:item"):
        out.append(pack.registry.attribute("minecraft:item", entry, "localization_key"))
    return out
"""
    assert run_starlark(src, pack) == ["item.spawn.ant_pupa", "block.spawn.anthill"]
