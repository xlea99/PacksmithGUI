"""Adopting a new packdump without tearing the workspace down — design 3.1.

A packdump change is not a profile change. Layer 2 — tags, blueprints, views, jobs — lives
in the profile database, and a new registry does not touch a byte of it, so there is nothing
that requires closing the database or destroying every open tab. The import fires on window
focus, which is exactly when the user comes back from playtesting; a mod the launcher
updated on its own is enough to trigger it. Charging them their whole workspace for that is
a punishment for the loop PackSmith exists to support.

What replaces the teardown is a **rebind**, and rebinding is only safe if it is exhaustive.
A holder that keeps the old dump does not crash — it answers, confidently, from a registry
the game no longer has. §3.1 calls exactly that out as the failure worse than an unwanted
import, *because it looks fine*. So the load-bearing test here is `test_no_holder_is_left_
on_the_old_packdump`, which walks the live window and finds holders on its own rather than
asking the code which ones it thinks exist — a list that agrees with itself proves nothing.
"""
import json
import os
import pathlib
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.packdump import Packdump
from packsmith.core.profile import Profile, delete_profile

PROFILE = "packdump_adopt_probe"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    yield app


def write_dump(path, *, items=("minecraft:stone", "minecraft:dirt"), mods=("alpha",),
               generated="2026-08-09T12:00:00+00:00"):
    """A packdump on disk, as the Forge mod would leave it."""
    (path / "registries").mkdir(parents=True, exist_ok=True)
    (path / "attributes").mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({
        "type": "packsmith_full_dump", "schema_version": 1,
        "generated_at_utc": generated,
        "minecraft_version": "1.20.1", "loader": "forge", "loader_version": "47.4.10",
        "mods": [{"mod_id": m, "name": m.title(), "version": "1.0"} for m in mods],
        "registries": [{"type": "minecraft:item", "file": "minecraft_item.json",
                        "count": len(items)}],
    }), encoding="utf-8")
    (path / "registries" / "minecraft_item.json").write_text(
        json.dumps({"values": list(items)}), encoding="utf-8")
    (path / "attributes" / "localization.json").write_text(
        json.dumps({"locale": "en_us", "values": {}}), encoding="utf-8")
    return path


@pytest.fixture
def window():
    """A real MainWindow over a real profile with a real packdump.

    Deliberately not a stub: the whole question is whether every live holder gets rebound,
    and a stub window can only confirm the holders someone remembered to stub.
    """
    from packsmith.gui.main_window import MainWindow

    instance = pathlib.Path(tempfile.mkdtemp()) / "instance"
    (instance / "packsmith").mkdir(parents=True)
    write_dump(instance / "packsmith")
    try:
        delete_profile(PROFILE)
    except Exception:
        pass
    Profile.create(PROFILE, mc_path=str(instance), loader="forge",
                   loader_version="47.4.10", mc_version="1.20.1")
    win = MainWindow(profile_name=PROFILE)
    win._instance_path = instance          # for tests that write a second dump
    try:
        yield win
    finally:
        win.close()
        try:
            delete_profile(PROFILE)
        except Exception:
            pass


def new_dump_arrives(win, *, adopted=True, **kwargs):
    """Write a genuinely different dump into the instance and refocus the window, exactly
    as coming back from the game does.

    The default has to differ from the fixture's dump in something `Packdump.__eq__` looks
    at, or the import is a no-op and every assertion after it passes without meaning
    anything — which is how two tests here first went green. `adopted` says whether the
    window is expected to take the new dump on, and is checked either way.
    """
    kwargs.setdefault("items", ("minecraft:stone", "minecraft:dirt", "minecraft:glass"))
    kwargs.setdefault("generated", "2026-08-09T13:00:00+00:00")
    before = win._packdump
    write_dump(win._instance_path / "packsmith", **kwargs)
    win._check_for_new_packdump()
    if adopted:
        assert win._packdump is not before, "no import happened; the test proves nothing"
    else:
        assert win._packdump is before
    return before


# --- the exhaustiveness check ------------------------------------------------------------

