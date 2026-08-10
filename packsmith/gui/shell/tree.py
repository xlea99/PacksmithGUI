"""The shared tree widget for panels — a QTreeWidget that draws its own expanders.

Qt's native branch indicators disappear the moment a stylesheet touches ``::branch``:
styling the sub-control replaces its rendering wholesale, and ``LIST_QSS`` sets a branch
background so selection doesn't bleed into the gutter. The usual fix is
``::branch { image: url(...) }``, but QSS loads indicator images only from files or Qt
resources — a build step and two shipped assets for what is, at this stage, a triangle.

So we paint them. ``drawBranches`` is the exact hook, the colours come from the shell
palette, and it stays correct if the palette changes later. Placeholder-grade on purpose;
the icon pass (§4.1) will revisit it.
"""
from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPolygonF
from PySide6.QtWidgets import QTreeWidget

from packsmith.gui.shell import style

_ARROW = 3.4        # half-width of the chevron, in px
_INSET = 1.0        # nudges the flat edge off-centre so it reads as pointing


class PanelTree(QTreeWidget):
    """A panel tree with visible expanders. Drop-in for ``QTreeWidget()``."""

    def __init__(self, parent=None, extra_qss=""):
        super().__init__(parent)
        self.setStyleSheet(style.LIST_QSS + extra_qss)

    def mousePressEvent(self, event):
        """Clicking empty space clears the selection.

        Qt's default is to keep whatever was selected, which leaves a tree with no way to
        select *nothing* — and "nothing" is a real answer whenever selection means
        context. In the blueprint schema tree it means "the top level", so without this
        you can't add a root slot once you've clicked into a group.
        """
        if self.itemAt(event.position().toPoint()) is None:
            self.clearSelection()
            self.setCurrentItem(None)
        super().mousePressEvent(event)

    def drawBranches(self, painter: QPainter, rect: QRect, index):
        # Let the stylesheet paint the gutter first; the marker goes on top of it.
        super().drawBranches(painter, rect, index)
        model = self.model()
        if model is None or not index.isValid() or not model.hasChildren(index):
            return

        # The indicator belongs in the last indent slot — the one immediately left of the
        # item's own text, whatever depth it sits at.
        slot = QRect(rect.x() + rect.width() - self.indentation(), rect.y(),
                     self.indentation(), rect.height())
        cx = slot.center().x() + 1.0
        cy = slot.center().y() + 1.0

        if self.isExpanded(index):
            points = [QPointF(cx - _ARROW, cy - _INSET),
                      QPointF(cx + _ARROW, cy - _INSET),
                      QPointF(cx, cy + _ARROW)]
        else:
            points = [QPointF(cx - _INSET, cy - _ARROW),
                      QPointF(cx - _INSET, cy + _ARROW),
                      QPointF(cx + _ARROW, cy)]

        colour = style.TEXT if index == self.currentIndex() else style.TEXT_MUTED
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawPolygon(QPolygonF(points))
        painter.restore()
