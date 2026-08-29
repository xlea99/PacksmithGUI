"""The right-click menu on a tab — reveal, and jump to it in the browser.

This file exists because the feature shipped broken and the suite did not notice. Every
piece was tested — the tree descent, the reveal helper, the path splitting — and nothing
ever *called the handler*, so a missing import inside it was invisible until someone
right-clicked. The lesson is not "add an import test"; it is that a menu nobody builds in
a test is a menu whose first construction happens in front of the user.

The other failures worth pinning are about which file a tab is: a jar member that reveals
its member instead of its jar has nothing to open, and a tab with no file at all that
answers with one produces a menu whose actions point somewhere arbitrary.
"""
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.profile import Profile
from tests.test_profile_lifecycle import instance, userdata, with_packdump   # noqa: F401


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def profile(userdata, instance):
    with_packdump(instance)
    (instance / "mods").mkdir(exist_ok=True)
    (instance / "mods" / "twigs.jar").write_bytes(b"PK\x03\x04not really")
    return Profile.create("tabmenu", loader="forge", mc_path=str(instance),
                          mc_version="1.20.1", loader_version="47.4.13")


@pytest.fixture
def window(profile, qapp):
    from packsmith.gui.main_window import MainWindow
    win = MainWindow(profile_name=profile.name)
    assert win._blocked is None, "the shell never built, so nothing here is being tested"
    yield win
    win.close()


# --- the handler actually runs -----------------------------------------------------------

def labels(menu):
    return [] if menu is None else [a.text() for a in menu.actions()]


def test_right_clicking_a_file_tab_builds_a_menu(window):
    """The regression. `_on_tab_menu` referenced `QMenu` without importing it, which is a
    NameError at the moment of the click and nowhere earlier — the module imports fine, the
    window builds fine, and every other test passes."""
    tab = window._open_file("config/quark-common.toml")
    assert tab is not None

    assert labels(window._tab_menu(tab)) == ["Reveal in Explorer", "Show in Files Tab"]


def test_a_tab_with_no_file_behind_it_gets_no_menu(window):
    """A View, a job editor, a packdump diff. Offering "Reveal in Explorer" for one would
    have to point at something, and there is nothing to point at."""
    window._open_browse("minecraft:item")
    tab = window._open_tabs[("browse", "minecraft:item")]

    assert window._tab_file(tab) is None
    assert window._tab_menu(tab) is None


# --- which file a tab is -----------------------------------------------------------------

def test_an_instance_file_answers_with_its_root_and_path(window):
    tab = window._open_file("config/quark-common.toml")
    assert window._tab_file(tab) == ("minecraft", "config/quark-common.toml")


def test_a_jar_member_answers_with_the_jar(window):
    """"Reveal `assets/x/y.png` inside twigs.jar" has no answer the OS or the file browser
    could act on. The jar is the file that exists; extracting the member somewhere just to
    have a path would be the scratch file §6.5 exists to remove."""
    key = window._editor_host.key_for("jar", "mods/twigs.jar!assets/twigs/lang/en_us.json")
    fake = object()
    window._open_tabs[("doc", key)] = fake

    assert window._tab_file(fake) == ("minecraft", "mods/twigs.jar")


def test_a_package_file_answers_with_an_absolute_path_and_no_root(window):
    """Packages live outside every tracked root — provenance, not ownership (§3.3.1). A
    None root is what says "the other half is absolute", and it is why the menu offers
    Reveal but not Show in Files."""
    key = window._editor_host.key_for("package", "deep_end/stone_blocks.star")
    fake = object()
    window._open_tabs[("doc", key)] = fake

    root, path = window._tab_file(fake)
    assert root is None
    assert os.path.isabs(path)
    assert path.endswith(os.path.join("deep_end", "stone_blocks.star"))


