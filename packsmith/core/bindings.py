"""The mapping system — binding user artifacts into an action's declared slots
(design 7.1, 7.2).

An action declares typed slots ("a bool tag on ``minecraft:item``, likely ``remove``").
A step binds the user's own artifacts into those slots. This module validates a binding
against a slot's contract, resolves a whole step's bindings + config into what the runner
needs, and best-guesses bindings from the user's existing tags.

Four slot kinds work. ``kind='tag'`` validates on **tag type**, with lookups scoped to
the slot's ``registry_type`` (definitions are registry-scoped, design 3.2.1).
``kind='blueprint'`` binds one of the user's schemas and validates it **structurally**
against the mapping's ``required_shape`` (design 3.3 — see ``core/shapes.py``), so the
contract is about shape rather than the name the user happened to pick.
``blueprint_instance`` binds the instances themselves and is judged by the same shape, so
one mapping can hold instances from several schemas. ``registry_entry`` binds one Layer 1
entry and is typed by ``registry_type``.

Every kind honours ``cardinality``: ``one`` resolves to a scalar, ``many`` to a list.

``access`` enforcement (write-on-a-read-slot) is Phase 4.
"""
from dataclasses import dataclass

from packsmith.core.shapes import describe_shape, mismatches

SUPPORTED_KINDS = ("tag", "blueprint", "blueprint_instance", "registry_entry")


def parse_instance_ref(ref, blueprint_store):
    """``"StoneType:granite"`` -> ``("StoneType", "granite")``, or None if it resolves to
    nothing.

    Resolved by *lookup*, not by splitting on the first colon: blueprint and instance names
    only forbid dots and blanks, so a colon is legal in both and `A:B:C` is genuinely
    ambiguous. The longest blueprint name that actually owns a matching instance wins.
    """
    if not isinstance(ref, str):
        return None
    for name in sorted(blueprint_store.names(), key=len, reverse=True):
        prefix = f"{name}:"
        if not ref.startswith(prefix):
            continue
        instance = ref[len(prefix):]
        if any(i.name == instance for i in blueprint_store.instances(name)):
            return name, instance
    return None


# === WHAT A STORED BINDING POINTS AT (design 3.2.1's identity table) =========
#
# "Job step bindings (mappings) -> **id**. Internal, per-profile. Stable across renames."
# So a binding stores the artifact's surrogate id, and the **type is the discriminator**:
# an int is an id, a str is a legacy name written before this rule was enforced. No schema
# change, no wrapper object, and legacy rows keep resolving until something rewrites them.
#
# `registry_entry` is the deliberate exception: an entry id IS Layer 1 identity and cannot
# be renamed, so it stays the string it always was.
#
# NOTE saved View queries stay NAME-based on purpose (same table, opposite ruling — views
# are portable and an id means nothing in another profile). Do not "fix" them to match.


def binding_id(slot, artifact_name, *, tag_store=None, blueprint_store=None):
    """The value to STORE for a chosen artifact — its id, or the string for L1 entries."""
    if slot.kind in ("registry_entry", "pack"):
        # A pack has no id to store: its identity IS its directory name, and PackSmith does
        # not own that directory (design 3.3). Renaming it outside PackSmith therefore
        # breaks the binding — surfaced loudly at resolve time rather than papered over.
        return artifact_name
    if slot.kind == "tag":
        definition = tag_store.definition(slot.registry_type, artifact_name)
        return definition["id"] if definition else None
    if slot.kind == "blueprint":
        return blueprint_store.id_of(artifact_name)
    resolved = parse_instance_ref(artifact_name, blueprint_store)
    if resolved is None:
        return None
    blueprint, instance = resolved
    return blueprint_store.instance(blueprint, instance).id


