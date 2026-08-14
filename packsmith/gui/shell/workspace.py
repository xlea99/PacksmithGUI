"""The main workspace (design 4.1/4.2): a single tab bar hosting heterogeneous tabs.

All tab types are peers — View tabs, editor tabs, job editors, whatever comes later —
which is what §4.2 means by "one Qt-owned tab bar." When nothing is open the workspace
shows a hint pointing at the sidebar, because the sidebar is how you open things.
"""
from PySide6.QtCore import QEvent, QPointF, Qt, Signal
from PySide6.QtGui import QCursor, QHoverEvent
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget, QTabWidget, QLabel,
    QPushButton, QTabBar)

from packsmith.gui.shell import icons, style

# Qt lays a tab out as `padding-left | icon | text | padding-right | close-button`, and then
# puts the close button flush against the tab's right edge — the right padding lands *before*
# it, not after. So the stock arrangement gives a wide gap in front of the ✕ and none behind
# it, which is exactly backwards. Both numbers are set here instead: the padding buys the
# gap in front, and the button carries its own margin behind.
CLOSE_MARGIN = 5        # breathing room between the ✕ and the tab's right edge
CLOSE_GAP = 0           # between the tab title and the ✕
CLOSE_BUTTON = 14       # the hit target around the glyph
#
# The vertical padding is deliberately lopsided. Qt centres the label on the font's line
# box, and a line box reserves room for a descender whether or not the title has one — so
# an evenly padded tab renders its text a pixel below where the eye expects it. A pixel off
# the top puts the label back on the tab's middle.
PAD_TOP, PAD_BOTTOM = 4, 6


class _TabBar(QTabBar):
    """A tab bar that doesn't pay for its close button twice.

    Qt reserves room for a close *indicator* in every tab's size hint, and then separately
    lays out the button widget actually set on the tab — so a tab ends up about a button
    wider than its contents need. The surplus is not wasted at the end; the title is
    centred in what's left, so it lands as padding on **both** sides of the text. That is
    what made the tabs look inflated: a gap in front of the ✕ and a matching one after the
    icon, neither of which anything asked for.

    Reclaiming it here rather than by trimming the stylesheet padding, because padding
    can't go below zero and this surplus is larger than the padding was.

    **Only tabs that carry an icon have it to give.** Measured against stock Qt, a tab with
    an icon is granted 13px more than its title needs while a bare one — a View, a job
    editor, a packdump diff — is granted 1px. Taking a flat amount off both is how the
    first attempt at this quietly elided every tab in the app that isn't a document, so
    the reclaim is conditional and deliberately smaller than the full 13: fonts and DPI
    move that number around, and the failure mode on the wrong side of it is a truncated
    title rather than a slightly wide tab.
    """

    RECLAIM = 8

    def tabSizeHint(self, index):
        hint = super().tabSizeHint(index)
        if not self.tabIcon(index).isNull() \
                and self.tabButton(index, QTabBar.RightSide) is not None:
            hint.setWidth(hint.width() - self.RECLAIM)
        return hint


