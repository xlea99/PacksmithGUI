"""The Used-by table on an action's reference page (design 3.3.1, 3.3.2).

One row per step that runs the action, one column per declared mapping and config
parameter — so reading *down* `(MAP) target` compares how two jobs bound the same slot.

Only two things here are tested, and both for the same reason: they are the cells that can
be **confidently wrong**. Everything else on the page announces its own breakage — a
missing section is missing, a mis-sized column is mis-sized — but a cell that states a
plausible falsehood reads exactly like a cell that is right, and you act on it.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packsmith.core.packages import ActionManifest, ConfigParam, MappingSlot
from packsmith.core.shapes import parse_shape
from packsmith.gui.action_page import ActionPageTab, _on_error_cell, _shape_tree

REF = "removal_suite:nuke"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class Step:
    def __init__(self, bindings=None, config=None, on_error=None):
        self.action_ref, self.kind = REF, "action"
        self.bindings = bindings or {}
        self.config = config or {}
        self.on_error = on_error

    @property
    def is_action(self):
        return True


class Job:
    def __init__(self, id, name, steps, default_on_error="halt"):
        self.id, self.name, self.steps = id, name, steps
        self.default_on_error = default_on_error

    def on_error_for(self, step):
        return step.on_error or self.default_on_error


class Tags:
    """Bindings store ids (design 3.2.1); this is the id → name direction."""
    BY_ID = {11: "remove"}

    def definition_by_id(self, tag_id):
        name = self.BY_ID.get(tag_id)
        return {"id": tag_id, "name": name, "type": "bool"} if name else None


def manifest():
    return ActionManifest(
        package_name="removal_suite", action_id="nuke", file="nuke.star",
        function="run",
        mappings={"target": MappingSlot(name="target", kind="tag", tag_type="bool",
                                        registry_type="minecraft:item")},
        config={"batch_size": ConfigParam(name="batch_size", type="number", default=500)},
    )


def page(jobs):
    class Jobs:
        def all(self):
            return jobs

    return ActionPageTab(manifest(), None, Jobs(), tag_store=Tags())


# --- on-error: the EFFECTIVE policy, not the stored one ------------------------------------

def test_a_step_with_no_override_shows_the_job_default(qapp):
    """`on_error` is None on a step that takes the job's policy (design 3.3.2), so the
    stored value says nothing. Rendering it raw prints a blank — or worse, prints "halt"
    from some assumed default — for a step that will actually skip.

    That is a table stating the wrong thing about what happens when the step fails, in a
    cell that looks exactly like a correct one.
    """
    job = Job(1, "Nightly", [Step()], default_on_error="skip")
    text, colour = _on_error_cell(job, job.steps[0])

    assert text.startswith("skip"), "reported the wrong failure policy"
    assert "(job)" in text, "did not say the policy is inherited"
    assert colour, "an inherited value must be muted, or it reads as an override"


def test_an_explicit_override_is_not_marked_as_inherited(qapp):
    job = Job(1, "Nightly", [Step(on_error="skip")], default_on_error="halt")
    text, colour = _on_error_cell(job, job.steps[0])

    assert text == "skip"
    assert colour is None


# --- a binding whose artifact is gone ------------------------------------------------------

def test_a_dead_binding_says_so_rather_than_looking_unbound(qapp):
    """A binding holds an **id**. When the tag behind it is deleted the id resolves to
    nothing, and the tempting rendering — an empty cell — is indistinguishable from the
    slot never having been bound.

    They are opposite states: unbound may be fine (an optional slot), while a dead id means
    the job refuses to run until someone re-binds it. Saying nothing turns a blocking
    problem into something that looks deliberate.
    """
    tab = page([Job(1, "Nightly", [Step(bindings={"target": 99})])])
    text, colour = tab._binding_cell("target", tab._jobs.all()[0].steps[0])

    assert "missing" in text and "99" in text
    assert colour, "a dead binding must not render in ordinary text"


def test_a_live_binding_shows_the_tags_current_name(qapp):
    tab = page([Job(1, "Nightly", [Step(bindings={"target": 11})])])
    text, colour = tab._binding_cell("target", tab._jobs.all()[0].steps[0])

    assert text == "remove"
    assert colour is None


def test_an_unbound_slot_is_distinct_from_a_dead_one(qapp):
    tab = page([Job(1, "Nightly", [Step()])])
    text, _ = tab._binding_cell("target", tab._jobs.all()[0].steps[0])

    assert text == "unbound"
    assert "missing" not in text


# --- the shape a blueprint mapping needs ---------------------------------------------------

def test_the_shape_tree_puts_every_slot_under_the_right_parent(qapp):
    """`required_shape` arrives FLAT — ordered requirements keyed by dotted path — and the
    tree is drawn from it in one forward pass. Get the sibling scan wrong and a nested slot
    is drawn at the top level, or a branch's continuation bar stops early and its remaining
    children appear to belong to whatever came before.

    Nothing about that looks broken: the names and kinds are all still correct and present.
    Only the *parentage* is a lie — which is the one fact the tree exists to add over the
    flat list, and the one that decides whether the blueprint you go and build will match.
    """
    shape = parse_shape({
        "a": {"kind": "group", "slots": {
            "b": {"kind": "group", "slots": {
                "c": {"kind": "string"},
                "d": {"kind": "string"},
            }},
            "e": {"kind": "string"},
        }},
        "f": {"kind": "number"},
    }, where="test")

    drawn = [line.split("   ")[0].rstrip()
             for line in _shape_tree(shape).text().splitlines()]

    assert drawn == [
        "├─ a",
        "│  ├─ b",
        "│  │  ├─ c",
        "│  │  └─ d",
        "│  └─ e",
        "└─ f",
    ]


def test_a_flat_shape_needs_no_continuation_bars(qapp):
    shape = parse_shape({"one": {"kind": "string"}, "two": {"kind": "string"}},
                        where="test")
    drawn = [line.split("   ")[0].rstrip()
             for line in _shape_tree(shape).text().splitlines()]

    assert drawn == ["├─ one", "└─ two"]


# --- the tab's own name ---------------------------------------------------------------

def _titled(name, action_id="nuke"):
    manifest = ActionManifest(package_name="removal_suite", action_id=action_id,
                              file="n.star", function="run", name=name)

    class Jobs:
        def all(self):
            return []

    return ActionPageTab(manifest, None, Jobs(), tag_store=Tags()).title()


def test_the_tab_names_the_action_both_ways(qapp):
    """`"Display Name" (action_id)`.

    Both halves earn their place: the id is what a job step names and what you search for,
    and the name is what tells you which of `nuke`, `audit` and `fill` you are looking at.
    The quotes and brackets are what keep them visibly separate — two bare words ran
    together at a glance.
    """
    assert _titled("Nuke Items") == '"Nuke Items" (nuke)'


def test_a_long_name_is_truncated_so_the_id_survives(qapp):
    """The id is the shorter, more identifying half, so it is the half that must not be
    pushed off the end of the tab by a wordy display name."""
    title = _titled("Obliterate Every Trace Of This Item Everywhere")

    assert title.startswith('"Obliterate Every')
    assert title.endswith("(nuke)")
    assert "…" in title


def test_a_name_that_is_just_the_id_is_not_said_twice(qapp):
    """`name` defaults to the id when a manifest omits it, and `"nuke" (nuke)` is a tab
    that looks like a bug."""
    assert _titled("nuke") == "nuke"
    assert _titled("") == "nuke"