def binding_name(slot, bound, *, tag_store=None, blueprint_store=None):
    """What a stored binding is *called* right now — for display, matching and validation.

    Returns None when the id points at something that no longer exists, which is a real
    state the UI has to show rather than crash on.
    """
    if bound is None or slot.kind in ("registry_entry", "pack"):
        return bound
    if isinstance(bound, str):
        return bound                       # legacy name, written before ids
    if slot.kind == "tag":
        definition = tag_store.definition_by_id(bound) if tag_store else None
        return definition["name"] if definition else None
    if slot.kind == "blueprint":
        return blueprint_store.name_of(bound) if blueprint_store else None
    instance = blueprint_store.instance_by_id(bound) if blueprint_store else None
    return instance.ref if instance else None


def validate_binding(slot, tag_definition, bound=None):
    """Raise ValueError if ``tag_definition`` doesn't satisfy ``slot``'s contract.

    ``tag_definition`` is what ``TagStore.definition(name)`` returns (or None); ``bound``
    is the name that was looked up, carried through only so a failure can say which tag
    it was talking about.
    """
    if slot.kind != "tag":
        raise ValueError(
            f"mapping '{slot.name}' is kind '{slot.kind}'; only 'tag' mappings are supported in the MVP")
    if tag_definition is None:
        # Name the tag. "The bound tag does not exist" is true and useless — the whole
        # question the user has is *which* tag, and the answer is almost always "the one
        # you just renamed", which the message can say for itself.
        named = f" '{bound}'" if bound else ""
        raise ValueError(
            f"mapping '{slot.name}': the bound tag{named} no longer exists on "
            f"{slot.registry_type} — it was renamed or deleted; re-bind this step")
    if slot.tag_type and tag_definition["type"] != slot.tag_type:
        raise ValueError(
            f"mapping '{slot.name}' needs a '{slot.tag_type}' tag, "
            f"but the bound tag is '{tag_definition['type']}'")
    # Design 3.3: an action that branches on `if tier == "late"` has an undeclared
    # dependency unless it says so — the mapping system would happily bind it to an enum
    # with no `late` and the failure would only show up at run time as silently-skipped
    # work. Semantics are AT-LEAST: the user's enum must contain these, and may have more.
    if slot.requires_values:
        absent = [v for v in slot.requires_values
                  if v not in (tag_definition.get("values") or [])]
        if absent:
            raise ValueError(
                f"mapping '{slot.name}' needs the value(s) {', '.join(absent)} on "
                f"'{tag_definition['name']}', which only has "
                f"{', '.join(tag_definition.get('values') or []) or 'none'}")


# A ``kind='blueprint'`` mapping binds one of the user's blueprint SCHEMAS, and exists for
# the same reason tag mappings do (design 3.3): an action must not name the user's
# artifacts. Without it a blueprint-consuming action has to write
# `pack.blueprints.gaps("StoneType", …)`, which works for exactly one person — the one who
# happened to call their schema StoneType.


def blueprint_mismatches(slot, blueprint_name, blueprint_store) -> list:
    """Why ``blueprint_name`` doesn't satisfy ``slot``, or [] if it does.

    Split out from validation because the job editor needs the *reasons* to show, not an
    exception: "this schema doesn't fit, and here is the slot that's wrong" is the thing
    that lets a user fix their schema, and hiding non-matching options would leave them
    guessing why their blueprint never appears.
    """
    if blueprint_name not in set(blueprint_store.names()):
        return [f"no blueprint named '{blueprint_name}'"]
    if not slot.required_shape:
        return []
    return mismatches(slot.required_shape, blueprint_store.slots(blueprint_name))


def blueprints_fitting(slot, blueprint_store) -> list:
    """Every schema that satisfies this mapping's shape, in the store's order."""
    return [name for name in blueprint_store.names()
            if not blueprint_mismatches(slot, name, blueprint_store)]


