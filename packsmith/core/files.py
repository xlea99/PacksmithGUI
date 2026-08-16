"""File access and whole-file ownership — the OPEN-WORLD engine (design 6.0/6.1).

Distinct from the Layer 2 tag engine on purpose. L2 data lives in Packsmith's own
database where Packsmith is the sole writer; files live on disk where an
uncontrolled external world (the game, mod updates, the user in another editor)
also writes them. Same ownership vocabulary (user / action / untouched), different
machinery.

MVP scope: whole-file ownership only. Per-key ownership, comment-preserving
round-trip parsers, and the content-addressed blob store are all deferred. Reads and
writes are plain UTF-8 text within the instance root; paths are stored relative to it.
"""
import hashlib
import os
from pathlib import Path

from packsmith.core.staging import _side, classify


class FileOwnershipError(Exception):
    """An action tried to write a file the user owns (design 6.1).

    The open-world engine does not negotiate: user-owned files are **hard-blocked**, not
    resolved by policy. The write raises, which fails the step and discards everything it
    staged — a clean stop the user can fix by releasing ownership, rather than a partially
    applied action that silently skipped a file it believed it had written.
    """


def _like_prefix(key: str) -> str:
    """Escape a stored path key for use as a LIKE prefix.

    Filenames legitimately contain ``%`` and ``_`` — a mod called ``some_mod`` would
    otherwise match ``someXmod`` and take an unrelated file's ownership record with it.
    """
    return key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def content_hash(content: str) -> str:
    """A file's identity for reporting. Hashed from the exact bytes that get written, which
    is only a stable answer because writes no longer translate line endings."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_text(path) -> str:
    """Read text with newline translation OFF — see the note on `FileStore`'s reads."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write_text(path, content: str):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


