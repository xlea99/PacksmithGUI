"""Spacing inside a document tab — design 4.2.

Qt lays a tab out as `padding-left | icon | text | padding-right | close-button` and then
pins the close button to the tab's **right edge**. The right padding therefore lands in
front of the ✕ rather than behind it, so the stock arrangement gives a wide gap before the
button and none at all after it — measured at 31px in front and **1px** behind. The ✕ read
as jammed against the tab border, because it was.

Qt offers no margin for a tab button, so the margin has to be part of the widget handed
over. That is easy to "simplify" away later by passing the button directly, which is why
this measures the real geometry rather than trusting the constants.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QTabBar, QWidget


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def workspace(qapp):
    from packsmith.gui.shell.workspace import Workspace

    ws = Workspace()
    ws.resize(600, 80)
    ws.add_tab(QWidget(), "options.txt")
    ws.add_tab(QWidget(), "emi.json")
    # Shown, because tab buttons are positioned by the tab bar's layout and an unshown
    # widget never runs one — the buttons sit at the origin and every measurement below
    # would be of nothing.
    ws.show()
    qapp.processEvents()
    yield ws
    ws.deleteLater()


def close_button(ws, index):
    """The ✕ itself and its position within the tab bar — it lives inside a spacer, so its
    own geometry is relative to that."""
    from PySide6.QtWidgets import QPushButton

    holder = ws._tabs.tabBar().tabButton(index, QTabBar.RightSide)
    button = holder.findChild(QPushButton)
    return button, holder.x() + button.x(), holder.x() + button.x() + button.width()


@pytest.mark.parametrize("index", [0, 1])
def test_the_close_button_is_not_flush_against_the_tab_edge(workspace, index):
    """The complaint, as a number. Anything under a few pixels reads as a rendering fault
    rather than as a button."""
    rect = workspace._tabs.tabBar().tabRect(index)
    _, _, right = close_button(workspace, index)

    margin = rect.right() - right
    assert margin >= 4, f"only {margin}px between the ✕ and the tab's edge"


def test_every_tab_gets_the_same_margin(workspace):
    """A margin that depends on the title length would be a layout accident that happens to
    look right on one tab."""
    margins = []
    for index in range(workspace._tabs.count()):
        rect = workspace._tabs.tabBar().tabRect(index)
        margins.append(rect.right() - close_button(workspace, index)[2])
    assert len(set(margins)) == 1, f"inconsistent margins: {margins}"


def test_the_spacer_is_what_provides_the_margin(workspace):
    """If someone passes the button to `setTabButton` directly, Qt pins it to the edge again
    and the margin silently goes back to nothing."""
    holder = workspace._tabs.tabBar().tabButton(0, QTabBar.RightSide)
    button, _, _ = close_button(workspace, 0)

    assert button is not holder, "the button was handed to Qt bare"
    assert holder.width() > button.width(), "the spacer reserves no room"


def test_the_close_button_still_closes_its_own_tab(workspace):
    """The spacer sits between the button and the tab bar, so it is a new place for the
    click to get lost."""
    closed = []
    workspace.tab_closed.connect(closed.append)
    first = workspace.widgets()[0]

    close_button(workspace, 0)[0].click()

    assert closed == [first]
    assert workspace._tabs.count() == 1


def test_the_click_survives_a_dragged_tab(workspace):
    """Tabs are movable, so an index captured at build time goes stale. The button closes
    by widget for exactly this reason."""
    first, second = workspace.widgets()
    workspace._tabs.tabBar().moveTab(0, 1)

    close_button(workspace, 0)[0].click()      # index 0 is now the *second* tab added

    assert workspace.widgets() == [first]


# --- middle-click to close ---------------------------------------------------------------

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest


def centre(ws, index):
    return ws._tabs.tabBar().tabRect(index).center()


def middle_click(ws, pos):
    QTest.mouseClick(ws._tabs.tabBar(), Qt.MiddleButton, Qt.NoModifier, pos)


def test_middle_clicking_a_tab_closes_it(workspace):
    first, second = workspace.widgets()
    middle_click(workspace, centre(workspace, 0))
    assert workspace.widgets() == [second]


def test_middle_clicking_works_anywhere_on_the_tab_not_just_the_x(workspace):
    """The whole point — the ✕ is a small target and the tab is a large one."""
    first, second = workspace.widgets()
    rect = workspace._tabs.tabBar().tabRect(0)
    middle_click(workspace, rect.topLeft() + QPoint(6, rect.height() // 2))
    assert workspace.widgets() == [second]


def test_middle_clicking_empty_space_closes_nothing(workspace):
    """There is bar to the right of the last tab, and it is easy to hit.

    The bar is widened first because it otherwise sizes exactly to its tabs, leaving no
    empty space to aim at — the point has to genuinely miss every tab or this proves
    nothing. Widening alone is not enough either: a tab bar stretches its tabs to fill by
    default, so that has to be turned off before any of the bar is actually bare.
    """
    bar = workspace._tabs.tabBar()
    bar.setExpanding(False)
    bar.resize(bar.width() + 60, bar.height())
    beyond = QPoint(bar.width() - 5, bar.height() // 2)
    assert bar.tabAt(beyond) == -1, "the probe point is not actually empty"

    middle_click(workspace, beyond)
    assert workspace._tabs.count() == 2


def test_a_middle_click_dragged_off_its_tab_is_cancelled(workspace):
    """Released over a different tab than it was pressed on, so the user changed their
    mind — closing either one would be a surprise, and closing the *other* one would be a
    bug that eats work."""
    bar = workspace._tabs.tabBar()
    QTest.mousePress(bar, Qt.MiddleButton, Qt.NoModifier, centre(workspace, 0))
    QTest.mouseRelease(bar, Qt.MiddleButton, Qt.NoModifier, centre(workspace, 1))

    assert workspace._tabs.count() == 2


def test_an_unsaved_tab_can_still_veto_a_middle_click(workspace):
    """The ✕ asks before dropping unsaved work. A second way to close that skips the guard
    would be a way to lose it silently."""
    asked = []
    workspace.close_guard = lambda widget: asked.append(widget) or False

    middle_click(workspace, centre(workspace, 0))

    assert asked, "the guard was never consulted"
    assert workspace._tabs.count() == 2, "the tab closed despite the veto"


def test_middle_clicking_the_x_itself_closes_the_tab_too(workspace):
    """The ✕ sits on top of the tab, so a middle-click aimed at a tab can land on it. A
    button that swallowed the event would leave one dead spot in the middle of an
    otherwise middle-clickable tab, which reads as the feature being flaky rather than as
    a rule."""
    first, second = workspace.widgets()
    button, _, _ = close_button(workspace, 0)

    QTest.mouseClick(button, Qt.MiddleButton, Qt.NoModifier, button.rect().center())

    assert workspace.widgets() == [second]


# --- how wide a tab is, and how it reacts to the pointer ------------------------------------

def text_rect(ws, index):
    from PySide6.QtWidgets import QStyle, QStyleOptionTab

    bar = ws._tabs.tabBar()
    opt = QStyleOptionTab()
    bar.initStyleOption(opt, index)
    return bar.style().subElementRect(QStyle.SE_TabBarTabText, opt, bar)


def one_tab(qapp, title, icon=None):
    from packsmith.gui.shell.workspace import Workspace

    ws = Workspace()
    ws.resize(900, 80)
    ws.add_tab(QWidget(), title, icon=icon)
    ws.show()
    qapp.processEvents()
    return ws


@pytest.mark.parametrize("title", [
    "a.json", "options.txt", "usercache.json", "Quark-4.0-462.jar",
    "some-mod-with-a-long-config-name.toml",
])
def test_a_tighter_tab_still_fits_its_title(qapp, title):
    """Document tabs reclaim the width Qt reserves for a close indicator it never draws.
    Take one pixel too many and nothing looks tight — the title quietly elides to
    "options.tx…", which is a worse bug than the padding ever was.
    """
    from packsmith.gui.shell import icons, style

    ws = one_tab(qapp, title, icons.file_icon(title, colour=style.TEXT))
    try:
        granted = text_rect(ws, 0).width()
        needed = ws._tabs.tabBar().fontMetrics().horizontalAdvance(title)
        assert granted >= needed, f"{title!r} is elided — {granted}px for {needed}px of text"
    finally:
        ws.deleteLater()


def test_the_reclaim_is_real(qapp):
    """If the override stops applying, tabs silently go back to being padded out by a
    button Qt reserved but never draws."""
    from PySide6.QtWidgets import QTabBar
    from packsmith.gui.shell import icons, style

    ws = one_tab(qapp, "options.txt", icons.file_icon("options.txt", colour=style.TEXT))
    try:
        bar = ws._tabs.tabBar()
        stock = QTabBar.tabSizeHint(bar, 0).width()
        assert bar.tabSizeHint(0).width() < stock, "the tab is as wide as Qt's default again"
    finally:
        ws.deleteLater()


def test_a_tab_with_no_icon_is_left_at_its_full_width(qapp):
    """Views, job editors and packdump diffs carry no icon, and Qt grants those barely a
    pixel of slack to begin with — measured, 1px against a document tab's 13px. The first
    version of the reclaim took a flat amount off every tab and elided all of them. The
    rule is not "trim tabs", it is "trim the surplus an icon creates".
    """
    from PySide6.QtWidgets import QTabBar

    ws = one_tab(qapp, "Job: Nightly Removal")
    try:
        bar = ws._tabs.tabBar()
        assert bar.tabSizeHint(0).width() == QTabBar.tabSizeHint(bar, 0).width()
    finally:
        ws.deleteLater()


# --- hover feedback -------------------------------------------------------------------------

def hover(qapp, ws, index):
    """Put the pointer on a tab and return the bar's pixels.

    QTabBar tracks its hovered tab from *hover* events — a synthetic `QTest.mouseMove`
    leaves `State_MouseOver` unset and the stylesheet never fires. Passing -1 for the index
    parks the pointer off every tab, which is how a baseline is taken: the offscreen
    platform reports a phantom cursor at the origin, so a bar that has never been touched
    already has its first tab lit.
    """
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QHoverEvent

    bar = ws._tabs.tabBar()
    point = QPointF(bar.tabRect(index).center()) if index >= 0 \
        else QPointF(bar.width() + 50, -50)
    # The (pos, globalPos, oldPos) form — the two-point one is deprecated in Qt 6.
    qapp.sendEvent(bar, QHoverEvent(QEvent.HoverMove, point, point, QPointF(-1, -1)))
    qapp.processEvents()
    return bar.grab().toImage()


def tab_colour(image, ws, index):
    """The tab's own background, sampled clear of its icon, title and ✕."""
    rect = ws._tabs.tabBar().tabRect(index)
    return image.pixelColor(rect.x() + 3, rect.y() + 3).name()


