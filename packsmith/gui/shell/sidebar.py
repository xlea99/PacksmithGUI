"""The left sidebar (design 4.1): a vertical icon strip that swaps panel content.

Two widgets, deliberately separate so the window can place them correctly: the **strip**
is fixed furniture pinned to the window edge (always visible), while the **panel stack**
lives inside the splitter so it can be resized and collapsed. That's the IntelliJ/VS Code
arrangement §4.1 describes.

Clicking a different icon switches panels; clicking the active icon collapses the panel
away, leaving just the strip.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QStackedWidget, QFrame)

from packsmith.gui.shell import icons, style
from packsmith.gui.shell.panels import PANEL_SPECS

# Colour only — deliberately no font-size or font-weight. A stylesheet's font rules beat
# `setFont`, so specifying them here would squash the icon font back to a 13px bold text
# face and undo the whole point. Size is set per button instead.
_STRIP_BUTTON_QSS = f"""
    QPushButton {{
        background: transparent; color: {style.TEXT_MUTED};
        border: none; border-left: 2px solid transparent;
    }}
    QPushButton:hover {{ color: {style.TEXT}; background: {style.BG_CHROME}; }}
    QPushButton:checked {{
        color: {style.TEXT}; background: {style.BG_DEEP};
        border-left: 2px solid {style.ACCENT_EDGE};
    }}
"""


class Sidebar(QWidget):
    """The icon strip. Owns selection state; drives a :class:`PanelStack`."""

    panel_selected = Signal(str)   # panel key
    collapsed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(style.SIDEBAR_STRIP_WIDTH)
        self.setStyleSheet(
            f"background: {style.BG_PANEL}; border-right: 1px solid {style.BORDER};")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(2)

        self._buttons = {}
        self._current = None
        self._expanded = False

        icon_font = icons.icon_font()
        group = None
        for spec in PANEL_SPECS:
            if group is not None and spec.group != group:
                lay.addWidget(self._separator())
            group = spec.group
            # The letter is the fallback, not the plan: if the vendored font failed to load
            # the strip still works, just less legibly. Refusing to start over an icon font
            # would be a wild over-reaction.
            mark = icons.glyph(spec.key) if icon_font else ""
            btn = QPushButton(mark or spec.letter)
            if mark:
                btn.setFont(icon_font)
            else:
                fallback = btn.font()
                fallback.setPointSize(10)
                fallback.setBold(True)
                btn.setFont(fallback)
            btn.setCheckable(True)
            btn.setFixedSize(style.SIDEBAR_STRIP_WIDTH, 34)
            btn.setToolTip(spec.title)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(_STRIP_BUTTON_QSS)
            btn.clicked.connect(lambda _=False, k=spec.key: self._on_click(k))
            self._buttons[spec.key] = btn
            lay.addWidget(btn)

        lay.addStretch()

    @staticmethod
    def _separator() -> QWidget:
        """The rule between panel groups.

        Inset rather than full-bleed: a line touching both edges reads as the end of the
        strip, which is the opposite of what it means here.
        """
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFixedHeight(9)
        line.setStyleSheet(f"color: {style.DIVIDER}; margin: 4px 9px;")
        return line

    def _on_click(self, key):
        if self._current == key and self._expanded:
            self.collapse()
        else:
            self.select(key)

    def select(self, key):
        self._current = key
        self._expanded = True
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
        self.panel_selected.emit(key)

    def collapse(self):
        self._expanded = False
        for btn in self._buttons.values():
            btn.setChecked(False)
        self.collapsed.emit()


class PanelStack(QStackedWidget):
    """The swappable panel content beside the strip. Lives inside the splitter."""

    def __init__(self, panels: dict, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {style.BG_PANEL};")
        self._panels = {}
        for key, widget in panels.items():
            self._panels[key] = widget
            self.addWidget(widget)

    def show_panel(self, key):
        widget = self._panels.get(key)
        if widget is not None:
            self.setCurrentWidget(widget)
            self.show()
