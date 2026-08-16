"""The Run Report — what a run did, or would do (design 3.3, 4.1).

The interesting half is `core/reports`, which is why it lives outside the widget: a report
assembled inside a Qt class could only be tested by building a window, and the questions
worth asking — which changes belong to which step, what a run read back from history still
remembers — are plain data.

What is tested here is what would be **quietly wrong**: a report that describes the wrong
run, counts no-ops as changes, or loses a rolled-back run's record. Layout is not — a
mis-sized column announces itself the moment you look at the tab.
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core import reports
from packsmith.core.job_runner import describe_summary


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def change(engine="tag", kind="added", **extra):
    base = {"engine": engine, "kind": kind, "before": None,
            "after": {"value": True, "owner": "action", "action_ref": "p:a"}}
    base.update(extra)
    return base


class Step:
    def __init__(self, ref="p:a", status="success", reason=None, changes=(), run_id=1):
        self.action_ref, self.status, self.reason = ref, status, reason
        self.changes = list(changes)
        self.run_id = run_id


class Result:
    def __init__(self, steps, *, status="success", dry_run=False, not_run=0):
        self.job_name, self.status = "Removal Pass", status
        self.step_results, self.dry_run, self.not_run = steps, dry_run, not_run


# --- the summary ---------------------------------------------------------------------------

def test_no_ops_are_listed_but_not_counted(qapp):
    """A re-run of an applied job still reports every cell the action asserted — that is
    what lets it say "197 already removed, 3 newly removed" — but the headline must not
    claim 200 edits to a pack that did not move."""
    report = reports.from_result(Result([Step(changes=[
        change(entry_id="a", tag="remove"),
        change(entry_id="b", tag="remove", kind="unchanged"),
        change(entry_id="c", tag="remove", kind="unchanged"),
    ])]))

    assert len(report.by_engine("tag")) == 3
    assert report.summary() == {"tag": 1}
    assert describe_summary(report.summary()) == "1 tag"


def test_a_run_that_changes_nothing_says_so(qapp):
    report = reports.from_result(Result([Step(changes=[
        change(entry_id="a", tag="remove", kind="unchanged")])]))

    assert describe_summary(report.summary()) == "nothing"


def test_changes_keep_step_order(qapp):
    report = reports.from_result(Result([
        Step("p:first", changes=[change(entry_id="a", tag="t")]),
        Step("p:second", changes=[change(entry_id="b", tag="t")]),
    ]))
    assert [c["entry_id"] for c in report.changes] == ["a", "b"]


# --- blueprints group by instance ----------------------------------------------------------

def test_blueprint_changes_group_by_instance(qapp):
    """§3.2.2 makes gaps the point of a blueprint, so the unit that means something is the
    instance — eight flat rows saying "granite gained a slot" is the same information with
    the meaning taken out."""
    report = reports.from_result(Result([Step(changes=[
        change("blueprint", blueprint="StoneType", instance="granite", slot="a"),
        change("blueprint", kind="created", blueprint="StoneType", instance="tuff",
               slot=None),
        change("blueprint", blueprint="StoneType", instance="granite", slot="b"),
        change("blueprint", blueprint="StoneType", instance="tuff", slot="a"),
    ])]))

    groups = report.blueprint_groups()
    assert [key for key, _ in groups] == [("StoneType", "granite"), ("StoneType", "tuff")]
    assert [len(g) for _, g in groups] == [2, 2]


def test_an_instance_headline_counts_gaps_filled_not_slots_touched(qapp):
    """§3.2.2 makes filling gaps the number that means something, so "+N bound" has to
    mean gaps actually filled. Counting anything with a value was wrong twice: a `claimed`
    binding was already bound and only changed hands, and a `changed` one was rebound
    rather than filled — so a run that transferred ownership of two slots and rebound a
    third reported "+3 bound" to a blueprint whose gap count had not moved."""
    from packsmith.gui.run_report import _instance_headline

    group = [
        change("blueprint", kind="added", slot="a"),
        change("blueprint", kind="claimed", slot="b"),
        change("blueprint", kind="changed", slot="c"),
        change("blueprint", kind="removed", slot="d"),
        change("blueprint", kind="unchanged", slot="e"),
    ]
    headline = _instance_headline(group)

    assert "+1 bound" in headline, headline
    assert "1 rebound" in headline and "1 claimed" in headline
    assert "−1 unbound" in headline
    assert "+3" not in headline and "+5" not in headline


def test_a_created_instance_says_so(qapp):
    from packsmith.gui.run_report import _instance_headline

    headline = _instance_headline([
        change("blueprint", kind="created", slot=None),
        change("blueprint", kind="added", slot="base"),
    ])
    assert headline.startswith("new instance")
    assert "+1 bound" in headline


# --- read back out of history ----------------------------------------------------------

def test_a_recorded_run_rebuilds_into_the_same_report(qapp):
    """One shape, two adapters. The tab must not learn that a historical run is a different
    kind of thing, or it grows a branch that only one of them exercises."""
    stored = json.dumps({"l2": [], "files": {}, "blueprints": [],
                         "changes": [change(entry_id="a", tag="remove")]})
    report = reports.from_history(
        {"id": 7, "job_name": "Removal Pass", "status": "success",
         "finished_at": "2026-08-15T10:00:00"},
        [{"action_ref": "p:a", "status": "success", "reason": None,
          "rollback_data": stored}])

    assert report.job_name == "Removal Pass"
    assert report.dry_run is False, "a dry run is never recorded, so it can never come back"
    assert report.summary() == {"tag": 1}
    assert report.key == ("run", 7)


def test_a_run_recorded_before_change_records_existed_still_opens(qapp):
    """`changes` was added inside `rollback_data` so recording it needed no migration —
    which means older rows simply do not have it. An empty report of an old run is honest;
    refusing to open it is not."""
    report = reports.from_history(
        {"id": 1, "job_name": "Old", "status": "success"},
        [{"action_ref": "p:a", "status": "success",
          "rollback_data": json.dumps({"l2": [], "files": {}, "blueprints": []})}])

    assert report.steps[0].changes == []
    assert report.summary() == {}


def test_unreadable_rollback_data_does_not_take_the_report_down(qapp):
    report = reports.from_history({"id": 1, "job_name": "Odd", "status": "failed"},
                                  [{"action_ref": "p:a", "status": "failed",
                                    "rollback_data": "{not json"}])
    assert report.steps[0].changes == []


# --- what may be rolled back ---------------------------------------------------------------

def test_a_dry_run_offers_no_rollback(qapp):
    """There is nothing to undo, and offering it would imply there was."""
    report = reports.from_result(Result([Step(changes=[change(entry_id="a", tag="t")])],
                                        dry_run=True))
    assert not any(step.can_roll_back for step in report.steps)


def test_an_already_undone_step_still_shows_what_it_did(qapp):
    """`mark_rolled_back` spends the inverse and keeps the change record, so the report of
    a reversed run still says what happened — history is a record, and undoing a step does
    not unhappen it — while the step itself can no longer be undone twice."""
    spent = json.dumps({"l2": [], "files": {}, "blueprints": [],
                        "changes": [change(entry_id="a", tag="remove")]})
    report = reports.from_history(
        {"id": 3, "job_name": "Removal Pass", "status": "success"},
        [{"id": 9, "action_ref": "p:a", "status": "rolled_back", "rollback_data": spent}])

    step = report.steps[0]
    assert step.changes, "the record of what it did was lost with the inverse"
    assert not step.can_roll_back


def test_a_step_with_its_inverse_intact_can_be_rolled_back(qapp):
    live = json.dumps({"l2": [{"key": ["r", "e", "t"], "existed": False}],
                       "files": {}, "blueprints": [], "changes": []})
    report = reports.from_history(
        {"id": 3, "job_name": "Removal Pass", "status": "success"},
        [{"id": 9, "action_ref": "p:a", "status": "success", "rollback_data": live}])

    assert report.steps[0].can_roll_back
    assert report.steps[0].run_id == 9


# --- the table model ---------------------------------------------------------------------

def test_the_filter_narrows_rows_without_losing_the_total(qapp):
    """A removal job over `minecraft:item` stages tens of thousands of cells, so the count
    beside the box has to keep saying how many there were — "12 of 18,638" is the sentence
    that makes a filtered view legible."""
    from packsmith.gui.run_report import ChangeModel, TAG_COLUMNS

    model = ChangeModel([change(entry_id="quark:rope", tag="remove"),
                         change(entry_id="minecraft:stone", tag="remove"),
                         change(entry_id="quark:torch", tag="remove")], TAG_COLUMNS)
    assert model.rowCount() == 3

    model.set_filter("quark")
    assert model.shown == 2 and model.total == 3
    assert model.rowCount() == 2

    model.set_filter("")
    assert model.rowCount() == 3


def test_a_double_click_opens_the_row_you_clicked(qapp):
    """`at(row)` has to index the FILTERED list. Reaching for the unfiltered one is silent
    and plausible — the diff opens, it is just of a different file — and it only misbehaves
    once someone types in the box, which is exactly when they are looking for one file
    among hundreds."""
    from packsmith.gui.run_report import ChangeModel, FILE_COLUMNS

    def written(path):
        return change("file", path=path,
                      after={"value": "{}", "owner": "action", "action_ref": "p:a"})

    model = ChangeModel([written("a.json"), written("b.json"), written("c.json")],
                        FILE_COLUMNS)
    model.set_filter("c.json")

    assert model.rowCount() == 1
    assert model.at(0)["path"] == "c.json"


def test_the_filter_searches_every_column(qapp):
    """Filtering only the first column would make "show me everything claimed" impossible,
    and that is exactly the question ownership transfer creates."""
    from packsmith.gui.run_report import ChangeModel, TAG_COLUMNS

    model = ChangeModel([change(entry_id="a", tag="remove", kind="claimed"),
                         change(entry_id="b", tag="remove")], TAG_COLUMNS)
    model.set_filter("claimed")
    assert model.shown == 1
