"""The packdump banner — a condition, not a notification (design 3.1).

A refused packdump is not an event that happened once; it is a **state the profile is in**
until something changes. It gets re-detected every time the profile opens and every time
the window regains focus, so a dialog would fire on every alt-tab and train the user to
dismiss it without reading — which is exactly how the important one gets dismissed too.

So it's a bar that stays up while the condition holds and disappears by itself when the
condition clears. Nothing about it is timed, and nothing about it interrupts.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton

from packsmith.gui.shell import icons, style


class PackdumpBanner(QWidget):
    """Shown only when a packdump needs the user's attention. Hidden otherwise."""

    force_requested = Signal()
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 8, 5)
        lay.setSpacing(8)

        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.RichText)
        lay.addWidget(self._label, 1)

        self._force = self._button("Import anyway…")
        self._force.clicked.connect(self.force_requested)
        lay.addWidget(self._force)

        close = self._button("")
        icons.mark(close, "close", size=12)
        close.setFixedWidth(24)
        close.clicked.connect(self._dismiss)
        lay.addWidget(close)

        self.hide()

    @staticmethod
    def _button(text):
        button = QPushButton(text)
        button.setFixedHeight(20)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {style.TEXT};
                border: 1px solid {style.TEXT_MUTED}; font-size: 11px; padding: 0 8px;
            }}
            QPushButton:hover {{ border-color: {style.TEXT}; }}
        """)
        return button

    def _dismiss(self):
        self.hide()
        self.dismissed.emit()

    def show_result(self, result):
        """Render an ImportResult, or hide when it needs no attention.

        Only ``refused`` and ``unreadable`` land here. A successful import is reported in
        the status line and, when it orphaned something, by the Errors view — the point of
        this bar is conditions that *persist*, and a completed import doesn't.
        """
        self._result = result
        if result is None or result.status not in ("refused", "unreadable"):
            self.hide()
            return

        if result.status == "unreadable":
            self._paint(style.OWNER_ACTION,
                        f"<b>A packdump is present but couldn't be read.</b> "
                        f"{result.reason}")
            self._force.hide()
        else:
            problems = ", ".join(
                f"{name} {issue['actual']} ≠ {issue['expected']}"
                for name, issue in sorted(result.errors.items()))
            self._paint(style.ERROR,
                        f"<b>A newer packdump was found and refused</b> — {problems}. "
                        f"It describes a different game, so importing it would "
                        f"reinterpret every tag you own. Porting to a new version means "
                        f"a new profile.")
            self._force.show()
        self.show()

    def _paint(self, colour, text):
        self.setStyleSheet(
            f"background: {style.BG_CHROME}; border-bottom: 2px solid {colour};")
        self._label.setStyleSheet(f"color: {style.TEXT}; font-size: 12px;")
        self._label.setText(text)
