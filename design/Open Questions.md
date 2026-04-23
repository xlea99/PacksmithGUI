## Open Questions

These are questions that don't have a clean home in any specific section — they're
cross-cutting, genuinely new surfaces, or process-level concerns that sit outside the
architecture proper. Section-scoped questions live in each section's own "Live
Questions" subsection.

### Global Search

"Find any item, block, entity, tag, view, blueprint, or file by name across the whole
profile" — there's no specified way to do this. Filter bars are per-view. A global
search / command palette (Ctrl+Shift+P pattern — fuzzy search over every action, entity,
and setting in the app) would be essential for a tool at this complexity level. Not
covered by any existing section — it would be a new top-level UI surface.

### Settings / Preferences

Beyond `max_packdump_snapshot_count`, there's no user-settings system discussed. Themes,
keybinds, default behaviors, font size, etc. Not architecturally complex but needs a
design pass. No clear home in existing sections — would likely be its own §4.x.

### Testing Strategy

How does PackSmith get tested? The ownership model, action API, round-trip parsers, and
blueprint system all have complex invariants. Needs at minimum a strategy sketch — unit
tests, integration tests, what gets mocked vs. real. Process-level concern, not a
feature of any one section.
