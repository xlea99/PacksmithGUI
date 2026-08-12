"""Which job steps a blueprint schema change would break (design 3.2.2).

3.2.2: "When a user confirms a rename, retype, or removal on a blueprint slot, Packsmith
finds all action mappings whose `required_shape` references the mutated blueprint schema.
Each affected mapping is re-validated against the new schema shape... The user makes an
informed choice **before the mutation commits.**"

That last clause is the whole point. The run-time check added with `required_shape` already
refuses a step whose schema drifted — but it refuses *later*, at a moment disconnected from
the edit that caused it, when the user has long forgotten renaming a slot. This module moves
the same knowledge in front of the decision.

The mutation is applied to a **projection** of the slot list rather than to the database, so
the question "what would this break?" can be answered without breaking anything. Nothing
here writes.
"""
from dataclasses import dataclass, replace

from packsmith.core.bindings import binding_name
from packsmith.core.shapes import mismatches


@dataclass(frozen=True)
class BrokenStep:
    """One job step that a schema no longer satisfies."""
    job_name: str
    position: int               # 1-based, as the user counts steps
    action_ref: str
    mapping: str
    blueprint: str
    problems: tuple

    def describe(self) -> str:
        return f"{self.job_name} step {self.position} ({self.action_ref})"

    def detail(self) -> str:
        return f"{self.describe()}: {'; '.join(self.problems)}"


def project_removal(slots, path) -> list:
    """The slot list as it would be with ``path`` (and anything under it) gone."""
    return [s for s in slots if s.path != path and not s.path.startswith(f"{path}.")]


def project_rename(slots, path, new_name) -> list:
    """The slot list as it would be with ``path`` renamed — descendants move with it,
    because a rename is a metadata change on one row and paths are derived."""
    parent = path.rsplit(".", 1)[0] if "." in path else ""
    new_path = f"{parent}.{new_name}" if parent else new_name
    projected = []
    for slot in slots:
        if slot.path == path:
            projected.append(replace(slot, name=new_name, path=new_path))
        elif slot.path.startswith(f"{path}."):
            projected.append(replace(slot, path=new_path + slot.path[len(path):]))
        else:
            projected.append(slot)
    return projected


def project_retype(slots, path, type, *, registry_type=None, ref_blueprint=None,
                   enum_values=()) -> list:
    """The slot list as it would be with ``path`` holding a different type."""
    return [replace(slot, type=type, registry_type=registry_type,
                    ref_blueprint=ref_blueprint, enum_values=tuple(enum_values or ()))
            if slot.path == path else slot
            for slot in slots]


def broken_steps(blueprint, projected_slots, *, job_store, package_index,
                 blueprint_store=None) -> list:
    """Every step bound to ``blueprint`` that ``projected_slots`` would no longer satisfy.

    Scans all jobs rather than a subset: a mapping is bound per *step*, so "which steps
    care about this schema" is only answerable by looking at every step that exists.
    """
    found = []
    for job in job_store.all():
        for index, step in enumerate(job_store.steps_of(job.id), start=1):
            if step.kind != "action" or not step.action_ref:
                continue
            manifest = _manifest(package_index, step.action_ref)
            if manifest is None:
                continue            # package uninstalled; the step is already unrunnable
            for name, slot in manifest.mappings.items():
                if not _targets(slot, step.bindings.get(name), blueprint,
                                blueprint_store):
                    continue
                problems = mismatches(slot.required_shape, projected_slots)
                if problems:
                    found.append(BrokenStep(
                        job_name=job.name, position=index, action_ref=step.action_ref,
                        mapping=name, blueprint=blueprint, problems=tuple(problems)))
    return found


def currently_broken(blueprint_store, *, job_store, package_index) -> list:
    """Every step that is broken **right now** — the flag, not the forecast.

    3.2.2: a step whose mapping no longer validates "is flagged as needing attention" and
    "refuses to run until the user re-binds or the schema is reverted". The refusal is
    enforced in `resolve_step`; this is what lets the UI say so before you press run.
    """
    broken = []
    for name in blueprint_store.names():
        broken += broken_steps(name, blueprint_store.slots(name),
                               job_store=job_store, package_index=package_index,
                               blueprint_store=blueprint_store)
    return broken


def steps_declaring_values(registry_type, tag_name, values, *, job_store,
                          package_index, tag_store=None) -> list:
    """Every bound step whose mapping declares one of ``values`` in `requires_values`.

    3.2.1 requires the enum-removal confirmation to state BOTH blast radii — the
    assignments that become orphans, and the automations that break. The second half was
    unreportable because the field was never parsed, so removing a value a job depended on
    was silent until the run. 3.3: this is "deterministic precisely *because* actions
    declare the values they depend on".
    """
    wanted = set(values)
    found = []
    for job in job_store.all():
        for index, step in enumerate(job_store.steps_of(job.id), start=1):
            if step.kind != "action" or not step.action_ref:
                continue
            manifest = _manifest(package_index, step.action_ref)
            if manifest is None:
                continue
            for name, slot in manifest.mappings.items():
                if slot.kind != "tag" or not slot.requires_values:
                    continue
                if slot.registry_type and slot.registry_type != registry_type:
                    continue
                bound = step.bindings.get(name)
                if bound is None:
                    continue
                # Bindings hold ids (3.2.1); legacy rows hold names. Compare through the
                # same resolver everything else uses.
                if binding_name(slot, bound, tag_store=tag_store) != tag_name:
                    continue
                needed = sorted(wanted & set(slot.requires_values))
                if needed:
                    found.append(BrokenStep(
                        job_name=job.name, position=index, action_ref=step.action_ref,
                        mapping=name, blueprint=tag_name,
                        problems=tuple(f"declares '{v}'" for v in needed)))
    return found


def _targets(slot, bound, blueprint, blueprint_store) -> bool:
    """Is this binding pointed at ``blueprint``? Covers both blueprint kinds, both
    cardinalities, and both storage forms — bindings hold ids now (design 3.2.1) but legacy
    rows still hold names, so this has to compare through `binding_name` rather than
    against the raw stored value."""
    if slot.kind not in ("blueprint", "blueprint_instance"):
        return False
    items = bound if isinstance(bound, (list, tuple)) else [bound]
    for item in items:
        name = binding_name(slot, item, blueprint_store=blueprint_store)
        if name is None:
            continue
        if slot.kind == "blueprint" and name == blueprint:
            return True
        if slot.kind == "blueprint_instance" and name.startswith(f"{blueprint}:"):
            return True
    return False


def _manifest(package_index, action_ref):
    try:
        return package_index.get(action_ref)
    except (KeyError, ValueError):
        return None


def summarise(broken, *, mutation: str) -> str:
    """The sentence 3.2.2 asks for, naming the steps rather than counting them.

    A count alone ("3 mappings will break") is not an informed choice — the user needs to
    know *which* jobs, because that is what decides whether the rename is worth it.
    """
    if not broken:
        return ""
    named = "\n".join(f"  • {b.describe()} — {'; '.join(b.problems)}" for b in broken[:8])
    more = f"\n  …and {len(broken) - 8} more" if len(broken) > 8 else ""
    plural = "step" if len(broken) == 1 else "steps"
    return (f"This {mutation} will invalidate {len(broken)} job {plural}:\n{named}{more}\n\n"
            f"Those steps will refuse to run until you re-bind them or revert the schema.")
