import sqlite3
from pathlib import Path
from packsmith.common.logging import log


# Handles all reads/writes to a single profile's SQLite database.
# One profile = one .db file = one UserDB instance.
# This is the mailman — it delivers queries, it doesn't read the letters.
class UserDB:

    def __init__(self, db_path: Path):
        self._path = db_path
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.row_factory = sqlite3.Row  # rows always behave like dicts cuz this is the 21st century
        self._ensure_tables()
        log.info(f"UserDB connected: {db_path}")

    # Creates any/all tables that don't exist. Safe to call repeatedly
    def _ensure_tables(self):
        c = self._conn
        c.executescript("""
            -- Tag definitions: what tags exist and what type they are
            CREATE TABLE IF NOT EXISTS tag_definitions (
                name        TEXT PRIMARY KEY,
                type        TEXT NOT NULL CHECK(type IN ('bool', 'string', 'enum', 'number')),
                enum_values TEXT  -- JSON array, only used when type='enum', NULL otherwise
            );

            -- Tag assignments: which registry entries have which tag values
            CREATE TABLE IF NOT EXISTS tag_assignments (
                registry_type   TEXT NOT NULL,
                entry_id        TEXT NOT NULL,
                tag_name        TEXT NOT NULL REFERENCES tag_definitions(name) ON DELETE CASCADE,
                value           TEXT NOT NULL,  -- stored as text, cast on read based on tag type
                PRIMARY KEY (registry_type, entry_id, tag_name)
            );

            -- Blueprint definitions
            CREATE TABLE IF NOT EXISTS blueprints (
                name        TEXT PRIMARY KEY,
                description TEXT DEFAULT ''
            );

            -- Blueprint slots: the shape of a blueprint
            -- slot_type examples: 'string', 'number', 'bool',
            --   'registry:minecraft:block', 'registry:minecraft:item',
            --   'blueprint:stone_type',
            --   'list:registry:minecraft:block', 'list:blueprint:phylogeny_node'
            CREATE TABLE IF NOT EXISTS blueprint_slots (
                blueprint_name  TEXT NOT NULL REFERENCES blueprints(name) ON DELETE CASCADE,
                slot_name       TEXT NOT NULL,
                slot_type       TEXT NOT NULL DEFAULT 'string',
                PRIMARY KEY (blueprint_name, slot_name)
            );

            -- Blueprint instances: a named binding of a blueprint
            CREATE TABLE IF NOT EXISTS blueprint_instances (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                blueprint_name  TEXT NOT NULL REFERENCES blueprints(name) ON DELETE CASCADE,
                instance_name   TEXT NOT NULL,
                UNIQUE(blueprint_name, instance_name)
            );

            -- Instance bindings: which real entries fill which slots
            -- For list-typed slots, multiple rows share (instance_id, slot_name) with different positions
            -- value holds the actual data: a registry ID, blueprint instance name, string, number, etc.
            CREATE TABLE IF NOT EXISTS instance_bindings (
                instance_id     INTEGER NOT NULL REFERENCES blueprint_instances(id) ON DELETE CASCADE,
                slot_name       TEXT NOT NULL,
                value           TEXT NOT NULL,
                position        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (instance_id, slot_name, position)
            );

            -- View definitions: saved filter configurations
            CREATE TABLE IF NOT EXISTS views (
                name        TEXT PRIMARY KEY,
                config      TEXT NOT NULL  -- JSON blob of filter rules
            );

            -- Flow definitions: in-GUI automation wiring
            CREATE TABLE IF NOT EXISTS flows (
                name        TEXT PRIMARY KEY,
                description TEXT DEFAULT '',
                config      TEXT NOT NULL  -- JSON blob of steps/script references
            );
        """)
        c.commit()

    # Execute a write query (INSERT, UPDATE, DELETE) and commit.
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return cursor
    # Execute a write query for many rows and commit.
    def execute_many(self, sql: str, params_list: list[tuple]):
        self._conn.executemany(sql, params_list)
        self._conn.commit()
    # Execute a read query and return a single row (or None).
    def fetch_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()
    # Execute a read query and return all rows.
    def fetch_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    # Simply closes the connection to the db.
    def close(self):
        self._conn.close()
        log.info(f"UserDB closed: {self._path}")
