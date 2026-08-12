"""Editing NBT values — design 6.4.

The reader shipped first and was verified byte-exact against 8,369 real files; this is the
other half. Two rules carry it:

1. **A value never changes type.** `Count: 64` and `Count: 64b` are different files. A byte
   silently promoted to an int is a corrupted save that still *loads*, which is the worst
   failure available — so the existing tag decides how its replacement is parsed and
   anything that doesn't fit is refused rather than coerced.
2. **Values, not structure.** Adding, deleting, renaming and reordering tags are out of
   this slice, and deliberately: §6.4 defers "editor ergonomics, list addressing" to a real
   design session, and those questions live in structural editing. `MyList[3]` is a fragile
   handle precisely *because* something can reorder the list. Editing what a tag holds
   needs none of that settled.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core import nbt


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    yield app


def sample():
    root = nbt.Compound({
        "Count": nbt.Byte(64),
        "Level": nbt.Int(7),
        "Health": nbt.Float(19.5),
        "Name": nbt.String("Steve"),
        "Big": nbt.Long(2**40),
        "Bytes": nbt.ByteArray([1, 2, 3]),
        "Pos": nbt.List([nbt.Double(1.0), nbt.Double(64.0)], element_id=nbt.DOUBLE),
        "Inventory": nbt.Compound({"Slot": nbt.Byte(3)}),
    })
    return root


def tab_for(root=None, **kwargs):
    from packsmith.gui.nbt_viewer import NbtViewerTab
    data = nbt.dumps(root if root is not None else sample(), "", "gzip")
    return NbtViewerTab(data, "test.dat", **kwargs)


def row(tab, name):
    """The top-level row called ``name``."""
    tree = tab._tree
    return next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).text(0) == name)


def edit(tab, name, text):
    """Type ``text`` into a row's Value cell, exactly as the delegate would."""
    row(tab, name).setText(2, text)


# --- retype: the type is the contract ------------------------------------------------------

@pytest.mark.parametrize("tag, text, expected", [
    (nbt.Byte(1), "64", nbt.Byte(64)),
    (nbt.Short(1), "-300", nbt.Short(-300)),
    (nbt.Int(1), "70000", nbt.Int(70000)),
    (nbt.Long(1), "9999999999", nbt.Long(9999999999)),
    (nbt.Float(1), "19.5", nbt.Float(19.5)),
    (nbt.Double(1), "-0.25", nbt.Double(-0.25)),
    (nbt.String("a"), "hello world", nbt.String("hello world")),
])
def test_a_value_keeps_its_type(tag, text, expected):
    result = nbt.retype(tag, text)
    assert result == expected
    assert type(result) is type(tag), "the edit changed the tag's type"


@pytest.mark.parametrize("tag, text", [
    (nbt.Byte(0), "128"),          # one past the top of a signed byte
    (nbt.Byte(0), "-129"),
    (nbt.Short(0), "32768"),
    (nbt.Int(0), str(2**31)),
    (nbt.Long(0), str(2**63)),
])
def test_a_value_that_would_not_fit_is_refused(tag, text):
    """Refused, not wrapped. Python ints are unbounded, so without the check `128` on a
    byte would be written as `-128` — a value the user never typed."""
    with pytest.raises(ValueError, match="does not fit"):
        nbt.retype(tag, text)


def test_the_edges_themselves_are_accepted():
    """Off-by-one in the guard would make legal values unenterable."""
    assert nbt.retype(nbt.Byte(0), "127") == 127
    assert nbt.retype(nbt.Byte(0), "-128") == -128


@pytest.mark.parametrize("text", ["", "twelve", "1.5", "0x", "  "])
def test_nonsense_in_an_integer_field_is_refused(text):
    with pytest.raises(ValueError):
        nbt.retype(nbt.Int(0), text)


def test_an_array_is_edited_as_a_list_of_numbers():
    result = nbt.retype(nbt.ByteArray([1]), "[4, 5, 6]")
    assert list(result) == [4, 5, 6] and type(result) is nbt.ByteArray


def test_an_array_element_is_range_checked_too():
    with pytest.raises(ValueError, match="does not fit"):
        nbt.retype(nbt.ByteArray([1]), "[1, 999]")