def test_hovering_an_unselected_tab_lights_it(qapp, workspace):
    """Feedback that the thing under the pointer is a thing you can click."""
    from packsmith.gui.shell import style

    workspace._tabs.setCurrentIndex(1)
    at_rest = hover(qapp, workspace, -1)
    assert tab_colour(at_rest, workspace, 0) == style.BG_CHROME

    lit = hover(qapp, workspace, 0)
    assert tab_colour(lit, workspace, 0) == style.BG_HOVER


def test_hovering_the_selected_tab_changes_nothing(qapp, workspace):
    """The selected tab is already the brightest thing on the bar. Lighting it further
    would say "you are about to switch to this" about the tab you are looking at, so the
    highlight has to mean exactly one thing."""
    workspace._tabs.setCurrentIndex(1)
    at_rest = hover(qapp, workspace, -1)
    before = tab_colour(at_rest, workspace, 1)

    lit = hover(qapp, workspace, 1)
    assert tab_colour(lit, workspace, 1) == before, "the selected tab reacted to the pointer"


def test_the_tab_that_slides_under_the_pointer_lights_up(qapp):
    """Closing a tab slides the rest left under a pointer that hasn't moved.

    Qt caches the hovered tab as a rectangle and only recomputes when the pointer leaves
    it — so the tab now under the cursor stayed unlit, and because the pointer was still
    inside the stale rect, moving it didn't help either. Only leaving the bar and coming
    back did.

    This drives the real cursor rather than a stub, because the position the code reads is
    the system one; a test that fed it a coordinate would pass without proving the lookup
    works.
    """
    from PySide6.QtGui import QCursor
    from packsmith.gui.shell import icons, style
    from packsmith.gui.shell.workspace import Workspace

    ws = Workspace()
    ws.resize(700, 60)
    for name in ("a.json", "b.json", "c.json", "d.json"):
        ws.add_tab(QWidget(), name, icon=icons.file_icon(name, colour=style.TEXT))
    ws.show()
    qapp.processEvents()
    bar = ws._tabs.tabBar()
    # Selected second, so that neither the tab under the pointer now nor the one that
    # slides under it later is the selected one — a selected tab correctly ignores hover,
    # and this would otherwise measure that instead.
    ws._tabs.setCurrentIndex(1)
    qapp.processEvents()

    try:
        QCursor.setPos(bar.mapToGlobal(bar.tabRect(2).center()))
        hover(qapp, ws, 2)
        assert tab_colour(bar.grab().toImage(), ws, 2) == style.BG_HOVER, \
            "the pointer was never on tab 2 to begin with"

        ws.close_widget(ws.widgets()[0])        # everything shifts left; cursor unmoved
        qapp.processEvents()

        under = bar.tabAt(bar.mapFromGlobal(QCursor.pos()))
        assert under >= 0, "the cursor no longer sits on a tab — the test proves nothing"
        assert under != ws._tabs.currentIndex(), \
            "the cursor landed on the selected tab, which is meant to ignore hover"
        assert tab_colour(bar.grab().toImage(), ws, under) == style.BG_HOVER, \
            "the tab that slid under the pointer did not light up"
    finally:
        ws.deleteLater()


def test_closing_the_last_tab_does_not_go_looking_for_one(qapp):
    """The resync runs on an empty bar too, where there is nothing to hover."""
    from packsmith.gui.shell.workspace import Workspace

    ws = Workspace()
    ws.resize(400, 60)
    ws.add_tab(QWidget(), "only.json")
    ws.show()
    qapp.processEvents()
    try:
        ws.close_widget(ws.widgets()[0])        # must not raise
        assert ws._tabs.count() == 0
    finally:
        ws.deleteLater()


def test_the_highlight_is_distinguishable_from_both_other_states(qapp):
    """A hover the same colour as a resting tab is no feedback, and one the same colour as
    the selected tab says the wrong thing."""
    from packsmith.gui.shell import style

    assert style.BG_HOVER not in (style.BG_CHROME, style.BG_DEEP)


def test_left_clicking_a_tab_still_just_selects_it(workspace):
    """The filter sees every mouse event on the bar, so it is a place where ordinary
    clicking could quietly break."""
    first, second = workspace.widgets()
    QTest.mouseClick(workspace._tabs.tabBar(), Qt.LeftButton, Qt.NoModifier,
                     centre(workspace, 0))

    assert workspace._tabs.count() == 2
    assert workspace.current_widget() is first
