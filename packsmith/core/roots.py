"""Tracked roots — the places Packsmith may look at all (design 6.6).

Everything in §6 assumed one place: the instance, baked into `FileStore` as a single path.
That held while Packsmith's job ended at the instance, and stops holding the moment the
pack's content is *generated* — a glue mod's assets are authored in a Gradle project that is
never going to live inside a Minecraft instance.

**One `FileStore` per root, composed here.** The alternative was a `root=` argument on all
fifteen path-taking methods of `FileStore`, which is fifteen chances to forget one and have
it silently answer about the instance. A store cannot be asked about a root it is not, so the
wrong-root bug is unrepresentable rather than merely avoided.

**Adding a root is not granting an action access to it.** §6.2 asked that action access be a
second, explicit, per-root opt-in, and that is what a binding is: adding a root lets a
*person* browse and edit it; binding it to a step lets *one action* write into it. An action
can no more reach an arbitrary root than an arbitrary resource pack.
"""
import re
from pathlib import Path

from packsmith.common.logging import log
from packsmith.core.files import FileStore

# The instance. Seeded by the schema with a NULL path, meaning "wherever this profile's
# instance is" — copying the path into the database would make two sources of truth for a
# value the user can change, and they would disagree the first time someone moved it.
INSTANCE_ROOT = "minecraft"

_NAME = re.compile(r"[a-z][a-z0-9_]*$")


class RootError(ValueError):
    """A tracked root could not be added, renamed or removed, and why."""


class FileRoots:
    """Every tracked root of one profile, and the `FileStore` for each."""

    def __init__(self, db, instance_root, *, protected=()):
        self._db = db
        self._instance = Path(instance_root).resolve()
        # Folders no root may contain or be contained by — `userdata/` above all. §6.2:
        # reaching `.star` sources through the browser would edit code by a door that
        # applies none of §3.3.1's provenance rules, and checking `path != userdata` is not
        # enough because adding userdata's PARENT defeats it.
        self._protected = [Path(p).resolve() for p in protected]
        self._stores = {}
        self.reload()

    # --- reading ------------------------------------------------------------

    def reload(self):
        """Rebuild the stores from `tracked_roots`. Cheap, and called after every change so
        a stale registry cannot outlive the row it was built from."""
        self._stores = {}
        for row in self._db.fetch_all("SELECT id, name, path FROM tracked_roots ORDER BY id"):
            path = self._instance if row["path"] is None else Path(row["path"])
            self._stores[row["name"]] = FileStore(
                self._db, path, root_id=row["id"], name=row["name"])

    def names(self) -> list:
        return list(self._stores)

    def store(self, name: str = INSTANCE_ROOT) -> FileStore:
        """The store for one root. Unknown names raise rather than falling back to the
        instance — silently writing somewhere else is the whole failure this prevents."""
        store = self._stores.get(name or INSTANCE_ROOT)
        if store is None:
            raise RootError(
                f"no tracked folder called '{name}' — add it first, or bind one to this step")
        return store

    def has(self, name: str) -> bool:
        return name in self._stores

    def paths(self) -> dict:
        return {name: store.root for name, store in self._stores.items()}

    @property
    def instance(self) -> FileStore:
        return self._stores[INSTANCE_ROOT]

    # --- changing -----------------------------------------------------------

    def add(self, name: str, path) -> FileStore:
        """Track a folder. Every check here is a correctness requirement, not a taste."""
        name = (name or "").strip().lower()
        if not _NAME.match(name):
            raise RootError(
                f"'{name}' is not a usable name — lowercase letters, digits and "
                f"underscores, starting with a letter")
        if name in self._stores:
            raise RootError(f"'{name}' is already tracked")

        folder = Path(path).expanduser().resolve()
        if not folder.is_dir():
            raise RootError(f"{folder} is not a folder")
        self._refuse_overlap(folder)

        self._db.execute("INSERT INTO tracked_roots (name, path) VALUES (?, ?)",
                         (name, str(folder)))
        self.reload()
        log.info("Tracking '%s' -> %s", name, folder)
        return self._stores[name]

    def rename(self, name: str, new_name: str):
        """A name is a LABEL (design 3.2.1). Ownership rows and bindings key on the id, so
        this is a one-row update that orphans nothing."""
        new_name = (new_name or "").strip().lower()
        self._require_removable(name, "renamed")
        if not _NAME.match(new_name):
            raise RootError(f"'{new_name}' is not a usable name")
        if new_name in self._stores and new_name != name:
            raise RootError(f"'{new_name}' is already tracked")
        self._db.execute("UPDATE tracked_roots SET name = ? WHERE name = ?", (new_name, name))
        self.reload()

    def remove(self, name: str):
        """Stop tracking. Its ownership rows go with it — `file_ownership.root_id` cascades,
        which is right: Packsmith's claim on a file it can no longer see is not a claim, and
        keeping the rows would make them resurface attached to whatever took the name next.
        The FILES are untouched.
        """
        self._require_removable(name, "removed")
        self._db.execute("DELETE FROM tracked_roots WHERE name = ?", (name,))
        self.reload()
        log.info("Stopped tracking '%s'", name)

    # --- guards -------------------------------------------------------------

    def _require_removable(self, name, verb):
        if name == INSTANCE_ROOT:
            raise RootError(
                f"'{INSTANCE_ROOT}' is the instance and cannot be {verb} — a profile "
                f"without one is not a profile")
        if name not in self._stores:
            raise RootError(f"no tracked folder called '{name}'")

    def _refuse_overlap(self, folder: Path):
        """No root may contain, or be contained by, another root or a protected folder.

        Overlap is not a preference. If one root contains another the same file has two
        identities and two ownership rows, and the hard-block stops composing — an action
        blocked under one name writes it freely under the other. Refused when the folder is
        added, the one moment the check is cheap and the answer is actionable.
        """
        for other in self._protected:
            if _nests(folder, other):
                raise RootError(
                    f"{folder} contains or sits inside Packsmith's own data ({other}) — "
                    f"tracking it would expose action sources to editing by a door that "
                    f"applies none of the package rules")
        for name, store in self._stores.items():
            if _nests(folder, store.root):
                raise RootError(
                    f"{folder} overlaps '{name}' ({store.root}) — one file would have two "
                    f"identities, and an action blocked under one name could write it "
                    f"under the other")


def _nests(a: Path, b: Path) -> bool:
    """Either contains the other, or they are the same folder."""
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)
