"""What was open, per profile, across restarts (design 4.1).

The machinery mostly existed: `_reopen_tabs` has always put tabs back after a packdump
rebuild, and `save_ui_state` already remembers panel heights, column layouts, view filters
and the selected job. This joins the two — the workspace is furniture like the rest of it.

Two things here would be **quietly wrong** rather than obviously broken, and they are what
this file is really about:

* **A kind that is persisted but not reopenable.** `_reopen_tabs` handles kinds by name and
  silently ignores any it does not know — its own docstring records that the packdump-diff
  tab went missing that way for a while. Persisting a kind it cannot rebuild would be the
  same bug with a longer fuse: the tab vanishes on the *next launch*, not this one.
* **A teardown recorded as the user's intent.** Switching profiles closes every tab before
  opening the new one. If that empty workspace were written back, the profile you left
  would come back empty next time — and you would blame the profile, not the switch.
"""
import inspect
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.common.setup import load_ui_state, save_ui_state
from packsmith.core.profile import Profile
from tests.test_profile_lifecycle import instance, userdata, with_packdump   # noqa: F401


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def profile(userdata, instance):
    with_packdump(instance)
    return Profile.create("workspace", loader="forge", mc_path=str(instance),
                          mc_version="1.20.1", loader_version="47.4.13")


@pytest.fixture
def window(profile, qapp):
    """A window on a profile with a real (if tiny) packdump, so the shell actually builds."""
    from packsmith.gui.main_window import MainWindow
    win = MainWindow(profile_name=profile.name)
    assert win._blocked is None, "the shell never built, so nothing here is being tested"
    yield win
    win.close()


def reopened(profile_name):
    """A second window on the same profile — the restart, in effect."""
    from packsmith.gui.main_window import MainWindow
    return MainWindow(profile_name=profile_name)


# --- the two lists have to agree -----------------------------------------------------

def test_every_persisted_kind_can_actually_be_reopened(qapp):
    """The failure with the long fuse. A kind added to `RESTORABLE_TABS` without a branch
    in `_reopen_tabs` writes fine, restores nothing, and does it a launch later — by which
    point the two edits are nowhere near each other."""
    from packsmith.gui.main_window import MainWindow

    source = inspect.getsource(MainWindow._reopen_tabs)
    for kind in MainWindow.RESTORABLE_TABS:
        assert f'== "{kind}"' in source, f"{kind} is persisted but never reopened"


def test_the_moment_shaped_tabs_are_left_out(qapp):
    """A run report, a file diff from a run, a packdump diff: each answers a question about
    one moment. Restoring one a week later presents an answer nobody asked for."""
    from packsmith.gui.main_window import MainWindow

    assert set(MainWindow.RESTORABLE_TABS).isdisjoint(
        {"report", "diff", "packdump-diff"})


# --- the round trip ------------------------------------------------------------------

def test_what_was_open_comes_back(window, profile, qapp):
    window._open_browse("minecraft:item")
    window._open_encyclopedia()
    window._remember_open_tabs()
    window.close()

    again = reopened(profile.name)
    try:
        assert ("browse", "minecraft:item") in again._open_tabs
        assert ("encyclopedia",) in again._open_tabs
    finally:
        again.close()


def test_nothing_open_stays_nothing_open(window, profile, qapp):
    """An empty workspace must not be a reason to restore the *previous* session — and the
    stored list has to be able to say "nothing", not just fail to say anything."""
    window._open_browse("minecraft:item")
    window._remember_open_tabs()
    window._workspace.close_widget(window._open_tabs[("browse", "minecraft:item")])
    window._remember_open_tabs()
    window.close()

    again = reopened(profile.name)
    try:
        assert again._workspace.widgets() == []
    finally:
        again.close()


def test_the_order_is_the_order_they_are_in(window, profile, qapp):
    """Not the order they were opened. Tabs are movable, and `_open_tabs` is a dict in
    insertion order — the two stop agreeing the first time anyone drags one."""
    window._open_browse("minecraft:item")
    window._open_encyclopedia()
    tabs = window._workspace._tabs
    tabs.tabBar().moveTab(1, 0)               # drag the encyclopedia in front
    window._remember_open_tabs()

    stored = load_ui_state(profile.name)["open_tabs"]
    assert [tuple(key) for key in stored] == [
        ("encyclopedia",), ("browse", "minecraft:item")]


def test_the_tab_you_were_looking_at_comes_back_in_front(window, profile, qapp):
    window._open_browse("minecraft:item")
    window._open_encyclopedia()
    window._workspace.focus_widget(window._open_tabs[("browse", "minecraft:item")])
    window._remember_open_tabs()
    window.close()

    again = reopened(profile.name)
    try:
        assert again._workspace.current_widget() is \
            again._open_tabs[("browse", "minecraft:item")]
    finally:
        again.close()


def test_a_tab_whose_subject_is_gone_simply_does_not_come_back(window, profile, qapp):
    """Best-effort, like `_reopen_tabs` has always been. An empty tab claiming to show a
    deleted view would be worse than its absence."""
    save_ui_state(profile.name, open_tabs=[["view", 4242], ["browse", "minecraft:item"]])
    window.close()

    again = reopened(profile.name)
    try:
        assert ("browse", "minecraft:item") in again._open_tabs
        assert ("view", 4242) not in again._open_tabs
    finally:
        again.close()


def test_junk_in_the_stored_list_does_not_stop_the_window_opening(window, profile, qapp):
    save_ui_state(profile.name, open_tabs=[[], ["nonsense", 1], ["browse", "minecraft:item"]])
    window.close()

    again = reopened(profile.name)
    try:
        assert ("browse", "minecraft:item") in again._open_tabs
    finally:
        again.close()


# --- what must not be recorded --------------------------------------------------------

