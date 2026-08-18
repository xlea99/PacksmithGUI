"""The Job Runs strip, and the log that moved into the report (design 4.1).

Two surfaces were both trying to be the full account of a run. The strip predates the Run
Report tab — back when it was the only account, so it grew wrapped failure reasons on
auto-expanding child rows — and §4.1's ruling for the packdump strip settles it for this
one too: *a strip a few rows tall is a place to learn THAT something changed, not to read
WHAT.* So the strip lost its detail and the report gained the run's log.

What is worth pinning here is not layout — a mis-sized column announces itself the moment
you look at it. It is the two things that would be **quietly wrong**: a change count that
silently inflates (counting no-ops, or double-counting a run by adding its steps to itself),
and a log that is present in a fresh run but lost when the same run is read back from
history — which is exactly the case nobody would notice until they went looking days later.
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core import reports
from packsmith.gui.shell.bottom_views import (
    JobRunsView, _change_counts, _error_sliver)


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def change(kind="added", engine="tag"):
    return {"engine": engine, "kind": kind, "before": None,
            "after": {"value": True, "owner": "action", "action_ref": "p:a"}}


def rollback(*kinds, inverse=True):
    return json.dumps({
        "l2": [{"x": 1}] if inverse else [], "files": {}, "blueprints": [],
        "changes": [change(kind) for kind in kinds],
    })


# --- how much changed, as a number ---------------------------------------------------

def test_changes_are_counted_by_kind(qapp):
    assert _change_counts([rollback("added", "added", "changed")]) == "2 added, 1 changed"


def test_kinds_read_in_a_fixed_order_not_alphabetically(qapp):
    """What came into existence, what moved, what left. Alphabetical would open with
    `changed` and bury `added`, and the column would read differently run to run."""
    counts = _change_counts([rollback("removed", "claimed", "added", "changed")])
    assert counts == "1 added, 1 changed, 1 claimed, 1 removed"


def test_no_ops_are_not_counted(qapp):
    """§3.3 keeps `unchanged` in the record and out of the headline. Counting it would make
    every re-run of an applied job look like it did the whole thing again."""
    assert _change_counts([rollback("added", "unchanged", "unchanged")]) == "1 added"


def test_a_run_counts_its_steps_once_between_them(qapp):
    """A run row sums its steps rather than restating each — the old column said
    "3 step(s)", which is a fact about the job, not about what it did."""
    assert _change_counts([rollback("added"), rollback("added", "removed")]) == \
        "2 added, 1 removed"


@pytest.mark.parametrize("stored", [None, "", "{not json", json.dumps({"l2": []})])
def test_nothing_to_count_says_so_rather_than_failing(qapp, stored):
    """Includes a row written before change records existed — an old run honestly has
    nothing to show, which is not a reason to break the strip it appears in."""
    assert _change_counts([stored]) == "—"


# --- the sliver of a failure ---------------------------------------------------------

def test_a_starlark_failure_shows_the_line_that_says_what_broke(qapp):
    """A Starlark failure arrives as a traceback whose first line is "Traceback (most
    recent call last):" — true of every failure, so it identifies none of them."""
    reason = ("StarlarkActionError: Traceback (most recent call last):\n"
              "  * action.star#call:1, in <module>\n"
              "      run(pack)\n"
              "error: Variable `run` not found, did you mean `True`?")
    assert _error_sliver(reason) == "error: Variable `run` not found, did you mean `True`?"


def test_an_authors_own_reason_comes_through_as_written(qapp):
    assert _error_sliver("no datapack called 'imaginary'") == "no datapack called 'imaginary'"


def test_a_long_reason_is_cut_short(qapp):
    sliver = _error_sliver("x" * 200)
    assert len(sliver) <= 64 and sliver.endswith("…")


@pytest.mark.parametrize("reason", [None, "", "   \n  "])
def test_no_reason_is_not_an_empty_dash(qapp, reason):
    assert _error_sliver(reason) == ""


# --- the strip itself ----------------------------------------------------------------

class FakeHistory:
    def __init__(self, rows):
        self.rows = list(rows)

    def list(self):
        return self.rows


def run_row(run_id=9, name="Removal", status="success"):
    return {"id": run_id, "job_name": name, "status": status, "finished_at": ""}


