"""Copies of a profile's database, kept because nothing else protects hand-entered work.

Packsmith defends the data it *generates* very well — actions stage their writes, dry runs
withhold promotion, every step records enough to be rolled back, and files it does not own
are hard-blocked. None of that covers the data a person **typed**. Filling in a blueprint
is thousands of individual judgments that no heuristic can reproduce (design 5.3 is
explicit that this is the human's job), and until now a single bad delete, a mis-drag, or
one unlucky schema drift took all of it with no way back.

The asymmetry is the whole argument: re-running an action costs seconds, and re-deriving a
blueprint costs weeks. The cheap data had a safety net and the expensive data did not.

**Why SQLite's backup API rather than copying the file.** A database at rest can be more
than one file — the rollback journal sits beside it — and a plain copy can catch the two
disagreeing, producing a copy that is not a database. `Connection.backup` walks the pages
through SQLite itself, takes the right locks, and starts over if a writer changes something
underneath it, so what lands is always consistent. It also works while the app is running,
which matters because the moments worth capturing are all mid-session.

**Coalescing.** Snapshots are skipped when one was taken moments ago, so deleting forty
instances leaves one copy of the state before the batch rather than forty copies of the
demolition in progress — the last of which would be the least useful thing to keep, and
would have evicted everything worth having. `force=True` opts out, for the one caller that
is about to destroy the database on purpose.
"""
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from packsmith.common.logging import log

# Enough to cover a bad week, not so many that a profile directory becomes a museum. Each
# is the size of the database — a few MB — so this is cheap in every realistic profile.
DEFAULT_KEEP = 15

# A snapshot younger than this stands in for the ones that would follow it. Long enough to
# collapse a burst of edits, short enough that a session has several distinct moments in it.
COALESCE_SECONDS = 300

_STAMP = "%Y-%m-%d_%H-%M-%S"
_SAFE = re.compile(r"[^a-z0-9]+")


def backups_dir(db_path) -> Path:
    """Beside the database, inside the profile — a backup that lives somewhere else is one
    nobody finds when they need it, and moving a profile has to move its history too."""
    return Path(db_path).parent / "backups"


def _slug(reason: str) -> str:
    return _SAFE.sub("-", (reason or "manual").lower()).strip("-") or "manual"


def snapshot(conn, db_path, reason: str, *, keep: int = DEFAULT_KEEP,
             force: bool = False, now=None) -> Path | None:
    """Copy the live database into `backups/`. Returns the path, or None if skipped.

    Skipped when a recent snapshot already covers this moment, or when the database holds
    no tables at all — a copy of an empty file is not history, it is clutter that evicts
    real history.

    Never raises for a routine call: a backup that takes the app down with it is worse than
    the missing backup. The one caller that *needs* it to have worked checks the return.
    """
    db_path = Path(db_path)
    now = now or datetime.now()
    try:
        if not conn.execute("SELECT COUNT(*) FROM sqlite_master "
                            "WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]:
            return None

        existing = list_backups(db_path)
        if not force and existing:
            age = (now - existing[0]["taken"]).total_seconds()
            if age < COALESCE_SECONDS:
                return None

        folder = backups_dir(db_path)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{now.strftime(_STAMP)}_{_slug(reason)}.db"
        # Two snapshots inside one second would otherwise land on the same name and the
        # second would overwrite the first — the exact loss this module exists to prevent.
        suffix = 1
        while target.exists():
            target = folder / f"{now.strftime(_STAMP)}_{_slug(reason)}-{suffix}.db"
            suffix += 1

        # `with sqlite3.connect(...)` commits; it does NOT close. The leaked handle keeps
        # the file open, and on Windows an open handle makes it undeletable — so `_prune`
        # below fails on every old snapshot and retention silently stops working.
        copy = sqlite3.connect(str(target))
        try:
            conn.backup(copy)
        finally:
            copy.close()
        log.info("Snapshotted %s -> %s", db_path.name, target.name)
        _prune(db_path, keep)
        return target
    except (sqlite3.Error, OSError) as e:
        log.warning("Could not snapshot %s: %s", db_path, e)
        return None


def list_backups(db_path) -> list[dict]:
    """Newest first. Unreadable names are ignored rather than guessed at — a file that
    cannot say when it was taken cannot be trusted to be pruned in the right order.

    Split from the RIGHT: the stamp contains underscores and a reason slug never does.
    """
    folder = backups_dir(db_path)
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob("*.db"):
        stamp, _, reason = path.stem.rpartition("_")
        try:
            taken = datetime.strptime(stamp, _STAMP)
        except ValueError:
            continue
        found.append({"path": path, "taken": taken, "reason": reason,
                      "size": path.stat().st_size})
    return sorted(found, key=lambda b: b["taken"], reverse=True)


def _prune(db_path, keep: int):
    for old in list_backups(db_path)[max(keep, 1):]:
        try:
            old["path"].unlink()
            log.info("Pruned old snapshot: %s", old["path"].name)
        except OSError as e:
            log.warning("Could not prune %s: %s", old["path"], e)


def restore(db_path, backup_path):
    """Put a snapshot back, having first snapshotted what is being replaced.

    Restoring is itself destructive — the state you are abandoning may turn out to be the
    one you wanted — so it leaves a way back before it takes one away.
    """
    db_path, backup_path = Path(db_path), Path(backup_path)
    live = sqlite3.connect(str(db_path))
    try:
        if snapshot(live, db_path, "before-restore", force=True) is None:
            log.warning("Restoring %s without a snapshot of what it replaces", db_path.name)
        source = sqlite3.connect(str(backup_path))
        try:
            source.backup(live)
        finally:
            source.close()
    finally:
        live.close()
    log.info("Restored %s from %s", db_path.name, backup_path.name)
