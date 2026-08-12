"""Turning a raw packdump comparison into something a person can read — design 3.1/4.1.

`Packdump.compare` answers in terms of *which side* a thing was on: `only_in_self`,
`only_in_other`. That is the right shape for a comparison of two arbitrary snapshots and
the wrong one for a review screen, where the question is always "what did this update do
to my pack" — added, removed, changed.

Getting the direction backwards is the obvious bug here and an easy one, because both
answers look plausible: a screen confidently reporting 214 removals when 214 entries were
added is wrong in a way nobody spots by eye. So the translation happens once, here, with
the convention stated and tested rather than remembered:

    old.compare(new)  ->  only_in_self = gone from the new dump   = REMOVED
                          only_in_other = new in the new dump     = ADDED
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RegistryChange:
    """What one registry gained and lost."""
    registry_type: str
    added: tuple = ()
    removed: tuple = ()

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed)

    def headline(self) -> str:
        parts = []
        if self.added:
            parts.append(f"+{len(self.added):,}")
        if self.removed:
            parts.append(f"−{len(self.removed):,}")
        return " ".join(parts) or "no change"


@dataclass(frozen=True)
class ModChange:
    """A mod that arrived, left, or moved version."""
    mod_id: str
    kind: str                  # "added" | "removed" | "updated"
    old_version: str = None
    new_version: str = None

    def headline(self) -> str:
        if self.kind == "added":
            return f"added ({self.new_version})" if self.new_version else "added"
        if self.kind == "removed":
            return f"removed (was {self.old_version})" if self.old_version else "removed"
        return f"{self.old_version} → {self.new_version}"


@dataclass
class DiffSummary:
    """A whole comparison, in the direction a review screen asks about."""
    registries: list = field(default_factory=list)      # [RegistryChange]
    mods: list = field(default_factory=list)            # [ModChange]
    registries_added: tuple = ()                        # whole registries that appeared
    registries_removed: tuple = ()
    identity: dict = field(default_factory=dict)        # mc_version / loader / loader_version
    locales_added: tuple = ()
    locales_removed: tuple = ()
    renamed: int = 0                                    # display names that changed

    @property
    def entries_added(self) -> int:
        return sum(len(change.added) for change in self.registries)

    @property
    def entries_removed(self) -> int:
        return sum(len(change.removed) for change in self.registries)

    @property
    def empty(self) -> bool:
        return not (self.registries or self.mods or self.identity
                    or self.registries_added or self.registries_removed
                    or self.locales_added or self.locales_removed or self.renamed)

    def headline(self) -> str:
        """One line for the bottom strip. Says nothing rather than "0 changes" when there
        is nothing, because a summary of nothing is noise."""
        if self.empty:
            return "no changes"
        parts = []
        if self.entries_added:
            parts.append(f"+{self.entries_added:,} entries")
        if self.entries_removed:
            parts.append(f"−{self.entries_removed:,} entries")
        if self.mods:
            parts.append(f"{len(self.mods)} mod{'s' if len(self.mods) != 1 else ''}")
        if self.renamed:
            parts.append(f"{self.renamed:,} renamed")
        return "  ·  ".join(parts) or "metadata only"


def summarise_diff(diff: dict) -> DiffSummary:
    """Normalise ``old.compare(new)`` into added/removed terms.

    Takes the raw dict rather than the two dumps so a stored diff — the one an
    ``ImportResult`` carries — can be re-read later without both snapshots still existing.
    """
    summary = DiffSummary()
    for field_name in ("mc_version", "loader", "loader_version"):
        if field_name in diff:
            was, now = diff[field_name]
            summary.identity[field_name] = (was, now)

    registries = diff.get("registries") or {}
    summary.registries_removed = tuple(sorted(registries.get("only_in_self") or ()))
    summary.registries_added = tuple(sorted(registries.get("only_in_other") or ()))
    for registry_type, change in sorted((registries.get("changed") or {}).items()):
        summary.registries.append(RegistryChange(
            registry_type=registry_type,
            # `only_in_other` is what the NEW dump has and the old one didn't.
            added=tuple(sorted(change.get("only_in_other") or ())),
            removed=tuple(sorted(change.get("only_in_self") or ())),
        ))

    mods = diff.get("mods") or {}
    for mod_id in sorted(mods.get("only_in_other") or ()):
        summary.mods.append(ModChange(mod_id=mod_id, kind="added"))
    for mod_id in sorted(mods.get("only_in_self") or ()):
        summary.mods.append(ModChange(mod_id=mod_id, kind="removed"))
    for mod_id, changes in sorted((mods.get("changed") or {}).items()):
        was, now = changes.get("version", (None, None))
        summary.mods.append(ModChange(mod_id=mod_id, kind="updated",
                                      old_version=was, new_version=now))

    localizations = diff.get("localizations") or {}
    summary.locales_removed = tuple(sorted(localizations.get("only_in_self") or ()))
    summary.locales_added = tuple(sorted(localizations.get("only_in_other") or ()))
    for _locale, per_registry in (localizations.get("changed") or {}).items():
        for _registry, entries in (per_registry.get("changed") or {}).items():
            summary.renamed += len(entries)
    return summary


def tags_at_risk(tag_store, packdump) -> list:
    """Assignments that would be orphaned by adopting ``packdump``.

    §4.1 asks the tab to "surface orphaned tags at risk". This is the same derived orphan
    check the Errors panel uses (§3.2.1) — pointed at a dump that may not be active yet,
    which is what makes it a *warning* rather than a report.
    """
    return [orphan for orphan in tag_store.find_orphans(packdump)
            if orphan.reason == "missing_entry"]
