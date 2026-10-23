"""The disk moving under open documents — design 6.3.

Minecraft rewrites configs as it closes; mods correct values they dislike; logs grow by the
millisecond. Before this, an open tab never re-read its file, so the change was invisible
until the tab was reopened — and a Ctrl+S on the stale buffer silently put the old text
back over the game's, the overwrite §6.1 forbids.

Everything here guards a failure that HIDES: a save that overwrites without asking, a
reload that eats unsaved edits, our own write reported back as somebody else's, a watch
that quietly stops after a rename, a log that appends a new session onto the old one.

`EditorHost` is built field-by-field as in test_editor_locks — its `__init__` starts
Chromium — and its JS calls are recorded instead of run.
"""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject

from packsmith.core.files import FileStore
from packsmith.gui.editor.follow import HEAD_BYTES, LogFollower, is_followed
from packsmith.gui.editor.host import EditorHost
from packsmith.gui.editor.sources import InstanceFileSource
from packsmith.gui.editor.watch import FileWatch

KEY = "instance:config/mod.toml"


@pytest.fixture(scope="module", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class _Watch:
    """Stands in for FileWatch: the host's decisions are under test, not the polling."""

    def __init__(self):
        self.acknowledged = []

    def add(self, key, path):
        pass

    def remove(self, key):
        pass

    def acknowledge(self, key):
        self.acknowledged.append(key)

    def scan(self):
        pass


@pytest.fixture
def host(tags, tmp_path):
    files = FileStore(tags._db, tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "mod.toml").write_bytes(b"value = 1\n")

    host = EditorHost.__new__(EditorHost)
    QObject.__init__(host)
    host._sources = {"instance": InstanceFileSource(files)}
    host._opened, host._locked, host._dirty = set(), set(), set()
    host._known, host._conflicts, host._deleted, host._followers = {}, {}, set(), {}
    host._watch = _Watch()
    host.js = []
    host._js = host.js.append
    host.events = []
    host.disk_reloaded.connect(lambda k: host.events.append(("reloaded", k)))
    host.disk_conflict.connect(lambda k, gone: host.events.append(("conflict", k, gone)))
    host.disk_resolved.connect(lambda k: host.events.append(("resolved", k)))
    host.save_failed.connect(lambda k, why: host.events.append(("save_failed", k)))
    host.file_saved.connect(lambda k: host.events.append(("saved", k)))
    host.open_document("instance", "config/mod.toml")
    host.js.clear()
    return host, tmp_path / "config" / "mod.toml"


def _game_writes(path, text):
    path.write_bytes(text.encode("utf-8"))      # exact bytes: no newline translation


# --- the host's decisions ------------------------------------------------------------------

def test_a_clean_buffer_takes_the_disks_version(host):
    host, path = host
    _game_writes(path, "value = 2\n")
    host._on_disk_changed(KEY)

    assert host.js == [f'reloadModel("{KEY}", "value = 2\\n", false)']
    assert host.events == [("reloaded", KEY)]
    assert not host.has_disk_conflict(KEY)


def test_unsaved_edits_are_never_overwritten_by_a_disk_change(host):
    """The heart of it: the game rewrote the file while you were typing. Reloading would
    eat your edits; doing nothing would let Ctrl+S eat the game's. It must ask."""
    host, path = host
    host._on_dirty(KEY, True)
    _game_writes(path, "value = 2\n")
    host._on_disk_changed(KEY)

    assert not any(call.startswith("reloadModel") for call in host.js)
    assert ("conflict", KEY, False) in host.events
    assert host.has_disk_conflict(KEY)


def test_saving_over_an_unresolved_change_is_refused(host):
    host, path = host
    host._on_dirty(KEY, True)
    _game_writes(path, "value = 2\n")
    host._on_disk_changed(KEY)

    host._on_save(KEY, "value = 99\n")
    assert path.read_text(encoding="utf-8") == "value = 2\n"
    assert ("save_failed", KEY) in host.events


def test_a_save_that_races_the_watch_still_cannot_overwrite_blind(host):
    """The game wrote, the poll has not ticked yet, you press Ctrl+S. Checked at save time,
    so the window between the write and the next tick is not a window for data loss."""
    host, path = host
    host._on_dirty(KEY, True)
    _game_writes(path, "value = 2\n")          # no _on_disk_changed: the watch is behind

    host._on_save(KEY, "value = 99\n")
    assert path.read_text(encoding="utf-8") == "value = 2\n"
    assert ("conflict", KEY, False) in host.events
    assert ("save_failed", KEY) in host.events


def test_keep_mine_makes_the_next_save_overwrite_on_purpose(host):
    host, path = host
    host._on_dirty(KEY, True)
    _game_writes(path, "value = 2\n")
    host._on_disk_changed(KEY)

    host.keep_mine(KEY)
    host._on_save(KEY, "value = 99\n")
    assert path.read_text(encoding="utf-8") == "value = 99\n"
    assert ("saved", KEY) in host.events


def test_reload_takes_the_disk_even_over_unsaved_edits(host):
    host, path = host
    host._on_dirty(KEY, True)
    _game_writes(path, "value = 2\n")
    host._on_disk_changed(KEY)
    host.js.clear()

    host.reload_from_disk(KEY)
    assert host.js == [f'reloadModel("{KEY}", "value = 2\\n", true)']
    assert not host.has_disk_conflict(KEY)


def test_our_own_save_is_not_reported_back_as_a_disk_change(host):
    host, path = host
    host._on_save(KEY, "value = 5\n")
    host.js.clear()
    host.events.clear()

    host._on_disk_changed(KEY)                   # the watch sees our write land
    assert host.js == []
    assert host.events == []
    assert KEY in host._watch.acknowledged


def test_a_reload_the_page_refuses_becomes_a_conflict(host):
    """Python's dirty state can trail a keystroke; the page knows. Its refusal must end in
    the same place as if Python had known — a choice, not a silent no-op."""
    host, path = host
    _game_writes(path, "value = 2\n")
    host._on_disk_changed(KEY)                   # Python thought the buffer was clean
    host._on_reload_refused(KEY)                 # ... the page says otherwise

    assert host.has_disk_conflict(KEY)
    assert ("conflict", KEY, False) in host.events


def test_a_deleted_file_is_said_and_saving_puts_it_back(host):
    host, path = host
    path.unlink()
    host._on_disk_changed(KEY)
    assert ("conflict", KEY, True) in host.events

    host._on_save(KEY, "value = 1\n")
    assert path.read_text(encoding="utf-8") == "value = 1\n"


def test_logs_are_followed_not_reloaded(host, tmp_path):
    host, _ = host
    log = tmp_path / "logs" / "latest.log"
    log.parent.mkdir()
    log.write_bytes(b"[12:00:00] start\n")
    key = host.open_document("instance", "logs/latest.log")
    host.js.clear()

    with open(log, "ab") as f:
        f.write(b"[12:00:01] more\n")
    host._on_disk_changed(key)
    assert host.js[0].startswith(f'appendModel("{key}", "[12:00:01] more\\n"')

    host._on_save(key, "anything")                 # Ctrl+S in a log: nothing, quietly
    assert log.read_text(encoding="utf-8").endswith("more\n")
    assert host.is_locked(key) and host.resync_locks() == []


# --- the log follower ----------------------------------------------------------------------

def test_the_follower_reads_only_what_was_added(tmp_path):
    log = tmp_path / "latest.log"
    log.write_bytes(b"one\n")
    follower = LogFollower(log)
    assert follower.open() == "one\n"
    with open(log, "ab") as f:
        f.write(b"two\n")
    assert follower.poll() == ("append", "two\n")
    assert follower.poll() == ("none", "")


def test_a_new_session_starts_over_rather_than_appending(tmp_path):
    """Minecraft starts a new latest.log every launch. Shorter than where we were, or
    already longer but with different first bytes — either way, a restart."""
    log = tmp_path / "latest.log"
    log.write_bytes(b"[10:00:00] old session\n" * 3)
    follower = LogFollower(log)
    follower.open()

    log.write_bytes(b"[11:00:00] new\n")
    assert follower.poll() == ("reset", "[11:00:00] new\n")

    follower.open()
    log.write_bytes(b"[12:00:00] newer, and longer than before " * 20)
    kind, text = follower.poll()
    assert kind == "reset" and text.startswith("[12:00:00]")


def test_a_huge_log_opens_at_its_tail_and_says_so(tmp_path):
    log = tmp_path / "debug.log"
    log.write_bytes(b"".join(b"line %d\n" % i for i in range(5000)))
    text = LogFollower(log, tail_bytes=1000).open()
    assert text.startswith("[packsmith]")
    assert text.endswith("line 4999\n")
    assert "\nline 4" in text and "line 0\n" not in text


def test_a_character_split_across_reads_is_not_mangled(tmp_path):
    log = tmp_path / "latest.log"
    log.write_bytes(b"x" * HEAD_BYTES + b"\n")
    follower = LogFollower(log)
    follower.open()
    snowman = "☃".encode("utf-8")
    with open(log, "ab") as f:
        f.write(snowman[:1])
    assert follower.poll() == ("append", "")
    with open(log, "ab") as f:
        f.write(snowman[1:])
    assert follower.poll() == ("append", "☃")


def test_only_logs_are_followed():
    assert is_followed("logs/latest.log") and is_followed("logs/DEBUG.LOG")
    assert not is_followed("config/mod.toml") and not is_followed("logs/old.log.gz")


# --- the watch itself ----------------------------------------------------------------------

def _replace(source, target):
    """`os.replace`, retried briefly. Windows lets antivirus and the indexer open a file the
    instant it is written, and a rename onto it is refused while they hold it — measured at
    about one run in five here, with no watcher involved at all (a bare QFileSystemWatcher on
    the file and its folder does not block the rename)."""
    for attempt in range(20):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def _changed(watch):
    seen = []
    watch.changed.connect(seen.append)
    return seen


def test_a_change_is_reported_once_it_holds_still(tmp_path):
    target = tmp_path / "mod.toml"
    target.write_text("a = 1\n", encoding="utf-8")
    watch = FileWatch()
    seen = _changed(watch)
    watch.add("k", target)

    target.write_text("a = 22\n", encoding="utf-8")
    watch.scan()
    assert seen == []                      # first sight: wait to see that it settles
    watch.scan()
    assert seen == ["k"]
    watch.scan()
    assert seen == ["k"]                   # once


def test_a_file_replaced_by_rename_is_still_watched(tmp_path):
    """NightConfig saves by writing a temp file and renaming it over the original, which
    ends the native watch on the old file. The change must still be reported, and the next
    one after it too."""
    target = tmp_path / "mod.toml"
    target.write_text("a = 1\n", encoding="utf-8")
    watch = FileWatch()
    seen = _changed(watch)
    watch.add("k", target)

    for n, text in enumerate(("a = 22\n", "a = 333\n"), start=1):
        temp = tmp_path / "mod.toml.tmp"
        temp.write_text(text, encoding="utf-8")
        _replace(temp, target)
        watch.scan()
        watch.scan()
        assert seen == ["k"] * n
        assert str(target) in watch._watcher.files()


def test_acknowledged_writes_are_not_reported(tmp_path):
    target = tmp_path / "mod.toml"
    target.write_text("a = 1\n", encoding="utf-8")
    watch = FileWatch()
    seen = _changed(watch)
    watch.add("k", target)

    target.write_text("a = 22\n", encoding="utf-8")
    watch.acknowledge("k")
    watch.scan()
    watch.scan()
    assert seen == []


def test_a_file_that_never_holds_still_is_reported_anyway(tmp_path):
    target = tmp_path / "latest.log"
    target.write_text("x\n", encoding="utf-8")
    watch = FileWatch(max_wait_ms=0)
    seen = _changed(watch)
    watch.add("k", target)

    with open(target, "a", encoding="utf-8") as f:
        f.write("y\n")
    watch.scan()
    time.sleep(0.01)
    with open(target, "a", encoding="utf-8") as f:
        f.write("zz\n")
    watch.scan()
    assert seen == ["k"]
