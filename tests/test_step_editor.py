"""The step editor's round-trip: open a bound step, press OK, change nothing.

This file exists because its absence let a data-loss regression ship. The id migration
(design 3.2.1) changed what a binding *is*, and the widgets kept comparing against names —
so every `many` binding rendered unchecked and OK saved the empty selection, and every tag
combo fell through to index 0, which is not "nothing" but *the alphabetically first tag*.

The invariant every test here asserts: **opening a dialog and accepting it is a no-op.**
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.bindings import binding_id
from packsmith.core.blueprints import BlueprintStore
from packsmith.core.jobs import JobStep
from packsmith.core.packages import ActionManifest, MappingSlot
from packsmith.gui.job_editor import StepForm

REG = "minecraft:item"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    yield app


class Dump:
    registry = {"minecraft:block": {"values": ["minecraft:granite", "minecraft:stone"]}}

    def attribute(self, *a):
        return None


@pytest.fixture
def world(tags, user_db):
    tags.define(REG, "remove", "bool")
    tags.define(REG, "aaa_first", "bool")        # sorts before "remove", on purpose
    bps = BlueprintStore(user_db)
    bps.define("StoneType")
    bps.add_slot("StoneType", "base_block", "registry", registry_type="minecraft:block")
    for name in ("granite", "andesite"):
        bps.create_instance("StoneType", name)
    return tags, bps


def _round_trip(manifest, bindings, tags, bps):
    step = JobStep(id=1, job_id=1, position=0, kind="action", action_ref="p:a",
                   bindings=dict(bindings))
    form = StepForm(manifest, tags, step, blueprint_store=bps, packdump=Dump())
    bindings, _config, _on_error = form.read()
    return bindings


def _manifest(**slots):
    return ActionManifest(package_name="p", action_id="a", file="a.star", function="run",
                          mappings=slots)


def test_a_many_binding_survives_open_and_ok(world):
    """The regression: every bound instance rendered unchecked, so OK deleted them all."""
    tags, bps = world
    slot = MappingSlot(name="stones", kind="blueprint_instance", cardinality="many")
    bound = [binding_id(slot, f"StoneType:{n}", blueprint_store=bps)
             for n in ("granite", "andesite")]
    assert _round_trip(_manifest(stones=slot), {"stones": bound}, tags, bps) == {
        "stones": bound}


def test_a_tag_binding_survives_open_and_ok(world):
    """It didn't just lose the binding — it retargeted the step to whatever sorted first."""
    tags, bps = world
    slot = MappingSlot(name="src", kind="tag", tag_type="bool", registry_type=REG)
    bound = binding_id(slot, "remove", tag_store=tags)
    assert _round_trip(_manifest(src=slot), {"src": bound}, tags, bps) == {"src": bound}


def test_a_blueprint_binding_survives_open_and_ok(world):
    tags, bps = world
    slot = MappingSlot(name="palette", kind="blueprint")
    bound = binding_id(slot, "StoneType", blueprint_store=bps)
    assert _round_trip(_manifest(palette=slot), {"palette": bound}, tags, bps) == {
        "palette": bound}


def test_a_registry_entry_binding_survives_open_and_ok(world):
    tags, bps = world
    slot = MappingSlot(name="anchor", kind="registry_entry",
                       registry_type="minecraft:block")
    assert _round_trip(_manifest(anchor=slot), {"anchor": "minecraft:granite"},
                       tags, bps) == {"anchor": "minecraft:granite"}


def test_a_tag_binding_written_by_the_gui_is_an_id(world):
    """Otherwise GUI-authored steps never upgrade, and stay rename-vulnerable forever."""
    tags, bps = world
    slot = MappingSlot(name="src", kind="tag", tag_type="bool", registry_type=REG)
    saved = _round_trip(_manifest(src=slot), {}, tags, bps)
    assert saved["src"] == tags.definition(REG, "aaa_first")["id"]
    assert not isinstance(saved["src"], str)


@pytest.mark.parametrize("legacy,kind,extra", [
    ("remove", "tag", {"tag_type": "bool", "registry_type": REG}),
    ("StoneType", "blueprint", {}),
])
def test_a_legacy_name_binding_still_resolves_and_upgrades(world, legacy, kind, extra):
    """Existing rows hold names. Opening a step must find them — not fall to index 0 —
    and saving should write the id, which is how a step upgrades in place."""
    tags, bps = world
    slot = MappingSlot(name="m", kind=kind, **extra)
    saved = _round_trip(_manifest(m=slot), {"m": legacy}, tags, bps)
    assert saved["m"] == binding_id(slot, legacy, tag_store=tags, blueprint_store=bps)


def test_a_many_binding_keeps_its_stored_order(world):
    """Open+OK must not rewrite data just because the widget enumerates differently."""
    tags, bps = world
    slot = MappingSlot(name="stones", kind="blueprint_instance", cardinality="many")
    bound = [binding_id(slot, f"StoneType:{n}", blueprint_store=bps)
             for n in ("granite", "andesite")]          # NOT alphabetical
    assert _round_trip(_manifest(stones=slot), {"stones": bound}, tags, bps)["stones"] == bound


# --- a suggestion is not a selection --------------------------------------------------
#
# A combo box always has a current row; there is no empty state. So a picker the user has
# never touched looks exactly like a deliberate choice — and the step it belongs to reads
# as bound when its binding is empty. Two halves: say so visually, and let the obvious
# gesture commit it.