def find_packdump_holders(root):
    """Every live object reachable from the window that stores a packdump.

    Found by walking, not by asking. `MainWindow._packdump_holders` is the production
    enumeration; if this test used it, the two would agree by construction and a holder
    missing from both would pass. So this crawls PackSmith objects, their containers and the
    Qt child tree, and reports anything holding something that looks like a dump.
    """
    from PySide6.QtCore import QObject

    found, seen = [], set()

    def visit(obj, depth=0):
        if obj is None or depth > 6 or id(obj) in seen:
            return
        seen.add(id(obj))

        if isinstance(obj, (list, tuple, set, frozenset)):
            for item in obj:
                visit(item, depth + 1)
            return
        if isinstance(obj, dict):
            for item in obj.values():
                visit(item, depth + 1)
            return
        if type(obj).__module__.split(".")[0] != "packsmith":
            return

        for attr in ("_packdump", "_dump"):
            held = getattr(obj, attr, None)
            if isinstance(held, Packdump):
                found.append((obj, attr, held))

        for value in vars(obj).values():
            visit(value, depth + 1)
        if isinstance(obj, QObject):
            for child in obj.children():
                visit(child, depth + 1)

    visit(root)
    return found


def test_no_holder_is_left_on_the_old_packdump(window):
    """The test the whole design rests on.

    Rebinding is only as good as its coverage, and the failure mode of a miss is silence —
    a panel or a tab that keeps answering from a registry the game no longer has. If a new
    holder is added without a `set_packdump`, this is what says so.
    """
    window._open_browse("minecraft:item")
    window._blueprints.define("StoneType")
    window._open_blueprint("StoneType")
    window._open_job_editor(window._jobs.create("Nightly"))
    window._open_packdump_diff()

    old = window._packdump
    new_dump_arrives(window, items=("minecraft:stone", "minecraft:copper_ingot"))
    assert window._packdump is not old, "no import happened; the rest proves nothing"

    stale = [(type(obj).__name__, attr)
             for obj, attr, held in find_packdump_holders(window) if held is old]
    assert stale == [], f"holders left on the old packdump: {stale}"


def test_the_walk_would_actually_catch_a_missed_holder(window):
    """Guards the guard. A crawl that quietly reaches nothing would pass this suite while
    proving nothing at all, so one holder is sabotaged and has to be found."""
    window._open_browse("minecraft:item")
    old = window._packdump
    new_dump_arrives(window)

    window._registry_panel._packdump = old          # as if someone forgot the rebind
    stale = [(type(obj).__name__, attr)
             for obj, attr, held in find_packdump_holders(window) if held is old]
    assert ("RegistryPanel", "_packdump") in stale


def test_the_crawl_reaches_more_than_the_window_itself(window):
    """It has to get into panels, stores and open tabs — a walk that only found
    `MainWindow._packdump` would pass the sabotage test above and still be useless."""
    window._open_browse("minecraft:item")
    kinds = {type(obj).__name__ for obj, _, _ in find_packdump_holders(window)}
    assert {"MainWindow", "BlueprintStore", "RegistryPanel", "ErrorsView",
            "RegistryTableModel"} <= kinds, f"the crawl only reached {sorted(kinds)}"


# --- what the user actually notices ---------------------------------------------------------

def test_the_workspace_survives_the_import(window):
    """The reason for all of it: coming back from the game must not cost you your tabs."""
    browse = window._open_browse("minecraft:item")
    window._blueprints.define("StoneType")
    blueprint = window._open_blueprint("StoneType")

    new_dump_arrives(window, items=("minecraft:stone", "minecraft:copper_ingot"))

    assert window._open_tabs.get(("browse", "minecraft:item")) is browse, \
        "the tab was rebuilt rather than kept — scroll, cursor and undo went with it"
    assert window._open_tabs.get(("blueprint", "StoneType")) is blueprint


def test_the_database_is_never_closed(window):
    """Layer 2 is untouched by a registry change, so reopening it was pure cost — and it is
    what dragged the tabs down."""
    db = window._db
    tags = window._tags
    new_dump_arrives(window)
    assert window._db is db and window._tags is tags


def test_an_open_view_shows_the_new_registry(window):
    """Rebinding without re-evaluating would leave the table describing a registry that no
    longer exists — the silent staleness this is all here to prevent."""
    tab = window._open_browse("minecraft:item")
    model = window._tab_models[tab]
    assert "minecraft:copper_ingot" not in _ids(model)

    new_dump_arrives(window, items=("minecraft:stone", "minecraft:copper_ingot"))

    ids = _ids(model)
    assert "minecraft:copper_ingot" in ids
    assert "minecraft:dirt" not in ids, "an entry the new dump dropped is still listed"


