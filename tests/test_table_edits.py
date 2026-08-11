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
    m = RegistryTableModel(Query(scope=Registry(REG), select=[Id, Tag("remove")]),
                           Dump(), tags)
    m.set_editing(1, True)
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
