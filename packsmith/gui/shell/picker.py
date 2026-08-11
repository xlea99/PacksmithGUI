"""A floating, bounded, filterable list you pick one thing from.

A ``QMenu`` is the obvious thing to reach for and the wrong one: it grows to fit its
contents, so a few thousand registry ids produce a menu taller than the screen and wider
than the window, with no way to scroll and long ids running off the edge.

This is the same gesture — pops at the cursor, closes on Escape or an outside click, Enter
picks — but **bounded and scrollable**, with a filter box because a list you can't narrow
isn't much better than a list you can't scroll. Long ids elide in the middle
(``v_slab_compat:create/polished_cut_tuff_vertical_slab`` keeps both its ends, which are
the informative parts) and carry the full text as a tooltip.

Deliberately generic: it takes strings and emits the one you chose. Blueprint candidate
suggestions are the first caller, and any cell that wants "pick from a long list of ids"
is the next.
"""
from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication, QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout,
)

from packsmith.core.query.tokens import matches_tokens
from packsmith.gui.shell import style


def _token_match(candidate: str, text: str) -> bool:
    """The same rule the filter bar uses: words in any order, plurals ignored, and the
    word still under the cursor treated as a prefix so the list narrows as you type."""
    words = text.split()
    if not words:
        return True
    if not text[-1].isspace() and not words[-1].endswith("*"):
        words[-1] += "*"
    return matches_tokens(candidate, words)


# Only a windowful ever enters the widget; the filter still searches everything.
_MAX_ROWS = 400
_MAX_WIDTH = 460
_MAX_HEIGHT = 380
# Below this a filter box is just clutter — you can see the whole list.
_FILTER_THRESHOLD = 12


