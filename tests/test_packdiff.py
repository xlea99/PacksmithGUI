"""Reading a packdump comparison the way a person asks about it — design 3.1/4.1.

`compare()` answers in terms of which SIDE a thing was on. A review screen asks what the
update *did*. Translating between the two is one line of thought and the single easiest
thing to get backwards in this whole feature — a screen reporting 214 removals when 214
entries were added is wrong in a way nobody catches by eye, because both numbers look
plausible.

So the convention is asserted here rather than remembered:
    old.compare(new) -> only_in_self = REMOVED, only_in_other = ADDED
"""
import json
import zipfile

import pytest

from packsmith.core.packdiff import ModChange, summarise_diff, tags_at_risk
from packsmith.core.packdump import (
    Packdump, auto_adopt_enabled, compare_snapshots, previous_snapshot,
    revert_to_snapshot, snapshot_timeline)
from packsmith.core.profile import Profile
from packsmith.core.tags import TagStore

REG = "minecraft:item"


def write_dump(path, *, items=("minecraft:stone",), mods=(("alpha", "1.0"),),
               mc_version="1.20.1", loader_version="47.4.10", generated="2026-08-09T12:00:00+00:00",
               names=None):
    (path / "registries").mkdir(parents=True, exist_ok=True)
    (path / "attributes").mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({
        "type": "packsmith_full_dump", "schema_version": 1, "generated_at_utc": generated,
        "minecraft_version": mc_version, "loader": "forge", "loader_version": loader_version,
        "mods": [{"mod_id": m, "name": m.title(), "version": v} for m, v in mods],
        "registries": [{"type": REG, "file": "i.json", "count": len(items)}],
    }), encoding="utf-8")
    (path / "registries" / "i.json").write_text(
        json.dumps({"values": list(items)}), encoding="utf-8")
    (path / "attributes" / "localization.json").write_text(
        json.dumps({"locale": "en_us", "values": {REG: names or {}}}), encoding="utf-8")
    return path


def diff_between(tmp_path, **new_kwargs):
    old = Packdump.load(write_dump(tmp_path / "old"))
    new = Packdump.load(write_dump(tmp_path / "new", **new_kwargs))
    return old.compare(new)


# --- the direction, which is the whole point ---------------------------------------------

def test_entries_the_new_dump_added_are_reported_as_added(tmp_path):
    summary = summarise_diff(diff_between(
        tmp_path, items=("minecraft:stone", "minecraft:dirt", "minecraft:sand")))
    assert summary.entries_added == 2
    assert summary.entries_removed == 0
    assert summary.registries[0].added == ("minecraft:dirt", "minecraft:sand")


def test_entries_the_new_dump_lost_are_reported_as_removed(tmp_path):
    old = Packdump.load(write_dump(tmp_path / "old",
                                   items=("minecraft:stone", "minecraft:dirt")))
    new = Packdump.load(write_dump(tmp_path / "new", items=("minecraft:stone",)))
    summary = summarise_diff(old.compare(new))
    assert summary.entries_removed == 1
    assert summary.entries_added == 0
    assert summary.registries[0].removed == ("minecraft:dirt",)


def test_a_mod_that_arrived_reads_as_added(tmp_path):
    summary = summarise_diff(diff_between(
        tmp_path, mods=(("alpha", "1.0"), ("beta", "2.0"))))
    assert summary.mods == [ModChange(mod_id="beta", kind="added")]


def test_a_mod_that_left_reads_as_removed(tmp_path):
    old = Packdump.load(write_dump(tmp_path / "old",
                                   mods=(("alpha", "1.0"), ("beta", "2.0"))))
    new = Packdump.load(write_dump(tmp_path / "new", mods=(("alpha", "1.0"),)))
    assert summarise_diff(old.compare(new)).mods == [
        ModChange(mod_id="beta", kind="removed")]


def test_a_version_bump_reads_in_the_right_order(tmp_path):
    summary = summarise_diff(diff_between(tmp_path, mods=(("alpha", "2.0"),)))
    change = summary.mods[0]
    assert change.kind == "updated"
    assert (change.old_version, change.new_version) == ("1.0", "2.0")
    assert change.headline() == "1.0 → 2.0"


def test_identity_changes_are_reported_old_then_new(tmp_path):
    summary = summarise_diff(diff_between(tmp_path, loader_version="47.9.9"))
    assert summary.identity["loader_version"] == ("47.4.10", "47.9.9")


