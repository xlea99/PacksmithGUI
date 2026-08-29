"""A declared package file, as a slot in the step editor (design 3.3.1, 3.3.2).

The whole point of declaring a file is that somebody who did not write the package finds
out it exists. So the failure worth guarding is not "the action cannot read it" — that
breaks loudly — it is **the row never appearing**, which breaks silently and leaves a
stranger with a package that quietly does half of what it should.

That is the same shape as the tab menu that shipped with a missing import: every piece was
tested and nothing ever built the thing. So this builds the row.

The other silent one is the PATH the row asks for. It travels as `<package>/<file>`, which
is what `_open_package_document` takes; get the shape wrong and Create makes a file
somewhere nobody reads while reporting success.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.jobs import JobStep
from packsmith.core.packages import ActionManifest, PackageFile, load_package
from packsmith.gui.job_editor import StepForm

REG = "minecraft:item"


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def manifest_with_file(**over):
    declared = PackageFile(name="custom_rules", path="rules/custom.json",
                           description="Your own rules.", **over)
    return ActionManifest(package_name="p", action_id="a", file="a.star",
                          function="run", files={"custom_rules": declared})


def form_for(tags, package_dir, **over):
    step = JobStep(id=1, job_id=1, position=0, kind="action", action_ref="p:a")
    return StepForm(manifest_with_file(**over), tags, step, package_dir=package_dir)


def row(form):
    return form._file_widgets["custom_rules"]


# --- the row exists ----------------------------------------------------------------------

def test_a_declared_file_becomes_a_row(qapp, tags, tmp_path):
    """§3.3.2: mappings and config are one list of "slots to fill". A file is a third kind
    of slot, and a package that declares one the editor never renders is a package whose
    user never learns there is anything to write."""
    form = form_for(tags, tmp_path)
    assert "custom_rules" in form._file_widgets
    assert "rules/custom.json" in row(form).where.text()


def test_an_absent_file_offers_to_create_it_and_says_so(qapp, tags, tmp_path):
    form = form_for(tags, tmp_path)

    assert row(form).button.text() == "Create"
    assert "not created yet" in row(form).where.text()


def test_a_file_that_exists_offers_to_edit_it(qapp, tags, tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "custom.json").write_text("[]", encoding="utf-8")
    form = form_for(tags, tmp_path)

    assert row(form).button.text() == "Edit"
    assert "not created yet" not in row(form).where.text()


def test_the_button_becomes_edit_once_it_is_there(qapp, tags, tmp_path):
    """After Create, without rebuilding the panel — otherwise the row keeps offering to
    create a file that now exists, and pressing it again looks like it did nothing."""
    form = form_for(tags, tmp_path)
    assert row(form).button.text() == "Create"

    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "custom.json").write_text("[]", encoding="utf-8")
    form.refresh_files()

    assert row(form).button.text() == "Edit"


def test_no_package_folder_disables_rather_than_lies(qapp, tags):
    """A form built without one must not offer a button that cannot work."""
    form = form_for(tags, None)
    assert row(form).button.isEnabled() is False
    assert "unavailable" in row(form).where.text()


# --- the path it asks for ----------------------------------------------------------------

def test_pressing_it_asks_for_the_declared_path(qapp, tags, tmp_path):
    """The form emits what the manifest declared, unqualified — `StepPanel` prefixes the
    package name on the way out, because the row knows a path and not which package it is
    in. Two layers, and this is the lower one; the qualification is asserted where it
    happens rather than assumed here."""
    form = form_for(tags, tmp_path)
    asked = []
    form.package_file_requested.connect(lambda path, template: asked.append((path, template)))
    row(form).button.click()

    assert asked == [("rules/custom.json", "")]


def test_the_template_travels_with_it(qapp, tags, tmp_path):
    form = form_for(tags, tmp_path, template="rules/custom.example.json")
    asked = []
    form.package_file_requested.connect(lambda path, template: asked.append((path, template)))
    row(form).button.click()

    assert asked == [("rules/custom.json", "rules/custom.example.json")]


# --- the manifest half -------------------------------------------------------------------

def test_a_manifest_declares_files(tmp_path):
    package = tmp_path / "p"
    package.mkdir()
    (package / "a.star").write_text("def run(pack):\n    pass\n", encoding="utf-8")
    (package / "manifest.json5").write_text("""{
      "package": {"name": "p"},
      "actions": [{
        "id": "a", "file": "a.star", "function": "run",
        "files": {
          "custom_rules": {
            "path": "rules/custom.json",
            "description": "Your own rules.",
            "template": "rules/custom.example.json",
          },
        },
      }],
    }""", encoding="utf-8")

    loaded = load_package(package)
    declared = loaded.actions[0].files["custom_rules"]

    assert declared.path == "rules/custom.json"
    assert declared.template == "rules/custom.example.json"
    # Defaults to the user's, because that is what declaring a file is FOR — an action's
    # own scratch state needs no announcement.
    assert declared.user_authored is True
    assert loaded.actions[0].user_authored_paths == ["rules/custom.json"]


def test_a_declared_file_without_a_path_is_refused(tmp_path):
    package = tmp_path / "p"
    package.mkdir()
    (package / "a.star").write_text("def run(pack):\n    pass\n", encoding="utf-8")
    (package / "manifest.json5").write_text(
        '{"package": {"name": "p"}, "actions": [{"id": "a", "file": "a.star", '
        '"function": "run", "files": {"oops": {"description": "no path"}}}]}',
        encoding="utf-8")

    with pytest.raises(ValueError, match="needs a 'path'"):
        load_package(package)


def test_the_panel_qualifies_it_with_the_package_name(qapp, tags, tmp_path):
    """The upper half. `<package>/<file>` is what `_open_package_document` resolves against
    the packages directory — a bare `rules/custom.json` would create a file in a folder no
    package owns, and report success."""
    from packsmith.gui.job_editor import StepPanel

    class _Index:
        directory = tmp_path

    panel = StepPanel(job_store=None, tag_store=tags, package_index=_Index())
    panel._current_manifest = manifest_with_file(template="rules/custom.example.json")
    asked = []
    panel.package_file_requested.connect(lambda p, t: asked.append((p, t)))
    panel._on_package_file("rules/custom.json", "rules/custom.example.json")

    assert asked == [("p/rules/custom.json", "p/rules/custom.example.json")]