def instance_mismatches(slot, ref, blueprint_store) -> list:
    """Why instance ``ref`` doesn't satisfy ``slot``, or [] if it does.

    An instance is judged by its *schema's* shape — 3.3: "instances can come from multiple
    user-schemas as long as each satisfies the declared required_shape; the action's
    contract is about shape, not schema identity." So a `many` mapping can legitimately mix
    StoneType and MyRockKind instances in one binding.
    """
    resolved = parse_instance_ref(ref, blueprint_store)
    if resolved is None:
        return [f"no instance '{ref}'"]
    blueprint, _ = resolved
    if not slot.required_shape:
        return []
    return mismatches(slot.required_shape, blueprint_store.slots(blueprint))


def instances_fitting(slot, blueprint_store) -> list:
    """Every instance ref whose schema satisfies this mapping's shape."""
    return [instance.ref
            for name in blueprints_fitting(slot, blueprint_store)
            for instance in blueprint_store.instances(name)]


def mapping_mismatches(slot, bound, blueprint_store) -> list:
    """Whatever this mapping kind needs checked, checked. Blueprint kinds only."""
    if slot.kind == "blueprint_instance":
        return instance_mismatches(slot, bound, blueprint_store)
    return blueprint_mismatches(slot, bound, blueprint_store)


def validate_blueprint_binding(slot, bound, blueprint_store):
    """Raise ValueError unless the bound artifact satisfies the mapping's shape."""
    problems = mapping_mismatches(slot, bound, blueprint_store)
    if not problems:
        return
    detail = "; ".join(problems)
    if slot.required_shape:
        raise ValueError(
            f"mapping '{slot.name}': '{bound}' doesn't fit the shape this action "
            f"needs ({describe_shape(slot.required_shape)}) — {detail}")
    raise ValueError(f"mapping '{slot.name}': {detail}")


def entry_exists(registry_type, entry_id, packdump) -> bool:
    if packdump is None:
        return True             # nothing to check against; the runner validates for real
    registry = packdump.registry.get(registry_type)
    return bool(registry) and entry_id in registry.get("values", ())


def resolve_step(manifest, *, bindings: dict, config: dict, tag_store,
                 blueprint_store=None, packdump=None, pack_targets=None):
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
        chosen = _as_list(bound)
        # An empty `many` selection is treated as unbound, not as "bound to nothing": a
        # required mapping the user selected zero artifacts for cannot do its job, and
        # 3.3 says required mappings block execution when unbound.
        if not chosen:
            if slot.required:
                raise ValueError(f"required mapping '{name}' is unbound")
            resolved_mappings[name] = [] if slot.cardinality == "many" else None
            continue
        if slot.cardinality == "one" and len(chosen) > 1:
            raise ValueError(
                f"mapping '{name}' takes one artifact but {len(chosen)} are bound")

        values = []
        for stored in chosen:
            # An id is only an identity while the thing still exists. A dead id is loud
            # here rather than silently resolving to whatever now holds that name — which
            # is the whole reason 3.2.1 puts ids on bindings.
            item = binding_name(slot, stored, tag_store=tag_store,
                                blueprint_store=blueprint_store)
            if item is None:
                raise ValueError(
                    f"mapping '{name}' points at something that no longer exists "
                    f"(id {stored!r}) — re-bind this step")
            if slot.kind in ("blueprint", "blueprint_instance"):
                if blueprint_store is None:
                    raise ValueError(
                        f"mapping '{name}' binds a blueprint, but this step has no "
                        f"blueprint store")
                # Re-checked here, not just when the step was bound: a schema is a live
                # artifact, and the slot the action needs can be renamed or retyped
                # between binding this step and running it.
                validate_blueprint_binding(slot, item, blueprint_store)
                values.append(_instance_value(slot, item, blueprint_store))
            elif slot.kind == "registry_entry":
                if not entry_exists(slot.registry_type, item, packdump):
                    raise ValueError(
                        f"mapping '{name}': '{item}' is not in {slot.registry_type}")
                values.append(item)
            elif slot.kind == "pack":
                # Checked here rather than only when bound, for the same reason blueprint
                # shapes are: the pack is a directory PackSmith does not own, so it can be
                # renamed or deleted between binding this step and running it — and a write
                # into a pack that isn't there is a silent no-op, not an error Minecraft
                # reports.
                available = pack_targets.available(slot.pack_kind) \
                    if pack_targets is not None else None
                if available is None:
                    raise ValueError(
                        f"mapping '{name}' needs a {slot.pack_kind[:-1]}, but no pack "
                        f"loader is installed in this profile — see design 8.1")
                if item not in available:
                    raise ValueError(
                        f"mapping '{name}': there is no {slot.pack_kind[:-1]} called "
                        f"'{item}' any more — re-bind this step")
                values.append(item)
            else:
                validate_binding(slot, tag_store.definition(slot.registry_type, item),
                                 bound=item)
                values.append(item)
        resolved_mappings[name] = values if slot.cardinality == "many" else values[0]

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


