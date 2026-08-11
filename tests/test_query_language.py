"""The filter-bar language (design 3.2.3).

Two things are being checked. That the syntax §3.2.3 specifies actually parses — those
examples are lifted from the doc verbatim. And that **every query survives a round trip**,
because §3.2.4 rejected SQL specifically on the grounds that arbitrary SQL cannot be lifted
back into a visual builder: *"the two-way binding dies."* A surface language that can't be
re-read is the same failure in a different costume.
"""
import pytest

from packsmith.core.query.ast import (
    And, Attribute, Cmp, Has, Id, Mod, Not, Or, Slot, Tag,
)
from packsmith.core.query.language import QuerySyntaxError, format, parse


def roundtrip(text):
    """Parse, print, parse again — the printed form must mean the same thing."""
    first = parse(text)
    printed = format(first)
    assert parse(printed) == first, f"{text!r} -> {printed!r} did not survive"
    return printed


# --- the syntax the design specifies ----------------------------------------

def test_the_examples_from_the_design_doc():
    assert parse("t:remove == true") == Cmp(Tag("remove"), "eq", True)
    assert parse('t:tier IN ("early", "mid")') == Cmp(Tag("tier"), "in", ["early", "mid"])
    assert parse("t:weight > 5") == Cmp(Tag("weight"), "gt", 5)
    assert parse("HAS t:notes") == Has(Tag("notes"))
    assert parse("NOT HAS t:tier") == Not(Has(Tag("tier")))
    assert parse('id CONTAINS "granite"') == Cmp(Id, "contains", "granite")
    assert parse('mod == "quark"') == Cmp(Mod, "eq", "quark")
    assert parse('a:localization MATCHES ".*Brick.*"') == \
        Cmp(Attribute("localization"), "matches", ".*Brick.*")


def test_blueprint_slots_use_the_b_sigil():
    assert parse("HAS b:polished.wall") == Has(Slot("polished.wall"))
    assert parse('b:base_block CONTAINS "granite"') == \
        Cmp(Slot("base_block"), "contains", "granite")


def test_boolean_logic_and_precedence():
    """NOT binds tighter than AND, which binds tighter than OR."""
    assert parse("HAS t:a AND HAS t:b OR HAS t:c") == Or([
        And([Has(Tag("a")), Has(Tag("b"))]), Has(Tag("c"))])
    assert parse("NOT HAS t:a AND HAS t:b") == And([Not(Has(Tag("a"))), Has(Tag("b"))])
    assert parse("HAS t:a AND (HAS t:b OR HAS t:c)") == And([
        Has(Tag("a")), Or([Has(Tag("b")), Has(Tag("c"))])])


def test_not_in_is_an_operator_not_a_negation():
    assert parse('t:tier NOT IN ("early")') == Cmp(Tag("tier"), "not_in", ["early"])


def test_unquoted_values_and_numbers():
    assert parse("mod == quark") == Cmp(Mod, "eq", "quark")
    assert parse("t:weight >= 2.5") == Cmp(Tag("weight"), "gte", 2.5)
    assert parse("t:done == false") == Cmp(Tag("done"), "eq", False)


def test_keywords_are_case_insensitive():
    assert parse("has t:a and not has t:b") == parse("HAS t:a AND NOT HAS t:b")


# --- bare words: the candidate finder ---------------------------------------

def test_bare_words_search_id_and_display_name():
    """The most common gesture in the bar does the most valuable thing (design 5.3)."""
    node = parse("granite brick wall")
    assert node == Or([                      # tokens are canonicalised, hence sorted
        Cmp(Id, "matches_tokens", ["brick", "granite", "wall"]),
        Cmp(Attribute("localization"), "matches_tokens", ["brick", "granite", "wall"]),
    ])


def test_bare_words_are_one_term_not_three():
    """`granite brick wall` must be a single three-token match. Three separate terms would
    match anything containing 'wall', which is the opposite of narrowing."""
    node = parse("granite brick wall")
    assert isinstance(node, Or) and len(node.clauses) == 2


def test_a_quoted_phrase_is_bare_too():
    assert parse('"granite brick"') == parse("granite brick")


def test_bare_words_stop_at_keywords_and_operators():
    assert parse("granite AND HAS t:remove") == And([
        parse("granite"), Has(Tag("remove"))])
    assert parse("granite wall AND mod == quark") == And([
        parse("granite wall"), Cmp(Mod, "eq", "quark")])


def test_a_field_without_an_operator_is_just_a_word():
    """`mod quark` is a phrase search, not a broken comparison — one token of lookahead
    tells them apart."""
    assert parse("mod quark") == parse("quark mod")


def test_tilde_and_matches_tokens_are_the_same_operator():
    assert parse('id ~ "granite brick"') == parse('id MATCHES_TOKENS ("granite", "brick")')


