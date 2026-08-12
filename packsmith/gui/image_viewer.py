"""Viewing an image — design 6.0's editor dispatch, for textures.

Read-only by nature rather than by policy: there is no image editor here and there is not
going to be one. What this is for is *looking* — at a block texture, a GUI sheet, a pack
icon — which on a modpack is something you do constantly and currently need a second
application for.

**Built for pixel art, not photographs**, because that is what a pack contains. Of 381
textures sampled from eight real mods, 229 were 16x16 and 285 had an alpha channel. Two
consequences drive the whole widget:

- **Nearest-neighbour magnification.** Qt's smooth scaling is right for a photograph and
  ruinous for a 16x16 texture — it turns deliberate pixels into a blur. Minification still
  smooths, which is correct for the same reason: a 1024px GUI sheet shrunk to fit is being
  sampled, not magnified.
- **A checkerboard behind transparency.** Three quarters of textures have alpha, and on a
  flat background transparent is indistinguishable from black — which for a texture is
  usually the exact thing you are trying to see.

It takes **bytes**, not a path, which is why it works unchanged for a file on disk and for
a member read out of a jar (design 6.5). Nothing is extracted for the latter.
"""
from math import ceil

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
)

from packsmith.gui.shell import style

# Integer steps only. A fractional zoom reintroduces exactly the blur that nearest-neighbour
# was chosen to avoid, because a pixel would land across two.
ZOOM_STEPS = (1, 2, 4, 8, 16, 32)
_CHECKER = 8            # px per checkerboard square


def _checker_tile() -> QPixmap:
    """One 2x2-square tile, for a QBrush to repeat.

    Qt tiles a brush in C++; drawing the same pattern from Python costs one call per
    square, which at high zoom is hundreds of thousands of them per frame.
    """
    tile = QPixmap(_CHECKER * 2, _CHECKER * 2)
    tile.fill(QColor("#2e2e2e"))
    painter = QPainter(tile)
    light = QColor("#3a3a3a")
    painter.fillRect(0, 0, _CHECKER, _CHECKER, light)
    painter.fillRect(_CHECKER, _CHECKER, _CHECKER, _CHECKER, light)
    painter.end()
    return tile


def describe_dimensions(width: int, height: int) -> str:
    """``"16x16"``, or ``"16x256 — 16 frames of 16x16"`` for an animation strip.

    Minecraft animates a texture by stacking its frames vertically, so a tall exact
    multiple is almost always an animation rather than a strangely shaped picture. Saying
    so turns a confusing smear into something legible; playing it needs the `.mcmeta`
    sidecar for frame timing, which is a later concern.
    """
    base = f"{width}x{height}"
    if height > width and width and height % width == 0:
        return f"{base} — {height // width} frames of {width}x{width}"
    return base


