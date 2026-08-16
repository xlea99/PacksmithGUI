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
