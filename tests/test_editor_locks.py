"""Lock state in an open tab, resynced against the world — design 6.1 / 6.3.

A tab's lock is decided when the document opens; ownership changes whenever it likes. Both
directions matter and both were reported: a job run taking a file (tab still looks
editable), and the user taking a file back in the Files panel (tab still looks locked, with
an unlock button that does nothing and a Ctrl+S that goes nowhere).

`EditorHost` is instantiated field-by-field here rather than constructed: its `__init__`
builds a `QWebEngineView` and starts Chromium, which this has nothing to say about. What is
under test is the lock bookkeeping, so the JS calls are recorded instead of run.
"""
import pytest

from packsmith.core.files import FileStore
from packsmith.gui.editor.host import EditorHost
from packsmith.gui.editor.sources import InstanceFileSource


@pytest.fixture
def host(tags, tmp_path):
    files = FileStore(tags._db, tmp_path)
    (tmp_path / "cfg.json").write_text('{"v": 1}', encoding="utf-8")

    host = EditorHost.__new__(EditorHost)
    host._sources = {"instance": InstanceFileSource(files)}
    host._opened = {"instance:cfg.json"}
    host._locked = set()
    host.js = []
    host._js = host.js.append
    return host, files


def test_an_action_taking_the_file_locks_the_open_tab(host):
    host, files = host
    files.write("cfg.json", "{}", owner="action", owner_action_ref="palette:fill")

    assert host.resync_locks() == ["instance:cfg.json"]
    assert host.is_locked("instance:cfg.json")
    assert host.js == ['setReadOnly("instance:cfg.json", true)']


def test_taking_the_file_back_unlocks_the_open_tab(host):
    """The panel-take direction. The lock is lifted in place: no reopen, because the
    buffer is still the user's own text and there is nothing to reload."""
    host, files = host
    files.write("cfg.json", "{}", owner="action", owner_action_ref="palette:fill")
    host.resync_locks()
    host.js.clear()

    files.claim("cfg.json", owner="user")             # what the Files panel's Take does

    assert host.resync_locks() == ["instance:cfg.json"]
    assert not host.is_locked("instance:cfg.json")
    assert host.js == ['setReadOnly("instance:cfg.json", false)']


def test_resync_is_quiet_when_nothing_moved(host):
    host, files = host
    assert host.resync_locks() == []
    assert host.js == []


def test_the_files_panel_take_reaches_the_open_tab(host):
    """The bug itself: the panel and the tab both knew, and nobody told the tab.

    The window's handler is exercised directly — a real `MainWindow` needs a profile and a
    Chromium view — but it is the piece that was missing, and the `connect` that calls it
    is one line away.
    """
    from packsmith.gui.main_window import MainWindow

    host, files = host
    files.write("cfg.json", "{}", owner="action", owner_action_ref="palette:fill")
    host.resync_locks()
    assert host.is_locked("instance:cfg.json")

    said = []
    recoloured = []
    window = type("W", (), {
        "_editor_host": host,
        "_set_status": lambda s, m: said.append(m),
        # Ownership moving is also what the tab icons are coloured by (§6.1), so the
        # handler re-colours them; the stub has to model that or it is testing a
        # `MainWindow` that no longer exists.
        "_refresh_tab_icons": lambda s: recoloured.append(True),
    })()
    files.claim("cfg.json", owner="user")
    MainWindow._file_ownership_changed(window, "You took ownership of cfg.json.")

    assert not host.is_locked("instance:cfg.json"), "the tab kept a lock that no longer existed"
    assert "editable now" in said[0], "and said nothing about it"
    assert recoloured, "the tab icon still shows the previous owner"


def test_a_locked_save_says_why_instead_of_vanishing(host):
    """§6.3 makes Ctrl+S a no-op while locked. A no-op the user cannot distinguish from a
    failed save is the actual complaint — the refusal has to be audible."""
    host, files = host
    files.write("cfg.json", "{}", owner="action", owner_action_ref="palette:fill")
    host.resync_locks()

    blocked = []
    host.edit_blocked = type("S", (), {"emit": lambda self, *a: blocked.append(a)})()

    host._on_save("instance:cfg.json", "{'mine': true}")

    assert blocked, "a locked Ctrl+S said nothing at all"
    assert "palette:fill" in blocked[0][1]
    assert files.read("cfg.json") == "{}", "the action's file was written anyway"


# --- the status-bar ownership indicator (design 6.3) ------------------------------------

@pytest.mark.parametrize("setup, expected", [
    (lambda f: None, "untouched"),
    (lambda f: f.claim("cfg.json", owner="user"), "yours"),
    (lambda f: f.claim("cfg.json", owner="action", owner_action_ref="palette:fill"),
     "palette:fill"),
])
def test_the_status_bar_says_who_owns_the_open_file(host, setup, expected):
    """§6.3 asks for a status-bar indicator for whole-file ownership. Without it the only
    way to learn a file is action-managed is to try to type in it."""
    from packsmith.gui.main_window import MainWindow

    host, files = host
    setup(files)
    window = type("W", (), {"_editor_host": host, "_file_store": files})()
    tab = type("T", (), {"key": "instance:cfg.json"})()

    line = MainWindow._ownership_line(window, tab)
    assert "cfg.json" in line
    assert expected in line
