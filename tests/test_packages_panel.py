"""The Packages panel (design 3.3.1) — one package, whole.

Two tests, and both are here for the same reason: they fail in a way a developer's own
profile will not show them. Everything else about this panel — the tree, the icons, which
menu items appear — announces itself the moment you look at it.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.packages import PackageIndex, create_package
from packsmith.gui.shell.panels.packages_panel import PackagesPanel


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def installed(root, name):
    """A downloaded package — structurally identical, provenance apart (§3.3.1)."""
    package = root / name
    package.mkdir(parents=True)
    (package / "manifest.toml").write_text(
        f'[package]\nname = "{name}"\nprovenance = "downloaded"\n', encoding="utf-8")


def panel_over(root):
    return PackagesPanel(PackageIndex(root))


def labels(menu):
    return [action.text() for action in menu.actions() if action.text()]


def test_a_mixed_profile_opens_on_a_real_package(qapp, tmp_path):
    """The separator carries **None** as its item data, and so does "nothing selected
    yet" — so a first load that looks up the remembered selection with `findData(None)`
    finds the separator, selects it, and the panel reports having no package while holding
    three.

    It needs BOTH halves of the dropdown to exist, which is exactly why it hides: a profile
    with nothing installed has no separator and works perfectly, so the author of an
    authored-only pack never sees it and the first person to install something does.
    """
    create_package(tmp_path, "mine")
    installed(tmp_path, "vendored")

    panel = PackagesPanel(PackageIndex(tmp_path))
    try:
        assert panel.package is not None, "opened on the separator, not on a package"
        assert panel.package.name == "mine"
    finally:
        panel.deleteLater()


def test_making_a_package_is_never_gated_on_having_one(qapp, tmp_path):
    """`＋` is otherwise the authored-package menu, and gating the whole thing on the
    selection being authored is the obvious reading — it is also a dead end.

    A profile holding only downloaded packages, or none at all, would have no way to create
    a first one, and nothing about that looks broken: the button is simply absent or empty,
    which reads as "this app cannot do that" rather than as a bug. Creating a package is
    not an edit to the selected one, so it does not answer to the selection's provenance.
    """
    installed(tmp_path, "vendored")
    panel = PackagesPanel(PackageIndex(tmp_path))
    try:
        assert panel.package.provenance == "downloaded"
        offered = labels(panel.add_menu())
        assert any("Package" in text for text in offered), \
            "no way to create a package from a profile that has no authored one"
        # ...and nothing that would write to somebody else's package.
        assert not any(text.startswith(("New Action", "New File")) for text in offered)
    finally:
        panel.deleteLater()


def test_an_empty_profile_can_still_make_a_package(qapp, tmp_path):
    panel = PackagesPanel(PackageIndex(tmp_path))
    try:
        assert panel.package is None
        assert any("Package" in text for text in labels(panel.add_menu()))
    finally:
        panel.deleteLater()
