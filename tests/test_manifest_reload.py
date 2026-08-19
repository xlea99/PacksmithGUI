"""Editing a manifest by hand has to take effect now, not on the next launch.

The bug, reported from real use: an action was declared with `function = "run"` while its
`.star` file only defined `helper`, so running it failed — correctly. Changing `function`
to `helper` in `manifest.json5` and running again failed **identically**, naming `run`, and
kept doing so until the app was restarted.

**Why it was so confusing is the asymmetry.** `PackageIndex.load_callable` deliberately
re-reads an action's *source* on every run, so editing a `.star` file applies immediately
(there is a test for that in `test_starlark`). Manifests are parsed once, at scan. So half
a package hot-reloaded and half did not, and the half that did not is the half that decides
which function the other half calls.

`PackageIndex.reload()` already existed and said so in its own docstring; nothing called it
when a document was saved through the editor. The in-app operations (New Action, Delete
File…) all route through `_after_package_change`, which does — so this was specifically the
hand-editing path, which is exactly the one §3.3.1 promises works: "they are just files on
disk."

The second half of this file is about what a hand-editor saves *first*: something broken.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.pack import Pack
from packsmith.core.packages import PackageIndex
from packsmith.core.starlark_runtime import StarlarkActionError
# Reused rather than rebuilt: these already know how to make a profile the shell will
# actually open, which is most of the setup a MainWindow test needs.
from tests.test_profile_lifecycle import instance, userdata      # noqa: F401  (fixtures)

SOURCE = '''
def helper(pack):
    pack.log("info", "helper ran")
    return "helper"
'''


def manifest(function, *, name="deep_end", action="removal"):
    return f'''{{
  "package": {{ "name": "{name}", "version": "0.1.0" }},
  "actions": [
    {{
      "id": "{action}",
      "file": "removal_and_hide.star",
      "function": "{function}",
      "name": "Removal",
    }},
  ],
}}
'''


@pytest.fixture
def packages(tmp_path):
    """One authored package whose declared entry point does not exist — the reported
    starting state, arrived at by accepting the default file and mistyping the function."""
    root = tmp_path / "packages"
    pkg = root / "deep_end"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json5").write_text(manifest("run"), encoding="utf-8")
    (pkg / "removal_and_hide.star").write_text(SOURCE, encoding="utf-8")
    return root


def invoke(index, ref):
    """Run an action the way the runner does — through `load_callable`, with a real `Pack`.

    A stub `pack` is not an option here: the Starlark prelude is assembled from the whole
    injected surface, so anything thinner fails at injection rather than at the line under
    test. Collaborators can all be None because these actions only log.
    """
    pack = Pack(staging=None, tag_store=None, packdump=None, action_ref=ref,
                blueprint_store=object())
    return index.load_callable(ref)(pack)


# --- the reported bug ----------------------------------------------------------------

def test_the_starting_state_fails_the_way_it_did(packages):
    """Establishes the premise rather than asserting the fix: the declared entry point is
    genuinely missing, so the failure the user saw is the correct one."""
    index = PackageIndex(packages)
    with pytest.raises(StarlarkActionError) as caught:
        invoke(index, "deep_end:removal")
    assert "run" in str(caught.value)


def test_editing_the_manifest_changes_which_function_runs(packages):
    """The fix. Without the reload the index keeps the manifest it parsed at scan, so the
    run keeps calling `run` and keeps reporting that `run` does not exist — while the file
    on disk plainly says `helper`."""
    index = PackageIndex(packages)
    (packages / "deep_end" / "manifest.json5").write_text(
        manifest("helper"), encoding="utf-8")

    index.reload()

    assert index.get("deep_end:removal").function == "helper"
    assert invoke(index, "deep_end:removal") == "helper"


def test_an_edited_source_still_needs_no_reload(packages):
    """The other half of the asymmetry, pinned so a future change cannot 'fix' the manifest
    side by making both cold. Sources are re-read per run on purpose."""
    (packages / "deep_end" / "manifest.json5").write_text(
        manifest("helper"), encoding="utf-8")
    index = PackageIndex(packages)

    (packages / "deep_end" / "removal_and_hide.star").write_text(
        'def helper(pack):\n    return "edited"\n', encoding="utf-8")

    assert invoke(index, "deep_end:removal") == "edited", "no reload should be needed"


# --- what a hand-editor saves first: something broken --------------------------------

def break_manifest(packages, name="deep_end"):
    (packages / name / "manifest.json5").write_text(
        '{"package": {"name": "deep_end"}, "actions": [ {', encoding="utf-8")


def test_a_broken_manifest_is_recorded_rather_than_raised(packages):
    """It must not raise, because the same scan runs when a profile opens — an unparseable
    manifest that propagated would stop the user reaching the editor they need in order to
    fix it."""
    index = PackageIndex(packages)
    break_manifest(packages)

    index.reload()

    assert "deep_end" in index.errors
    assert index.errors["deep_end"], "an error with no reason is not much of a report"


def test_a_broken_manifest_does_not_stop_a_profile_opening(packages):
    """The same thing at construction, which is where `MainWindow._load_profile` hits it.
    Before this, a hand-edited typo made the profile unopenable — and the only route back
    was a text editor outside Packsmith."""
    break_manifest(packages)
    index = PackageIndex(packages)          # must not raise
    assert index.actions == {}
    assert "deep_end" in index.errors


def test_a_broken_manifest_does_not_take_the_other_packages_with_it(packages):
    """One bad file used to abort the scan partway, leaving the index holding whichever
    packages happened to sort before it — which reads as 'the rest were deleted'."""
    other = packages / "palette"
    other.mkdir()
    (other / "manifest.json5").write_text(
        manifest("helper", name="palette", action="fill"), encoding="utf-8")
    (other / "removal_and_hide.star").write_text(SOURCE, encoding="utf-8")
    break_manifest(packages)

    index = PackageIndex(packages)

    assert "palette:fill" in index.actions
    assert index.package("palette") is not None


def test_a_broken_manifest_drops_its_actions_rather_than_keeping_them_stale(packages):
    """The deliberate half. Keeping the last good declaration would mean running a manifest
    that no longer exists — which is the bug at the top of this file, arrived at from the
    other direction."""
    index = PackageIndex(packages)
    assert "deep_end:removal" in index.actions

    break_manifest(packages)
    index.reload()

    assert "deep_end:removal" not in index.actions


def test_fixing_it_brings_the_package_back(packages):
    index = PackageIndex(packages)
    break_manifest(packages)
    index.reload()

    (packages / "deep_end" / "manifest.json5").write_text(
        manifest("helper"), encoding="utf-8")
    index.reload()

    assert index.errors == {}
    assert invoke(index, "deep_end:removal") == "helper"


# --- the wiring, which is where the bug actually was ---------------------------------
#
# Everything above passed before the fix too: `reload()` always worked. What was missing was
# anyone calling it when a document was saved. So these drive the real signal rather than
# the handler, because "the handler is correct but nothing connects to it" is precisely the
# shape of the bug.

@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(userdata, instance, packages, qapp):
    from packsmith.core.profile import Profile
    from packsmith.gui.main_window import MainWindow
    from tests.test_profile_lifecycle import with_packdump

    with_packdump(instance)
    profile = Profile.create("edits", loader="forge", mc_path=str(instance),
                             mc_version="1.20.1", loader_version="47.4.13")
    # The package the fixture built, moved to where this profile will look for it.
    import shutil
    shutil.copytree(packages, profile.packages_dir, dirs_exist_ok=True)

    win = MainWindow(profile_name="edits")
    assert win._blocked is None, "the shell never built, so nothing below is being tested"
    yield win
    win.close()


def saved(window, path):
    """Emit exactly what the editor emits after writing a document to disk."""
    from packsmith.gui.editor.host import EditorHost
    window._editor_host.file_saved.emit(EditorHost.key_for("package", path))


def test_saving_a_manifest_in_the_editor_takes_effect_immediately(window, packages):
    """The reported bug, end to end: edit `manifest.json5` in Packsmith's own editor, save,
    and the next run uses the new entry point — no restart."""
    assert window._packages.get("deep_end:removal").function == "run"

    (window._profile.packages_dir / "deep_end" / "manifest.json5").write_text(
        manifest("helper"), encoding="utf-8")
    saved(window, "deep_end/manifest.json5")

    assert window._packages.get("deep_end:removal").function == "helper"


def test_saving_an_instance_file_does_not_rescan_packages(window, monkeypatch):
    """The reload hangs off the *package* source specifically. Firing it for every save
    would rescan every manifest on every keystroke-to-disk in a config file."""
    calls = []
    monkeypatch.setattr(window._packages, "reload", lambda: calls.append(1))
    from packsmith.gui.editor.host import EditorHost

    window._editor_host.file_saved.emit(EditorHost.key_for("instance", "config/quark.toml"))

    assert calls == []


def test_saving_a_broken_manifest_says_so_in_the_same_moment(window, monkeypatch):
    """Those actions have just stopped being declared and the Actions panel has lost them.
    Reporting that later — or only in the log — is the failure mode §6.1 argues against:
    the user meets the effect with no way back to the cause."""
    warnings = []
    monkeypatch.setattr("packsmith.gui.main_window.QMessageBox.warning",
                        lambda *args, **kw: warnings.append(args[2]))

    break_manifest(window._profile.packages_dir)
    saved(window, "deep_end/manifest.json5")

    assert len(warnings) == 1
    assert "deep_end" in warnings[0]
    assert window._packages.actions == {}


# --- edits made OUTSIDE Packsmith ----------------------------------------------------
#
# Reported from real use: an action added to `manifest.json5` in another editor did not
# appear in the job editor's step picker until a restart. The in-app save path reloads
# correctly (above) — nothing was watching for an edit Packsmith never saw. §3.3.1 invites
# exactly that, since a package is "just files on disk".

def test_the_fingerprint_notices_an_outside_edit(packages):
    index = PackageIndex(packages)
    before = index.fingerprint()

    (packages / "deep_end" / "manifest.json5").write_text(
        manifest("helper", action="renamed"), encoding="utf-8")

    assert index.fingerprint() != before


def test_the_fingerprint_notices_a_whole_new_package(packages):
    """Globbed from disk rather than taken from what is already indexed, so a package added
    while Packsmith was in the background counts."""
    index = PackageIndex(packages)
    before = index.fingerprint()

    new = packages / "late_arrival"
    new.mkdir()
    (new / "manifest.json5").write_text(
        manifest("helper", name="late_arrival", action="thing"), encoding="utf-8")

    assert index.fingerprint() != before


def test_an_untouched_directory_looks_untouched(packages):
    """The common case is alt-tabbing back having changed nothing, and it runs on every
    window activation — so it has to be quiet, and cost one stat per package."""
    index = PackageIndex(packages)
    assert index.fingerprint() == index.fingerprint()


def test_focus_picks_up_an_action_added_by_hand(window, packages):
    """End to end, through the signal the window actually reacts to."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QWindowStateChangeEvent

    assert "deep_end:removal" in window._packages.actions
    assert "deep_end:second" not in window._packages.actions

    text = (window._profile.packages_dir / "deep_end" / "manifest.json5").read_text(
        encoding="utf-8")
    text = text.replace('  ],', '    { "id": "second", "file": "removal_and_hide.star",\n'
                                '      "function": "helper" },\n  ],')
    (window._profile.packages_dir / "deep_end" / "manifest.json5").write_text(
        text, encoding="utf-8")

    window._check_for_edited_packages()

    assert "deep_end:second" in window._packages.actions


