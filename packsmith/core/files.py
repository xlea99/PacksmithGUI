"""File access and whole-file ownership — the OPEN-WORLD engine (design 6.0/6.1).

Distinct from the Layer 2 tag engine on purpose. L2 data lives in PackSmith's own
database where PackSmith is the sole writer; files live on disk where an
uncontrolled external world (the game, mod updates, the user in another editor)
also writes them. Same ownership vocabulary (user / action / untouched), different
machinery.

MVP scope: whole-file ownership only. Per-key ownership, comment-preserving
round-trip parsers, and the content-addressed blob store are all deferred. Reads and
writes are plain UTF-8 text within the instance root; paths are stored relative to it.
"""
from pathlib import Path


class FileOwnershipError(Exception):
    """An action tried to write a file the user owns (design 6.1).

    The open-world engine does not negotiate: user-owned files are **hard-blocked**, not
    resolved by policy. The write raises, which fails the step and discards everything it
    staged — a clean stop the user can fix by releasing ownership, rather than a partially
    applied action that silently skipped a file it believed it had written.
    """


class FileStore:
    """Reads/writes files under the instance root and tracks whole-file ownership.

    The open-world cousin of TagStore. Paths are relative to ``instance_root`` and
    may not escape it. Writing stamps ownership; nothing here stages — staging is
    FileStaging's job, and it calls ``write`` only at commit time.
    """

    def __init__(self, db, instance_root):
        self._db = db
        self._root = Path(instance_root).resolve()

    def _abs(self, rel_path: str) -> Path:
        p = (self._root / rel_path).resolve()
        if p != self._root and not p.is_relative_to(self._root):
            raise ValueError(f"Path escapes the instance root: {rel_path}")
        return p

    # --- reads ---

    def read(self, rel_path: str):
        p = self._abs(rel_path)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def exists(self, rel_path: str) -> bool:
        return self._abs(rel_path).is_file()

    def ownership(self, rel_path: str):
        row = self._db.fetch_one(
            "SELECT owner_kind, owner_action_ref FROM file_ownership WHERE path = ?",
            (rel_path,),
        )
        if not row:
            return None  # untouched — no ownership record
        return {"kind": row["owner_kind"], "action_ref": row["owner_action_ref"]}

    @property
    def root(self) -> Path:
        return self._root

    def all_ownership(self) -> dict:
        """Every ownership record, keyed by path. The file browser needs the whole set at
        once — asking per file while walking a tree of thousands would be absurd."""
        return {
            row["path"]: {"kind": row["owner_kind"], "action_ref": row["owner_action_ref"]}
            for row in self._db.fetch_all(
                "SELECT path, owner_kind, owner_action_ref FROM file_ownership")
        }

    # --- ownership without writing (design 6.1) ---
    # §6.1 allows a file to become user-owned "either manually in the Text Editor or
    # through an explicit claim". These are that explicit path: they move a file between
    # untouched / user-owned / action-owned without touching its bytes.

    def claim(self, rel_path: str, *, owner: str = "user", owner_action_ref: str = None):
        """Record ownership of a file without modifying it."""
        if owner not in ("user", "action"):
            raise ValueError(f"Invalid owner: '{owner}'")
        if owner == "action" and not owner_action_ref:
            raise ValueError("Action ownership requires an owner_action_ref")
        self._abs(rel_path)      # keep the escape check honest even when not writing
        self._db.execute(
            """INSERT INTO file_ownership (path, owner_kind, owner_action_ref)
               VALUES (?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       owner_kind = excluded.owner_kind,
                       owner_action_ref = excluded.owner_action_ref""",
            (rel_path, owner, owner_action_ref),
        )

    def release(self, rel_path: str):
        """Drop the ownership record, returning the file to **untouched** — anyone may
        claim it again. The file on disk is untouched; only the claim goes away."""
        self._db.execute("DELETE FROM file_ownership WHERE path = ?", (rel_path,))

    # --- write (called at commit; captures prior bytes for store-by-path rollback) ---

    def write(self, rel_path: str, content: str, *, owner, owner_action_ref=None,
              file_must_exist: bool = False) -> str | None:
        """Write the whole file and record ownership. Returns the file's PRIOR
        content (or None if it didn't exist) so the caller can snapshot it for
        rollback. ``file_must_exist=True`` fails loudly if the file is missing."""
        p = self._abs(rel_path)
        if file_must_exist and not p.is_file():
            raise FileNotFoundError(f"Expected file to exist: {rel_path}")

        prior = p.read_text(encoding="utf-8") if p.is_file() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

        self._db.execute(
            """INSERT INTO file_ownership (path, owner_kind, owner_action_ref)
               VALUES (?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       owner_kind = excluded.owner_kind,
                       owner_action_ref = excluded.owner_action_ref""",
            (rel_path, owner, owner_action_ref),
        )
        return prior

    def delete(self, rel_path: str):
        """Remove the file from disk (if present) and drop its ownership record."""
        p = self._abs(rel_path)
        if p.is_file():
            p.unlink()
        self._db.execute("DELETE FROM file_ownership WHERE path = ?", (rel_path,))

    def restore(self, rel_path: str, prior_content, prior_ownership):
        """Return a file to a prior state (for rollback). A None prior_content means
        the file didn't exist before — so delete it; otherwise rewrite the old bytes
        and restore (or clear) the ownership record."""
        if prior_content is None:
            self.delete(rel_path)
            return
        p = self._abs(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(prior_content, encoding="utf-8")
        if prior_ownership:
            self._db.execute(
                """INSERT INTO file_ownership (path, owner_kind, owner_action_ref)
                   VALUES (?, ?, ?)
                       ON CONFLICT(path) DO UPDATE SET
                           owner_kind = excluded.owner_kind,
                           owner_action_ref = excluded.owner_action_ref""",
                (rel_path, prior_ownership["kind"], prior_ownership["action_ref"]),
            )
        else:
            self._db.execute("DELETE FROM file_ownership WHERE path = ?", (rel_path,))


class FileStaging:
    """Buffers whole-file writes for one action step over a FileStore.

    Nothing reaches disk until ``commit()``; ``discard()`` throws the buffer away.
    On commit, each file's prior content is captured (returned by FileStore.write)
    for store-by-path rollback — recorded by the runner into the step run.
    """

    def __init__(self, file_store: FileStore):
        self._store = file_store
        self._pending = {}   # rel_path -> {"content", "owner", "owner_action_ref", "file_must_exist"}
        self.snapshots = {}  # rel_path -> prior content (or None), populated at commit

    def write(self, rel_path, content, *, owner, owner_action_ref=None, file_must_exist=False):
        # Hard-block user-owned files (design 6.1) at STAGING time rather than commit, so
        # the failure points at the line that attempted it instead of surfacing later.
        if owner == "action":
            current = self._store.ownership(rel_path)
            if current is not None and current["kind"] == "user":
                raise FileOwnershipError(
                    f"'{rel_path}' is owned by you — actions are blocked from writing it. "
                    f"Release ownership in the Files panel to let '{owner_action_ref}' "
                    f"write it.")
        self._pending[rel_path] = {
            "content": content, "owner": owner, "owner_action_ref": owner_action_ref,
            "file_must_exist": file_must_exist,
        }

    def read(self, rel_path):
        if rel_path in self._pending:
            return self._pending[rel_path]["content"]
        return self._store.read(rel_path)

    def exists(self, rel_path):
        return rel_path in self._pending or self._store.exists(rel_path)

    def ownership(self, rel_path):
        staged = self._pending.get(rel_path)
        if staged is not None:
            return {"kind": staged["owner"], "action_ref": staged["owner_action_ref"]}
        return self._store.ownership(rel_path)

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def commit(self):
        for rel_path, staged in self._pending.items():
            prior_owner = self._store.ownership(rel_path)          # capture before overwrite
            prior_content = self._store.write(
                rel_path, staged["content"],
                owner=staged["owner"], owner_action_ref=staged["owner_action_ref"],
                file_must_exist=staged["file_must_exist"],
            )
            self.snapshots[rel_path] = {"content": prior_content, "ownership": prior_owner}
        self._pending.clear()

    def discard(self):
        self._pending.clear()
