"""Round-trip fidelity — the guarantee a dry run rests on (design 3.3, 7.4).

A dry run buffers what a real run commits, and the two are only interchangeable if
**what goes into a store comes back out unchanged**. Every failure in this file is silent:
a value quietly changes type, a file quietly gains a line ending, a query quietly returns
a different order — and a run and its preview disagree about something nobody looked at.
That disagreement is the one thing the whole reporting design cannot survive, because its
entire proposition is that the preview is the run.

§7.4 keeps a clock and randomness out of the capability catalog for the same reason. These
are the rest of that promise.
"""
import pytest

from packsmith.core.files import FileStore
from packsmith.core.staging import L2Staging

REG = "minecraft:item"


# --- values survive the store ----------------------------------------------------------

@pytest.fixture
def store(tags):
    """The conftest TagStore, with one tag of each type defined."""
    tags.define(REG, "flag", "bool")
    tags.define(REG, "weight", "number")
    tags.define(REG, "notes", "string")
    tags.define(REG, "tier", "enum", enum_values=["early", "mid", "late"])
    return tags


@pytest.mark.parametrize("tag_name,value", [
    ("flag", True), ("flag", False),
    ("weight", 0), ("weight", 5), ("weight", -3), ("weight", 5.5), ("weight", 0.0),
    ("notes", "plain"), ("notes", ""), ("notes", "True"), ("notes", "0"),
    ("notes", "a\nb"), ("notes", "  padded  "), ("notes", "héllo ✓"),
    ("tier", "mid"),
])
def test_a_value_comes_back_as_itself(store, tag_name, value):
    """Assignments are stored as `str(value)` and cast back on read. Anything that does not
    survive that trip is a cell whose dry-run value and committed value differ — and
    `notes = "True"` versus `flag = True` is exactly where a careless cast would collapse
    two different things into one."""
    store.assign(REG, "e", tag_name, value)
    back = store.get_tag(REG, "e", tag_name)

    assert back == value
    assert type(back) is type(value), f"{value!r} came back as {type(back).__name__}"


def test_an_empty_string_is_not_none(store):
    """The distinction §3.2.1 turns on: a cell holding "" is ASSIGNED, and a pristine cell
    is not. Collapsing them would make an emptied note read as never-touched."""
    store.assign(REG, "e", "notes", "")
    assert store.get_tag(REG, "e", "notes") == ""
    assert store.get_ownership(REG, "e", "notes") is not None


# --- files survive the disk -------------------------------------------------------------

@pytest.mark.parametrize("content", [
    "a\nb\n",            # LF
    "a\r\nb\r\n",        # CRLF — the one that used to double
    "a\r\nb\nc",         # mixed, as a half-edited config genuinely is
    "a\rb",              # lone CR
    "no trailing eol",
    "",
    "unicode: héllo ✓\n",
])
def test_a_file_comes_back_byte_for_byte(tmp_path, user_db, content):
    """Python's default text mode translates line endings in BOTH directions. Left on, a
    file written with CRLF lands on disk as `\\r\\r\\n` and reads back with a blank line
    inserted — and a `.toml` an action barely touched comes back entirely LF, reporting
    every line as modified.
    """
    root = tmp_path / "instance"
    root.mkdir()
    store = FileStore(user_db, root)
    store.write("f.txt", content, owner="user")

    assert store.read("f.txt") == content
    assert (root / "f.txt").read_bytes() == content.encode("utf-8"), \
        "what landed on disk is not what the action wrote"


def test_a_rollback_restores_the_original_bytes(tmp_path, user_db):
    """Rollback rewrites the snapshot. If it re-translates on the way back, it restores a
    file that is not the file it captured."""
    root = tmp_path / "instance"
    root.mkdir()
    store = FileStore(user_db, root)
    original = "key = 1\r\nother = 2\r\n"
    store.write("c.toml", original, owner="user")

    prior = store.write("c.toml", "key = 9\n", owner="user")
    store.restore("c.toml", prior, {"kind": "user", "action_ref": None})

    assert (root / "c.toml").read_bytes() == original.encode("utf-8")


# --- the query sees this step's own writes ----------------------------------------------

def test_an_action_can_find_what_it_just_wrote(store):
    """`pack.tags.query` was the only read that skipped staging, so an action could write a
    cell and miss it one line later. Every other read on `pack` is staged-first, and
    `blueprints.gaps` advertises the opposite behaviour — this was an inconsistency, not a
    policy."""
    staging = L2Staging(store)
    staging.write(REG, "rope", "flag", True, owner="user")

    assert staging.query(REG, "flag", True) == ["rope"]
    assert store.query(REG, flag=True) == [], "nothing should have reached the store"


def test_a_staged_change_of_value_leaves_the_old_result(store):
    store.assign(REG, "rope", "flag", True)
    staging = L2Staging(store)
    staging.write(REG, "rope", "flag", False, owner="user")

    assert staging.query(REG, "flag", True) == []
    assert staging.query(REG, "flag", False) == ["rope"]


def test_a_staged_delete_drops_out_even_when_the_default_would_match(store):
    """After a commit the row is gone and the store selects FROM assignments, so a pristine
    cell cannot match however its default displays. The overlay has to agree — reproducing
    the store's answer is the entire job."""
    store.define(REG, "defaulted", "bool", default=True)
    store.assign(REG, "rope", "defaulted", True)
    staging = L2Staging(store)
    staging.delete(REG, "rope", "defaulted")

    assert staging.query(REG, "defaulted", True) == []


def test_the_overlay_agrees_with_the_store_once_committed(store):
    """The property that makes a dry run interchangeable with a real one: what the buffer
    SAYS the query will return is what the store DOES return afterwards."""
    store.assign(REG, "existing", "flag", True)
    staging = L2Staging(store)
    staging.write(REG, "added", "flag", True, owner="user")
    staging.write(REG, "excluded", "flag", False, owner="user")
    staging.delete(REG, "existing", "flag")

    predicted = staging.query(REG, "flag", True)
    staging.commit()

    assert predicted == store.query(REG, flag=True) == ["added"]


def test_query_results_are_ordered(store):
    """An action iterates this. One that writes numbered output, or stops after N, produces
    different files on different runs if the order moves.

    The two halves are not equally at risk, and the assertion is kept on both anyway. The
    **store** currently comes back sorted whatever we do — SQLite's `DISTINCT` happens to
    sort — but that is a query-planner detail rather than a documented guarantee, and the
    SQL has no `ORDER BY` to make it one. The **overlay** genuinely needs it: it merges
    through a `set`, whose iteration order for strings is arbitrary, and dropping the sort
    there really does scramble the result.
    """
    for entry in ("zeta", "alpha", "mid", "beta"):
        store.assign(REG, entry, "flag", True)

    assert store.query(REG, flag=True) == ["alpha", "beta", "mid", "zeta"]
    assert L2Staging(store).query(REG, "flag", True) == ["alpha", "beta", "mid", "zeta"]