def _as_list(bound) -> list:
    """A binding is a scalar or a list depending on cardinality; work in lists either way."""
    if bound is None:
        return []
    if isinstance(bound, (list, tuple)):
        return [item for item in bound if item is not None]
    return [bound]


def _instance_value(slot, bound, blueprint_store):
    """What the action receives. Schemas arrive as their name; instances arrive **split**,
    because every blueprint API takes blueprint and instance separately and making the
    author re-split a ref string would be handing them back a problem we already solved."""
    if slot.kind != "blueprint_instance":
        return bound
    blueprint, instance = parse_instance_ref(bound, blueprint_store)
    return {"blueprint": blueprint, "instance": instance, "ref": bound}


def policy_key(kind: str, scope, name) -> tuple:
    """The identity a conflict policy is stored and looked up under.

    A bare name is not an identity (design 3.2.1: tag definitions are registry-scoped, and
    names are labels). Keyed by name alone, three different artifacts collided into one
    policy: the same tag name on ``minecraft:item`` and ``minecraft:block``, and — worse
    across the namespace boundary — a tag called ``palette`` and a *blueprint* called
    ``palette``, which share nothing but a string yet shared a rule about who may overwrite
    whom.
    """
    return (kind, scope, name)


def conflict_policies_for(manifest, mappings: dict) -> dict:
    """Map each bound artifact to the conflict policy its slot declared (design 3.3).

    The runtime enforces policy per *artifact*, because that's what an action names when it
    writes (``pack.tags.write(..., tag_name, ...)``), while the declaration lives on the
    *slot*. This is the translation between the two.

    Two slots binding the same artifact with **different** policies is refused rather than
    resolved: 3.3 makes the declaration mandatory precisely so the choice is never implicit,
    and letting whichever mapping iterated last win would be the silent default the rule
    exists to forbid.
    """
    policies = {}
    for name, slot in manifest.mappings.items():
        if not slot.conflict_policy:
            continue
        for bound in _as_list(mappings.get(name)):
            if slot.kind in ("blueprint", "blueprint_instance"):
                # An instance mapping resolves to {blueprint, instance, ref}, but the
                # blueprint runtime asks per BLUEPRINT — binding two instances of one
                # schema is still one policy.
                artifact = bound["blueprint"] if isinstance(bound, dict) else bound
                key = policy_key("blueprint", None, artifact)
            else:
                key = policy_key("tag", slot.registry_type, bound)
            existing = policies.get(key)
            if existing is not None and existing != slot.conflict_policy:
                raise ValueError(
                    f"mappings '{name}' and another both bind {key[2]!r} but declare "
                    f"different conflict policies ('{slot.conflict_policy}' vs "
                    f"'{existing}') — one artifact cannot have two rules")
            policies[key] = slot.conflict_policy
    return policies


