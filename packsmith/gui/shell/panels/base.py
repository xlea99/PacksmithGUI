"""Sidebar panel scaffolding (design 4.1).

Every sidebar panel is just a QWidget in a stack, so adding one is trivial — which is
exactly what §4.1 promises. Panels that aren't built yet use :class:`StubPanel`, which
states plainly that it isn't built and describes what will live there. That honesty is
deliberate: a skeleton that looks finished lies to you about progress.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QLineEdit, QTreeWidgetItem

from packsmith.gui.shell import style


def note_row(text: str, columns: int = 2) -> QTreeWidgetItem:
    """A row that explains why a tree is empty rather than leaving you to guess.

    Enabled but **not selectable**: it is a sentence, not a thing you can act on, and a
    message you can highlight invites a double-click that does nothing.
    """
    item = QTreeWidgetItem([text] + [""] * (columns - 1))
    item.setForeground(0, style.qt_colour(style.TEXT_FAINT))
    item.setFlags(Qt.ItemIsEnabled)
    return item


class SearchBox(QLineEdit):
    """The one-line filter a panel grows once its list stops fitting on screen.

    Shared rather than copied because it is the same gesture in every panel, and a filter
    box that looks or behaves subtly differently from panel to panel is worse than no
    filter at all — the sidebar is one surface you switch between, not five.

    Deliberately **not** the query bar (§3.2.3). That one parses a language, reports errors
    and can be folded into a View; this is a substring, and pretending otherwise would put
    two things that look alike and behave differently in the same app.
    """

    def __init__(self, noun: str, parent=None):
        super().__init__(parent)
        self.setPlaceholderText(f"Search {noun}…")
        self.setStyleSheet(f"""
            QLineEdit {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                border: 1px solid {style.BORDER}; margin: 0 6px 4px 6px;
                padding: 3px 6px; font-size: 11px;
            }}
        """)

    def needle(self) -> str:
        """What to match against — trimmed and folded, or "" for no filter."""
        return self.text().strip().lower()


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
        self._header = PanelHeader(title)
        self._layout.addWidget(self._header)

    def body(self) -> QVBoxLayout:
        return self._layout

    def hide_header(self):
        """Drop the title strip, for a panel nested inside another one.

        A panel usually owns the top of the sidebar and names itself there. Nested in a
        tab, that name is already on the tab — two labels for one thing, stacked, is just
        clutter reading as a bug.
        """
        self._header.hide()


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