class PickerPopup(QFrame):
    """Choose one string from a bounded, scrollable, filterable list."""

    chosen = Signal(str)
    # Emitted synchronously by `dismiss()`. `destroyed` would do as well, except it
    # waits for deferred deletion — so an owner clearing its reference on that alone
    # keeps pointing at a retired picker for an unpredictable number of event turns.
    dismissed = Signal()

    def __init__(self, items, *, header="", placeholder="type to narrow", parent=None,
                 match=None, attach=None, dock=None):
        # Two different windows, decided here rather than flipped later: changing the flags
        # afterwards recreates the native window, and this frame should never spend even one
        # moment being an ordinary decorated window.
        #
        # Standalone, Qt.Popup is right: it grabs the mouse, so an outside click dismisses
        # it. **Attached, that grab is the enemy.** The popup eats the first click of the
        # double-click meant for another cell, so opening the next cell takes three clicks
        # instead of two (and Qt's mouse-replay makes it inconsistently two, which is
        # worse). Qt.ToolTip is the same frameless, non-activating window WITHOUT the grab,
        # so clicks reach the table exactly as if nothing were open.
        # WindowDoesNotAcceptFocus asks the window manager never to activate this window
        # (WS_EX_NOACTIVATE on Windows). Without it, clicking the scrollbar activates the
        # popup, the cell editor gets a FocusOut, and the view helpfully ends the edit —
        # so scrolling the suggestions would close them.
        #
        # `dock` is the third case and the least window-like: an ordinary child widget
        # inside the view's viewport. A top-level popup is positioned in *screen*
        # coordinates, so it stays put when the main window moves — the suggestions
        # detach from the cell they belong to and hang in space over the desktop. A child
        # moves with its parent by construction, which is the only way to be genuinely
        # stuck to a cell rather than repeatedly chasing it. The cost is that it clips to
        # the viewport, so it must be sized to fit inside one (see `dock_under`).
        if dock is not None:
            super().__init__(dock)
            # A frameless child of a scroll area inherits nothing to paint on, so without
            # this the list renders over whatever cells are behind it.
            self.setAutoFillBackground(True)
        else:
            super().__init__(parent, (Qt.ToolTip | Qt.WindowDoesNotAcceptFocus)
                             if attach is not None else Qt.Popup)
        self._docked = dock is not None
        self._dismissed = False    # set by dismiss(); nothing may re-show it after
        # Token matching by default, because that is what typing into a box means
        # everywhere else in this app — a plain substring filter would fail on
        # "polished granite stair" (ids use underscores), and failing on a space is the
        # first thing anyone would type.
        self._match = match or _token_match
        self._attached = None      # a text box driving this list, in attached mode
        self._done = False         # itemActivated and itemClicked can both fire on one click
        self.setFrameShape(QFrame.StyledPanel)
        self.setMaximumSize(_MAX_WIDTH, _MAX_HEIGHT)
        self.setStyleSheet(f"""
            QFrame {{ background: {style.BG_PANEL}; border: 1px solid {style.BORDER}; }}
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        if header:
            label = QLabel(header, self)
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px; border: none;")
            lay.addWidget(label)

        # Parented at construction, every one of them. A parentless widget is a top-level
        # *window*, and setVisible(True) on one is exactly what show() does — so hiding or
        # showing a child before it reaches the layout flashes a real, natively decorated
        # window on screen for a frame.
        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText(placeholder)
        self._filter.setStyleSheet(f"""
            QLineEdit {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                border: 1px solid {style.BORDER}; padding: 2px 5px; font-size: 11px;
            }}
        """)
        self._filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self._filter)
        # Below the threshold you can see the whole list, so the box is just clutter.
        # Ordered after addWidget deliberately: see the note above.
        self._filter.setVisible(len(items) >= _FILTER_THRESHOLD)

        self._list = QListWidget(self)
        self._list.setTextElideMode(Qt.ElideMiddle)
        self._list.setUniformItemSizes(True)   # skip per-row layout on a long list
        self._list.setStyleSheet(style.LIST_QSS)
        self._list.itemActivated.connect(self._pick)
        self._list.itemClicked.connect(self._pick)
        lay.addWidget(self._list)

        self._footer = QLabel(self)
        self._footer.setStyleSheet(
            f"color: {style.TEXT_FAINT}; font-size: 10px; border: none;")
        lay.addWidget(self._footer)

        # The full list stays in Python; only a windowful goes into the widget. Fourteen
        # thousand QListWidgetItems is a lot of machinery to build for the fourteen rows
        # anyone can actually see, and the filter searches all of them regardless.
        self._items = [str(t) for t in items]
        self._repopulate("")

        self.resize(min(_MAX_WIDTH, 420),
                    min(_MAX_HEIGHT, 70 + 20 * min(len(self._items), 14)))
        if attach is not None:
            self.attach_to(attach)

    # --- attached mode -----------------------------------------------------

    def attach_to(self, editor):
        """Drive the list from someone else's text box instead of the built-in filter.

        This turns the picker into a completion popup: the thing you type into is the cell
        editor, and this just follows along underneath it.

        **It must not take focus**, or the table view sees the cell editor lose focus and
        closes the edit out from under you. And because a Qt.ToolTip window has no keyboard
        grab, arrow keys go to the editor by default — where they would move a text cursor
        instead of the selection — so navigation is intercepted with an event filter on the
        editor. That is precisely how ``QCompleter`` steers its own popup.
        """
        self._attached = editor
        self._filter.hide()
        self.setFocusPolicy(Qt.NoFocus)
        self._list.setFocusPolicy(Qt.NoFocus)
        # The scrollbars too — they are separate widgets, and the scrollbar is the one part
        # of this popup you are most likely to click without meaning to choose anything.
        for bar in (self._list.verticalScrollBar(), self._list.horizontalScrollBar()):
            bar.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # Installed a turn late, and that is load-bearing. Event filters run in reverse
        # order of installation, and the item view installs the delegate's filter *after*
        # createEditor returns — so filtering from here would put us second, and
        # QStyledItemDelegate would cancel the whole edit on Escape before we ever saw it.
        QTimer.singleShot(0, self._filter_keys)
        editor.textChanged.connect(self._repopulate)
        editor.destroyed.connect(lambda *_: self.dismiss())
        self._repopulate(editor.text())

    def _has_the_pointer(self) -> bool:
        """Is this popup what the user is currently interacting with?

        Both halves change meaning when docked, and getting either wrong swallows every
        FocusOut — which means the cell editor never closes and the grid becomes unusable:

        - `isActiveWindow()` asks about the *top-level* window. For a floating popup that
          is the popup itself, so it answers the intended question. For a docked child it
          is the main window, so it is true whenever the app has focus at all, and this
          would always return True.
        - `geometry()` is in screen coordinates only for a top-level window; for a child
          it is relative to the viewport. Mapping the cursor into local coordinates is
          right either way.
        """
        if not self._docked and self.isActiveWindow():
            return True
        return self.rect().contains(self.mapFromGlobal(QCursor.pos()))

    def _refocus(self):
        try:
            self._attached.setFocus(Qt.OtherFocusReason)
        except (AttributeError, RuntimeError):
            pass        # the edit ended anyway

    def _filter_keys(self):
        try:
            self._attached.installEventFilter(self)
        except (AttributeError, RuntimeError):
            pass        # the edit ended before we got here

    def eventFilter(self, obj, event):
        """Steer the list from the editor's keyboard, without stealing the rest of it."""
        if obj is not self._attached or not self.isVisible():
            return False
        if event.type() == QEvent.FocusOut and self._has_the_pointer():
            # Belt and braces for the scrollbar, in case a platform activates us anyway:
            # interacting with our own popup is not "you have finished editing". Swallow
            # the notification so the view can't close the editor, and take focus back.
            QTimer.singleShot(0, self._refocus)
            return True
        if event.type() != QEvent.KeyPress:
            return False
        key = event.key()
        if key in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_PageUp):
            self._move(key)
            return True
        if key in (Qt.Key_Return, Qt.Key_Enter):
            item = self._list.currentItem()
            if item is not None:
                self._pick(item)
                return True
            return False        # nothing highlighted: Enter commits what was typed
        if key == Qt.Key_Escape:
            self.close()         # dismisses the suggestions, not the edit behind them
            return True
        return False

    # --- behaviour ---------------------------------------------------------

    def _apply_filter(self, text):
        self._repopulate(text)

    def _repopulate(self, text):
        matched = [t for t in self._items
                   if not text.strip() or self._match(t, text)]
        self._list.clear()
        for entry in matched[:_MAX_ROWS]:
            item = QListWidgetItem(entry)
            item.setToolTip(entry)          # elided in the row, whole in the tooltip
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)

        if not matched:
            self._footer.setText("nothing matches")
        elif len(matched) > _MAX_ROWS:
            self._footer.setText(
                f"showing {_MAX_ROWS} of {len(matched):,} — keep typing")
        else:
            self._footer.setText(f"{len(matched):,}")

    def _pick(self, item):
        if item is None or item.isHidden() or self._done:
            return
        self._done = True
        # Closed before the signal: a listener that writes the chosen value back into the
        # text box driving this list would otherwise re-filter a popup that is on its way
        # out, and in attached mode that text box is exactly where the value goes.
        self.close()
        self.chosen.emit(item.text())

    def keyPressEvent(self, event):
        """Type in the filter, steer in the list — the list never takes focus, so its keys
        have to be forwarded or the arrows would move a cursor instead of a selection."""
        key = event.key()
        if key in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_PageUp):
            self._move(key)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            item = self._list.currentItem()
            if item is not None:
                self._pick(item)
                return
            # Nothing highlighted — fall through, so Enter still commits what was typed.
        if key == Qt.Key_Escape:
            # Dismisses the suggestions, not the edit behind them.
            self.close()
            return
        if self._attached is not None:
            QApplication.sendEvent(self._attached, event)
            return
        super().keyPressEvent(event)

    def _move(self, key):
        step = {Qt.Key_Down: 1, Qt.Key_Up: -1,
                Qt.Key_PageDown: 10, Qt.Key_PageUp: -10}[key]
        count = self._list.count()
        if not count:
            return
        self._list.setCurrentRow(max(0, min(count - 1, self._list.currentRow() + step)))
        self._list.scrollToItem(self._list.currentItem())

    def popup_at(self, global_pos):
        """Show at a point, clamped so the whole popup stays on screen.

        Moving to the raw cursor position puts a 420x340 window off the edge whenever you
        click near the bottom or right of the screen — which reads as "the button did
        nothing", because most of it genuinely isn't visible.
        """
        self.adjustSize()
        target = QPoint(global_pos)
        area = self._screen_area(global_pos)
        if area is not None:
            target.setX(min(max(target.x(), area.left()),
                            area.right() - self.width()))
            target.setY(min(max(target.y(), area.top()),
                            area.bottom() - self.height()))
        self._show_at(target)

    def popup_under(self, rect_global: QRect):
        """Open hanging off a rectangle — a table cell — flipping above it if there's no
        room below.

        Clamping alone is wrong for an anchored popup: pushing it back up on a bottom-row
        cell parks it *over* the cell you're editing, hiding the text you're typing.
        """
        self.adjustSize()
        self.resize(max(self.width(), min(_MAX_WIDTH, rect_global.width())),
                    self.height())
        x, y = rect_global.left(), rect_global.bottom() + 1
        area = self._screen_area(rect_global.center())
        if area is not None:
            if y + self.height() > area.bottom():
                above = rect_global.top() - self.height()
                y = above if above >= area.top() else area.bottom() - self.height()
            x = min(max(x, area.left()), area.right() - self.width())
        self._show_at(QPoint(x, y))

    # How much clear space to leave between the popup and the viewport edge. Small enough
    # to waste nothing, big enough that the popup never looks welded to the bottom.
    _EDGE_MARGIN = 10

    def dismiss(self):
        """Close this picker **for good**, rather than merely hiding it.

        `close()` is enough for a top-level window: it is hidden, nothing else references
        it, and Python collects it. A docked picker is a child widget, so closing leaves it
        parked in the viewport's child list, alive and still wired to whatever was driving
        it — and anything that later asks it to reposition will call `show()` and bring a
        picker for an edit that finished minutes ago back onto the screen. Dragging across
        a row and letting the grid autoscroll resurrected one per cell visited.

        The flag matters as much as the delete: `deleteLater` runs at the next event loop
        pass, and the scroll handlers fire before that.
        """
        self._dismissed = True
        self.dismissed.emit()
        self.hide()
        self.close()
        self.deleteLater()

    def dock_under(self, rect: QRect):
        """Position under a cell **inside the viewport**, sized to fit within it.

        The docked counterpart of `popup_under`, and the differences are all consequences
        of being a child widget rather than a window:

        - Coordinates are the viewport's, which is exactly what `QAbstractItemView.
          visualRect` already returns — no mapping to global and back.
        - It cannot overhang, so instead of flipping to whichever side has *any* room, it
          takes whichever side has *more* and then caps its height to what actually fits.
          Below is preferred on a tie, because a list that grows downward from what you
          are typing is the shape everyone expects.
        - The height cap keeps the internal scrollbar doing the work. Nothing is dropped
          from the list — only the number of rows visible at once changes.
        """
        viewport = self.parentWidget()
        if viewport is None or self._dismissed:
            return
        self.adjustSize()
        available = viewport.height()
        below = available - rect.bottom() - 1 - self._EDGE_MARGIN
        above = rect.top() - self._EDGE_MARGIN

        if below >= above:
            height, top = min(self.height(), below), rect.bottom() + 1
        else:
            height = min(self.height(), above)
            top = rect.top() - height

        width = max(min(_MAX_WIDTH, max(rect.width(), self.width())),
                    0)
        width = min(width, max(viewport.width() - 2 * self._EDGE_MARGIN, 1))
        left = min(max(rect.left(), 0), max(viewport.width() - width, 0))

        self.setGeometry(left, max(top, 0), width, max(height, 1))
        self.show()
        self.raise_()          # above the cell editor, which is a sibling in the viewport

    @staticmethod
    def _screen_area(point):
        screen = QApplication.screenAt(point) or QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    def _show_at(self, target: QPoint):
        if self._dismissed:
            return
        self.move(target)
        self.show()
        self.raise_()
        # In attached mode focus stays where the typing is; taking it would close the edit.
        if self._attached is None:
            self._filter.setFocus()

    # --- for tests ---------------------------------------------------------

    def visible_items(self) -> list:
        return [self._list.item(r).text() for r in range(self._list.count())]

    def match_count(self, text="") -> int:
        """How many of the FULL list match — the widget only ever holds a windowful."""
        return sum(1 for t in self._items if not text.strip() or self._match(t, text))
