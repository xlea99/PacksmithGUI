"""Snapshots of profile.db (packsmith/core/backup.py).

Nothing here is about backups working in the abstract — it is about the two ways this
protection would be worthless while looking present:

* **A snapshot taken too late.** `_rebuild` drops every table, and `_drop_stale_tables`
  drops four more. A copy taken after either faithfully preserves the damage. Ordering is
  the whole feature, so most of this file is about *when*.
* **A snapshot that quietly did not happen.** `snapshot` swallows errors on purpose — a
  backup that takes the app down is worse than a missing one — which means the one caller
  that must not proceed without a copy has to check the return, not assume it.

The data being protected is the expensive kind: a filled blueprint is thousands of human
judgments that no heuristic reproduces (design 5.3). Re-running an action costs seconds;
re-deriving that costs weeks.
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from packsmith.core import backup
from packsmith.core.db import RebuildRefused, UserDB


@pytest.fixture
def db(tmp_path):
    return UserDB(tmp_path / "profile.db")


def with_data(db):
    db.execute("INSERT INTO tag_definitions (registry_type, name, type) "
               "VALUES ('minecraft:item', 'remove', 'bool')")
    return db


def taken(db):
    return [b["reason"] for b in db.backups()]


# --- the copy is a database, not a file that resembles one ---------------------------

def test_a_snapshot_is_a_usable_database(db):
    with_data(db)
    made = db.snapshot("manual", force=True)

    copy = sqlite3.connect(str(made))
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT name FROM tag_definitions").fetchone()[0] == "remove"


def test_snapshots_live_beside_the_database(db, tmp_path):
    with_data(db)
    made = db.snapshot("manual", force=True)
    assert made.parent == tmp_path / "backups", \
        "a backup somewhere else is one nobody finds when they need it"


def test_an_empty_database_is_not_worth_keeping(tmp_path):
    """A copy of nothing is clutter that evicts real history."""
    fresh = UserDB(tmp_path / "profile.db")
    assert fresh.snapshot("open") is None
    assert fresh.backups() == []


# --- ordering: the part that is actually load-bearing ---------------------------------

def test_reopening_a_database_snapshots_it(tmp_path):
    path = tmp_path / "profile.db"
    with_data(UserDB(path))

    UserDB(path)                                   # the reopen

    saved = backup.list_backups(path)
    assert [b["reason"] for b in saved] == ["open"]
    copy = sqlite3.connect(str(saved[0]["path"]))
    assert copy.execute("SELECT COUNT(*) FROM tag_definitions").fetchone()[0] == 1


def test_the_open_snapshot_predates_the_tables_that_get_dropped(tmp_path):
    """The one destructive path with no backup of its own. `_drop_stale_tables` runs during
    __init__ and drops four blueprint tables outright — no mutation record, no rollback, no
    confirmation. Only the ORDER of the open snapshot stands between that and the data.

    Checked against a database whose tables actually get dropped, because a reopen that
    destroys nothing cannot tell a snapshot taken first from one taken last.
    """
    path = tmp_path / "profile.db"
    UserDB(path).close()

    raw = sqlite3.connect(str(path))               # the pre-3.2.2 shape: no `id` column
    raw.executescript("""
        DROP TABLE instance_bindings; DROP TABLE blueprint_instances;
        DROP TABLE blueprint_slots;   DROP TABLE blueprints;
        CREATE TABLE blueprints (name TEXT PRIMARY KEY, description TEXT);
        CREATE TABLE blueprint_slots (blueprint TEXT, name TEXT);
        CREATE TABLE blueprint_instances (blueprint TEXT, name TEXT);
        CREATE TABLE instance_bindings (instance TEXT, slot TEXT, value TEXT);
        INSERT INTO blueprints VALUES ('StoneType', 'the expensive one');
    """)
    raw.commit(); raw.close()

    db = UserDB(path)
    assert db.execute("SELECT COUNT(*) FROM blueprints").fetchone()[0] == 0,         "the drop is expected to have happened"

    saved = backup.list_backups(path)
    assert saved, "nothing was kept before four tables were dropped"
    copy = sqlite3.connect(str(saved[-1]["path"]))
    assert copy.execute("SELECT name FROM blueprints").fetchone()[0] == "StoneType",         "the snapshot holds the aftermath, not the data — it was taken too late"


def test_a_rebuild_leaves_the_data_behind_in_a_snapshot(tmp_path):
    """The regression that motivated all of this: drift was detected, `_rebuild` dropped
    every table on a log warning, and the profile's work was simply gone."""
    path = tmp_path / "profile.db"
    with_data(UserDB(path)).close()

    # Drift it exactly as a schema change would: a column the code expects, missing.
    raw = sqlite3.connect(str(path))
    raw.execute("ALTER TABLE tag_definitions RENAME COLUMN enum_values TO gone")
    raw.execute("PRAGMA user_version = 0")     # drift is only checked on a stale stamp
    raw.commit(); raw.close()

    db = UserDB(path)
    assert db.execute("SELECT COUNT(*) FROM tag_definitions").fetchone() is not None
    assert db._has_data() is False, "the rebuild is expected to have happened"

    saved = backup.list_backups(path)
    assert "before-rebuild" in [b["reason"] for b in saved]
    rescued = next(b for b in saved if b["reason"] == "before-rebuild")
    copy = sqlite3.connect(str(rescued["path"]))
    assert copy.execute("SELECT name FROM tag_definitions").fetchone()[0] == "remove", \
        "the snapshot must hold the data, not the aftermath"


