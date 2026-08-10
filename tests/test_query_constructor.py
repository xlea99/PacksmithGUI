"""The ⚙ constructor as a faithful front-end (design 3.2.3).

§3.2.4's "Why Not SQL" decision record makes two-way binding a hard requirement: "you
cannot reliably lift arbitrary SQL back into the builder." That argument only holds if the
builder is honest about what it *can* lift — a builder that silently rewrites what it
doesn't understand is worse than SQL, because SQL at least fails loudly.

The invariant under test: **open a filter in the dialog, press OK, get the same filter.**
"""
import os

import pytest

# Widgets need a QApplication, and the suite must stay runnable with a bare `pytest` on a
# machine with no display — so the platform is forced here rather than in the environment.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.query.ast import Query, Registry, Id
from packsmith.core.query.language import parse
from packsmith.gui.query_constructor import QueryConstructorDialog

REG = "minecraft:item"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    # A modal box has no one to dismiss it here and blocks the run forever. Recording the
    # calls instead also lets a test assert that the user was actually told.
    QMessageBox.warning = staticmethod(lambda *a, **k: _WARNINGS.append(a[2] if len(a) > 2 else a))
    yield app


_WARNINGS = []


@pytest.fixture
def tag_store(tags):
    tags.define(REG, "tier", "enum", enum_values=["early", "late"])
    tags.define(REG, "remove", "bool")
    tags.define(REG, "weight", "number")
    return tags


def open_and_ok(tag_store, text):
    node = parse(text)
    dlg = QueryConstructorDialog(tag_store, REG,
                                 query=Query(scope=Registry(REG), select=[Id],
                                             filter=node))
    mode = "text" if getattr(dlg, "_text_edit", None) is not None else "rows"
    dlg.accept()
    return node, dlg.result_filter, mode


@pytest.mark.parametrize("text", [
    't:tier == "early"',
    't:tier != "early"',
    'HAS t:remove',
    'NOT HAS t:remove',
    't:weight > 5',
    't:tier == "early" AND HAS t:remove',
    't:tier IN ("early", "late")',
    't:tier NOT IN ("early")',
])
def test_a_representable_filter_survives_the_dialog(tag_store, text):
    original, result, mode = open_and_ok(tag_store, text)
    assert result == original
    assert mode == "rows", "this one should still be visually editable"


@pytest.mark.parametrize("text", [
    "granite brick",                       # token match from the filter bar
    "granite AND (brick OR wall)",         # nested group
    "NOT (granite OR brick)",
])
def test_an_unrepresentable_filter_is_preserved_via_text(tag_store, text):
    """The reported bug: `granite brick` → Keep → ⚙ → OK saved
    Cmp(Id, 'eq', "['brick', 'granite']"). Silently."""
    original, result, mode = open_and_ok(tag_store, text)
    assert result == original
    assert mode == "text", "the rows cannot hold this, so it must not pretend to"


def test_the_fidelity_check_asks_the_widgets_rather_than_a_list(tag_store):
    """No hand-maintained table of what the builder supports — it presets the rows and
    reads them straight back. A capability list would drift from the widgets; this can't."""
    node = parse("granite brick")
    dlg = QueryConstructorDialog(tag_store, REG,
                                 query=Query(scope=Registry(REG), select=[Id],
                                             filter=node))
    assert dlg._text_edit.text() == "brick granite"


def test_editing_the_text_is_honoured(tag_store):
    node = parse("granite brick")
    dlg = QueryConstructorDialog(tag_store, REG,
                                 query=Query(scope=Registry(REG), select=[Id],
                                             filter=node))
    dlg._text_edit.setText('t:tier == "late"')
    dlg.accept()
    assert dlg.result_filter == parse('t:tier == "late"')


def test_unparseable_text_refuses_to_save_rather_than_dropping_the_filter(tag_store):
    node = parse("granite brick")
    dlg = QueryConstructorDialog(tag_store, REG,
                                 query=Query(scope=Registry(REG), select=[Id],
                                             filter=node))
    _WARNINGS.clear()
    dlg._text_edit.setText("granite AND AND")
    dlg.accept()
    assert _WARNINGS, "the user must be told, not silently ignored"
    assert dlg.result_filter == node, "the original must survive a rejected edit"


def test_clearing_the_text_means_no_filter(tag_store):
    node = parse("granite brick")
    dlg = QueryConstructorDialog(tag_store, REG,
                                 query=Query(scope=Registry(REG), select=[Id],
                                             filter=node))
    dlg._text_edit.setText("")
    dlg.accept()
    assert dlg.result_filter is None
