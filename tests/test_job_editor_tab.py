"""Opening a job in its editor tab.

Written because a crash shipped here that 885 tests did not catch: nothing constructed a
`JobEditorTab`, so every change to `refresh()` was verified only by reading it. The tab is
the main way a job is looked at, and "does it open at all" is the cheapest possible check.
"""
import os

import pytest
from PySide6.QtCore import Qt

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.bindings import binding_id, record_names, stale_bindings
from packsmith.core.blueprints import BlueprintStore
from packsmith.core.jobs import JobStore
from packsmith.core.packages import ActionManifest, ConfigParam, MappingSlot
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


# --- muting and duplicating, from the tab -----------------------------------------------

def add_step(jobs, job_id, tags):
    bindings = {"target": binding_id(MANIFEST.mappings["target"], "remove", tag_store=tags)}
    return jobs.add_action_step(job_id, "removal:nuke", bindings=bindings,
                                bound_names=record_names(MANIFEST, bindings, tag_store=tags))


def test_unticking_a_step_mutes_it(world, qapp):
    """The checkbox has to reach the database, not just the pixels.

    It rides on `itemChanged`, which is also what a *rebuild* emits — so the danger is the
    opposite of a dead control: a refresh re-firing the signal for every row it draws.
    """
    tags, jobs, _ = world
    job = jobs.create("nightly")
    first = add_step(jobs, job.id, tags)
    second = add_step(jobs, job.id, tags)

    tab = open_tab(jobs.get(job.id), world)
    tab._tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)
    qapp.processEvents()               # let the posted rebuild run

    stored = {s.id: s.enabled for s in jobs.get(job.id).steps}
    assert stored == {first.id: False, second.id: True}, "the refresh re-fired the toggle"
    assert tab._tree.topLevelItem(0).checkState(0) == Qt.Unchecked
    assert tab._tree.topLevelItem(1).checkState(0) == Qt.Checked
    assert tab._tree.topLevelItem(0).font(1).strikeOut(), "the rebuild never happened"


def test_muting_does_not_destroy_the_item_mid_signal(world):
    """The crash this shipped with, caught deterministically.

    `refresh()` clears the tree, destroying every item in it. Called straight from
    `itemChanged`, one of those is the item Qt is *still delivering for* — it returns into
    `QTreeModel` code that keeps using the pointer. That is a use-after-free, and a
    use-after-free only crashes once the freed block has been reused: the click survived
    every probe here and killed the real application with an access violation.

    So the assertion is not "does it crash" — that question is unreliable by construction.
    It is the deterministic one underneath: does the item outlive the slot?
    """
    import shiboken6

    tags, jobs, _ = world
    job = jobs.create("nightly")
    add_step(jobs, job.id, tags)
    add_step(jobs, job.id, tags)
    tab = open_tab(jobs.get(job.id), world)

    verdict = {}
    slot = tab._on_item_changed

    def watched(item, column):
        slot(item, column)
        verdict["alive"] = shiboken6.isValid(item)

    tab._tree.itemChanged.disconnect()
    tab._tree.itemChanged.connect(watched)
    tab._tree.topLevelItem(1).setCheckState(0, Qt.Unchecked)

    assert verdict["alive"] is True, (
        "the tree was rebuilt inside itemChanged, destroying the item Qt still holds")


def test_a_muted_step_reads_as_muted(world):
    tags, jobs, _ = world
    job = jobs.create("nightly")
    step = add_step(jobs, job.id, tags)
    jobs.set_step_enabled(step.id, False)

    tab = open_tab(jobs.get(job.id), world)

    item = tab._tree.topLevelItem(0)
    assert item.checkState(0) == Qt.Unchecked
    assert item.font(1).strikeOut()


def test_duplicating_a_step_copies_it_and_lands_beneath_the_original(world):
    """Placement is the point as much as the copy: a duplicate that appears eight rows
    away reads as a new step rather than as a copy of this one."""
    tags, jobs, _ = world
    job = jobs.create("nightly")
    first = add_step(jobs, job.id, tags)
    last = add_step(jobs, job.id, tags)

    tab = open_tab(jobs.get(job.id), world)
    tab._duplicate_step(first)

    steps = jobs.get(job.id).steps
    assert [s.id for s in steps] == [first.id, steps[1].id, last.id]
    assert steps[1].id not in (first.id, last.id)
    assert steps[1].bindings == first.bindings
    assert steps[1].bound_names == first.bound_names


def test_a_step_that_has_never_run_says_so(world):
    tags, jobs, _ = world
    job = jobs.create("nightly")
    add_step(jobs, job.id, tags)

    tab = open_tab(jobs.get(job.id), world)

    assert tab._tree.topLevelItem(0).text(4) == "never"