def test_an_array_can_be_emptied():
    assert list(nbt.retype(nbt.IntArray([1, 2]), "[]")) == []


@pytest.mark.parametrize("tag", [nbt.Compound({}), nbt.List([], element_id=nbt.BYTE)])
def test_structure_is_not_editable_as_a_value(tag):
    """Compounds and lists are structure. Editing them means add/delete/reorder, which is
    the slice §6.4 defers."""
    with pytest.raises(ValueError, match="not editable"):
        nbt.retype(tag, "whatever")


# --- the tab: an edit reaches the tree ---------------------------------------------------------

def test_editing_a_cell_changes_the_underlying_tag(qapp):
    tab = tab_for()
    edit(tab, "Count", "100")
    assert tab.root["Count"] == 100
    assert type(tab.root["Count"]) is nbt.Byte


def test_a_refused_edit_leaves_the_value_alone_and_says_why(qapp):
    """Both halves matter: the tree must not change, and the cell must not keep showing a
    number the file doesn't contain."""
    tab = tab_for()
    said = []
    tab.status.connect(said.append)

    edit(tab, "Count", "9999")

    assert tab.root["Count"] == 64, "the refused edit was applied anyway"
    assert row(tab, "Count").text(2) == "64", "the cell kept a value the file doesn't have"
    assert said and "does not fit" in said[-1]
    assert not tab.is_dirty


def test_editing_a_string_does_not_accumulate_quotes(qapp):
    """Strings render quoted, so the quotes come back in on the next edit. Left alone they
    become part of the value and grow by two characters every round trip."""
    tab = tab_for()
    edit(tab, "Name", '"Alex"')
    assert tab.root["Name"] == "Alex"


def test_a_nested_value_is_reached_through_its_parent(qapp):
    """Scalars are immutable, so an edit can't mutate the tag — it assigns back into the
    compound that holds it, and the row is the only thing that knows which."""
    tab = tab_for()
    parent = row(tab, "Inventory")
    parent.setExpanded(True)
    child = next(parent.child(i) for i in range(parent.childCount())
                 if parent.child(i).text(0) == "Slot")

    child.setText(2, "9")
    assert tab.root["Inventory"]["Slot"] == 9


def test_a_list_element_is_addressed_by_position(qapp):
    """A list has no names — its key is the index, and writing to the wrong one would
    silently move a different value."""
    tab = tab_for()
    parent = row(tab, "Pos")
    parent.setExpanded(True)

    parent.child(1).setText(2, "72.0")
    assert list(tab.root["Pos"]) == [1.0, 72.0]


# --- dirty, save, round trip ----------------------------------------------------------------

def test_an_untouched_tree_round_trips_byte_identical(qapp):
    """The property the editor rests on — stated at the level where it is actually true.

    The NBT *payload* is byte-exact. The gzip *container* is not, and cannot be: Minecraft's
    Java writer stamps `XFL=0` in the gzip header where Python's level-9 deflate stamps
    `XFL=2`, and two different compressors won't produce the same stream regardless. A real
    file from a 246-mod pack caught this — the earlier version of this test compressed with
    our own writer and compared against that, which proves only that we agree with
    ourselves.
    """
    from packsmith.gui.nbt_viewer import NbtViewerTab
    original = nbt.dumps(sample(), "", "gzip")
    rewritten = NbtViewerTab(original, "test.dat").to_bytes()
    assert nbt.decompress(rewritten)[0] == nbt.decompress(original)[0]


def test_a_file_written_by_another_tool_still_round_trips(qapp):
    """The case the self-referential version missed entirely: bytes produced by a *different*
    gzip writer, which is every file Minecraft ever wrote."""
    import gzip as gziplib
    from packsmith.gui.nbt_viewer import NbtViewerTab

    payload = nbt.dumps(sample(), "", "none")
    foreign = gziplib.compress(payload, compresslevel=6, mtime=0)   # XFL=0, as Java writes
    assert foreign[8] == 0

    rewritten = NbtViewerTab(foreign, "foreign.dat").to_bytes()
    assert nbt.decompress(rewritten)[0] == payload