def test_renamed_display_names_are_counted(tmp_path):
    old = Packdump.load(write_dump(tmp_path / "old",
                                   names={"minecraft:stone": "Stone"}))
    new = Packdump.load(write_dump(tmp_path / "new",
                                   names={"minecraft:stone": "Rock"}))
    assert summarise_diff(old.compare(new)).renamed == 1


# --- summarising -------------------------------------------------------------------------

def test_an_identical_pair_summarises_as_empty(tmp_path):
    summary = summarise_diff(diff_between(tmp_path))
    assert summary.empty
    assert summary.headline() == "no changes"


def test_the_headline_leads_with_entry_counts(tmp_path):
    summary = summarise_diff(diff_between(
        tmp_path, items=("minecraft:stone", "minecraft:dirt"), mods=(("alpha", "2.0"),)))
    assert "+1 entries" in summary.headline()
    assert "1 mod" in summary.headline()


def test_a_registry_headline_shows_both_directions(tmp_path):
    old = Packdump.load(write_dump(tmp_path / "old",
                                   items=("minecraft:stone", "minecraft:dirt")))
    new = Packdump.load(write_dump(tmp_path / "new",
                                   items=("minecraft:stone", "minecraft:sand")))
    change = summarise_diff(old.compare(new)).registries[0]
    assert change.headline() == "+1 −1"
    assert change.total == 2


def test_a_whole_new_registry_is_not_mistaken_for_entry_changes(tmp_path):
    """A registry that only exists on one side has no per-entry diff, so it needs its own
    line or it vanishes from the report entirely."""
    old = Packdump.load(write_dump(tmp_path / "old"))
    new = Packdump.load(write_dump(tmp_path / "new"))
    raw = old.compare(new)
    raw.setdefault("registries", {})["only_in_other"] = ["minecraft:enchantment"]
    summary = summarise_diff(raw)
    assert summary.registries_added == ("minecraft:enchantment",)
    assert not summary.empty


# --- what it would cost the user ------------------------------------------------------------

def test_tags_at_risk_names_assignments_the_new_dump_would_orphan(user_db, tmp_path):
    """§4.1: the tab "surfaces orphaned tags at risk". Same derived check the Errors panel
    uses, pointed at a dump that may not be active yet — a warning, not a report."""
    tags = TagStore(user_db)
    tags.define(REG, "remove", "bool")
    tags.assign(REG, "minecraft:dirt", "remove", True)

    survives = Packdump.load(write_dump(tmp_path / "a",
                                        items=("minecraft:stone", "minecraft:dirt")))
    drops_it = Packdump.load(write_dump(tmp_path / "b", items=("minecraft:stone",)))

    assert tags_at_risk(tags, survives) == []
    at_risk = tags_at_risk(tags, drops_it)
    assert [o.entry_id for o in at_risk] == ["minecraft:dirt"]


# --- adopting, and undoing it ---------------------------------------------------------------

@pytest.fixture
def profile(tmp_path):
    root = tmp_path / "profile"
    root.mkdir()
    return Profile(name="p", root=root, mc_path=tmp_path / "instance",
                   mc_version="1.20.1", loader="forge", loader_version="47.4.10",
                   settings={})


def test_auto_adopt_is_the_default(profile):
    assert auto_adopt_enabled(profile) is True


def test_auto_adopt_can_be_turned_off(profile):
    """A setting rather than a constant, so a future settings menu has one thing to flip."""
    profile.settings["auto_adopt_packdump"] = False
    assert auto_adopt_enabled(profile) is False


def test_reverting_makes_an_archived_snapshot_active_again(profile, tmp_path):
    latest = profile.packdumps_dir / "latest"
    history = profile.packdumps_dir / "history"
    write_dump(history / "2026-01-01_00-00-00", items=("minecraft:stone",),
               generated="2026-01-01T00:00:00+00:00")
    write_dump(latest, items=("minecraft:stone", "minecraft:dirt"),
               generated="2026-08-09T12:00:00+00:00")

    result = revert_to_snapshot(profile, "2026-01-01_00-00-00")

    assert result.status == "imported"
    assert set(Packdump.load(latest).registry[REG]["values"]) == {"minecraft:stone"}


def test_reverting_archives_what_it_replaced(profile, tmp_path):
    """Otherwise reverting is a one-way door, which turns a review into a commitment."""
    latest = profile.packdumps_dir / "latest"
    history = profile.packdumps_dir / "history"
    write_dump(history / "2026-01-01_00-00-00", generated="2026-01-01T00:00:00+00:00")
    write_dump(latest, items=("minecraft:stone", "minecraft:dirt"),
               generated="2026-08-09T12:00:00+00:00")

    revert_to_snapshot(profile, "2026-01-01_00-00-00")

    archived = Packdump.load(history / "2026-08-09_12-00-00")
    assert "minecraft:dirt" in archived.registry[REG]["values"]


