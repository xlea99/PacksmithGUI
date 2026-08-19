"""One dropdown, used everywhere a dropdown is simple.

Qt's combo box does something on this platform that nobody wants: it opens the list
*over* the box, positioned so the current selection lands under the pointer. Where the
popup appears therefore depends on what happens to be selected — pick the last item and
the list climbs up over whatever sits above the control. It is also the reason a combo
never quite feels like the dropdowns in the rest of the app.

The fix has three parts and needs all three, which is exactly why it belongs in one place
rather than being re-derived per call site:

1. **`SH_ComboBox_Popup` off**, via a proxy style. This is what decides *where* the list
   goes. Nothing else moves it.
2. **A plain `QListView`.** Popup placement drags a different item delegate along with it,
   so turning the placement off without this leaves the rows drawn in the old style.
3. **A painted caret.** Styling `::drop-down` at all replaces the whole sub-control,
   arrow included, and Qt does not honour the zero-size-border CSS triangle that would
   redraw it in a browser — it renders a flat bar. So the caret is the same Phosphor glyph
   the rest of the shell uses, drawn here.

**Simple dropdowns only.** Cell editors — the blueprint grid's slot editors, the registry
table's enum delegate — are deliberately not built on this. A delegate's editor is created
and destroyed per edit inside a view that owns its own painting, and it has its own
placement rules; giving it a second style to carry buys nothing and adds a lifetime to get
wrong.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QComboBox, QListView, QProxyStyle, QStyle

from packsmith.gui.shell import icons, style

QSS = f"""
    QComboBox {{
        background: {style.BG_DEEP}; color: {style.TEXT};
        border: 1px solid {style.BORDER}; padding: 2px 6px; font-size: 11px;
    }}
    /* A value the app SUGGESTED, which the user has not confirmed. A combo box always has
       a current item — there is no empty state — so an untouched picker looks exactly like
       a deliberate choice, and the step it belongs to reads as bound when it is not. Dimmed
       and italic says "this is what I would pick", not "this is what you picked". Cleared
       the moment anything is chosen, including choosing the same row. */
    QComboBox[unconfirmed="true"] {{
        color: {style.TEXT_FAINT}; font-style: italic;
    }}
    /* Reserves the caret's space so a long value never runs under it. The caret itself is
       painted by DropDown.paintEvent. */
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox:hover {{ border-color: {style.ACCENT_EDGE}; }}
    QComboBox:disabled {{ color: {style.TEXT_FAINT}; border-color: {style.BORDER}; }}
    QComboBox QAbstractItemView {{
        background: {style.BG_DEEP}; color: {style.TEXT};
        border: 1px solid {style.BORDER};
        outline: none;
        /* The bright accent, not the muted selection blue. This list is open for one
           moment and closes on the next click — a tint you have to look for is no use at
           all when the whole job is "which row am I about to hit". */
        selection-background-color: {style.ACCENT_EDGE};
        selection-color: #ffffff;
    }}
    QComboBox QAbstractItemView::item {{ padding: 3px 6px; }}
    QComboBox QAbstractItemView::item:selected {{
        background: {style.ACCENT_EDGE}; color: #ffffff;
    }}
    QComboBox QAbstractItemView::separator {{
        height: 1px; background: {style.DIVIDER}; margin: 4px 6px;
    }}
"""


class _DownwardStyle(QProxyStyle):
    """Turns off the placement that centres the current item on the pointer.

    **Constructed with no base style, deliberately.** ``QProxyStyle(style)`` *takes
    ownership* of what it is handed, and a widget with no style of its own returns the
    APPLICATION's style from ``.style()`` — so the obvious-looking
    ``_DownwardStyle(combo.style())`` makes the proxy a co-owner of the style the whole app
    shares. Both then delete it on the way out, and Packsmith exits with an access
    violation (0xC0000005) every single time. With no base the proxy resolves to the
    application style without owning it, which is the documented usage and the same
    behaviour. See `tests/test_shutdown.py`.
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ComboBox_Popup:
            return 0
        return super().styleHint(hint, option, widget, returnData)


class DropDown(QComboBox):
    """A combo box that opens downward, starts at the top, and paints its own caret.

    ``styled=False`` keeps the behaviour and skips the stylesheet, for the handful of
    places that carry their own (the header's run control sizes itself to the chrome
    around it).
    """

    def __init__(self, parent=None, *, styled=True):
        super().__init__(parent)
        # Held on the instance: `setStyle` does NOT take ownership, and a proxy collected
        # while the widget still points at it is a dangling pointer at teardown.
        self._proxy_style = _DownwardStyle()
        self.setStyle(self._proxy_style)
        self.setView(QListView())
        if styled:
            self.setStyleSheet(QSS)

    def paintEvent(self, event):
        super().paintEvent(event)
        glyph = icons.ui("down")
        if not glyph:
            return                      # the icon font failed to load; the box still works
        font = QFont(icons.family())
        font.setPixelSize(10)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.setFont(font)
        painter.setPen(QColor(style.TEXT if self.underMouse() else style.TEXT_MUTED))
        painter.drawText(self.rect().adjusted(0, 0, -6, 0),
                         Qt.AlignRight | Qt.AlignVCenter, glyph)
        painter.end()
