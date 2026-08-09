"""The bottom panel (design 4.1): output and status, collapsed by default.

Two structural points from §4.1 that this encodes:

* The panel is **collapsible and closed by default** — it should be scannable without
  stealing focus from the workspace.
* The **status bar lives at the very bottom of this panel**, not as a window-level strip,
  and stays visible even when the tab content is collapsed. So the strip doubles as the
  panel's handle: its right-hand buttons open the panel to a given tab.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTabWidget, QPushButton, QPlainTextEdit,
)

from packsmith.gui.shell import style

# §4.1's bottom tabs, with the blurbs that describe what each becomes.
BOTTOM_TABS = [
    ("logs", "Logs", None),
    ("job_results", "Job Results",
     "Per-run summary — files modified, skipped, and why. Expandable to per-file diffs; "
     "where the file versioning system surfaces."),
    ("errors", "Errors",
     "Orphaned tags, broken blueprint bindings, ownership conflicts — clickable to jump "
     "to the relevant view, file, or tag."),
    ("packdump", "Packdump",
     "Packdump management and the blessing workflow: diff summary for a newly imported "
     "dump, tags at risk, and the action to bless it as active."),
]

_TOGGLE_QSS = f"""
    QPushButton {{
        background: transparent; color: {style.TEXT_MUTED};
        border: none; padding: 1px 8px; font-size: 11px;
    }}
    QPushButton:hover {{ color: {style.TEXT}; background: {style.BG_CHROME}; }}
    QPushButton:checked {{ color: {style.TEXT}; background: {style.ACCENT}; }}
"""


def _stub_body(text) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(20, 12, 20, 12)
    not_built = QLabel("not built yet")
    not_built.setAlignment(Qt.AlignCenter)
    not_built.setStyleSheet(f"color: {style.TEXT_FAINT}; font-style: italic; font-size: 12px;")
    blurb = QLabel(text)
    blurb.setWordWrap(True)
    blurb.setAlignment(Qt.AlignCenter)
    blurb.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
    lay.addStretch()
    lay.addWidget(not_built)
    lay.addWidget(blurb)
    lay.addStretch()
    return w


class BottomPanel(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {style.BG_PANEL};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- tab content (the collapsible half) ---
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border-top: 1px solid {style.BORDER}; }}
            QTabBar::tab {{
                background: {style.BG_PANEL}; color: {style.TEXT_MUTED};
                padding: 4px 12px; font-size: 11px; border: none;
            }}
            QTabBar::tab:selected {{ color: {style.TEXT}; background: {style.BG_DEEP}; }}
        """)

        # Logs is a real (if empty) log viewer — an empty log is an honest empty log.
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setStyleSheet(
            f"background: {style.BG_DEEP}; color: {style.TEXT_MUTED};"
            f" border: none; font-family: Consolas, monospace; font-size: 11px;")

        self._tab_index = {}
        self._panels = {}
        for key, title, blurb in BOTTOM_TABS:
            body = self._log_view if key == "logs" else _stub_body(blurb)
            self._tab_index[key] = self._tabs.addTab(body, title)
            self._panels[key] = body
        root.addWidget(self._tabs)

        # --- status strip (always visible, doubles as the panel handle) ---
        strip = QWidget()
        strip.setFixedHeight(22)
        strip.setStyleSheet(
            f"background: {style.BG_PANEL}; border-top: 1px solid {style.BORDER};")
        strip_lay = QHBoxLayout(strip)
        strip_lay.setContentsMargins(8, 0, 0, 0)
        strip_lay.setSpacing(0)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        strip_lay.addWidget(self._status)
        strip_lay.addStretch()

        self._toggles = {}
        for i, (key, title, _blurb) in enumerate(BOTTOM_TABS):
            btn = QPushButton(title)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(22)
            btn.setStyleSheet(_TOGGLE_QSS)
            btn.clicked.connect(lambda _=False, idx=i: self._on_toggle(idx))
            self._toggles[key] = btn
            strip_lay.addWidget(btn)

        root.addWidget(strip)

        self._expanded = True
        self.collapse()   # §4.1: closed by default

    # --- collapse / expand -------------------------------------------------

    def _on_toggle(self, index):
        if self._expanded and self._tabs.currentIndex() == index:
            self.collapse()
        else:
            self.expand(index)

    def expand(self, index=0):
        self._tabs.setCurrentIndex(index)
        self._tabs.show()
        self._expanded = True
        self._sync_toggles()

    def collapse(self):
        self._tabs.hide()
        self._expanded = False
        self._sync_toggles()

    def toggle(self):
        self.collapse() if self._expanded else self.expand(self._tabs.currentIndex())

    def _sync_toggles(self):
        current = self._tabs.currentIndex()
        for i, (key, _t, _b) in enumerate(BOTTOM_TABS):
            self._toggles[key].setChecked(self._expanded and i == current)

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    def collapsed_height(self) -> int:
        return 22

    # --- content -----------------------------------------------------------

    def set_status(self, text):
        self._status.setText(text)

    def log(self, text):
        self._log_view.appendPlainText(text)

    def set_panel(self, key, widget):
        """Swap real content into one of the bottom tabs, replacing its stub."""
        idx = self._tab_index.get(key)
        if idx is None:
            return
        title = self._tabs.tabText(idx)
        self._tabs.removeTab(idx)
        self._tabs.insertTab(idx, widget, title)
        self._panels[key] = widget

    def panel(self, key):
        return self._panels.get(key)

    def refresh_panels(self):
        """Ask every bottom panel that can refresh itself to do so (after an action run,
        a packdump change, etc.)."""
        for widget in self._panels.values():
            refresh = getattr(widget, "refresh", None)
            if callable(refresh):
                refresh()