def test_the_printer_always_uses_the_long_spelling():
    """Type the short one; saved queries stay self-documenting."""
    assert format(parse('id ~ "granite brick"')) == 'id MATCHES_TOKENS ("brick", "granite")'


def test_the_same_query_written_three_ways_lowers_identically():
    """Matching is set-based, so token lists are canonicalised (split, deduped, sorted).
    Without it, two identical queries would serialize differently in saved Views."""
    assert (parse('id ~ "granite brick"')
            == parse('id ~ ("brick", "granite")')
            == parse('id MATCHES_TOKENS ("granite", "brick", "granite")'))


def test_canonicalising_keeps_the_users_spelling():
    """`bricks` stays `bricks` — singularisation happens at match time, not in the text."""
    assert format(parse("granite bricks")) == "bricks granite"


# --- the round trip, which is the whole reason SQL was rejected -------------

@pytest.mark.parametrize("text", [
    "t:remove == true",
    't:tier IN ("early", "mid")',
    't:tier NOT IN ("early")',
    "t:weight > 5",
    "HAS t:notes",
    "NOT HAS t:tier",
    'id CONTAINS "granite"',
    'mod == "quark"',
    'a:localization MATCHES ".*Brick.*"',
    "HAS b:polished.wall",
    "granite brick wall",
    "HAS t:a AND HAS t:b OR HAS t:c",
    "HAS t:a AND (HAS t:b OR HAS t:c)",
    "NOT HAS t:a AND HAS t:b",
    "granite AND mod == quark",
    'NOT (HAS t:a OR HAS t:b)',
    't:weight >= 2.5 AND t:done == false',
])
def test_every_query_survives_a_round_trip(text):
    roundtrip(text)


def test_grouping_is_preserved_only_where_it_matters():
    """Parens are re-emitted when precedence needs them and dropped when it doesn't —
    otherwise the text drifts noisier every time the builder rewrites it."""
    assert format(parse("HAS t:a AND (HAS t:b OR HAS t:c)")) == \
        "HAS t:a AND (HAS t:b OR HAS t:c)"
    assert format(parse("(HAS t:a AND HAS t:b) OR HAS t:c")) == \
        "HAS t:a AND HAS t:b OR HAS t:c"


# --- errors point at the problem --------------------------------------------

def test_an_empty_bar_is_no_filter_not_an_error():
    assert parse("") is None
    assert parse("   ") is None
    assert format(None) == ""


@pytest.mark.parametrize("text,message", [
    ("t:remove = true", "use '=='"),
    ('t:tier IN "early"', "expected a list"),
    ("HAS", "expected a field"),
    ("HAS nonsense", "not a field"),
    ("(HAS t:a", r"expected '\)'"),
    ('id CONTAINS "unterminated', "unterminated string"),
    ("t:a == true)", "unexpected"),
    ("t:a ==", "expected a value"),
])
def test_syntax_errors_say_what_is_wrong(text, message):
    with pytest.raises(QuerySyntaxError, match=message):
        parse(text)


def test_errors_carry_a_position_for_the_bar_to_point_at():
    with pytest.raises(QuerySyntaxError) as caught:
        parse("t:remove = true")
    assert caught.value.position == 9
    assert "^" in str(caught.value)


# --- round-trip completeness (Q-6) -----------------------------------------

def test_a_value_containing_a_quote_survives_the_round_trip():
    """Without escaping, `format` emits a string the tokenizer then cuts short — and the
    ⚙ dialog edits filters as text, so an unrepresentable value is a corrupted filter."""
    node = Cmp(Id, "eq", 'say "hi"')
    assert parse(format(node)) == node


def test_a_backslash_in_a_value_survives_too():
    node = Cmp(Id, "eq", r"back\slash")
    assert parse(format(node)) == node


@pytest.mark.parametrize("text, op", [
    ('id CONTAINS_I "Gran"', "contains"),
    ('a:localization MATCHES_I "brick"', "matches"),
    ('id MATCHES_TOKENS_I ("gran")', "matches_tokens"),
])
def test_case_insensitivity_has_a_spelling_and_round_trips(text, op):
    """`Cmp.ci` was engine-legal with no surface form, so `format` dropped it silently and
    a case-insensitive filter came back case-sensitive — quietly changing what matches."""
    node = parse(text)
    assert node.op == op and node.ci is True
    assert parse(format(node)) == node


def test_the_case_sensitive_spelling_is_unaffected():
    node = parse('id CONTAINS "gran"')
    assert node.ci is False
    assert format(node) == 'id CONTAINS "gran"'


def test_a_hand_built_ci_node_can_be_printed():
    """The engine can produce these even if the bar never has; `format` must not lie."""
    node = Cmp(Id, "contains", "Gran", True)
    assert "CONTAINS_I" in format(node)
    assert parse(format(node)) == node