def _pack_combo(qapp, stored=None):
    from packsmith.core.packages import ActionManifest, MappingSlot
    from packsmith.gui.job_editor import StepForm
    from packsmith.core.jobs import JobStep

    slot = MappingSlot(name="out", kind="pack", pack_kind="resourcepacks", access="write")
    manifest = ActionManifest(package_name="p", action_id="a", file="a.star",
                              function="run", mappings={"out": slot})

    class Targets:
        def available(self, kind):
            return ["auto_lang_overrides", "tweaks_bakery"]

    step = JobStep(id=1, job_id=1, position=0, kind="action", action_ref="p:a",
                   bindings={"out": stored} if stored else {})
    form = StepForm(manifest, None, step, packdump=None, pack_targets=Targets())
    return form, form._mapping_widgets["out"]


def test_an_unchosen_picker_shows_its_suggestion_dimmed(qapp):
    """It still shows a row — a combo has no empty state — but says it is a recommendation
    rather than a decision."""
    _form, combo = _pack_combo(qapp)
    assert combo.currentText() == "auto_lang_overrides"
    assert combo.property("unconfirmed") is True


def test_a_stored_binding_is_not_dimmed(qapp):
    _form, combo = _pack_combo(qapp, stored="tweaks_bakery")
    assert combo.currentText() == "tweaks_bakery"
    assert combo.property("unconfirmed") is False


def test_choosing_the_row_already_shown_still_commits(qapp):
    """The reported jank. `currentIndexChanged` never fires for the row that is already
    current, so picking the very item the app suggested — the one you most want — emitted
    nothing, and you had to select something else and come back."""
    form, combo = _pack_combo(qapp)
    commits = []
    form.committed.connect(lambda: commits.append(True))   # a Signal, so connect to it

    combo.activated.emit(combo.currentIndex())      # what clicking that row does

    assert commits, "picking the already-current row has to write the binding through"
    assert combo.property("unconfirmed") is False
    assert form.read()[0]["out"] == "auto_lang_overrides"


def test_the_index_changing_still_commits_as_before(qapp):
    """Guards the addition: `activated` is an extra door, not a replacement. Choosing a
    different row must not have quietly stopped working."""
    form, combo = _pack_combo(qapp)
    commits = []
    form.committed.connect(lambda: commits.append(True))

    combo.setCurrentIndex(1)

    assert commits


# --- required vs optional, said out loud ----------------------------------------------
#
# Optionality was only ever visible when a step refused to run. Required-and-unbound now
# wears a red asterisk; optional says so in italic. The asterisk clears the moment the slot
# is satisfied, because a form that shouts at a finished field shouts at nothing.

def _form_with(qapp, required_bound=None):
    from packsmith.core.jobs import JobStep
    from packsmith.core.packages import ActionManifest, MappingSlot
    from packsmith.gui.job_editor import StepForm

    class Tags:
        def definitions_for(self, registry_type):
            return {"remove": {"type": "bool"}, "queued": {"type": "bool"}}
        def definition(self, registry_type, name):
            return {"id": 1, "type": "bool"}

    manifest = ActionManifest(
        package_name="p", action_id="a", file="a.star", function="run",
        mappings={
            "must": MappingSlot(name="must", kind="tag", tag_type="bool",
                                registry_type="minecraft:item", required=True),
            "may": MappingSlot(name="may", kind="tag", tag_type="bool",
                               registry_type="minecraft:item", required=False),
        })
    step = JobStep(id=1, job_id=1, position=0, kind="action", action_ref="p:a",
                   bindings={"must": required_bound} if required_bound else {})
    return StepForm(manifest, Tags(), step, packdump=None)


def _label_text(form, name):
    return form._labels[name][0].text()


def test_an_unbound_required_slot_wears_a_red_asterisk(qapp):
    form = _form_with(qapp)
    assert "*" in _label_text(form, "must")
    assert "color:" in _label_text(form, "must"), "and it is coloured, not just punctuation"


def test_an_optional_slot_says_so_quietly(qapp):
    form = _form_with(qapp)
    text = _label_text(form, "may")
    assert "(optional)" in text and "<i>" in text
    assert "*" not in text


def test_the_asterisk_clears_once_the_slot_is_satisfied(qapp):
    """Otherwise the form keeps demanding something you already gave it."""
    form = _form_with(qapp)
    assert "*" in _label_text(form, "must")

    combo = form._mapping_widgets["must"]
    # Driven the way a user does: Qt emits `activated` for any pick, which is what marks
    # the value as chosen rather than suggested. A programmatic index change deliberately
    # does NOT, or restoring a form would count as choosing.
    combo.setCurrentIndex(combo.count() - 1)
    combo.activated.emit(combo.currentIndex())

    assert "*" not in _label_text(form, "must")


def test_the_refusal_says_what_would_satisfy_it(qapp):
    """`required mapping 'x' is unbound` reaches the user as the Jobs panel's "won't run"
    and as the pre-flight refusal, where it has to stand alone."""
    from packsmith.core.bindings import resolve_step
    from packsmith.core.packages import ActionManifest, MappingSlot

    slot = MappingSlot(name="must", kind="tag", tag_type="bool",
                       registry_type="minecraft:item", required=True)
    manifest = ActionManifest(package_name="p", action_id="a", file="a.star",
                              function="run", mappings={"must": slot})
    with pytest.raises(ValueError) as caught:
        resolve_step(manifest, bindings={}, config={}, tag_store=None)

    assert "bool tag on minecraft:item" in str(caught.value)
