"""Splitter grab area, and the bottom panel's remembered height.

Two small ergonomics, both in the "settings that are never set" family: you drag the
divider and *that* is the setting. Putting a number for it in a dialog would be worse than
the dragging, so the drag is what persists.

The height lives in app **state**, keyed by profile — not in `profile.json`, which holds the
pack's contract (instance path, MC version, loader) and is a file the user reads and
sometimes edits. A splitter position has no business in there.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.common import setup as app_setup
from packsmith.common.setup import load_ui_state, save_ui_state
from packsmith.gui.shell import style
from packsmith.gui.shell.bottom_panel import (
    DEFAULT_CONTENT_HEIGHT, MIN_CONTENT_HEIGHT, BottomPanel)


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(app_setup.GLOBAL_PATHS, "config", config)
    return config


# --- per-profile UI state -------------------------------------------------------------

def test_nothing_remembered_is_an_empty_dict(state_dir):
    assert load_ui_state("deep_end") == {}


def test_a_dragged_height_comes_back(state_dir):
    save_ui_state("deep_end", bottom_height=480)
    assert load_ui_state("deep_end")["bottom_height"] == 480


def test_profiles_do_not_share_a_layout(state_dir):
    """Different packs, different monitors, different habits. Leaking one profile's layout
    into another would make the memory feel random rather than helpful."""
    save_ui_state("deep_end", bottom_height=480)
    save_ui_state("testicles", bottom_height=200)

    assert load_ui_state("deep_end")["bottom_height"] == 480
    assert load_ui_state("testicles")["bottom_height"] == 200


def test_saving_one_key_keeps_the_others(state_dir):
    """So the next thing worth remembering doesn't wipe this one."""
    save_ui_state("deep_end", bottom_height=480)
    save_ui_state("deep_end", something_later="x")
    assert load_ui_state("deep_end") == {"bottom_height": 480, "something_later": "x"}


def test_ui_state_does_not_disturb_the_rest_of_the_state_file(state_dir):
    """It shares `state.json` with the last-opened-profile breadcrumb."""
    app_setup.save_state(last_profile="deep_end")
    save_ui_state("deep_end", bottom_height=480)
    assert app_setup.load_state()["last_profile"] == "deep_end"


def test_no_profile_name_is_a_no_op_rather_than_a_crash(state_dir):
    """The blocked shell has no profile at all, and it still has a window."""
    save_ui_state("", bottom_height=480)
    assert load_ui_state("") == {}


# --- the splitter grab area ---------------------------------------------------------------

def test_splitters_are_wider_than_qts_default():
    """Qt's ~4px default is a precision-aiming exercise. The drawn seam stays a hairline —
    the handle is transparent and only its inner edge is painted — so this is hit area, not
    a visible gutter."""
    assert style.SPLITTER_WIDTH >= 8
    assert "background: transparent" in style.SPLITTER_QSS
    assert "1px solid" in style.SPLITTER_QSS


def test_every_splitter_in_the_app_uses_it():
    """Three of them — the sidebar, the bottom panel, and the blueprint editor's. One
    styled differently is the one you notice, because it is the one that still needs
    aiming at."""
    import ast
    import pathlib

    for path in pathlib.Path("packsmith/gui").rglob("*.py"):
        if "vendor" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for count, line in enumerate(source.splitlines()):
            if "QSplitter(" in line and "=" in line:
                following = source.splitlines()[count:count + 3]
                assert any("SPLITTER_WIDTH" in f for f in following), \
                    f"{path}:{count + 1} builds a splitter without the shared grab width"


# --- how tall it opens ----------------------------------------------------------------------

def test_it_opens_to_a_usable_height():
    """The first thing anyone does with a log or a diff is drag it taller; opening small
    makes the panel feel like it is resisting."""
    panel = BottomPanel()
    try:
        assert DEFAULT_CONTENT_HEIGHT >= 250
        assert panel.expanded_height() == panel.collapsed_height() + DEFAULT_CONTENT_HEIGHT
    finally:
        panel.deleteLater()


def test_the_floor_is_below_the_default_but_not_zero():
    """The floor stops a splitter handing back a sliver that reads as a rendering fault;
    the default is what it opens to when nothing is remembered. Collapsing them into one
    number would mean either opening tiny or never being draggable small."""
    panel = BottomPanel()
    try:
        assert 0 < MIN_CONTENT_HEIGHT < DEFAULT_CONTENT_HEIGHT
        assert panel.minimum_expanded_height() < panel.expanded_height()
    finally:
        panel.deleteLater()


# --- the window honours it -----------------------------------------------------------------

@pytest.fixture
def window(state_dir, monkeypatch):
    """A real MainWindow, so the splitter arithmetic is exercised rather than described."""
    import pathlib
    import tempfile
    import json as jsonlib
    from packsmith.core.profile import Profile, delete_profile
    from packsmith.gui.main_window import MainWindow

    name = "panel_layout_probe"
    instance = pathlib.Path(tempfile.mkdtemp()) / "instance"
    dump = instance / "packsmith"
    (dump / "registries").mkdir(parents=True)
    (dump / "attributes").mkdir(parents=True)
    (dump / "meta.json").write_text(jsonlib.dumps({
        "type": "packsmith_full_dump", "schema_version": 1,
        "generated_at_utc": "2026-08-12T00:00:00+00:00",
        "minecraft_version": "1.20.1", "loader": "forge", "loader_version": "47.4.10",
        "mods": [{"mod_id": "a", "name": "A", "version": "1"}],
        "registries": [{"type": "minecraft:item", "file": "i.json", "count": 1}],
    }), encoding="utf-8")
    (dump / "registries" / "i.json").write_text('{"values": ["minecraft:stone"]}',
                                                encoding="utf-8")
    (dump / "attributes" / "localization.json").write_text(
        '{"locale": "en_us", "values": {}}', encoding="utf-8")
    try:
        delete_profile(name)
    except Exception:
        pass
    Profile.create(name, mc_path=str(instance), loader="forge",
                   loader_version="47.4.10", mc_version="1.20.1")
    win = MainWindow(profile_name=name)
    win.resize(1200, 800)
    win.show()
    try:
        yield win, win._h_split.widget(1)
    finally:
        _dispose(win)
        try:
            delete_profile(name)
        except Exception:
            pass


