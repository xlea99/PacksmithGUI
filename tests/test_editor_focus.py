"""Coming back to an editor tab puts the keyboard back in the editor (design 6.3).

Reported from real use: click away from a Monaco tab, click back, and you had to click
*inside the text* before you could type. Measured with a real web view, focus after a tab
switch sat on the **QTabBar** — the editor was visible, looked focused, and every keystroke
went somewhere else.

The cause is that there are two kinds of focus and only one of them was being set.
`showModel` calls Monaco's own `editor.focus()`, which decides where the caret goes *inside
the page*; it says nothing about which widget Qt sends key events to. Switching tabs gives
Qt focus to the tab, not to the native child that was just reparented into it.

`EditorHost` is stubbed field-by-field here rather than constructed, the same way
`test_editor_locks` does it: `__init__` starts Chromium, which none of this has anything to
say about. What is under test is the handoff.
"""
import pytest

from packsmith.gui.editor.host import EditorHost, EditorTab


class FakeView:
    """Just enough QWebEngineView to answer the two questions `_take_focus` asks."""

    def __init__(self, visible=True):
        self._visible = visible
        self.focused_with = None

    def isVisible(self):
        return self._visible

    def setFocus(self, reason=None):
        self.focused_with = reason


@pytest.fixture
def host():
    host = EditorHost.__new__(EditorHost)
    host.view = FakeView()
    host.js = []
    host._js = host.js.append
    return host


def test_focusing_hands_the_keyboard_to_the_view(host):
    """The Qt half. Without it the keys go to whatever the tab switch focused — measured
    as the QTabBar, which is why the editor ignored them until it was clicked."""
    host._take_focus()
    assert host.view.focused_with is not None


def test_focusing_also_places_the_caret_in_the_page(host):
    """The Monaco half, re-asserted. `showModel` already called `editor.focus()`, but it
    ran while the widget could not accept focus at all — so the page had a caret nowhere."""
    host._take_focus()
    assert any("editor.focus()" in call for call in host.js)


def test_a_parked_view_is_not_focused(host):
    """The shared view lives in one tab at a time and is parked on the container between
    moves. Focusing it while parked would steal the keyboard for a widget nobody is
    looking at — and the timer can genuinely fire after the tab moved on, because the
    handoff is deferred a turn on purpose."""
    host.view._visible = False

    host._take_focus()

    assert host.view.focused_with is None
    assert host.js == []


def test_the_handoff_is_deferred_rather_than_immediate(host, qapp_unused=None):
    """`focus_editor` starts a timer instead of focusing now. The view has only just been
    reparented and shown, and focusing mid-move is dropped when the widget is re-shown at
    its new home — which is the whole bug, arrived at from the other side."""
    started = []
    host._focus_timer = type("T", (), {"start": lambda self: started.append(True)})()

    host.focus_editor()

    assert started == [True], "focus must wait a turn for the reparent to settle"
    assert host.view.focused_with is None, "and must not have focused synchronously"


def test_activating_a_tab_asks_for_focus():
    """The wiring. Everything above is dead code if `activate` never calls it — which is
    exactly the state this was found in."""
    asked = []
    stub = type("Host", (), {
        "key_for": staticmethod(lambda s, p: f"{s}:{p}"),
        "attach_to": lambda self, slot: None,
        "show_document": lambda self, key: None,
        "focus_editor": lambda self: asked.append(True),
    })()

    EditorTab.activate(type("Tab", (), {"_host": stub, "slot": None, "key": "k"})())

    assert asked == [True]