class Workspace(QWidget):

    tab_closed = Signal(QWidget)
    tab_activated = Signal(QWidget)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Optional veto for closing a tab — returns False to cancel. Used by editor tabs
        # so unsaved work can't vanish behind a stray click on the ×.
        self.close_guard = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget()

        # Empty state — the workspace starts with nothing open (§4.1: the sidebar is
        # the way in), so it should say so rather than showing a blank void.
        self._empty = QLabel(
            "Nothing open.\n\nPick a panel on the left to browse a registry or open a View.")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setStyleSheet(
            f"background: {style.BG_DEEP}; color: {style.TEXT_FAINT}; font-size: 13px;")

        self._tabs = QTabWidget()
        self._tabs.setTabBar(_TabBar())
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self._tabs.currentChanged.connect(self._on_current_changed)
        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border: 1px solid {style.BORDER}; background: {style.BG_DEEP}; }}
            QTabBar::tab {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER}; font-size: 12px;
                padding: {PAD_TOP}px {CLOSE_GAP}px {PAD_BOTTOM}px 10px;
            }}
            QTabBar::tab:selected {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                border-bottom: 1px solid {style.BG_DEEP};
            }}
            /* Hover, on unselected tabs only. The selected tab is already the brightest
               thing on the bar and lighting it further would say "you are about to switch
               to this" about the tab you are looking at. `!selected` is what keeps the
               feedback meaning one thing. */
            QTabBar::tab:hover:!selected {{
                background: {style.BG_HOVER}; color: {style.TEXT};
            }}
        """)

        # Middle-click anywhere on a tab closes it. Qt has no setting for this, so the tab
        # bar's mouse events are watched directly.
        self._middle_press = -1
        self._tabs.tabBar().installEventFilter(self)

        self._stack.addWidget(self._empty)
        self._stack.addWidget(self._tabs)
        root.addWidget(self._stack)
        self._sync()

    def eventFilter(self, watched, event):
        """Middle-click-to-close, on the whole tab rather than the ✕.

        Closing on *release over the tab that was pressed* rather than on press, which is
        how every other button in the app behaves: a middle-click that lands somewhere
        unintended can be dragged off and let go harmlessly. Pressing straight to close
        would make a misplaced click unrecoverable, and closing a tab is exactly the kind
        of thing you want to be able to change your mind about mid-click.

        It goes through `_close_tab`, so `close_guard` still gets to veto — a middle-click
        must not be a way to lose unsaved work that the ✕ would have asked about.
        """
        bar = self._tabs.tabBar()
        if watched is bar and getattr(event, "button", None) is not None \
                and event.button() == Qt.MiddleButton:
            if event.type() == QEvent.MouseButtonPress:
                self._middle_press = bar.tabAt(event.position().toPoint())
                return True
            if event.type() == QEvent.MouseButtonRelease:
                index, self._middle_press = self._middle_press, -1
                if index >= 0 and bar.tabAt(event.position().toPoint()) == index:
                    self._close_tab(index)
                return True
        return super().eventFilter(watched, event)

    def _sync(self):
        self._stack.setCurrentWidget(self._tabs if self._tabs.count() else self._empty)

    def add_tab(self, widget, title, *, icon=None, select=True) -> QWidget:
        index = self._tabs.addTab(widget, title)
        if icon is not None:
            self._tabs.setTabIcon(index, icon)
        self._install_close_button(index, widget)
        if select:
            self._tabs.setCurrentWidget(widget)
        self._sync()
        return widget

    def _install_close_button(self, index, widget):
        """Replace Qt's stock close button with a Phosphor ✕.

        The default is drawn by the platform style — on Windows a small red-tinted square
        that looks nothing like the rest of the chrome. Qt only lets a *stylesheet* replace
        it with an image URL, which would mean shipping a PNG; a per-tab widget avoids that
        and colours like everything else.

        It closes by **widget**, never by the index captured here: tabs are movable, so an
        index goes stale the moment one is dragged, and the stale one would close somebody
        else's tab.

        It is wrapped in a spacer so the ✕ isn't flush against the tab's edge. Qt offers no
        way to ask for a margin on a tab button — it pins one to the edge — so the margin
        has to be part of the widget handed over.
        """
        button = QPushButton()
        icons.mark(button, "close", size=10)
        button.setFixedSize(CLOSE_BUTTON, CLOSE_BUTTON)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip("Close  (or middle-click the tab)")
        button.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {style.TEXT_FAINT};
                border: none; border-radius: {CLOSE_BUTTON // 2}px; padding: 0;
            }}
            QPushButton:hover {{ background: {style.BG_CHROME}; color: {style.TEXT}; }}
        """)
        button.clicked.connect(lambda: self.close_widget(widget))

        holder = QWidget()
        holder.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, CLOSE_MARGIN, 0)
        lay.setSpacing(0)
        lay.addWidget(button)
        self._tabs.tabBar().setTabButton(index, QTabBar.RightSide, holder)

    def close_widget(self, widget):
        index = self._tabs.indexOf(widget)
        if index >= 0:
            self._close_tab(index)

    def _on_current_changed(self, index):
        widget = self._tabs.widget(index)
        if widget is not None:
            self.tab_activated.emit(widget)

    def _close_tab(self, index):
        widget = self._tabs.widget(index)
        if self.close_guard is not None and not self.close_guard(widget):
            return
        self._tabs.removeTab(index)
        self._sync()
        self._resync_hover()
        self.tab_closed.emit(widget)
        widget.deleteLater()

    def _resync_hover(self):
        """Re-decide which tab the pointer is on, after the tabs moved underneath it.

        Qt caches the hovered tab as a **rectangle** and only recomputes when the pointer
        leaves it. Closing a tab slides the others left under a pointer that hasn't moved,
        so the cached rect still contains it and the tab now under the cursor stays unlit —
        and because the pointer is still inside that stale rect, moving it doesn't fix
        things either. Only leaving the tab entirely and coming back does, which is a
        strange thing to have to teach someone.

        The cure is what the user would otherwise do by hand: step the pointer off the bar
        so the cache clears, then put it back where it actually is.
        """
        bar = self._tabs.tabBar()
        if not bar.count():
            return
        here = QPointF(bar.mapFromGlobal(QCursor.pos()))
        away = QPointF(-1000, -1000)
        app = QApplication.instance()
        if app is None:
            return
        app.sendEvent(bar, QHoverEvent(QEvent.HoverMove, away, away, here))
        if bar.rect().contains(here.toPoint()):
            app.sendEvent(bar, QHoverEvent(QEvent.HoverMove, here, here, away))

    def current_widget(self):
        return self._tabs.currentWidget() if self._tabs.count() else None

    def widgets(self):
        return [self._tabs.widget(i) for i in range(self._tabs.count())]

    def focus_widget(self, widget) -> bool:
        """Bring an already-open tab forward. True if it was found."""
        idx = self._tabs.indexOf(widget)
        if idx < 0:
            return False
        self._tabs.setCurrentIndex(idx)
        return True

    def set_tab_icon(self, widget, icon):
        idx = self._tabs.indexOf(widget)
        if idx >= 0:
            self._tabs.setTabIcon(idx, icon)

    def set_tab_title(self, widget, title):
        idx = self._tabs.indexOf(widget)
        if idx < 0 or self._tabs.tabText(idx) == title:
            return
        self._tabs.setTabText(idx, title)
        # A retitled tab changes width, which slides every tab after it — the same way a
        # closed one does, and with the same stale hover. It happens on rename and on the
        # unsaved dot appearing, so it is not rare.
        self._resync_hover()

    def tab_title(self, widget) -> str:
        idx = self._tabs.indexOf(widget)
        return self._tabs.tabText(idx) if idx >= 0 else ""
