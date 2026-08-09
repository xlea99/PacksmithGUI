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
        self._migrate()
        log.info(f"UserDB connected: {db_path}")

    # Creates any/all tables that don't exist. Safe to call repeatedly
    def _ensure_tables(self):
        c = self._conn
        c.executescript("""
            -- Tag definitions: what tags exist and what type they are. Definitions are
            -- STRICTLY scoped to a single registry type (design 3.2.1): a `remove` tag on
            -- minecraft:item is a wholly separate definition from `remove` on minecraft:block —
            -- independent type, enum values, and default. Hence UNIQUE (registry_type, name).
            --
            -- `id` is the IDENTITY; `name` is only a label (design 3.2.1, "Identity, Naming,
            -- and References"). Everything internal points at the id, so renaming a tag is a
            -- one-row update that touches no assignment and no binding.
            CREATE TABLE IF NOT EXISTS tag_definitions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                registry_type   TEXT NOT NULL,
                name            TEXT NOT NULL,
                type            TEXT NOT NULL CHECK(type IN ('bool', 'string', 'enum', 'number')),
                default_value   TEXT,
                enum_values     TEXT,  -- JSON array, only used when type='enum', NULL otherwise
                UNIQUE (registry_type, name)
            );

            -- Tag assignments: which registry entries have which tag values.
            -- Every assignment has exactly one owner (see design 3.2.1). Ownership is a
            -- property of the row's EXISTENCE, not its value: no row = pristine (nobody
            -- owns it); a row always has an owner. owner_action_ref is package:action_id
            -- when owner_kind='action', otherwise NULL.
            -- Assignments reference the definition by ID, never by name — so a rename costs
            -- zero rows here. registry_type is deliberately absent: tag_id already implies it
            -- (a definition belongs to exactly one registry), so storing it again would be a
            -- second source of truth that could disagree.
            CREATE TABLE IF NOT EXISTS tag_assignments (
                tag_id           INTEGER NOT NULL REFERENCES tag_definitions(id) ON DELETE CASCADE,
                entry_id         TEXT NOT NULL,
                value            TEXT NOT NULL,  -- stored as text, cast on read based on tag type
                owner_kind       TEXT NOT NULL DEFAULT 'user' CHECK(owner_kind IN ('user', 'action')),
                owner_action_ref TEXT,
                PRIMARY KEY (tag_id, entry_id)
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

            -- Saved Views (design 3.2.3): a View is a named (query, renderer, renderer
            -- config) triple. The query is the serialized AST (design 3.2.4) — a query is
            -- DATA, which is exactly what makes a View storable, forkable, and shareable.
            -- `id` is a surrogate key so renaming is trivial and identity survives it;
            -- `renderer` is stored now (only one exists today) so adding the blueprint-node
            -- renderer later is additive rather than a migration.
            CREATE TABLE IF NOT EXISTS views (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL UNIQUE,
                query_json      TEXT NOT NULL,
                renderer        TEXT NOT NULL DEFAULT 'registry_table',
                renderer_config TEXT,
                position        INTEGER NOT NULL DEFAULT 0
            );

            -- File ownership: the OPEN-WORLD engine (design 6.0/6.1). Whole-file only
            -- for the MVP. `path` is relative to the instance root. Deliberately its
            -- own table, separate from L2 ownership — files have an uncontrolled
            -- external writer (the game, mod updates, the user in another tool), so
            -- this engine plays conservative.
            CREATE TABLE IF NOT EXISTS file_ownership (
                path             TEXT PRIMARY KEY,
                owner_kind       TEXT NOT NULL CHECK(owner_kind IN ('user', 'action')),
                owner_action_ref TEXT
            );

            -- Jobs: a user-authored, user-owned sequence of steps (design 3.3.2). Pure
            -- Layer 2 data — the same category of artifact as a view or a tag definition.
            -- Jobs never touch Starlark; they compose actions BY REFERENCE.
            CREATE TABLE IF NOT EXISTS jobs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL UNIQUE,
                default_on_error TEXT NOT NULL DEFAULT 'halt'
                                     CHECK(default_on_error IN ('halt', 'skip')),
                pinned           INTEGER NOT NULL DEFAULT 0,
                position         INTEGER NOT NULL DEFAULT 0
            );

            -- Steps carry ALL per-invocation state (design 3.3.2 "Model B"): bindings,
            -- config, and error policy live on the step, never on the action. One action
            -- can appear in dozens of steps with independent bindings.
            -- A step is either an action invocation or a reference to another job.
            CREATE TABLE IF NOT EXISTS job_steps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                position    INTEGER NOT NULL,
                kind        TEXT NOT NULL CHECK(kind IN ('action', 'job_ref')),
                action_ref  TEXT,          -- action steps: package:action_id
                ref_job_id  INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
                on_error    TEXT CHECK(on_error IN ('halt', 'skip')),  -- NULL = inherit job default
                bindings    TEXT,          -- JSON: slot -> tag name
                config      TEXT           -- JSON: param -> value
            );

            -- One row per job execution. job_name is denormalized so run history survives
            -- the job being deleted — history is a record of what happened, not a
            -- reference to what still exists.
            CREATE TABLE IF NOT EXISTS job_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
                job_name    TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                status      TEXT NOT NULL   -- success | partial | failed | running
            );

            -- Run history: one row per action step executed. rollback_data holds the
            -- inverse of everything the step committed (prior L2 values+owners, prior
            -- file contents+ownership), so a committed step can be reversed. Surfacing
            -- this in the UI is a later slice; the record is captured now.
            CREATE TABLE IF NOT EXISTS step_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                action_ref    TEXT NOT NULL,
                status        TEXT NOT NULL,   -- success | failed
                reason        TEXT,
                started_at    TEXT NOT NULL,
                finished_at   TEXT NOT NULL,
                rollback_data TEXT NOT NULL,   -- JSON: {"l2": [...], "files": {...}}
                log_output    TEXT NOT NULL    -- JSON: [[level, message], ...]
            );
        """)
        c.commit()

    # Idempotent, additive migrations for databases created before a column existed.
    # There's no migration framework yet (see design 9.2 live question) — for indev this
    # just brings older tables up to the current column set without destroying data.
    # New CHECK constraints can't be added via ALTER, so migrated columns rely on the
    # Python-side validation in the stores; fresh DBs still get the full CREATE constraints.
    def _migrate(self):
        # Link a recorded step back to the job run it belonged to (design 3.3.3). All
        # nullable: a standalone run (one action, no job) legitimately has none of them.
        self._add_column_if_missing("step_runs", "job_run_id", "INTEGER")
        self._add_column_if_missing("step_runs", "step_id", "INTEGER")
        self._add_column_if_missing("step_runs", "position_in_run", "INTEGER")
        # `flows` was a dead placeholder table, never used — drop it if an old DB has it.
        self._conn.execute("DROP TABLE IF EXISTS flows")
        self._conn.commit()

    def _add_column_if_missing(self, table: str, column: str, definition: str):
        existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self._conn.commit()
            log.info(f"Migrated {table}: added column '{column}'")

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
