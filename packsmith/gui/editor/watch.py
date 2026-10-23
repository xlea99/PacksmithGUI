"""Noticing that a file open in the editor changed on disk (design 6.3).

Instance files live in an *open world* (see `sources.py`): the game rewrites configs as it
closes, a mod corrects a value it did not like, a log grows every few milliseconds. A tab
that only ever shows the file as it was when it opened is stale the moment any of that
happens — and a Ctrl+S on that stale buffer silently puts the old text back over whatever
was written in the meantime, which is the one thing §6.1 says never to do.

**Two signals, because neither is enough alone.** `QFileSystemWatcher` is immediate but
unreliable in exactly the cases that matter here: a writer that saves by writing a temp
file and renaming it over the original (NightConfig, which most mod configs go through)
replaces the file the watcher was attached to, and the watch silently ends. A poll is
reliable and slow. So the watcher makes it fast, the poll makes it certain, and the
watcher is re-armed whenever a path falls out of it.

**Held still, mostly.** A file is reported once its signature (mtime, size) has stayed the
same across one short settle — reading a config mid-write would show half of it, and a
half-written file shown as "changed on disk" is a conflict the user did not cause. A file
that never holds still (a log during startup) is reported anyway once it has been moving
for `MAX_WAIT_MS`: following a log a second behind is fine, never following it is not.

This module only says *that* something changed. What a change means — reload, conflict,
append — is the host's business, because it depends on the buffer, not the file.
"""
import time
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

POLL_MS = 1000          # the backstop; also the cadence a busy log is followed at
SETTLE_MS = 200         # how long a signature must hold before it counts
MAX_WAIT_MS = 1000      # how long a file that never holds still waits anyway


def signature(path):
    """``(mtime_ns, size)``, or None when the file is not there."""
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class _Watched:
    __slots__ = ("path", "seen", "pending", "pending_since")

    def __init__(self, path):
        self.path = Path(path)
        self.seen = signature(path)
        self.pending = None
        self.pending_since = 0.0


class FileWatch(QObject):
    """Watches a set of files by key and says when one of them has changed."""

    changed = Signal(str)       # key

    def __init__(self, parent=None, *, poll_ms=POLL_MS, settle_ms=SETTLE_MS,
                 max_wait_ms=MAX_WAIT_MS):
        super().__init__(parent)
        self._files = {}                     # key -> _Watched
        self._max_wait = max_wait_ms / 1000.0
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_event)
        self._watcher.directoryChanged.connect(self._on_event)
        # Parented to self, like every timer in this project: a timer that outlives its
        # owner fires into a deleted C++ object.
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(settle_ms)
        self._settle.timeout.connect(self.scan)
        self._poll = QTimer(self)
        self._poll.setInterval(poll_ms)
        self._poll.timeout.connect(self.scan)

    def add(self, key, path):
        self._files[key] = _Watched(path)
        self._arm(Path(path))
        if not self._poll.isActive():
            self._poll.start()

    def remove(self, key):
        watched = self._files.pop(key, None)
        if watched is None:
            return
        if not any(w.path == watched.path for w in self._files.values()):
            self._disarm(watched.path)
        if not self._files:
            self._poll.stop()

    def clear(self):
        for key in list(self._files):
            self.remove(key)

    def acknowledge(self, key):
        """Take the file's current state as already known — after our own write, or after
        a change has been dealt with — so it is not reported back to us."""
        watched = self._files.get(key)
        if watched is not None:
            watched.seen = signature(watched.path)
            watched.pending = None

    def keys(self):
        return list(self._files)

    # --- detection -----------------------------------------------------------

    def scan(self):
        """Look at every watched file now. Public so a save, or a test, can force it."""
        now = time.monotonic()
        moving = False
        for key, watched in list(self._files.items()):
            current = signature(watched.path)
            if current == watched.seen:
                watched.pending = None
                continue
            held = current == watched.pending
            waited = watched.pending is not None and now - watched.pending_since >= self._max_wait
            if held or waited:
                watched.seen = current
                watched.pending = None
                self._arm(watched.path)          # a rename-replace ended the old watch
                self.changed.emit(key)
                continue
            if watched.pending is None:
                watched.pending_since = now
            watched.pending = current
            moving = True
        if moving:
            self._settle.start()

    def _on_event(self, _path):
        self._settle.start()

    # --- the native watch ------------------------------------------------------
    #
    # The file AND its folder. The folder is what still reports when the file is replaced
    # by a rename, or deleted and recreated (Minecraft does that to latest.log on every
    # launch), after which the file's own watch is gone.

    def _arm(self, path: Path):
        targets = [str(path.parent)] + ([str(path)] if path.exists() else [])
        current = set(self._watcher.files()) | set(self._watcher.directories())
        missing = [t for t in targets if t not in current and Path(t).exists()]
        if missing:
            self._watcher.addPaths(missing)

    def _disarm(self, path: Path):
        folder = path.parent
        others = [w.path for w in self._files.values()]
        drop = [str(path)]
        if not any(other.parent == folder for other in others):
            drop.append(str(folder))
        present = set(self._watcher.files()) | set(self._watcher.directories())
        drop = [d for d in drop if d in present]
        if drop:
            self._watcher.removePaths(drop)