def _dispose(win):
    """Actually destroy a MainWindow, rather than just hiding it.

    `close()` only hides. A MainWindow owns a QWebEngineView (the Monaco host), and leaving
    several of those alive across a test session crashes the interpreter later — in an
    unrelated file, with an access violation and no usable traceback. Deleting on the spot
    keeps that from becoming somebody else's mystery.
    """
    from PySide6.QtWidgets import QApplication
    win.close()
    win.deleteLater()
    QApplication.processEvents()


def test_opening_gives_the_panel_real_room(window):
    """The splitter was told to hold it at its collapsed height, and would have honoured
    that forever — the panel would open as a sliver."""
    win, split = window
    assert split.sizes()[1] == win._bottom.collapsed_height()

    win._bottom.show_panel("logs")
    assert split.sizes()[1] == win._bottom.expanded_height()


def test_a_dragged_height_survives_collapsing(window):
    win, split = window
    win._bottom.show_panel("logs")
    total = sum(split.sizes())
    split.setSizes([total - 420, 420])
    win._on_bottom_dragged(split)

    win._bottom.toggle()
    assert split.sizes()[1] == win._bottom.collapsed_height()
    win._bottom.toggle()
    assert split.sizes()[1] == 420


def test_a_dragged_height_survives_the_session(window):
    """The whole point of persisting it. A fresh window is a fresh session."""
    from packsmith.gui.main_window import MainWindow

    win, split = window
    win._bottom.show_panel("logs")
    total = sum(split.sizes())
    split.setSizes([total - 420, 420])
    win._on_bottom_dragged(split)
    win._save_bottom_height()
    name = win._profile.name

    reopened = MainWindow(profile_name=name)
    reopened.resize(1200, 800)
    reopened.show()
    try:
        reopened._bottom.show_panel("logs")
        assert reopened._h_split.widget(1).sizes()[1] == 420
    finally:
        _dispose(reopened)


def test_a_remembered_height_cannot_swallow_the_workspace(window):
    """A height dragged on a big monitor must not leave no workspace on a small one."""
    win, split = window
    win._bottom_height = 100_000
    win._bottom.show_panel("logs")

    workspace, bottom = split.sizes()
    assert workspace >= 150, f"the workspace was crushed to {workspace}px"
    assert bottom >= win._bottom.minimum_expanded_height()


def test_a_collapsed_panel_cannot_be_resized(window):
    """There is nothing behind the strip to reveal, so a drag could only stretch two
    fixed-height rows across empty space — which is what it did: `QVBoxLayout` handed the
    surplus out as spacing and the strip and status bar floated off the bottom edge."""
    win, split = window
    assert not win._bottom.is_expanded

    total = sum(split.sizes())
    split.setSizes([total - 400, 400])          # as though the divider were dragged

    assert win._bottom.height() == win._bottom.collapsed_height(), \
        "the collapsed panel grew and its rows would drift from the bottom edge"


def test_the_handle_is_dead_while_collapsed(window):
    """The height cap already makes the drag do nothing, but a live handle still shows a
    resize cursor and invites the attempt — which reads as the app being broken rather
    than the action being meaningless."""
    win, split = window
    assert not split.handle(1).isEnabled()

    win._bottom.show_panel("logs")
    assert split.handle(1).isEnabled()

    win._bottom.toggle()
    assert not split.handle(1).isEnabled()


def test_content_absorbs_the_spare_space_when_open(window):
    """The other half of the same fix: with the content stretching, the strip and status
    bar stay pinned to the bottom at any panel height."""
    from PySide6.QtWidgets import QApplication

    win, split = window
    win._bottom.show_panel("logs")
    total = sum(split.sizes())
    split.setSizes([total - 500, 500])
    # Child layout is queued: the panel's own geometry updates at once, but where its rows
    # sit inside it does not, so reading straight away sees the previous arrangement.
    QApplication.processEvents()

    panel = win._bottom
    # The STRIP's position inside the panel — not the button's inside the strip, which is
    # always ~0 and would pass no matter where the strip itself had drifted to.
    strip = panel._tabs["logs"].parentWidget().geometry()
    status = panel._status.parentWidget().geometry()

    assert panel._stack.geometry().height() > 300, "the content did not take the surplus"
    assert strip.top() == panel._stack.geometry().bottom() + 1
    assert status.bottom() >= panel.height() - 1, \
        "the status bar is floating rather than sitting on the bottom edge"


def test_dragging_while_collapsed_is_not_remembered(window):
    """Otherwise the collapsed height becomes the remembered one and the panel reopens
    shut."""
    win, split = window
    win._bottom_height = 420
    total = sum(split.sizes())
    split.setSizes([total - 46, 46])
    win._on_bottom_dragged(split)
    assert win._bottom_height == 420
