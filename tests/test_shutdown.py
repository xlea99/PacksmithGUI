"""Packsmith must exit with status 0.

A shutdown crash is the definition of a failure that hides. The window closes, the user
gets their prompt back, and nothing on screen is wrong — the only trace is an exit code
nobody looks at unless their IDE happens to print it. Meanwhile the cause is a double
free, which is undefined behaviour rather than a cosmetic blemish: the same corruption is
free to surface mid-session instead of at the end.

It cannot be asserted in-process, because the failure *is* the process dying — a segfault
takes pytest with it and reports nothing. So each case runs in a subprocess and the
assertion is its return code.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


_PRELUDE = """\
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication([])
app.setStyle("Fusion")
HELD = []
"""


def run_qt(body: str) -> subprocess.CompletedProcess:
    """Run `body` in a fresh interpreter with a Qt app, and report how it exited.

    The body runs at module level, so it is dedented and appended rather than nested —
    the objects it appends to HELD stay alive until finalization, which is the moment
    under test.
    """
    script = _PRELUDE + textwrap.dedent(body) + "\ndel app\nsys.exit(0)\n"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "QT_QPA_PLATFORM": "offscreen"}
    return subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def test_building_the_packages_panel_does_not_poison_shutdown(tmp_path):
    """The regression, at its smallest.

    `PackagesPanel` gives its combo box a `QProxyStyle` so the dropdown opens downward.
    `QProxyStyle(style)` **takes ownership** of the style it is handed, and a widget with
    no style of its own returns the *application's* style from `.style()` — so
    `_DropDownStyle(self._picker.style())` made the panel a co-owner of the style the
    entire app shares. Panel and QApplication both deleted it on the way out, and every
    single launch ended in an access violation (0xC0000005) at teardown.

    Constructing the panel is enough to reproduce it; it never has to be shown.
    """
    result = run_qt(f"""
        from packsmith.core.packages import PackageIndex
        from packsmith.gui.shell.panels.packages_panel import PackagesPanel
        HELD.append(PackagesPanel(PackageIndex({str(tmp_path)!r})))
    """)
    assert result.returncode == 0, (
        f"building PackagesPanel made the interpreter exit {result.returncode}\n"
        f"{result.stderr[-2000:]}")


def test_a_proxy_style_never_takes_the_application_style(tmp_path):
    """The rule the fix rests on, stated so it can't be reintroduced elsewhere.

    This is the shape to avoid — and it is the *tempting* shape, because handing a proxy
    "the style this widget already uses" reads as obviously correct. It is a double free.
    Asserted directly against Qt rather than through Packsmith, so it keeps documenting
    why even if `PackagesPanel` stops using a proxy style entirely.
    """
    poison = run_qt("""
        from PySide6.QtWidgets import QComboBox, QProxyStyle
        combo = QComboBox()
        assert combo.style() is app.style(), "premise changed: widget no longer shares the app style"
        HELD.append(QProxyStyle(combo.style()))
    """)
    assert poison.returncode != 0, (
        "handing the application's own style to a QProxyStyle no longer double-frees — "
        "if Qt fixed this, the guard in packages_panel can be simplified")

    clean = run_qt("""
        from PySide6.QtWidgets import QComboBox, QProxyStyle
        combo = QComboBox()
        proxy = QProxyStyle()          # no base: resolves to the app style without owning it
        combo.setStyle(proxy)
        HELD.append(proxy)
    """)
    assert clean.returncode == 0, (
        f"a base-less QProxyStyle should be safe, exited {clean.returncode}\n"
        f"{clean.stderr[-2000:]}")