def test_reverting_to_something_that_is_not_there_says_so(profile):
    write_dump(profile.packdumps_dir / "latest")
    result = revert_to_snapshot(profile, "nope")
    assert result.status == "missing" and "nope" in result.reason


def test_a_revert_reports_what_changed(profile):
    latest = profile.packdumps_dir / "latest"
    history = profile.packdumps_dir / "history"
    write_dump(history / "2026-01-01_00-00-00", generated="2026-01-01T00:00:00+00:00")
    write_dump(latest, items=("minecraft:stone", "minecraft:dirt"),
               generated="2026-08-09T12:00:00+00:00")

    summary = summarise_diff(revert_to_snapshot(profile, "2026-01-01_00-00-00").diff)
    assert summary.entries_removed == 1, "reverting drops what the newer dump added"


# --- looking back through the history ------------------------------------------------------

@pytest.fixture
def history(profile):
    """Three snapshots plus an active one, deliberately NOT written in date order."""
    base = profile.packdumps_dir
    write_dump(base / "history" / "2026-03-01_00-00-00", items=("a", "b", "c"),
               generated="2026-03-01T00:00:00+00:00")
    write_dump(base / "history" / "2026-01-01_00-00-00", items=("a",),
               generated="2026-01-01T00:00:00+00:00")
    write_dump(base / "history" / "2026-02-01_00-00-00", items=("a", "b"),
               generated="2026-02-01T00:00:00+00:00")
    write_dump(base / "latest", items=("a", "b", "c", "d"),
               generated="2026-04-01T00:00:00+00:00")
    return profile


def test_the_timeline_runs_newest_first_and_includes_the_active_dump(history):
    assert [entry["name"] for entry in snapshot_timeline(history)] == [
        "latest", "2026-03-01_00-00-00", "2026-02-01_00-00-00", "2026-01-01_00-00-00"]


def test_the_timeline_marks_which_one_is_active(history):
    timeline = snapshot_timeline(history)
    assert timeline[0]["active"] is True
    assert not any(entry["active"] for entry in timeline[1:])


def test_ordering_follows_the_dump_timestamp_not_the_folder(history):
    """A revert rewrites folders, so file times record when PackSmith shuffled things
    around. Only the dump's own timestamp says when the pack looked like that."""
    base = history.packdumps_dir / "history"
    (base / "2026-01-01_00-00-00" / "meta.json").touch()      # newest on disk, oldest in fact
    assert snapshot_timeline(history)[-1]["name"] == "2026-01-01_00-00-00"


def test_the_previous_snapshot_is_the_one_before_it_in_time(history):
    assert previous_snapshot(history, "2026-03-01_00-00-00")["name"] == "2026-02-01_00-00-00"
    assert previous_snapshot(history, "latest")["name"] == "2026-03-01_00-00-00"


def test_the_oldest_snapshot_has_nothing_before_it(history):
    assert previous_snapshot(history, "2026-01-01_00-00-00") is None


def test_an_unknown_snapshot_has_no_previous(history):
    assert previous_snapshot(history, "not-a-snapshot") is None


def test_comparing_two_snapshots_reports_what_the_newer_one_added(history):
    summary = summarise_diff(
        compare_snapshots(history, "2026-01-01_00-00-00", "2026-02-01_00-00-00"))
    assert summary.entries_added == 1 and summary.entries_removed == 0
    assert summary.registries[0].added == ("b",)


def test_the_comparison_direction_cannot_be_supplied_backwards(history):
    """Older then newer, always — the argument order is the guard, so a caller cannot
    accidentally produce a confident report with every sign flipped."""
    forwards = summarise_diff(
        compare_snapshots(history, "2026-01-01_00-00-00", "2026-03-01_00-00-00"))
    backwards = summarise_diff(
        compare_snapshots(history, "2026-03-01_00-00-00", "2026-01-01_00-00-00"))
    assert forwards.entries_added == 2 and forwards.entries_removed == 0
    assert backwards.entries_removed == 2 and backwards.entries_added == 0


def test_comparing_against_something_that_is_not_there_says_which(history):
    with pytest.raises(ValueError, match="nope"):
        compare_snapshots(history, "nope", "latest")
