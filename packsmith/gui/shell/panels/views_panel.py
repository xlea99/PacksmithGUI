"""The Views panel (design 4.1) — the home for saved Views.

"Searchable list of saved view configurations. Double-click opens the View in its chosen
renderer."

This panel is what makes closing a tab safe: a View lives *here*, not in the tab bar, so
closing its tab only closes a window onto it. Deleting is a separate, explicit act —
right-click → Delete — which is exactly the close-≠-delete lifecycle the shell wanted.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QListWidget, QListWidgetItem, QPushButton, QHBoxLayout, QWidget, QMenu, QLineEdit,
)

from packsmith.gui.shell import style
from packsmith.gui.shell.panels.base import Panel


class ViewsPanel(Panel):

    view_activated = Signal(object)    # View
    new_view_requested = Signal()
    rename_requested = Signal(object)  # View
    delete_requested = Signal(object)  # View

    def __init__(self, views=None, parent=None):
        super().__init__("Views", parent)
        self._views = list(views or [])

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(6, 4, 6, 4)
        bar_lay.setSpacing(4)

        new_btn = QPushButton("＋  New View")
        new_btn.setFixedHeight(22)
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER}; font-size: 11px; padding: 1px 8px;
            }}
            QPushButton:hover {{ color: {style.TEXT}; border-color: {style.ACCENT_EDGE}; }}
        """)
        new_btn.clicked.connect(self.new_view_requested)
        bar_lay.addWidget(new_btn)
        bar_lay.addStretch()
        self.body().addWidget(bar)

        # §4.1 calls this a *searchable* list — a mature pack accumulates hundreds.
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search views…")
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                border: 1px solid {style.BORDER}; margin: 0 6px 4px 6px;
                padding: 3px 6px; font-size: 11px;
            }}
        """)
        self._search.textChanged.connect(self.refresh)
        self.body().addWidget(self._search)

        self._list = QListWidget()
        self._list.setStyleSheet(style.LIST_QSS)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.itemActivated.connect(self._on_activated)
        self._list.itemDoubleClicked.connect(self._on_activated)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self.body().addWidget(self._list)

        self.refresh()

    def set_views(self, views):
        self._views = list(views)
        self.refresh()

    def refresh(self, *_):
        needle = self._search.text().strip().lower()
        self._list.clear()
        for view in self._views:
            if needle and needle not in view.name.lower():
                continue
            item = QListWidgetItem(view.name)
            item.setData(Qt.UserRole, view)
            item.setToolTip(f"{view.name} — double-click to open")
            self._list.addItem(item)

    def select_view(self, view):
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.UserRole).id == view.id:
                self._list.setCurrentRow(i)
                return

    def _on_activated(self, item):
        self.view_activated.emit(item.data(Qt.UserRole))

    def _on_context_menu(self, pos):
        item = self._list.itemAt(pos)
        if item is None:
            return
        view = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.view_activated.emit(view))
        menu.addSeparator()
        menu.addAction("Rename…", lambda: self.rename_requested.emit(view))
        menu.addAction("Delete", lambda: self.delete_requested.emit(view))
        menu.exec(self._list.mapToGlobal(pos))
