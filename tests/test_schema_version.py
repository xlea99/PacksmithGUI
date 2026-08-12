"""Stamping and checking the database schema — design 9.2's live question.

A profile's `profile.db` holds every tag, blueprint, view, job, ownership record and run in
the user's Layer 2. When the code's idea of those tables changes and a database on disk has
the old shape, something has to reconcile them — `CREATE TABLE IF NOT EXISTS` sees the name,
does nothing, and every query naming the new column then fails.

Additive reconciliation already existed (`_migrate` + `_add_column_if_missing`). What was
missing was the ability to *know what shape a database is*, and that is the one piece with a
deadline: real migration steps can be written the day a change needs them, but a database
holding work you care about that cannot say what version it is has permanently lost the
ability to be reasoned about.

Two rules carry the behaviour below, and they point in opposite directions on purpose:
**an older or unlabelled database is brought forward or rebuilt**, because indev data is
disposable and being stuck is worse; **a newer one is refused outright**, because opening it
would let old code write rows a newer schema won't understand, and rebuilding it would
destroy work a newer Packsmith did. Neither of those is recoverable.
"""
import sqlite3

import pytest

from packsmith.core.db import (
    SCHEMA_VERSION, SchemaTooNewError, UserDB, _drift_between, _shape_of)
from packsmith.core.tags import TagStore


def version_of(path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "profile.db"


def seed(path, tag="remove"):
    """A database with real user data in it, closed cleanly."""
    db = UserDB(path)
    TagStore(db).define("minecraft:item", tag, "bool")
    db.close()


# --- stamping ----------------------------------------------------------------------------

def test_a_fresh_database_is_stamped(db_path):
    db = UserDB(db_path)
    db.close()
    assert version_of(db_path) == SCHEMA_VERSION


def tag_names(db):
    return sorted(TagStore(db).definitions_for("minecraft:item"))


def unstamp(path, extra_sql=()):
    """Make a database look like one written before versioning existed."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA user_version = 0")
        for statement in extra_sql:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def test_reopening_is_a_no_op(db_path):
    """Opening happens constantly — every profile switch and packdump rebuild. It must not
    churn the schema or the data."""
    seed(db_path)
    db = UserDB(db_path)
    assert tag_names(db) == ["remove"]
    db.close()
    assert version_of(db_path) == SCHEMA_VERSION


def test_an_unstamped_but_current_database_is_adopted_not_destroyed(db_path):
    """The case that covers every profile in existence right now: correct shape, no stamp.
    Rebuilding those would be gratuitous — drift is about the shape, not the label."""
    seed(db_path)
    unstamp(db_path)

    db = UserDB(db_path)
    survived = tag_names(db)
    db.close()

    assert survived == ["remove"], "a perfectly good database was wiped for lacking a stamp"
    assert version_of(db_path) == SCHEMA_VERSION


def test_a_missing_table_is_recreated_rather_than_triggering_a_rebuild(db_path):
    """Subtle and worth pinning: `_ensure_tables` runs *before* the version check, so a
    dropped table is simply recreated and never reads as drift. That is right — the schema
    ends up correct, and nuking every other table because one was missing would destroy
    far more than the DROP already did.

    Which means drift is specifically **what the additive passes cannot fix**.
    """
    seed(db_path)
    unstamp(db_path, ["DROP TABLE views"])

    db = UserDB(db_path)
    try:
        assert tag_names(db) == ["remove"], "unrelated tables were wiped"
        assert "views" in _shape_of(db._conn)
    finally:
        db.close()


# --- drift detection ----------------------------------------------------------------------

def test_a_missing_column_is_drift(db_path):
    expected = {"tag_definitions": {"id", "name", "type"}}
    found = {"tag_definitions": {"id", "name"}}
    assert _drift_between(expected, found) == ["tag_definitions is missing type"]


def test_a_missing_table_is_drift():
    assert _drift_between({"views": {"id"}}, {}) == ["views is missing"]


def test_an_extra_column_is_not_drift():
    """One-directional on purpose. A leftover column is inert — nothing reads it — while a
    missing one breaks a query. Rebuilding a working database over a leftover would be
    destruction with no upside."""
    assert _drift_between({"t": {"a"}}, {"t": {"a", "b_from_an_old_build"}}) == []


def test_the_expected_shape_comes_from_the_code_not_a_list(db_path):
    """Built by running the real DDL into an in-memory database. A hand-maintained column
    list would be a second source of truth whose failure mode is the worst kind: the check
    passes while disagreeing with the schema it exists to check."""
    db = UserDB(db_path)
    try:
        assert _drift_between(db._expected_shape(), _shape_of(db._conn)) == []
    finally:
        db.close()


# --- rebuilding ----------------------------------------------------------------------------

# Real drift: a table that EXISTS with the wrong columns. `CREATE TABLE IF NOT EXISTS` sees
# the name and skips it, and `_migrate` only knows the columns someone thought to list — so
# this is the residue neither healing pass can reach, and precisely what `_drop_stale_tables`
# was hand-written for once before.
STALE_VIEWS = ["DROP TABLE views",
               "CREATE TABLE views (id INTEGER PRIMARY KEY, name TEXT)"]


def test_a_drifted_database_is_rebuilt(db_path):
    """Indev data is disposable; being unable to open a profile is not. The user's call:
    "if any of those existing profiles has drifted, nuke the whole db and rebuild"."""
    seed(db_path)
    unstamp(db_path, STALE_VIEWS)

    db = UserDB(db_path)
    try:
        assert _drift_between(db._expected_shape(), _shape_of(db._conn)) == []
        assert tag_names(db) == [], "expected a clean rebuild"
    finally:
        db.close()
    assert version_of(db_path) == SCHEMA_VERSION


def test_a_rebuild_leaves_a_working_database(db_path):
    """The point of rebuilding rather than refusing: you get a usable profile back."""
    seed(db_path)
    unstamp(db_path, STALE_VIEWS)

    db = UserDB(db_path)
    try:
        tags = TagStore(db)
        tags.define("minecraft:item", "fresh", "bool")
        tags.assign("minecraft:item", "minecraft:stone", "fresh", True)
        assert tags.get_tag("minecraft:item", "minecraft:stone", "fresh") is True
    finally:
        db.close()


def test_rebuilding_drops_leftover_tables_too(db_path):
    """A rebuild is a rebuild. Recreating the declared tables while leaving a stranger's
    behind would produce a database matching no version of anything."""
    seed(db_path)
    unstamp(db_path, STALE_VIEWS + ["CREATE TABLE something_ancient (x)"])

    db = UserDB(db_path)
    try:
        assert "something_ancient" not in _shape_of(db._conn)
    finally:
        db.close()


# --- the one case that refuses ----------------------------------------------------------------

def test_a_newer_database_is_refused_rather_than_opened(db_path):
    """Never destroy work a newer build did, and never let old code write into a schema it
    doesn't understand. Both are unrecoverable, so this stops instead of acting."""
    seed(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaTooNewError, match="newer Packsmith"):
        UserDB(db_path)


def test_a_refused_database_is_left_completely_untouched(db_path):
    """The refusal is only worth anything if the data is still there afterwards."""
    seed(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaTooNewError):
        UserDB(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM tag_definitions").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 5
    finally:
        conn.close()
