"""Editing a JSON5 manifest without reformatting it (design 3.3.1).

A manifest is hand-editable — "they are just files on disk" — so the app must never
parse-and-redump one: that would reflow the user's file and eat their comments every time
they used the New Action dialog. TOML made this easy in exactly one way, which is that a
new `[[actions]]` block could be *appended*. JSON5 has no append; an action object goes
inside the `actions` array, before its closing bracket. Finding that bracket without a
parser is the price of the format, and this file is the receipt.

Everything here is about failures that are **quiet**. A splice that drops a comment, loses
a sibling's field, or is fooled by a `}` inside a string still produces a file that opens —
just not the one the user wrote. Nothing announces it; you find out later when something
you meant to say is missing.
"""
import json5
import pytest

from packsmith.core.packages import (
    _actions_layout, _insert_action, _strip_action_object)

# Deliberately awkward: mixed quoting, an unquoted key, comments in three positions, a
# trailing comma on one entry and none on the last. All legal JSON5, all things a person
# actually writes, and each one has broken a draft of the scanner.
HAND_WRITTEN = """{
  // deep_end's own actions.
  "package": { "name": "deep_end", version: '0.1.0' },

  "actions": [
    {
      "id": "obliterate",
      "file": "removal_and_hide.star",
      "function": "obliterate",
      // the tag is bound by the user, never named here
      "mappings": { "target_items": { "kind": "tag", "required": true } },
    },
    // a comment sitting between entries
    { "id": "hide", "file": "removal_and_hide.star", "function": "hide" }
  ],
}
"""

EMPTY = """{
  "package": { "name": "fresh", "version": "0.1.0" },

  "actions": [
  ],
}
"""


def ids(text):
    return [a["id"] for a in json5.loads(text)["actions"]]


def comments(text):
    return text.count("//")


# --- finding the array -----------------------------------------------------------------

def test_the_layout_finds_the_array_and_its_children():
    layout = _actions_layout(HAND_WRITTEN)
    assert layout is not None
    open_i, close_i, children = layout
    assert HAND_WRITTEN[open_i] == "[" and HAND_WRITTEN[close_i] == "]"
    assert len(children) == 2
    assert json5.loads(HAND_WRITTEN[children[0][0]:children[0][1]])["id"] == "obliterate"


def test_a_manifest_with_no_actions_array_is_not_guessed_at():
    """`_insert_action` turns this into a refusal naming the file. Inventing an array would
    write a manifest the author did not lay out."""
    assert _actions_layout('{"package": {"name": "x"}}') is None


def test_nested_braces_in_a_mapping_do_not_end_the_action():
    """A mapping is an object inside an object inside the array. Counting depth wrong here
    would cut the first action short and silently drop its mappings."""
    _open, _close, children = _actions_layout(HAND_WRITTEN)
    first = json5.loads(HAND_WRITTEN[children[0][0]:children[0][1]])
    assert first["mappings"]["target_items"]["kind"] == "tag"


def test_brackets_and_comment_markers_inside_strings_are_just_text():
    """The case that decides whether this is a scanner or a regex. A description is free
    text and may contain every character the structure uses."""
    tricky = """{
      "actions": [
        { "id": "a", "file": "a.star", "function": "run",
          "description": "handles ] and } and // not a comment", },
      ],
    }
    """
    after = _insert_action(tricky, {"id": "b", "file": "b.star", "function": "run"})
    assert ids(after) == ["a", "b"]
    assert json5.loads(after)["actions"][0]["description"].endswith("not a comment")


# --- adding ----------------------------------------------------------------------------

def test_adding_keeps_every_comment():
    """The whole reason this is text surgery and not a round trip."""
    after = _insert_action(HAND_WRITTEN, {"id": "report", "file": "r.star",
                                          "function": "run"})
    assert comments(after) == comments(HAND_WRITTEN)
    assert "a comment sitting between entries" in after


def test_adding_leaves_the_siblings_exactly_as_they_were():
    after = _insert_action(HAND_WRITTEN, {"id": "report", "file": "r.star",
                                          "function": "run"})
    assert ids(after) == ["obliterate", "hide", "report"]
    assert json5.loads(after)["actions"][0] == json5.loads(HAND_WRITTEN)["actions"][0]


def test_adding_after_an_entry_with_no_trailing_comma():
    """The last hand-written entry here ends `}` with no comma — appending after it without
    adding one produces `} {`, which does not parse. JSON5 permits the trailing comma; TOML
    had no equivalent hazard, which is why this is the first thing to get wrong."""
    after = _insert_action(HAND_WRITTEN, {"id": "third", "file": "c.star",
                                          "function": "run"})
    assert ids(after) == ["obliterate", "hide", "third"]


def test_adding_to_an_empty_array():
    after = _insert_action(EMPTY, {"id": "only", "file": "only.star", "function": "run"})
    assert ids(after) == ["only"]
    assert json5.loads(after)["package"]["name"] == "fresh"


def test_the_written_object_quotes_its_keys():
    """`json5.dumps` quotes only JavaScript *reserved words*, which would leave `function`
    quoted and `id` bare in the same object. Writes emit strict, reads tolerate."""
    after = _insert_action(EMPTY, {"id": "only", "file": "only.star", "function": "run"})
    assert '"function": "run"' in after and '"id": "only"' in after


def test_a_refusal_names_the_file_rather_than_inventing_an_array():
    with pytest.raises(ValueError, match="manifest.json5"):
        _insert_action('{"package": {"name": "x"}}', {"id": "a", "file": "a.star",
                                                      "function": "run"})


# --- removing --------------------------------------------------------------------------

@pytest.mark.parametrize("victim, left", [
    ("obliterate", ["hide"]),          # the first, which owns the nested mappings
    ("hide", ["obliterate"]),          # the last, which has no trailing comma
])
def test_removing_leaves_the_others_intact(victim, left):
    after = _strip_action_object(HAND_WRITTEN, victim)
    assert ids(after) == left


def test_removing_keeps_the_comments_around_it():
    after = _strip_action_object(HAND_WRITTEN, "obliterate")
    assert "a comment sitting between entries" in after
    assert "deep_end's own actions" in after


def test_removing_something_that_is_not_there_says_so():
    """None rather than a silent no-op — `remove_action` turns it into an error naming the
    package, and an undeclare that quietly did nothing would be worse than a failure."""
    assert _strip_action_object(HAND_WRITTEN, "never_declared") is None


def test_add_then_remove_is_a_round_trip():
    """The strongest single assertion here: the file comes back with the same actions and
    the same comments it started with."""
    added = _insert_action(HAND_WRITTEN, {"id": "temp", "file": "t.star",
                                          "function": "run"})
    back = _strip_action_object(added, "temp")
    assert json5.loads(back)["actions"] == json5.loads(HAND_WRITTEN)["actions"]
    assert comments(back) == comments(HAND_WRITTEN)


def test_an_id_appearing_as_a_value_is_not_mistaken_for_the_declaration():
    """Identified by parsing the slice, not by matching an `id` line — a description or a
    file name may contain another action's id."""
    text = """{
      "actions": [
        { "id": "keep", "file": "a.star", "function": "run",
          "description": "runs before id: doomed" },
        { "id": "doomed", "file": "b.star", "function": "run" },
      ],
    }
    """
    after = _strip_action_object(text, "doomed")
    assert ids(after) == ["keep"]
    assert "runs before id: doomed" in after
