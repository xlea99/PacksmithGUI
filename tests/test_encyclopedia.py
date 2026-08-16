"""The Encyclopedia Packsmithia — design 4.1's Help surface.

A skeleton with one real page. What is worth pinning is not the prose but the wiring: that
a page can be linked to **by id** from anywhere in the app, that links resolve internally
rather than reaching the network, and that the one written page stays true to the parser
it documents.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl

from packsmith.gui.encyclopedia import EncyclopediaTab
from packsmith.gui.encyclopedia_pages import CATEGORIES, FILTER_HELP, PAGES


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def guide(qapp):
    tab = EncyclopediaTab()
    yield tab
    tab.deleteLater()


# --- the wiring ------------------------------------------------------------------------

def test_every_page_is_reachable_by_id(guide):
    """Deep-linking is the whole point of pages-as-data; a page nothing can address is a
    page the app cannot send you to."""
    for page_id in PAGES:
        assert guide.show_page(page_id), page_id


def test_an_unknown_page_is_refused_rather_than_blanking(guide):
    assert guide.show_page("no-such-page") is False


def test_internal_links_resolve_without_leaving_the_app(guide):
    """`page:` links are ours. Anything else is ignored rather than handed to a browser —
    an offline tool that opens a web page is doing something nobody asked for."""
    guide._on_link(QUrl("page:tags"))
    assert guide._contents.currentItem().data(0, 0) is not None

    guide.show_page("views")
    guide._on_link(QUrl("https://example.com"))          # must not raise, must not navigate
    assert "views" in _current(guide)


def test_the_contents_follows_a_deep_link(guide):
    """Arriving from the ? beside the filter bar has to select the page in the contents
    too, or the sidebar says you are somewhere you are not."""
    guide.show_page("blueprints")
    assert guide._contents.currentItem().text(0) == "Blueprints"


def test_a_category_heading_is_not_a_destination(guide):
    """There is no page behind a heading, so it must not be selectable — clicking one and
    getting nothing reads as a broken link."""
    from PySide6.QtCore import Qt
    heading = guide._contents.topLevelItem(0)
    assert heading.data(0, Qt.UserRole) is None
    assert not (heading.flags() & Qt.ItemIsSelectable)


def _current(guide):
    item = guide._contents.currentItem()
    from PySide6.QtCore import Qt
    return item.data(0, Qt.UserRole) if item else ""


# --- the one real page -------------------------------------------------------------------

def test_the_filter_help_target_exists():
    """The ? beside the filter bar points here by name. A dangling target would be a
    button that silently does nothing."""
    assert FILTER_HELP in PAGES


def test_the_filter_page_documents_the_operators_the_parser_accepts():
    """A reference that disagrees with the parser is worse than none — it is confidently
    wrong exactly when someone is stuck. Checked against the language module rather than
    against memory.
    """
    from packsmith.core.query import language

    body = PAGES[FILTER_HELP].body
    spellings = set(language._WORD_OPS) | set(language._CI_WORD_OPS)
    missing = [op for op in spellings if op.upper() not in body.upper()]
    assert missing == [], f"undocumented operators: {missing}"


def test_the_filter_page_covers_has():
    """`NOT HAS` is the gap-finding idiom the whole engine is built around, and it is
    invisible to anyone who only ever compares values."""
    body = PAGES[FILTER_HELP].body
    assert "HAS" in body and "NOT HAS" in body


def test_every_field_prefix_is_documented():
    body = PAGES[FILTER_HELP].body
    for prefix in ("t:", "a:", "b:", "id", "mod"):
        assert prefix in body, prefix


def test_internal_links_in_pages_point_somewhere_real():
    """A skeleton is allowed to be mostly stubs; it is not allowed to link into space."""
    import re
    for page in PAGES.values():
        for target in re.findall(r'href="page:([^"]+)"', page.body):
            assert target in PAGES, f"{page.id} links to missing page '{target}'"
