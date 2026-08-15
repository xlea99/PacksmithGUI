"""The header's run control, and the merged Automation panel — designs 3.3.2 and 4.1.

§3.3.2 calls jobs *"rarely edited but constantly re-run"*, and pinning used to be defined
as sorting one to the top of the Jobs panel — *"purely UX"*. That left running a job as:
open the panel, find it, press play, and now the panel covers what you were reading.

The run control is the run-configuration answer: a picker and a play button in the header,
reachable from anywhere. **Pinning now promotes a job into it**, which is the first thing
the flag has ever bought.

Its two refusals are the load-bearing part. A job that cannot run says why rather than
failing on click, using the *same* readiness function the Jobs panel colours from — §3.3.2
insists those must agree, and a third surface must not become the one that disagrees. And
it locks during a run, because runs are serialised by the window blocking, but the progress
reporting keeps the UI live enough to click.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.gui.run_control import RunControl


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class Job:
    def __init__(self, id, name, pinned=False):
        self.id, self.name, self.pinned = id, name, pinned


class Problem:
    def __init__(self, detail):
        self.detail = detail


@pytest.fixture
def control(qapp):
    widget = RunControl()
    yield widget
    widget.deleteLater()


def labels(control):
    return [control._picker.itemText(i) for i in range(control._picker.count())]


# --- what pinning now buys ----------------------------------------------------------------

def names(control):
    """Labels with the pin mark stripped, whatever that mark currently is."""
    from packsmith.gui.shell import icons
    pin = icons.ui("pin")
    return [l.replace(pin, "").strip() if pin else l.strip() for l in labels(control)]


def test_pinned_jobs_come_first(control):
    """The whole point of the change: pinning promotes a job into the control."""
    control.set_jobs([Job(1, "Nightly"), Job(2, "Stone", pinned=True), Job(3, "Hide")])
    assert names(control) == ["Stone", "Nightly", "Hide"]


def test_a_pinned_job_is_marked(control):
    """By the Phosphor pin, not an emoji — an emoji beside a real icon set is what looks
    unfinished."""
    from packsmith.gui.shell import icons
    control.set_jobs([Job(1, "Nightly"), Job(2, "Stone", pinned=True)])
    assert labels(control)[0].startswith(icons.ui("pin"))
    assert not labels(control)[1].startswith(icons.ui("pin"))


def test_unpinned_jobs_are_still_offered(control):
    """Pinning is a promotion, not a filter — §3.3.2 keeps unpinned jobs runnable."""
    control.set_jobs([Job(1, "Nightly"), Job(2, "Stone", pinned=True)])
    assert len(labels(control)) == 2


def test_order_within_a_group_is_the_stores_order(control):
    """Two orderings for one set of jobs is how a user learns to distrust both."""
    control.set_jobs([Job(1, "zebra"), Job(2, "alpha"), Job(3, "middle")])
    assert names(control) == ["zebra", "alpha", "middle"]


# --- running ------------------------------------------------------------------------------

def test_play_emits_the_selected_job(control):
    fired = []
    control.run_requested.connect(fired.append)
    control.set_jobs([Job(1, "Nightly"), Job(2, "Stone")])
    control.select(2)
    control._play.click()

    assert [j.id for j in fired] == [2]


def test_selecting_a_job_that_is_not_there_fails_honestly(control):
    control.set_jobs([Job(1, "Nightly")])
    assert control.select(99) is False
    assert control.current_job_id() == 1


# --- refusing, out loud ----------------------------------------------------------------------

def test_an_unrunnable_job_disables_play_and_says_why(control):
    """Not merely greyed. "Why is this disabled" is the question a disabled button always
    raises, and the answer already exists."""
    control.set_jobs([Job(1, "Nightly")],
                     readiness=lambda job: [Problem("'target' was renamed to 'banned'")])

    assert not control._play.isEnabled()
    assert "can't run yet" in control._play.toolTip()
    assert "renamed to 'banned'" in control._play.toolTip()


def test_a_runnable_job_offers_to_run_it(control):
    control.set_jobs([Job(1, "Nightly")], readiness=lambda job: [])
    assert control._play.isEnabled()
    assert control._play.toolTip() == "Run 'Nightly'"


def test_clicking_a_disabled_play_emits_nothing(control):
    """Belt and braces: the guard is in the handler too, because `click()` bypasses the
    enabled state in code paths that reach it directly."""
    fired = []
    control.run_requested.connect(fired.append)
    control.set_jobs([Job(1, "Nightly")], readiness=lambda job: [Problem("nope")])
    control._on_play()
    assert fired == []


def test_readiness_is_rechecked_when_the_selection_changes(control):
    """A stale enabled state on the new job would be a play button that lies."""
    broken = {2}
    control.set_jobs([Job(1, "Fine"), Job(2, "Broken")],
                     readiness=lambda job: [Problem("x")] if job.id in broken else [])
    assert control._play.isEnabled()

    control.select(2)
    assert not control._play.isEnabled()


def test_a_readiness_function_that_raises_does_not_break_the_header(control):
    """A cosmetic check must never take down the window chrome."""
    def hostile(job):
        raise RuntimeError("boom")

    control.set_jobs([Job(1, "Nightly")], readiness=hostile)   # must not raise
    assert control._play.isEnabled()


# --- while a run is in flight --------------------------------------------------------------------

def test_the_control_locks_during_a_run(control):
    """Runs are serialised by the window blocking — but `processEvents` keeps the UI live
    enough to click, so this disables rather than trusting nobody tries."""
    control.set_jobs([Job(1, "Nightly")], readiness=lambda job: [])
    control.set_running(True)

    assert not control._play.isEnabled()
    assert control._play.toolTip() == "A job is already running"

    control.set_running(False)
    assert control._play.isEnabled()


# --- empty and edge states -------------------------------------------------------------------

def test_with_no_jobs_the_control_hides_itself(control):
    """An empty dropdown in the header is chrome that does nothing."""
    control.set_jobs([])
    assert control.isHidden()
    assert control.current_job() is None


def test_it_reappears_once_a_job_exists(control):
    control.set_jobs([])
    control.set_jobs([Job(1, "Nightly")])
    assert not control.isHidden()


def test_reloading_keeps_the_current_selection(control):
    """Job lists reload constantly — on rename, pin, packdump adopt. Snapping back to the
    first job each time would make the header wander."""
    control.set_jobs([Job(1, "A"), Job(2, "B"), Job(3, "C")])
    control.select(3)
    control.set_jobs([Job(1, "A"), Job(2, "B"), Job(3, "C")])
    assert control.current_job_id() == 3


def test_a_selection_that_was_deleted_falls_back(control):
    control.set_jobs([Job(1, "A"), Job(2, "B")])
    control.select(2)
    control.set_jobs([Job(1, "A")])
    assert control.current_job_id() == 1


def test_an_explicit_selection_wins_on_reload(control):
    """How a remembered choice is restored when a profile opens."""
    control.set_jobs([Job(1, "A"), Job(2, "B")], select=2)
    assert control.current_job_id() == 2


# --- the merged Automation panel (§4.1) --------------------------------------------------------

@pytest.fixture
def automation(qapp):
    from packsmith.gui.shell.panels.automation_panel import AutomationPanel
    from packsmith.gui.shell.panels.base import Panel

    class Child(Panel):
        def __init__(self, title):
            super().__init__(title)
            self.refreshed = 0

        def refresh(self):
            self.refreshed += 1

    jobs, actions = Child("Jobs"), Child("Actions")
    panel = AutomationPanel(jobs, actions)
    yield panel, jobs, actions
    panel.deleteLater()


def test_both_panels_share_the_slot(automation):
    panel, jobs, actions = automation
    assert panel.panel("jobs") is jobs
    assert panel.panel("actions") is actions
    # Three: Packages joined them, and it builds its own placeholder rather than being
    # passed in — there is nothing to wire to a stub.
    assert panel._tabs.count() == 3
    assert panel.panel("packages") is not None


def test_jobs_is_the_default_tab(automation):
    """Running moved to the header, so the panel opens on the thing you came to edit."""
    panel, _, _ = automation
    assert panel.current_key == "jobs"


def test_switching_tabs_works_by_name(automation):
    panel, _, _ = automation
    panel.show_tab("actions")
    assert panel.current_key == "actions"


def test_an_unknown_tab_name_is_ignored(automation):
    panel, _, _ = automation
    panel.show_tab("nonsense")
    assert panel.current_key == "jobs"


def test_the_nested_panels_lose_their_own_titles(automation):
    """The tab already carries the name; two stacked labels for one thing reads as a bug."""
    _, jobs, actions = automation
    assert jobs._header.isHidden() and actions._header.isHidden()


def test_refreshing_reloads_both_tabs_not_just_the_visible_one(automation):
    """The panel stack keeps hidden widgets alive, so a tab refreshed only when shown sits
    on stale data — the exact staleness the rest of the app keeps having to chase out."""
    panel, jobs, actions = automation
    panel.refresh()
    assert (jobs.refreshed, actions.refreshed) == (1, 1)
