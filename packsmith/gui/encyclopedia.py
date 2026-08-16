"""The Encyclopedia Packsmithia — an in-app guide, as a tab.

A tab rather than a dialog, deliberately. Help you have to dismiss before you can act on it
is help you read once and then re-open; a tab sits beside the thing you were doing, keeps
its place, and closes when you are done with it like anything else.

Two panes: the contents on the left, the page on the right. Pages are plain HTML from
`encyclopedia_pages`, and links between them use a ``page:`` scheme the reader resolves
itself — nothing here touches the network, and there is no browser to get lost in.
"""
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QSplitter, QTextBrowser, QTreeWidget, QTreeWidgetItem, QWidget,
)

from packsmith.gui.encyclopedia_pages import CATEGORIES, PAGES
from packsmith.gui.shell import style
from packsmith.gui.shell.tree import PanelTree

_PAGE_ROLE = Qt.UserRole

_DOC_QSS = f"""
    QTextBrowser {{
        background: {style.BG_DEEP}; color: {style.TEXT};
        border: none; padding: 18px 26px; font-size: 13px;
    }}
"""

# Applied to every page. Qt's rich text is a subset of HTML 4 with its own quirks — it has
# no `rem`, no flexbox and no CSS variables — so this stays deliberately plain.
_PAGE_CSS = f"""
<style>
  body {{ color: {style.TEXT}; line-height: 150%; }}
  h1 {{ color: {style.TEXT}; font-size: 20px; margin: 0 0 2px 0; }}
  h2 {{ color: {style.TEXT}; font-size: 14px; margin: 22px 0 4px 0; }}
  p  {{ margin: 8px 0; }}
  a  {{ color: {style.ACCENT_EDGE}; text-decoration: none; }}
  code {{ color: #d8b26a; }}
  pre {{ background: {style.BG_PANEL}; color: #d8b26a;
         padding: 8px 12px; margin: 8px 0; }}
  td {{ padding: 3px 14px 3px 0; }}
  i  {{ color: {style.TEXT_MUTED}; }}
</style>
"""


class EncyclopediaTab(QWidget):
    """The guide. `show_page(id)` is the deep-link entry point."""

    page_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self._contents = PanelTree()
        self._contents.setHeaderHidden(True)
        self._contents.setStyleSheet(style.LIST_QSS)
        self._contents.setFixedWidth(210)
        self._contents.itemClicked.connect(self._on_picked)

        self._doc = QTextBrowser()
        self._doc.setStyleSheet(_DOC_QSS)
        self._doc.setOpenLinks(False)          # `page:` links are ours to resolve
        self._doc.setOpenExternalLinks(False)
        self._doc.anchorClicked.connect(self._on_link)

        split = QSplitter(Qt.Horizontal)
        split.setStyleSheet(style.SPLITTER_QSS)
        split.setHandleWidth(style.SPLITTER_WIDTH)
        split.addWidget(self._contents)
        split.addWidget(self._doc)
        split.setStretchFactor(1, 1)
        lay.addWidget(split)

        self._items = {}
        self._build_contents()
        first = CATEGORIES[0].pages[0].id
        self.show_page(first)

    def _build_contents(self):
        for category in CATEGORIES:
            heading = QTreeWidgetItem([category.title])
            # A category is a heading, not a destination — there is no page behind it, so
            # it carries no id and clicking it opens nothing.
            heading.setFlags(heading.flags() & ~Qt.ItemIsSelectable)
            heading.setForeground(0, style.qt_colour(style.TEXT_MUTED))
            self._contents.addTopLevelItem(heading)
            for page in category.pages:
                item = QTreeWidgetItem([page.title])
                item.setData(0, _PAGE_ROLE, page.id)
                item.setToolTip(0, page.summary or page.title)
                heading.addChild(item)
                self._items[page.id] = item
            heading.setExpanded(True)

    # --- navigation --------------------------------------------------------

    def show_page(self, page_id: str) -> bool:
        """Render a page and select it in the contents. False if there is no such page."""
        page = PAGES.get(page_id)
        if page is None:
            return False
        self._doc.setHtml(_PAGE_CSS + page.body)
        self._doc.verticalScrollBar().setValue(0)
        item = self._items.get(page_id)
        if item is not None:
            # Selecting fires `itemClicked`? No — only user clicks do. But the contents
            # and the page must not disagree after a deep link from elsewhere in the app.
            self._contents.setCurrentItem(item)
        self.page_changed.emit(page_id)
        return True

    def _on_picked(self, item, _column=0):
        page_id = item.data(0, _PAGE_ROLE)
        if page_id:
            self.show_page(page_id)

    def _on_link(self, url: QUrl):
        """Resolve a ``page:`` link internally.

        Anything else is ignored rather than handed to a browser: an offline tool that
        silently opens a web page is doing something the user did not ask for, and every
        link in here is one of ours.
        """
        target = url.toString()
        if target.startswith("page:"):
            self.show_page(target[len("page:"):])
