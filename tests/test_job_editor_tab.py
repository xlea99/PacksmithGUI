"""Opening a job in its editor tab.

Written because a crash shipped here that 885 tests did not catch: nothing constructed a
`JobEditorTab`, so every change to `refresh()` was verified only by reading it. The tab is
the main way a job is looked at, and "does it open at all" is the cheapest possible check.
"""
import os

import pytest
from PySide6.QtCore import Qt

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.bindings import binding_id, record_names
from packsmith.core.blueprints import BlueprintStore
from packsmith.core.jobs import JobStore
from packsmith.core.packages import ActionManifest, MappingSlot
from packsmith.core.tags import TagStore
from packsmith.gui.job_editor import JobEditorTab
from packsmith.gui.shell import style

REG = "minecraft:item"

MANIFEST = ActionManifest(
    package_name="removal", action_id="nuke", file="n.star", function="run",
    mappings={"target": MappingSlot(name="target", kind="tag", tag_type="bool",
                                    registry_type=REG)})


class Index:
    actions = {"removal:nuke": MANIFEST}

    def get(self, ref):
        return {"removal:nuke": MANIFEST}[ref]


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    yield app


@pytest.fixture
def world(user_db):
    tags = TagStore(user_db)
    tags.define(REG, "remove", "bool")
    return tags, JobStore(user_db), BlueprintStore(user_db)


def open_tab(job, world):
    tags, jobs, blueprints = world
    return JobEditorTab(job, job_store=jobs, package_index=Index(), tag_store=tags,
                        blueprint_store=blueprints, packdump=None)


def step_colour(tab, row=0):
    return tab._tree.topLevelItem(row).foreground(1).color().name().lower()


def test_a_job_tab_opens(world):
    """The crash: `refresh()` read a problems map that was never built."""
    tags, jobs, _ = world
    job = jobs.create("nightly")
    bindings = {"target": binding_id(MANIFEST.mappings["target"], "remove", tag_store=tags)}
    jobs.add_action_step(job.id, "removal:nuke", bindings=bindings,
                         bound_names=record_names(MANIFEST, bindings, tag_store=tags))

    tab = open_tab(jobs.get(job.id), world)
    assert tab._tree.topLevelItemCount() == 1


def test_an_empty_job_opens(world):
    _, jobs, _ = world
    tab = open_tab(jobs.create("empty"), world)
    assert tab._tree.topLevelItemCount() == 0


def test_a_healthy_step_is_not_flagged(world):
    tags, jobs, _ = world
    job = jobs.create("nightly")
    bindings = {"target": binding_id(MANIFEST.mappings["target"], "remove", tag_store=tags)}
    jobs.add_action_step(job.id, "removal:nuke", bindings=bindings,
                         bound_names=record_names(MANIFEST, bindings, tag_store=tags))

    tab = open_tab(jobs.get(job.id), world)
    assert "⚠" not in tab._tree.topLevelItem(0).text(1)


def test_a_renamed_tag_flags_its_step_amber(world):
    """Amber, not red: a relink fixes it (design 3.2.1)."""
    tags, jobs, _ = world
    job = jobs.create("nightly")
    bindings = {"target": binding_id(MANIFEST.mappings["target"], "remove", tag_store=tags)}
    jobs.add_action_step(job.id, "removal:nuke", bindings=bindings,
                         bound_names=record_names(MANIFEST, bindings, tag_store=tags))
    tags.rename(REG, "remove", "cull")

    tab = open_tab(jobs.get(job.id), world)
    assert "⚠" in tab._tree.topLevelItem(0).text(1)
    assert step_colour(tab) == style.WARNING.lower()
    assert "was bound to 'remove'" in tab._tree.topLevelItem(0).toolTip(1)


def test_a_broken_binding_flags_its_step_red(world):
    """Red, because no amount of confirming fixes a binding that resolves to nothing."""
    tags, jobs, _ = world
    job = jobs.create("legacy")
    jobs.add_action_step(job.id, "removal:nuke", bindings={"target": "remove"})
    tags.rename(REG, "remove", "cull")

    tab = open_tab(jobs.get(job.id), world)
    assert step_colour(tab) == style.ERROR.lower()


def test_a_tab_still_opens_when_the_readiness_check_explodes(world, monkeypatch):
    """A cosmetic check must never be the reason a tab won't open — which is exactly the
    failure this file exists because of."""
    import packsmith.gui.job_editor as module

    tags, jobs, _ = world
    job = jobs.create("nightly")
    jobs.add_action_step(job.id, "removal:nuke", bindings={})
    monkeypatch.setattr(module, "step_problems",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    tab = open_tab(jobs.get(job.id), world)
    assert tab._tree.topLevelItemCount() == 1


# --- a tab that is already open must not go stale ---------------------------------------

def test_an_open_tab_flags_the_step_after_the_tag_is_deleted(world):
    """The reported gap: the tab was open when the tag went away, so it kept rendering a
    step that had just become unrunnable. Opening it fresh was fine — which is exactly why
    it survived: every check looked at a newly built tab."""
    tags, jobs, _ = world
    job = jobs.create("Obliterate Items")
    jobs.add_action_step(job.id, "removal:nuke", bindings={"target": "remove"})

    tab = open_tab(jobs.get(job.id), world)
    assert "⚠" not in tab._tree.topLevelItem(0).text(1), "flagged before anything was wrong"

    tags.undefine(REG, "remove")
    tab.refresh()                      # what _refresh_after_tag_change() now triggers

    item = tab._tree.topLevelItem(0)
    assert "⚠" in item.text(1), "an open tab kept showing a broken step as fine"
    assert step_colour(tab) == style.ERROR.lower()
    assert "'remove'" in item.toolTip(1)


def test_an_open_tab_clears_the_flag_when_the_tag_comes_back(world):
    """Redefining is a real fix, and the tab has to agree — a warning that never clears
    teaches you to ignore warnings."""
    tags, jobs, _ = world
    job = jobs.create("Obliterate Items")
    jobs.add_action_step(job.id, "removal:nuke", bindings={"target": "remove"})
    tab = open_tab(jobs.get(job.id), world)

    tags.undefine(REG, "remove")
    tab.refresh()
    assert "⚠" in tab._tree.topLevelItem(0).text(1)

    tags.define(REG, "remove", "bool")
    tab.refresh()
    assert "⚠" not in tab._tree.topLevelItem(0).text(1)
