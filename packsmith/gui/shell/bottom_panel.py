"""The bottom panel (design 4.1): output and status, collapsed by default.

Three rows, top to bottom, modelled on IntelliJ's tool-window strip:

    ┌───────────────────────────────┐
    │  content        (only when expanded)
    ├───────────────────────────────┤
    │  Logs  Job Results  Errors …  │  the tab strip — always visible
    ├───────────────────────────────┤
    │  1,983 items in minecraft:item│  the true status bar — always visible
    └───────────────────────────────┘

**The strip is the tabs and the handle at once**, which is the point of the rewrite. The
panel used to carry two separate rows of the same four names: a `QTabWidget`'s own tab bar
*and* a set of buttons in the status strip that merely jumped to it. Two controls for one
choice, and the status bar's right-hand side spent on a duplicate.

So the tab bar is gone and the buttons became the tabs:

* click a tab while collapsed — open the panel on it
* click the **active** tab — collapse the panel
* click a **different** tab — switch to it, staying open

Only the content collapses. The strip never moves, so the panel opens and closes without
anything below it shifting under the cursor — and the status bar is left to be purely
status, with its right-hand half now free for whatever earns it.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QStackedWidget,
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

STRIP_HEIGHT = 24
STATUS_HEIGHT = 22
# Qt's QWIDGETSIZE_MAX — "no maximum". Not exported by PySide6, hence the literal.
_UNBOUNDED = 16777215
# The floor. Without one the splitter can hand the panel a couple of pixels and "expanded"
# becomes a sliver that reads as a rendering fault.
MIN_CONTENT_HEIGHT = 110
# What a panel with no remembered height opens to. Generous on purpose: the first thing
# anyone does with a log or a diff is drag it taller, and opening too small makes the panel
# feel like it is resisting.
DEFAULT_CONTENT_HEIGHT = 300

_TAB_QSS = f"""
    QPushButton {{
        background: transparent; color: {style.TEXT_MUTED};
        border: none; border-top: 2px solid transparent;
        padding: 0 10px; font-size: 11px;
    }}
    QPushButton:hover {{ color: {style.TEXT}; background: {style.BG_CHROME}; }}
    QPushButton:checked {{
        color: {style.TEXT}; background: {style.BG_DEEP};
        border-top: 2px solid {style.ACCENT_EDGE};
    }}
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

    # So the window can give the panel real room when it opens; a splitter that was told
    # to keep it at its collapsed height will otherwise honour that forever.
    expanded_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {style.BG_PANEL};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- row 1: content, the only part that collapses ---
        self._stack = QStackedWidget()
        self._stack.setMinimumHeight(MIN_CONTENT_HEIGHT)
        self._stack.setStyleSheet(f"border-top: 1px solid {style.BORDER};")

        # Logs is a real (if empty) log viewer — an empty log is an honest empty log.
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setStyleSheet(
            f"background: {style.BG_DEEP}; color: {style.TEXT_MUTED};"
            f" border: none; font-family: Consolas, monospace; font-size: 11px;")

        self._tab_index = {}
        self._panels = {}
        for key, _title, blurb in BOTTOM_TABS:
            body = self._log_view if key == "logs" else _stub_body(blurb)
            self._tab_index[key] = self._stack.addWidget(body)
            self._panels[key] = body
        # Stretch 1: the content soaks up every spare pixel. The strip and the status bar
        # are fixed-height, so without this a QVBoxLayout hands the surplus out as spacing
        # and the two rows drift into the middle of the panel instead of sitting at its
        # bottom edge.
        root.addWidget(self._stack, 1)

        # --- row 2: the tab strip, always visible ---
        strip = QWidget()
        strip.setFixedHeight(STRIP_HEIGHT)
        strip.setStyleSheet(
            f"background: {style.BG_PANEL}; border-top: 1px solid {style.BORDER};")
        strip_lay = QHBoxLayout(strip)
        strip_lay.setContentsMargins(4, 0, 0, 0)
        strip_lay.setSpacing(0)

        self._tabs = {}
        for index, (key, title, _blurb) in enumerate(BOTTOM_TABS):
            button = QPushButton(title)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedHeight(STRIP_HEIGHT)
            button.setStyleSheet(_TAB_QSS)
            button.clicked.connect(lambda _=False, i=index: self._on_tab_clicked(i))
            self._tabs[key] = button
            strip_lay.addWidget(button)
        strip_lay.addStretch()
        root.addWidget(strip)

        # --- row 3: the true status bar ---
        status_bar = QWidget()
        status_bar.setFixedHeight(STATUS_HEIGHT)
        status_bar.setStyleSheet(
            f"background: {style.BG_PANEL}; border-top: 1px solid {style.BORDER};")
        status_lay = QHBoxLayout(status_bar)
        status_lay.setContentsMargins(8, 0, 8, 0)
        status_lay.setSpacing(0)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        status_lay.addWidget(self._status)
        status_lay.addStretch()
        root.addWidget(status_bar)

        self._expanded = True
        self.collapse()   # §4.1: closed by default

    # --- collapse / expand -------------------------------------------------

    def _index_of(self, which) -> int:
        """Accepts a tab key or a positional index. Keys are preferred at call sites — a
        bare number silently means a different tab the moment BOTTOM_TABS is reordered."""
        if isinstance(which, str):
            return self._tab_index.get(which, 0)
        return int(which)

    def _on_tab_clicked(self, index):
        if self._expanded and self._stack.currentIndex() == index:
            self.collapse()
        else:
            self.expand(index)

    def expand(self, which=0):
        was = self._expanded
        self._stack.setCurrentIndex(self._index_of(which))
        self._stack.show()
        self.setMaximumHeight(_UNBOUNDED)
        self._expanded = True
        self._sync_tabs()
        if not was:
            self.expanded_changed.emit(True)

    def collapse(self):
        """Shut, and pinned shut.

        The height cap is what stops a collapsed panel being draggable at all. There is
        nothing behind the strip to reveal, so a drag could only stretch two fixed rows
        across empty space — which is exactly what it did before the cap, floating them off
        the bottom edge of the window.
        """
        was = self._expanded
        self._stack.hide()
        self.setMaximumHeight(self.collapsed_height())
        self._expanded = False
        self._sync_tabs()
        if was:
            self.expanded_changed.emit(False)

    def toggle(self):
        self.collapse() if self._expanded else self.expand(self._stack.currentIndex())

    def _sync_tabs(self):
        """Highlight the open tab, and nothing when the panel is shut — a lit tab over a
        closed panel would claim something is showing that isn't."""
        current = self._stack.currentIndex()
        for index, (key, _t, _b) in enumerate(BOTTOM_TABS):
            self._tabs[key].setChecked(self._expanded and index == current)

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    @property
    def current_key(self) -> str:
        index = self._stack.currentIndex()
        return BOTTOM_TABS[index][0] if 0 <= index < len(BOTTOM_TABS) else ""

    def collapsed_height(self) -> int:
        """Both permanent rows. The strip is part of the furniture now, not part of the
        content, so a collapsed panel is taller than it used to be by exactly one strip."""
        return STRIP_HEIGHT + STATUS_HEIGHT

    def expanded_height(self) -> int:
        return self.collapsed_height() + DEFAULT_CONTENT_HEIGHT

    def minimum_expanded_height(self) -> int:
        return self.collapsed_height() + MIN_CONTENT_HEIGHT

    # --- content -----------------------------------------------------------

    def set_status(self, text):
        self._status.setText(text)

    def log(self, text):
        self._log_view.appendPlainText(text)

    def set_panel(self, key, widget):
        """Swap real content into one of the bottom tabs, replacing its stub."""
        index = self._tab_index.get(key)
        if index is None:
            return
        old = self._stack.widget(index)
        self._stack.insertWidget(index, widget)
        if old is not None:
            self._stack.removeWidget(old)
            old.deleteLater()
        self._panels[key] = widget

    def panel(self, key):
        return self._panels.get(key)

    def show_panel(self, key):
        """Bring one bottom tab to the front and open the panel. Used when something needs
        the user's attention rather than when the user asked for it."""
        if key in self._tab_index:
            self.expand(key)

    def refresh_panels(self):
        """Ask every bottom panel that can refresh itself to do so (after an action run,
        a packdump change, etc.)."""
        for widget in self._panels.values():
            refresh = getattr(widget, "refresh", None)
            if callable(refresh):
                refresh()
