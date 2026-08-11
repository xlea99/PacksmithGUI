"""The gated first-launch shell and every way out of it (design 3.1).

§3.1 gates PackSmith on its first packdump. A gate you cannot pass is just a wall, so this
covers the escape hatches — which is the coverage whose absence let all four of them ship
broken: the shell has `_profile = None` and never builds a workspace, and four recovery
paths dereferenced one or the other.
"""
import os
import pathlib
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    yield app


@pytest.fixture
def no_profiles(monkeypatch):
    """A machine with nothing set up at all."""
    import packsmith.gui.main_window as mw

    class _Cancelled:
        result_name = None
        result_deleted = None

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return 0

    monkeypatch.setattr(mw, "list_profiles", lambda: [])
    monkeypatch.setattr(mw, "OpenProfileDialog", _Cancelled)
    monkeypatch.setattr(mw, "NewProfileDialog", _Cancelled)
    return mw


def test_first_launch_builds_a_window_rather_than_raising(no_profiles):
    win = no_profiles.MainWindow()
    assert win._blocked
    assert win._profile is None


@pytest.mark.parametrize("path", [
    "_open_profile",            # the "Profiles…" button
    "_new_profile",             # File > New Profile
    "_check_for_new_packdump",  # refocusing after running the game
    "_close_all_tabs",          # reached by any profile switch
])
def test_every_recovery_path_survives_the_blocked_shell(no_profiles, path):
    """They didn't raise loudly either — the excepthook swallowed it, so the buttons just
    appeared to do nothing."""
    win = no_profiles.MainWindow()
    getattr(win, path)()


def test_closing_tabs_is_vacuously_done_when_there_is_no_workspace(no_profiles):
    """It must return True: False means "the user cancelled", which would silently abort
    the profile switch that is the way out."""
    win = no_profiles.MainWindow()
    assert win._close_all_tabs() is True


def test_switching_into_a_profile_from_the_blocked_shell(no_profiles, monkeypatch):
    """The gesture after File > New Profile — there is no previous profile to compare
    against, close, or fall back to."""
    from packsmith.core.profile import Profile, delete_profile
    win = no_profiles.MainWindow()

    root = pathlib.Path(tempfile.mkdtemp()) / "instance"
    root.mkdir(parents=True)
    name = "blocked_shell_probe"
    try:
        delete_profile(name)
    except Exception:
        pass
    Profile.create(name, mc_path=str(root), loader="forge",
                   loader_version="47.4.10", mc_version="1.20.1")
    try:
        win._switch_profile(name)          # must not raise
        assert win._profile.name == name
        assert win._blocked                # still gated: this instance has no dump
    finally:
        try:
            delete_profile(name)
        except Exception:
            pass
