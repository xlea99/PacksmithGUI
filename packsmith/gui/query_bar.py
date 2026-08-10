"""The filter bar (design 3.2.3, 5.3).

§3.2.3 puts a filter bar on every view: *"This is where queries live — not in the sidebar,
not in a separate dialog."* But a later decision made the View's own query its **stable
identity**, edited deliberately behind the ⚙ constructor rather than by an always-on bar.

Both hold, because they're about different things:

* the **view's query** is what the view *is* — scope, columns, the filter that defines it;
* the **bar** is "narrow what I'm looking at right now" — transient, ANDed on top, gone
  when you clear it.

So typing here never edits the view. When a refinement turns out to be worth keeping,
*Keep* folds it into the view's own query — which is §3.2.3's "filter bar → save" path,
made explicit instead of implicit.

Bare words are the candidate finder (design 5.3): typing `granite brick wall` finds
`quark:granite_bricks_wall` regardless of word order or plurals, which is the single most
common thing anyone will type here.
"""
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from packsmith.core.query.ast import And
from packsmith.core.query.language import QuerySyntaxError, format, parse
from packsmith.core.query.template import parameters
from packsmith.gui.shell import style

# Long enough that typing a word doesn't re-query on every letter, short enough to feel
# live. Re-evaluation walks the whole registry, so this is the difference between a bar
# that responds and one that fights you.
_DEBOUNCE_MS = 250

_PLACEHOLDER = 'granite brick wall     |     t:remove == true AND mod == "quark"'


class QueryBar(QWidget):
    """A text filter over the tab's base query. Emits parsed AST, never text."""

    filter_changed = Signal(object)     # a filter AST node, or None for "no refinement"
    keep_requested = Signal(object)     # fold this refinement into the view's own query

    def __init__(self, parent=None):
        super().__init__(parent)
        self._node = None
        self._can_keep = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._input = QLineEdit()
        self._input.setPlaceholderText(_PLACEHOLDER)
        self._input.setClearButtonEnabled(True)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {style.BG_DEEP}; color: {style.TEXT};
                border: 1px solid {style.BORDER}; padding: 3px 6px; font-size: 12px;
            }}
            QLineEdit:focus {{ border-color: {style.ACCENT_EDGE}; }}
        """)
        self._input.textChanged.connect(self._on_typed)
        self._input.returnPressed.connect(self._apply)
        lay.addWidget(self._input, 1)

        self._status = QLabel()
        self._status.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
        self._status.setMinimumWidth(120)
        lay.addWidget(self._status)

        self._keep = QPushButton("Keep")
        self._keep.setFixedHeight(22)
        self._keep.setCursor(Qt.PointingHandCursor)
        self._keep.setToolTip("Fold this refinement into the view's own query")
        self._keep.setStyleSheet(f"""
            QPushButton {{
                background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                border: 1px solid {style.BORDER}; font-size: 11px; padding: 1px 10px;
            }}
            QPushButton:hover {{ color: {style.TEXT}; border-color: {style.ACCENT_EDGE}; }}
        """)
        self._keep.clicked.connect(lambda: self.keep_requested.emit(self._node))
        self._keep.hide()
        lay.addWidget(self._keep)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DEBOUNCE_MS)
        self._timer.timeout.connect(self._apply)

    # --- typing ------------------------------------------------------------

    def _on_typed(self, _text):
        # Parse on every keystroke (cheap, and it's what turns the error red immediately);
        # only the RE-QUERY waits for the debounce.
        self._timer.start()
        try:
            self._parse()
        except QuerySyntaxError as e:
            self._show_error(e)

    def _parse(self):
        # typing=True: the word under the cursor matches as a prefix, so results narrow as
        # you type instead of sitting at zero until the last letter.
        node = parse(self._input.text(), typing=True)
        # A filter has no cell to resolve against, so `@col` here would quietly search for
        # the literal text "@col" and find nothing. Say so instead.
        used = parameters(node)
        if used:
            raise QuerySyntaxError(
                f"{used[0]} only works in a blueprint's Suggest bar — a filter has no "
                f"row and column to resolve it against")
        self._mark_valid()
        return node

    def _apply(self):
        self._timer.stop()
        try:
            node = self._parse()
        except QuerySyntaxError:
            return                      # keep the last good result on screen
        self._node = node
        self._keep.setVisible(bool(node) and self._can_keep)
        self.filter_changed.emit(node)

    # --- feedback ----------------------------------------------------------

    def _show_error(self, error: QuerySyntaxError):
        self._input.setStyleSheet(self._input.styleSheet().replace(
            style.BORDER, style.ERROR, 1))
        self._status.setStyleSheet(f"color: {style.ERROR}; font-size: 11px;")
        self._status.setText(str(error).splitlines()[0])
        self._status.setToolTip(str(error))

    def _mark_valid(self):
        self._input.setStyleSheet(self._input.styleSheet().replace(
            style.ERROR, style.BORDER, 1))
        self._status.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
        self._status.setToolTip("")

    def report(self, shown: int, total: int, seconds: float = None):
        """Called back by the tab once the refinement has actually been run."""
        if self._node is None:
            self._status.setText(f"{total:,} rows")
        else:
            timing = f"  ({seconds:.1f}s)" if seconds and seconds > 0.5 else ""
            self._status.setText(f"{shown:,} of {total:,}{timing}")

    def set_keepable(self, keepable: bool):
        """Only a saved view has a query worth folding into."""
        self._can_keep = keepable
        self._keep.setVisible(bool(self._node) and keepable)

    def text(self) -> str:
        return self._input.text()

    def set_node(self, node):
        """Show an AST as text — the printer half of the round trip, used when something
        other than typing changes the refinement."""
        self._input.setText(format(node))
        self._apply()

    def clear(self):
        self._input.clear()


def combine(base, refinement):
    """The view's own filter AND the bar's, without nesting Ands pointlessly."""
    if refinement is None:
        return base
    if base is None:
        return refinement
    clauses = list(base.clauses) if isinstance(base, And) else [base]
    clauses += list(refinement.clauses) if isinstance(refinement, And) else [refinement]
    return And(clauses)
