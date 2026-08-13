"""The bottom panel's strip — design 4.1.

The panel used to carry the same four names twice: a `QTabWidget`'s own tab bar, and a row
of buttons in the status strip that merely jumped to it. Two controls for one choice, and
the status bar's right-hand half spent on the duplicate.

Now the strip *is* the tabs, IntelliJ-style — three rows, top to bottom: content (only when
open), the strip, the true status bar. The strip never moves, so opening and closing the
panel shifts nothing underneath the cursor, and the status bar is purely status again.

The behaviour that makes it a handle rather than a tab bar is the third case below: clicking
the tab that is already showing **closes** the panel.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.gui.shell.bottom_panel import BOTTOM_TABS, BottomPanel


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def panel(qapp):
    p = BottomPanel()
    yield p
    p.deleteLater()


def click(panel, key):
    panel._tabs[key].click()


# --- the strip is the only set of tab labels -------------------------------------------

def test_there_is_exactly_one_control_per_tab(panel):
    """The redundancy this replaced: two rows offering the same four choices."""
    assert set(panel._tabs) == {key for key, _t, _b in BOTTOM_TABS}
    from PySide6.QtWidgets import QTabBar
    assert panel.findChildren(QTabBar) == [], "a second row of tab labels came back"


def test_the_strip_stays_visible_when_collapsed(panel):
    """It is the handle. A handle that disappears when you use it cannot be used again."""
    panel.show()
    panel.collapse()
    assert all(button.isVisible() for button in panel._tabs.values())
    assert not panel._stack.isVisible()


# --- clicking ----------------------------------------------------------------------------

def test_it_starts_collapsed(panel):
    """§4.1: closed by default, so it never steals focus from the workspace."""
    assert not panel.is_expanded


def test_clicking_a_tab_while_collapsed_opens_it_there(panel):
    click(panel, "errors")
    assert panel.is_expanded
    assert panel.current_key == "errors"


def test_clicking_the_open_tab_closes_the_panel(panel):
    """What makes the strip a handle and not just a tab bar."""
    click(panel, "errors")
    click(panel, "errors")
    assert not panel.is_expanded


def test_clicking_a_different_tab_switches_without_closing(panel):
    """The case that must NOT collapse — otherwise moving between tabs would shut the
    panel every other click."""
    click(panel, "errors")
    click(panel, "packdump")

    assert panel.is_expanded
    assert panel.current_key == "packdump"


def test_reopening_returns_to_the_last_tab(panel):
    click(panel, "packdump")
    click(panel, "packdump")          # close
    click(panel, "packdump")          # open again
    assert panel.current_key == "packdump"


# --- highlighting -------------------------------------------------------------------------

def test_only_the_open_tab_is_lit(panel):
    click(panel, "errors")
    lit = [key for key, button in panel._tabs.items() if button.isChecked()]
    assert lit == ["errors"]


def test_nothing_is_lit_while_collapsed(panel):
    """A lit tab over a shut panel would claim something is showing that isn't."""
    click(panel, "errors")
    click(panel, "errors")
    assert not any(button.isChecked() for button in panel._tabs.values())


# --- opening by key, not by position --------------------------------------------------------

def test_show_panel_opens_the_named_tab(panel):
    panel.show_panel("job_results")
    assert panel.is_expanded and panel.current_key == "job_results"


def test_an_unknown_key_does_nothing_rather_than_opening_tab_zero(panel):
    """Silently opening Logs because a key was misspelled would look like the feature
    working."""
    panel.show_panel("nonexistent")
    assert not panel.is_expanded


def test_expand_still_accepts_a_position(panel):
    """Kept for compatibility, but every call site uses keys — a bare index quietly means
    a different tab the moment BOTTOM_TABS is reordered, which is exactly the bug that
    made surfacing Errors jump to Job Results."""
    panel.expand(2)
    assert panel.current_key == BOTTOM_TABS[2][0]


# --- telling the window when to make room ------------------------------------------------------

def test_opening_and_closing_are_announced(panel):
    """A splitter told to hold the panel at its collapsed height keeps doing so forever,
    so the panel has to say when it opens or the content appears as a sliver."""
    seen = []
    panel.expanded_changed.connect(seen.append)

    click(panel, "errors")
    click(panel, "errors")
    assert seen == [True, False]


def test_switching_tabs_is_not_an_open_event(panel):
    """It was already open. Re-announcing would make the window resize on every tab
    click, throwing away a height the user had dragged to."""
    click(panel, "errors")
    seen = []
    panel.expanded_changed.connect(seen.append)

    click(panel, "packdump")
    assert seen == []


def test_the_collapsed_height_covers_both_permanent_rows(panel):
    """The strip is furniture now rather than part of the content, so a shut panel is
    taller than it used to be by exactly one strip."""
    from packsmith.gui.shell.bottom_panel import STATUS_HEIGHT, STRIP_HEIGHT
    assert panel.collapsed_height() == STRIP_HEIGHT + STATUS_HEIGHT
    assert panel.expanded_height() > panel.collapsed_height()


# --- content still works --------------------------------------------------------------------

def test_logging_reaches_the_logs_tab(panel):
    panel.log("Profile 'deep_end' loaded")
    assert "deep_end" in panel._log_view.toPlainText()


def test_a_real_panel_replaces_its_stub(panel):
    from PySide6.QtWidgets import QLabel
    real = QLabel("the real errors view")
    panel.set_panel("errors", real)

    assert panel.panel("errors") is real
    panel.show_panel("errors")
    assert panel._stack.currentWidget() is real


def test_replacing_a_panel_keeps_the_tab_order(panel):
    """The stack is positional and the strip is not, so a replacement that appended rather
    than inserted would leave every tab after it opening the wrong content."""
    from PySide6.QtWidgets import QLabel
    panel.set_panel("job_results", QLabel("results"))

    panel.show_panel("packdump")
    assert panel.current_key == "packdump"
    panel.show_panel("job_results")
    assert panel.current_key == "job_results"


def test_the_status_bar_is_independent_of_the_tabs(panel):
    """It is the true footer now — it must read the same whatever the panel is doing."""
    panel.set_status("1,983 items in minecraft:item")
    click(panel, "errors")
    click(panel, "errors")
    assert panel._status.text() == "1,983 items in minecraft:item"