def test_an_unchanged_file_is_never_written_at_all(qapp):
    """What actually protects the user, given the container can't be byte-stable: a save
    with nothing dirty emits nothing, so an untouched file keeps its original bytes."""
    from packsmith.gui.nbt_viewer import NbtViewerTab
    tab = NbtViewerTab(nbt.dumps(sample(), "", "gzip"), "test.dat")
    saved = []
    tab.save_requested.connect(saved.append)
    tab.request_save()
    assert saved == []


def test_the_compression_it_arrived_in_is_what_it_leaves_in(qapp):
    for compression in ("gzip", "zlib", "none"):
        raw = nbt.dumps(sample(), "root", compression)
        tab = tab_for()
        from packsmith.gui.nbt_viewer import NbtViewerTab
        tab = NbtViewerTab(raw, "x.dat")
        assert tab.compression == compression
        assert nbt.loads(tab.to_bytes())[2] == compression


def test_an_edit_survives_the_round_trip(qapp):
    tab = tab_for()
    edit(tab, "Health", "3.5")
    _, root, _ = nbt.loads(tab.to_bytes())
    assert root["Health"] == pytest.approx(3.5)
    assert type(root["Health"]) is nbt.Float


def reparse(tab):
    """Serialise the tab and read it back — what the file will be next time it's opened."""
    return nbt.loads(tab.to_bytes())[1]


@pytest.mark.parametrize("name, typed, expected, expected_type", [
    ("Count", "100", 100, nbt.Byte),
    ("Level", "-70000", -70000, nbt.Int),
    ("Big", "9007199254740993", 9007199254740993, nbt.Long),
    ("Health", "0.5", 0.5, nbt.Float),
    ("Name", "Alex", "Alex", nbt.String),
])
def test_every_scalar_type_survives_an_edit_through_the_cell(qapp, name, typed, expected,
                                                             expected_type):
    """Edited the way a user edits — through the cell, not by assigning into the tree — then
    serialised and re-read. Poking `tab.root` directly would skip the whole edit path and
    prove only that the writer works, which was already known."""
    tab = tab_for()
    edit(tab, name, typed)
    again = reparse(tab)
    assert again[name] == expected
    assert type(again[name]) is expected_type, "the type did not survive the round trip"


def test_an_array_edited_through_the_cell_survives(qapp):
    tab = tab_for()
    edit(tab, "Bytes", "[7, -8, 9]")
    again = reparse(tab)
    assert list(again["Bytes"]) == [7, -8, 9]
    assert type(again["Bytes"]) is nbt.ByteArray


def test_a_nested_edit_survives_serialisation(qapp):
    """The tag lives inside a compound, so the edit has to land in the parent — and then
    the parent has to be the thing that gets written."""
    tab = tab_for()
    parent = row(tab, "Inventory")
    parent.setExpanded(True)
    next(parent.child(i) for i in range(parent.childCount())
         if parent.child(i).text(0) == "Slot").setText(2, "9")

    again = reparse(tab)
    assert again["Inventory"]["Slot"] == 9
    assert type(again["Inventory"]["Slot"]) is nbt.Byte


def test_a_list_edit_survives_serialisation_in_the_right_position(qapp):
    """Position is the whole identity of a list element. Writing the right value to the
    wrong index produces a file that parses perfectly and means something else."""
    tab = tab_for()
    parent = row(tab, "Pos")
    parent.setExpanded(True)
    parent.child(1).setText(2, "72.5")

    again = reparse(tab)
    assert list(again["Pos"]) == [1.0, 72.5]
    assert again["Pos"].element_id == nbt.DOUBLE, "the list's element type was lost"


def test_several_edits_in_one_session_all_land(qapp):
    """One edit landing proves the mechanism; the file the user actually saves has many,
    and a stale row index or a mis-tracked container shows up here rather than there."""
    tab = tab_for()
    for name, typed in (("Count", "1"), ("Level", "2"), ("Name", "three"),
                        ("Health", "4.0"), ("Bytes", "[5]")):
        edit(tab, name, typed)

    again = reparse(tab)
    assert again["Count"] == 1 and again["Level"] == 2 and again["Name"] == "three"
    assert again["Health"] == pytest.approx(4.0) and list(again["Bytes"]) == [5]


