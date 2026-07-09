"""The mapping system — binding user artifacts into an action's declared slots
(design 7.1, 7.2).

An action declares typed slots ("a bool tag on ``minecraft:item``, likely ``remove``").
A step binds the user's own artifacts into those slots. This module validates a binding
against a slot's contract, resolves a whole step's bindings + config into what the runner
needs, and best-guesses bindings from the user's existing tags.

MVP scope: only ``kind='tag'`` slots. Validation matches on **tag type**, and tag lookups
are scoped to the slot's ``registry_type`` (definitions are registry-scoped, design 3.2.1).
``access`` enforcement (write-on-a-read-slot) is Phase 4.
"""


def validate_binding(slot, tag_definition):
    """Raise ValueError if ``tag_definition`` doesn't satisfy ``slot``'s contract.
    ``tag_definition`` is what ``TagStore.definition(name)`` returns (or None)."""
    if slot.kind != "tag":
        raise ValueError(
            f"mapping '{slot.name}' is kind '{slot.kind}'; only 'tag' mappings are supported in the MVP")
    if tag_definition is None:
        raise ValueError(f"mapping '{slot.name}': the bound tag does not exist")
    if slot.tag_type and tag_definition["type"] != slot.tag_type:
        raise ValueError(
            f"mapping '{slot.name}' needs a '{slot.tag_type}' tag, "
            f"but the bound tag is '{tag_definition['type']}'")


def resolve_step(manifest, *, bindings: dict, config: dict, tag_store):
    """Validate a step's bindings + config against the manifest and return the
    ``(mappings, config)`` pair the runner consumes. Raises on an unbound required
    mapping, a type mismatch, or a missing required config value.

    ``bindings`` maps slot-name → the user's tag name; the returned ``mappings`` maps
    slot-name → that same tag name (what ``pack.step.mappings[slot]`` yields), or None
    for an unbound optional slot.
    """
    resolved_mappings = {}
    for name, slot in manifest.mappings.items():
        bound = bindings.get(name)
        if bound is None:
            if slot.required:
                raise ValueError(f"required mapping '{name}' is unbound")
            resolved_mappings[name] = None
            continue
        validate_binding(slot, tag_store.definition(slot.registry_type, bound))
        resolved_mappings[name] = bound

    resolved_config = {}
    for name, param in manifest.config.items():
        if name in config:
            resolved_config[name] = config[name]
        elif param.default is not None:
            resolved_config[name] = param.default
        elif param.required:
            raise ValueError(f"required config '{name}' is unset")
        else:
            resolved_config[name] = None

    return resolved_mappings, resolved_config


def best_guess_bindings(manifest, tag_store) -> dict:
    """Suggest a binding per mapping slot from the user's existing tags: prefer an
    exact ``likely_name`` match that's type-compatible, else the first type-compatible
    tag, else None. Candidates are drawn from the slot's own ``registry_type`` (definitions
    are registry-scoped). Returns slot-name → suggested tag name (or None)."""
    suggestions = {}
    for name, slot in manifest.mappings.items():
        definitions = tag_store.definitions_for(slot.registry_type)
        pick = None
        if slot.likely_name and slot.likely_name in definitions:
            if _type_ok(slot, definitions[slot.likely_name]):
                pick = slot.likely_name
        if pick is None:
            for tag_name, definition in definitions.items():
                if _type_ok(slot, definition):
                    pick = tag_name
                    break
        suggestions[name] = pick
    return suggestions


def _type_ok(slot, definition) -> bool:
    return slot.kind == "tag" and (not slot.tag_type or definition["type"] == slot.tag_type)