def _ids(model):
    from PySide6.QtCore import Qt
    return {model.data(model.index(row, 0), Qt.DisplayRole)
            for row in range(model.rowCount())}


def test_the_header_count_follows_the_dump(window):
    """The most visible thing that would silently disagree with the pack."""
    new_dump_arrives(window, mods=("alpha", "beta", "gamma"))
    assert "3 mods" in window._header_info.text()


def test_the_registry_panel_recounts(window):
    new_dump_arrives(window, items=("minecraft:stone", "a:b", "a:c"))
    item = window._registry_panel._tree.topLevelItem(0)
    assert item.text(1) == "3"


def test_orphans_are_recomputed_against_the_new_dump(window):
    """The Errors panel *is* the fallout report, so a stale reference here would keep
    saying the pack was fine while it wasn't."""
    window._tags.define("minecraft:item", "keep", "bool")
    window._tags.assign("minecraft:item", "minecraft:dirt", "keep", True)
    errors = window._bottom.panel("errors")
    assert not errors.has_problems()

    new_dump_arrives(window, items=("minecraft:stone",))     # dirt is gone

    assert errors.has_problems(), "the assignment on the dropped entry was not reported"


def test_the_loader_table_is_recomputed(window):
    """§8.1: a mod update can install or remove the pack loader itself, so the resolution
    table is derived from the dump rather than kept."""
    before = window._loaders
    new_dump_arrives(window, mods=("alpha", "paxi"))
    assert window._loaders is not before


# --- when rebinding cannot be done ------------------------------------------------------------

def test_a_failed_rebind_falls_back_to_the_full_rebuild(window, monkeypatch):
    """A half-rebound window is worse than a rebuilt one: part of it would still be
    answering from the old registry, which is precisely what this replaced."""
    window._open_browse("minecraft:item")
    rebuilt = []
    monkeypatch.setattr(type(window), "_switch_profile_in_place",
                        lambda self: rebuilt.append(True))
    monkeypatch.setattr(type(window), "_rebind_packdump",
                        lambda self, dump: (_ for _ in ()).throw(RuntimeError("nope")))

    new_dump_arrives(window, adopted=False)     # the stub rebuild never swaps it in
    assert rebuilt == [True]


def test_the_fallback_rebuild_puts_every_kind_of_tab_back(window, monkeypatch):
    """`_reopen_tabs` handles kinds by name, so a kind it has never heard of doesn't fail
    loudly — it just stops coming back. That is how the packdump-diff tab, the one thing
    you most want to read after an import, quietly went missing across a rebuild."""
    monkeypatch.setattr(type(window), "_rebind_packdump",
                        lambda self, dump: (_ for _ in ()).throw(RuntimeError("nope")))
    window._blueprints.define("StoneType")
    window._open_browse("minecraft:item")
    window._open_blueprint("StoneType")
    window._open_job_editor(window._jobs.create("Nightly"))
    window._open_packdump_diff()
    before = set(window._open_tabs)

    new_dump_arrives(window)                    # rebinding fails; the real rebuild runs

    assert set(window._open_tabs) == before, "a tab kind was not restored"


def test_a_postponed_rebuild_is_tried_again(window, monkeypatch):
    """If the unsaved-changes guard refuses the fallback rebuild, the dump is already on
    disk — so `check_packdump` would compare the instance against it and answer "unchanged"
    from then on. Without a retry the import would be lost for good.
    """
    monkeypatch.setattr(type(window), "_close_all_tabs", lambda self: False)
    monkeypatch.setattr(type(window), "_rebind_packdump",
                        lambda self, dump: (_ for _ in ()).throw(RuntimeError("nope")))

    old = new_dump_arrives(window, adopted=False,
                           items=("minecraft:stone", "minecraft:copper_ingot"))
    assert window._rebuild_pending, "the import was dropped silently"

    # Now the tabs can close, and refocusing must pick the owed import back up even though
    # nothing on the instance side changed again.
    monkeypatch.undo()
    window._check_for_new_packdump()
    assert not window._rebuild_pending
    assert window._packdump is not old
