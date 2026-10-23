"""A dropdown whose list is a `PickerPopup` — the search-first way to choose one thing.

`DropDown` is right for a handful of fixed options: an operator, a combiner, a tag type.
It is wrong the moment the list is *the user's vocabulary* or *the pack's contents*: 135
registries, or every tag and attribute a registry carries, which grows every time a tag is
defined. A combo box makes you scroll for those; this lets you type, with the same token
matching as every other search in the app, and each row can say what it is underneath
("enum tag · overworld, nether, end") so choosing does not mean remembering.

It reads like a `DropDown` — same box, same caret, same place in a form — so a row of
controls mixing the two does not look like two toolkits. What it holds is a list of
`Choice`s and a current *value*; the label shown is the chosen row's label.

**Values are strings**, because that is what `PickerPopup.chosen` carries. Callers with
richer objects keep a key → object map and hand this the keys.
"""
from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QPushButton

from packsmith.gui.shell import icons, style
from packsmith.gui.shell.picker import Choice, PickerPopup, _as_choice

QSS = f"""
    QPushButton {{
        background: {style.BG_DEEP}; color: {style.TEXT};
        border: 1px solid {style.BORDER}; padding: 2px 22px 2px 6px; font-size: 11px;
        text-align: left;
    }}
    QPushButton:hover {{ border-color: {style.ACCENT_EDGE}; }}
    QPushButton:focus {{ border-color: {style.ACCENT_EDGE}; }}
    QPushButton:disabled {{ color: {style.TEXT_FAINT}; border-color: {style.BORDER}; }}
"""


class PickerButton(QPushButton):
    """Choose one value from a searchable list. Emits `changed(value)` on a real change."""

    changed = Signal(str)
    # Forwarded from the popup's "＋ New …" row, with whatever was typed. The owner decides
    # what creating means and, if it made something, calls `set_choices` + `set_current`.
    create_requested = Signal(str)

    def __init__(self, choices=(), *, header="", placeholder="type to search",
                 create=None, parent=None):
        super().__init__(parent)
        self._header = header
        self._placeholder = placeholder
        self._create = create
        self._choices = []
        self._current = None
        self._picker = None            # held: a popup with no owner vanishes mid-show
        self.setStyleSheet(QSS)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(self.open_picker)
        self.set_choices(choices)

    # --- state -------------------------------------------------------------

    def set_choices(self, choices):
        """Replace the list. The current value survives if it is still offered; otherwise
        the first row becomes current, as a combo box would — silently holding a value no
        longer in the list is how a stale choice ends up saved."""
        self._choices = [_as_choice(c) for c in choices]
        if self._index_of(self._current) < 0:
            self._current = self._choices[0].returns if self._choices else None
        self._sync_text()

    def set_current(self, value) -> bool:
        """Select `value` without emitting. False if it is not one of the choices."""
        if self._index_of(value) < 0:
            return False
        self._current = value
        self._sync_text()
        return True

    def current(self):
        return self._current

    def current_choice(self) -> Choice | None:
        index = self._index_of(self._current)
        return self._choices[index] if index >= 0 else None

    def choices(self) -> list:
        return list(self._choices)

    def _index_of(self, value) -> int:
        if value is None:
            return -1
        return next((i for i, c in enumerate(self._choices) if c.returns == value), -1)

    def _sync_text(self):
        choice = self.current_choice()
        self.setText(choice.label if choice else "")
        self.setToolTip("\n".join(filter(None, (choice.label, choice.detail)))
                        if choice else "")

    # --- the popup ---------------------------------------------------------

    def open_picker(self):
        picker = PickerPopup(self._choices, header=self._header,
                             placeholder=self._placeholder, parent=self,
                             create=self._create, searchable=True)
        picker.chosen.connect(self._on_chosen)
        picker.create_requested.connect(self.create_requested)
        self._picker = picker
        rect = QRect(self.mapToGlobal(QPoint(0, 0)), self.size())
        # A turn late, like every other picker in the app: opened from inside the click that
        # is still being delivered, a Qt.Popup can receive that same release and read it as
        # an outside click, closing the moment it opens.
        QTimer.singleShot(0, lambda: picker.popup_under(rect))
        return picker

    def _on_chosen(self, value):
        if value == self._current:
            return
        self._current = value
        self._sync_text()
        self.changed.emit(value)

    # --- look --------------------------------------------------------------

    def paintEvent(self, event):
        """The same caret `DropDown` paints, so the two read as one control."""
        super().paintEvent(event)
        glyph = icons.ui("down")
        if not glyph:
            return
        font = QFont(icons.family())
        font.setPixelSize(10)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.setFont(font)
        painter.setPen(QColor(style.TEXT if self.underMouse() else style.TEXT_MUTED))
        painter.drawText(self.rect().adjusted(0, 0, -6, 0),
                         Qt.AlignRight | Qt.AlignVCenter, glyph)
        painter.end()