def test_the_menu_follows_the_tab_that_was_clicked(window, monkeypatch):
    """Not the current one. Right-clicking a background tab and revealing whatever happens
    to be in front is worse than doing nothing — it opens a folder confidently."""
    first = window._open_file("config/quark-common.toml")
    window._open_browse("minecraft:item")           # now the tab in front

    seen = []
    monkeypatch.setattr(window, "_reveal_tab_file",
                        lambda root, rel: seen.append((root, rel)))
    menu = window._tab_menu(first)
    next(a for a in menu.actions() if a.text() == "Reveal in Explorer").trigger()

    assert seen == [("minecraft", "config/quark-common.toml")]


def test_a_package_tab_is_revealed_but_not_shown_in_files(window):
    """Reveal works on an absolute path; Show in Files has no row to land on."""
    key = window._editor_host.key_for("package", "deep_end/stone_blocks.star")
    fake = object()
    window._open_tabs[("doc", key)] = fake

    assert labels(window._tab_menu(fake)) == ["Reveal in Explorer"]


# --- copying the texture a tab is showing ------------------------------------------------

def image_tab(window, name="cut_abyssmarine.png", data=None, rel="mods/twigs.jar"):
    """An image tab holding bytes, keyed like one opened from a cell."""
    from packsmith.core.textures import blank_png
    from packsmith.gui.image_viewer import ImageViewerTab
    tab = ImageViewerTab(data if data is not None else blank_png(), name)
    tab.packsmith_file = ("minecraft", rel)
    window._open_tabs[("texture", "assets/x/textures/block/y.png")] = tab
    return tab


def test_an_image_tab_offers_copy(window):
    assert labels(window._tab_menu(image_tab(window)))[0] == "Copy Texture"


def test_a_text_tab_does_not(window):
    """It would put a `.toml` on the clipboard under a menu item that says Texture."""
    tab = window._open_file("config/quark-common.toml")
    assert "Copy Texture" not in labels(window._tab_menu(tab))


def test_the_tab_copies_its_own_bytes_not_the_file_it_came_from(window):
    """The bug this exists for: a texture opened from a cell was read out of a JAR, so the
    tab's `rel` names `mods/twigs.jar` and not the member. Re-reading that path to get the
    bytes would put an entire mod jar on the clipboard, and it would succeed."""
    from PySide6.QtWidgets import QApplication
    tab = image_tab(window, data=b"\x89PNG\r\n\x1a\nthese-exact-bytes",
                    rel="mods/twigs.jar")
    # Through the menu's own handler, not the helper underneath it — the mistake being
    # guarded is which BYTES that handler chooses, so a test that hands them over itself
    # would pass no matter what the handler did.
    window._copy_tab_image(tab, "mods/twigs.jar")

    staged = Path(QApplication.clipboard().mimeData().urls()[0].toLocalFile())
    assert staged.read_bytes() == b"\x89PNG\r\n\x1a\nthese-exact-bytes"
    assert staged.name == "cut_abyssmarine.png"


def test_the_clipboard_carries_a_file_and_an_image(window):
    """Both formats, because they are different destinations. A file manager takes the URL
    and drops a real .png; an image editor takes the bitmap. Offering one makes the other
    silently paste nothing."""
    from PySide6.QtWidgets import QApplication
    tab = image_tab(window)
    staged = window._copy_file_to_clipboard(tab.label, tab.data)

    mime = QApplication.clipboard().mimeData()
    assert [u.toLocalFile() for u in mime.urls()] == [str(staged).replace("\\", "/")]
    assert mime.hasImage()


def test_bytes_that_are_not_an_image_still_copy_as_a_file(window):
    """A null QImage on the clipboard is a paste that produces an empty rectangle, so the
    bitmap is only offered when it decodes. The file half must still work."""
    from PySide6.QtWidgets import QApplication
    staged = window._copy_file_to_clipboard("notes.txt", b"not an image at all")

    assert staged.read_bytes() == b"not an image at all"
    assert QApplication.clipboard().mimeData().urls()