def test_closing_everything_for_a_profile_switch_is_not_a_decision(window, profile, qapp):
    """The bug with the worst symptom: leave a profile, come back, and it is empty — which
    reads as the profile having lost your work rather than the switch having done it.

    Closing every tab queues a write for each one. Both halves of the guard are checked
    here, because either alone leaves a hole: the freeze stops a write that lands *during*
    the teardown, and stopping the timer stops the one that would land just after it, while
    `_profile` still names the profile being left.
    """
    window._open_browse("minecraft:item")
    window._remember_open_tabs()

    assert window._close_all_tabs() is True

    assert window._tab_memory_timer.isActive() is False, "a write was still queued"
    stored = load_ui_state(profile.name)["open_tabs"]
    assert [tuple(key) for key in stored] == [("browse", "minecraft:item")]


def test_a_frozen_memory_ignores_a_write(window, profile, qapp):
    """The freeze half, on its own — `_close_all_tabs` and `_restore_open_tabs` both rely
    on a write arriving mid-operation being dropped rather than merely unscheduled."""
    window._open_browse("minecraft:item")
    window._remember_open_tabs()

    window._tab_memory_frozen = True
    window._workspace.close_widget(window._open_tabs[("browse", "minecraft:item")])
    window._remember_open_tabs()

    stored = load_ui_state(profile.name)["open_tabs"]
    assert [tuple(key) for key in stored] == [("browse", "minecraft:item")]


def test_restoring_does_not_rewrite_what_it_is_reading(window, profile, qapp):
    """Reopening fires the same signals a user opening a tab does. A half-restored list
    written back over the stored one would erase whatever had not been reached yet."""
    save_ui_state(profile.name, open_tabs=[["browse", "minecraft:item"], ["encyclopedia"]])
    frozen = []
    original = window._remember_open_tabs
    window._remember_open_tabs = lambda: frozen.append(window._tab_memory_frozen)

    window._restore_open_tabs()
    window._remember_open_tabs = original

    assert all(frozen), "a write during the restore would have seen a partial list"
    assert window._tab_memory_frozen is False, "and the freeze has to lift afterwards"


def test_a_refused_switch_does_not_leave_the_memory_frozen(window, profile, qapp):
    """`_close_all_tabs` returns False when a dirty buffer refuses. Leaving the freeze on
    from a branch that changed nothing would silently stop the memory updating for the rest
    of the session."""
    window._open_browse("minecraft:item")
    window._workspace.close_guard = lambda _widget: False

    assert window._close_all_tabs() is False
    assert window._tab_memory_frozen is False


def test_transient_tabs_are_not_recorded(window, profile, qapp):
    """A report tab is open in the workspace like anything else; it just must not be in the
    list, or the next launch would open a report of a run from last week."""
    from packsmith.core import reports
    from packsmith.gui.run_report import RunReportTab

    class Step:
        action_ref, status, reason, changes, run_id = "p:a", "success", None, [], 1
        log_lines = ()

    class Result:
        job_name, status, dry_run, not_run = "Removal", "success", False, 0
        step_results = [Step()]

    report = reports.from_result(Result(), key=("report", 1))
    window._open_browse("minecraft:item")
    tab = RunReportTab(report)
    window._workspace.add_tab(tab, "Run: Removal")
    window._open_tabs[("report", 1)] = tab

    window._remember_open_tabs()

    stored = load_ui_state(profile.name)["open_tabs"]
    assert [tuple(key) for key in stored] == [("browse", "minecraft:item")]


# --- tab keys are not all the same shape ----------------------------------------------
#
# Reported as a crash after a job run: `too many values to unpack (expected 2)`, from
# `_reload_job_tabs`. Three loops unpacked every key as a fixed `(kind, key)` pair, which
# holds right up until a tag view, the encyclopedia, or a run-report diff is open — because
# only the FIRST element of a key is the kind, and everything after it belongs to that kind.
# The traceback pointed at job tabs and the cause was an open tag view.

# Every shape the app builds, shortest to longest. Kept explicit rather than derived: the
# point is that the lengths differ, which a clever derivation would hide.
KEY_SHAPES = [
    ("encyclopedia",),
    ("job", 1),
    ("view", 1),
    ("browse", "minecraft:item"),
    ("action", "p:a"),
    ("report", 1),
    ("packdump-diff", "latest"),
    ("doc", "instance:config/x.json"),
    ("blueprint", "StoneType"),
    ("tag", "minecraft:item", "remove"),
    ("diff", ("report", 1), "config/x.json"),
]


@pytest.mark.parametrize("key", KEY_SHAPES, ids=[k[0] for k in KEY_SHAPES])
def test_refreshing_survives_every_kind_of_open_tab(window, key, qapp):
    """Each refresher walks every open tab, so one unfamiliar key shape takes down a pass
    that had nothing to do with it."""
    window._open_tabs[key] = object()
    try:
        window._reload_job_tabs()
        window._reload_blueprint_tabs()
        window._refresh_tab_icons()
    finally:
        window._open_tabs.pop(key, None)


def test_the_kind_filter_reads_only_the_first_element(window, qapp):
    window._open_tabs.clear()
    window._open_tabs[("tag", "minecraft:item", "remove")] = "tag tab"
    window._open_tabs[("job", 7)] = "job tab"
    window._open_tabs[("encyclopedia",)] = "encyclopedia tab"

    assert [tab for _key, tab in window._tabs_of_kind("job")] == ["job tab"]
    assert [tab for _key, tab in window._tabs_of_kind("tag")] == ["tag tab"]
    assert [tab for _key, tab in window._tabs_of_kind("encyclopedia")] == \
        ["encyclopedia tab"]
    assert list(window._tabs_of_kind("nothing")) == []
