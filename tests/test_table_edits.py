"""Registry table editing details (design 3.2.1, 5.1)."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.query.ast import Id, Query, Registry, Tag
from packsmith.gui.table.registry_table_model import RegistryTableModel

REG = "r"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class Dump:
    registry = {REG: {"values": ["a", "b"]}}

    def attribute(self, *a):
        return None


@pytest.fixture
def model(tags):
    tags.define(REG, "remove", "bool", default=False)
    # No `set_editing` — tag columns are editable because they are tag columns. The
    # per-column arming mode this used to need predated both systems that now do that
    # job: §3.2.1's ownership rules for the conflict that matters, and undo for the
    # misclick that doesn't.
    m = RegistryTableModel(Query(scope=Registry(REG), select=[Id, Tag("remove")]),
                           Dump(), tags)
    return m, tags


def test_a_user_can_assert_the_default_deliberately(model):
    """"Explicitly false" and "pristine, defaulting to false" are different states, and
    §3.2.1 keeps the distinction — so choosing the default must be a real edit, not a
    no-op. (Closed as a side effect of the TBL-1 fix; pinned here so it stays closed.)"""
    from PySide6.QtCore import Qt
    m, tags = model
    assert tags.assignment(REG, "a", "remove") is None
    assert m.setData(m.index(0, 1), False, Qt.EditRole) is True
    stored = tags.assignment(REG, "a", "remove")
    assert stored is not None and stored.value is False and stored.owner == "user"


def test_re_asserting_the_same_stored_value_is_still_a_no_op(model):
    from PySide6.QtCore import Qt
    m, tags = model
    m.setData(m.index(0, 1), True, Qt.EditRole)
    assert m.setData(m.index(0, 1), True, Qt.EditRole) is False


def test_bools_render_and_copy_as_true_false(model):
    """§3.2.1 spells them `true`/`false`; DisplayRole is what Copy puts on the clipboard,
    so `str(True)` leaked Python's spelling into external TSV."""
    from PySide6.QtCore import Qt
    m, tags = model
    tags.assign(REG, "a", "remove", True)
    tags.assign(REG, "b", "remove", False)
    m.reevaluate()
    shown = {m.data(m.index(r, 0), Qt.DisplayRole): m.data(m.index(r, 1), Qt.DisplayRole)
             for r in range(m.rowCount())}
    assert shown == {"a": "true", "b": "false"}


def test_a_tag_cell_is_editable_without_arming_a_column(model):
    """The arming mode is gone: a tag cell on a row that names an entry is editable, full
    stop. Ownership decides whether the write is *allowed* (§3.2.1) and undo covers the
    misclick — neither of which a mode above the table was helping with."""
    from PySide6.QtCore import Qt
    m, _ = model
    assert m.flags(m.index(0, 1)) & Qt.ItemIsEditable


def test_a_non_tag_column_is_still_not_editable(model):
    """L1 is read-only. The id column was never editable and must not become so just
    because the gate above it moved."""
    from PySide6.QtCore import Qt
    m, _ = model
    assert not (m.flags(m.index(0, 0)) & Qt.ItemIsEditable)


# --- the context menu follows the clicked column ----------------------------

def _menu_labels(view, column):
    from PySide6.QtCore import QItemSelectionModel
    index = view.model().index(0, column)
    view.selectionModel().select(index, QItemSelectionModel.ClearAndSelect)
    return [a.text() for a in view.menu_for(index).actions()]


@pytest.fixture
def view(model, qapp):
    from packsmith.gui.table.registry_table_view import RegistryTableView
    m, _ = model
    v = RegistryTableView()
    v.setModel(m)
    yield v
    v.deleteLater()


def test_an_l1_column_offers_no_clear(view):
    """id / mod / attributes are read-only for the life of the app (§3.1), so clearing an
    assignment is not a thing that column has. Offering it greyed out reads as "you did
    something wrong" rather than "this concept does not apply here"."""
    labels = _menu_labels(view, 0)
    assert labels == ["Copy"]


def test_a_tag_column_offers_more_than_copy(view, model):
    """The pairing with the L1 case above. Needs an actual assignment now — the offer is
    contextual on there being something to remove, not on the column's type alone."""
    m, tags = model
    tags.assign(REG, "a", "remove", True)
    m.reevaluate()
    assert len(_menu_labels(view, 1)) > 1


def test_a_pristine_tag_cell_offers_nothing_to_clear(view, model):
    """`_bulk_clear` already skips pristine cells, so a greyed-out "Clear" was the menu
    describing a state the code had handled all along."""
    labels = _menu_labels(view, 1)
    assert labels == ["Copy"]


def test_an_assigned_cell_on_a_DEFAULTED_tag_offers_a_reset(view, model):
    """Removing the row does not empty the cell — it goes back to showing `false`. "Clear"
    describes a blank that never appears."""
    m, tags = model
    tags.assign(REG, "a", "remove", True)
    m.reevaluate()
    assert _menu_labels(view, 1) == ["Reset to default", "Copy"]


def test_an_assigned_cell_on_an_UNDEFAULTED_tag_still_says_clear(tags, qapp):
    """No default means the cell really does go empty, so "Clear" is the honest word."""
    from packsmith.gui.table.registry_table_view import RegistryTableView
    tags.define(REG, "note", "string")            # no default
    tags.assign(REG, "a", "note", "something")
    m = RegistryTableModel(Query(scope=Registry(REG), select=[Id, Tag("note")]),
                           Dump(), tags)
    v = RegistryTableView()
    v.setModel(m)
    try:
        assert _menu_labels(v, 1) == ["Clear assignment", "Copy"]
    finally:
        v.deleteLater()


def test_the_count_is_of_assigned_cells_not_selected_ones(view, model):
    """Select three, only one of which holds an assignment: the label has to name what
    will actually happen, or it promises work it will not do."""
    from PySide6.QtCore import QItemSelectionModel
    m, tags = model
    tags.assign(REG, "a", "remove", True)
    m.reevaluate()

    sel = view.selectionModel()
    sel.clearSelection()
    for row in range(3):
        sel.select(m.index(row, 1), QItemSelectionModel.Select)
    labels = [a.text() for a in view.menu_for(m.index(0, 1)).actions()]
    assert labels == ["Reset to default", "Copy"], labels