def best_guess_bindings(manifest, tag_store, blueprint_store=None, packdump=None,
                        pack_targets=None) -> dict:
    """Suggest a binding per mapping slot from the user's existing tags: prefer an
    exact ``likely_name`` match that's type-compatible, else the first type-compatible
    tag, else None. Candidates are drawn from the slot's own ``registry_type`` (definitions
    are registry-scoped). Returns slot-name → suggested tag name (or None)."""
    suggestions = {}
    for name, slot in manifest.mappings.items():
        if slot.kind in ("blueprint", "blueprint_instance"):
            # Only ever guess something that actually FITS. Guessing a misfit is worse than
            # guessing nothing: the step looks bound and fails at run time.
            if blueprint_store is None:
                fitting = []
            elif slot.kind == "blueprint_instance":
                fitting = instances_fitting(slot, blueprint_store)
            else:
                fitting = blueprints_fitting(slot, blueprint_store)
            def _id(chosen):
                return binding_id(slot, chosen, tag_store=tag_store,
                                  blueprint_store=blueprint_store)

            if slot.cardinality == "many":
                # "zero or more" — the useful default for a bulk action is everything that
                # qualifies, which the user then narrows.
                suggestions[name] = [_id(f) for f in fitting]
            else:
                pick = (slot.likely_name if slot.likely_name in fitting
                        else (fitting[0] if fitting else None))
                suggestions[name] = _id(pick) if pick is not None else None
            continue
        if slot.kind == "pack":
            # 3.3's best-guess fill: prefer the author's hint when the user actually has a
            # pack by that name, else pre-select when there is exactly one — with several,
            # which one an override lands in is a real decision and guessing is worse than
            # asking.
            packs = pack_targets.available(slot.pack_kind) if pack_targets else None
            packs = packs or []
            pick = (slot.likely_name if slot.likely_name in packs
                    else (packs[0] if len(packs) == 1 else None))
            suggestions[name] = ([pick] if pick else []) \
                if slot.cardinality == "many" else pick
            continue
        if slot.kind == "registry_entry":
            # `likely_name` is the author's hint at an id. Offered only if the pack
            # actually has it — same rule as everywhere else, for the same reason: a
            # suggestion that fails at run time is worse than no suggestion.
            guess = slot.likely_name
            ok = guess and entry_exists(slot.registry_type, guess, packdump)
            suggestions[name] = [guess] if (ok and slot.cardinality == "many") else (
                guess if ok else ([] if slot.cardinality == "many" else None))
            continue
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
        suggestions[name] = (binding_id(slot, pick, tag_store=tag_store)
                             if pick is not None else None)
    return suggestions


def _type_ok(slot, definition) -> bool:
    return slot.kind == "tag" and (not slot.tag_type or definition["type"] == slot.tag_type)


def record_names(manifest, bindings: dict, *, tag_store=None) -> dict:
    """Snapshot what each bound TAG is called, at the moment the step is saved.

    This is the whole mechanism behind §3.2.1's "bound job steps must be explicitly
    relinked" after a rename. A binding stores an id, and a rename doesn't change the id —
    so nothing is mechanically broken and there is no natural signal that the step's
    *meaning* moved. Comparing the name recorded here against the tag's current name is
    that signal, and it's derived rather than flagged: the rename is one UPDATE, and every
    step's answer changes with it, atomically. Nothing to propagate, nothing to miss.

    **Tags only.** §3.2.2 makes a blueprint rename explicitly non-destructive — bindings
    are keyed by id and a rename is "a metadata-only operation" — so recording blueprint
    names here would gate jobs on a mutation the design says is silent.
    """
    recorded = {}
    for name, slot in manifest.mappings.items():
        if slot.kind != "tag":
            continue
        bound = bindings.get(name)
        if bound is None:
            continue
        chosen = _as_list(bound)
        names = [binding_name(slot, one, tag_store=tag_store) for one in chosen]
        names = [n for n in names if n is not None]
        if not names:
            continue
        recorded[name] = names if isinstance(bound, list) else names[0]
    return recorded


