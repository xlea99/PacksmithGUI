"""Importing a packdump (design 3.1) — auto-adopt, but never silently.

The design call: **stale is the worse failure mode.** An unwanted import announces itself
the moment you look at anything; staleness produces confidently wrong output that looks
fine. So imports are automatic. What is *not* automatic is adopting a dump that fails the
profile's contract, because that isn't an update — it's a different game.

Note what the import never does: it does not touch Layer 2. Orphans are *detected* against
the new registry, never resolved, which is what makes reverting to an earlier snapshot an
honest operation rather than a promise we can't keep.
"""
import json

import pytest

from packsmith.core.packdump import (
    Packdump, check_packdump, current_packdump, import_packdump)
from packsmith.core.profile import Profile


def write_dump(path, *, mc_version="1.20.1", loader="forge", loader_version="47.4.10",
               items=("minecraft:stone", "minecraft:dirt"), mods=("alpha",),
               generated="2026-08-09T12:00:00+00:00"):
    (path / "registries").mkdir(parents=True, exist_ok=True)
    (path / "attributes").mkdir(parents=True, exist_ok=True)
    meta = {
        "type": "packsmith_full_dump", "schema_version": 1,
        "generated_at_utc": generated,
        "minecraft_version": mc_version, "loader": loader,
        "loader_version": loader_version,
        "mods": [{"mod_id": m, "name": m.title(), "version": "1.0"} for m in mods],
        "registries": [{"type": "minecraft:item", "file": "minecraft_item.json",
                        "count": len(items)}],
    }
    (path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (path / "registries" / "minecraft_item.json").write_text(
        json.dumps({"values": list(items)}), encoding="utf-8")
    (path / "attributes" / "localization.json").write_text(
        json.dumps({"locale": "en_us", "values": {}}), encoding="utf-8")
    return path


@pytest.fixture
def profile(tmp_path):
    root = tmp_path / "profile"
    root.mkdir()
    return Profile(name="p", root=root, mc_path=tmp_path / "instance",
                   mc_version="1.20.1", loader="forge", loader_version="47.4.10",
                   settings={"max_packdump_snapshot_count": 3})


@pytest.fixture
def source(tmp_path):
    return write_dump(tmp_path / "instance" / "packsmith")


def test_first_import_adopts_the_dump(profile, source):
    result = import_packdump(profile)
    assert result.status == "imported"
    assert current_packdump(profile) is not None


def test_an_identical_dump_is_a_no_op(profile, source):
    import_packdump(profile)
    result = import_packdump(profile)
    assert result.status == "unchanged"
    # nothing was archived — opening a profile repeatedly must not churn history
    assert not list((profile.packdumps_dir / "history").iterdir())


def test_a_swap_with_no_count_change_still_counts_as_changed(profile, source):
    """Equality decides whether an import happens at all, so it compares entry ids rather
    than entry counts. A config change that swaps one item for another must not read as
    'identical' — that is silent staleness, the thing this design exists to prevent."""
    import_packdump(profile)
    write_dump(source, items=("minecraft:stone", "minecraft:gravel"),
               generated="2026-08-09T13:00:00+00:00")

    result = import_packdump(profile)
    assert result.status == "imported"
    assert result.registry_delta() == (1, 1)


def test_a_changed_dump_reports_what_moved(profile, source):
    import_packdump(profile)
    write_dump(source, items=("minecraft:stone", "quark:rope"), mods=("alpha", "quark"),
               generated="2026-08-09T13:00:00+00:00")

    result = import_packdump(profile)
    assert result.status == "imported"
    assert result.registry_delta() == (1, 1)      # +quark:rope, -minecraft:dirt
    assert result.mod_delta() == (1, 0)


def test_a_version_bump_is_refused_and_latest_is_untouched(profile, source):
    """The whole point: adopting this would reinterpret every tag against another game."""
    import_packdump(profile)
    write_dump(source, mc_version="1.21.1", items=("minecraft:stone",),
               generated="2026-08-09T13:00:00+00:00")

    result = import_packdump(profile)
    assert result.status == "refused"
    assert result.errors["mc_version"]["actual"] == "1.21.1"
    assert current_packdump(profile).mc_version == "1.20.1"


def test_a_loader_change_is_refused(profile, source):
    import_packdump(profile)
    write_dump(source, loader="fabric", generated="2026-08-09T13:00:00+00:00")
    assert import_packdump(profile).status == "refused"


def test_a_refusal_is_stable_across_repeated_checks(profile, source):
    """It gets re-detected on every open and every window focus, so it has to stay a
    refusal rather than wear us down into importing it."""
    import_packdump(profile)
    write_dump(source, mc_version="1.21.1", generated="2026-08-09T13:00:00+00:00")
    for _ in range(3):
        assert check_packdump(profile).status == "refused"
        assert import_packdump(profile).status == "refused"
    assert current_packdump(profile).mc_version == "1.20.1"


def test_force_overrides_a_refusal(profile, source):
    import_packdump(profile)
    write_dump(source, mc_version="1.21.1", generated="2026-08-09T13:00:00+00:00")

    result = import_packdump(profile, force=True)
    assert result.status == "imported" and result.forced
    assert current_packdump(profile).mc_version == "1.21.1"


def test_a_new_forge_build_imports_with_only_an_info_issue(profile, source):
    import_packdump(profile)
    write_dump(source, loader_version="47.4.11", generated="2026-08-09T13:00:00+00:00")
    result = import_packdump(profile)
    assert result.status == "imported"
    assert result.issues["loader_version"]["level"] == "info"
    assert result.errors == {}


def test_check_does_not_mutate_anything(profile, source):
    """Focus-polling calls this constantly; it must never adopt or archive."""
    import_packdump(profile)
    write_dump(source, items=("minecraft:stone",), generated="2026-08-09T13:00:00+00:00")

    assert check_packdump(profile).status == "imported"      # i.e. "would import"
    assert current_packdump(profile).registry["minecraft:item"]["count"] == 2
    assert not list((profile.packdumps_dir / "history").iterdir())


def test_a_missing_dump_is_reported_not_raised(profile, tmp_path):
    """The instance may simply never have had the Forge mod run in it."""
    result = import_packdump(profile)
    assert result.status == "missing"
    assert "packsmith" in result.reason


def test_a_corrupt_dump_is_unreadable_not_missing(profile, source):
    """Distinct from missing on purpose: 'the mod never ran here' and 'the mod wrote
    garbage' send the user to completely different places."""
    import_packdump(profile)
    (source / "meta.json").write_text("{ not json", encoding="utf-8")
    result = import_packdump(profile)
    assert result.status == "unreadable"
    assert current_packdump(profile) is not None      # latest survives


def test_an_undeclared_profile_adopts_its_contract_from_the_first_dump(tmp_path, source):
    root = tmp_path / "blank"
    root.mkdir()
    blank = Profile(name="b", root=root, mc_path=tmp_path / "instance",
                    mc_version=None, loader=None, loader_version=None, settings={})
    blank.save = lambda: None

    result = import_packdump(blank)
    assert result.status == "imported"
    assert sorted(result.adopted) == ["loader", "loader_version", "mc_version"]
    assert blank.mc_version == "1.20.1"

    # ...and the contract now bites.
    write_dump(source, mc_version="1.21.1", generated="2026-08-09T13:00:00+00:00")
    assert import_packdump(blank).status == "refused"


def test_history_is_pruned_to_the_profile_setting(profile, source):
    import_packdump(profile)
    for hour in range(13, 19):
        write_dump(source, items=[f"mod:item{hour}"],
                   generated=f"2026-08-09T{hour}:00:00+00:00")
        assert import_packdump(profile).status == "imported"
    assert len(list((profile.packdumps_dir / "history").iterdir())) == 3
