"""Jobs, Actions and Packages, in one panel (design 4.1).

They were two sidebar slots, and the run control is what made that wrong. Jobs used to be
the app's busiest panel — open it, find the job, press play, over and over — and that
traffic earned it a slot of its own. Now running happens from the header, so the Jobs
*panel* is only for editing jobs, which is rare. Actions was always rare. Two occasional
surfaces, both Layer 3, both about automation: one slot.

**Tabs rather than stacked sections**, because the two are not the same shape. Jobs is a
flat list with run buttons; Actions is a file tree grouped by package with §3.3.1's
provenance rules on it. Stacked sections make differently-shaped lists fight over a narrow
column; tabs let each have the whole panel.

The tabs also keep the two ownership models visually apart — jobs are plain user data,
actions are governed by where they came from — so this panel deliberately grows no shared
toolbar that would imply one set of rules covers both.

**Packages is the third tab**, and it exists because one tree was answering two questions
at once. "What can I run?" wants a flat list of declared actions across every package;
"what is in this package?" wants that one package's whole file tree. Interleaved, the
Actions tab made you scroll past library files to find an entry point, and made an
undeclared `.star` look like a mistake rather than a helper. Split, each tab has one job.
"""
from PySide6.QtWidgets import QTabWidget

from packsmith.gui.shell import style
from packsmith.gui.shell.panels.base import Panel, StubPanel

# The dividers are the point. Three flat labels in a row, distinguished only by which one
# is brighter, read as one run-on string — you cannot see where "Actions" stops and
# "Packages" starts, so the strip has to be parsed rather than glanced at. A hairline
# between them is what makes them look like three targets. `:last-tab` keeps a trailing
# divider off the end, where there is nothing to separate from.
_TABS_QSS = f"""
    QTabWidget::pane {{ border: none; border-top: 1px solid {style.BORDER}; }}
    QTabBar {{ qproperty-drawBase: 0; }}
    QTabBar::tab {{
        background: {style.BG_PANEL}; color: {style.TEXT_MUTED};
        padding: 4px 6px; font-size: 11px; border: none;
        border-right: 1px solid {style.BORDER};
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:last {{ border-right: none; }}
    QTabBar::tab:only-one {{ border-right: none; }}
    QTabBar::tab:hover {{ color: {style.TEXT}; }}
    QTabBar::tab:selected {{
        color: {style.TEXT}; border-bottom: 2px solid {style.ACCENT_EDGE};
    }}
"""

_ORDER = ("jobs", "actions", "packages")
_TITLES = {"jobs": "Jobs", "actions": "Actions", "packages": "Packages"}

_PACKAGES_BLURB = (
    "One package at a time, picked from a dropdown: its whole file tree alongside the "
    "actions its manifest declares.\n\nThe Actions tab answers \"what can I run\"; this "
    "one will answer \"what is in this package\" — which is a different question and was "
    "making a mess of one tree."
)


class AutomationPanel(Panel):
    """The Jobs, Actions and Packages panels, sharing one sidebar slot."""

    def __init__(self, jobs_panel, actions_panel, packages_panel=None, parent=None):
        super().__init__("Automation", parent)
        # Built here rather than passed in, because there is nothing yet to wire: a stub
        # has no signals and no store. It becomes a constructor argument like the other two
        # on the day it has either.
        self._children = {
            "jobs": jobs_panel,
            "actions": actions_panel,
            "packages": packages_panel or StubPanel("Packages", _PACKAGES_BLURB),
        }

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(_TABS_QSS)
        self._tabs.setDocumentMode(True)
        # Share the width between them, and set it AFTER `setDocumentMode`, which turns
        # expanding off on the way past. Without this the three tabs are sized to their own
        # labels: they used two-thirds of a 230px sidebar and left the rest bare, and the
        # third tab tipped the total past the panel width into scroll arrows — an overflow
        # control for three words.
        self._tabs.tabBar().setExpanding(True)
        self._tabs.tabBar().setDrawBase(False)
        for key in _ORDER:
            child = self._children[key]
            child.hide_header()          # the tab already carries the name
            self._tabs.addTab(child, _TITLES[key])
        self.body().addWidget(self._tabs)

    @property
    def current_key(self) -> str:
        index = self._tabs.currentIndex()
        return _ORDER[index] if 0 <= index < len(_ORDER) else _ORDER[0]

    def show_tab(self, key: str):
        if key in _ORDER:
            self._tabs.setCurrentIndex(_ORDER.index(key))

    def panel(self, key):
        return self._children.get(key)

    def refresh(self):
        """Reload both halves.

        Both, not just the visible one: the panel stack keeps hidden widgets alive, so a
        tab refreshed only when shown would sit on stale data — the exact staleness the
        rest of the app has been chasing out all week.
        """
        for child in self._children.values():
            reload_it = getattr(child, "refresh", None)
            if callable(reload_it):
                reload_it()
