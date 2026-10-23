"""From a sound-event cell to the right verbs.

Offering the wrong label is cosmetic. Firing the handler with the wrong file is not: "Open
variant 3" that opens variant 1 is indistinguishable from variant 1 sounding like variant 3.
"""
import pytest

from packsmith.core.sounds import SOUND_EVENTS, SoundFile
from packsmith.gui.sound_menu import SoundMenu


class FakeCatalog:
    def __init__(self, events):
        self._events = events

    def files(self, event_id):
        return list(self._events.get(event_id, []))


STEP = [SoundFile("assets/m/sounds/block/step1.ogg"), SoundFile("assets/m/sounds/block/step2.ogg", weight=3)]
ONE = [SoundFile("assets/m/sounds/ambient/hum.ogg")]
CATALOG = FakeCatalog({"m:block.step": STEP, "m:ambient.hum": ONE})


@pytest.fixture
def menu():
    widgets = pytest.importorskip("PySide6.QtWidgets")
    widgets.QApplication.instance() or widgets.QApplication([])
    return widgets.QMenu()


def labels(menu):
    return [a.text() for a in menu.actions() if a.text()]


def test_only_sound_events_get_the_verbs(menu):
    assert SoundMenu(lambda: CATALOG, None, None).extend(menu, "minecraft:block", "m:block.step") is False
    assert labels(menu) == []


def test_a_single_file_offers_play_and_open(menu):
    added = SoundMenu(lambda: CATALOG, None, None).extend(menu, SOUND_EVENTS, "m:ambient.hum")
    assert added is True
    assert labels(menu) == ["Play Sound", "Open Sound"]


def test_several_files_open_through_a_submenu_naming_each(menu):
    SoundMenu(lambda: CATALOG, None, None).extend(menu, SOUND_EVENTS, "m:block.step")
    assert labels(menu) == ["Play Sound", "Open Sound (2)"]
    sub = next(a for a in menu.actions() if a.menu() is not None).menu()
    assert labels(sub) == ["block/step1.ogg", "block/step2.ogg"]


def test_open_fires_with_the_specific_file(menu):
    fired = []
    SoundMenu(lambda: CATALOG, None, lambda t, f: fired.append((t.event_id, f.member))).extend(
        menu, SOUND_EVENTS, "m:block.step")
    sub = next(a for a in menu.actions() if a.menu() is not None).menu()
    sub.actions()[1].trigger()
    assert fired == [("m:block.step", "assets/m/sounds/block/step2.ogg")]


def test_play_fires_with_the_whole_target(menu):
    fired = []
    SoundMenu(lambda: CATALOG, fired.append, None).extend(menu, SOUND_EVENTS, "m:block.step")
    next(a for a in menu.actions() if a.text() == "Play Sound").trigger()
    assert fired[0].event_id == "m:block.step" and fired[0].count == 2


def test_an_event_with_no_files_offers_nothing(menu):
    added = SoundMenu(lambda: CATALOG, None, None).extend(menu, SOUND_EVENTS, "m:code_only")
    assert added is False and labels(menu) == []


def test_no_catalog_offers_nothing(menu):
    assert SoundMenu(lambda: None, None, None).extend(menu, SOUND_EVENTS, "m:block.step") is False