def test_regaining_focus_with_nothing_changed_reloads_nothing(window, monkeypatch):
    """Otherwise every alt-tab rebuilds the Actions and Packages panels, losing whatever
    was selected in them."""
    calls = []
    monkeypatch.setattr(window._packages, "reload", lambda: calls.append(1))

    window._check_for_edited_packages()
    window._check_for_edited_packages()

    assert calls == []


def test_focus_drops_an_action_deleted_by_hand(window, packages):
    """The other direction, which is the one that misleads: a picker still offering an
    action nobody declares any more lets you build a step that cannot run."""
    assert "deep_end:removal" in window._packages.actions

    (window._profile.packages_dir / "deep_end" / "manifest.json5").write_text(
        '{ "package": { "name": "deep_end" }, "actions": [] }', encoding="utf-8")
    window._check_for_edited_packages()

    assert window._packages.actions == {}


def test_saving_a_deletion_in_the_editor_drops_it_immediately(window):
    """And through the in-app path, which does not wait for focus to leave and come back."""
    from packsmith.gui.editor.host import EditorHost

    (window._profile.packages_dir / "deep_end" / "manifest.json5").write_text(
        '{ "package": { "name": "deep_end" }, "actions": [] }', encoding="utf-8")
    window._editor_host.file_saved.emit(
        EditorHost.key_for("package", "deep_end/manifest.json5"))

    assert window._packages.actions == {}
