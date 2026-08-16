"""The run control — a job picker and a play button, in the header.

The problem it solves is the one §3.3.2 describes without answering: jobs are *"rarely
edited but constantly re-run"*. Pinning was specified as sorting a job to the top of the
Jobs panel and nothing else — *"purely UX"* — which still leaves running one as: open the
panel, find the job, press play, and now the panel is covering whatever you were reading.

This is the run-configuration pattern instead. An IDE does not make you dock a tool window
to press play; it keeps a control in the toolbar, reachable from anywhere, whatever panel
is open. So **pinning now means something**: a pinned job is promoted into this control.

Two things it deliberately does NOT do:

* **Refuse quietly.** A job that cannot run (an unbound mapping, a renamed tag) disables
  play and says why in the tooltip. §3.3.2 already insists the panel's colouring and the
  behaviour on pressing play must agree; a third surface must not be the one that disagrees.
* **Let you start a second run.** Runs are globally serialised (§3.3) and the window blocks
  during one, but `processEvents` keeps the UI live enough to click — so the control locks
  itself for the duration rather than relying on nobody trying.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QPushButton, QWidget

from packsmith.gui.shell import icons, style

_COMBO_QSS = f"""
    QComboBox {{
        background: {style.BG_CHROME}; color: {style.TEXT};
        border: 1px solid {style.BORDER}; border-radius: 2px;
        padding: 2px 8px; font-size: 11px; min-width: 150px;
    }}
    QComboBox:hover {{ border-color: {style.ACCENT_EDGE}; }}
    QComboBox::drop-down {{ border: none; width: 16px; }}
    QComboBox QAbstractItemView {{
        background: {style.BG_PANEL}; color: {style.TEXT};
        selection-background-color: {style.ACCENT};
        border: 1px solid {style.BORDER}; outline: none;
    }}
"""

_PLAY_QSS = f"""
    QPushButton {{
        background: transparent; color: {style.SUCCESS};
        border: 1px solid {style.BORDER}; border-radius: 2px;
        padding: 2px 9px; font-size: 13px;
    }}
    QPushButton:hover:enabled {{ background: {style.BG_CHROME}; color: {style.TEXT}; }}
    QPushButton:disabled {{ color: {style.TEXT_FAINT}; }}
"""

# The dry-run button is deliberately quieter than play — muted rather than green. It is
# the safe one, and a control that shouts about the harmless option and whispers about the
# committing one has its emphasis exactly backwards.
_DRY_QSS = f"""
    QPushButton {{
        background: transparent; color: {style.TEXT_MUTED};
        border: 1px solid {style.BORDER}; border-radius: 2px;
        padding: 2px 9px; font-size: 13px;
    }}
    QPushButton:hover:enabled {{ background: {style.BG_CHROME}; color: {style.TEXT}; }}
    QPushButton:disabled {{ color: {style.TEXT_FAINT}; }}
"""


class RunControl(QWidget):
    """Pick a job, press play. Lives in the header, works from any panel."""

    run_requested = Signal(object)      # the Job
    dry_run_requested = Signal(object)  # the Job — walk it, promote nothing

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs = []
        self._readiness = None
        self._running = False

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self._picker = QComboBox()
        self._picker.setStyleSheet(_COMBO_QSS)
        self._picker.currentIndexChanged.connect(lambda _: self._sync())
        row.addWidget(self._picker)

        # Dry run sits BEFORE play, in reading order. The safe one first is the same
        # instinct as putting Cancel before OK: you pass it on the way to the one that
        # commits, rather than reaching past the committing one to find it.
        self._dry = QPushButton()
        icons.mark(self._dry, "dry_run", size=14)
        self._dry.setCursor(Qt.PointingHandCursor)
        self._dry.setStyleSheet(_DRY_QSS)
        self._dry.clicked.connect(self._on_dry_run)
        row.addWidget(self._dry)

        self._play = QPushButton()
        icons.mark(self._play, "play", size=14)
        self._play.setCursor(Qt.PointingHandCursor)
        self._play.setStyleSheet(_PLAY_QSS)
        self._play.clicked.connect(self._on_play)
        row.addWidget(self._play)

    # --- content -----------------------------------------------------------

    def set_jobs(self, jobs, readiness=None, select=None):
        """Fill the picker. ``readiness(job)`` returns that job's blocking problems.

        Pinned jobs come first — that is what pinning buys now. Within each group the
        store's own order is kept, so the control and the Jobs panel list things the same
        way; two orderings for one set of jobs is how a user learns to distrust both.
        """
        self._jobs = sorted(jobs, key=lambda j: not getattr(j, "pinned", False))
        self._readiness = readiness

        previous = select if select is not None else self.current_job_id()
        self._picker.blockSignals(True)
        self._picker.clear()
        for job in self._jobs:
            pin = icons.ui("pin")
            label = (f"{pin}  {job.name}"
                     if pin and getattr(job, "pinned", False) else job.name)
            self._picker.addItem(label, job.id)
        index = self._picker.findData(previous)
        self._picker.setCurrentIndex(index if index >= 0 else 0)
        self._picker.blockSignals(False)

        self.setVisible(bool(self._jobs))
        self._sync()

    def current_job_id(self):
        return self._picker.currentData()

    def current_job(self):
        job_id = self.current_job_id()
        return next((j for j in self._jobs if j.id == job_id), None)

    def select(self, job_id) -> bool:
        index = self._picker.findData(job_id)
        if index < 0:
            return False
        self._picker.setCurrentIndex(index)
        return True

    def set_running(self, running: bool):
        self._running = running
        self._sync()

    # --- state -------------------------------------------------------------

    def _problems(self, job) -> list:
        if job is None or self._readiness is None:
            return []
        try:
            return list(self._readiness(job))
        except Exception:
            return []       # a cosmetic check must never break the header

    def _sync(self):
        job = self.current_job()
        problems = self._problems(job)
        # Gated identically. A dry run of a job with an unbound mapping would fail at
        # binding resolution before any action body ran, so it could not tell you anything
        # the tooltip does not already say — and §3.3.2 insists the surfaces that judge
        # runnability agree with each other.
        ready = bool(job) and not problems and not self._running
        self._play.setEnabled(ready)
        self._dry.setEnabled(ready)

        if self._running:
            self._play.setToolTip("A job is already running")
            self._dry.setToolTip("A job is already running")
            self._picker.setToolTip("")
            return
        if job is None:
            self._play.setToolTip("No jobs yet")
            self._dry.setToolTip("No jobs yet")
            return
        if problems:
            # Named, not merely greyed. "Why is this disabled" is the question a disabled
            # button always raises, and the answer already exists.
            detail = "\n".join(f"• {p.detail}" for p in problems[:4])
            self._play.setToolTip(f"'{job.name}' can't run yet:\n{detail}")
            self._dry.setToolTip(f"'{job.name}' can't run yet:\n{detail}")
        else:
            self._play.setToolTip(f"Run '{job.name}'")
            self._dry.setToolTip(
                f"Dry run '{job.name}' — walk every step and change nothing")
        self._picker.setToolTip(job.name)

    def _on_play(self):
        job = self.current_job()
        if job is not None and not self._problems(job) and not self._running:
            self.run_requested.emit(job)

    def _on_dry_run(self):
        job = self.current_job()
        if job is not None and not self._problems(job) and not self._running:
            self.dry_run_requested.emit(job)
