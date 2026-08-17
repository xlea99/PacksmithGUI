"""Two-line rows in the picker (`gui/shell/picker.py`).

The detail line is drawn a notch smaller than the label, and both things that can go wrong
there are arithmetic rather than painting — so they are tested as arithmetic, against an
explicit font. Measuring a real widget instead would make these depend on whatever the
application font happens to be, which any other test file can change.

Both failures are quiet. Truncated text with an ellipsis looks like text that was simply
too long; nothing about it says the widget threw away space it had, or that it rendered at
a size nobody chose.
"""
import os

import pytest
from PySide6.QtGui import QFont, QFontMetrics

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.gui.shell.picker import _TwoLineDelegate

# Long enough to require eliding at every width tested, so "it fits entirely" can never
# be mistaken for "it filled the row".
LONG = ("i_wish_i_knew_why_everybody_glazes_kettles_but_the_answer_when_it_arrives "
        "at last subsequently reveals more to know, and then some more after that, "
        "and a good deal more besides, on and on well past any reasonable width. " * 6)


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def points(size=10):
    font = QFont("Arial")
    font.setPointSize(size)
    return font


@pytest.mark.parametrize("width", [200, 400, 900])
def test_the_detail_line_fills_the_width_it_is_given(width, qapp):
    """It used to elide with `option.fontMetrics` — the metrics of the DEFAULT font, while
    being drawn a notch smaller. The string measured a third wider than it would render,
    so the line stopped well short of the edge with room to spare beside it. Measured on a
    real row before the fix: 63%, at every width."""
    font = points()
    drawn = _TwoLineDelegate.detail_text(font, LONG, width)
    painted = QFontMetrics(_TwoLineDelegate._small(font)).horizontalAdvance(drawn)

    assert painted <= width, "it overflowed the row"
    assert painted / width > 0.9, f"only used {painted / width:.0%} of the space"


def test_a_wider_row_shows_more_of_the_description(qapp):
    """Dragging the splitter wider has to buy more text, not a bigger gap after the same
    short line."""
    font = points()
    short = _TwoLineDelegate.detail_text(font, LONG, 200)
    wide = _TwoLineDelegate.detail_text(font, LONG, 600)

    assert len(wide) > len(short)
    # A prefix short enough to sit well clear of the narrow row's own ellipsis.
    assert wide.startswith(short[:10])          # the same text, just more of it


def test_a_pixel_sized_font_is_not_collapsed_to_six_point(qapp):
    """The bug this file found on its way in.

    A QFont carries EITHER a point size or a pixel size; the other reads as -1. Qt's
    default here is pixel-sized, so `setPointSizeF(pointSizeF() - 1)` evaluated
    `max(6.0, -2.0)` and rendered every detail line at **6pt** — a size nobody chose, and
    one that silently changed depending on what set the application font last.
    """
    pixel = QFont("Arial")
    pixel.setPixelSize(12)
    small = _TwoLineDelegate._small(pixel)

    assert small.pixelSize() == 11
    assert small.pointSizeF() < 0, "a pixel-sized font must not become point-sized"


def test_a_point_sized_font_steps_down_by_a_point(qapp):
    small = _TwoLineDelegate._small(points(10))

    assert small.pointSizeF() == pytest.approx(9.0)
    assert small.pixelSize() < 0
