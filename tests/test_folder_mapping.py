"""Binding a step to a tracked folder — §6.6's second consent, in the step editor.

`kind = "folder"` was validated in `resolve_step` and guessed at in `best_guess_bindings`
from the day tracked roots landed, and `StepForm` was even handed a `file_roots` to pick
from. Nothing ever used it. The form has no default branch, so a folder slot fell through
to the TAG picker and rendered as a disabled dropdown reading "no  tags on None" — a slot
with no registry type, described by a widget that only knows how to describe tags.

That is a shape of bug worth a permanent test: the action was correct, the core was
correct, and the only way to find out was to build a job around it and look.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.bindings import best_guess_bindings, binding_name
from packsmith.core.db import UserDB
from packsmith.core.jobs import JobStep
from packsmith.core.packages import ActionManifest, MappingSlot
from packsmith.core.roots import FileRoots


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def slot(**kw):
    base = dict(name="target", kind="folder", access="write", required=True)
    base.update(kw)
    return MappingSlot(**base)


def manifest(target):
    return ActionManifest(package_name="p", action_id="a", file="a.star", function="run",
                          mappings={"target": target})


@pytest.fixture
def roots(tmp_path, qapp):
    instance = tmp_path / "instance"
    instance.mkdir()
    for name in ("assets_mod", "tweaks"):
        (tmp_path / name).mkdir()
    db = UserDB(tmp_path / "p.db")
    made = FileRoots(db, instance, protected=[tmp_path / "userdata"])
    made.add("assets_mod", tmp_path / "assets_mod")
    made.add("tweaks", tmp_path / "tweaks")
    yield made
    db.close()


def form_for(target, roots, bound=None):
    from packsmith.gui.job_editor import StepForm

    step = JobStep(id=1, job_id=1, position=0, kind="action", action_ref="p:a",
                   bindings={"target": bound} if bound else {})
    return StepForm(manifest(target), None, step, file_roots=roots)


def combo(form):
    return form._mapping_widgets["target"]


def options(form):
    box = combo(form)
    return [box.itemData(i) for i in range(box.count())]


# --- the bug ---------------------------------------------------------------------------

def test_a_folder_slot_gets_a_usable_picker(roots):
    """It used to render as the tag picker's empty state — disabled, and describing a
    registry the slot does not have."""
    form = form_for(slot(), roots)

    assert combo(form).isEnabled(), "the picker was disabled with nothing to pick"
    assert "assets_mod" in options(form)
    assert "tweaks" in options(form)


def test_the_instance_is_not_offered(roots):
    """Unqualified paths already mean the instance (§6.6), so an action wanting it would
    not have declared a folder mapping. Offering `minecraft` invites binding generated
    output at the pack instead of at the repo — the one wrong answer that looks right."""
    assert "minecraft" not in options(form_for(slot(), roots))


def test_with_no_tracked_folders_it_says_where_to_add_one(tmp_path, qapp):
    """A disabled dropdown that says nothing is how the original bug read. Empty is a real
    state here — a fresh profile has only the instance — so it has to give an instruction."""
    instance = tmp_path / "i"
    instance.mkdir()
    db = UserDB(tmp_path / "p.db")
    try:
        form = form_for(slot(), FileRoots(db, instance, protected=[]))
        box = combo(form)
        assert not box.isEnabled()
        assert "Tracked Folders" in box.itemText(box.count() - 1)
    finally:
        db.close()


# --- what gets stored ------------------------------------------------------------------

def test_the_stored_value_is_the_root_NAME(roots):
    """`resolve_step` checks against `file_roots.names()` and
    `pack.filesystem.resolve(root=…)` takes a name, so an id would have to be translated
    twice and could disagree once."""
    form = form_for(slot(), roots)
    box = combo(form)
    box.setCurrentIndex(options(form).index("assets_mod"))

    bindings, _config, _on_error = form.read()
    assert bindings["target"] == "assets_mod"


def test_an_existing_binding_comes_back_selected(roots):
    form = form_for(slot(), roots, bound="tweaks")
    assert combo(form).currentData() == "tweaks"


def test_a_bound_folder_displays_as_itself(roots):
    """`binding_name` reached the right answer through a branch labelled "legacy name,
    written before ids". A folder is bound by name BY DESIGN, and the two should not be
    confused — the legacy branch is a candidate for deletion one day."""
    assert binding_name(slot(), "tweaks") == "tweaks"


# --- the guess -------------------------------------------------------------------------

def test_the_instance_is_never_guessed(roots):
    """`minecraft` fits every folder slot and so would always win the guess, quietly
    aiming generated output at the pack rather than the repo the author meant."""
    guessed = best_guess_bindings(manifest(slot()), tag_store=None, file_roots=roots)
    assert guessed["target"] != "minecraft"


def test_likely_name_wins_when_it_exists(roots):
    guessed = best_guess_bindings(manifest(slot(likely_name="assets_mod")),
                                  tag_store=None, file_roots=roots)
    assert guessed["target"] == "assets_mod"


# --- the chain, not just the leaf --------------------------------------------------------
#
# Every test above builds a `StepForm` directly, and all eight passed while the picker was
# still empty in the running app. The break was one link up: `JobEditorTab` holds
# `file_roots`, hands it to `best_guess_bindings` and `step_problems`, and constructed its
# `StepPanel` without it. So the guess chose the right folder and the picker it was shown in
# had nothing to offer — a binding that was correct and unselectable at once.
#
# Testing a widget in isolation cannot see that. This walks the real chain.

class FolderIndex:
    MANIFEST = None

    def __init__(self, target):
        FolderIndex.MANIFEST = manifest(target)
        self.actions = {"p:a": FolderIndex.MANIFEST}

    def get(self, ref):
        return self.actions[ref]


def open_tab(roots, tmp_path):
    from packsmith.core.jobs import JobStore
    from packsmith.core.tags import TagStore
    from packsmith.gui.job_editor import JobEditorTab

    db = roots._db
    jobs = JobStore(db)
    job = jobs.create("gen")
    step = jobs.add_action_step(job.id, "p:a", bindings={})
    tab = JobEditorTab(jobs.get(job.id), job_store=jobs, package_index=FolderIndex(slot()),
                       tag_store=TagStore(db), blueprint_store=None, packdump=None,
                       file_roots=roots)
    tab._select_step(step.id)
    return tab


def test_the_picker_inside_a_real_job_tab_sees_the_tracked_folders(roots, tmp_path):
    """The bug as reported: a folder was tracked and the dropdown still said there were
    none, because the roots stopped one constructor short of the widget that needed them."""
    tab = open_tab(roots, tmp_path)
    try:
        box = tab._panel._form._mapping_widgets["target"]
        values = [box.itemData(i) for i in range(box.count())]

        assert box.isEnabled(), "the picker is disabled despite two tracked folders existing"
        assert "assets_mod" in values and "tweaks" in values, values
    finally:
        tab.deleteLater()
