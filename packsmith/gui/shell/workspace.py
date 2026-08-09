"""The main workspace (design 4.1/4.2): a single tab bar hosting heterogeneous tabs.

All tab types are peers — View tabs, editor tabs, job editors, whatever comes later —
which is what §4.2 means by "one Qt-owned tab bar." When nothing is open the workspace
shows a hint pointing at the sidebar, because the sidebar is how you open things.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QStackedWidget, QTabWidget, QLabel

from packsmith.gui.shell import style


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
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self._tabs.currentChanged.connect(self._on_current_changed)
        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border: 1px solid {style.BORDER}; background: {style.BG_DEEP}; }}
            QTabBar::tab {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER}; padding: 5px 14px; font-size: 12px;
            }}
            QTabBar::tab:selected {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                border-bottom: 1px solid {style.BG_DEEP};
            }}
        """)

        self._stack.addWidget(self._empty)
        self._stack.addWidget(self._tabs)
        root.addWidget(self._stack)
        self._sync()

    def _sync(self):
        self._stack.setCurrentWidget(self._tabs if self._tabs.count() else self._empty)

    def add_tab(self, widget, title, *, select=True) -> QWidget:
        self._tabs.addTab(widget, title)
        if select:
            self._tabs.setCurrentWidget(widget)
        self._sync()
        return widget

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
        self.tab_closed.emit(widget)
        widget.deleteLater()

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

    def set_tab_title(self, widget, title):
        idx = self._tabs.indexOf(widget)
        if idx >= 0:
            self._tabs.setTabText(idx, title)
