"""Where an open document comes from, and what governs editing it (design 6.3).

§6.3 says Ctrl+S "saves through whatever path is appropriate for the file's origin" — this
is that seam. Two origins exist today and they are governed by genuinely different rules,
which is the whole reason the abstraction is worth having:

* **Instance files** live in the Minecraft instance, an *open world* with uncontrolled
  external writers (the game, mod updates, other editors). They are governed by
  **ownership** (§6.1): an action-owned file is locked until the user takes it.

* **Package files** are PackSmith's own userdata — Starlark actions and manifests the user
  authored. Nothing outside PackSmith writes them, so the ownership engine does not apply;
  running them through claim/release would be a category error (you would be "taking" a
  file you wrote, from nobody). They are governed by **provenance** instead: authored is
  editable, downloaded is read-only (§3.3.1, §6.3).

Deliberately, package files are *not* browsable in the Files panel. That panel is the
semantic browser over the **game instance**; a `.star` file the user wrote is not a game
file in any meaningful sense. They surface in the Actions panel, next to the actions they
implement.
"""
from pathlib import Path

from packsmith.core.files import FileOwnershipError


class DocumentSource:
    """Read/write access to one family of editable documents."""

    name = "source"

    def read(self, path: str) -> str | None:
        raise NotImplementedError

    def write(self, path: str, content: str):
        raise NotImplementedError

    def read_only_reason(self, path: str) -> str | None:
        """Why this document can't be edited, or None if it can."""
        return None

    def can_unlock(self, path: str) -> bool:
        """True when the user is *allowed* to lift the lock (and we should offer to)."""
        return False

    def unlock(self, path: str):
        """Lift the lock. Only called when :meth:`can_unlock` is True."""
        raise NotImplementedError

    def on_first_edit(self, path: str):
        """Hook for the moment a clean buffer becomes dirty."""


class InstanceFileSource(DocumentSource):
    """Files inside the Minecraft instance — the open world, governed by §6.1 ownership."""

    name = "instance"

    def __init__(self, file_store, on_claim=None):
        self._files = file_store
        self._on_claim = on_claim or (lambda path: None)

    def read(self, path):
        return self._files.read(path)

    def write(self, path, content):
        """Save — refusing if an action has taken the file since this buffer was read.

        §6.1: "Never silent overwrites", and editing an action-owned file is locked until
        an explicit take. That lock used to live only in the editor's read-only flag, which
        is computed once when the document opens — so a file that became action-owned while
        the tab sat there stayed writable, and Ctrl+S both destroyed the action's output and
        transferred ownership with no prompt. The rule belongs on the write, where it can't
        go stale.
        """
        managing = self.managing_action(path)
        if managing:
            raise FileOwnershipError(
                f"'{path}' is now managed by '{managing}' — it was written while you had "
                f"it open, so saving would overwrite that action's output with an older "
                f"copy. Reload it, or take ownership first.")
        self._files.write(path, content, owner="user")

    def managing_action(self, path):
        ownership = self._files.ownership(path)
        if ownership and ownership["kind"] == "action":
            return ownership.get("action_ref") or "an action"
        return None

    def read_only_reason(self, path):
        managing = self.managing_action(path)
        return f"managed by '{managing}'" if managing else None

    def can_unlock(self, path):
        return self.managing_action(path) is not None

    def unlock(self, path):
        self._files.claim(path, owner="user")
        self._on_claim(path)

    def on_first_edit(self, path):
        # §6.2: editing is the gesture that tracks a file — but only claim when it belongs
        # to nobody. Taking from nobody needs no ceremony; taking from an action does, and
        # that path goes through the lock instead.
        if self._files.ownership(path) is None:
            self._files.claim(path, owner="user")
            self._on_claim(path)


class PackageFileSource(DocumentSource):
    """Action sources and manifests under the profile's packages directory.

    Closed world: PackSmith is the only writer, so no ownership. Editability is decided by
    the package's **provenance** — you may edit what you authored, not what you downloaded.
    """

    name = "package"

    def __init__(self, package_index, packages_dir):
        self._index = package_index
        self._root = Path(packages_dir).resolve()

    def _abs(self, path: str) -> Path:
        resolved = (self._root / path).resolve()
        if resolved != self._root and not resolved.is_relative_to(self._root):
            raise ValueError(f"Path escapes the packages directory: {path}")
        return resolved

    @staticmethod
    def path_for(package_name: str, file_name: str) -> str:
        return f"{package_name}/{file_name}"

    def _package_of(self, path: str):
        head = str(path).replace("\\", "/").split("/", 1)[0]
        return self._index.package(head)

    def read(self, path):
        target = self._abs(path)
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def write(self, path, content):
        target = self._abs(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def read_only_reason(self, path):
        # Phrased as a noun clause so it reads correctly wherever it's interpolated —
        # "Locked — {reason}" and "This document is {reason}." both have to work.
        package = self._package_of(path)
        if package is not None and package.provenance == "downloaded":
            return f"part of the downloaded package '{package.name}'"
        return None

    def can_unlock(self, path):
        # Downloaded source is read-only by provenance, not by a claim someone can release.
        # Editing it would silently fork a package that still reports its upstream version.
        return False
