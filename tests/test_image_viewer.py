"""Viewing images — design 6.0 editor dispatch, for textures.

Built for pixel art rather than photographs, which is what a modpack contains: of 381
textures sampled from eight real mods, 229 were 16x16 and 285 had alpha. Everything here
follows from those two facts.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QPoint, Qt
from PySide6.QtGui import QImage, QPainter

from packsmith.core import filetypes
from packsmith.gui.image_viewer import ZOOM_STEPS, ImageViewerTab, describe_dimensions


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def png_bytes(width, height, *, alpha=False):
    """A real PNG, encoded the way a mod would ship one."""
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(Qt.transparent if alpha else Qt.red)
    if alpha:
        painter = QPainter(image)
        painter.fillRect(0, 0, max(1, width // 2), height, Qt.blue)
        painter.end()
    # The QByteArray is held in a local on purpose: passing a temporary to QBuffer lets
    # Python collect it while Qt is still writing into it, which crashes the interpreter
    # rather than raising.
    store = QByteArray()
    buffer = QBuffer(store)
    buffer.open(QBuffer.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(store)


# --- decoding -------------------------------------------------------------------------

def test_a_texture_opens(qapp):
    tab = ImageViewerTab(png_bytes(16, 16), "stone.png")
    assert tab.ok
    assert "16x16" in tab._info.text()


def test_an_alpha_channel_is_called_out(qapp):
    """Three quarters of textures have one, and on a flat background transparent is
    indistinguishable from black — usually the exact thing you were looking for.

    The label says "alpha channel" rather than "transparency" deliberately: Qt answers
    about the image FORMAT, so a fully opaque RGBA texture would make the stronger claim
    false. The checkerboard shows which it actually is.
    """
    opaque = QImage(16, 16, QImage.Format_RGB32)
    opaque.fill(Qt.red)
    store = QByteArray()
    buffer = QBuffer(store)
    buffer.open(QBuffer.WriteOnly)
    opaque.save(buffer, "PNG")
    buffer.close()

    assert "alpha" in ImageViewerTab(png_bytes(16, 16, alpha=True), "x.png")._info.text()
    assert "alpha" not in ImageViewerTab(bytes(store), "x.png")._info.text()


def test_an_undecodable_image_says_so_instead_of_showing_an_empty_frame(qapp):
    """A truncated texture is a real thing to find in a jar; an empty frame would read as
    a bug in the viewer."""
    tab = ImageViewerTab(b"\x89PNG\r\n\x1a\n truncated right here", "broken.png")
    assert not tab.ok
    assert "could not be decoded" in tab._info.text()


def test_no_bytes_at_all_does_not_raise(qapp):
    assert not ImageViewerTab(b"", "gone.png").ok


# --- zoom -------------------------------------------------------------------------------

def test_a_tiny_texture_opens_magnified(qapp):
    """16x16 at 1:1 is a postage stamp — opening there would mean four clicks before every
    single texture became useful."""
    tab = ImageViewerTab(png_bytes(16, 16), "stone.png")
    assert tab.zoom > 1


def test_a_large_image_opens_unmagnified(qapp):
    tab = ImageViewerTab(png_bytes(2048, 2048), "atlas.png")
    assert tab.zoom == 1


def test_zoom_steps_are_integers(qapp):
    """A fractional zoom lands a source pixel across two screen pixels, reintroducing
    exactly the blur nearest-neighbour was chosen to avoid."""
    assert all(isinstance(z, int) for z in ZOOM_STEPS)


def test_zooming_in_and_out_moves_through_the_steps(qapp):
    tab = ImageViewerTab(png_bytes(64, 64), "gui.png")
    start = tab.zoom
    tab.zoom_in()
    assert tab.zoom > start
    tab.zoom_out()
    assert tab.zoom == start


def test_zoom_stops_at_both_ends(qapp):
    tab = ImageViewerTab(png_bytes(16, 16), "stone.png")
    for _ in range(20):
        tab.zoom_out()
    assert tab.zoom == ZOOM_STEPS[0]
    for _ in range(20):
        tab.zoom_in()
    assert tab.zoom == ZOOM_STEPS[-1]


def test_the_canvas_grows_with_the_zoom(qapp):
    tab = ImageViewerTab(png_bytes(16, 16), "stone.png")
    before = tab._canvas.width()
    tab.zoom_in()
    assert tab._canvas.width() > before


# --- animation strips ---------------------------------------------------------------------

@pytest.mark.parametrize("width, height, expected", [
    (16, 16, "16x16"),
    (16, 256, "16x256 — 16 frames of 16x16"),      # acid_still.png, from a real mod
    (32, 512, "32x512 — 16 frames of 32x32"),      # acid_flowing.png
    (64, 32, "64x32"),                             # wider than tall: not a strip
    (16, 24, "16x24"),                             # tall but not a multiple
])
def test_animation_strips_are_recognised(width, height, expected):
    """Minecraft animates by stacking frames vertically, so a tall exact multiple is
    almost always an animation rather than a strangely shaped picture."""
    assert describe_dimensions(width, height) == expected


def test_a_zero_width_image_does_not_divide_by_zero():
    assert describe_dimensions(0, 10) == "0x10"


# --- dispatch ------------------------------------------------------------------------------

def test_images_are_classified_to_the_viewer():
    assert filetypes.classify("block/stone.png", png_bytes(16, 16)[:64]) == filetypes.IMAGE


def test_an_image_inside_a_jar_reaches_the_viewer_without_extraction(tmp_path, qapp):
    """The point of taking bytes rather than a path: a texture on disk and a texture inside
    a jar are the same case, and the jar one is never unpacked."""
    import zipfile

    from packsmith.gui.editor.sources import JarMemberSource, member_path

    (tmp_path / "mods").mkdir()
    with zipfile.ZipFile(tmp_path / "mods" / "m.jar", "w") as archive:
        archive.writestr("assets/m/textures/block/stone.png", png_bytes(16, 16, alpha=True))

    source = JarMemberSource(tmp_path)
    path = member_path("mods/m.jar", "assets/m/textures/block/stone.png")
    before = {p for p in tmp_path.rglob("*")}

    data = source.raw(path)
    tab = ImageViewerTab(data, "stone.png")

    assert tab.ok and "16x16" in tab._info.text()
    assert {p for p in tmp_path.rglob("*")} == before, "the texture was extracted"


# --- zoom keeps its bearings ---------------------------------------------------------------

def _image_point_under(tab, anchor):
    """Which image pixel currently sits under a point in the viewport."""
    horizontal = tab._scroll.horizontalScrollBar()
    vertical = tab._scroll.verticalScrollBar()
    offset = tab._offset()
    return ((horizontal.value() + anchor.x() - offset.x()) / tab.zoom,
            (vertical.value() + anchor.y() - offset.y()) / tab.zoom)


def _settle(qapp):
    for _ in range(3):
        qapp.processEvents()


def _big_tab(qapp):
    tab = ImageViewerTab(png_bytes(256, 256), "atlas.png")
    tab.resize(500, 400)
    tab.show()
    _settle(qapp)
    tab._zoom_index = 1
    tab._apply_zoom()
    _settle(qapp)
    return tab


@pytest.mark.parametrize("anchor", [QPoint(250, 190), QPoint(150, 120), QPoint(380, 300)])
def test_the_pixel_under_the_cursor_stays_under_the_cursor(qapp, anchor):
    """Zoom grew the canvas from its top-left while the scroll position stayed put, so the
    pixel you were looking at slid away — at 16x it left the viewport after one step, which
    makes zoom useless for the thing zoom is for."""
    tab = _big_tab(qapp)
    for _ in range(3):
        before = _image_point_under(tab, anchor)
        tab.zoom_in(anchor)
        _settle(qapp)
        after = _image_point_under(tab, anchor)
        assert abs(after[0] - before[0]) < 1, f"drifted horizontally at {tab.zoom}x"
        assert abs(after[1] - before[1]) < 1, f"drifted vertically at {tab.zoom}x"


def test_zooming_back_out_holds_the_anchor_too(qapp):
    tab = _big_tab(qapp)
    anchor = QPoint(300, 200)
    for _ in range(3):
        tab.zoom_in(anchor)
    _settle(qapp)
    for _ in range(3):
        before = _image_point_under(tab, anchor)
        tab.zoom_out(anchor)
        _settle(qapp)
        after = _image_point_under(tab, anchor)
        assert abs(after[0] - before[0]) < 1, f"drifted at {tab.zoom}x"


def test_the_buttons_anchor_on_the_centre(qapp):
    """A button in the toolbar says nothing about where you were looking, so the only
    sensible anchor is what is already in the middle of the view."""
    tab = _big_tab(qapp)
    centre = QPoint(tab._scroll.viewport().width() // 2,
                    tab._scroll.viewport().height() // 2)
    before = _image_point_under(tab, centre)
    tab.zoom_in()                       # no anchor, as the button sends it
    _settle(qapp)
    after = _image_point_under(tab, centre)
    assert abs(after[0] - before[0]) < 1 and abs(after[1] - before[1]) < 1


def test_a_fast_wheel_spin_still_lands_on_the_anchor(qapp):
    """Several notches arrive faster than the event loop drains them, so each one used to
    compute from a scroll position the previous one had not applied yet — which is the
    normal way anybody zooms."""
    tab = _big_tab(qapp)
    anchor = QPoint(320, 240)
    before = _image_point_under(tab, anchor)

    for _ in range(3):
        tab.zoom_in(anchor)          # no settling between: a real wheel spin
    _settle(qapp)

    after = _image_point_under(tab, anchor)
    assert abs(after[0] - before[0]) < 1 and abs(after[1] - before[1]) < 1