def step_row(run_id=9, status="success", reason=None, position=0):
    return {"id": 100 + position, "job_run_id": run_id, "action_ref": "p:a",
            "status": status, "reason": reason, "position_in_run": position,
            "rollback_data": None, "finished_at": ""}


def strip(steps, runs):
    return JobRunsView(FakeHistory(steps), FakeHistory(runs))


def test_a_step_row_is_a_leaf(qapp):
    """The reason used to hang off the step as wrapped child rows that auto-expanded. It is
    in the report now, in full, beside what the step managed to change before it stopped."""
    view = strip(
        [{"id": 1, "job_run_id": 9, "action_ref": "p:a", "status": "failed",
          "reason": "error: Variable `run` not found", "position_in_run": 0,
          "rollback_data": rollback("added"), "finished_at": "2026-08-17T12:00:00"}],
        [{"id": 9, "job_name": "Removal", "status": "failed",
          "finished_at": "2026-08-17T12:00:00"}])

    run = view._tree.topLevelItem(0)
    assert run.childCount() == 1, "a run shows its steps"
    assert run.child(0).childCount() == 0, "and a step shows nothing beneath it"


def test_a_failed_step_wears_a_sliver_of_why(qapp):
    view = strip(
        [{"id": 1, "job_run_id": 9, "action_ref": "p:a", "status": "failed",
          "reason": "error: Variable `run` not found", "position_in_run": 0,
          "rollback_data": None, "finished_at": ""}],
        [{"id": 9, "job_name": "Removal", "status": "failed", "finished_at": ""}])

    step = view._tree.topLevelItem(0).child(0)
    assert step.text(1).startswith("failed — error: Variable")
    assert step.toolTip(1) == "error: Variable `run` not found", "the whole reason survives"


def test_double_clicking_a_run_asks_for_its_report(qapp):
    """The row IS the way through — which is what lets everything else here be brief."""
    view = strip([], [{"id": 42, "job_name": "Removal", "status": "success",
                       "finished_at": ""}])
    asked = []
    view.report_requested.connect(asked.append)

    view._on_activated(view._tree.topLevelItem(0))

    assert asked == [42]


def test_a_step_row_does_not_ask_for_a_report(qapp):
    """Only the run carries an id. A step double-click landing on the run's report by
    accident would be luck, not design."""
    view = strip(
        [{"id": 1, "job_run_id": 9, "action_ref": "p:a", "status": "success",
          "position_in_run": 0, "rollback_data": None, "finished_at": ""}],
        [{"id": 9, "job_name": "Removal", "status": "success", "finished_at": ""}])
    asked = []
    view.report_requested.connect(asked.append)

    view._on_activated(view._tree.topLevelItem(0).child(0))

    assert asked == []


# --- what stays open ------------------------------------------------------------------

def test_a_run_arrives_collapsed(qapp):
    """Pressing play refreshes the whole strip, so a run that arrives expanded pushes
    everything below it down — and it did that to every run at once."""
    view = strip([step_row()], [run_row()])
    assert view._tree.topLevelItem(0).isExpanded() is False


def test_opening_a_run_survives_the_next_refresh(qapp):
    """The reported annoyance: a refresh happens on every run, and it used to throw away
    whatever you had opened."""
    view = strip([step_row()], [run_row()])
    view._tree.topLevelItem(0).setExpanded(True)

    view.refresh()

    assert view._tree.topLevelItem(0).isExpanded() is True


def test_closing_a_run_also_survives(qapp):
    """The memory has to hold both answers. Recording only 'expanded' would make a run you
    deliberately closed spring open again the moment it was reopened once."""
    view = strip([step_row()], [run_row()])
    item = view._tree.topLevelItem(0)
    item.setExpanded(True)
    item.setExpanded(False)

    view.refresh()

    assert view._tree.topLevelItem(0).isExpanded() is False


def test_a_new_run_does_not_disturb_an_open_one(qapp):
    """The actual shape of the complaint — running a job reset every other row."""
    view = strip([step_row(run_id=9)], [run_row(9)])
    view._tree.topLevelItem(0).setExpanded(True)

    view._job_history.rows.insert(0, run_row(10, name="Second"))
    view._history.rows.append(step_row(run_id=10))
    view.refresh()

    rows = {view._tree.topLevelItem(i).text(0): view._tree.topLevelItem(i)
            for i in range(view._tree.topLevelItemCount())}
    assert rows["Second"].isExpanded() is False, "the new one arrives collapsed"
    assert rows["Removal"].isExpanded() is True, "and the one you opened stays open"