def menu_labels(tab, row):
    """Built rather than shown: `exec()` would block on a modal event loop."""
    return [a.text() for a in tab._menu_for(tab._tree.topLevelItem(row)).actions()
            if a.text()]


def test_the_row_menu_offers_both_ways_to_run_part_of_a_job(world):
    """Two gestures, two questions — and the labels have to say which is which.

    "Up to here" never settles whether *this* step is included, so the range is stated
    outright. Dry before Run in both pairs: the safe one is the one you want while
    iterating, and its destructive twin at the top of a much-used menu is a mis-click away.
    """
    tags, jobs, _ = world
    job = jobs.create("nightly")
    for _ in range(3):
        add_step(jobs, job.id, tags)
    tab = open_tab(jobs.get(job.id), world)

    labels = menu_labels(tab, 2)          # the third step

    assert labels[1:5] == ["Dry run steps 1–3", "Dry run step 3 only",
                           "Run steps 1–3", "Run step 3 only"]


def test_the_first_step_offers_no_range(world):
    """"Steps 1–1" is the same run as "step 1 only", and a menu that offers one thing
    twice under two names makes you stop and work out which you meant."""
    tags, jobs, _ = world
    job = jobs.create("nightly")
    add_step(jobs, job.id, tags)
    add_step(jobs, job.id, tags)
    tab = open_tab(jobs.get(job.id), world)

    labels = menu_labels(tab, 0)

    assert "Dry run step 1 only" in labels
    assert not any("steps 1–" in text for text in labels)


def test_a_muted_step_offers_no_range(world):
    """Running the sequence up to and including a step you switched off is a
    contradiction. Running it *alone* is not, so that one stays."""
    tags, jobs, _ = world
    job = jobs.create("nightly")
    add_step(jobs, job.id, tags)
    third = add_step(jobs, job.id, tags)
    jobs.set_step_enabled(third.id, False)
    tab = open_tab(jobs.get(job.id), world)

    labels = menu_labels(tab, 1)

    assert "Dry run step 2 only" in labels
    assert not any("steps 1–" in text for text in labels)


# --- the docked step panel ---------------------------------------------------------------

TWO_SLOT = ActionManifest(
    package_name="removal", action_id="pair", file="p.star", function="run",
    mappings={
        "target": MappingSlot(name="target", kind="tag", tag_type="bool",
                              registry_type=REG),
        "second": MappingSlot(name="second", kind="tag", tag_type="bool",
                              registry_type=REG, required=False),
    },
    config={"size": ConfigParam(name="size", type="number", default=8)})


class PairIndex:
    actions = {"removal:pair": TWO_SLOT}

    def get(self, ref):
        return TWO_SLOT


def open_pair_tab(world):
    tags, jobs, blueprints = world
    tags.define(REG, "other", "bool")
    job = jobs.create("pair job")
    bindings = {"target": binding_id(TWO_SLOT.mappings["target"], "remove", tag_store=tags)}
    step = jobs.add_action_step(job.id, "removal:pair", bindings=bindings,
                                bound_names=record_names(TWO_SLOT, bindings, tag_store=tags))
    tab = JobEditorTab(jobs.get(job.id), job_store=jobs, package_index=PairIndex(),
                       tag_store=tags, blueprint_store=blueprints, packdump=None)
    tab._select_step(step.id)
    return tab, jobs, job, step


def test_editing_in_the_panel_persists(world, qapp):
    """No OK button, so the wiring IS the save. If it broke, the panel would look like it
    worked and lose every edit — which is the whole reason a modal had a button."""
    tab, jobs, job, step = open_pair_tab(world)

    box = tab._panel._form._config_widgets["size"][0]
    box.setText("42")
    box.editingFinished.emit()
    qapp.processEvents()

    assert jobs.get(job.id).steps[0].config["size"] == 42


def test_a_value_the_action_cannot_take_is_refused_rather_than_stored(world, qapp):
    """It has to be refused *inline*: a modal per committed field would be unusable in a
    surface you edit continuously, and storing it would hand the runner nonsense."""
    tab, jobs, job, step = open_pair_tab(world)

    box = tab._panel._form._config_widgets["size"][0]
    box.setText("not a number")
    box.editingFinished.emit()
    qapp.processEvents()

    assert "size" not in jobs.get(job.id).steps[0].config
    assert "expects a number" in tab._panel._notice.text()