def test_a_rebuild_that_cannot_be_backed_up_does_not_happen(tmp_path, monkeypatch):
    """`snapshot` swallows its own errors, so "it returned None" is the only signal that a
    copy was not made. Proceeding anyway would destroy the one thing worth keeping."""
    path = tmp_path / "profile.db"
    with_data(UserDB(path)).close()
    raw = sqlite3.connect(str(path))
    raw.execute("ALTER TABLE tag_definitions RENAME COLUMN enum_values TO gone")
    raw.execute("PRAGMA user_version = 0")
    raw.commit(); raw.close()

    monkeypatch.setattr(backup, "snapshot", lambda *a, **k: None)   # disk full, say
    with pytest.raises(RebuildRefused, match="left alone"):
        UserDB(path)

    survivor = sqlite3.connect(str(path))
    assert survivor.execute("SELECT name FROM tag_definitions").fetchone()[0] == "remove"


def test_rebuilding_an_empty_database_needs_no_ceremony(tmp_path):
    """Refusing here would brick a fresh profile over a drifted, worthless file."""
    path = tmp_path / "profile.db"
    UserDB(path).close()
    raw = sqlite3.connect(str(path))
    raw.execute("ALTER TABLE tag_definitions RENAME COLUMN enum_values TO gone")
    raw.execute("PRAGMA user_version = 0")
    raw.commit(); raw.close()

    UserDB(path)                                   # no raise
    assert backup.list_backups(path) == []


# --- coalescing, so asking often stays cheap ------------------------------------------

def test_a_burst_of_deletes_leaves_one_copy_of_the_state_before_it(db):
    """Forty snapshots of a demolition in progress would evict every useful one, and the
    newest — taken after thirty-nine deletes — is the least useful thing to keep."""
    with_data(db)
    first = db.snapshot("before-delete-instance")
    assert first is not None
    for _ in range(39):
        assert db.snapshot("before-delete-instance") is None

    assert len(db.backups()) == 1


def test_force_is_how_the_destructive_caller_opts_out(db):
    with_data(db)
    assert db.snapshot("manual", force=True) is not None
    assert db.snapshot("manual", force=True) is not None
    assert len(db.backups()) == 2


