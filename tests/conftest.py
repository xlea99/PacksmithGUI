"""Shared fixtures for the Packsmith unit suite.

Every unit test runs against its own isolated database, created fresh from
pytest's ``tmp_path`` and discarded when the test ends. This hermetic setup is
deliberate, not a shortcut: no shared state means tests are fast and
order-independent, so any test runs correctly alone or in any combination.

Tests that need a real Minecraft instance and packdump on disk are integration
tests and are kept out of this suite.
"""
import pytest

from packsmith.common import setup as app_setup
from packsmith.core.db import UserDB
from packsmith.core.tags import TagStore


@pytest.fixture(autouse=True)
def isolated_app_state(tmp_path, monkeypatch):
    """Keep every test out of the developer's real `userdata/config/`.

    App state (`state.json`) records the last-opened profile and remembered panel layouts,
    and anything constructing a `MainWindow` writes to it. Without this, running the suite
    quietly rewrote the developer's own state — a throwaway test profile became the one
    that opened on next launch, and probe profiles accumulated layout entries forever.

    Autouse rather than opt-in: the writes happen deep inside window construction, so a
    test cannot reasonably know it needs the protection. It was a *debounced* write that
    exposed this — a timer that outlived its test and landed in the real file after
    teardown had already restored the path.
    """
    config = tmp_path / "app-config"
    config.mkdir()
    monkeypatch.setattr(app_setup.GLOBAL_PATHS, "config", config)
    return config


@pytest.fixture
def user_db(tmp_path):
    """An isolated :class:`UserDB` for a single test.

    ``tmp_path`` gives each test a private directory, so every test gets a clean
    ``profile.db`` with no leakage between tests. The connection is closed on teardown.
    """
    db = UserDB(tmp_path / "profile.db")
    yield db
    db.close()


@pytest.fixture
def tags(user_db):
    """A :class:`TagStore` over a clean, isolated database."""
    return TagStore(user_db)