def test_the_whole_loop_edit_save_reopen(qapp):
    """The actual user loop, which nothing else here covered: edit, save, and open the
    saved bytes fresh. Everything upstream can pass while the file that lands on disk is
    unopenable — this is the test that says it isn't."""
    from packsmith.gui.nbt_viewer import NbtViewerTab

    tab = tab_for()
    saved = []
    tab.save_requested.connect(saved.append)
    edit(tab, "Count", "42")
    edit(tab, "Name", "Reopened")
    tab.request_save()

    reopened = NbtViewerTab(saved[0], "test.dat")
    assert reopened.ok, "the saved file could not be parsed back"
    assert reopened.root["Count"] == 42 and type(reopened.root["Count"]) is nbt.Byte
    assert reopened.root["Name"] == "Reopened"
    # And it is still a working editor, not just parseable.
    assert reopened.editable
    edit(reopened, "Count", "43")
    assert reparse(reopened)["Count"] == 43


def test_a_refused_edit_leaves_the_saved_file_untouched(qapp):
    """A rejection must not half-apply: the tree keeps the old value AND the bytes do."""
    tab = tab_for()
    edit(tab, "Count", "9999")
    assert reparse(tab)["Count"] == 64


def test_editing_marks_the_tab_dirty_and_saving_clears_it(qapp):
    tab = tab_for()
    seen = []
    tab.dirty_changed.connect(seen.append)
    assert not tab.is_dirty

    edit(tab, "Level", "12")
    assert tab.is_dirty and seen == [True]

    tab.mark_saved()
    assert not tab.is_dirty and seen == [True, False]


def test_setting_a_value_to_what_it_already_was_is_not_a_change(qapp):
    """Otherwise clicking into a cell and pressing Enter marks the file unsaved, and the
    dot stops meaning anything."""
    tab = tab_for()
    edit(tab, "Count", "64")
    assert not tab.is_dirty


def test_ctrl_s_emits_the_serialised_file(qapp):
    tab = tab_for()
    saved = []
    tab.save_requested.connect(saved.append)

    edit(tab, "Level", "3")
    tab.request_save()

    assert len(saved) == 1
    assert nbt.loads(saved[0])[1]["Level"] == 3


def test_saving_with_nothing_changed_says_so_rather_than_writing(qapp):
    """A no-op write would touch the file's mtime and make "did this change" unanswerable
    by comparing files."""
    tab = tab_for()
    saved, said = [], []
    tab.save_requested.connect(saved.append)
    tab.status.connect(said.append)

    tab.request_save()
    assert saved == []
    assert "no changes" in said[-1]


# --- read-only ------------------------------------------------------------------------------------

def test_a_read_only_tab_offers_no_editable_cells(qapp):
    """A jar member's copy must stay exactly as the mod shipped it — the whole override
    mechanism depends on that."""
    from PySide6.QtCore import Qt
    tab = tab_for(read_only_reason='Read-only — inside quark.jar. Use "Save as override".')
    assert not tab.editable
    assert not (row(tab, "Count").flags() & Qt.ItemIsEditable)


def test_ctrl_s_on_a_read_only_tab_explains_instead_of_doing_nothing(qapp):
    """§6.3's rule, borrowed: a silent no-op reads as a bug."""
    tab = tab_for(read_only_reason="Read-only — use Save as override.")
    saved, said = [], []
    tab.save_requested.connect(saved.append)
    tab.status.connect(said.append)

    tab.request_save()
    assert saved == []
    assert "Save as override" in said[-1]


def test_a_file_that_is_not_nbt_is_not_editable(qapp):
    from packsmith.gui.nbt_viewer import NbtViewerTab
    tab = NbtViewerTab(b"this is not nbt at all", "mystery.dat")
    assert not tab.ok and not tab.editable
    tab.request_save()          # must not raise


# --- the filter view edits the same tree ----------------------------------------------------------

def test_a_value_found_by_filtering_is_editable_there(qapp):
    """Filtering is how you find a value in a 337,000-node file. Making the results
    read-only would mean clearing the filter and hunting for the row by hand."""
    tab = tab_for()
    tab._filter.setText("Health")
    found = tab._tree.topLevelItem(0)
    assert found.text(0) == "Health"

    found.setText(2, "5.0")
    assert tab.root["Health"] == pytest.approx(5.0)
    assert tab.is_dirty
