import sqlite3
from contextlib import contextmanager
from pathlib import Path
from packsmith.common.logging import log


# Handles all reads/writes to a single profile's SQLite database.
# One profile = one .db file = one UserDB instance.
# This is the mailman — it delivers queries, it doesn't read the letters.
class UserDB:

    def __init__(self, db_path: Path):
        self._path = db_path
        self._depth = 0            # open `transaction()` scopes; see execute()
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.row_factory = sqlite3.Row  # rows always behave like dicts cuz this is the 21st century
        self._drop_stale_tables()
        self._ensure_tables()
        self._migrate()
        log.info(f"UserDB connected: {db_path}")

    # Tables whose *shape* changed incompatibly, which CREATE TABLE IF NOT EXISTS cannot
    # fix — it sees the name and does nothing, then later statements referencing the new
    # columns fail. Has to run BEFORE _ensure_tables, unlike the additive _migrate below.
    def _drop_stale_tables(self):
        # The original blueprint tables predated design 3.2.2 being written: no parent_id
        # (so no inline groups), slots keyed by name (so a rename would be a data
        # migration, which 3.2.2 forbids), and no per-binding ownership. Every one of them
        # is empty in every profile — they were never written to.
        columns = {row["name"] for row in
                   self._conn.execute("PRAGMA table_info(blueprints)")}
        if columns and "id" not in columns:
            for table in ("instance_bindings", "blueprint_instances",
                          "blueprint_slots", "blueprints"):
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            self._conn.commit()
            log.info("Dropped the pre-3.2.2 blueprint stub tables")

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

            -- Blueprints (design 3.2.2): an instantiable schema — "a struct, or a class
            -- without methods" — modelling relationships between registry entries that
            -- the game's flat registry doesn't formally connect (the stone palette
            -- problem).
            --
            -- `id` is the IDENTITY and `name` is only a label, the same split tags use
            -- (design 3.2.1). It is what makes 3.2.2's "rename is a metadata-only
            -- operation, not a remove-plus-add" true rather than aspirational: everything
            -- points at ids, so a rename touches exactly one row.
            CREATE TABLE IF NOT EXISTS blueprints (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT ''
            );

            -- The shape of a blueprint. `parent_id` gives 3.2.2's **inline groups** —
            -- "nesting depth is unlimited… at the DB level it's parent-child rows, but
            -- the user sees one cohesive schema definition."
            --
            -- Two kinds of row: a 'group' (structure only, holds no value) and a 'value'
            -- (a bindable slot). A value slot's type is spread across explicit columns
            -- rather than encoded into one string, so nothing has to parse
            -- 'list:registry:minecraft:block' back apart:
            --   scalar     -> type in (string, number, bool, enum); enum_values for enum
            --   registry   -> type='registry', registry_type='minecraft:block'
            --   blueprint  -> type='blueprint', ref_blueprint_id -> blueprints(id)
            --
            -- ref_blueprint_id is RESTRICT, not CASCADE: deleting a blueprint another
            -- schema points at would silently gut that schema. Schema-level CYCLES are
            -- explicitly legal (3.2.2) — a CladeNode whose `descendants` slot is a
            -- CladeNode is the point, not a bug.
            CREATE TABLE IF NOT EXISTS blueprint_slots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                blueprint_id    INTEGER NOT NULL REFERENCES blueprints(id) ON DELETE CASCADE,
                parent_id       INTEGER REFERENCES blueprint_slots(id) ON DELETE CASCADE,
                name            TEXT NOT NULL,
                kind            TEXT NOT NULL CHECK(kind IN ('group', 'value')),
                type            TEXT CHECK(type IN ('string', 'number', 'bool', 'enum',
                                                    'registry', 'blueprint')),
                registry_type   TEXT,
                ref_blueprint_id INTEGER REFERENCES blueprints(id) ON DELETE RESTRICT,
                enum_values     TEXT,   -- JSON array, only when type='enum'
                position        INTEGER NOT NULL DEFAULT 0
            );
            -- Sibling names must be unique, but SQLite treats NULLs as distinct in a
            -- UNIQUE constraint, so root-level slots need their own index to be covered.
            CREATE UNIQUE INDEX IF NOT EXISTS blueprint_slots_nested
                ON blueprint_slots(blueprint_id, parent_id, name) WHERE parent_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS blueprint_slots_root
                ON blueprint_slots(blueprint_id, name) WHERE parent_id IS NULL;

            -- A named instantiation: StoneType:granite, StoneType:andesite.
            -- `created_by` is 3.2.2's attributability ("this instance was created by
            -- mod_classifier:classify"); NULL means the user made it.
            -- `orphaned_by` is 3.2.2's orphan state: a destructive schema change (slot
            -- removal or retype) orphans the WHOLE instance, not just the affected
            -- binding, and "any action that touches a blueprint with orphaned instances
            -- refuses to run until the orphans are resolved." NULL means healthy.
            CREATE TABLE IF NOT EXISTS blueprint_instances (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                blueprint_id    INTEGER NOT NULL REFERENCES blueprints(id) ON DELETE CASCADE,
                name            TEXT NOT NULL,
                created_by      TEXT,
                orphaned_by     INTEGER REFERENCES blueprint_mutations(id) ON DELETE SET NULL,
                UNIQUE(blueprint_id, name)
            );

            -- One destructive schema change, kept so it can be undone. Revert is ONE step
            -- back by design decision, so at most one mutation per blueprint is ever
            -- outstanding: a blueprint with unresolved orphans refuses further destructive
            -- edits, because "snap back to the pre-mutation state" stops meaning anything
            -- once two of them are stacked.
            CREATE TABLE IF NOT EXISTS blueprint_mutations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                blueprint_id    INTEGER NOT NULL REFERENCES blueprints(id) ON DELETE CASCADE,
                kind            TEXT NOT NULL CHECK(kind IN ('remove_slot', 'retype_slot')),
                slot_path       TEXT NOT NULL,
                snapshot        TEXT NOT NULL,   -- JSON: slot subtree + affected bindings
                created_at      TEXT NOT NULL
            );

            -- Bindings a destructive change displaced. 3.2.2's "Preserve" resolution keeps
            -- them "in a limbo state… not exposed to actions but retained in the database,
            -- useful if the slot might return." A separate table is what makes "not
            -- exposed" structural rather than a flag every reader has to remember.
            CREATE TABLE IF NOT EXISTS blueprint_limbo (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id     INTEGER NOT NULL REFERENCES blueprint_instances(id) ON DELETE CASCADE,
                slot_path       TEXT NOT NULL,
                value           TEXT NOT NULL,
                owner           TEXT NOT NULL DEFAULT 'user',
                action_ref      TEXT,
                mutation_id     INTEGER REFERENCES blueprint_mutations(id) ON DELETE SET NULL
            );

            -- Which real entries fill which slots. `value` holds a registry id, a
            -- blueprint instance id, or a scalar, according to the slot's type.
            --
            -- Ownership is PER BINDING, not per instance (3.2.2): "an instance created by
            -- an action can have its bindings individually overwritten by the user,
            -- transferring ownership slot-by-slot." Same shape as tag_assignments, so the
            -- conflict policies in 3.3 apply unchanged.
            CREATE TABLE IF NOT EXISTS instance_bindings (
                instance_id     INTEGER NOT NULL REFERENCES blueprint_instances(id) ON DELETE CASCADE,
                slot_id         INTEGER NOT NULL REFERENCES blueprint_slots(id) ON DELETE CASCADE,
                value           TEXT NOT NULL,
                owner           TEXT NOT NULL DEFAULT 'user' CHECK(owner IN ('user', 'action')),
                action_ref      TEXT,
                PRIMARY KEY (instance_id, slot_id)
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
        # Orphan state arrived with blueprint schema evolution (design 3.2.2), after the
        # instances table already existed in some profiles.
        self._add_column_if_missing("blueprint_instances", "orphaned_by", "INTEGER")
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
        self._commit_unless_batched()
        return cursor
    # Execute a write query for many rows and commit.
    def execute_many(self, sql: str, params_list: list[tuple]):
        self._conn.executemany(sql, params_list)
        self._commit_unless_batched()

    def _commit_unless_batched(self):
        """Statement-at-a-time commits are the default, and inside `transaction()` they are
        exactly the bug: a loop of them is a loop of transactions, so a failure halfway
        through leaves the first half permanently applied."""
        if self._depth == 0:
            self._conn.commit()

    @contextmanager
    def transaction(self):
        """Make everything inside one all-or-nothing SQLite transaction.

        Design 3.3 promises "on failure the entire staging area is discarded; no real state
        is touched… A failed step therefore touches nothing, for free". Flushing a staging
        buffer through per-statement commits cannot keep that promise — this is what makes
        the flush a single COMMIT so the promise is structural rather than hopeful.

        Re-entrant: nested scopes join the outermost one, so a caller can wrap two stores
        that share a connection and get one transaction across both.
        """
        self._depth += 1
        try:
            yield self
        except Exception:
            self._depth -= 1
            if self._depth == 0:
                self._conn.rollback()
            raise
        self._depth -= 1
        if self._depth == 0:
            self._conn.commit()
    # Execute a read query and return a single row (or None).
    def fetch_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()
    # Execute a read query and return all rows.
    def fetch_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    # Releases the sqlite connection. Needed when switching profiles: on Windows an open
    # connection holds a file lock, so a profile you're still connected to can't be deleted
    # and its db can't be replaced. (There were two identical copies of this method; the
    # later definition silently won, so the docs on the earlier one described code that
    # never ran.)
    def close(self):
        self._conn.close()
        log.info(f"UserDB closed: {self._path}")