class _Canvas(QWidget):
    """Paints the image over a checkerboard, at an integer zoom."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._zoom = 1.0
        self._brush = QBrush(_checker_tile())
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_image(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self._resize()

    def set_zoom(self, zoom: float):
        self._zoom = zoom
        self._resize()

    def _resize(self):
        if self._pixmap.isNull():
            self.setFixedSize(QSize(1, 1))
            return
        self.setFixedSize(QSize(max(1, int(self._pixmap.width() * self._zoom)),
                                max(1, int(self._pixmap.height() * self._zoom))))
        self.update()

    def paintEvent(self, event):
        """Paint only what is exposed, and let Qt tile the checkerboard.

        Both halves matter once you zoom in far enough to compare individual pixels, which
        is the whole reason 32x exists. A 128px texture at 32x is a 4096x4096 canvas:
        drawing the checkerboard with a Python loop over 8px cells is 263,000 fillRect
        calls (measured at 65ms a frame, on every scroll), and scaling the entire pixmap
        when a viewport-sized sliver is visible pays for 4000 rows nobody can see.

        A tiled QBrush moves the first into Qt's C++ loop, and `event.rect()` reduces the
        second to the handful of source pixels actually on screen.
        """
        painter = QPainter(self)
        area = event.rect()
        painter.fillRect(area, self._brush)
        if self._pixmap.isNull():
            return

        zoom = self._zoom
        # The source pixels under the exposed region, rounded outward so partially covered
        # pixels at the edges are still drawn.
        left = max(0, int(area.left() / zoom))
        top = max(0, int(area.top() / zoom))
        right = min(self._pixmap.width(), ceil((area.right() + 1) / zoom))
        bottom = min(self._pixmap.height(), ceil((area.bottom() + 1) / zoom))
        if right <= left or bottom <= top:
            return
        source = QRectF(left, top, right - left, bottom - top)
        target = QRectF(left * zoom, top * zoom,
                        (right - left) * zoom, (bottom - top) * zoom)

        # Nearest when magnifying, smooth when shrinking. Both are "show me what is really
        # there" for the case they apply to.
        painter.setRenderHint(QPainter.SmoothPixmapTransform, zoom < 1)
        painter.drawPixmap(target, self._pixmap, source)


class ImageViewerTab(QWidget):
    """A zoomable, read-only look at one image."""

    status = Signal(str)

    def __init__(self, data: bytes, label: str, parent=None):
        super().__init__(parent)
        self.label = label
        self._image = QImage.fromData(data)
        self._zoom_index = 0
        self._pending = None      # (image_x, image_y, anchor) awaiting a scroll

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 6, 8, 6)
        bar_lay.setSpacing(6)
        for text, tip, slot in (("−", "Zoom out", self.zoom_out),
                                ("+", "Zoom in", self.zoom_in)):
            button = QPushButton(text)
            button.setFixedWidth(26)
            button.setToolTip(tip)
            button.setStyleSheet(f"""
                QPushButton {{
                    background: {style.BG_CHROME}; color: {style.TEXT_MUTED};
                    border: 1px solid {style.BORDER}; font-size: 12px; padding: 1px 4px;
                }}
                QPushButton:hover {{ color: {style.TEXT}; }}
            """)
            # `clicked` passes a bool, which would arrive as the anchor.
            button.clicked.connect(lambda _checked=False, fn=slot: fn())
            bar_lay.addWidget(button)
        self._zoom_label = QLabel()
        self._zoom_label.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        bar_lay.addWidget(self._zoom_label)
        bar_lay.addStretch()
        self._info = QLabel()
        self._info.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        bar_lay.addWidget(self._info)
        root.addWidget(bar)

        self._canvas = _Canvas()
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._canvas)
        self._scroll.setAlignment(Qt.AlignCenter)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {style.BG_DEEP}; border: none; }}")
        self._scroll.viewport().installEventFilter(self)
        root.addWidget(self._scroll)

        if self._image.isNull():
            # A truncated texture is a real thing to find in a jar. Say so rather than
            # showing an empty frame that looks like a bug in the viewer.
            self._info.setText("could not be decoded")
            self.status.emit(f"{label} — not a readable image")
            return

        self._canvas.set_image(QPixmap.fromImage(self._image))
        # "has an alpha channel", not "has transparency": Qt answers about the FORMAT, and
        # a fully opaque RGBA texture would make the stronger claim a lie. The checkerboard
        # shows which it is at a glance, so the label doesn't need to scan four million
        # pixels to find out.
        self._info.setText(
            f"{describe_dimensions(self._image.width(), self._image.height())}"
            f"{'  ·  alpha channel' if self._image.hasAlphaChannel() else ''}")
        self._zoom_index = self._default_zoom_index()
        self._apply_zoom()

    # --- zoom --------------------------------------------------------------

    def _default_zoom_index(self) -> int:
        """Open at the largest integer zoom that still fits.

        A 16x16 texture at 1:1 is a postage stamp on any modern monitor — opening there
        would mean every single texture needs the same four clicks before it is useful.
        """
        available = self._scroll.viewport().size()
        width = max(available.width() - 24, 200)
        height = max(available.height() - 24, 200)
        best = 0
        for index, zoom in enumerate(ZOOM_STEPS):
            if (self._image.width() * zoom <= width
                    and self._image.height() * zoom <= height):
                best = index
        return best

    def _apply_zoom(self, anchor=None, previous=None):
        """Change zoom, keeping one point of the IMAGE where it already was on screen.

        Without this, zooming grows the canvas from its top-left and the scroll position
        stays put, so the pixel you were looking at slides away — at 32x it leaves the
        viewport entirely after one step, which makes zoom useless for the thing zoom is
        for.

        ``anchor`` is a point in viewport coordinates to hold still. The wheel passes the
        cursor, because pointing at a pixel *is* the statement of which pixel you care
        about. The buttons pass nothing and get the viewport centre, because a button in
        the toolbar says nothing about where you were looking.
        """
        # The zoom we are coming FROM, passed in by the caller. Reading it from
        # `_zoom_index` here would read the zoom we are going *to* — the index has already
        # moved by the time this runs — which does the anchor maths in the wrong scale and
        # lands every zoom back at the top-left corner.
        before = previous if previous is not None else ZOOM_STEPS[self._zoom_index]
        horizontal = self._scroll.horizontalScrollBar()
        vertical = self._scroll.verticalScrollBar()
        viewport = self._scroll.viewport()
        if anchor is None:
            anchor = QPoint(viewport.width() // 2, viewport.height() // 2)

        # Which point of the image currently sits under the anchor. `_offset` accounts for
        # the centring the scroll area applies while the canvas is smaller than the view.
        #
        # When a scroll is still pending for this same anchor, reuse ITS target instead of
        # reading the scrollbars: the pending value hasn't been applied yet, so the bars
        # still describe the position from before the last zoom. Spinning a wheel produces
        # several notches faster than the event loop drains, which made every notch after
        # the first compute from a stale position. And the reused point is exactly right by
        # definition — it is the image point we are in the middle of putting under this
        # anchor.
        if self._pending is not None and self._pending[2] == anchor:
            image_x, image_y = self._pending[0], self._pending[1]
        else:
            image_x = (horizontal.value() + anchor.x() - self._offset().x()) / before
            image_y = (vertical.value() + anchor.y() - self._offset().y()) / before
        self._pending = (image_x, image_y, anchor)

        zoom = ZOOM_STEPS[self._zoom_index]
        self._canvas.set_zoom(zoom)
        self._zoom_label.setText(f"{zoom * 100}%")

        # Applied on the next turn, not now. The scroll area recomputes its ranges when it
        # processes the canvas resize — which happens after this returns — and that pass
        # overwrites anything set here, silently leaving the position at zero. Deferring by
        # one turn puts the scroll after the recompute instead of before it.
        QTimer.singleShot(0, lambda: self._centre_on(image_x, image_y, anchor, zoom))

    def _centre_on(self, image_x, image_y, anchor, zoom):
        """Scroll so the given image point sits under ``anchor`` again.

        Clamped by the scrollbars, which is correct: near an edge the image simply cannot
        move far enough, and every viewer behaves this way.
        """
        self._pending = None
        try:
            offset = self._offset()
            self._scroll.horizontalScrollBar().setValue(
                round(image_x * zoom + offset.x() - anchor.x()))
            self._scroll.verticalScrollBar().setValue(
                round(image_y * zoom + offset.y() - anchor.y()))
        except RuntimeError:
            pass            # the tab closed before the turn came round

    def _offset(self) -> QPoint:
        """How far the canvas is inset inside the viewport by centre alignment. Zero once
        the image is larger than the view, which is whenever anchoring matters most."""
        viewport = self._scroll.viewport()
        return QPoint(max(0, (viewport.width() - self._canvas.width()) // 2),
                      max(0, (viewport.height() - self._canvas.height()) // 2))

    def zoom_in(self, anchor=None):
        if self._zoom_index < len(ZOOM_STEPS) - 1:
            previous = ZOOM_STEPS[self._zoom_index]
            self._zoom_index += 1
            self._apply_zoom(anchor, previous)

    def zoom_out(self, anchor=None):
        if self._zoom_index > 0:
            previous = ZOOM_STEPS[self._zoom_index]
            self._zoom_index -= 1
            self._apply_zoom(anchor, previous)

    def eventFilter(self, watched, event):
        """Take Ctrl+wheel from the scroll area before it scrolls with it.

        This was a `wheelEvent` on the tab, which worked only by accident: a wheel event
        reaches the parent solely when the child ignores it, and a QScrollArea ignores one
        exactly while it has nowhere to scroll. So zooming *in* worked — until the image
        outgrew the viewport, at which point the scroll area started consuming the event
        and Ctrl+wheel-down silently became a scroll. Filtering the viewport gets the
        event first, whatever the scrollbars happen to be doing.
        """
        if (watched is self._scroll.viewport() and event.type() == QEvent.Wheel
                and event.modifiers() & Qt.ControlModifier):
            # The cursor is the anchor: pointing at a pixel is the statement of which
            # pixel you care about.
            at = event.position().toPoint()
            self.zoom_in(at) if event.angleDelta().y() > 0 else self.zoom_out(at)
            return True                  # consumed: do not also scroll
        return super().eventFilter(watched, event)

    # --- for tests and callers ---------------------------------------------

    @property
    def zoom(self) -> int:
        return ZOOM_STEPS[self._zoom_index]

    @property
    def ok(self) -> bool:
        return not self._image.isNull()
