"""The mapping system: validate a binding, resolve a step, best-guess fill (design 7.1)."""
import pytest

from packsmith.core.packages import ActionManifest, MappingSlot, ConfigParam
from packsmith.core.bindings import validate_binding, resolve_step, best_guess_bindings

REG = "minecraft:item"


def _manifest(mappings=None, config=None):
    return ActionManifest(package_name="p", action_id="a", file="a.py", function="run",
                          mappings=mappings or {}, config=config or {})


# --- validate_binding ------------------------------------------------------

def test_validate_accepts_matching_type(tags):
    tags.define(REG, "remove", "bool")
    slot = MappingSlot(name="s", kind="tag", tag_type="bool", registry_type=REG)
    validate_binding(slot, tags.definition(REG, "remove"))          # no raise


def test_validate_rejects_type_mismatch(tags):
    tags.define(REG, "notes", "string")
    with pytest.raises(ValueError):
        validate_binding(MappingSlot(name="s", tag_type="bool"), tags.definition(REG, "notes"))


def test_validate_rejects_nonexistent_tag():
    with pytest.raises(ValueError):
        validate_binding(MappingSlot(name="s", tag_type="bool"), None)


def test_validate_rejects_non_tag_kind():
    with pytest.raises(ValueError):
        validate_binding(MappingSlot(name="s", kind="blueprint"), {"type": "bool"})


# --- resolve_step ----------------------------------------------------------

def test_resolve_happy_path_with_config_default(tags):
    tags.define(REG, "remove", "bool")
    tags.define(REG, "queued", "bool")
    m = _manifest(
        mappings={"source": MappingSlot("source", tag_type="bool", registry_type=REG),
                  "target": MappingSlot("target", tag_type="bool", registry_type=REG)},
        config={"registry_type": ConfigParam("registry_type", "string", default="minecraft:item")},
    )
    mappings, config = resolve_step(m, bindings={"source": "remove", "target": "queued"},
                                    config={}, tag_store=tags)
    assert mappings == {"source": "remove", "target": "queued"}
    assert config == {"registry_type": "minecraft:item"}       # default applied


def test_resolve_missing_required_mapping_raises(tags):
    m = _manifest(mappings={"source": MappingSlot("source", tag_type="bool", registry_type=REG, required=True)})
    with pytest.raises(ValueError):
        resolve_step(m, bindings={}, config={}, tag_store=tags)


def test_resolve_optional_unbound_mapping_is_none(tags):
    m = _manifest(mappings={"note": MappingSlot("note", tag_type="string", registry_type=REG, required=False)})
    mappings, _ = resolve_step(m, bindings={}, config={}, tag_store=tags)
    assert mappings == {"note": None}


def test_resolve_type_mismatch_raises(tags):
    tags.define(REG, "notes", "string")
    m = _manifest(mappings={"source": MappingSlot("source", tag_type="bool", registry_type=REG)})
    with pytest.raises(ValueError):
        resolve_step(m, bindings={"source": "notes"}, config={}, tag_store=tags)


def test_resolve_required_config_unset_raises(tags):
    m = _manifest(config={"group": ConfigParam("group", "string", required=True)})
    with pytest.raises(ValueError):
        resolve_step(m, bindings={}, config={}, tag_store=tags)


def test_resolve_explicit_config_overrides_default(tags):
    m = _manifest(config={"reg": ConfigParam("reg", "string", default="minecraft:item")})
    _, config = resolve_step(m, bindings={}, config={"reg": "minecraft:block"}, tag_store=tags)
    assert config == {"reg": "minecraft:block"}


# --- best_guess_bindings ---------------------------------------------------

def test_best_guess_prefers_likely_name(tags):
    tags.define(REG, "banned", "bool")
    tags.define(REG, "remove", "bool")
    m = _manifest(mappings={"source": MappingSlot("source", tag_type="bool", registry_type=REG, likely_name="remove")})
    assert best_guess_bindings(m, tags)["source"] == tags.definition(REG, "remove")["id"]


def test_best_guess_falls_back_to_type_match(tags):
    tags.define(REG, "banned", "bool")                         # no `remove` tag exists
    m = _manifest(mappings={"source": MappingSlot("source", tag_type="bool", registry_type=REG, likely_name="remove")})
    assert best_guess_bindings(m, tags)["source"] == tags.definition(REG, "banned")["id"]


def test_best_guess_likely_name_wrong_type_is_skipped(tags):
    tags.define(REG, "remove", "string")                      # likely_name exists but wrong type
    tags.define(REG, "banned", "bool")
    m = _manifest(mappings={"source": MappingSlot("source", tag_type="bool", registry_type=REG, likely_name="remove")})
    assert best_guess_bindings(m, tags)["source"] == tags.definition(REG, "banned")["id"]  # falls back to the bool tag


def test_best_guess_none_when_nothing_compatible(tags):
    tags.define(REG, "notes", "string")
    m = _manifest(mappings={"source": MappingSlot("source", tag_type="bool", registry_type=REG)})
    assert best_guess_bindings(m, tags)["source"] is None


# --- requires_values: the tag analog of required_shape (design 3.3) ---------

def test_requires_values_is_parsed_and_scoped_to_enum_tags():
    from packsmith.core.packages import _parse_mappings
    parsed = _parse_mappings({"tier": {"kind": "tag", "tag_type": "enum",
                                       "registry_type": REG,
                                       "requires_values": ["early", "late"]}})
    assert parsed["tier"].requires_values == ("early", "late")

    with pytest.raises(ValueError, match="only applies to enum tag mappings"):
        _parse_mappings({"t": {"kind": "tag", "tag_type": "bool",
                               "requires_values": ["x"]}})
    with pytest.raises(ValueError, match="only applies to enum tag mappings"):
        _parse_mappings({"b": {"kind": "blueprint", "requires_values": ["x"]}})


def test_binding_an_enum_missing_a_required_value_is_refused(tags):
    """§3.3: without this an action branching on `tier == "late"` has an undeclared
    dependency, and the failure shows up at run time as silently-skipped work."""
    tags.define(REG, "tier", "enum", ["early", "mid"])
    slot = MappingSlot("tier", kind="tag", tag_type="enum", registry_type=REG,
                       requires_values=("early", "late"))
    with pytest.raises(ValueError, match="late"):
        validate_binding(slot, tags.definition(REG, "tier"))


def test_semantics_are_at_least_not_exact(tags):
    """"the user's enum must *contain* the declared values but may have others" — adding a
    value must never invalidate an action that doesn't care about it."""
    tags.define(REG, "tier", "enum", ["early", "mid", "late", "end"])
    slot = MappingSlot("tier", kind="tag", tag_type="enum", registry_type=REG,
                       requires_values=("early", "late"))
    validate_binding(slot, tags.definition(REG, "tier"))       # must not raise


def test_a_mapping_declaring_nothing_accepts_any_enum(tags):
    tags.define(REG, "tier", "enum", ["whatever"])
    slot = MappingSlot("tier", kind="tag", tag_type="enum", registry_type=REG)
    validate_binding(slot, tags.definition(REG, "tier"))