def test_two_snapshots_in_one_second_do_not_overwrite_each_other(db):
    """The names carry a whole-second timestamp. Same second, same name, and the second
    copy silently replaces the first — precisely the loss this module exists to prevent."""
    with_data(db)
    when = datetime(2026, 8, 18, 19, 45, 12)
    a = backup.snapshot(db._conn, db._path, "manual", force=True, now=when)
    b = backup.snapshot(db._conn, db._path, "manual", force=True, now=when)

    assert a != b
    assert a.exists() and b.exists()


# --- retention -------------------------------------------------------------------------

def test_old_snapshots_are_pruned_newest_first(tmp_path):
    db = with_data(UserDB(tmp_path / "profile.db", keep_backups=3))
    start = datetime(2026, 8, 18, 12, 0, 0)
    for minute in range(6):
        backup.snapshot(db._conn, db._path, f"n{minute}", keep=3, force=True,
                        now=start + timedelta(minutes=minute))

    assert taken(db) == ["n5", "n4", "n3"]


def test_a_file_that_cannot_say_when_it_was_taken_is_ignored(db):
    """Rather than guessed at — a mis-ordered prune deletes the wrong one."""
    with_data(db)
    db.snapshot("manual", force=True)
    (backup.backups_dir(db._path) / "handcopy.db").write_bytes(b"")

    assert [b["reason"] for b in db.backups()] == ["manual"]


# --- the operations that ask for one ---------------------------------------------------

def _blueprints(db):
    from packsmith.core.blueprints import BlueprintStore
    from tests.test_blueprints import FakeDump
    return BlueprintStore(db, packdump=FakeDump())


@pytest.mark.parametrize("op", ["instance", "blueprint", "tag"])
def test_the_destructive_operations_take_a_copy_first(tmp_path, op):
    """Each of these can erase hours of typing, and none of them is undoable — rollback is
    scoped to what an ACTION did, and these are what a person did."""
    db = UserDB(tmp_path / "profile.db")
    store = _blueprints(db)
    store.define("StoneType")
    store.add_slot("StoneType", "base", "registry", registry_type="minecraft:block")
    store.create_instance("StoneType", "granite")
    store.bind("StoneType", "granite", "base", "minecraft:granite")
    from packsmith.core.tags import TagStore
    tags = TagStore(db)
    tags.define("minecraft:item", "remove", "bool")

    for old in backup.list_backups(db._path):       # ignore the on-open one
        old["path"].unlink()

    if op == "instance":
        store.delete_instance("StoneType", "granite")
        expected = "before-delete-instance"
    elif op == "blueprint":
        store.delete("StoneType")
        expected = "before-delete-blueprint"
    else:
        tags.undefine("minecraft:item", "remove")
        expected = "before-delete-tag"

    saved = db.backups()
    assert [b["reason"] for b in saved] == [expected]
    copy = sqlite3.connect(str(saved[0]["path"]))
    assert copy.execute("SELECT COUNT(*) FROM instance_bindings").fetchone()[0] == 1, \
        "the copy has to predate the deletion"


# --- putting one back ------------------------------------------------------------------

def test_restoring_keeps_a_way_back_from_the_restore(tmp_path):
    """Restoring is itself destructive: the state being abandoned may be the one you wanted."""
    path = tmp_path / "profile.db"
    db = with_data(UserDB(path))
    saved = db.snapshot("manual", force=True)
    db.execute("INSERT INTO tag_definitions (registry_type, name, type) "
               "VALUES ('minecraft:item', 'hide', 'bool')")
    db.close()

    backup.restore(path, saved)

    live = sqlite3.connect(str(path))
    assert [r[0] for r in live.execute("SELECT name FROM tag_definitions")] == ["remove"]
    replaced = next(b for b in backup.list_backups(path) if b["reason"] == "before-restore")
    superseded = sqlite3.connect(str(replaced["path"]))
    assert "hide" in [r[0] for r in superseded.execute("SELECT name FROM tag_definitions")]