def test_restoring_state_does_not_count_as_the_user_setting_it(qapp):
    """Qt fires `itemExpanded` for a programmatic `setExpanded` too. Without the guard a
    refresh would re-record what it just applied — including recording the *default* for a
    run nobody has touched, which quietly makes the default unchangeable."""
    view = strip([step_row()], [run_row()])
    assert view._expanded == {}, "a run nobody has touched has no remembered state"

    view.refresh()
    assert view._expanded == {}

    view._tree.topLevelItem(0).setExpanded(True)
    assert view._expanded == {9: True}, "a real interaction is recorded"


# --- the log, now part of the report -------------------------------------------------

class Step:
    def __init__(self, log=(), ref="p:a", status="success"):
        self.action_ref, self.status, self.reason = ref, status, None
        self.changes, self.run_id, self.log_lines = [], 1, list(log)


class Result:
    def __init__(self, steps, dry_run=False):
        self.job_name, self.status = "Removal Pass", "success"
        self.step_results, self.dry_run, self.not_run = steps, dry_run, 0


def test_a_fresh_run_carries_what_it_said(qapp):
    report = reports.from_result(Result([Step(log=[("info", "removed 12")])]))
    assert report.log_lines() == [("info", "removed 12")]


def test_the_log_reads_as_one_run_in_step_order(qapp):
    """Flattened rather than kept per step: a run reads as one narrative, and the step
    boundaries are already visible in the table above it."""
    report = reports.from_result(Result([
        Step(log=[("info", "first")]), Step(log=[]), Step(log=[("warning", "third")])]))
    assert report.log_lines() == [("info", "first"), ("warning", "third")]


def test_a_recorded_run_still_has_its_log(qapp):
    """The case that would rot unnoticed. The strip's live tail is gone the moment anything
    else logs, so if `from_history` dropped this the log would appear to work all session
    and be empty tomorrow."""
    report = reports.from_history(
        {"id": 7, "job_name": "Removal Pass", "status": "success"},
        [{"id": 1, "action_ref": "p:a", "status": "success", "rollback_data": "{}",
          "log_output": json.dumps([["info", "removed 12"], ["error", "and stopped"]])}])

    assert report.log_lines() == [("info", "removed 12"), ("error", "and stopped")]


@pytest.mark.parametrize("stored", [None, "", "{not json", "[[1]]"])
def test_an_unreadable_log_does_not_take_the_report_down(qapp, stored):
    report = reports.from_history(
        {"id": 1, "job_name": "Odd", "status": "failed"},
        [{"id": 1, "action_ref": "p:a", "status": "failed", "rollback_data": "{}",
          "log_output": stored}])
    assert isinstance(report.log_lines(), list)


def test_a_dry_run_shows_its_log_too(qapp):
    """The reason this is worth having beyond errors: a dry run is how you test an action
    you are writing, and `pack.log` is the only output an action has."""
    report = reports.from_result(Result([Step(log=[("debug", "would remove quark:rope")])],
                                        dry_run=True))
    assert report.log_lines() == [("debug", "would remove quark:rope")]


def test_the_report_tab_shows_the_log(qapp):
    from PySide6.QtWidgets import QPlainTextEdit
    from packsmith.gui.run_report import RunReportTab

    tab = RunReportTab(reports.from_result(Result([Step(log=[("info", "removed 12")])])))
    panes = tab.findChildren(QPlainTextEdit)

    assert len(panes) == 1
    assert panes[0].toPlainText() == "[info] removed 12"
    assert panes[0].isReadOnly()


def test_a_silent_run_grows_no_log_section(qapp):
    """An empty pane reads as something failing to load. Most actions say nothing."""
    from PySide6.QtWidgets import QPlainTextEdit
    from packsmith.gui.run_report import RunReportTab

    tab = RunReportTab(reports.from_result(Result([Step(log=[])])))
    assert tab.findChildren(QPlainTextEdit) == []
