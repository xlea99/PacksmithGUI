"""The tag query engine: operators and combinations (design 3.2.1).

Note: this covers the current dict-filter query API. The eventual universal
query language / filter-bar grammar (design 3.2.3, 5.3) is a separate, later thing.
"""
import pytest

REG = "minecraft:item"


@pytest.fixture
def stocked(tags):
    """A TagStore preloaded with a small, known set of tagged items."""
    tags.define(REG, "tier", "enum", ["early", "mid", "late", "end"])
    tags.define(REG, "weight", "number")
    tags.define(REG, "banned", "bool")
    #        tier,     weight, banned
    data = {
        "wood":      ("early", 1, True),
        "stone":     ("early", 2, True),
        "iron":      ("mid",   4, False),
        "diamond":   ("mid",   3, False),
        "netherite": ("end",   5, False),
    }
    for eid, (tier, weight, banned) in data.items():
        tags.assign(REG, eid, "tier", tier)
        tags.assign(REG, eid, "weight", weight)
        tags.assign(REG, eid, "banned", banned)
    return tags


def test_eq_via_kwarg(stocked):
    assert set(stocked.query(REG, tier="mid")) == {"iron", "diamond"}


def test_in(stocked):
    got = stocked.query(REG, filters=[{"tag": "tier", "op": "in", "values": ["early", "end"]}])
    assert set(got) == {"wood", "stone", "netherite"}


def test_not_in(stocked):
    got = stocked.query(REG, filters=[{"tag": "tier", "op": "not_in", "values": ["early", "end"]}])
    assert set(got) == {"iron", "diamond"}


def test_gt(stocked):
    got = stocked.query(REG, filters=[{"tag": "weight", "op": "gt", "value": 3}])
    assert set(got) == {"iron", "netherite"}


def test_lte(stocked):
    got = stocked.query(REG, filters=[{"tag": "weight", "op": "lte", "value": 2}])
    assert set(got) == {"wood", "stone"}


def test_neq(stocked):
    got = stocked.query(REG, filters=[{"tag": "tier", "op": "neq", "value": "early"}])
    assert set(got) == {"iron", "diamond", "netherite"}


def test_exists_and_not_exists(tags):
    tags.define(REG, "tier", "enum", ["early"])
    tags.define(REG, "notes", "string")
    tags.assign(REG, "a", "tier", "early")
    tags.assign(REG, "b", "tier", "early")
    tags.assign(REG, "a", "notes", "hi")
    assert set(tags.query(REG, filters=[{"tag": "notes", "op": "exists"}])) == {"a"}
    got = tags.query(REG, filters=[
        {"tag": "tier", "op": "exists"},
        {"tag": "notes", "op": "not_exists"},
    ])
    assert set(got) == {"b"}


def test_combined_kwargs_and_filters(stocked):
    got = stocked.query(REG, filters=[{"tag": "weight", "op": "gte", "value": 3}], banned="False")
    assert set(got) == {"iron", "diamond", "netherite"}


def test_invalid_op_raises(stocked):
    with pytest.raises(ValueError):
        stocked.query(REG, filters=[{"tag": "tier", "op": "sideways", "value": "x"}])