@dataclass(frozen=True)
class StaleBinding:
    """A step bound to a tag that has since been renamed."""
    step_id: int
    job_name: str
    position: int
    action_ref: str
    slot: str
    was: str            # what it was called when it was bound
    now: str            # what that same tag is called today

    def describe(self) -> str:
        return (f"{self.job_name} step {self.position + 1} ({self.action_ref}): "
                f"'{self.slot}' was bound to '{self.was}', now called '{self.now}'")


def stale_bindings(job, *, package_index, tag_store) -> list:
    """Every binding in ``job`` whose tag has been renamed since it was bound.

    Empty for a job with nothing to relink, so callers can treat it as a boolean.
    """
    found = []
    for step in job.steps:
        if not step.is_action or not step.bound_names:
            continue
        try:
            manifest = package_index.get(step.action_ref)
        except (KeyError, AttributeError):
            continue        # uninstalled package: not our problem to report here
        for slot_name, slot in manifest.mappings.items():
            if slot.kind != "tag":
                continue
            was = step.bound_names.get(slot_name)
            if was is None:
                continue    # never recorded: written before rename existed
            bound = step.bindings.get(slot_name)
            current = [binding_name(slot, one, tag_store=tag_store)
                       for one in _as_list(bound)]
            for old, now in zip(_as_list(was), current):
                if now is not None and old != now:
                    found.append(StaleBinding(
                        step_id=step.id, job_name=job.name, position=step.position,
                        action_ref=step.action_ref, slot=slot_name, was=old, now=now))
    return found


@dataclass(frozen=True)
class StepProblem:
    """Why one step cannot run, known without running it."""
    step_id: int
    position: int
    action_ref: str
    detail: str
    kind: str           # "relink" | "broken"

    @property
    def needs_relink(self) -> bool:
        """Relink problems are answerable in one click; broken ones need a real re-bind."""
        return self.kind == "relink"


def step_problems(job, *, package_index, tag_store, blueprint_store=None,
                  packdump=None, pack_targets=None) -> list:
    """Everything that would stop ``job`` running, determined WITHOUT running it.

    One function so that three consumers cannot disagree: the pre-flight gate, the Errors
    panel, and the Jobs panel's "this won't run" colouring. A job painted as runnable that
    then refuses is worse than no colouring at all.

    Two kinds, because they need different things from the user:

    - **relink** — the tag was renamed and the binding still resolves (it holds an id).
      Nothing is broken; the user confirms the step still means what they want.
    - **broken** — the binding does not resolve at all: a legacy name-based binding whose
      tag was renamed out from under it, a deleted artifact, a shape that no longer fits.
      This needs a real re-bind, and it is why the two are not merged.
    """
    problems = []
    for stale in stale_bindings(job, package_index=package_index, tag_store=tag_store):
        problems.append(StepProblem(
            step_id=stale.step_id, position=stale.position, action_ref=stale.action_ref,
            detail=(f"'{stale.slot}' was bound to '{stale.was}', now called "
                    f"'{stale.now}'"),
            kind="relink"))

    # Deliberately NOT de-duplicated by step. A step can be both — renamed *and* missing a
    # value its action declares — and suppressing the second would offer a one-click relink
    # that leaves the step just as unrunnable, which is a worse failure than saying two
    # things. In the ordinary case the stale step still resolves fine, so nothing doubles.
    for step in job.steps:
        if not step.is_action:
            continue
        try:
            manifest = package_index.get(step.action_ref)
        except (KeyError, AttributeError):
            problems.append(StepProblem(
                step_id=step.id, position=step.position, action_ref=step.action_ref,
                detail=f"'{step.action_ref}' is not installed", kind="broken"))
            continue
        try:
            resolve_step(manifest, bindings=step.bindings, config=step.config,
                         tag_store=tag_store, blueprint_store=blueprint_store,
                         packdump=packdump, pack_targets=pack_targets)
        except ValueError as e:
            problems.append(StepProblem(
                step_id=step.id, position=step.position, action_ref=step.action_ref,
                detail=str(e), kind="broken"))
    return problems
