"""Token matching — the candidate finder (design 5.3).

§5.3: *"the hardest part of the stone problem isn't applying recipe changes to each item
ID, it's **finding each item ID**."* Substring matching can't: `alexscaves:galena` wants a
wall, `quark:ac_galena_wall` is the wall, and the `ac_` infix plus the namespace break
every naive comparison. Tokens ignore both.

The singularisation tests are the load-bearing ones. They exist because a real 304-mod pack
splits the six candidates for a granite brick wall across `brick` and `bricks`, so exact
token matching finds 1 of 6 or 5 of 6 depending on which spelling the user types — and
never all six.
"""
import pytest

from packsmith.core.query.ast import Cmp, Id, Query, Registry, QueryError, VALID_OPS
from packsmith.core.query.evaluator import evaluate
from packsmith.core.query.tokens import matches_tokens, normalise, tokenize


class Dump:
    def __init__(self, ids):
        self.registry = {"minecraft:block": {"values": list(ids)}}

    def attribute(self, *_a):
        return None


def ids(result):
    return [r.values["id"] for r in result.rows]


def find(entries, tokens):
    return ids(evaluate(Query(scope=Registry("minecraft:block"), select=[Id],
                              filter=Cmp(Id, "matches_tokens", tokens), order_by=[Id]),
                        packdump=Dump(entries), tag_store=None))


# --- tokenising -------------------------------------------------------------

def test_ids_split_on_every_separator_and_keep_the_namespace(self=None):
    assert tokenize("quark:ac_galena_wall") == {"quark", "ac", "galena", "wall"}
    assert tokenize("v_slab_compat:create/polished_cut_tuff_vertical_slab") >= {
        "create", "polished", "cut", "tuff", "vertical", "slab"}


def test_display_names_tokenise_to_the_same_thing_as_ids():
    """A recommendation can be written against either surface."""
    assert tokenize("Galena Wall") <= tokenize("quark:ac_galena_wall")


def test_singularisation_is_symmetric_and_naive():
    assert normalise("bricks") == normalise("brick") == "brick"
    # Not linguistically right, and it doesn't need to be — only identical on both sides.
    assert normalise("glass") == "glass"        # -ss is left alone
    assert normalise("ss") == "ss"              # too short to strip


# --- the case the design is written about -----------------------------------

GRANITE_WALLS = [
    "caverns_and_chasms:granite_brick_wall",
    "create:cut_granite_brick_wall",
    "create:small_granite_brick_wall",
    "quark:granite_bricks_wall",
    "stoneworks:granite_brick_wall",
    "stoneworks:mossy_granite_brick_wall",
    "minecraft:granite_wall",
    "minecraft:polished_granite",
]


def test_either_spelling_finds_the_same_six():
    """The user's real question — "which of these is the blessed granite bricks" — must not
    depend on whether they type brick or bricks."""
    singular = find(GRANITE_WALLS, ["granite", "brick", "wall"])
    plural = find(GRANITE_WALLS, ["granite", "bricks", "wall"])
    assert singular == plural
    assert len(singular) == 6
    assert "minecraft:granite_wall" not in singular      # no brick token


def test_the_galena_wall_case_from_the_design():
    entries = ["alexscaves:galena", "alexscaves:galena_stairs", "quark:ac_galena_wall"]
    assert find(entries, ["galena", "wall"]) == ["quark:ac_galena_wall"]


def test_extra_tokens_never_disqualify():
    """`quark` and `ac` are along for the ride. Matching asks whether the wanted tokens are
    PRESENT, not whether the sets are equal."""
    assert matches_tokens("quark:ac_galena_wall", ["galena", "wall"])
    assert not matches_tokens("quark:ac_galena_wall", ["galena", "wall", "polished"])


def test_word_order_does_not_matter():
    assert find(["mod:wall_granite_brick"], ["granite", "brick", "wall"]) == \
        ["mod:wall_granite_brick"]


def test_a_phrase_works_as_well_as_a_list():
    assert matches_tokens("quark:granite_bricks_wall", "granite brick wall")


# --- guardrails -------------------------------------------------------------

def test_no_tokens_is_an_error_not_a_match_everything():
    """A recommendation template whose anchor is unbound resolves to nothing. Matching
    every row would hand back the entire registry as "candidates"."""
    for empty in ([], "", ["   "]):
        with pytest.raises(QueryError, match="at least one token"):
            find(["minecraft:stone"], empty)


def test_a_missing_value_never_matches():
    assert not matches_tokens(None, ["granite"])


def test_the_op_is_a_first_class_part_of_the_language():
    """Not a special case bolted onto the evaluator — an ordinary Cmp op, so a query using
    it stays plain serializable data (design 3.2.4)."""
    assert "matches_tokens" in VALID_OPS


# --- prefix matching: the half-typed word ------------------------------------
#
# A finished word and a word still under the cursor want opposite things, and the real
# data makes it stark: `granit` matches 0 blocks and `granit*` matches 96, while `stone`
# matches 228 and `stone*` matches 1907 (it swallows `stonezone` and `stonework`). Exact
# everywhere makes a live bar look broken until the last keystroke; prefix everywhere
# makes finished words useless. So the caller says which it means.

def test_a_trailing_star_matches_by_prefix():
    entries = ["minecraft:granite", "minecraft:granite_stairs", "minecraft:stone"]
    assert find(entries, ["granit*"]) == ["minecraft:granite", "minecraft:granite_stairs"]
    assert find(entries, ["granit"]) == []


def test_a_finished_word_does_not_bleed_into_longer_ones():
    entries = ["minecraft:stone", "stonezone:thing", "stoneworks:slab"]
    assert find(entries, ["stone"]) == ["minecraft:stone"]
    assert len(find(entries, ["stone*"])) == 3


def test_prefix_and_exact_tokens_mix():
    entries = ["minecraft:polished_granite_stairs", "minecraft:polished_diorite_stairs"]
    assert find(entries, ["granite", "stai*"]) == ["minecraft:polished_granite_stairs"]


def test_the_typing_flag_marks_only_the_word_under_the_cursor():
    from packsmith.core.query.language import parse as parse_text
    typed = parse_text("polished granit", typing=True)
    assert typed.clauses[0].value == ["granit*", "polished"]
    # a trailing space means the word is finished
    assert parse_text("polished granit ", typing=True).clauses[0].value == \
        ["granit", "polished"]
    # and nothing is implied when the caller isn't typing
    assert parse_text("polished granit").clauses[0].value == ["granit", "polished"]


def test_an_explicit_star_survives_the_round_trip():
    from packsmith.core.query.language import format as fmt, parse as parse_text
    assert fmt(parse_text("stone*")) == "stone*"
    assert parse_text(fmt(parse_text("stone*"))) == parse_text("stone*")
