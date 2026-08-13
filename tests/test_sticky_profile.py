"""Remembering the last profile — design 4.1.

§4.1 promised it — *"Packsmith remembers the last active profile and auto-loads it on
startup"* — and nothing implemented it; launch picked `packsmith_test` if present and
otherwise whatever sorted first.

The breadcrumb is app **state**, not app config, and the two are kept in separate files on
purpose. Config (`main.toml`) is what the *user* writes, comments and all, and Packsmith
rewriting it to record which profile was open would destroy work that isn't its to touch.
State (`state.json`) is machine-owned, so it can be rewritten freely — and JSON because the
standard library reads TOML but cannot produce it.
"""
import json

import pytest

from packsmith.common import setup as app_setup
from packsmith.common.setup import load_state, save_state


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point the state file at a temp directory so tests never touch real userdata."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(app_setup.GLOBAL_PATHS, "config", config)
    return config


def state_file(state_dir):
    return state_dir / app_setup.STATE_FILE


# --- the state file itself ----------------------------------------------------------------

def test_nothing_remembered_yet_is_an_empty_dict(state_dir):
    """First run. Must not be an error — there is simply nothing to recall."""
    assert load_state() == {}


def test_what_is_saved_comes_back(state_dir):
    save_state(last_profile="deep_end")
    assert load_state()["last_profile"] == "deep_end"


def test_saving_merges_rather_than_replaces(state_dir):
    """So one caller cannot silently drop another's key the first time a second thing
    wants remembering."""
    save_state(last_profile="deep_end")
    save_state(something_else=42)

    state = load_state()
    assert state == {"last_profile": "deep_end", "something_else": 42}


def test_a_corrupt_state_file_reads_as_nothing(state_dir):
    """Unlike a malformed `main.toml`, which raises: nobody hand-wrote this file, so there
    is no intent to respect, and refusing to launch over a damaged breadcrumb would be a
    spectacular over-reaction to forgetting which profile was open."""
    state_file(state_dir).write_text("{not json at all", encoding="utf-8")
    assert load_state() == {}


def test_a_state_file_that_is_not_an_object_reads_as_nothing(state_dir):
    state_file(state_dir).write_text("[1, 2, 3]", encoding="utf-8")
    assert load_state() == {}


def test_an_unwritable_location_does_not_raise(state_dir, monkeypatch):
    """Failing to record a breadcrumb must never take down whatever the user was doing."""
    monkeypatch.setattr(app_setup.GLOBAL_PATHS, "config",
                        state_dir / "nested" / "\0illegal")
    save_state(last_profile="deep_end")      # must not raise


def test_state_is_written_as_readable_json(state_dir):
    """It is a debugging surface as much as a store — someone will open it by hand."""
    save_state(last_profile="deep_end")
    assert json.loads(state_file(state_dir).read_text(encoding="utf-8")) == {
        "last_profile": "deep_end"}


# --- choosing the startup profile -----------------------------------------------------------

@pytest.fixture
def window_class(monkeypatch, state_dir):
    """`MainWindow` with a controllable set of profiles, without building a window."""
    import packsmith.gui.main_window as mw

    def with_profiles(*names):
        monkeypatch.setattr(mw, "list_profiles", lambda: list(names))
        return mw.MainWindow
    return with_profiles


def test_the_remembered_profile_wins(window_class):
    save_state(last_profile="deep_end")
    assert window_class("packsmith_test", "deep_end")._default_profile() == "deep_end"


def test_a_remembered_profile_that_was_deleted_falls_through(window_class):
    """The breadcrumb is a convenience, never a dependency — a stale name must not block
    startup or open something that no longer exists."""
    save_state(last_profile="gone_forever")
    assert window_class("packsmith_test", "deep_end")._default_profile() == "packsmith_test"


def test_with_nothing_remembered_the_old_fallback_stands(window_class):
    assert window_class("aaa", "packsmith_test")._default_profile() == "packsmith_test"
    assert window_class("aaa", "bbb")._default_profile() == "aaa"


def test_no_profiles_at_all_is_still_the_empty_string(window_class):
    """First run opens the gated shell (§3.1) rather than inventing a profile."""
    save_state(last_profile="deep_end")
    assert window_class()._default_profile() == ""


# --- recording it ------------------------------------------------------------------------------

def test_entering_a_profile_records_it(state_dir):
    import packsmith.gui.main_window as mw
    mw.MainWindow._remember_profile("testicles")
    assert load_state()["last_profile"] == "testicles"


def test_an_empty_name_is_not_recorded(state_dir):
    """The blocked shell enters with no profile at all; remembering "" would then override
    a perfectly good previous answer with nothing."""
    save_state(last_profile="deep_end")
    import packsmith.gui.main_window as mw
    mw.MainWindow._remember_profile("")
    assert load_state()["last_profile"] == "deep_end"
