"""Shared fixtures for the PackSmith unit suite.

Every unit test runs against its own isolated database, created fresh from
pytest's ``tmp_path`` and discarded when the test ends. This hermetic setup is
deliberate, not a shortcut: no shared state means tests are fast and
order-independent, so any test runs correctly alone or in any combination.

Tests that need a real Minecraft instance and packdump on disk are integration
tests and are kept out of this suite.
"""
import pytest

from packsmith.core.db import UserDB
from packsmith.core.tags import TagStore


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
