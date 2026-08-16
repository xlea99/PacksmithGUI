"""Dry Run beside Run, everywhere Run already is (design 3.3).

Only the gating is tested. Which button emits which signal is self-reporting — press it
and nothing happens — but a **dry run that is not gated like a real one** looks fine and is
wrong: §3.3.2 insists the surfaces judging whether a job can run agree with each other, and
a control offering a preview of a job the panel has greyed out is a third opinion.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.bindings import StepProblem
from packsmith.gui.run_control import RunControl


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class Job:
    def __init__(self, id=1, name="Nightly", pinned=False):
        self.id, self.name, self.pinned = id, name, pinned


@pytest.fixture
def control(qapp):
    widget = RunControl()
    yield widget
    widget.deleteLater()


def test_both_buttons_follow_the_same_readiness(control):
    """A job that cannot run cannot be previewed either. A dry run resolves the same
    bindings first, so it would fail before any action body ran — it could tell you nothing
    the tooltip does not already say."""
    problem = StepProblem(step_id=1, position=0, action_ref="p:a",
                          detail="'target' is unbound", kind="broken")
    control.set_jobs([Job()], readiness=lambda job: [problem])

    assert not control._play.isEnabled()
    assert not control._dry.isEnabled(), "a blocked job was still offered as a dry run"
    assert "unbound" in control._dry.toolTip()


def test_both_are_offered_when_the_job_is_ready(control):
    control.set_jobs([Job()], readiness=lambda job: [])
    assert control._play.isEnabled() and control._dry.isEnabled()


def test_a_run_in_flight_locks_both(control):
    """Runs are globally serialised (§3.3) and the window blocks — but `processEvents`
    keeps it live enough to click, so the control locks rather than trusting nobody tries.
    A dry run is still a run for that purpose: it holds the same buffers and the same
    single-threaded loop."""
    control.set_jobs([Job()], readiness=lambda job: [])
    control.set_running(True)

    assert not control._play.isEnabled()
    assert not control._dry.isEnabled(), "a dry run could be started during a real one"


def test_a_blocked_job_emits_nothing(control):
    """The tooltip explains, the button refuses. Belt and braces, because `_on_dry_run` is
    reachable from the keyboard shortcut as well as the button."""
    problem = StepProblem(step_id=1, position=0, action_ref="p:a", detail="nope",
                          kind="broken")
    control.set_jobs([Job()], readiness=lambda job: [problem])
    fired = []
    control.dry_run_requested.connect(fired.append)

    control._on_dry_run()

    assert fired == []
