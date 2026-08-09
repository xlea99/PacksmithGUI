"""Sidebar panel scaffolding (design 4.1).

Every sidebar panel is just a QWidget in a stack, so adding one is trivial — which is
exactly what §4.1 promises. Panels that aren't built yet use :class:`StubPanel`, which
states plainly that it isn't built and describes what will live there. That honesty is
deliberate: a skeleton that looks finished lies to you about progress.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

from packsmith.gui.shell import style


class PanelHeader(QLabel):
    """The small caps title strip at the top of a sidebar panel."""

    def __init__(self, text, parent=None):
        super().__init__(text.upper(), parent)
        self.setStyleSheet(
            f"color: {style.TEXT_MUTED}; background: {style.BG_PANEL};"
            f" font-size: 11px; font-weight: bold; letter-spacing: 1px;"
            f" padding: 6px 8px; border-bottom: 1px solid {style.BORDER};"
        )


class Panel(QWidget):
    """Base for sidebar panels: a titled column."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {style.BG_PANEL};")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addWidget(PanelHeader(title))

    def body(self) -> QVBoxLayout:
        return self._layout


class StubPanel(Panel):
    """A panel that isn't built yet. Says so, and says what it will become."""

    def __init__(self, title, description, parent=None):
        super().__init__(title, parent)

        not_built = QLabel("not built yet")
        not_built.setAlignment(Qt.AlignCenter)
        not_built.setStyleSheet(
            f"color: {style.TEXT_FAINT}; font-size: 12px; font-style: italic; padding: 4px 10px;")

        blurb = QLabel(description)
        blurb.setWordWrap(True)
        blurb.setAlignment(Qt.AlignTop)
        blurb.setStyleSheet(
            f"color: {style.TEXT_FAINT}; font-size: 11px; padding: 0 12px; line-height: 140%;")

        self._layout.addStretch(1)
        self._layout.addWidget(not_built)
        self._layout.addWidget(blurb)
        self._layout.addStretch(2)