class FileStore:
    """Reads/writes files under the instance root and tracks whole-file ownership.

    The open-world cousin of TagStore. Paths are relative to ``instance_root`` and
    may not escape it. Writing stamps ownership; nothing here stages — staging is
    FileStaging's job, and it calls ``write`` only at commit time.
    """

    def __init__(self, db, instance_root):
        self._db = db
        self._root = Path(instance_root).resolve()

    @staticmethod
    def key(rel_path: str) -> str:
        """The canonical identity of a path — **one disk file, one key**.

        Ownership used to be keyed on whatever string the writer happened to pass, so
        ``config/foo.json``, ``config\\foo.json`` and (on Windows) ``Config/foo.json`` were
        three rows for one file. §6.1's hard-block is an exact lookup, so that isn't a
        cosmetic split: a user claim under one spelling silently failed to block an action
        writing another, and rollback snapshots fragmented across the same keys.

        ``normcase`` is doing the platform-sensitive half deliberately: it lowercases on
        Windows, where the filesystem genuinely treats those as one file, and is the
        identity function on POSIX, where they are genuinely two. Case-folding
        unconditionally would merge distinct files on Linux.
        """
        text = str(rel_path).replace("\\", "/").strip("/")
        # Collapse "." and ".." without touching the disk, so the key of a path that
        # doesn't exist yet is still stable.
        parts = []
        for part in text.split("/"):
            if part in ("", "."):
                continue
            if part == ".." and parts and parts[-1] != "..":
                parts.pop()
                continue
            parts.append(part)
        text = "/".join(parts)
        # normcase is used to *detect* a case-insensitive filesystem, not to transform the
        # path: on Windows it also rewrites "/" to "\\", which would undo the separator
        # normalisation two lines up and make the stored key platform-specific. Keys must
        # be portable — a profile is a folder a user can move between machines.
        return text.lower() if os.path.normcase("A") == "a" else text

    def _abs(self, rel_path: str) -> Path:
        p = (self._root / rel_path).resolve()
        if p != self._root and not p.is_relative_to(self._root):
            raise ValueError(f"Path escapes the instance root: {rel_path}")
        return p

    # --- reads ---
    #
    # `newline=""` on every text read and write in this class, deliberately. Python's
    # default (`newline=None`) translates in BOTH directions: `\n` becomes `os.linesep` on
    # the way out and every ending becomes `\n` on the way back. Two consequences, both bad
    # here:
    #
    # * **It corrupts.** Writing "a\r\nb" puts `a\r\r\nb` on disk, which reads back as
    #   "a\n\nb" — the file gained a blank line nobody asked for.
    # * **It rewrites files an action barely touched.** A `.toml` with CRLF endings, read,
    #   one key changed, written back, comes out entirely LF — every line reported as
    #   modified. Mod configs are the canonical thing actions edit, and §6.1's whole-file
    #   ownership means we hand back what we were given plus the change.
    #
    # With translation off, a round trip is byte-exact and the file's own line endings
    # survive. See `tests/test_fidelity.py`.

    def read(self, rel_path: str):
        p = self._abs(rel_path)
        return _read_text(p) if p.is_file() else None

    def exists(self, rel_path: str) -> bool:
        return self._abs(rel_path).is_file()

    def ownership(self, rel_path: str):
        row = self._db.fetch_one(
            "SELECT owner_kind, owner_action_ref FROM file_ownership WHERE path = ?",
            (self.key(rel_path),),
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
            (self.key(rel_path), owner, owner_action_ref),
        )

    def release(self, rel_path: str):
        """Drop the ownership record, returning the file to **untouched** — anyone may
        claim it again. The file on disk is untouched; only the claim goes away."""
        self._db.execute("DELETE FROM file_ownership WHERE path = ?",
                         (self.key(rel_path),))

    # --- write (called at commit; captures prior bytes for store-by-path rollback) ---

    def write(self, rel_path: str, content: str, *, owner, owner_action_ref=None,
              file_must_exist: bool = False) -> str | None:
        """Write the whole file and record ownership. Returns the file's PRIOR
        content (or None if it didn't exist) so the caller can snapshot it for
        rollback. ``file_must_exist=True`` fails loudly if the file is missing."""
        p = self._abs(rel_path)
        if file_must_exist and not p.is_file():
            raise FileNotFoundError(f"Expected file to exist: {rel_path}")

        prior = _read_text(p) if p.is_file() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_text(p, content)

        self._db.execute(
            """INSERT INTO file_ownership (path, owner_kind, owner_action_ref)
               VALUES (?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       owner_kind = excluded.owner_kind,
                       owner_action_ref = excluded.owner_action_ref""",
            (self.key(rel_path), owner, owner_action_ref),
        )
        return prior

    def write_bytes(self, rel_path: str, content: bytes, *, owner,
                    owner_action_ref=None) -> bytes | None:
        """The binary sibling of :meth:`write`, with the same ownership semantics.

        Text is the common case in a modpack but not the only one — a resource-pack
        override is a PNG, an NBT structure is a gzipped tag tree — and routing those
        through the text path either mangles them or raises `UnicodeDecodeError` while
        *capturing the prior content for rollback*, which is a confusing place to fail.

        Returns the prior bytes so a caller can snapshot them, exactly as ``write`` does.
        """
        p = self._abs(rel_path)
        prior = p.read_bytes() if p.is_file() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        self._db.execute(
            """INSERT INTO file_ownership (path, owner_kind, owner_action_ref)
               VALUES (?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       owner_kind = excluded.owner_kind,
                       owner_action_ref = excluded.owner_action_ref""",
            (self.key(rel_path), owner, owner_action_ref),
        )
        return prior

    def read_bytes(self, rel_path: str) -> bytes | None:
        p = self._abs(rel_path)
        return p.read_bytes() if p.is_file() else None

    def delete(self, rel_path: str):
        """Remove the file from disk (if present) and drop its ownership record."""
        p = self._abs(rel_path)
        if p.is_file():
            p.unlink()
        self._db.execute("DELETE FROM file_ownership WHERE path = ?",
                         (self.key(rel_path),))

    def rename(self, rel_path: str, new_rel_path: str):
        """Move a file or directory, taking its ownership records with it.

        Ownership belongs to the artifact, not to the string naming it — a file an action
        manages is still that action's file after the user renames it. Dropping the record
        instead would silently launder an action-owned file into an untouched one, which
        is exactly the transition §6.1 refuses to make quietly.
        """
        src, dst = self._abs(rel_path), self._abs(new_rel_path)
        if not src.exists():
            raise FileNotFoundError(f"No such file: {rel_path}")
        if dst.exists():
            raise FileExistsError(f"Already exists: {new_rel_path}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

        old_key, new_key = self.key(rel_path), self.key(new_rel_path)
        # A directory rename moves every record beneath it too. The trailing separator
        # keeps "cfg" from also matching "cfgold/x.json".
        self._db.execute(
            """UPDATE file_ownership
                  SET path = ? || substr(path, ?)
                WHERE path LIKE ? ESCAPE '\\'""",
            (new_key, len(old_key) + 1, _like_prefix(old_key) + "/%"))
        self._db.execute("UPDATE file_ownership SET path = ? WHERE path = ?",
                         (new_key, old_key))

    def delete_tree(self, rel_path: str):
        """Recursively remove a directory and forget everything owned beneath it."""
        import shutil
        p = self._abs(rel_path)
        if p.is_dir():
            shutil.rmtree(p)
        self._db.execute("DELETE FROM file_ownership WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                         (self.key(rel_path), _like_prefix(self.key(rel_path)) + "/%"))

    def owned_under(self, rel_path: str) -> dict:
        """Ownership records at or beneath a path — what a recursive delete would destroy."""
        key = self.key(rel_path)
        return {row["path"]: {"kind": row["owner_kind"], "action_ref": row["owner_action_ref"]}
                for row in self._db.fetch_all(
                    "SELECT path, owner_kind, owner_action_ref FROM file_ownership "
                    "WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                    (key, _like_prefix(key) + "/%"))}

    def restore(self, rel_path: str, prior_content, prior_ownership):
        """Return a file to a prior state (for rollback). A None prior_content means
        the file didn't exist before — so delete it; otherwise rewrite the old bytes
        and restore (or clear) the ownership record."""
        if prior_content is None:
            self.delete(rel_path)
            return
        p = self._abs(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Same rule as `write`, and it matters most here: a rollback that re-translated
        # line endings would restore a file that is not the file it snapshotted.
        _write_text(p, prior_content)
        if prior_ownership:
            self._db.execute(
                """INSERT INTO file_ownership (path, owner_kind, owner_action_ref)
                   VALUES (?, ?, ?)
                       ON CONFLICT(path) DO UPDATE SET
                           owner_kind = excluded.owner_kind,
                           owner_action_ref = excluded.owner_action_ref""",
                (self.key(rel_path), prior_ownership["kind"],
                 prior_ownership["action_ref"]),
            )
        else:
            self._db.execute("DELETE FROM file_ownership WHERE path = ?",
                         (self.key(rel_path),))


class FileStaging:
    """Buffers whole-file writes for one action step over a FileStore.

    Nothing reaches disk until ``commit()``; ``discard()`` throws the buffer away.
    On commit, each file's prior content is captured (returned by FileStore.write)
    for store-by-path rollback — recorded by the runner into the step run.
    """

    def __init__(self, file_store: FileStore, log=None):
        self._store = file_store
        self._log = log or (lambda level, message: None)
        # Keyed by the STORE's canonical key, not the caller's spelling. Staging under two
        # spellings of one file in a single step otherwise produces two pending writes and
        # two rollback snapshots — and the second snapshot captures the FIRST write's
        # content, so rolling back would restore mid-step state rather than the prior file.
        self._pending = {}   # key -> {"path", "content", "owner", "owner_action_ref", …}
        self._touched = set()   # written by the current step — see `L2Staging._touched`
        self.snapshots = {}  # key -> prior content (or None), populated at commit

    def logs_to(self, log):
        """Send this buffer's notices to the action's own log. Set after construction
        because the runner builds staging before the `pack` that owns the log."""
        self._log = log

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
            if (current is not None and current["kind"] == "action"
                    and current["action_ref"] != owner_action_ref):
                # Taking a file from ANOTHER ACTION is allowed — files are open-world and
                # §6.1 hard-blocks only the user. But allowed is not the same as unremarked:
                # the L2 engine logs exactly this ("took {cell} from {who}"), and with
                # declaration-time detection deferred, two actions fighting over one file
                # is otherwise invisible and simply last-run-wins.
                self._log("info", f"took '{rel_path}' from "
                                  f"'{current['action_ref']}' — both actions write it")
        # §7.3: the existence flags "guard the write at the moment of the call". Checking
        # at commit told the author their file was missing long after the line that assumed
        # it, in a traceback about flushing a buffer — same reasoning as the hard-block
        # above, which is why both now live here. A file this step staged earlier counts as
        # existing: an action that writes a file and then edits it is doing so on purpose.
        key = self._store.key(rel_path)
        if file_must_exist and not (key in self._pending
                                    or self._store.exists(rel_path)):
            raise FileNotFoundError(f"Expected file to exist: {rel_path}")
        self._pending[key] = {
            "path": rel_path, "content": content, "owner": owner,
            "owner_action_ref": owner_action_ref, "file_must_exist": file_must_exist,
        }
        self._touched.add(key)

    def read(self, rel_path):
        staged = self._pending.get(self._store.key(rel_path))
        if staged is not None:
            return staged["content"]
        return self._store.read(rel_path)

    def exists(self, rel_path):
        return (self._store.key(rel_path) in self._pending
                or self._store.exists(rel_path))

    def ownership(self, rel_path):
        staged = self._pending.get(self._store.key(rel_path))
        if staged is not None:
            return {"kind": staged["owner"], "action_ref": staged["owner_action_ref"]}
        return self._store.ownership(rel_path)

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def begin_step(self):
        self._touched = set()
        return dict(self._pending)

    def rollback(self, savepoint):
        self._pending = dict(savepoint)
        self._touched = set()

    def changes(self, since=None) -> list:
        """What this buffer would DO, classified, in the shape the other engines use.

        The content is carried on both sides, not just the path: for a file the question
        "what would this action do" is answered by the bytes, and a preview that only
        listed paths would be a table of contents rather than a preview. It is also the
        only way a dry run can be diffed at all — there is nothing on disk to compare to.

        `after` additionally carries a **hash**. A committed run does not persist the new
        bytes (§1.1 flags the store-by-path snapshot as deliberately quick-and-dirty
        pending the content-addressed blob store, so doubling it is the wrong direction) —
        it keeps the hash, which is enough to tell a diff you can trust from one where the
        file has been edited since. §3.3's crash recovery asks for the same value.
        """
        described = []
        keys = self._touched if since is not None else self._pending.keys()
        for key in list(keys):
            staged = self._pending.get(key)
            if staged is None:
                continue                       # rolled back out from under us
            path = staged["path"]
            earlier = since.get(key) if since is not None else None
            if earlier is not None:
                # An earlier step staged this file, so ITS bytes are what this step is
                # editing — the same relation a committed earlier step would have.
                before = _side(earlier["content"], earlier["owner"],
                               earlier["owner_action_ref"])
            elif self._store.exists(path):
                prior_owner = self._store.ownership(path) or {}
                before = _side(self._store.read(path), prior_owner.get("kind"),
                               prior_owner.get("action_ref"))
            else:
                before = None
            after = _side(staged["content"], staged["owner"], staged["owner_action_ref"])
            after["hash"] = content_hash(staged["content"])
            described.append({
                "engine": "file", "path": path, "kind": classify(before, after),
                "before": before, "after": after,
            })
        return sorted(described, key=lambda d: d["path"])

    def commit(self):
        # No existence pre-flight here any more, deliberately. §7.3 puts that guard "at the
        # moment of the call", and `FileStaging.write` now enforces it — so by the time a
        # write is staged it has already been validated, and re-checking against the DISK
        # would reject the legitimate "produce a file, then edit it" pattern whose target
        # only exists in this same buffer. Failing during the action is also strictly
        # better than failing during the flush: nothing has been written yet either way,
        # but the traceback names the author's own line.

        for key, staged in self._pending.items():
            rel_path = staged["path"]      # the spelling the action used, for the message
            prior_owner = self._store.ownership(rel_path)          # capture before overwrite
            prior_content = self._store.write(
                rel_path, staged["content"],
                owner=staged["owner"], owner_action_ref=staged["owner_action_ref"],
                # Already validated at staging time; re-asserting here could only fail
                # mid-loop, which is the partial-commit hole this engine can't roll back.
                file_must_exist=False,
            )
            # Snapshot under the canonical key too, so two spellings of one file cannot
            # produce two rollback entries — the second of which would hold the FIRST
            # write's content and restore mid-step state.
            self.snapshots.setdefault(
                key, {"content": prior_content, "ownership": prior_owner})
        self._pending.clear()

    def discard(self):
        self._pending.clear()
