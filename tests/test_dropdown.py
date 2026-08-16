"""The shared dropdown (`gui/shell/dropdown.py`).

Every simple dropdown in the app is this widget, so what it does and what it must not do
are both worth pinning. Two things are tested, and both are silent when broken:

* **Where the popup opens.** A stock combo places its list so the *current row* lands on
  the box, which means the popup moves as the selection moves and climbs over whatever
  sits above the control. Reverting to that is invisible until someone watches a popup
  carefully — which is how it survived this long in the first place.
* **That it doesn't poison shutdown.** The proxy style behind the placement is a
  double-free waiting to happen if it is ever handed a base style, and it is now behind
  two dozen controls rather than one.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QComboBox, QMainWindow, QVBoxLayout, QWidget

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.gui.shell.dropdown import DropDown

ROOT = Path(__file__).resolve().parents[1]
ROWS = [f"item {i}" for i in range(12)]


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    yield app


def popup_offsets(combo, qapp):
    """Popup top edge minus box bottom edge, for a selection near each end of the list.

    0 means "directly below the box". Negative means the list opened over the control.
    """
    window = QMainWindow()
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.addSpacing(200)          # push it down the window, so opening upward is visible
    layout.addWidget(combo)
    layout.addStretch()
    window.setCentralWidget(holder)
    window.resize(400, 500)
    window.show()
    qapp.processEvents()

    offsets = []
    for index in (0, 6, 11):
        combo.setCurrentIndex(index)
        qapp.processEvents()
        combo.showPopup()
        qapp.processEvents()
        bottom = combo.mapToGlobal(combo.rect().bottomLeft()).y()
        offsets.append(combo.view().window().geometry().top() - bottom)
        combo.hidePopup()
        qapp.processEvents()
    window.close()
    return offsets


def test_the_popup_opens_below_the_box_whatever_is_selected(qapp):
    combo = DropDown()
    combo.addItems(ROWS)

    assert popup_offsets(combo, qapp) == [0, 0, 0]


def test_a_stock_combo_is_the_thing_being_fixed(qapp):
    """The premise, asserted rather than assumed.

    If Qt ever stops centring the current row on the box, this fails and the shared widget
    can lose its proxy style — which is worth knowing, because the proxy is the part with
    a lifetime to get wrong. Measured here: the popup climbs from 21px to 230px above the
    control as the selection moves down.
    """
    combo = QComboBox()
    combo.addItems(ROWS)

    offsets = popup_offsets(combo, qapp)

    assert len(set(offsets)) > 1, "a stock combo no longer moves its popup with the selection"
    assert all(o < 0 for o in offsets), "a stock combo no longer opens over the box"


def test_the_shared_dropdown_does_not_poison_shutdown():
    """`QProxyStyle(style)` takes ownership of what it is given, and a widget with no style
    of its own hands back the *application's*. Getting that wrong here now double-frees the
    app-wide style behind two dozen controls rather than one, and the only symptom is a
    nonzero exit code nobody looks at. It cannot be asserted in-process — the failure is
    the process dying — so the assertion is a subprocess's return code.
    """
    script = textwrap.dedent("""
        import os, sys
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        app.setStyle("Fusion")
        from packsmith.gui.shell.dropdown import DropDown
        HELD = [DropDown() for _ in range(3)]
        for d in HELD:
            d.addItems(["a", "b"])
        del app
        sys.exit(0)
    """)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, (
        f"building DropDowns made the interpreter exit {result.returncode}\n"
        f"{result.stderr[-2000:]}")
