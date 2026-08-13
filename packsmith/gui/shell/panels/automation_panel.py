"""Jobs and Actions, in one panel (design 4.1).

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
"""
from PySide6.QtWidgets import QTabWidget

from packsmith.gui.shell import style
from packsmith.gui.shell.panels.base import Panel

_TABS_QSS = f"""
    QTabWidget::pane {{ border: none; }}
    QTabBar::tab {{
        background: {style.BG_PANEL}; color: {style.TEXT_MUTED};
        padding: 4px 12px; font-size: 11px; border: none;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:hover {{ color: {style.TEXT}; }}
    QTabBar::tab:selected {{
        color: {style.TEXT}; border-bottom: 2px solid {style.ACCENT_EDGE};
    }}
"""


class AutomationPanel(Panel):
    """The Jobs and Actions panels, sharing one sidebar slot."""

    def __init__(self, jobs_panel, actions_panel, parent=None):
        super().__init__("Automation", parent)
        self._children = {"jobs": jobs_panel, "actions": actions_panel}

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(_TABS_QSS)
        self._tabs.setDocumentMode(True)
        for key, title in (("jobs", "Jobs"), ("actions", "Actions")):
            child = self._children[key]
            child.hide_header()          # the tab already carries the name
            self._tabs.addTab(child, title)
        self.body().addWidget(self._tabs)

    @property
    def current_key(self) -> str:
        index = self._tabs.currentIndex()
        return ("jobs", "actions")[index] if 0 <= index < 2 else "jobs"

    def show_tab(self, key: str):
        order = ("jobs", "actions")
        if key in order:
            self._tabs.setCurrentIndex(order.index(key))

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