def test_the_form_is_not_rebuilt_under_the_cursor(world, qapp):
    """Every write refreshes the row, and the refresh reselects the step. If showing an
    already-shown step rebuilt its widgets, the control you just used would be destroyed
    mid-use — on every single edit."""
    tab, jobs, job, step = open_pair_tab(world)
    before = tab._panel._form

    box = tab._panel._form._config_widgets["size"][0]
    box.setText("12")
    box.editingFinished.emit()
    qapp.processEvents()

    assert tab._panel._form is before, "the panel rebuilt its form while you were editing"


def test_the_panel_keeps_its_step_across_a_refresh(world, qapp):
    """A refresh clears the tree, which drops the selection the panel is driven by. Runs
    finishing and tags being renamed elsewhere both trigger one, so without this the panel
    would empty itself out from under whatever you were configuring."""
    tab, jobs, job, step = open_pair_tab(world)
    assert tab._panel.step_id == step.id

    tab.refresh()
    qapp.processEvents()

    assert tab._panel.step_id == step.id
    assert tab._panel._form is not None


def test_editing_one_slot_does_not_settle_another_slots_relink(world, qapp):
    """The safety property, and the one thing this refactor genuinely changed.

    §3.2.1: a step bound to a renamed tag refuses to run until the user *confirms* it still
    means what they want. The dialog cleared that for every slot the moment you pressed OK,
    whether or not you had looked at the renamed one. With no OK button the assertion has
    to be deliberate — so touching an unrelated slot must leave the relink owed, or a step
    silently starts running again on the strength of an edit that said nothing about it.
    """
    tags, jobs, _ = world
    tab, jobs, job, step = open_pair_tab(world)
    tags.rename(REG, "remove", "cull")           # 'target' now owes a relink
    tab.refresh()
    qapp.processEvents()

    assert stale_bindings(jobs.get(job.id), package_index=PairIndex(), tag_store=tags)

    box = tab._panel._form._config_widgets["size"][0]   # an unrelated field
    box.setText("5")
    box.editingFinished.emit()
    qapp.processEvents()

    assert jobs.get(job.id).steps[0].config["size"] == 5, "the edit didn't save"
    assert stale_bindings(jobs.get(job.id), package_index=PairIndex(), tag_store=tags), \
        "editing an unrelated field silently settled the relink"


def test_the_relink_button_settles_it(world, qapp):
    """And the deliberate gesture does work — a warning that cannot be cleared is worse
    than no warning, because it teaches you to ignore the next one."""
    tags, jobs, _ = world
    tab, jobs, job, step = open_pair_tab(world)
    tags.rename(REG, "remove", "cull")
    tab.refresh()
    qapp.processEvents()

    assert tab._panel._relink.isVisibleTo(tab._panel)
    tab._panel._relink.click()
    qapp.processEvents()

    assert not stale_bindings(jobs.get(job.id), package_index=PairIndex(), tag_store=tags)


def test_view_action_info_leads_the_menu_and_actually_emits(world, qapp):
    """A menu item that emits nothing looks exactly like one that works.

    Ordering is asserted too, because the grouping is deliberate: read it, run it, change
    it — weakest consequence first, so the thing that only opens a page is nowhere near
    Remove.
    """
    tags, jobs, _ = world
    job = jobs.create("nightly")
    add_step(jobs, job.id, tags)
    tab = open_tab(jobs.get(job.id), world)

    seen = []
    tab.action_info_requested.connect(seen.append)
    menu = tab._menu_for(tab._tree.topLevelItem(0))
    labels = [a.text() for a in menu.actions() if a.text()]

    assert labels[0] == "View Action Info", "it should lead the menu"
    assert labels.index("View Action Info") < labels.index("Dry run step 1 only")
    assert labels.index("Run step 1 only") < labels.index("Remove")

    # Three groups, so two separators — and they have to fall in the right places or the
    # grouping is decoration rather than meaning.
    grouped = [[]]
    for entry in menu.actions():
        grouped.append([]) if entry.isSeparator() else grouped[-1].append(entry.text())
    assert grouped == [["View Action Info"],
                       ["Dry run step 1 only", "Run step 1 only"],
                       ["Duplicate", "Mute step", "Remove"]]

    next(a for a in menu.actions() if a.text() == "View Action Info").trigger()
    assert seen == ["removal:nuke"], "the menu item is wired to nothing"


def test_a_job_step_offers_no_action_info(world, qapp):
    """A job-reference step runs another job, so there is no action to have a page."""
    tags, jobs, _ = world
    inner = jobs.create("inner")
    outer = jobs.create("outer")
    jobs.add_job_step(outer.id, inner.id)
    tab = open_tab(jobs.get(outer.id), world)

    labels = [a.text() for a in tab._menu_for(tab._tree.topLevelItem(0)).actions()]

    assert "View Action Info" not in labels
